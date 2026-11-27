"""
«Глобальный ИИ» Google Gemini — необязательное дополнение к своей модели «зрение + текст» (выбор владельца 2026-11-27).

Как работает:
  - ключ Gemini (бесплатный, aistudio.google.com) вводит сам владелец в «Настройках». Хранится в `<media>/gemini_settings.json`
    ЗАШИФРОВАННЫМ отдельным подключом AUTH_SECRET_KEY (как ключ Google Диска, app/core/auth_secrets.encrypt_text). Ключ не
    пишется в журнал, не возвращается страницей (видны только последние 4 знака) и уходит только в заголовке запроса к Google;
  - запрос идёт ОДНОВРЕМЕННО в свою модель и в Gemini (app/services/assistant.py, app/services/product_namer.py); годный ответ
    Gemini предпочитается (модель сильнее), ошибка, нет сети, нет ключа, исчерпан лимит — ответ своей модели, как раньше.
    Без интернета и без ключа программа работает полностью на своей модели;
  - бесплатный лимит Google не перерасходуется: программа сама считает запросы за сутки (gemini_daily_limit, сутки — по времени
    Google, тихоокеанскому) и при ответе Google «лимит» (HTTP 429) делает паузу; «Настройки» показывают понятное состояние;
  - что уходит в Gemini: вопросы помощнику (с данными каталога, которые помощник нашёл) и номер штрихкода неизвестного товара
    (поиск Google). Фото покупателей — НЕ уходят, пока владелец сам не включит отдельный переключатель «Отправлять фото».

Сеть подаётся функцией http(method, url, body, headers, timeout) -> dict — в тестах заглушка, настоящих запросов нет.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
SETTINGS_FILE = "gemini_settings.json"
USAGE_FILE = "gemini_usage.json"
KEY_LABEL = b"uznat:gemini-key:v1"
LIMIT_PAUSE = 600.0             # Google ответил «лимит» (429: запросов в минуту или в сутки) — 10 минут не спрашиваем
OFFLINE_PAUSE = 60.0            # нет сети — минуту не спрашиваем (не ждать на каждом шаге)
MIN_OUTPUT_TOKENS = 8192        # «размышления» Gemini тоже входят в max_tokens: с 500, как у своей модели, ответ обрезался бы
_PACIFIC = timezone(timedelta(hours=-8))    # бесплатный лимит Google обнуляется в полночь по тихоокеанскому времени
_KEY_RE = re.compile(r"^[A-Za-z0-9_\-.]{20,200}$")

Http = Callable[[str, str, dict | None, dict, float], dict]


class HttpStatus(Exception):
    """Google ответил ошибкой: status — код HTTP, text — начало ответа (без ключа: ключ в заголовке, не в адресе)."""

    def __init__(self, status: int, text: str = ""):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.text = text


class GeminiError(RuntimeError):
    """Gemini не ответил. code: off | no_key | no_secret | limit | bad_key | offline | failed. Текст безопасен для показа."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _default_http(method: str, url: str, body: dict | None, headers: dict, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 — адрес Google, постоянный
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            text = ""
        raise HttpStatus(exc.code, text) from None


_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gemini")


def submit(fn, *args):
    """Запрос к Gemini в фоне — одновременно со своей моделью."""
    return _pool.submit(fn, *args)


class Gemini:
    def __init__(self, *, media_root: Path, secret_key: str, model: str = "gemini-flash-latest", daily_limit: int = 200,
                 timeout: float = 20.0, http: Http | None = None, clock=time.time):
        self.media_root = Path(media_root)
        self.secret_key = secret_key or ""
        self.model = (model or "gemini-flash-latest").strip()
        self.daily_limit = max(0, int(daily_limit))
        self.timeout = float(timeout)
        self._http = http or _default_http
        self._clock = clock
        self._lock = threading.Lock()
        self._paused_until = 0.0
        self._pause_reason = ""

    # ----- хранение
    def _conf(self) -> dict:
        from app.services.lite_backup import read_json
        return read_json(self.media_root / SETTINGS_FILE)

    def _change(self, change) -> dict:
        from app.services.lite_backup import read_json, write_json
        with self._lock:
            data = change(read_json(self.media_root / SETTINGS_FILE))
            write_json(self.media_root / SETTINGS_FILE, data)
            return data

    def can_store(self) -> bool:
        from app.core.auth_secrets import is_configured
        return is_configured(self.secret_key)

    def api_key(self) -> str:
        token = self._conf().get("key")
        if not token or not self.can_store():
            return ""
        try:
            from app.core.auth_secrets import decrypt_text
            return decrypt_text(token, self.secret_key, KEY_LABEL)
        except Exception:  # noqa: BLE001 — сменился AUTH_SECRET_KEY: ключ ввести заново
            return ""

    def save_key(self, key: str) -> None:
        """Сохранить ключ владельца (зашифрованным). Неверный вид — ValueError; ключ проверяется у Google (check_key)."""
        key = (key or "").strip()
        if not _KEY_RE.match(key):
            raise ValueError("bad_format")
        if not self.can_store():
            raise GeminiError("no_secret")
        from app.core.auth_secrets import encrypt_text
        token = encrypt_text(key, self.secret_key, KEY_LABEL)
        self._change(lambda d: {**d, "key": token, "key_tail": key[-4:], "bad_key": False,
                                "enabled": d.get("enabled", True), "photos": bool(d.get("photos", False))})
        self._paused_until, self._pause_reason = 0.0, ""

    def remove_key(self) -> None:
        self._change(lambda d: {k: v for k, v in d.items() if k not in ("key", "key_tail", "bad_key")})

    def set_options(self, *, enabled: bool | None = None, photos: bool | None = None) -> None:
        def change(d: dict) -> dict:
            if enabled is not None:
                d["enabled"] = bool(enabled)
            if photos is not None:
                d["photos"] = bool(photos)
            return d
        self._change(change)

    # ----- счётчик бесплатного лимита
    def _day(self) -> str:
        return datetime.fromtimestamp(self._clock(), _PACIFIC).strftime("%Y-%m-%d")

    def used_today(self) -> int:
        from app.services.lite_backup import read_json
        usage = read_json(self.media_root / USAGE_FILE)
        return int(usage.get("count") or 0) if usage.get("day") == self._day() else 0

    def _reserve(self) -> bool:
        """Учесть один запрос; False — суточный лимит программы исчерпан."""
        from app.services.lite_backup import read_json, write_json
        with self._lock:
            day = self._day()
            usage = read_json(self.media_root / USAGE_FILE)
            count = int(usage.get("count") or 0) if usage.get("day") == day else 0
            if count >= self.daily_limit:
                return False
            write_json(self.media_root / USAGE_FILE, {"day": day, "count": count + 1})
            return True

    # ----- состояние
    def state(self, *, photo: bool = False) -> str:
        """ok — можно спрашивать; иначе код причины (см. GeminiError)."""
        conf = self._conf()
        if not conf.get("key"):
            return "no_key"
        if not self.can_store() or not self.api_key():
            return "no_secret"
        if not conf.get("enabled", True):
            return "off"
        if photo and not conf.get("photos", False):
            return "off"
        if conf.get("bad_key"):
            return "bad_key"
        if self._clock() < self._paused_until:
            return self._pause_reason or "offline"
        if self.used_today() >= self.daily_limit:
            return "limit"
        return "ok"

    def usable(self, *, photo: bool = False) -> bool:
        return self.state(photo=photo) == "ok"

    def status(self) -> dict:
        conf = self._conf()
        return {"configured": bool(conf.get("key")), "can_store": self.can_store(), "enabled": bool(conf.get("enabled", True)),
                "photos": bool(conf.get("photos", False)), "key_tail": str(conf.get("key_tail") or ""),
                "state": self.state(), "used_today": self.used_today(), "daily_limit": self.daily_limit, "model": self.model}

    # ----- запросы
    def _call(self, method: str, url: str, body: dict | None, *, photo: bool = False, count: bool = True) -> dict:
        code = self.state(photo=photo)
        if code not in ("ok", "limit") or (code == "limit" and count):
            raise GeminiError(code)
        if count and not self._reserve():
            raise GeminiError("limit")
        try:
            return self._http(method, url, body, {"x-goog-api-key": self.api_key()}, self.timeout)
        except HttpStatus as exc:
            text = exc.text or ""
            if exc.status == 429:
                self._paused_until, self._pause_reason = self._clock() + LIMIT_PAUSE, "limit"
                logger.warning("Gemini: лимит Google (429) — пауза %d мин", int(LIMIT_PAUSE // 60))
                raise GeminiError("limit") from None
            if exc.status in (401, 403) or (exc.status == 400 and ("API_KEY" in text or "API key" in text)):
                self._change(lambda d: {**d, "bad_key": True})
                logger.warning("Gemini: ключ не принят Google (HTTP %d)", exc.status)
                raise GeminiError("bad_key") from None
            logger.warning("Gemini: ошибка HTTP %d", exc.status)
            raise GeminiError("failed") from None
        except GeminiError:
            raise
        except Exception as exc:  # noqa: BLE001 — нет сети, Google не ответил вовремя
            self._paused_until, self._pause_reason = self._clock() + OFFLINE_PAUSE, "offline"
            logger.info("Gemini: нет связи (%s) — работает своя модель", type(exc).__name__)
            raise GeminiError("offline") from None

    def check_key(self) -> None:
        """Проверить ключ у Google без расхода лимита (сведения о модели). Ошибка — GeminiError."""
        self._call("GET", f"{BASE_URL}/models/{self.model}", None, count=False)

    def chat(self, payload: dict, *, photo: bool = False) -> dict:
        """OpenAI-совместимый запрос (тот же payload, что своей модели) -> ответ в том же виде {"choices": [...]}."""
        body = {**payload, "model": self.model,
                "max_tokens": max(int(payload.get("max_tokens") or 0), MIN_OUTPUT_TOKENS)}
        return self._call("POST", f"{BASE_URL}/openai/chat/completions", body, photo=photo)

    def search(self, prompt: str) -> str:
        """Текстовый вопрос с поиском Google (номер штрихкода неизвестного товара) -> текст ответа."""
        reply = self._call("POST", f"{BASE_URL}/models/{self.model}:generateContent",
                           {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "tools": [{"google_search": {}}],
                            "generationConfig": {"temperature": 0}})
        try:
            parts = reply["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return ""
        return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))


_service: Gemini | None = None
_service_lock = threading.Lock()


def from_settings(settings) -> Gemini | None:
    """Gemini этой программы (один на процесс) или None (настройки без папки данных — тесты)."""
    global _service
    media = getattr(settings, "media_root", None)
    if not media:
        return None
    with _service_lock:
        if _service is None:
            from app.core.config import resolve_project_path
            _service = Gemini(media_root=resolve_project_path(media), secret_key=getattr(settings, "auth_secret_key", "") or "",
                              model=getattr(settings, "gemini_model", "") or "gemini-flash-latest",
                              daily_limit=int(getattr(settings, "gemini_daily_limit", 200)),
                              timeout=float(getattr(settings, "gemini_timeout_seconds", 20.0)))
        return _service


def usable(settings, *, photo: bool = False) -> Gemini | None:
    """Gemini, если его можно спросить прямо сейчас (ключ есть, включён, лимит не исчерпан, сеть была); иначе None."""
    try:
        service = from_settings(settings)
        return service if service is not None and service.usable(photo=photo) else None
    except Exception:  # noqa: BLE001 — сбой чтения настроек не мешает своей модели
        return None
