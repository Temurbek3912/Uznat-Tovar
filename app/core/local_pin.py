"""
Простой вход на САМОМ компьютере программы (решение владельца 2026-11-15): ПИН-код из 6 цифр вместо пароля и кода из приложения.
Чистая логика без FastAPI/SQLAlchemy — tests/test_local_pin.py.

Почему это безопасно настолько, насколько нужно магазину:
  * ПИН принимается, только если запрос пришёл с этого же компьютера: сокет сервера и клиента — петлевые адреса (127.0.0.1/::1),
    нет заголовков прокси (туннель Tailscale, nginx — всегда их ставят), имя в адресе — localhost/127.0.0.1 (защита от подмены
    DNS), а если браузер прислал Origin — тоже локальный (чужой сайт в браузере этого компьютера ПИН не подберёт).
    Покупатели из интернета и сотрудники по Wi-Fi до ПИН не доходят — у них прежний вход паролем (и кодом у администратора).
  * 5 ошибок — пауза 15 минут (общий счётчик попыток входа); PIN_MAX_FAILURES ошибок подряд — ПИН отключается совсем, новый
    задаётся только по одноразовой ссылке, которую выдаёт программа на этом компьютере (окно программы или «Забыли ПИН.bat»).
  * Сеанс по ПИН закрывается после PIN_IDLE_MINUTES без действий.
Риск, о котором сказано владельцу: тот, кто сидит за этим компьютером и знает ПИН, войдёт. Защита компьютера — пароль Windows.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from app.core.mfa_policy import LOCAL_HOSTNAMES, has_forwarding_headers, host_from_header, is_loopback

PIN_LENGTH = 6
PIN_MAX_FAILURES = 10            # ошибок подряд — ПИН снимается, нужна новая ссылка «Придумайте ПИН»
PIN_IDLE_MINUTES = 30            # сеанс по ПИН без действий дольше — закрыт
SETUP_LINK_MINUTES = 30          # сколько живёт ссылка «Придумайте ПИН»
METHOD_PIN = "pin"

_SIMPLE = {"012345", "123456", "234567", "345678", "456789", "567890", "987654", "876543", "765432", "654321", "543210",
           "098765", "121212", "123123", "112233", "102030", "111222", "696969", "147258", "159753"}


class PinPolicyError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def check_pin(pin: str) -> str:
    """ПИН для установки: ровно 6 цифр, не одна и та же цифра и не простой ряд. Возвращает его же."""
    value = (pin or "").strip()
    if len(value) != PIN_LENGTH or not value.isdigit() or not value.isascii():
        raise PinPolicyError("ПИН — ровно 6 цифр", "pin_format")
    if len(set(value)) == 1 or value in _SIMPLE:
        raise PinPolicyError("Слишком простой ПИН — его легко угадать. Придумайте другой", "pin_too_simple")
    return value


def is_this_computer(scope: Mapping, headers: Mapping[str, str]) -> bool:
    """Пришёл ли запрос с самого компьютера программы (см. описание модуля). scope — ASGI scope запроса (server, client)."""
    server = (scope.get("server") or (None, None))[0]
    client = (scope.get("client") or (None, None))[0]
    if not (is_loopback(server) and is_loopback(client)):
        return False
    if has_forwarding_headers(headers):
        return False
    if host_from_header(headers.get("host")) not in LOCAL_HOSTNAMES:
        return False
    origin = headers.get("origin")
    if origin:
        if origin == "null":
            return False
        if (urlsplit(origin).hostname or "").lower() not in LOCAL_HOSTNAMES:
            return False
    return True


def pin_session_idle(last_used: datetime | None, now: datetime) -> bool:
    """Сеанс по ПИН простаивал дольше предела (last_used_at обновляется раз в минуту — точность ±1 мин)."""
    return last_used is not None and now - last_used > timedelta(minutes=PIN_IDLE_MINUTES)
