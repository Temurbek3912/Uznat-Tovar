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
import unicodedata
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Callable

from app.services.product_kinds import KIND_IDS, VISION_HINTS, is_kind

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
    kind: str = ""               # вид товара (app/services/product_kinds.py); name пустое — известен только вид

    def as_dict(self) -> dict:
        return {"name": self.name, "brand": self.brand, "source": self.source, "kind": self.kind}


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
    # «Nescafé» и «Nescafe», «Nestlé» и «Nestle» — одно слово: знаки над латинскими буквами снимаются (кириллица уже переведена)
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
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
        "kind": {"type": "string", "enum": [*KIND_IDS, "other"]},
    },
    "required": ["is_product", "name", "brand", "confidence", "kind"],
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
        "Never invent a brand you cannot see. "
        "'kind' is the type of the product by what it is and what it is for (use the name you read), even if you cannot "
        "read the name, one of: "
        + "; ".join(f"{k} = {VISION_HINTS[k]}" for k in KIND_IDS)
        + "; other = none of these or no product. Reply with JSON only."
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


CONFIDENCE_LEVELS = ("low", "medium", "high")


def ask_vision(jpeg: bytes, lang: str, url: str, model: str, timeout: float,
               http_post: HttpPost | None = None) -> dict | None:
    """Кадр -> сырой ответ модели {is_product, name, brand, confidence, kind} или None (модель недоступна, ответ не разобран).
    Отдельно от решения «принять или нет» — чтобы замер (ml/benchmark_naming.py) видел и отвергнутые ответы."""
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
    return _parse_answer(content if isinstance(content, str) else "")


def answer_kind(answer: dict | None) -> str:
    """Вид товара из сырого ответа модели ("" — «не товар», «other», неизвестный код)."""
    if not answer or answer.get("is_product") is False:
        return ""
    kind = str(answer.get("kind") or "").strip()
    return kind if is_kind(kind) else ""


def accept_answer(answer: dict | None, min_confidence: str = "medium", with_kind: bool = False) -> Guess | None:
    """Сырой ответ модели -> догадка или None («не удалось определить»). Ниже порога уверенности, «не товар», пустое название — None.
    Порог — naming.min_confidence в config.yaml; выбран по замеру (документ 07, §10.6).
    with_kind: название не принято, но вид товара назван — догадка только с видом (name пустое; документ 07, §10.7)."""
    if not answer or answer.get("is_product") is False:
        return None
    kind = answer_kind(answer)
    confidence = str(answer.get("confidence", "")).lower()
    floor = min_confidence if min_confidence in CONFIDENCE_LEVELS else "medium"
    name = _clean(answer.get("name"))
    if confidence not in CONFIDENCE_LEVELS or CONFIDENCE_LEVELS.index(confidence) < CONFIDENCE_LEVELS.index(floor)             or confidence == "low" or not name:
        return Guess(name="", source="vision", kind=kind) if with_kind and kind else None
    return Guess(name=name, brand=_clean(answer.get("brand")), source="vision", sure=confidence == "high", kind=kind)


def name_by_vision(jpeg: bytes, lang: str, url: str, model: str, timeout: float,
                   http_post: HttpPost | None = None, min_confidence: str = "medium") -> Guess | None:
    """Кадр -> название от локальной модели «зрение + текст» или None (не уверена, не товар, модель недоступна)."""
    return accept_answer(ask_vision(jpeg, lang, url, model, timeout, http_post=http_post), min_confidence)


KindFn = Callable[..., object]


def _small_kind(jpeg: bytes, settings, classify: KindFn | None, min_score: float | None) -> str | None:
    """Вид от малой модели или None (выключена, не загрузилась, «не товар», ниже порога)."""
    if classify is None:
        if not getattr(settings, "naming_kind_model", ""):
            return None
        from app.pipeline.kind_classifier import classify as classify  # noqa: PLC0415 — torch только при включённой модели
    result = classify(jpeg, settings, min_score=min_score)
    kind = getattr(result, "kind", None)
    return kind if is_kind(kind) else None


def kind_by_small_model(jpeg: bytes, settings, classify: KindFn | None = None) -> Guess | None:
    """Малая быстрая модель (app/pipeline/kind_classifier.py): только вид товара, без названия; порог — из настроек."""
    kind = _small_kind(jpeg, settings, classify, None)
    return Guess(name="", source="kind_model", kind=kind) if kind else None


def _agreed_kind(guess: Guess | None, jpeg: bytes, settings, classify: KindFn | None) -> Guess | None:
    """Вид от большой модели остаётся, только если малая назвала тот же вид (замер 2026-11-15: так ошибок 1–2 % против 8–9 % у
    большой одной). Малой модели нет — вид большой модели как есть."""
    if guess is None or not guess.kind:
        return guess
    if classify is None and not (getattr(settings, "naming_kind_model", "") and _small_available(settings)):
        return guess                                  # малой модели нет или она не загрузилась — вид большой как есть
    small = _small_kind(jpeg, settings, classify, 0.0)
    if small == guess.kind:
        return guess
    if not guess.name:
        return None                                   # «только вид», а модели не согласны — честное «не удалось определить»
    return replace(guess, kind="")


def _small_available(settings) -> bool:
    try:
        from app.pipeline.kind_classifier import get_classifier  # noqa: PLC0415
        return get_classifier(settings) is not None
    except Exception:  # noqa: BLE001
        return False


def name_product(jpeg: bytes | None, codes: list[str], lang: str, settings,
                 http_get: HttpGet | None = None, http_post: HttpPost | None = None,
                 classify: KindFn | None = None) -> Guess | None:
    """Товара нет в базе: штрихкод -> открытые базы; иначе -> локальная модель по кадру (название и вид товара; вид — если с
    ним согласна малая модель); большая модель недоступна -> малая модель (только вид); иначе None («не удалось определить»).
    Догадка может быть «только вид» (name пустое, kind заполнен): «Это напиток» (документ 07, §10.7)."""
    if not getattr(settings, "naming_enabled", False):
        return None
    if getattr(settings, "naming_barcode_lookup", False):
        for code in codes:
            guess = lookup_barcode(code, lang, http_get=http_get)
            if guess is not None:
                return guess
    if not jpeg:
        return None
    url = getattr(settings, "naming_vision_url", "") or ""
    answer = ask_vision(jpeg, lang, url, getattr(settings, "naming_vision_model", "") or "",
                        float(getattr(settings, "naming_vision_timeout_seconds", 40.0)), http_post=http_post) if url else None
    if answer is not None:          # большая модель ответила — её «не товар» окончательное
        guess = accept_answer(answer, str(getattr(settings, "naming_min_confidence", "high") or "high"), with_kind=True)
        return _agreed_kind(guess, jpeg, settings, classify)
    return kind_by_small_model(jpeg, settings, classify)


def normalize_lang(value: str | None) -> str:
    value = (value or "").strip()
    for code in LANG_NAMES:
        if value.lower() == code.lower():
            return code
    return "ru"
