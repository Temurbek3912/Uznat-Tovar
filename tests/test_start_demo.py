"""
scripts/start_demo.py, scripts/make_shortcut.py, start_demo.bat.

Сценарии запуска/сбоев — на подставных процессах; плюс интеграционные тесты на НАСТОЯЩИХ подпроцессах и настоящем HTTP-сервере
(реальный Popen, ожидание готовности, остановка). Не проверяется: настоящие Docker/uvicorn/celery, Windows (PowerShell, cmd).
"""
from __future__ import annotations

import http.server
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

from app.core import hardware
from scripts import make_shortcut
from scripts import start_demo as sd
from scripts.start_demo import Deps, Options, Service, run, service_commands

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------- подставные части

class FakeProc:
    """Подставной процесс. exits_after — после скольких опросов poll() он «упадёт» (None — не падает)."""
    def __init__(self, name, exits_after=None, code=1, hang_on_terminate=False):
        self.name, self.exits_after, self.code, self.polls = name, exits_after, code, 0
        self.returncode, self.events, self.hang = None, [], hang_on_terminate

    def poll(self):
        self.polls += 1
        if self.returncode is None and self.exits_after is not None and self.polls > self.exits_after:
            self.returncode = self.code
        return self.returncode

    def terminate(self):
        self.events.append("terminate")
        if not self.hang:
            self.returncode = -15

    def wait(self, timeout=None):
        if self.hang and "kill" not in self.events:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.returncode

    def kill(self):
        self.events.append("kill")
        self.returncode = -9


class Env:
    def __init__(self, tmp_path, **over):
        self.lines, self.started, self.docker_calls, self.sleeps, self.opened = [], [], [], [], []
        self.procs, self.applied = {}, {}
        self.exit_plan = over.pop("exit_plan", {})
        self.stop_after = over.pop("stop_after", 6)
        settings = over.pop("settings", NS(database_url="postgresql://u:p@dbhost:5432/x", queue_broker_url="redis://redishost:6379/0"))
        kw = dict(
            popen=self._popen, run_cmd=self._run, which=lambda n: "/usr/bin/docker",
            port_busy=lambda p: False, tcp_wait=lambda h, p, t: True, http_ready=lambda u: True,
            sleep=self._sleep, clock=self._clock, load_settings=lambda: settings, preflight=lambda: 0,
            lan_ip=lambda: "192.168.1.57", render_qr=lambda url: f"[QR {url}]", open_browser=self.opened.append,
            out=self.lines.append, platform="win32", python="PY", root=tmp_path, worker_status=lambda p: {"ready": True},
            migrate=lambda: "", probe_hw=lambda: hardware.Hardware(ram_gb=32, cores=8, gpu_name="RTX", vram_gb=8.0),
            apply_env=self.applied.update, hf_cache=lambda: tmp_path / "no-hf-cache",
        )
        kw.update(over)
        self.deps = Deps(**kw)
        self.t = 0.0

    def _clock(self):
        return self.t

    def _sleep(self, dt):
        self.sleeps.append(dt)
        self.t += dt
        if len(self.sleeps) >= self.stop_after:
            raise KeyboardInterrupt

    def _popen(self, argv, **kw):
        name = "api" if "uvicorn" in argv else "worker"
        proc = FakeProc(name, **self.exit_plan.get(name, {}))
        self.started.append((name, argv, kw))
        self.procs[name] = proc
        return proc

    def _run(self, argv, **kw):
        self.docker_calls.append((argv, kw))
        return NS(returncode=getattr(self, "docker_rc", 0))

    @property
    def text(self):
        return "\n".join(str(x) for x in self.lines)


def go(tmp_path, opts=None, **over):
    env = Env(tmp_path, **over)
    code = run(opts or Options(no_browser=True), env.deps)
    return code, env


# ----------------------------------------------------------------------- команды служб

def test_service_commands_differ_by_platform_and_listen_on_all_interfaces():
    api, worker = service_commands("PY", 8123, "win32")
    assert api.argv == ["PY", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8123"]
    assert worker.argv[:6] == ["PY", "-m", "celery", "-A", "app.worker.celery_app", "worker"] and "--pool=solo" in worker.argv
    assert "--concurrency=1" in service_commands("PY", 8000, "linux")[1].argv                 # один процесс — индекс каталога
    assert "--pool=solo" not in service_commands("PY", 8000, "linux")[1].argv


# ----------------------------------------------------------------------- успешный запуск и остановка

def test_happy_path_starts_in_order_prints_addresses_and_stops_everything_on_ctrl_c(tmp_path):
    code, env = go(tmp_path)
    assert code == 0
    assert env.docker_calls[0][0] == ["docker", "compose", "up", "-d", "db", "redis", "frames"]
    assert [n for n, _, _ in env.started] == ["api", "worker"]
    assert env.started[0][2]["cwd"] == str(tmp_path)
    assert "http://192.168.1.57:8000/ui/index.html" in env.text and "[QR http://192.168.1.57:8000/ui/shop.html]" in env.text
    assert "Ctrl+C" in env.text and "Остановка" in env.text
    assert env.procs["api"].events == ["terminate"] and env.procs["worker"].events == ["terminate"]
    assert (tmp_path / "logs" / "api.log").exists() and (tmp_path / "logs" / "worker.log").exists()


def test_database_schema_is_upgraded_before_the_services_start(tmp_path):
    """После обновления программы (новая миграция) владелец просто запускает start_demo: схема обновляется сама."""
    order = []
    code, env = go(tmp_path, migrate=lambda: order.append("migrate") or "Схема базы обновлена: ревизия 0005 -> 0006.")
    assert code == 0 and order == ["migrate"]
    assert "Схема базы обновлена: ревизия 0005 -> 0006." in env.text and [n for n, _, _ in env.started] == ["api", "worker"]


def test_schema_that_cannot_be_upgraded_stops_with_a_plain_message(tmp_path):
    def fail():
        raise RuntimeError("схема базы старее базовой ревизии")
    code, env = go(tmp_path, migrate=fail)
    assert code == 1 and env.started == []
    assert "Не удалось обновить схему базы: схема базы старее базовой ревизии" in env.text


def test_services_are_stopped_in_reverse_order(tmp_path):
    order = []
    class Recording(FakeProc):
        def terminate(self):
            order.append(self.name)
            super().terminate()
    env = Env(tmp_path)
    env._popen = lambda argv, **kw: Recording("api" if "uvicorn" in argv else "worker")
    env.deps.popen = env._popen
    run(Options(no_browser=True), env.deps)
    assert order == ["worker", "api"]


def test_browser_opens_home_page_unless_disabled_and_browser_errors_are_ignored(tmp_path):
    _, env = go(tmp_path, Options(no_browser=False))
    assert env.opened == ["http://localhost:8000/ui/index.html"]
    _, env = go(tmp_path, Options(no_browser=True))
    assert env.opened == []
    def boom(url):
        raise RuntimeError("no browser")
    code, _ = go(tmp_path, Options(no_browser=False), open_browser=boom)
    assert code == 0


def test_custom_port_is_used_for_services_health_and_urls(tmp_path):
    seen = []
    code, env = go(tmp_path, Options(port=8123, no_browser=True), http_ready=lambda u: (seen.append(u), True)[1])
    assert seen[0] == "http://127.0.0.1:8123/health" and "--port" in env.started[0][1] and "8123" in env.started[0][1]
    assert "http://192.168.1.57:8123/ui/index.html" in env.text


# ----------------------------------------------------------------------- предупреждение об отсутствии весов детектора

def _settings_with_detector(path):
    return NS(database_url="postgresql://u:p@dbhost:5432/x", queue_broker_url="redis://redishost:6379/0",
              detector=NS(weights_path=path))


def test_missing_detector_weights_are_not_announced_detector_is_not_needed(tmp_path):
    # «Узнать товар» работает без детектора полок (2026-11-14): предупреждение о его весах только пугало бы
    code, env = go(tmp_path, settings=_settings_with_detector("models/detector/y.pt"))
    assert code == 0 and not [l for l in env.lines if "детектор" in str(l)]


def test_frames_store_is_shared_by_api_and_worker(tmp_path):
    # API и воркер — два процесса: без общего Redis кадров воркер не видит кадр («кадр устарел» на каждом снимке)
    code, env = go(tmp_path, settings=_settings_with_detector("models/detector/y.pt"))
    assert code == 0
    envs = [kw.get("env") for _, _, kw in env.started]
    assert len(envs) == 2 and all(e and e.get("FRAMES_REDIS_URL") == "redis://127.0.0.1:6380/0" for e in envs)
    assert all(e.get("ML_STATUS_FILE", "").endswith("worker_status.json") for e in envs)


def test_waits_for_the_worker_models_before_announcing_and_warns_when_recognition_is_not_ready(tmp_path):
    answers = iter([None, None, {"ready": False, "problems": ["embedder: нет интернета"]}])
    code, env = go(tmp_path, worker_status=lambda p: next(answers), stop_after=20)
    assert code == 0
    text = env.text
    assert "Жду загрузки моделей" in text and "работает только штрихкод" in text and "нет интернета" in text
    assert text.index("работает только штрихкод") < text.index("ВСЁ ЗАПУЩЕНО")


def test_existing_detector_weights_produce_no_notice_relative_and_absolute(tmp_path):
    (tmp_path / "models" / "detector").mkdir(parents=True)
    (tmp_path / "models" / "detector" / "y.pt").write_bytes(b"w")
    _, env = go(tmp_path, settings=_settings_with_detector("models/detector/y.pt"))
    assert "эвристика" not in env.text
    _, env = go(tmp_path, settings=_settings_with_detector(str(tmp_path / "models" / "detector" / "y.pt")))
    assert "эвристика" not in env.text


def test_detector_notice_covers_missing_config_and_unknown_settings(tmp_path):
    assert "не настроен" in sd.detector_notice(NS(detector=None), tmp_path)
    assert "не настроен" in sd.detector_notice(NS(detector=NS(weights_path="")), tmp_path)
    assert "не настроен" in sd.detector_notice(NS(), tmp_path)
    assert sd.detector_notice(_settings_with_detector("w.pt"), tmp_path).startswith("ВНИМАНИЕ")
    code, env = go(tmp_path, load_settings=lambda: (_ for _ in ()).throw(ValueError("bad")))
    assert "весов детектора" not in env.text and "не настроен" not in env.text                  # настройки не загрузились — не гадаем


# ----------------------------------------------------------------------- Docker, Postgres, Redis

def test_docker_missing_switches_to_lite_mode_and_failed_docker_stops_everything(tmp_path):
    code, env = go(tmp_path, which=lambda n: None)
    assert code == 0 and "Docker не найден" in env.text and "облегчённый режим" in env.text
    assert [argv for argv, _ in env.docker_calls if argv[0] == "docker"] == []          # Docker не вызывался
    assert [n for n, _, _ in env.started] == ["api"] and env.applied.get("LITE_MODE") == "1"   # одна служба, без воркера
    env = Env(tmp_path)
    env.docker_rc = 1
    assert run(Options(no_browser=True), env.deps) == 1
    assert "Docker Desktop" in env.text and "--no-docker" in env.text and "start_lite.bat" in env.text and env.started == []


def test_no_docker_flag_never_touches_docker(tmp_path):
    asked = []
    code, env = go(tmp_path, Options(no_docker=True, no_browser=True), which=lambda n: asked.append(n) or "/x")
    assert code == 0 and asked == [] and env.docker_calls == []


def test_postgres_or_redis_not_answering_aborts_before_starting_services(tmp_path):
    waited = []
    def tcp_wait(host, port, timeout):
        waited.append((host, port))
        return port != 6379
    code, env = go(tmp_path, tcp_wait=tcp_wait)
    assert code == 1 and waited == [("dbhost", 5432), ("redishost", 6379)]
    assert "Redis не отвечает" in env.text and env.started == []


def test_settings_failure_is_a_warning_not_a_blocker(tmp_path):
    def broken():
        raise ValueError("bad config")
    code, env = go(tmp_path, load_settings=broken)
    assert code == 0 and "настройки не загрузились" in env.text and "bad config" in env.text


def test_busy_port_is_reported_with_a_hint_and_nothing_starts(tmp_path):
    code, env = go(tmp_path, port_busy=lambda p: True)
    assert code == 1 and "уже занят" in env.text and "--port" in env.text and env.started == []


def test_check_flag_runs_preflight_first_and_blocks_on_failure(tmp_path):
    calls = []
    code, env = go(tmp_path, Options(check=True, no_browser=True), preflight=lambda: (calls.append("pf"), 1)[1])
    assert code == 1 and calls == ["pf"] and env.docker_calls == [] and env.started == []
    code, env = go(tmp_path, Options(check=True, no_browser=True), preflight=lambda: 0)
    assert code == 0 and env.started


# ----------------------------------------------------------------------- сбои служб

def test_api_that_never_gets_ready_times_out_and_everything_is_stopped(tmp_path):
    code, env = go(tmp_path, Options(timeout=3.0, no_browser=True), http_ready=lambda u: False, stop_after=999)
    assert code == 1 and "не ответил за 3 с" in env.text and "--timeout 300" in env.text
    assert env.procs["api"].events == ["terminate"] and env.procs["worker"].events == ["terminate"]


def test_service_dying_during_startup_shows_its_log_tail_and_stops_the_others(tmp_path):
    def popen_and_log(argv, **kw):
        name = "api" if "uvicorn" in argv else "worker"
        kw["stdout"].write(b"line1\nModuleNotFoundError: No module named 'torch'\n")
        kw["stdout"].flush()
        proc = FakeProc(name, exits_after=0 if name == "api" else None, code=3)
        env.procs[name] = proc
        return proc
    env = Env(tmp_path, http_ready=lambda u: False)
    env.deps.popen = popen_and_log
    assert run(Options(no_browser=True), env.deps) == 1
    assert "Служба «api» остановилась (код 3)" in env.text and "No module named 'torch'" in env.text
    assert env.procs["worker"].events == ["terminate"]


def test_worker_crashing_right_after_api_is_ready_is_detected(tmp_path):
    # воркер падает сразу (нет весов, нет Redis…): API уже отвечает, но «всё запущено» говорить рано
    code, env = go(tmp_path, exit_plan={"worker": {"exits_after": 0, "code": 2}})
    assert code == 1 and "Служба «worker» остановилась (код 2)" in env.text and "ВСЁ ЗАПУЩЕНО" not in env.text
    assert env.procs["api"].events == ["terminate"]


def test_service_dying_while_running_ends_the_demo_with_an_error(tmp_path):
    code, env = go(tmp_path, exit_plan={"api": {"exits_after": 5, "code": 1}}, stop_after=999)
    assert code == 1 and "ВСЁ ЗАПУЩЕНО" in env.text and "Служба «api» остановилась" in env.text
    assert env.procs["worker"].events == ["terminate"]


def test_unresponsive_service_is_killed_after_the_terminate_timeout(tmp_path):
    code, env = go(tmp_path, exit_plan={"worker": {"hang_on_terminate": True}})
    assert code == 0 and env.procs["worker"].events == ["terminate", "kill"] and env.procs["api"].events == ["terminate"]


def test_missing_lan_ip_qr_and_console_encoding_problems_do_not_break_startup(tmp_path):
    code, env = go(tmp_path, lan_ip=lambda: None)
    assert code == 0 and "IP в локальной сети не определён" in env.text
    code, env = go(tmp_path, render_qr=lambda url: None)
    assert code == 0 and "pip install qrcode" in env.text
    lines = []
    def picky(msg):
        if "[QR" in str(msg):
            raise UnicodeEncodeError("cp866", "█", 0, 1, "x")
        lines.append(str(msg))
    code, _ = go(tmp_path, out=picky)
    assert code == 0 and any("консоль не показывает QR-код" in l for l in lines)


# ----------------------------------------------------------------------- настоящие процессы

class _Health(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


class _Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _real_env(tmp_path, port, **over):
    """Настоящие Popen/http_ready, подставлены только docker/настройки/браузер/сон."""
    popen_log = []
    def popen(argv, **kw):
        p = subprocess.Popen(argv, **kw)
        popen_log.append(p)
        return p
    lines, sleeps = [], []
    def sleep(dt):
        sleeps.append(dt)
        if len(sleeps) >= 4:
            raise KeyboardInterrupt
        time.sleep(0.05)
    kw = dict(popen=popen, http_ready=sd._http_ready, worker_status=lambda p: {"ready": True}, which=lambda n: None, load_settings=lambda: None,
              port_busy=lambda p: False, sleep=sleep, out=lines.append, lan_ip=lambda: "10.0.0.2", render_qr=lambda u: None,
              open_browser=lambda u: None, platform="linux", python=sys.executable, root=tmp_path, migrate=lambda: "")
    kw.update(over)
    return Deps(**kw), popen_log, lines


def test_real_subprocesses_start_become_ready_and_are_really_terminated(tmp_path):
    srv = _Srv(("127.0.0.1", 0), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        deps, popen_log, lines = _real_env(tmp_path, port)
        sleeper = [sys.executable, "-c", "import time; print('service up', flush=True); time.sleep(120)"]
        code = run(Options(port=port, no_docker=True, no_browser=True, timeout=10), deps,
                   services=[Service("api", sleeper), Service("worker", sleeper)])
        assert code == 0
        assert len(popen_log) == 2 and all(p.poll() is not None for p in popen_log)        # реально остановлены, не висят
        assert "service up" in (tmp_path / "logs" / "api.log").read_text()                  # вывод службы попал в журнал
        assert any("ВСЁ ЗАПУЩЕНО" in l for l in lines)
    finally:
        srv.shutdown()
        srv.server_close()


def test_real_service_that_exits_immediately_is_reported_with_its_output(tmp_path):
    srv = _Srv(("127.0.0.1", 0), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        deps, popen_log, lines = _real_env(tmp_path, port, http_ready=lambda u: False)
        crash = [sys.executable, "-c", "import sys; print('ImportError: boom 42'); sys.exit(3)"]
        code = run(Options(port=port, no_docker=True, no_browser=True, timeout=10), deps,
                   services=[Service("api", crash), Service("worker", [sys.executable, "-c", "import time; time.sleep(60)"])])
        assert code == 1
        text = "\n".join(lines)
        assert "Служба «api» остановилась (код 3)" in text and "ImportError: boom 42" in text
        assert all(p.poll() is not None for p in popen_log)                                  # соседняя служба тоже остановлена
    finally:
        srv.shutdown()
        srv.server_close()


def test_real_port_busy_detection():
    srv = _Srv(("127.0.0.1", 0), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        assert sd._port_busy(port) is True
    finally:
        srv.shutdown()
        srv.server_close()
    assert sd._port_busy(port) is False


def test_real_http_ready_and_tcp_wait():
    srv = _Srv(("127.0.0.1", 0), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        assert sd._http_ready(f"http://127.0.0.1:{port}/health") is True
        assert sd._http_ready(f"http://127.0.0.1:{port}/nope") is False
        assert sd._tcp_wait("127.0.0.1", port, 2.0) is True
    finally:
        srv.shutdown()
        srv.server_close()
    assert sd._http_ready(f"http://127.0.0.1:{port}/health") is False
    ticks = {"t": 0.0}
    assert sd._tcp_wait("127.0.0.1", port, 1.5, sleep=lambda dt: ticks.__setitem__("t", ticks["t"] + dt),
                        clock=lambda: ticks["t"]) is False


# ----------------------------------------------------------------------- ярлык на рабочем столе

def test_ps_quote_doubles_apostrophes():
    assert make_shortcut.ps_quote("C:\\Users\\O'Neil\\app") == "'C:\\Users\\O''Neil\\app'"
    assert make_shortcut.ps_quote("a b") == "'a b'"


def test_powershell_script_has_all_parts_and_safe_quoting():
    script = make_shortcut.build_ps_script(target=Path("C:/Users/O'Neil/proj/start_demo.bat"), workdir=Path("C:/Users/O'Neil/proj"),
                                           icon=Path("C:/Users/O'Neil/proj/frontend/icons/app.ico"))
    assert "GetFolderPath('Desktop')" in script                                   # а не путь, угаданный по имени пользователя
    assert "'Узнать товар.lnk'" in script and "O''Neil" in script and "O'Neil" not in script.replace("O''Neil", "")
    assert "start_demo.bat'" in script and "app.ico,0'" in script and "WorkingDirectory" in script and "$s.Save()" in script
    assert "\r\n" in script and "\n" not in script.replace("\r\n", "")


def test_shortcut_refuses_other_platforms_and_missing_files(tmp_path):
    lines = []
    assert make_shortcut.main([], platform="linux", out=lines.append) == 1 and "только в Windows" in lines[0]
    lines.clear()
    assert make_shortcut.main([], platform="win32", root=tmp_path, out=lines.append) == 1 and "Не найден файл" in lines[0]


def test_shortcut_runs_powershell_with_a_bom_script_and_reports_results(tmp_path):
    (tmp_path / "frontend" / "icons").mkdir(parents=True)
    (tmp_path / "start_demo.bat").write_text("x")
    (tmp_path / "frontend" / "icons" / "app.ico").write_bytes(b"ico")
    seen = {}
    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["bytes"] = Path(argv[-1]).read_bytes()                                # файл существует только во время вызова
        return NS(returncode=0, stdout="C:\\Users\\me\\Desktop\\Узнать товар.lnk\r\n", stderr="")
    lines = []
    assert make_shortcut.main([], platform="win32", run=fake_run, root=tmp_path, out=lines.append) == 0
    assert seen["argv"][:5] == ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    assert seen["bytes"].startswith(b"\xef\xbb\xbf") and "Узнать товар".encode() in seen["bytes"]
    assert "создан на рабочем столе" in lines[0] and "Узнать товар.lnk" in lines[0]
    assert not Path(seen["argv"][-1]).exists()                                      # временный файл удалён

    bad = lambda argv, **kw: NS(returncode=1, stdout="", stderr="Access denied")
    lines.clear()
    assert make_shortcut.main([], platform="win32", run=bad, root=tmp_path, out=lines.append) == 1 and "Access denied" in lines[0]


# ----------------------------------------------------------------------- start_demo.bat

def test_bat_launcher_is_crlf_uses_the_venv_python_and_keeps_the_window_open():
    raw = (ROOT / "start_demo.bat").read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b"")                                  # только CRLF — cmd.exe капризен к LF
    text = raw.decode("utf-8")
    assert '%~dp0' in text and ".venv\\Scripts\\python.exe" in text and "-m scripts.start_demo %*" in text
    assert "pause" in text and "chcp 65001" in text and text.startswith("@echo off")


def test_bat_runs_the_module_that_exists_and_icon_for_the_shortcut_exists():
    assert (ROOT / "scripts" / "start_demo.py").is_file() and (ROOT / "scripts" / "make_shortcut.py").is_file()
    assert (ROOT / "frontend" / "icons" / "app.ico").is_file()


def test_https_is_used_automatically_when_the_store_certificate_exists(tmp_path):
    """Документ 18, §14: пароли по Wi-Fi без защиты не передаются — при наличии сертификата магазина API запускается по HTTPS."""
    from scripts.start_demo import tls_files
    assert tls_files(tmp_path) is None
    api = service_commands("PY", 8000, "linux")[0]
    assert "--ssl-certfile" not in api.argv
    (tmp_path / "secrets" / "tls").mkdir(parents=True)
    (tmp_path / "secrets" / "tls" / "server.crt").write_text("c")
    assert tls_files(tmp_path) is None                                     # без ключа — нет
    (tmp_path / "secrets" / "tls" / "server.key").write_text("k")
    cert, key = tls_files(tmp_path)
    api = service_commands("PY", 8000, "linux", (cert, key))[0]
    assert api.argv[-4:] == ["--ssl-certfile", cert, "--ssl-keyfile", key] and "0.0.0.0" in api.argv


def test_local_vision_model_server_is_switched_on_when_lm_studio_is_installed(tmp_path):
    settings = NS(database_url="postgresql://u:p@dbhost:5432/x", queue_broker_url="redis://redishost:6379/0",
                  naming_enabled=True, naming_vision_url="http://127.0.0.1:1234/v1")
    code, env = go(tmp_path, settings=settings, find_lms=lambda which: "C:/lms.exe")
    assert code == 0 and ["C:/lms.exe", "server", "start"] in [argv for argv, _ in env.docker_calls]
    assert "сервер включён" in env.text


def test_no_lm_studio_or_a_remote_model_is_not_touched(tmp_path):
    settings = NS(database_url="postgresql://u:p@dbhost:5432/x", queue_broker_url="redis://redishost:6379/0",
                  naming_enabled=True, naming_vision_url="http://127.0.0.1:1234/v1")
    code, env = go(tmp_path, settings=settings, find_lms=lambda which: None)
    assert code == 0 and "только по штрихкоду" in env.text
    remote = NS(**{**vars(settings), "naming_vision_url": "https://example.org/v1"})
    code, env = go(tmp_path, settings=remote, find_lms=lambda which: "C:/lms.exe")
    assert not [a for a, _ in env.docker_calls if a[0] == "C:/lms.exe"]


# ----------------------------------------------------------------------- облегчённый режим (документ 11, §10)

def lite(tmp_path, opts=None, **over):
    return go(tmp_path, opts or Options(no_browser=True, lite=True), **over)


def test_lite_mode_runs_one_process_without_docker_on_sqlite_in_data_lite(tmp_path):
    code, env = lite(tmp_path)
    assert code == 0
    assert all(argv[0] != "docker" for argv, _ in env.docker_calls)
    bootstrap = [argv for argv, _ in env.docker_calls if "scripts.bootstrap" in argv]
    assert bootstrap == [["PY", "-m", "scripts.bootstrap", "--non-interactive", "--wait-timeout", "5"]]
    (name, argv, kw), = env.started
    assert "uvicorn" in argv and "celery" not in " ".join(argv)
    child = kw["env"]
    assert child["LITE_MODE"] == "1" and child["QUEUE_BROKER_URL"] == "memory://" and child["FRAMES_REDIS_URL"] == ""
    assert child["DATABASE_URL"] == "sqlite:///" + (tmp_path / "data" / "lite" / "shop.db").as_posix()
    assert child["CATALOG_INDEX_PATH"].endswith("data/lite/index/catalog.faiss") and child["MEDIA_ROOT"].endswith("data/lite")
    assert child["ML_STATUS_FILE"].endswith("lite_status.json")                     # не путается с полным режимом
    assert (tmp_path / "logs" / "lite.log").exists() and not (tmp_path / "logs" / "api.log").exists()
    assert "ВСЁ ЗАПУЩЕНО" in env.text and env.applied["LITE_MODE"] == "1"


def test_lite_mode_without_gpu_runs_on_cpu_and_turns_the_vision_model_off(tmp_path):
    weak = hardware.Hardware(ram_gb=8, cores=4)
    code, env = lite(tmp_path, probe_hw=lambda: weak)
    child = env.started[0][2]["env"]
    assert code == 0 and child["ML_DEVICE"] == "cpu" and child["NAMING_VISION_URL"] == ""
    assert "процессор" in env.text and "зрение + текст" in env.text and "выключена" in env.text


def test_lite_mode_with_a_good_gpu_keeps_the_vision_model_and_force_cpu_turns_it_off(tmp_path):
    code, env = lite(tmp_path)
    assert code == 0 and "NAMING_VISION_URL" not in env.started[0][2]["env"]
    assert env.started[0][2]["env"]["ML_DEVICE"] == "auto"
    code, env = lite(tmp_path, Options(no_browser=True, lite=True, cpu=True))
    assert env.started[0][2]["env"]["ML_DEVICE"] == "cpu" and env.started[0][2]["env"]["NAMING_VISION_URL"] == ""


def test_lite_mode_stops_with_the_bootstrap_message_when_the_database_cannot_be_prepared(tmp_path):
    env = Env(tmp_path)
    env.docker_rc = 1
    assert run(Options(no_browser=True, lite=True), env.deps) == 1
    assert env.started == [] and "data/lite/shop.db" in env.text


def test_lite_environment_uses_offline_model_cache_only_when_it_exists(tmp_path):
    plan = hardware.LitePlan(device="cpu", vision=True)
    assert "HF_HUB_OFFLINE" not in sd.lite_environment(tmp_path, plan, tmp_path / "missing")
    (tmp_path / "cache").mkdir()
    assert sd.lite_environment(tmp_path, plan, tmp_path / "cache")["HF_HUB_OFFLINE"] == "1"
    assert "NAMING_VISION_URL" not in sd.lite_environment(tmp_path, plan)


def test_full_mode_on_a_small_ram_computer_suggests_the_lite_mode(tmp_path):
    code, env = go(tmp_path, probe_hw=lambda: hardware.Hardware(ram_gb=8, cores=4))
    assert code == 0 and "start_lite.bat" in env.text and [n for n, _, _ in env.started] == ["api", "worker"]


def test_cli_flags_for_lite_mode(tmp_path):
    seen = []
    sd.run = lambda opts, deps=None: seen.append(opts) or 0        # noqa: E731 — подмена только в этом тесте
    try:
        sd.main(["--lite", "--vision", "off"])
        sd.main(["--cpu"])
    finally:
        import importlib
        importlib.reload(sd)
    assert seen[0].lite and seen[0].vision == "off" and not seen[0].cpu
    assert seen[1].lite and seen[1].cpu                                    # --cpu сам по себе = облегчённый режим на процессоре


def test_start_lite_bat_runs_the_lite_mode():
    text = (ROOT / "start_lite.bat").read_bytes().decode("utf-8")
    assert "-m scripts.start_demo --lite" in text and text.count(chr(13) + chr(10)) >= 5 and "chcp 65001" in text
