"""
Запуск всей демонстрации одним действием: Postgres+Redis (Docker), API, воркер -> ждёт готовности ->
показывает адрес и QR-код для телефона. Ctrl+C (или закрытие окна) останавливает API и воркер.

    python -m scripts.start_demo                  # обычный запуск
    python -m scripts.start_demo --check          # сначала предстартовая проверка (scripts.preflight)
    python -m scripts.start_demo --no-docker      # Postgres и Redis уже запущены без Docker
    python -m scripts.start_demo --no-browser     # не открывать дашборд на этом компьютере
    python -m scripts.start_demo --port 8080

Двойной щелчок: `start_demo.bat` в корне проекта; ярлык с иконкой на рабочем столе — `python -m scripts.make_shortcut`.
Журналы служб — в папке logs/ (api.log, worker.log): при сбое последние строки показываются сразу.

Воркер запускается в ОДНОМ процессе (на Windows --pool=solo обязателен; на остальных --concurrency=1): индекс каталога
пока не рассчитан на несколько процессов (docs/HANDOFF.md, №14). Postgres и Redis после остановки НЕ выключаются
(данные сохраняются; выключить — `docker compose stop`).
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from scripts import phone_url
from scripts.preflight import parse_host_port

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRAMES_URL = "redis://127.0.0.1:6380/0"    # служба frames из docker-compose.yml (порт FRAMES_PORT, только 127.0.0.1)
STOP_TIMEOUT = 8.0


@dataclass(frozen=True)
class Service:
    name: str
    argv: list[str]


def tls_files(root: Path) -> tuple[str, str] | None:
    """Сертификат HTTPS сети магазина (scripts/make_local_tls.py): есть — API запускается по HTTPS (документ 18, §14)."""
    cert, key = root / "secrets" / "tls" / "server.crt", root / "secrets" / "tls" / "server.key"
    return (str(cert), str(key)) if cert.is_file() and key.is_file() else None


def service_commands(python: str, port: int, platform: str, tls: tuple[str, str] | None = None) -> list[Service]:
    api_argv = [python, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)]
    if tls:
        api_argv += ["--ssl-certfile", tls[0], "--ssl-keyfile", tls[1]]
    api = Service("api", api_argv)
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


def start_local_vision(d: "Deps", settings) -> None:
    """«Назвать товар вне базы» (документ 07, §10.6) по фото — локальная модель в LM Studio на этом компьютере. Включаем её сервер
    (модель LM Studio загружает сама при первом запросе). Не получилось — не беда: штрихкод и поиск по базе работают и без неё."""
    url = (getattr(settings, "naming_vision_url", "") or "") if getattr(settings, "naming_enabled", False) else ""
    if not url or not any(h in url for h in ("127.0.0.1", "localhost")):
        return
    lms = d.find_lms(d.which)
    if not lms:
        d.out("LM Studio не найден — товары вне базы будут называться только по штрихкоду (docs/RUNBOOK.md, «Назвать любой товар»).")
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


def _preflight() -> int:
    from scripts import preflight
    return preflight.main()


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
    lan_ip: Callable[[], str | None] = phone_url.lan_ip
    render_qr: Callable[[str], str | None] = phone_url.render_qr
    worker_status: Callable[[Path], dict | None] = _worker_status
    find_lms: Callable = _find_lms
    open_browser: Callable[[str], object] = webbrowser.open
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
        code = proc.poll()
        if code is not None:
            dead.append((svc, code))
    return dead


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


def _show_addresses(d: Deps, port: int, no_browser: bool, notice: str | None = None, tls: bool = False) -> None:
    out = d.out
    scheme = "https" if tls else "http"
    local = f"{scheme}://localhost:{port}/ui/"
    ip = d.lan_ip()
    out("\n" + "=" * 60)
    out("  ВСЁ ЗАПУЩЕНО")
    out("=" * 60)
    if ip:
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
    out(f"На этом компьютере: {local}index.html   (разделы: {local}identify.html   {local}products.html)")
    # Документ 18, §12: в запуске без Docker управление действует только с самого компьютера (адрес localhost), с телефонов — нет.
    out(f"Управление (только на этом компьютере): {local}admin.html")
    if notice:
        out("\n" + notice)
    out("Остановить: Ctrl+C в этом окне (или просто закройте его).")
    if not no_browser:
        try:
            d.open_browser(f"{local}index.html")
        except Exception:           # noqa: BLE001 — не критично
            pass


# ------------------------------------------------------------------ основной сценарий


def run(opts: Options, deps: Deps | None = None, services: list[Service] | None = None) -> int:
    d = deps or Deps()
    out = d.out
    out("Запуск демонстрации «Узнать товар»...")

    if opts.check:
        out("Предстартовая проверка...")
        if d.preflight() != 0:
            out("Проверка нашла блокирующие проблемы (см. выше). Исправьте [FAIL] и запустите снова.")
            return 1

    if not opts.no_docker:
        if d.which("docker"):
            out("Запускаю Postgres и Redis (docker compose up -d db redis frames)...")
            res = d.run_cmd(["docker", "compose", "up", "-d", "db", "redis", "frames"], cwd=str(d.root))
            if res.returncode != 0:
                out("Не удалось запустить Postgres/Redis. Запущен ли Docker Desktop? "
                    "Если Postgres и Redis уже работают без Docker — запустите с --no-docker.")
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
    if settings is not None:
        start_local_vision(d, settings)
    status_file = d.root / "logs" / "worker_status.json"
    child_env = {**d.environ, "ML_STATUS_FILE": str(status_file)}     # где воркер скажет «модели загружены» (ждём его ниже)
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

    if d.port_busy(opts.port):
        out(f"Порт {opts.port} уже занят — возможно, демонстрация уже запущена в другом окне. "
            f"Закройте его или запустите с другим портом: --port 8001.")
        return 1

    tls = tls_files(d.root)
    services = services if services is not None else service_commands(d.python, opts.port, d.platform, tls)
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

        _show_addresses(d, opts.port, opts.no_browser, notice, tls=bool(tls))
        while True:
            d.sleep(1.0)
            dead = _dead(procs)
            if dead:
                _report_dead(dead, d.root, out)
                return 1
    except KeyboardInterrupt:
        out("\nОстановка...")
        return 0
    finally:
        _stop_all(procs, out)


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
    a = ap.parse_args(argv)
    return run(Options(port=a.port, no_docker=a.no_docker, no_browser=a.no_browser, check=a.check, timeout=a.timeout), deps)


if __name__ == "__main__":
    raise SystemExit(main())
