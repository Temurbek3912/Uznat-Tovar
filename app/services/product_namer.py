"""
«Назвать любой товар» (документ 07, §10.6): товара нет в базе магазина — программа всё равно пытается сказать, что это.

Порядок (каждый следующий шаг — только если предыдущий не дал ответа):
  1. штрихкод на кадре -> открытые базы товаров (Open Food Facts, Open Beauty Facts, Open Products Facts). Наружу уходит ТОЛЬКО
     номер штрихкода, кадр — нет. Ответы запоминаются в памяти процесса, чтобы не спрашивать одно и то же;
  2. локальная модель «зрение + текст» с OpenAI-совместимым входом (LM Studio на этом компьютере). Кадр уходит только на
     настроенный адрес (по умолчанию 127.0.0.1) и только в памяти — на диск не пишется (D-10а);
  3. иначе — честное «не удалось определить».

Модель должна сама сказать, насколько уверена. «low», «не товар», пустое название — это «не удалось определить», а не догадка.
Название из этого модуля НИКОГДА не выдаётся за товар из базы: в ответе оно идёт отдельным полем guess (статус not_in_catalog).

Внешний мир (HTTP) подаётся функцией http_post/http_get — в тестах подменяется, сеть не нужна.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

USER_AGENT = "UznatTovar/1.0 (store product lookup)"
OPEN_DBS = (
    "https://world.openfoodfacts.org",
    "https://world.openbeautyfacts.org",
    "https://world.openproductsfacts.org",
)
BARCODE_TIMEOUT = 4.0
VISION_MAX_SIDE = 896            # кадр уменьшается перед отправкой модели: быстрее, а название на упаковке ещё читается
CACHE_SIZE = 1024
LANG_NAMES = {"ru": "Russian", "uz": "Uzbek (Latin script)", "uz-Cyrl": "Uzbek (Cyrillic script)", "en": "English"}
_CODE_RE = re.compile(r"^\d{8,14}$")

HttpGet = Callable[[str, float], dict | None]
HttpPost = Callable[[str, dict, float], dict]


@dataclass(frozen=True)
class Guess:
    name: str
    brand: str = ""
    source: str = "vision"       # barcode_db | vision
    sure: bool = False           # штрихкод из открытой базы или модель ответила «high»: можно спорить с поиском по виду

    def as_dict(self) -> dict:
        return {"name": self.name, "brand": self.brand, "source": self.source}


def _http_get_json(url: str, timeout: float) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 — адреса фиксированы в OPEN_DBS
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _http_post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:       # noqa: S310 — адрес из настроек
        return json.loads(r.read().decode("utf-8"))


class _Lru:
    def __init__(self, size: int = CACHE_SIZE):
        self.size = size
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return True, self._d[key]
            return False, None

    def put(self, key, value) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)


_barcode_cache = _Lru()


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:160]


def _product_name(product: dict, lang: str) -> str:
    short = lang.split("-")[0]
    for key in (f"product_name_{short}", "product_name", "product_name_en", "product_name_ru", "generic_name"):
        name = _clean(product.get(key))
        if name:
            return name
    return ""


def _pick_brand(brands, name: str) -> str:
    """«Mars Wrigley Confectionery UK Limited, Snickers» -> «Snickers»: бренд, который есть в названии, иначе самый короткий."""
    items = [_clean(b) for b in str(brands or "").split(",") if _clean(b)]
    if not items:
        return ""
    low = name.lower()
    for item in items:
        if item.lower() in low:
            return item
    return min(items, key=len)


_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "j", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "s",
    "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ў": "o", "қ": "q", "ғ": "g",
    "ҳ": "h", "c": "k", "ʻ": "", "'": "",
})
_STOP = {"the", "and", "with", "for", "original", "classic", "ml", "gr", "kg", "pcs"}


def _tokens(text: str) -> set[str]:
    text = (text or "").lower().translate(_TRANSLIT)
    return {t for t in re.findall(r"[a-z]{3,}", text) if t not in _STOP}


def names_overlap(guess: Guess, candidate: dict) -> bool:
    """Есть ли у названия от модели/открытой базы общее слово с товаром из базы (латиница и кириллица сравниваются в одной записи:
    «Кока-Кола» и «Coca-Cola» совпадают). Нужна, чтобы не спорить с поиском по виду, когда речь об одном и том же товаре."""
    mine = _tokens(f"{guess.name} {guess.brand}")
    theirs = _tokens(f"{candidate.get('name', '')} {candidate.get('brand', '')}")
    return bool(mine & theirs)


def lookup_barcode(code: str, lang: str = "ru", http_get: HttpGet | None = None) -> Guess | None:
    """Штрихкод -> название из открытых баз товаров или None. Ошибки сети — None (тихо, в журнал без подробностей кадра)."""
    if not _CODE_RE.match(code or ""):
        return None
    hit, cached = _barcode_cache.get((code, lang))
    if hit:
        return cached
    get = http_get or _http_get_json
    guess = None
    failed = False
    for base in OPEN_DBS:
        url = f"{base}/api/v2/product/{code}.json?fields=product_name,product_name_{lang.split('-')[0]},product_name_en," \
              f"product_name_ru,generic_name,brands"
        try:
            data = get(url, BARCODE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — нет интернета, база не отвечает: пробуем следующую
            logger.info("Открытая база товаров %s не ответила (%s)", base, type(exc).__name__)
            failed = True
            continue
        if not data or data.get("status") != 1 or not isinstance(data.get("product"), dict):
            continue
        name = _product_name(data["product"], lang)
        if name:
            guess = Guess(name=name, brand=_pick_brand(data["product"].get("brands"), name), source="barcode_db", sure=True)
            break
    if guess is not None or not failed:          # «не нашли» запоминаем, а сбой сети — нет: в следующий раз спросим снова
        _barcode_cache.put((code, lang), guess)
    return guess


VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_product": {"type": "boolean"},
        "name": {"type": "string"},
        "brand": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["is_product", "name", "brand", "confidence"],
}


def _prompt(lang: str) -> str:
    language = LANG_NAMES.get(lang, "Russian")
    return (
        "You see a photo taken by a shopper in a store. Identify the single main retail product in the photo. "
        "Read the brand and the product name from the packaging if visible. "
        f"Answer in {language}: 'name' is a short product name a shopper would understand, e.g. brand + product type + "
        "variant/size if visible (keep brand names as printed). 'brand' is the brand as printed, or empty. "
        "Set 'confidence' to 'high' only if you can clearly read or recognise the product; 'medium' if you know the product type "
        "but not exactly; 'low' if unsure. If there is no product in the photo (person, floor, blur), set is_product=false. "
        "Never invent a brand you cannot see. Reply with JSON only."
    )


def _shrink_jpeg(jpeg: bytes, max_side: int = VISION_MAX_SIDE) -> bytes:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return jpeg
    with Image.open(io.BytesIO(jpeg)) as im:  # exif-ok: кадр уже повёрнут по EXIF и пересохранён (normalize_photo)
        im = im.convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=88)
        return out.getvalue()


def _parse_answer(text: str) -> dict | None:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def name_by_vision(jpeg: bytes, lang: str, url: str, model: str, timeout: float,
                   http_post: HttpPost | None = None) -> Guess | None:
    """Кадр -> название от локальной модели «зрение + текст» или None (не уверена, не товар, модель недоступна)."""
    if not url:
        return None
    post = http_post or _http_post_json
    image = base64.b64encode(_shrink_jpeg(jpeg)).decode("ascii")
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 300,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _prompt(lang)},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
        ]}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "product", "strict": True, "schema": VISION_SCHEMA}},
    }
    del image
    try:
        reply = post(url.rstrip("/") + "/chat/completions", payload, timeout)
    except Exception as exc:  # noqa: BLE001 — LM Studio не запущен, модель не загружена, долго думает
        logger.warning("Модель «зрение + текст» недоступна (%s) — товар вне базы не назван", type(exc).__name__)
        return None
    finally:
        payload.clear()
    try:
        content = reply["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    answer = _parse_answer(content if isinstance(content, str) else "")
    if not answer or answer.get("is_product") is False:
        return None
    if str(answer.get("confidence", "")).lower() not in ("high", "medium"):
        return None
    name = _clean(answer.get("name"))
    if not name:
        return None
    sure = str(answer.get("confidence", "")).lower() == "high"
    return Guess(name=name, brand=_clean(answer.get("brand")), source="vision", sure=sure)


def name_product(jpeg: bytes | None, codes: list[str], lang: str, settings,
                 http_get: HttpGet | None = None, http_post: HttpPost | None = None) -> Guess | None:
    """Товара нет в базе: штрихкод -> открытые базы; иначе -> локальная модель по кадру; иначе None («не удалось определить»)."""
    if not getattr(settings, "naming_enabled", False):
        return None
    if getattr(settings, "naming_barcode_lookup", False):
        for code in codes:
            guess = lookup_barcode(code, lang, http_get=http_get)
            if guess is not None:
                return guess
    if jpeg:
        return name_by_vision(jpeg, lang, getattr(settings, "naming_vision_url", "") or "",
                              getattr(settings, "naming_vision_model", "") or "",
                              float(getattr(settings, "naming_vision_timeout_seconds", 40.0)), http_post=http_post)
    return None


def normalize_lang(value: str | None) -> str:
    value = (value or "").strip()
    for code in LANG_NAMES:
        if value.lower() == code.lower():
            return code
    return "ru"
