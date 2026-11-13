"""Коды ошибок API (app/core/errors.py): каждый код, который может прийти от сервера, страница умеет показать на всех четырёх
языковых вариантах (ключ server.<код> в frontend/i18n.*.js), а маршрут действительно отдаёт код и параметры.

Коды собираются из исходного кода (ApiError(…, "код", …), code="код" у исключений сервисов, unauthorized(…, code="код"),
коды звена предела тела запроса) — новый код без перевода роняет тест.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tools.uz_review import parse_dictionary

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"
DICT_FILES = {"ru": "i18n.ru.js", "uz": "i18n.uz.js", "uz-Cyrl": "i18n.uz-cyrl.js", "en": "i18n.en.js"}

_PATTERNS = (
    re.compile(r'ApiError\(\s*[\w.]+,\s*"([a-z_]+)"'),                                  # ApiError(404, "код", …)
    re.compile(r'\bcode="([a-z_]+)"'),                                                 # PhotoError("…", code="код")
    re.compile(r'\bcode: str = "([a-z_]+)"'),                                          # код по умолчанию (unauthorized)
    re.compile(r'(?:Error|Violation)\(\s*f?"(?:[^"\\]|\\.)*",\s*"([a-z_]+)"', re.S),     # StoreInputError("текст", "код", …)
    re.compile(r'_respond\(send,\s*\d+,\s*"([a-z_]+)"'),                                # предел тела запроса
    re.compile(r'DeleteBlocker\(\s*"([a-z_]+)"'),                                        # причины запрета удаления
)

# Запасные коды: ставятся, только когда у исключения нет своего кода; их НАМЕРЕННО нет в словарях — страница покажет
# конкретный русский текст сервера, а не общую фразу.
FALLBACK_CODES = {"catalog_error", "invalid_input", "store_input_invalid", "policy_violation"}


def _codes_in_sources() -> set[str]:
    from app.core import app_errors

    codes: set[str] = set(app_errors.GENERIC_CODES.values()) | {app_errors.INTERNAL_ERROR_CODE, "client_error", "task_failed"}
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in _PATTERNS:
            codes.update(c for c in pattern.findall(text) if c)
        codes.update(re.findall(r'default_code = "([a-z_]+)"', text))
    return codes - FALLBACK_CODES


def _field_keys() -> set[str]:
    text = (ROOT / "app/services/catalog_upload.py").read_text(encoding="utf-8")
    block = re.search(r"_FIELD_CODES = \{([^}]*)\}", text).group(1)
    return {f"server.field_{code}" for code in re.findall(r':\s*"([a-z_]+)"', block)}


def _dicts() -> dict[str, dict]:
    return {lang: parse_dictionary((FRONT / name).read_text(encoding="utf-8")) for lang, name in DICT_FILES.items()}


def test_the_scan_finds_the_known_codes():
    codes = _codes_in_sources()
    for expected in ("product_not_found", "photo_numbered", "photo_too_small", "login_failed", "request_too_large", "forbidden",
                     "token_invalid", "queue_unavailable", "identify_timeout", "price_not_number"):
        assert expected in codes, expected
    assert len(codes) >= 30


@pytest.mark.parametrize("lang", list(DICT_FILES))
def test_every_server_code_has_a_message_in_every_language(lang):
    dictionary = _dicts()[lang]
    missing = sorted(k for k in {f"server.{c}" for c in _codes_in_sources()} | _field_keys() if k not in dictionary)
    assert missing == [], f"{lang}: нет перевода для {missing}"


def test_message_placeholders_match_the_params_the_server_sends():
    """{n}, {mb}, {reason}… в переводе — ровно те, что сервер кладёт в params (проверка по русскому словарю и исходному коду)."""
    ru = _dicts()["ru"]
    expected = {"photo_too_small": {"width", "height"}, "photos_per_product_limit": {"max", "existing"}, "frame_too_large": {"mb"},
                "photo_too_large": {"n", "mb"}, "photo_numbered": {"n", "reason"}, "photos_per_request_limit": {"max"},
                "field_required": {"field"}, "field_too_long": {"field", "max"}, "product_not_found": set(), "login_failed": set()}
    for code, params in expected.items():
        assert set(re.findall(r"\{(\w+)\}", ru[f"server.{code}"])) == params, code


def test_error_body_keeps_the_russian_detail_for_old_pages_and_adds_code_and_params():
    from app.core.app_errors import error_payload

    assert error_payload("Товар не найден.", "product_not_found") == {"detail": "Товар не найден.", "code": "product_not_found", "params": {}}
    main_src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert "install_error_handlers(app, cors_origins=_cors_origins)" in main_src             # подробно обработчики проверяет tests/test_error_handlers.py


def test_service_exceptions_carry_codes_and_stay_backward_compatible():
    from app.services.catalog_upload import CatalogAddError, InputError, PhotoError, clean_text, normalize_photo, parse_price

    with pytest.raises(PhotoError) as photo:
        normalize_photo(b"")
    assert photo.value.code == "photo_empty" and str(photo.value) == "Файл пустой — выберите фото ещё раз."
    with pytest.raises(InputError) as price:
        parse_price("дорого")
    assert price.value.code == "price_not_number"
    with pytest.raises(InputError) as field:
        clean_text("", label="название товара", max_len=10, required=True)
    assert field.value.code == "field_required" and field.value.params == {"field": {"key": "server.field_name", "params": {}}}
    plain = CatalogAddError("старый вызов без кода")
    assert plain.code is None and plain.params == {} and str(plain) == "старый вызов без кода"


# ------------------------------------------------------------------ магазины, пользователи, политика паролей, воркер

def test_store_errors_carry_codes_and_params():
    from app.services import store_service as ss

    cases = [(lambda: ss.validate_store_fields("", None, None), "store_name_required", {}),
             (lambda: ss.validate_store_fields("x" * 500, None, None), "store_name_too_long", {"max": ss.MAX_NAME_LENGTH}),
             (lambda: ss.validate_store_fields("Магазин", None, "космодром"), "store_format_invalid", {"formats": ", ".join(ss.STORE_FORMATS)})]
    for call, code, params in cases:
        with pytest.raises(ss.StoreInputError) as err:
            call()
        assert err.value.code == code and err.value.params == params and isinstance(err.value, ValueError)
        assert str(err.value)                                   # русский текст для прежних клиентов на месте


def test_password_and_login_policy_codes():
    from app.core import security as sec

    with pytest.raises(sec.PolicyViolation) as short:
        sec.check_password_policy("a")
    assert short.value.code == "password_too_short" and short.value.params == {"min": sec.MIN_PASSWORD_LENGTH}
    with pytest.raises(sec.PolicyViolation) as login:
        sec.validate_username(" !")
    assert login.value.code == "username_invalid" and isinstance(login.value, ValueError)


def test_ml_not_ready_shows_component_names_but_never_the_reasons():
    from app.services.catalog_upload import NotReadyError, not_ready_error

    problems = ["embedder: OSError: [Errno 2] No such file or directory: '/root/.cache/torch/hub/checkpoints/dinov2.pth'",
                "matcher: IndexIntegrityError: Индекс /srv/data/catalog.faiss не читается (password=hunter2)", "embedder: повтор"]
    err = not_ready_error(problems)
    assert isinstance(err, NotReadyError) and err.code == "ml_not_ready" and err.kind == "unavailable"
    assert err.params == {"components": "embedder, matcher"}
    for leak in ("/root", "/srv", "OSError", "Errno", "hunter2", "faiss", "Index"):
        assert leak not in str(err), leak
    assert not_ready_error(["компоненты распознавания не инициализированы"]).params == {}
    worker_src = (ROOT / "app/worker.py").read_text(encoding="utf-8")
    assert "return not_ready_error(exc.problems)" in worker_src and 'logger.error("Распознавание не готово' in worker_src


# ------------------------------------------------------------------ маршруты: один механизм, без утечек

def test_routes_use_only_api_error_and_never_echo_exception_text():
    for path in sorted((ROOT / "app/api").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "HTTPException" not in text, f"{path.name}: ошибки маршрутов — только через ApiError (app/core/errors.py)"
        assert not re.search(r"detail\s*=\s*str\(", text), f"{path.name}: текст исключения не уходит клиенту как есть"
        assert "traceback" not in text and "format_exc" not in text, path.name


def test_auth_dependencies_answer_with_codes():
    deps = (ROOT / "app/api/deps.py").read_text(encoding="utf-8")
    assert 'code: str = "token_invalid"' in deps and 'headers={"WWW-Authenticate": "Bearer"}' in deps
    for code in ("forbidden", "store_forbidden"):
        assert f'"{code}"' in deps, code


def test_delete_blockers_keep_russian_text_and_carry_codes_for_translation():
    from app.services import store_service as ss

    reasons = ss.store_delete_blockers(shelves=3, cameras=0, snapshots=7, users=1)
    assert [r.code for r in reasons] == ["blocker_store_shelves", "blocker_store_snapshots", "blocker_store_users"]
    assert [r.params for r in reasons] == [{"n": 3}, {"n": 7}, {"n": 1}]
    assert reasons[0] == "в магазине есть полки (3) — удалите их сначала" and isinstance(reasons[0], str)   # прежний текст
    shelf = ss.shelf_delete_blockers(snapshots=0, cameras=2, planogram_slots=12, with_planogram=False)
    assert [(r.code, r.params) for r in shelf] == [("blocker_shelf_cameras", {"n": 2}), ("blocker_shelf_planogram", {"n": 12})]
    ru = _dicts()["ru"]
    for r in reasons + shelf:                                     # перевод с тем же числом совпадает с прежним текстом по смыслу
        assert set(re.findall(r"\{(\w+)\}", ru[f"server.{r.code}"])) == set(r.params), r.code
    routes = (ROOT / "app/api/routes_stores.py").read_text(encoding="utf-8")
    assert 'reason(getattr(r, "code", None), getattr(r, "params", None))' in routes and '{"reasons": items}' in routes


def test_exception_class_names_go_only_to_the_log_in_api_and_health_checks():
    """Класс исключения — внутренняя подробность: в маршрутах и в /health/ready он только пишется в журнал, клиенту — «fail»."""
    for path in [*sorted((ROOT / "app/api").glob("*.py")), ROOT / "app/core/readiness.py"]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "type(exc).__name__" in line:
                assert "logger." in line or line.strip().startswith(("#", '"', "'")), f"{path.name}:{number}: {line.strip()}"
