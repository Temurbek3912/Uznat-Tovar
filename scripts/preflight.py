"""
Предстартовая проверка окружения: за пару секунд говорит, чего не хватает для запуска, ДО того как вы
запустите API и воркер и получите непонятный стек-трейс.

    python -m scripts.preflight

Коды возврата: 0 — блокирующих проблем нет (предупреждения возможны), 1 — есть хотя бы одна FAIL.

Что проверяет: версия Python; установлены ли пакеты; настройки; PyTorch и видеокарта (в т.ч. поддержка
Blackwell/RTX 50xx); Tesseract и языки; веса детектора и эмбеддера; доступность Postgres и Redis;
таблицы, пользователь-администратор, магазины и каталог; запись в папку снимков; место на диске.
Ничего не изменяет в БД и не создаёт таблиц.

Каждая проверка — отдельная функция, а внешний мир (импорты, сокеты, БД, диск) подаётся через `Probes`,
поэтому логика тестируется без реальных сервисов (tests/test_preflight.py).
"""
from __future__ import annotations

import importlib
import os
import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"

# (имя для import, имя для pip, обязателен)
PACKAGES: list[tuple[str, str, bool]] = [
    ("fastapi", "fastapi", True), ("uvicorn", "uvicorn[standard]", True), ("pydantic", "pydantic", True),
    ("pydantic_settings", "pydantic-settings", True), ("sqlalchemy", "sqlalchemy", True),
    ("psycopg2", "psycopg2-binary", True), ("celery", "celery", True), ("redis", "redis", True),
    ("numpy", "numpy", True), ("cv2", "opencv-python-headless", True), ("PIL", "pillow", True),
    ("yaml", "pyyaml", True), ("httpx", "httpx", True), ("multipart", "python-multipart", True),
    ("openpyxl", "openpyxl", True), ("reportlab", "reportlab", True),
    ("torch", "torch", True), ("torchvision", "torchvision", True), ("transformers", "transformers", True),
    ("ultralytics", "ultralytics", True),
    ("faiss", "faiss-cpu", False), ("pytesseract", "pytesseract", False), ("prometheus_client", "prometheus-client", False),
    ("qrcode", "qrcode", False),   # только для scripts/phone_url.py (QR-код с адресом для телефона)
]
WANTED_OCR_LANGS = ("rus", "uzb", "uzb_cyrl")
EXPECTED_TABLES = {"stores", "shelves", "products", "planogram_slots", "shelf_snapshots", "detections",
                   "compliance_reports", "users", "user_store_access", "api_tokens"}
MIN_FREE_GB = 5.0
BLACKWELL_FIX = "pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128"


@dataclass(frozen=True)
class Result:
    status: str
    name: str
    detail: str = ""
    hint: str = ""


# ------------------------------------------------------------------ чистые проверки


def check_python(version_info) -> Result:
    v = f"{version_info[0]}.{version_info[1]}.{version_info[2]}"
    if tuple(version_info[:2]) < (3, 10):
        return Result(FAIL, "Python", f"{v} — нужен 3.10+ (рекомендуется 3.14)", "Установите Python 3.14 и пересоздайте .venv")
    return Result(OK, "Python", v)


def check_packages(importer: Callable[[str], str | None]) -> tuple[Result, set[str]]:
    """Возвращает (результат, множество отсутствующих import-имён). importer(name) -> версия или ImportError."""
    missing_req, missing_opt, versions = [], [], {}
    for mod, pip_name, required in PACKAGES:
        try:
            versions[mod] = importer(mod)
        except ImportError:
            (missing_req if required else missing_opt).append((mod, pip_name))
    missing_names = {m for m, _ in missing_req + missing_opt}
    if missing_req:
        pips = " ".join(p for _, p in missing_req)
        return (Result(FAIL, "Пакеты Python", "не установлены: " + ", ".join(m for m, _ in missing_req),
                       f"pip install {pips}   (или pip install -r requirements.txt)"), missing_names)
    if missing_opt:
        return (Result(WARN, "Пакеты Python", "необязательные не установлены: " + ", ".join(m for m, _ in missing_opt),
                       "pip install " + " ".join(p for _, p in missing_opt)), missing_names)
    return Result(OK, "Пакеты Python", f"все {len(PACKAGES)} на месте"), missing_names


def check_cuda(torch_mod) -> Result:
    if torch_mod is None:
        return Result(SKIP, "GPU (CUDA)", "PyTorch не установлен")
    if not torch_mod.cuda.is_available():
        return Result(WARN, "GPU (CUDA)", "видеокарта недоступна PyTorch — всё будет считаться на CPU (медленно)",
                      "Обновите драйвер NVIDIA и поставьте PyTorch с CUDA: " + BLACKWELL_FIX)
    name = torch_mod.cuda.get_device_name(0)
    major, minor = torch_mod.cuda.get_device_capability(0)
    arch_list = list(torch_mod.cuda.get_arch_list())
    sm = f"sm_{major}{minor}"
    # Blackwell (RTX 50xx) = compute capability 12.x. Сборка PyTorch без sm_120 не падает при импорте, а
    # ломается при первом CUDA-вызове («no kernel image is available») — ловим заранее.
    if major >= 12 and not any(a in arch_list for a in (sm, f"compute_{major}{minor}", "sm_120", "compute_120")):
        return Result(FAIL, "GPU (CUDA)", f"{name} ({sm}), но эта сборка PyTorch её не поддерживает: {', '.join(arch_list)}",
                      BLACKWELL_FIX)
    return Result(OK, "GPU (CUDA)", f"{name} ({sm})")


def check_tesseract(which: Callable[[str], str | None], get_languages: Callable[[], list[str]] | None) -> Result:
    if which("tesseract") is None:
        return Result(WARN, "Tesseract (OCR ценников)", "программа не найдена — сверка цен (F11) будет пропускаться",
                      "Установите Tesseract + языки rus/uzb/uzb_cyrl и добавьте в PATH (см. RUNBOOK, п. 1)")
    try:
        langs = set(get_languages()) if get_languages else set()
    except Exception as exc:     # noqa: BLE001 — pytesseract бросает разные исключения
        return Result(WARN, "Tesseract (OCR ценников)", f"не удалось получить список языков: {exc}")
    missing = [l for l in WANTED_OCR_LANGS if l not in langs]
    if missing:
        return Result(WARN, "Tesseract (OCR ценников)", "нет языковых пакетов: " + ", ".join(missing),
                      "Доустановите языки Tesseract (узбекская латиница uzb, кириллица uzb_cyrl, русский rus)")
    return Result(OK, "Tesseract (OCR ценников)", "найден, языки rus/uzb/uzb_cyrl на месте")


def hf_cache_has_model(model_id: str, env: dict[str, str] | None = None, home: Path | None = None) -> bool:
    env = os.environ if env is None else env
    home = home if home is not None else Path.home()
    cache = env.get("HF_HUB_CACHE") or str(Path(env.get("HF_HOME") or home / ".cache" / "huggingface") / "hub")
    snaps = Path(cache) / ("models--" + model_id.replace("/", "--")) / "snapshots"
    return snaps.is_dir() and any(snaps.iterdir())


def check_weights(detector_exists: bool, detector_path: str, embedder_dir_exists: bool, embedder_path: str,
                  hub_model: str, hub_cached: bool) -> list[Result]:
    out: list[Result] = []
    if detector_exists:
        out.append(Result(OK, "Веса детектора (YOLO)", detector_path))
    else:
        out.append(Result(WARN, "Веса детектора (YOLO)", f"нет файла {detector_path} — будет грубая эвристика на контурах",
                          "Обучите детектор (docs/training_guide.md) и положите best.pt по этому пути"))
    if embedder_dir_exists:
        out.append(Result(OK, "Веса эмбеддера (DINOv2)", embedder_path))
    elif hub_cached:
        out.append(Result(OK, "Веса эмбеддера (DINOv2)", f"дообученных нет, используется {hub_model} (уже скачана)"))
    else:
        out.append(Result(WARN, "Веса эмбеддера (DINOv2)",
                          f"дообученных нет; при первом запуске будет скачана {hub_model} (~350 МБ)",
                          "Нужен интернет на первом запуске. Без него воркер не стартует (ALLOW_DEBUG_EMBEDDER=1 — только для отладки)"))
    return out


def parse_host_port(url: str, default_port: int) -> tuple[str, int]:
    p = urlparse(url)
    return p.hostname or "localhost", p.port or default_port


def check_tcp(name: str, host: str, port: int, connector: Callable[[str, int, float], None], hint: str) -> Result:
    try:
        connector(host, port, 2.0)
    except OSError as exc:
        return Result(FAIL, name, f"{host}:{port} недоступен ({exc.__class__.__name__})", hint)
    return Result(OK, name, f"{host}:{port} отвечает")


def check_disk(free_bytes: int, where: str, min_gb: float = MIN_FREE_GB) -> Result:
    gb = free_bytes / 1024 ** 3
    if gb < min_gb:
        return Result(WARN, "Место на диске", f"свободно {gb:.1f} ГБ в {where} (рекомендуется от {min_gb:.0f} ГБ для снимков)")
    return Result(OK, "Место на диске", f"свободно {gb:.0f} ГБ в {where}")


@dataclass
class DbState:
    reachable: bool
    error: str = ""
    tables: set[str] = field(default_factory=set)
    admin_count: int = 0
    store_count: int = 0
    product_count: int = 0


def check_db(state: DbState) -> list[Result]:
    if not state.reachable:
        return [Result(FAIL, "База данных", f"не удалось подключиться: {state.error}",
                       "Проверьте `docker compose up -d db` и database.url в configs/config.yaml (или DATABASE_URL)")]
    out = [Result(OK, "База данных", "подключение работает")]
    missing = sorted(EXPECTED_TABLES - state.tables)
    if missing:
        out.append(Result(WARN, "Таблицы БД", "нет: " + ", ".join(missing),
                          "Создаются командой `python -m scripts.seed_catalog` или `python -m scripts.setup_store list`"))
        return out
    out.append(Result(OK, "Таблицы БД", "все на месте"))
    out.append(Result(OK if state.admin_count else WARN, "Администратор",
                      f"найдено: {state.admin_count}" if state.admin_count else "ни одного пользователя-администратора",
                      "" if state.admin_count else "python -m scripts.create_user --username admin --role admin"))
    out.append(Result(OK if state.store_count else WARN, "Магазины",
                      f"заведено: {state.store_count}" if state.store_count else "нет ни одного магазина",
                      "" if state.store_count else "python -m scripts.setup_store store ...   (RUNBOOK, п. 6.4)"))
    out.append(Result(OK if state.product_count else WARN, "Каталог товаров",
                      f"SKU: {state.product_count}" if state.product_count else "пуст — все товары будут «unknown»",
                      "" if state.product_count else "python -m scripts.add_product_from_local_file ...   (RUNBOOK, п. 6.3)"))
    return out


def check_media_root(path: Path, can_write: Callable[[Path], None]) -> Result:
    try:
        can_write(path)
    except OSError as exc:
        return Result(FAIL, "Папка снимков", f"{path}: нет записи ({exc})", "Проверьте права или задайте media_root")
    return Result(OK, "Папка снимков", f"{path} доступна для записи")


# ------------------------------------------------------------------ внешний мир


@dataclass
class Probes:
    python_version: tuple = field(default_factory=lambda: tuple(sys.version_info[:3]))
    importer: Callable[[str], str | None] = None
    which: Callable[[str], str | None] = shutil.which
    connector: Callable[[str, int, float], None] = None
    load_settings: Callable[[], object] = None
    get_ocr_languages: Callable[[], list[str]] | None = None
    path_exists: Callable[[str], bool] = None
    dir_exists: Callable[[str], bool] = None
    disk_free: Callable[[Path], int] = None
    write_probe: Callable[[Path], None] = None
    db_state: Callable[[object], DbState] = None
    hub_cached: Callable[[str], bool] = hf_cache_has_model
    torch_module: Callable[[], object | None] = None
    project_root: Path = Path(__file__).resolve().parent.parent


def _real_importer(name: str):
    mod = importlib.import_module(name)
    return getattr(mod, "__version__", None)


def _real_connector(host: str, port: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


def _real_write_probe(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path, delete=True):
        pass


def _real_db_state(settings) -> DbState:
    try:
        from sqlalchemy import create_engine, inspect, text
        from app.db.url import normalize_database_url
        engine = create_engine(normalize_database_url(settings.database_url), pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            tables = set(inspect(conn).get_table_names())
            admins = stores = products = 0
            if "users" in tables:
                admins = conn.execute(text("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = TRUE")).scalar() or 0
            if "stores" in tables:
                stores = conn.execute(text("SELECT COUNT(*) FROM stores")).scalar() or 0
            if "products" in tables:
                products = conn.execute(text("SELECT COUNT(*) FROM products")).scalar() or 0
        return DbState(True, "", tables, int(admins), int(stores), int(products))
    except Exception as exc:    # noqa: BLE001 — любое исключение драйвера = «БД недоступна»
        return DbState(False, f"{exc.__class__.__name__}: {str(exc).splitlines()[0][:160] if str(exc) else ''}")


def real_probes() -> Probes:
    def _torch():
        try:
            return importlib.import_module("torch")
        except ImportError:
            return None

    def _langs():
        import pytesseract
        return list(pytesseract.get_languages(config=""))

    def _settings():
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.core.config import get_settings
        return get_settings()

    return Probes(
        importer=_real_importer, connector=_real_connector, load_settings=_settings, get_ocr_languages=_langs,
        path_exists=lambda p: Path(p).is_file(), dir_exists=lambda p: Path(p).is_dir(),
        disk_free=lambda p: shutil.disk_usage(p if p.exists() else p.parent if p.parent.exists() else Path.cwd()).free,
        write_probe=_real_write_probe, db_state=_real_db_state, torch_module=_torch,
    )


# ------------------------------------------------------------------ сборка проверок


def run_checks(pb: Probes) -> list[Result]:
    results: list[Result] = [check_python(pb.python_version)]

    pkg_result, missing = check_packages(pb.importer)
    results.append(pkg_result)
    core_missing = {"pydantic", "pydantic_settings", "yaml"} & missing

    # Настройки нужны для всего остального; без pydantic их не загрузить.
    settings = None
    if core_missing:
        results.append(Result(SKIP, "Настройки", "пропущено: нет " + ", ".join(sorted(core_missing))))
    else:
        try:
            settings = pb.load_settings()
            results.append(Result(OK, "Настройки", f"configs/config.yaml загружен, окружение: {settings.environment}"))
        except Exception as exc:    # noqa: BLE001
            results.append(Result(FAIL, "Настройки", f"не удалось загрузить: {exc.__class__.__name__}: {exc}",
                                  "Проверьте configs/config.yaml и файл .env"))

    results.append(check_cuda(pb.torch_module() if "torch" not in missing else None))

    if "pytesseract" in missing:
        results.append(Result(WARN, "Tesseract (OCR ценников)", "пакет pytesseract не установлен — сверка цен будет пропускаться"))
    else:
        results.append(check_tesseract(pb.which, pb.get_ocr_languages))

    if settings is not None:
        from app.pipeline.embedder import DEFAULT_HUB_MODEL, resolve_embedder_source   # noqa: PLC0415
        det = settings.detector.weights_path if settings.detector else "models/detector/sku110k_yolo11n.pt"
        emb = settings.embedder.weights_path if settings.embedder else ""
        root = pb.project_root
        det_ok = pb.path_exists(det) or pb.path_exists(str(root / det))
        _src, from_hub = resolve_embedder_source(emb, roots=[Path.cwd(), root]) if emb else (DEFAULT_HUB_MODEL, True)
        results += check_weights(det_ok, det, not from_hub, emb or "(не задан)", DEFAULT_HUB_MODEL, pb.hub_cached(DEFAULT_HUB_MODEL))

        db_host, db_port = parse_host_port(settings.database_url, 5432)
        redis_host, redis_port = parse_host_port(settings.queue_broker_url, 6379)
        pg = check_tcp("Postgres", db_host, db_port, pb.connector, "docker compose up -d db")
        results.append(pg)
        results.append(check_tcp("Redis", redis_host, redis_port, pb.connector, "docker compose up -d redis"))
        results += check_db(pb.db_state(settings)) if pg.status == OK else [
            Result(SKIP, "База данных", "пропущено: Postgres недоступен")]

        media = Path(settings.media_root)
        media = media if media.is_absolute() else root / media
        results.append(check_media_root(media, pb.write_probe))
        results.append(check_disk(pb.disk_free(media), str(media)))
    else:
        results.append(Result(SKIP, "Веса, Postgres, Redis, таблицы, диск", "пропущено: настройки не загружены"))
    return results


MARKS = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[ -- ]"}


def format_results(results: list[Result]) -> str:
    lines = []
    for r in results:
        lines.append(f"{MARKS[r.status]} {r.name}: {r.detail}".rstrip(": "))
        if r.hint and r.status in (WARN, FAIL):
            lines.append(f"         -> {r.hint}")
    n = {s: sum(1 for r in results if r.status == s) for s in (OK, WARN, FAIL)}
    lines.append("")
    lines.append(f"Итого: OK {n[OK]}, предупреждений {n[WARN]}, блокирующих {n[FAIL]}.")
    lines.append("Можно запускать." if not n[FAIL] else "Сначала исправьте пункты [FAIL].")
    return "\n".join(lines)


def environment_banner(executable: str, version: str, in_docker: bool) -> list[str]:
    """Шапка отчёта: ЧЬЁ окружение проверяется. Python на хосте Windows и Python внутри контейнера — разные окружения:
    пропуски пакетов на хосте не доказывают, что их нет в Docker-образе (там они ставятся из requirements.txt)."""
    if in_docker:
        return [f"Проверяется окружение ВНУТРИ Docker-контейнера: Python {version} ({executable}).", ""]
    major_minor = tuple(int(x) for x in version.split(".")[:2])
    lines = [
        f"Проверяется Python НА ХОСТЕ: {version} ({executable}).",
        "Приложение работает в Docker-контейнерах со своим Python и своими пакетами (docker-compose.yml, Dockerfile) —",
        "отсутствие пакетов здесь НЕ означает, что их нет в контейнерах. Проверка контейнера:",
        "    docker compose exec api python -m scripts.preflight",
    ]
    if major_minor >= (3, 15):
        lines += [
            f"Python {major_minor[0]}.{major_minor[1]} на хосте не мешает Docker-запуску. Если захотите запускать на хосте агента камер",
            "(нужен для USB-камер) — для самых новых версий Python у opencv/numpy могут отсутствовать готовые сборки;",
            "безопасный выбор — отдельный Python 3.14 (py -3.14), системный при этом удалять не нужно.",
        ]
    return lines + [""]


def main(probes: Probes | None = None) -> int:
    if probes is None:        # реальный запуск; в тестах probes передаются явно и шапка не печатается
        print("\n".join(environment_banner(sys.executable, ".".join(map(str, sys.version_info[:3])),
                                            os.path.exists("/.dockerenv"))))
    results = run_checks(probes or real_probes())
    print(format_results(results))
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
