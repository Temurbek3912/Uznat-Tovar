"""
Сведения о сеансе входа для списка «Мои сеансы» (документ 05, §12.10; документ 18): приблизительное устройство, откуда вход,
поколение сеансов. Чистая логика без FastAPI/SQLAlchemy — проверяется офлайн (tests/test_session_info.py).

Что НЕ хранится и не показывается: полный IP-адрес и полная строка браузера. IP превращается в HMAC-SHA256 с секретом сервера
(сравнить «тот же ли адрес» можно, восстановить адрес перебором всех IPv4 — нельзя, пока секрет не утёк), строка браузера —
в короткую подпись вида «Телефон · Android · Chrome». Источник — класс адреса: этот компьютер / локальная сеть / интернет.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from datetime import datetime, timedelta

# Как часто обновлять «последнюю активность» сеанса: не чаще раза в минуту, чтобы каждый запрос не писал в базу.
LAST_USED_RESOLUTION = timedelta(seconds=60)
DEVICE_LABEL_MAX = 60

SOURCE_LOCAL = "local"          # 127.0.0.1 / ::1 — сам компьютер магазина
SOURCE_LAN = "lan"              # частные адреса: 192.168.x.x, 10.x.x.x, 172.16–31.x.x, fd00::/8, link-local
SOURCE_REMOTE = "remote"        # публичный адрес: вход из интернета (через HTTPS-прокси или VPN с трансляцией адресов)
SOURCE_UNKNOWN = "unknown"


def classify_source(ip: str | None) -> str:
    if not ip:
        return SOURCE_UNKNOWN
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return SOURCE_UNKNOWN
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback:
        return SOURCE_LOCAL
    if addr.is_private or addr.is_link_local:
        return SOURCE_LAN
    return SOURCE_REMOTE


def ip_fingerprint(ip: str | None, key: bytes) -> str | None:
    """HMAC-SHA256(адрес) с секретом сервера, первые 32 hex-символа. Без секрета адрес не запоминается вовсе."""
    if not ip or not key:
        return None
    return hmac.new(key, ip.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


_OS_RULES = (
    (r"iPhone|iPod", "iPhone", "phone"), (r"iPad", "iPad", "tablet"), (r"Android", "Android", None),
    (r"Windows NT", "Windows", "computer"), (r"Mac OS X|Macintosh", "macOS", "computer"), (r"CrOS", "ChromeOS", "computer"),
    (r"Linux", "Linux", "computer"),
)
_BROWSER_RULES = (
    (r"Edg(?:e|A|iOS)?/", "Edge"), (r"OPR/|Opera", "Opera"), (r"YaBrowser/", "Яндекс Браузер"), (r"SamsungBrowser/", "Samsung Internet"),
    (r"Firefox/|FxiOS/", "Firefox"), (r"Chrome/|CriOS/", "Chrome"), (r"Safari/", "Safari"),
    (r"python-requests|curl/|httpx|Wget", "программа"),
)


def summarize_user_agent(user_agent: str | None) -> dict:
    """Строка браузера -> {"kind": phone|tablet|computer|unknown, "os": "...", "browser": "..."} — приблизительно, без версий."""
    ua = str(user_agent or "")[:512]
    kind, os_name = "unknown", ""
    for pattern, name, device_kind in _OS_RULES:
        if re.search(pattern, ua):
            os_name = name
            kind = device_kind or ("phone" if "Mobile" in ua else "tablet")
            break
    browser = next((name for pattern, name in _BROWSER_RULES if re.search(pattern, ua)), "")
    return {"kind": kind, "os": os_name, "browser": browser}


def clean_device_label(label: str | None) -> str | None:
    """Название устройства, которое человек ввёл сам («Ноутбук дома»): без управляющих символов, не длиннее 60."""
    if label is None:
        return None
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(label)).strip()
    text = re.sub(r"\s+", " ", text)[:DEVICE_LABEL_MAX]
    return text or None


def needs_last_used_update(last_used: datetime | None, now: datetime) -> bool:
    return last_used is None or now - last_used >= LAST_USED_RESOLUTION


def epoch_matches(token_epoch: int | None, user_epoch: int | None) -> bool:
    """Поколение сеансов (документ 05, §12.11): токен действует, только если его поколение равно поколению пользователя.
    Токены, выданные до появления поколений (сведений нет), относятся к поколению 0."""
    return int(token_epoch or 0) == int(user_epoch or 0)
