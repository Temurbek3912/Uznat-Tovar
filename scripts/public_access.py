"""
Вход покупателя из интернета при запуске (docs/PUBLIC_ACCESS.md): какой туннель запустить и какой адрес положить в QR.

PUBLIC_ACCESS (в .env или `start_demo --public ...`):
  off        — только сеть магазина (как раньше, QR с адресом компьютера в Wi-Fi);
  tailscale  — Tailscale Funnel: `tailscale funnel <порт>` идёт вместе с программой и выключается вместе с ней; адрес
               https://<имя-компьютера>.<сеть>.ts.net/ программа узнаёт сама (`tailscale status --json`). Для проверок
               и личного пользования: бесплатный тариф Tailscale — не для коммерческого использования;
  cloudflare — Cloudflare Tunnel: `cloudflared tunnel run` с секретом CLOUDFLARE_TUNNEL_TOKEN (передаётся через переменную
               TUNNEL_TOKEN, не в командной строке); адрес — PUBLIC_BASE_URL (имя, которое программист магазина настроил
               в панели Cloudflare на http://127.0.0.1:<CUSTOMER_PORT>);
  manual     — свой туннель или обратный прокси программиста магазина к http://127.0.0.1:<CUSTOMER_PORT>; программа ничего
               не запускает, только кладёт PUBLIC_BASE_URL в QR.

Туннель подключается ТОЛЬКО к входу покупателя (scripts/serve.py, app/core/customer_gate.py): страницы сотрудников и
управления через интернет не открываются. Ошибка настройки не останавливает программу: она работает в сети магазина и говорит,
что исправить.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.core.public_url import validate_public_base_url

MODES = ("off", "tailscale", "cloudflare", "manual")
SHOP_PATH = "ui/shop.html"
TAILSCALE_PATHS = (r"C:\Program Files\Tailscale\tailscale.exe",)
CLOUDFLARED_PATHS = (r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe")


@dataclass
class PublicPlan:
    mode: str = "off"
    base_url: str = ""                       # https://имя/ — постоянный адрес магазина в интернете
    argv: list[str] | None = None            # команда туннеля (None — программа туннель не запускает)
    env: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        """Вход из интернета включается: режим не off и ошибок нет."""
        return self.mode != "off" and not self.problems

    @property
    def shop_url(self) -> str:
        return self.base_url + SHOP_PATH if self.base_url else ""


def normalize_mode(value: str | None) -> str:
    return (value or "off").strip().lower() or "off"


def find_exe(name: str, which: Callable = shutil.which, known: tuple[str, ...] = (), exists=lambda p: Path(p).is_file()) -> str | None:
    found = which(name)
    if found:
        return found
    for path in known:
        if exists(path):
            return path
    return None


def tailscale_url(status: dict) -> tuple[str, str | None]:
    """(адрес https://имя/, ошибка) по ответу `tailscale status --json`."""
    state = str(status.get("BackendState") or "")
    if state and state != "Running":
        return "", (f"Tailscale не подключён (состояние: {state}). Откройте Tailscale на этом компьютере и войдите в свой аккаунт.")
    dns = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not dns:
        return "", "Tailscale не сообщил имя этого компьютера. Включите MagicDNS и HTTPS в панели Tailscale (DNS)."
    return f"https://{dns}/", None


def _tailscale_status(exe: str, run_cmd: Callable) -> dict | None:
    try:
        res = run_cmd([exe, "status", "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except Exception:            # noqa: BLE001 — нет ответа: это и скажем
        return None
    try:
        return json.loads(getattr(res, "stdout", "") or "")
    except (TypeError, ValueError):
        return None


def plan(settings, *, customer_port: int, mode: str | None = None, which: Callable = shutil.which,
         run_cmd: Callable | None = None, exists=lambda p: Path(p).is_file()) -> PublicPlan:
    """Что запустить для входа покупателя из интернета. mode — из командной строки (иначе PUBLIC_ACCESS из настроек)."""
    mode = normalize_mode(mode if mode is not None else getattr(settings, "public_access", "off"))
    result = PublicPlan(mode=mode)
    if mode == "off":
        return result
    if mode not in MODES:
        result.problems.append(f"PUBLIC_ACCESS={mode} — неизвестное значение. Допустимы: {', '.join(MODES)}.")
        return result

    if mode == "tailscale":
        exe = find_exe("tailscale", which, TAILSCALE_PATHS, exists)
        if not exe:
            result.problems.append("Tailscale не установлен. Установите с https://tailscale.com/download и войдите в аккаунт.")
            return result
        status = _tailscale_status(exe, run_cmd) if run_cmd else None
        if status is None:
            result.problems.append("Tailscale не отвечает. Запустите Tailscale на этом компьютере и войдите в аккаунт.")
            return result
        url, problem = tailscale_url(status)
        if problem:
            result.problems.append(problem)
            return result
        result.base_url = url
        result.argv = [exe, "funnel", str(customer_port)]     # без --bg: вход из интернета живёт, пока работает программа
        return result

    check = validate_public_base_url(getattr(settings, "public_base_url", ""), "server")
    if not check.ok:
        for f in check.errors:
            result.problems.append(f"PUBLIC_BASE_URL: {f.text} {f.hint}".strip())
        return result
    result.base_url = check.url

    if mode == "cloudflare":
        token = str(getattr(settings, "cloudflare_tunnel_token", "") or "").strip()
        if not token:
            result.problems.append("CLOUDFLARE_TUNNEL_TOKEN не задан: скопируйте секрет туннеля из панели Cloudflare в .env.")
            return result
        exe = find_exe("cloudflared", which, CLOUDFLARED_PATHS, exists)
        if not exe:
            result.problems.append("cloudflared не установлен: https://developers.cloudflare.com/cloudflare-one/connections/"
                                   "connect-networks/downloads/")
            return result
        result.argv = [exe, "tunnel", "--no-autoupdate", "run"]
        result.env = {"TUNNEL_TOKEN": token}                  # не в командной строке: её видно в списке процессов
    return result
