"""
Запуск всей демонстрации одним действием: Postgres+Redis (Docker), API, воркер -> ждёт готовности ->
показывает адрес и QR-код для телефона. Ctrl+C (или закрытие окна) останавливает API и воркер.

    python -m scripts.start_demo                  # обычный запуск
    python -m scripts.start_demo --check          # сначала предстартовая проверка (scripts.preflight)
    python -m scripts.start_demo --no-docker      # Postgres и Redis уже запущены без Docker
    python -m scripts.start_demo --no-browser     # не открывать дашборд на этом компьютере
    python -m scripts.start_demo --port 8080
    python -m scripts.start_demo --lite           # облегчённый режим: без Docker, один процесс, SQLite (start_lite.bat)
    python -m scripts.start_demo --lite --cpu     # то же, модель — только на процессоре (как на ноутбуке без видеокарты)
    python -m scripts.start_demo --public tailscale   # покупатели заходят из интернета (docs/PUBLIC_ACCESS.md; или PUBLIC_ACCESS в .env)

Облегчённый режим (документ 11, §10) — для слабых компьютеров и ноутбуков: нет Docker (экономия около 2 ГБ ОЗУ), нет отдельного
воркера (модель грузится один раз, в процессе API), база — файл SQLite в data/lite (свои товары и пользователи, отдельно от
PostgreSQL полного режима; перенести товары: python -m scripts.lite_copy). Модель «зрение + текст» включается по железу
(app/core/hardware.py). Если Docker на компьютере нет, обычный запуск сам выбирает облегчённый режим.

Двойной щелчок: `start_demo.bat` в корне проекта; ярлык с иконкой на рабочем столе — `python -m scripts.make_shortcut`.
Журналы служб — в папке logs/ (api.log, worker.log): при сбое последние строки показываются сразу.

Воркер запускается в ОДНОМ процессе (на Windows --pool=solo обязателен; на остальных --concurrency=1): индекс каталога
пока не рассчитан на несколько процессов (docs/HANDOFF.md, №14). Postgres и Redis после остановки НЕ выключаются
(данные сохраняются; выключить — `docker compose stop`).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from app.core import hardware
from scripts import child_tie, phone_url, public_access, vision_engine
from scripts.preflight import parse_host_port
from scripts.setup_link import write_run_mode

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRAMES_URL = "redis://127.0.0.1:6380/0"    # служба frames из docker-compose.yml (порт FRAMES_PORT, только 127.0.0.1)
STOP_TIMEOUT = 8.0


@dataclass(frozen=True)
class Service:
    name: str
    argv: list[str]
    optional: bool = False          # остановилась — программа работает дальше (туннель входа из интернета)


def tls_files(root: Path) -> tuple[str, str] | None:
    """Сертификат HTTPS сети магазина (scripts/make_local_tls.py): есть — API запускается по HTTPS (документ 18, §14)."""
    cert, key = root / "secrets" / "tls" / "server.crt", root / "secrets" / "tls" / "server.key"
    return (str(cert), str(key)) if cert.is_file() and key.is_file() else None


LITE_DIR = Path("data") / "lite"


def lite_environment(root: Path, plan: "hardware.LitePlan", hf_cache: Path | None = None) -> dict[str, str]:
    """Переменные облегчённого режима: всё своё — в data/lite (база, индекс, фото), без Redis и без отдельного воркера."""
    lite = root / LITE_DIR
    env = {
        "LITE_MODE": "1",
        "DATABASE_URL": "sqlite:///" + (lite / "shop.db").as_posix(),
        "MEDIA_ROOT": lite.as_posix(),
        "CATALOG_INDEX_PATH": (lite / "index" / "catalog.faiss").as_posix(),
        "QUEUE_BROKER_URL": "memory://",
        "FRAMES_REDIS_URL": "",                       # кадры — в памяти процесса API (D-10а: на диск не пишутся)
        "AUTH_THROTTLE_BACKEND": "memory",
        "ML_DEVICE": plan.device,
    }
    if not plan.vision:
        env["NAMING_VISION_URL"] = ""                 # модель «зрение + текст» выключена: остаётся штрихкод -> открытые базы
    if hf_cache is not None and hf_cache.is_dir() and _kind_model_cached(hf_cache):
        env["HF_HUB_OFFLINE"] = "1"                   # модели уже скачаны: не ходить в интернет при каждом запуске
    return env


def api_argv(python: str, port: int, tls: tuple[str, str] | None = None, customer_port: int | None = None) -> list[str]:
    """Команда API. customer_port — ещё и вход покупателя из интернета на 127.0.0.1 (scripts/serve.py, docs/PUBLIC_ACCESS.md)."""
    if customer_port:
        argv = [python, "-m", "scripts.serve", "--host", "0.0.0.0", "--port", str(port), "--customer-port", str(customer_port)]
    else:
        argv = [python, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)]
    if tls:
        argv += ["--ssl-certfile", tls[0], "--ssl-keyfile", tls[1]]
    return argv


def lite_commands(python: str, port: int, tls: tuple[str, str] | None = None, customer_port: int | None = None) -> list[Service]:
    """Облегчённый режим — одна служба: API, в котором выполняются и задачи (app/worker.py, LITE_MODE)."""
    return [Service("lite", api_argv(python, port, tls, customer_port))]


def _kind_model_cached(hf_cache: Path) -> bool:
    """Скачана ли малая модель «вид товара» (документ 07, §10.7) рядом с моделью распознавания. Нет — первый запуск с интернетом
    скачает её сам (HF_HUB_OFFLINE не ставится); выключена (NAMING_KIND_MODEL пусто) — не нужна."""
    from app.core.config import Settings  # noqa: PLC0415
    repo = os.environ.get("NAMING_KIND_MODEL", Settings.model_fields["naming_kind_model"].default)
    if not repo:
        return True
    return (hf_cache.parent / ("models--" + repo.replace("/", "--"))).is_dir()


def _hf_model_cache() -> Path:
    from scripts.fetch_models import embedder_hub_model  # noqa: PLC0415 — модель поиска по виду из config.yaml
    base = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
    return base / "hub" / ("models--" + embedder_hub_model().replace("/", "--"))


def _probe_hw():
    return hardware.probe()


def service_commands(python: str, port: int, platform: str, tls: tuple[str, str] | None = None,
                     customer_port: int | None = None) -> list[Service]:
    api = Service("api", api_argv(python, port, tls, customer_port))
    pool = ["--pool=solo"] if platform.startswith("win") else ["--concurrency=1"]
    worker = Service("worker", [python, "-m", "celery", "-A", "app.worker.celery_app", "worker", "--loglevel=info", *pool])
    return [api, worker]


# ------------------------------------------------------------------ внешний мир (подменяется в тестах)


def _port_busy(port: int) -> bool:
    """Занят ли порт: если к нему удаётся подключиться — на нём кто-то слушает. (bind() ненадёжен: на Windows
    SO_REUSEADDR позволяет «занять» уже занятый порт, а на Linux TIME_WAIT даёт ложное «занято».)"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _tcp_wait(host: str, port: int, timeout: float, sleep=time.sleep, clock=time.monotonic) -> bool:
    deadline = clock() + timeout
    while True:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            if clock() >= deadline:
                return False
            sleep(1.0)


def _http_ready(url: str) -> bool:
    try:
        import ssl
        # своя проверка готовности на 127.0.0.1: сертификат выпущен для адреса в сети, поэтому без проверки имени
        ctx = ssl._create_unverified_context() if url.startswith("https://") else None  # noqa: S323
        with urllib.request.urlopen(url, timeout=2, context=ctx) as r:
            return r.status == 200
    except urllib.error.HTTPError as exc:   # ответ «не 2xx» — тоже «ещё не готово»; ответ закрываем (иначе ResourceWarning)
        exc.close()
        return False
    except Exception:    # noqa: BLE001 — любая ошибка = «ещё не готово»
        return False


def _worker_status(path: Path) -> dict | None:
    """Файл статуса воркера (app/pipeline/ml_runtime.py): появляется, когда модели загружены. None — ещё не готов."""
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _find_lms(which) -> str | None:
    """Утилита LM Studio (lms): в PATH или в обычном месте установки (~/.lmstudio/bin)."""
    found = which("lms")
    if found:
        return found
    candidate = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if sys.platform.startswith("win") else "lms")
    return str(candidate) if candidate.is_file() else None


def _local_vision_url(settings) -> str:
    """Адрес модели «зрение + текст» на этом компьютере ("" — выключена или указан другой компьютер)."""
    url = (getattr(settings, "naming_vision_url", "") or "") if getattr(settings, "naming_enabled", False) else ""
    return url if any(h in url for h in ("127.0.0.1", "localhost")) else ""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def builtin_vision(d: "Deps", settings, gpu_name: str | None) -> tuple[Service, str] | None:
    """Свой движок модели «зрение + текст» (scripts/vision_engine.py, models/vision): служба и адрес для NAMING_VISION_URL.
    None — модель выключена, указан другой компьютер или движок не установлен (тогда — LM Studio, если она есть)."""
    if not _local_vision_url(settings) or not vision_engine.is_installed(d.root):
        return None
    device = d.pick_device(vision_engine.engine_exe(d.root), gpu_name)
    port = d.free_port()
    d.out("Модель «зрение + текст»: свой движок программы, " + ("на видеокарте" if device != "none" else "на процессоре")
          + " (первый ответ — после загрузки модели, около 10 с; журнал logs/vision.log).")
    return Service("vision", vision_engine.server_argv(d.root, port, device), optional=True), f"http://127.0.0.1:{port}/v1"


def vision_service(d: "Deps", settings, gpu_name: str | None, child_env: dict) -> Service | None:
    """Модель «зрение + текст»: свой движок (служба запускается и останавливается вместе с программой, адрес — в child_env)
    или, если его нет, LM Studio."""
    found = builtin_vision(d, settings, gpu_name)
    if found is None:
        start_local_vision(d, settings)
        return None
    child_env["NAMING_VISION_URL"] = found[1]
    return found[0]


def start_local_vision(d: "Deps", settings) -> None:
    """Запасной путь, если своего движка нет (models/vision не скачана): локальная модель в LM Studio на этом компьютере.
    Включаем её сервер. Не получилось — не беда: штрихкод и поиск по базе работают и без неё."""
    if not _local_vision_url(settings):
        return
    lms = d.find_lms(d.which)
    if not lms:
        d.out("Модель «зрение + текст» не установлена — товары вне базы будут называться только по штрихкоду. "
              "Установить: запустите «Установить.bat» ещё раз (скачает её сам).")
        return
    try:
        res = d.run_cmd([lms, "server", "start"], cwd=str(d.root), capture_output=True, timeout=60)
        ok = getattr(res, "returncode", 1) == 0
    except Exception:            # noqa: BLE001
        ok = False
    d.out("Модель «зрение + текст» (LM Studio): " + ("сервер включён." if ok else
          "не удалось включить сервер — откройте LM Studio и включите Local Server (вкладка Developer)."))


def _load_settings():
    from app.core.config import get_settings   # лениво: тяжёлый импорт и зависимости нужны только при реальном запуске
    return get_settings()


class MigrateError(RuntimeError):
    pass


def _migrate() -> str:
    """Схема базы — до последней ревизии (как bootstrap): после обновления программы владелец просто запускает start_demo.
    -> текст для окна ("" — обновлять было нечего). Старая база с расхождениями — MigrateError, база не тронута."""
    from app.db.migrate import upgrade_to_head
    from app.db.session import engine
    result = upgrade_to_head(engine)
    if result.drift:
        raise MigrateError("схема базы старее базовой ревизии — ничего не изменено. Запустите: python -m scripts.bootstrap")
    if result.before != result.after:
        return f"Схема базы обновлена: ревизия {result.before or 'нет'} -> {result.after}."
    return ""


def _preflight() -> int:
    from scripts import preflight
    return preflight.main()


def _setup_link(python: str, root: Path, env: dict) -> dict | None:
    """Ссылка «Придумайте ПИН», если ПИН ещё ни у кого нет (scripts/setup_link.py, тем же окружением, что и API)."""
    try:
        res = subprocess.run([python, "-m", "scripts.setup_link", "--json", "--if-needed", "--no-open"], cwd=str(root), env=env,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return json.loads((res.stdout or "").strip().splitlines()[-1])
    except Exception:            # noqa: BLE001 — без ссылки программа работает: вход паролем, «Забыли ПИН.bat»
        return None


@dataclass
class Deps:
    popen: Callable = subprocess.Popen
    run_cmd: Callable = subprocess.run
    which: Callable = shutil.which
    port_busy: Callable[[int], bool] = _port_busy
    tcp_wait: Callable[[str, int, float], bool] = _tcp_wait
    http_ready: Callable[[str], bool] = _http_ready
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    load_settings: Callable[[], object] = _load_settings
    preflight: Callable[[], int] = _preflight
    migrate: Callable[[], str] = _migrate
    lan_ip: Callable[[], str | None] = phone_url.lan_ip
    render_qr: Callable[[str], str | None] = phone_url.render_qr
    worker_status: Callable[[Path], dict | None] = _worker_status
    find_lms: Callable = _find_lms
    pick_device: Callable = vision_engine.pick_device
    free_port: Callable[[], int] = _free_port
    tie_child: Callable = child_tie.tie_to_this_process
    probe_hw: Callable[[], object] = _probe_hw
    apply_env: Callable[[dict], None] = os.environ.update
    hf_cache: Callable[[], Path] = _hf_model_cache
    open_browser: Callable[[str], object] = webbrowser.open
    setup_link: Callable[[str, Path, dict], dict | None] = _setup_link
    out: Callable[[str], None] = print
    platform: str = sys.platform
    environ: dict = field(default_factory=lambda: dict(os.environ))
    python: str = sys.executable
    root: Path = field(default_factory=lambda: ROOT)


@dataclass
class Options:
    port: int = 8000
    no_docker: bool = False
    no_browser: bool = False
    check: bool = False
    timeout: float = 90.0
    lite: bool = False
    cpu: bool = False
    vision: str = "auto"
    public: str | None = None        # вход покупателя из интернета: None — PUBLIC_ACCESS из .env (docs/PUBLIC_ACCESS.md)


# ------------------------------------------------------------------ вспомогательное


def _tail(path: Path, lines: int = 12) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(журнал недоступен)"
    return "\n".join("    " + ln for ln in text.splitlines()[-lines:]) or "    (журнал пуст)"


def _stop_all(procs: list, out) -> None:
    for svc, proc, logf in reversed(procs):
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        except Exception as exc:    # noqa: BLE001 — остановка не должна прерываться на одной службе
            out(f"  не удалось остановить {svc.name}: {exc}")
        finally:
            try:
                logf.close()
            except Exception:       # noqa: BLE001
                pass


def _report_dead(dead: list, root: Path, out) -> None:
    for svc, code in dead:
        out(f"\nСлужба «{svc.name}» остановилась (код {code}). Последние строки logs/{svc.name}.log:")
        out(_tail(root / "logs" / f"{svc.name}.log"))


def _dead(procs: list) -> list:
    """Службы, которые уже завершились: [(служба, код)]. Один опрос на процесс за проход (код завершения у ушедшего
    процесса не меняется, так что дело не в корректности — просто дешевле и проще для подставных процессов в тестах)."""
    dead = []
    for svc, proc, _ in procs:
        if svc.optional:
            continue
        code = proc.poll()
        if code is not None:
            dead.append((svc, code))
    return dead


def _check_optional(procs: list, reported: set, root: Path, out) -> None:
    """Необязательная служба (туннель) остановилась: сказать один раз, программа работает дальше в сети магазина."""
    for svc, proc, _ in procs:
        if svc.optional and svc.name not in reported:
            code = proc.poll()
            if code is not None:
                reported.add(svc.name)
                out(f"\nВход покупателя из интернета остановился (код {code}): QR из интернета не работает, сеть магазина — "
                    f"работает. Последние строки logs/{svc.name}.log:")
                out(_tail(root / "logs" / f"{svc.name}.log"))


def _customer_port(settings) -> int:
    from app.core.customer_gate import DEFAULT_CUSTOMER_PORT
    return int(getattr(settings, "customer_port", 0) or DEFAULT_CUSTOMER_PORT)


def public_access_plan(opts: "Options", d: "Deps", settings):
    """Вход покупателя из интернета: что запускать (ошибки — понятным текстом; программа всё равно запускается)."""
    try:
        return public_access.plan(settings, customer_port=_customer_port(settings), mode=opts.public, which=d.which,
                                  run_cmd=d.run_cmd)
    except Exception as exc:      # noqa: BLE001 — вход из интернета не должен мешать работе в сети магазина
        mode = public_access.normalize_mode(opts.public if opts.public is not None else getattr(settings, "public_access", "off"))
        return public_access.PublicPlan(mode=mode, problems=[f"не удалось подготовить ({exc.__class__.__name__}: {exc})"])


def apply_public_plan(plan, d: "Deps", opts: "Options", settings, child_env: dict) -> int | None:
    """Сообщает о входе из интернета; возвращает порт входа покупателя, если вход включается (иначе None)."""
    if plan.mode == "off":
        return None
    if plan.problems:
        d.out("Вход покупателя из интернета НЕ включён — программа работает только в сети магазина:")
        for problem in plan.problems:
            d.out("  - " + problem)
        d.out("  Настройка: docs/PUBLIC_ACCESS.md")
        return None
    port = _customer_port(settings)
    if port == opts.port:
        d.out(f"Вход покупателя из интернета НЕ включён: CUSTOMER_PORT={port} совпадает с основным портом.")
        return None
    if d.port_busy(port):
        d.out(f"Вход покупателя из интернета НЕ включён: порт {port} (CUSTOMER_PORT) уже занят.")
        return None
    child_env.update(plan.env)
    child_env["CUSTOMER_PORT"] = str(port)
    child_env["CUSTOMER_PUBLIC_URL"] = plan.shop_url     # сотрудники видят этот адрес и QR в «Настройках» (app/services/customer_qr.py)
    d.out(f"Вход покупателя из интернета ({plan.mode}): {plan.shop_url}")
    return port


def with_tunnel(services: list[Service], plan, customer_port: int | None) -> list[Service]:
    """Туннель — последней службой и необязательной: его сбой не останавливает работу в сети магазина."""
    if customer_port and plan.argv:
        return [*services, Service("tunnel", list(plan.argv), optional=True)]
    return services


def _tunnel_hints(root: Path) -> list[str]:
    """Строки журнала туннеля со ссылкой, которую надо открыть (например, Tailscale просит включить Funnel)."""
    try:
        text = (root / "logs" / "tunnel.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines() if "login.tailscale.com" in ln][:3]


def detector_notice(settings, root: Path) -> str | None:
    """Предупреждение, если обученных весов детектора нет. Воркер в этом случае МОЛЧА (только строка в logs/worker.log)
    переключается на грубую эвристику — демо «работает», но результаты плохи, и без этой строки причина неочевидна."""
    cfg = getattr(settings, "detector", None)
    path = getattr(cfg, "weights_path", None) if cfg is not None else None
    hint = "Обучите детектор на своих фото полки: docs/detector_quickstart.md"
    if not path:
        return f"ВНИМАНИЕ: детектор не настроен (models.detector в config.yaml) — будет грубая эвристика, годная только для простого макета. {hint}"
    full = Path(path) if Path(path).is_absolute() else root / path
    if full.is_file():
        return None
    return f"ВНИМАНИЕ: нет файла весов детектора ({path}) — будет работать грубая эвристика, годная только для простого макета. {hint}"


def _print_qr(d: Deps, url: str) -> None:
    qr = d.render_qr(url)
    if qr:
        try:
            d.out(qr)
        except UnicodeEncodeError:
            d.out("(консоль не показывает QR-код — откройте адрес на телефоне вручную)")
    else:
        d.out("(QR-код не показан: pip install qrcode)")


def _show_addresses(d: Deps, port: int, no_browser: bool, notice: str | None = None, tls: bool = False,
                    public_url: str = "", child_env: dict | None = None) -> None:
    out = d.out
    scheme = "https" if tls else "http"
    local = f"{scheme}://localhost:{port}/ui/"
    # Первый вход (решение владельца 2026-11-15): ПИН ещё ни у кого нет — сразу страница «Придумайте ПИН», без командной строки.
    setup = d.setup_link(d.python, d.root, child_env if child_env is not None else dict(d.environ)) or {}
    setup_url = f"{local}login.html#setup={setup['token']}" if setup.get("needed") and setup.get("token") else ""
    ip = d.lan_ip()
    out("\n" + "=" * 60)
    out("  ВСЁ ЗАПУЩЕНО")
    out("=" * 60)
    if public_url:
        # Вход покупателя из интернета (docs/PUBLIC_ACCESS.md): в QR — этот адрес; телефону не нужен Wi-Fi магазина
        out(f"Покупатели (из интернета, без Wi-Fi магазина) — этот адрес в QR-коде:\n\n    {public_url}\n")
        _print_qr(d, public_url)
        for hint in _tunnel_hints(d.root):
            out("Туннель просит действие: " + hint)
        if ip:
            out(f"Сотрудники (в сети магазина): {phone_url.phone_urls(ip, port, scheme)['home']}")
    elif ip:
        urls = phone_url.phone_urls(ip, port, scheme)
        out(f"Телефон (в той же Wi-Fi сети):\n\n    {urls['home']}\n")
        if not tls:
            # Решение владельца 2026-11-08 (документ 18, §14): по HTTP с телефона вход невозможен — пароль не отправляется.
            out("ВНИМАНИЕ: работа без HTTPS — войти с телефона нельзя (пароли по Wi-Fi без защиты не передаются).\n"
                "Настройка: python -m scripts.make_local_tls --ip " + ip + " — и перезапустите (docs/REMOTE_ACCESS_GUIDE.md, п. 7).\n")
        qr = d.render_qr(urls["shop"])
        if qr:
            try:
                out(qr)
            except UnicodeEncodeError:
                out("(консоль не показывает QR-код — откройте адрес на телефоне вручную)")
        else:
            out("(QR-код не показан: pip install qrcode)")
    else:
        out("IP в локальной сети не определён (нет Wi-Fi?) — телефон не подключится; страницы доступны на этом компьютере.")
    # Документ 18, §12: в запуске без Docker управление действует только с самого компьютера (адрес localhost), с телефонов — нет.
    out(f"Управление (на этом компьютере): {local}admin.html")
    if setup_url:
        out(f"\nПЕРВЫЙ ВХОД ({setup.get('username')}): придумайте ПИН-код на этой странице (действует 30 минут):\n\n    {setup_url}\n")
    else:
        out("Забыли ПИН? Запустите «Забыли ПИН.bat» в папке программы.")
    if notice:
        out("\n" + notice)
    out("Остановить: Ctrl+C в этом окне (или просто закройте его).")
    if not no_browser:
        try:
            d.open_browser(setup_url or f"{local}admin.html")
        except Exception:           # noqa: BLE001 — не критично
            pass


# ------------------------------------------------------------------ основной сценарий


def local_hf_home(root: Path, environ: dict) -> dict[str, str]:
    """Модели в папке программы (models/hf — их скачивает установщик магазина, scripts/install.py): использовать их, если
    HF_HOME не задан явно. Нет папки — как раньше (общая папка моделей пользователя)."""
    local = root / "models" / "hf"
    return {"HF_HOME": str(local)} if local.is_dir() and not environ.get("HF_HOME") else {}


def run(opts: Options, deps: Deps | None = None, services: list[Service] | None = None) -> int:
    d = deps or Deps()
    out = d.out
    out("Запуск демонстрации «Узнать товар»...")
    hf_env = local_hf_home(d.root, d.environ)
    if hf_env:
        d.environ.update(hf_env)
        d.apply_env(hf_env)

    if opts.check:
        out("Предстартовая проверка...")
        if d.preflight() != 0:
            out("Проверка нашла блокирующие проблемы (см. выше). Исправьте [FAIL] и запустите снова.")
            return 1

    if not opts.lite and not opts.no_docker and not d.which("docker"):
        out("Docker не найден — запускаю облегчённый режим (без Docker, база SQLite в data/lite). "
            "Если PostgreSQL и Redis работают без Docker — запустите с --no-docker.")
        opts = replace(opts, lite=True)
    if opts.lite:
        return run_lite(opts, d, services)

    try:
        hw = d.probe_hw()
    except Exception:            # noqa: BLE001 — подсказка о железе не должна мешать запуску
        hw = None
    ram = getattr(hw, "ram_gb", 0) or 0
    if ram and ram < hardware.LOW_RAM_GB:
        out(f"Подсказка: на этом компьютере {ram:.0f} ГБ ОЗУ, а Docker занимает около 2 ГБ. Облегчённый режим: start_lite.bat.")

    if not opts.no_docker:
        if d.which("docker"):
            out("Запускаю Postgres и Redis (docker compose up -d db redis frames)...")
            res = d.run_cmd(["docker", "compose", "up", "-d", "db", "redis", "frames"], cwd=str(d.root))
            if res.returncode != 0:
                out("Не удалось запустить Postgres/Redis. Запущен ли Docker Desktop? "
                    "Если Postgres и Redis уже работают без Docker — запустите с --no-docker. "
                    "Без Docker работает облегчённый режим: start_lite.bat.")
                return 1
        else:
            out("Docker не найден — считаю, что Postgres и Redis уже запущены.")

    try:
        settings = d.load_settings()
    except Exception as exc:        # noqa: BLE001
        out(f"Предупреждение: настройки не загрузились ({exc.__class__.__name__}: {exc}); проверка Postgres/Redis пропущена.")
        settings = None
    notice = None      # детектор полок «Узнать товар» не нужен (документ 07, §3.1): предупреждение о его весах больше не показывается
    if notice:
        out(notice)
    status_file = d.root / "logs" / "worker_status.json"
    child_env = {**d.environ, "ML_STATUS_FILE": str(status_file)}     # где воркер скажет «модели загружены» (ждём его ниже)
    vision = None
    if settings is not None and (hw is None or hardware.plan_lite(hw).vision):
        vision = vision_service(d, settings, getattr(hw, "gpu_name", None), child_env)
    if settings is not None:
        checks = [("Postgres", settings.database_url, 5432), ("Redis", settings.queue_broker_url, 6379)]
        frames_url = (getattr(settings, "frames_redis_url", "") or "").strip()
        if not frames_url and not opts.no_docker:
            # API и воркер — два процесса: кадр «Узнать товар» должен быть виден обоим. Хранилище в памяти процесса API воркеру
            # недоступно (кадр «устарел» на каждом снимке) — поэтому Redis кадров без сохранения на диск (служба frames, D-10а).
            frames_url = DEFAULT_FRAMES_URL
            child_env["FRAMES_REDIS_URL"] = frames_url
        if frames_url:
            checks.append(("Redis кадров", frames_url, 6380))
        for name, url, default in checks:
            host, port = parse_host_port(url, default)
            out(f"Жду {name} ({host}:{port})...")
            if not d.tcp_wait(host, port, 60.0):
                out(f"{name} не отвечает на {host}:{port} за 60 с. Проверьте `docker compose ps` и журналы Docker.")
                return 1
        try:
            message = d.migrate()
        except Exception as exc:        # noqa: BLE001 — без нужной схемы API отвечает «не готов» на каждый запрос
            out(f"Не удалось обновить схему базы: {exc}")
            return 1
        if message:
            out(message)

    if d.port_busy(opts.port):
        out(f"Порт {opts.port} уже занят — возможно, демонстрация уже запущена в другом окне. "
            f"Закройте его или запустите с другим портом: --port 8001.")
        return 1

    tls = tls_files(d.root)
    plan = public_access_plan(opts, d, settings)
    customer_port = apply_public_plan(plan, d, opts, settings, child_env)
    services = services if services is not None else service_commands(d.python, opts.port, d.platform, tls, customer_port)
    services = with_tunnel(services, plan, customer_port) + ([vision] if vision else [])
    return _launch(opts, d, services, child_env, status_file, tls, notice, public_url=plan.shop_url if customer_port else "")


def _launch(opts: Options, d: Deps, services: list[Service], child_env: dict, status_file: Path, tls, notice=None,
            public_url: str = "") -> int:
    """Запуск служб, ожидание готовности API и моделей, адреса и QR, остановка по Ctrl+C (общая часть обоих режимов)."""
    out = d.out
    log_dir = d.root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        status_file.unlink()                       # статус прошлого запуска не должен выдавать себя за текущий
    except OSError:
        pass
    procs: list = []
    try:
        for svc in services:
            logf = open(log_dir / f"{svc.name}.log", "wb")
            procs.append((svc, d.popen(svc.argv, cwd=str(d.root), stdout=logf, stderr=subprocess.STDOUT, env=child_env), logf))
            try:
                d.tie_child(procs[-1][1])            # закроется и при жёстком завершении программы (scripts/child_tie.py)
            except Exception:            # noqa: BLE001 — не получилось: остановится обычным путём (Ctrl+C)
                pass
            out(f"  запущено: {svc.name}  (журнал: logs/{svc.name}.log)")

        health = f"{'https' if tls else 'http'}://127.0.0.1:{opts.port}/health"
        deadline = d.clock() + opts.timeout
        out("Жду готовности API...")
        while not d.http_ready(health):
            dead = _dead(procs)
            if dead:
                _report_dead(dead, d.root, out)
                return 1
            if d.clock() > deadline:
                out(f"API не ответил за {opts.timeout:.0f} с. Смотрите logs/api.log (первый запуск с загрузкой моделей бывает долгим — "
                    f"повторите с --timeout 300).")
                return 1
            d.sleep(0.5)
        # Воркер не отвечает по HTTP: ждём файл статуса — модели распознавания загружены (первый запуск — до минуты). Без этого
        # первое фото сразу после «ВСЁ ЗАПУЩЕНО» получало «сейчас не получается узнать товар».
        out("Жду загрузки моделей распознавания...")
        status = None
        while status is None:
            dead = _dead(procs)
            if dead:
                _report_dead(dead, d.root, out)
                return 1
            status = d.worker_status(status_file)
            if status is None and d.clock() > deadline:
                out("Распознавание ещё загружается — первые снимки могут не получиться. Журнал: logs/worker.log")
                break
            if status is None:
                d.sleep(0.5)
        if status is not None and not status.get("ready", False):
            out("ВНИМАНИЕ: распознавание по виду не готово (работает только штрихкод): "
                + "; ".join(str(p) for p in status.get("problems") or []) + ". Журнал: logs/worker.log")

        write_run_mode(lite=opts.lite, port=opts.port, scheme="https" if tls else "http",
                       database_url=child_env.get("DATABASE_URL"), path=d.root / "data" / "run_mode.json")
        _show_addresses(d, opts.port, opts.no_browser, notice, tls=bool(tls), public_url=public_url, child_env=child_env)
        reported: set = set()
        while True:
            d.sleep(1.0)
            _check_optional(procs, reported, d.root, out)
            dead = _dead(procs)
            if dead:
                _report_dead(dead, d.root, out)
                return 1
    except KeyboardInterrupt:
        out("\nОстановка...")
        return 0
    finally:
        _stop_all(procs, out)


def run_lite(opts: Options, d: Deps, services: list[Service] | None = None) -> int:
    """Облегчённый режим: без Docker, одна служба (API с моделями внутри), база SQLite в data/lite. Что включить — по железу."""
    out = d.out
    try:
        hw = d.probe_hw()
    except Exception:            # noqa: BLE001
        hw = hardware.Hardware(ram_gb=0.0, cores=os.cpu_count() or 1)
    plan = hardware.plan_lite(hw, force_cpu=opts.cpu, vision=opts.vision)
    out("Облегчённый режим (без Docker). Компьютер: " + hw.describe())
    out("  распознавание по виду: " + ("видеокарта" if plan.device == "auto" and hw.gpu_name else "процессор")
        + "; модель «зрение + текст»: " + ("включена" if plan.vision else "выключена"))
    for note in plan.notes:
        out("  " + note)
    (d.root / LITE_DIR / "index").mkdir(parents=True, exist_ok=True)
    env = lite_environment(d.root, plan, d.hf_cache())
    status_file = d.root / "logs" / "lite_status.json"     # свой файл и свой журнал (logs/lite.log): не путать с полным режимом
    env["ML_STATUS_FILE"] = str(status_file)
    d.apply_env(env)                                   # и этому процессу: настройки ниже (LM Studio) — облегчённого режима
    child_env = {**d.environ, **env}

    # база: миграции и первый администратор (ADMIN_USERNAME/ADMIN_PASSWORD из .env) — тем же bootstrap, что и в Docker
    out("Готовлю базу облегчённого режима (data/lite/shop.db)...")
    res = d.run_cmd([d.python, "-m", "scripts.bootstrap", "--non-interactive", "--wait-timeout", "5"], cwd=str(d.root),
                    env=child_env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if getattr(res, "returncode", 1) != 0:
        out("Не удалось подготовить базу облегчённого режима (data/lite/shop.db):")
        out(((getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")).strip()[-3000:])
        return 1
    for line in (getattr(res, "stdout", "") or "").splitlines():
        if line.startswith("[4/4]") and "уже есть" not in line:    # первый запуск: что с администратором (пароля в выводе нет)
            out("  " + line.strip())

    vision = None
    if plan.vision:
        try:
            vision = vision_service(d, d.load_settings(), hw.gpu_name if plan.device == "auto" else None, child_env)
        except Exception:        # noqa: BLE001 — без модели «зрение + текст» программа работает (штрихкод и база)
            vision = None

    if d.port_busy(opts.port):
        out(f"Порт {opts.port} уже занят — возможно, программа уже запущена в другом окне. "
            f"Закройте его или запустите с другим портом: --port 8001.")
        return 1
    tls = tls_files(d.root)
    try:
        settings = d.load_settings()
    except Exception:            # noqa: BLE001 — без настроек вход из интернета просто не включается
        settings = None
    plan = public_access_plan(opts, d, settings)
    customer_port = apply_public_plan(plan, d, opts, settings, child_env)
    services = services if services is not None else lite_commands(d.python, opts.port, tls, customer_port)
    services = with_tunnel(services, plan, customer_port) + ([vision] if vision else [])
    return _launch(opts, d, services, child_env, status_file, tls, public_url=plan.shop_url if customer_port else "")


def main(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):      # окно без UTF-8 (вывод в файл, старая консоль): не падать на русских буквах журнала
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-docker", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--check", action="store_true", help="перед запуском выполнить scripts.preflight")
    ap.add_argument("--timeout", type=float, default=90.0, help="сколько ждать готовности API, с (по умолчанию 90)")
    ap.add_argument("--lite", action="store_true", help="облегчённый режим: без Docker, один процесс, база SQLite (data/lite)")
    ap.add_argument("--cpu", action="store_true", help="облегчённый режим, модель только на процессоре (даже если есть видеокарта)")
    ap.add_argument("--vision", choices=["auto", "on", "off"], default="auto",
                    help="модель «зрение + текст» в облегчённом режиме: auto — по железу (по умолчанию)")
    ap.add_argument("--public", choices=list(public_access.MODES), default=None,
                    help="вход покупателя из интернета (docs/PUBLIC_ACCESS.md); по умолчанию — PUBLIC_ACCESS из .env")
    a = ap.parse_args(argv)
    return run(Options(port=a.port, no_docker=a.no_docker, no_browser=a.no_browser, check=a.check, timeout=a.timeout,
                       lite=a.lite or a.cpu, cpu=a.cpu, vision=a.vision, public=a.public), deps)


if __name__ == "__main__":
    raise SystemExit(main())
