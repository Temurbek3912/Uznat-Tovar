"""Постоянный адрес каталога (PUBLIC_BASE_URL) и диагностика сети. Чистый модуль: только стандартная библиотека (без FastAPI, SQLAlchemy, pydantic) — проверяется офлайн
(tests/test_public_url.py).

Зачем. QR-код печатают и клеят на стену, поэтому он обязан содержать ПОСТОЯННЫЙ адрес: имя домена, а не IP компьютера. Если IP сменится, меняется запись в DNS (или в роутере),
а QR остаётся прежним. Поэтому здесь жёсткие правила, которые нельзя обойти настройкой:
  * адрес — только `схема://имя[:порт]/`; IP-адрес (в любой записи, которую понимают браузеры: 192.168.1.50, 2130706433, 0x7f.0.0.1, 127.1, [::1]), `localhost`, `*.localhost` —
    ошибка: такой QR на телефоне клиента открыл бы не компьютер магазина, а сам телефон, либо перестал бы работать при смене IP;
  * в адресе нет логина/пароля, параметров запроса, фрагмента и пути (приложение стоит в корне имени; путь был бы ловушкой для прокси);
  * в режиме SERVER схема обязана быть https и имя — публичное; в режиме LOCAL http допустим, но это честно помечается предупреждением: браузеры телефонов не включат живую камеру
    на незащищённом адресе (останется «сфотографировать и отправить»).

Диагностика не обещает того, чего нет: по результатам проверок (куда указывает имя, открывается ли страница, сколько дней осталось сертификату) она говорит, что исправить.
Сетевые операции вынесены в тонкие функции с подменяемыми зависимостями, поэтому логика проверяется без сети.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence
from urllib.parse import urlsplit

MODES = ("local", "server")
MAX_URL_LENGTH = 2048
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]+|[0-9]+)$")          # части, из которых браузер собирает IP: 192, 0x7f, 2130706433
_DEFAULT_PORTS = {"http": 80, "https": 443}
CERT_WARN_DAYS = 14                                              # осталось меньше — предупреждаем (Let's Encrypt продлевают за ~30 дней до конца)


@dataclass(frozen=True)
class Finding:
    """Одно замечание: level — error | warn | ok; code — для тестов и кода; text — человеку; hint — что сделать."""
    level: str
    code: str
    text: str
    hint: str = ""


@dataclass
class UrlCheck:
    ok: bool                                   # нет ошибок: адрес можно класть в QR
    url: str = ""                              # канонический адрес (схема://имя[:порт]/); пусто, если есть ошибки
    host: str = ""
    scheme: str = ""
    port: int | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]


class PublicUrlError(ValueError):
    """Адрес нельзя использовать для QR; в .findings — причины на русском."""

    def __init__(self, findings: Sequence[Finding]):
        self.findings = list(findings)
        super().__init__("; ".join(f.text for f in findings) or "адрес не подходит")


def looks_like_ip(host: str) -> bool:
    """Похоже ли имя на IP-адрес в любой записи, которую разберёт браузер (а он разбирает и 2130706433, и 0x7f.0.0.1, и 127.1)."""
    host = host.strip("[]").rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if ":" in host:                                                # что-то в стиле IPv6 в неверной записи — всё равно не имя
        return True
    labels = host.split(".")
    return all(_NUMERIC_LABEL.match(label) for label in labels)


def _is_localhost(host: str) -> bool:
    host = host.rstrip(".").lower()
    return host == "localhost" or host.endswith(".localhost")


def _ascii_host(host: str) -> str | None:
    """Имя в ASCII (кириллические и узбекские домены -> punycode); None, если имя не разобралось."""
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None


def validate_public_base_url(value: str | None, mode: str = "local") -> UrlCheck:
    """Проверяет PUBLIC_BASE_URL. Возвращает UrlCheck; ok=False, если адрес нельзя класть в QR. Не бросает исключений."""
    result = UrlCheck(ok=False)
    add = lambda level, code, text, hint="": result.findings.append(Finding(level, code, text, hint))     # noqa: E731

    if mode not in MODES:
        add("error", "bad_mode", f"Режим «{mode}» неизвестен.", "Допустимы APP_MODE=local и APP_MODE=server.")
        return result
    text = (value or "").strip()
    if not text:
        add("error", "empty", "Постоянный адрес каталога не задан.", "Задайте PUBLIC_BASE_URL, например https://catalog.ваш-домен.uz/ — QR-код строится только из него.")
        return result
    if len(text) > MAX_URL_LENGTH or any(ch.isspace() for ch in text):
        add("error", "bad_chars", "В адресе есть пробелы или он слишком длинный.", "Скопируйте адрес целиком без пробелов.")
        return result
    if "://" not in text:
        add("error", "no_scheme", "В адресе нет схемы (http:// или https://).", "Начните адрес с https://.")
        return result

    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        add("error", "bad_port", "Адрес не разбирается (проверьте номер порта и скобки).", "Порт — число от 1 до 65535: https://имя:8443/.")
        return result
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        add("error", "bad_scheme", f"Схема «{parts.scheme}» не подходит.", "Используйте https:// (или http:// только для режима LOCAL без живой камеры).")
        return result
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        add("error", "userinfo", "В адресе не должно быть логина и пароля.", "Уберите часть перед @: пароль в QR-коде увидит любой.")
        return result
    if parts.query or parts.fragment or "?" in text or "#" in text:
        add("error", "query_or_fragment", "В адресе не должно быть параметров (?…) и якоря (#…).", "QR ведёт на главную страницу каталога без параметров.")
        return result
    if parts.path not in ("", "/"):
        add("error", "path_not_root", f"Адрес не должен содержать путь («{parts.path}»).", "Приложение стоит в корне имени; путь ломает обратный прокси и ссылки.")
        return result

    raw_host = (parts.hostname or "").strip()
    if not raw_host:
        add("error", "no_host", "В адресе нет имени компьютера или домена.", "Например: https://catalog.ваш-домен.uz/.")
        return result
    if _is_localhost(raw_host):
        add("error", "localhost", "Адрес указывает на localhost: на телефоне клиента это сам телефон.",
            "Нужно постоянное имя из интернета или локальной сети, которое открывается с других устройств.")
        return result
    if looks_like_ip(raw_host):
        add("error", "ip_literal", "Адрес — это IP-адрес. QR из IP нельзя: при смене IP компьютера его пришлось бы перепечатывать.",
            "Заведите постоянное имя (домен) и направьте его на нужный IP в DNS или в роутере; в QR остаётся имя.")
        return result
    host = _ascii_host(raw_host.rstrip("."))
    if not host or len(host) > 253 or not all(_LABEL.match(label) for label in host.split(".")):
        add("error", "bad_host", f"Имя «{raw_host}» записано неверно.", "Допустимы буквы, цифры и дефисы; части разделяются точками.")
        return result

    result.host, result.scheme, result.port = host, scheme, port
    if mode == "server":
        if scheme != "https":
            add("error", "http_in_server_mode", "В режиме SERVER нужен https://.", "Без HTTPS браузеры телефонов не включат камеру и микрофон.")
        if "." not in host or host.endswith(".local"):
            add("error", "not_public_name", "В режиме SERVER нужен публичный домен.", "Имя из одной части или .local не откроется из интернета.")
    else:
        if scheme == "http":
            add("warn", "http_no_secure_context", "Адрес без HTTPS: браузеры телефонов не включат живую камеру (сканирование штрихкода и съёмка в браузере).",
                "Останется «сфотографировать и отправить». Для живой камеры нужен https:// и сертификат на ваш домен.")
        if host.endswith(".local"):
            add("warn", "mdns_name", "Имя .local работает не на всех телефонах и только в одной сети.", "Надёжнее собственный домен, указывающий на адрес компьютера.")
        elif "." not in host:
            add("warn", "single_label", "Имя из одной части («%s») понимает только сеть, где его задали (обычно роутер)." % host, "Надёжнее собственный домен.")
    if result.errors:
        return result

    netloc = host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
    result.url = f"{scheme}://{netloc}/"
    result.ok = True
    return result


def qr_target(value: str | None, mode: str = "local") -> str:
    """Адрес, который кладётся в QR. Только из проверенного PUBLIC_BASE_URL; при ошибке — PublicUrlError (QR не создаётся)."""
    check = validate_public_base_url(value, mode)
    if not check.ok:
        raise PublicUrlError(check.errors)
    return check.url


# ---------------------------------------------------------------- диагностика сети
def classify_ip(ip: str) -> str:
    """loopback | private | link_local | shared (100.64/10, оператор) | public | invalid — как видит адрес человек, а не как его называет библиотека."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "invalid"
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link_local"
    if addr.version == 4 and addr in ipaddress.ip_network("100.64.0.0/10"):
        return "shared"
    if addr.is_private:
        return "private"
    return "public"


def resolve_host(host: str, resolver: Callable = socket.getaddrinfo) -> list[str]:
    """IP-адреса, на которые сейчас указывает имя (пусто — имя не разрешилось). Ошибки сети не бросаются: для диагностики «не разрешилось» — тоже ответ."""
    try:
        infos = resolver(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen


def cert_days_left(host: str, port: int = 443, connect: Callable | None = None, now: Callable[[], datetime] | None = None) -> int | None:
    """Сколько дней осталось сертификату у host:port; None — узнать не удалось (нет HTTPS, нет сети, самоподписанный и т. п.)."""
    try:
        if connect is None:
            context = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=5) as raw, context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert()
        else:
            cert = connect(host, port)
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)
    except (OSError, ssl.SSLError, KeyError, ValueError):
        return None
    current = (now or (lambda: datetime.now(timezone.utc)))()
    return (expires - current).days


def diagnose(check: UrlCheck, mode: str, resolved: Iterable[str], reachable: bool | None, cert_days: int | None, lan_ips: Iterable[str] = ()) -> list[Finding]:
    """Что исправить владельцу. resolved — куда указывает имя; reachable — открылась ли страница каталога по этому адресу (None — не проверяли); cert_days — дней до конца
    сертификата (None — неизвестно); lan_ips — известные адреса самого компьютера в сети (если приложение их знает; из Docker они не всегда видны, поэтому необязательно)."""
    findings: list[Finding] = list(check.findings)
    if not check.ok:
        return findings
    ips = list(resolved)
    kinds = {ip: classify_ip(ip) for ip in ips}
    if not ips:
        findings.append(Finding("error", "dns_unresolved", f"Имя «{check.host}» сейчас никуда не указывает.",
                                "Создайте запись типа A для этого имени: в режиме LOCAL — на адрес компьютера магазина, в режиме SERVER — на адрес сервера. Если запись только что создана, подождите: DNS обновляется не сразу."))
    elif any(k == "loopback" for k in kinds.values()):
        findings.append(Finding("error", "dns_loopback", f"Имя «{check.host}» указывает на 127.0.0.1 — телефон клиента пойдёт на сам себя.", "Укажите в записи DNS адрес компьютера в сети магазина."))
    elif mode == "server" and any(k in ("private", "link_local", "shared") for k in kinds.values()):
        findings.append(Finding("error", "dns_private_in_server_mode", "В режиме SERVER имя указывает на внутренний адрес: из интернета оно не откроется.", "Укажите публичный адрес сервера."))
    elif mode == "local" and all(k == "public" for k in kinds.values()):
        findings.append(Finding("warn", "dns_public_in_local_mode", "Имя указывает на публичный адрес, а не на компьютер в сети магазина.",
                                "Если это сделано нарочно (туннель, проброс портов) — всё в порядке; иначе укажите адрес компьютера в сети магазина."))
    lan = list(lan_ips)
    if mode == "local" and lan and ips and not any(ip in lan for ip in ips) and any(k in ("private", "shared") for k in kinds.values()):
        findings.append(Finding("warn", "dns_not_this_computer", "Имя указывает на адрес, которого нет среди адресов этого компьютера.",
                                "Возможно, адрес компьютера изменился. Обновите запись DNS (QR перепечатывать не нужно) и закрепите адрес в роутере (резервирование DHCP)."))
    if reachable is False:
        findings.append(Finding("error", "unreachable", f"Страница каталога по адресу {check.url} не открылась.",
                                "Проверьте: запущены ли службы (docker compose ps), открыт ли порт на этом компьютере, не блокирует ли брандмауэр Windows, та же ли сеть у телефона."))
    if check.scheme == "https":
        if cert_days is None:
            findings.append(Finding("warn", "cert_unknown", "Срок действия сертификата определить не удалось.", "Откройте адрес в браузере: если появилось предупреждение, сертификат неверный или просрочен."))
        elif cert_days < 0:
            findings.append(Finding("error", "cert_expired", "Сертификат просрочен: браузеры телефонов откажутся открывать сайт и камеру.", "Продлите сертификат (инструкция в документации по HTTPS)."))
        elif cert_days < CERT_WARN_DAYS:
            findings.append(Finding("warn", "cert_expiring", f"Сертификат истекает через {cert_days} дн.", "Проверьте, что автоматическое продление работает и есть доступ в интернет."))
    if not any(f.level == "error" for f in findings) and reachable:
        findings.append(Finding("ok", "reachable", f"Каталог открывается по адресу {check.url}.", ""))
    return findings
