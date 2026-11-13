"""
docker-compose.yml / Dockerfile / .env.example / .dockerignore — инварианты «чистая машина -> docker compose up -> работает».

Проверяется только конфигурация (разбор YAML), а не сам Docker: в среде разработки его не было. Тест страхует от тихих
поломок при правке файлов — например, от возврата БД на bind-mount или от пароля админа в окружении api/worker.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
APP_SERVICES = ("api", "worker", "bootstrap")


def _env(service: str) -> dict:
    env = SERVICES[service].get("environment") or {}
    return env if isinstance(env, dict) else dict(item.split("=", 1) for item in env)


def _depends(service: str) -> dict:
    deps = SERVICES[service].get("depends_on") or {}
    return deps if isinstance(deps, dict) else {name: {} for name in deps}


# ------------------------------------------------------------------ состав и сборка

def test_expected_services_and_stable_project_name():
    assert set(SERVICES) == {"db", "redis", "bootstrap", "api", "admin-api", "worker"}
    assert COMPOSE["name"] == "retail-shelf-audit"        # имена томов не зависят от названия папки
    assert "version" not in COMPOSE                       # устарело: Compose v2 печатает предупреждение


def test_image_is_built_once_and_shared():
    builders = [n for n, s in SERVICES.items() if "build" in s]
    assert builders == ["api"]                            # иначе --no-cache собирал бы torch трижды
    images = {SERVICES[n]["image"] for n in APP_SERVICES}
    assert len(images) == 1
    for name in ("worker", "bootstrap"):
        assert SERVICES[name]["pull_policy"] == "never"   # не искать образ на Docker Hub


# ------------------------------------------------------------------ порядок запуска и здоровье

def test_bootstrap_runs_after_db_and_redis_are_healthy_and_never_restarts():
    deps = _depends("bootstrap")
    assert deps["db"]["condition"] == "service_healthy" and deps["redis"]["condition"] == "service_healthy"
    assert str(SERVICES["bootstrap"]["restart"]) == "no"
    assert SERVICES["bootstrap"]["command"][-1] == "scripts.bootstrap"


def test_api_and_worker_wait_for_bootstrap_to_finish_successfully():
    for name in ("api", "worker"):
        deps = _depends(name)
        assert deps["bootstrap"]["condition"] == "service_completed_successfully", name
        assert deps["db"]["condition"] == "service_healthy" and deps["redis"]["condition"] == "service_healthy", name


def test_every_long_running_service_has_a_healthcheck():
    for name in ("db", "redis", "api", "worker"):
        hc = SERVICES[name]["healthcheck"]
        assert hc["test"] and hc["retries"] >= 3, name
    assert "pg_isready" in " ".join(SERVICES["db"]["healthcheck"]["test"])
    assert "/health/ready" in " ".join(SERVICES["api"]["healthcheck"]["test"])
    assert "inspect ping" in " ".join(SERVICES["worker"]["healthcheck"]["test"])


def test_long_running_services_restart_after_reboot():
    for name in ("db", "redis", "api", "worker"):
        assert SERVICES[name]["restart"] == "unless-stopped", name


# ------------------------------------------------------------------ данные

def test_postgres_data_lives_in_a_named_volume_not_a_bind_mount():
    assert "pgdata:/var/lib/postgresql/data" in SERVICES["db"]["volumes"]
    assert "pgdata" in COMPOSE["volumes"]
    assert not any(str(v).startswith((".", "/", "~")) for v in SERVICES["db"]["volumes"])   # на Windows это ломает права Postgres


def test_redis_queue_and_model_cache_are_persistent():
    assert "redisdata:/data" in SERVICES["redis"]["volumes"] and "--appendonly" in SERVICES["redis"]["command"]
    for name in APP_SERVICES:
        assert "hf_cache:/root/.cache/huggingface" in SERVICES[name]["volumes"], name
    assert {"redisdata", "hf_cache"} <= set(COMPOSE["volumes"])


def test_app_services_share_the_project_folder_so_snapshots_and_index_are_common():
    for name in APP_SERVICES:
        assert ".:/srv/app" in SERVICES[name]["volumes"], name
    assert re.search(r"^WORKDIR /srv/app$", (ROOT / "Dockerfile").read_text(encoding="utf-8"), re.M)


# ------------------------------------------------------------------ подключения и секреты

def test_api_and_worker_use_the_same_database_and_broker_inside_the_compose_network():
    for name in APP_SERVICES:
        env = _env(name)
        assert env["DATABASE_URL"].startswith("postgresql+psycopg://"), name
        assert "@db:5432/" in env["DATABASE_URL"] and "localhost" not in env["DATABASE_URL"], name
        assert env["QUEUE_BROKER_URL"] == "redis://redis:6379/0", name
    assert _env("api")["DATABASE_URL"] == _env("worker")["DATABASE_URL"] == _env("bootstrap")["DATABASE_URL"]


def test_database_credentials_in_url_come_from_the_same_variables_as_the_server():
    url = _env("api")["DATABASE_URL"]
    for var in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        assert "${" + var + ":-" in url, var
        assert "${" + var + ":-" in SERVICES["db"]["environment"][var], var


def test_admin_password_is_visible_only_to_bootstrap():
    assert "ADMIN_PASSWORD" in _env("bootstrap") and "ADMIN_USERNAME" in _env("bootstrap")
    for name in ("api", "worker", "db", "redis"):
        assert "ADMIN_PASSWORD" not in _env(name), name


def test_database_and_redis_are_not_published_to_the_network_but_api_is():
    for name in ("db", "redis"):
        assert all(str(p).startswith("127.0.0.1:") for p in SERVICES[name]["ports"]), name
    assert not any(str(p).startswith("127.0.0.1") for p in SERVICES["api"]["ports"])    # телефон должен достучаться по Wi-Fi


def test_worker_runs_single_process_until_catalog_index_is_shared():
    assert "--concurrency=1" in SERVICES["worker"]["command"]


def test_debug_embedder_is_off_by_default():
    assert _env("worker")["ALLOW_DEBUG_EMBEDDER"].endswith(":-0}")


# ------------------------------------------------------------------ файлы вокруг

def test_env_example_does_not_ship_a_default_admin_password():
    values = dict(line.split("=", 1) for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
                  if re.match(r"^[A-Z_]+=", line))
    assert values["ADMIN_PASSWORD"].strip() == ""


def test_every_variable_used_in_compose_is_documented_in_env_example():
    lines = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
    text = "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in lines)      # комментарии YAML не считаются использованием
    used = set(re.findall(r"\$\{([A-Z_]+)(?::-[^}]*)?\}", text))
    documented = set(re.findall(r"^#?([A-Z_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M))
    assert used <= documented, used - documented


def test_dockerignore_keeps_secrets_data_and_virtualenvs_out_of_the_build_context():
    lines = {l.strip() for l in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    assert {".env", "data", ".venv", "models", ".git"} <= lines


def test_dockerfile_installs_system_ocr_and_checks_dependencies_after_copying_code():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for pkg in ("tesseract-ocr", "tesseract-ocr-rus", "tesseract-ocr-uzb", "tesseract-ocr-uzb-cyrl", "libgl1"):
        assert pkg in text, pkg
    assert text.index("COPY . .") < text.index("scripts.check_imports --runtime")
    assert text.index("pip install") < text.index("COPY . .")           # слой с зависимостями кэшируется отдельно от кода


def test_gitattributes_keeps_linux_files_lf():
    text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for pattern in ("Dockerfile", "*.yml", "*.py"):
        assert re.search(rf"^{re.escape(pattern)}\s+text eol=lf", text, re.M), pattern


# ------------------------------------------------------------------ камеры и агент убраны вместе с аудитом полок

def test_camera_agent_and_camera_key_are_gone_from_compose():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "agent" not in SERVICES and "camera_agent.py" not in text and "CAMERA_AGENT_PASSWORD" not in text
    for name in SERVICES:
        assert "CAMERA_SECRET_KEY" not in _env(name), name
    assert not (ROOT / "configs" / "agent.example.yaml").exists() and not (ROOT / "configs" / "cameras.example.yaml").exists()


def test_timezone_is_configured_for_every_app_service():
    for name in ("api", "worker", "bootstrap"):
        assert _env(name)["APP_TIMEZONE"] == "${APP_TIMEZONE:-Asia/Tashkent}", name



# ------------------------------------------------------------------ административный вход (решение В-1, документ 18)

def test_admin_listener_is_published_only_on_localhost_and_the_employee_listener_has_no_admin_rights():
    assert _env("api")["APP_LISTENER"] == "public"                   # порт 8000 (Wi-Fi магазина): права управления не действуют
    assert _env("admin-api")["APP_LISTENER"] == "admin"
    ports = [str(p) for p in SERVICES["admin-api"]["ports"]]
    assert ports and all(p.startswith("127.0.0.1:") for p in ports), ports        # не 0.0.0.0: ни сеть магазина, ни интернет
    assert "8001" in " ".join(SERVICES["admin-api"]["command"])
    assert SERVICES["admin-api"]["image"] == SERVICES["api"]["image"] and _env("admin-api")["DATABASE_URL"] == _env("api")["DATABASE_URL"]
    assert _env("admin-api")["AUTH_SECRET_KEY"] == _env("api")["AUTH_SECRET_KEY"]   # один ключ: второй фактор одинаков на обоих входах
    assert "ADMIN_PASSWORD" not in _env("admin-api")


def test_vpn_override_binds_only_to_an_explicit_vpn_address_and_refuses_without_it():
    vpn = yaml.safe_load((ROOT / "docker-compose.vpn.yml").read_text(encoding="utf-8"))
    assert set(vpn["services"]) == {"admin-api-vpn"}                  # отдельная служба: сбой VPN не роняет вход на самом компьютере
    service = vpn["services"]["admin-api-vpn"]
    (port,) = service["ports"]
    assert port.startswith("${VPN_BIND_IP:?")                         # без адреса VPN — ошибка запуска, а не порт для всех
    assert "0.0.0.0" not in port
    env = service["environment"]
    assert env["APP_LISTENER"] == "admin" and env["VPN_BIND_IP"] == "${VPN_BIND_IP:-}"
    base = _env("admin-api")
    for key, value in base.items():                                   # та же база, тот же ключ, тот же вход: расхождения быть не должно
        assert env.get(key) == value, key
    assert service["image"] == SERVICES["admin-api"]["image"] and service["command"] == SERVICES["admin-api"]["command"]
    assert service["volumes"] == SERVICES["admin-api"]["volumes"] and service["healthcheck"] == SERVICES["admin-api"]["healthcheck"]
    assert set(service["depends_on"]) == set(SERVICES["admin-api"]["depends_on"])


def test_admin_listener_knows_its_allowed_names_and_has_no_cors():
    env = _env("admin-api")
    assert env["VPN_BIND_IP"] == "${VPN_BIND_IP:-}" and env["ADMIN_ALLOWED_HOSTS"] == "${ADMIN_ALLOWED_HOSTS:-}"
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "if normalize_listener(settings.app_listener) == LISTENER_ADMIN:\n" in main and "_cors_origins = []" in main


def test_https_override_turns_the_employee_port_into_tls_and_leaves_the_admin_listener_alone():
    https = yaml.safe_load((ROOT / "docker-compose.https.yml").read_text(encoding="utf-8"))
    assert set(https["services"]) == {"api"}
    api = https["services"]["api"]
    assert set(api) == {"command", "healthcheck"}                       # порт, переменные и тома — из основного файла
    cmd = api["command"]
    assert cmd[cmd.index("--ssl-certfile") + 1] == "/srv/app/secrets/tls/server.crt"
    assert cmd[cmd.index("--ssl-keyfile") + 1] == "/srv/app/secrets/tls/server.key"
    assert cmd[cmd.index("--port") + 1] == "8000"
    assert "https://127.0.0.1:8000/health/ready" in " ".join(api["healthcheck"]["test"])
    assert "secrets/" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_backups_do_not_take_the_tls_private_keys():
    text = (ROOT / "scripts" / "full_backup.py").read_text(encoding="utf-8")
    assert "secrets/tls" not in text and '"secrets"' not in text
