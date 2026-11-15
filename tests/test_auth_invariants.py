"""
Инварианты входа (решения владельца 2026-11-08, документ 18) — статически, по исходному коду маршрутов: нет обходных путей мимо
второго фактора, нет скрытых способов входа, права управления только на административном входе, владельца нельзя назначить через API.

Проверка статическая, потому что FastAPI в облачной среде нет; поведение проверяют tests/test_auth_sessions_api.py (в среде по
requirements.txt) и чистые модули (tests/test_mfa_policy.py).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "app" / "api"
ROUTE_FILES = sorted(API.glob("routes_*.py"))

# Маршруты, которым токен не нужен (или нужен только для отзыва): вход, второй шаг входа, выход.
PUBLIC_ROUTES = {("post", "/auth/login"), ("post", "/auth/login/2fa"), ("post", "/auth/logout"), ("get", "/auth/transport"),
                 # публичный вход покупателя (app/api/routes_public.py): без входа по замыслу — только «Узнать товар» с ограничением
                 # частоты и только публичные поля; данных сотрудников и управления там нет (tests/test_public_api.py)
                 ("post", "/public/identify"), ("get", "/public/store"),
                 # список товаров покупателя (шаг 3, 2026-11-15): только товары не из архива, только поля карточки магазина,
                 # миниатюры; своё ограничение частоты (tests/test_public_api.py)
                 ("get", "/public/products"), ("get", "/public/categories"), ("get", "/public/photo/{ref}"),
                 # вход ПИН-кодом и ссылка «Придумайте ПИН» (решение владельца 2026-11-15): только с самого компьютера программы —
                 # каждый проверяет это первой строкой (test_pin_routes_check_this_computer_first, tests/test_local_pin.py)
                 ("get", "/auth/local"), ("post", "/auth/pin"), ("post", "/auth/setup/check"), ("post", "/auth/setup")}
LOCAL_ONLY_ROUTES = ("def login_pin(", "def setup_check(", "def setup_pin(", "def set_password_by_pin(")
AUTH_DEPS = {"get_current_user", "require_permission", "require_recent_auth", "get_enrolling_user", "get_password_changing_user"}
ENROLL_ROUTES = {("get", "/auth/2fa"), ("post", "/auth/2fa/setup"), ("post", "/auth/2fa/confirm")}


def _routes(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {}                                   # _admin = require_permission(Permission.STORE_MANAGE)
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = node.value.func.id
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and isinstance(deco.func.value, ast.Name)
                    and deco.func.value.id == "router" and deco.args and isinstance(deco.args[0], ast.Constant)):
                deps = set()
                for default in node.args.defaults + node.args.kw_defaults:
                    if isinstance(default, ast.Call) and getattr(default.func, "id", None) == "Depends" and default.args:
                        inner = default.args[0]
                        name = inner.func.id if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) else getattr(inner, "id", None)
                        deps.add(aliases.get(name, name))
                yield deco.func.attr, deco.args[0].value, node.name, deps


def _prefix(path: Path) -> str:
    match = re.search(r'APIRouter\(prefix="([^"]*)"', path.read_text(encoding="utf-8"))
    return match.group(1) if match else ""


def test_every_route_requires_a_session_except_sign_in_and_sign_out():
    unprotected = []
    for path in ROUTE_FILES:
        for method, route, func, deps in _routes(path):
            full = (_prefix(path) + route).replace("/api/v1", "")
            if (method, full) in PUBLIC_ROUTES:
                continue
            if not deps & AUTH_DEPS:
                unprotected.append(f"{path.name}:{func} {method.upper()} {full}")
    assert unprotected == [], "маршрут без проверки сеанса — скрытый путь мимо входа и второго фактора"


def test_enrollment_pass_is_accepted_only_by_the_three_2fa_setup_routes():
    users = set()
    for path in ROUTE_FILES:
        for method, route, func, deps in _routes(path):
            if "get_enrolling_user" in deps:
                users.add((method, (_prefix(path) + route).replace("/api/v1", "")))
    assert users == ENROLL_ROUTES


def test_pin_routes_check_this_computer_first():
    auth = (API / "routes_auth.py").read_text(encoding="utf-8")
    for head in LOCAL_ONLY_ROUTES:
        body = auth[auth.index(head):]
        first = body.index('"""', body.index('"""') + 3) + 3
        assert body[first:].lstrip().startswith("_require_this_computer(request)"), head
    local = auth[auth.index("def local_info("):auth.index("def login_pin(")]
    assert local.index("if not _this_computer(request):") < local.index("pin_users()")


def test_own_password_change_is_the_only_other_place_that_opens_a_session():
    """Кроме входа (_issue и пропуск настройки), сеанс открывает только смена своего пароля — после текущего пароля и кода
    (если включён второй фактор), в одной транзакции с новым поколением сеансов (документ 05, §12.11)."""
    service = (ROOT / "app/services/auth_service.py").read_text(encoding="utf-8")
    assert service.count("self._add_session(") == 3      # issue_token, change_own_password и set_password_with_pin (ниже)
    body = service[service.index("    def change_own_password("):service.index("    def temporary_password_state(")]
    order = [body.index(x) for x in ("self._require_password(", "totp.verify(", "self._end_all_sessions(", "self._add_session(",
                                       "self.db.commit()")]
    assert order == sorted(order) and body.count("self.db.commit()") == 1
    assert "mfa = bool(security is not None and security.totp_secret_enc and security.totp_enabled_at)" in body
    # пароль по ПИН-коду (2026-11-15): сначала ПИН, потом завершение всех сеансов и новый сеанс — одной транзакцией;
    # маршрут — только на этом компьютере (test_pin_routes_check_this_computer_first)
    by_pin = service[service.index("    def set_password_with_pin("):service.index("    # ---------------------------------------------------------------- владелец")]
    order = [by_pin.index(x) for x in ("verify_password(pin", "check_password_policy(", "self._end_all_sessions(",
                                         "self._add_session(", "self.db.commit()")]
    assert order == sorted(order) and by_pin.count("self.db.commit()") == 1


def test_tokens_are_issued_and_checked_in_one_place_each():
    sources = {p.name: p.read_text(encoding="utf-8") for p in sorted((ROOT / "app").rglob("*.py"))}
    issuers = {name for name, text in sources.items() if ".issue_token(" in text}
    assert issuers == {"routes_auth.py"}, issuers
    checkers = {name for name, text in sources.items() if ".resolve_token(" in text}
    assert checkers == {"deps.py"}, checkers
    auth = sources["routes_auth.py"]
    # обычный сеанс выдаётся только функцией _issue; кроме неё issue_token вызывается один раз — для пропуска настройки
    calls = [m.start() for m in re.finditer(r"\.issue_token\(", auth)]
    assert len(calls) == 3                                    # пропуск «смена пароля», пропуск настройки 2FA, _issue
    scopes = sorted(re.search(r"scope=(SCOPE_\w+)", auth[c:c + 400]).group(1) for c in calls)
    assert scopes == ["SCOPE_ENROLL", "SCOPE_FULL", "SCOPE_PASSWORD"], scopes
    for c in calls:
        chunk = auth[c:c + 400]
        if "scope=SCOPE_ENROLL" in chunk:
            assert "ENROLL_TTL_MINUTES" in chunk
        if "scope=SCOPE_PASSWORD" in chunk:
            assert "temp_password.PASS_TTL_MINUTES" in chunk
    # сеанс без второго фактора (mfa=False) выдаётся только после решения политики (ISSUE): во входе и после установки пароля
    assert auth.count("mfa=False") == 4                       # _issue во входе и после установки пароля, два пропуска
    login_body = auth[auth.index("def login("):auth.index("def login_second_step(")]
    assert login_body.index("after_password(") < login_body.index("return _issue(service, user, role, request, mfa=False)")
    # временный пароль проверяется ДО второго фактора и до выдачи чего-либо ещё
    assert login_body.index("temporary_password_state(") < login_body.index("after_password(")
    initial = auth[auth.index("def set_initial_password("):auth.index('@router.post("/auth/logout")')]
    assert initial.index("after_password(") < initial.index("if decision != ISSUE") < initial.index("return _issue(")


def test_admin_permissions_are_bound_to_the_admin_listener_in_the_dependency():
    deps = (API / "deps.py").read_text(encoding="utf-8")
    assert "ADMIN_PERMISSIONS: frozenset[Permission] = ADMIN_ONLY_PERMISSIONS" in deps
    assert "if permission in ADMIN_PERMISSIONS and not admin_rights_apply(request_listener(request), user.session_listener):" in deps
    for path in ROUTE_FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\.role\s*==\s*Role\.ADMIN\s*(?:and|or|\))?\s*:\s*$", text, re.M) or path.name == "routes_auth.py", path.name


def test_owner_flag_cannot_be_set_through_the_api():
    auth = (API / "routes_auth.py").read_text(encoding="utf-8")
    for model in ("UserCreate", "UserUpdate"):
        body = auth[auth.index(f"class {model}("):]
        body = body[:body.index("\nclass ", 1)] if "\nclass " in body[1:] else body
        assert "owner" not in body.lower(), model
    for path in ROUTE_FILES:
        assert ".set_owner(" not in path.read_text(encoding="utf-8"), path.name
    services = (ROOT / "app/services/auth_service.py").read_text(encoding="utf-8")
    assert services.count("is_owner = True") == 1                 # только set_owner (консоль и bootstrap)


def test_sensitive_user_operations_require_recent_reauthentication():
    auth = (API / "routes_auth.py").read_text(encoding="utf-8")
    for func in ("def create_user(", "def update_user(", "def reset_user_2fa("):
        block = auth[auth.index(func):auth.index(func) + 400]
        assert "require_recent_auth(Permission.USER_MANAGE)" in block, func
    stores = (API / "routes_stores.py").read_text(encoding="utf-8")
    for func in ("def delete_shelf(", "def delete_store("):
        block = stores[stores.index(func):stores.index(func) + 400]
        assert "require_recent_auth(Permission.STORE_MANAGE)" in block, func


def test_session_acceptance_rule_is_applied_for_every_resolved_token():
    service = (ROOT / "app/services/auth_service.py").read_text(encoding="utf-8")
    body = service[service.index("    def resolve_token("):service.index("    def refresh_session(")]
    assert "session_accepted(" in body and "scope not in allow_scopes" in body and "epoch_matches(" in body
    deps = (API / "deps.py").read_text(encoding="utf-8")
    assert deps.count("require_admin_mfa=get_settings().auth_require_2fa_for_admins") == 3
    assert "if scope == SCOPE_FULL and security is not None and security.must_change_password:" in body


def test_listener_of_a_request_is_decided_in_one_place_from_the_socket_and_host():
    """Вход программы для запроса — только deps.request_listener (адрес сокета сервера, Host, заголовки прокси). Маршруты входа
    не читают настройку APP_LISTENER сами: иначе запуск на 0.0.0.0 с настройкой «all» снова открыл бы управление сети магазина."""
    deps = (API / "deps.py").read_text(encoding="utf-8")
    body = deps[deps.index("def request_listener("):deps.index("def get_current_user(")]
    assert "listener_for_request(settings.app_listener, request.scope, request.headers" in body
    from app.core import mfa_policy
    import inspect
    pure = inspect.getsource(mfa_policy.listener_for_request)
    assert 'scope.get("server")' in pure and 'headers.get("host")' in pure and "has_forwarding_headers(headers)" in pure
    for path in ROUTE_FILES:
        assert "app_listener" not in path.read_text(encoding="utf-8"), path.name
    auth = (API / "routes_auth.py").read_text(encoding="utf-8")
    assert "admin_rights_apply(request_listener(request), user.session_listener)" in auth          # /auth/me говорит правду
    for func in ("def mfa_setup(", "def mfa_confirm("):
        block = auth[auth.index(func):auth.index(func) + 900]
        assert "request_listener(request) != LISTENER_ADMIN" in block, func                         # настройка — только там


# Методы сервиса, которые проверяют пароль или код: каждый вызов из маршрута — под общим ограничением попыток.
_SECRET_CHECKS = ("reauthenticate(", "begin_totp_setup(", "confirm_totp_setup(", "disable_totp(", "regenerate_recovery_codes(",
                  "change_own_password(")


def test_every_password_or_code_check_in_a_session_is_throttled():
    """Аудит 2026-11-08: украденный токен не должен позволять подбирать пароль и код без ограничения попыток."""
    auth = (API / "routes_auth.py").read_text(encoding="utf-8")
    tree = ast.parse(auth)
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        source = ast.get_source_segment(auth, node) or ""
        for call in _SECRET_CHECKS:
            if "." + call in source:
                assert "_guarded(request, user.username" in source, f"{node.name}: {call} без ограничения попыток"
    login = auth[auth.index("def login("):auth.index("def login_second_step(")]
    assert login.index("_throttle_check(form.username, client_ip)") < login.index("service.authenticate(")
    second = auth[auth.index("def login_second_step("):auth.index("def refresh(")]
    assert second.index("_throttle_check(service.mfa_challenge_username(") < second.index("complete_mfa_challenge(")
    guarded = auth[auth.index("def _guarded("):auth.index("def _issue(")]
    assert guarded.index("_throttle_check(") < guarded.index("action()") and 'kind", "") == "invalid"' in guarded


def test_owner_sessions_are_not_visible_to_other_admins():
    auth = (API / "routes_auth.py").read_text(encoding="utf-8")
    for func in ("def user_sessions(", "def end_user_sessions(", "def update_user(", "def reset_user_2fa("):
        block = auth[auth.index(func):auth.index(func) + 1400]
        assert "_guard_target(service, admin, user_id)" in block, func


def test_auth_responses_are_never_cached():
    from app.core.security_headers import headers_for
    for path in ("/api/v1/auth/login", "/api/v1/auth/2fa/confirm", "/api/v1/users", "/api/v1/users/1/sessions"):
        assert headers_for({"path": path, "scheme": "http"}).get("cache-control") == "no-store", path
    assert "cache-control" not in headers_for({"path": "/api/v1/catalog/products", "scheme": "http"})


def test_pages_are_revalidated_so_an_update_is_seen_at_once():
    """Страницы и скрипты /ui — no-cache: после обновления программы браузер не показывает старую страницу (найдено вживую
    2026-11-14: «Настройки» без QR-кода второго фактора из кэша браузера). Это не no-store: в них нет секретов."""
    from app.core.security_headers import headers_for
    for path in ("/ui/index.html", "/ui/settings.js", "/ui/shop.html", "/ui"):
        assert headers_for({"path": path, "scheme": "http"}).get("cache-control") == "no-cache", path


def test_admin_only_rights_include_catalog_editing_and_are_derived_from_the_role_table():
    """Решение владельца по В-5 (2026-11-08): правка каталога и все административные данные — только на административном входе.
    Состав выводится из таблицы ролей: право, которое есть только у администратора, защищено автоматически."""
    from app.core.security import ADMIN_ONLY_PERMISSIONS, ROLE_PERMISSIONS, Permission, Role
    assert ADMIN_ONLY_PERMISSIONS == {Permission.USER_MANAGE, Permission.STORE_MANAGE, Permission.CAMERA_MANAGE,
                                      Permission.CATALOG_WRITE}
    for perm in ADMIN_ONLY_PERMISSIONS:
        assert [r for r, perms in ROLE_PERMISSIONS.items() if perm in perms] == [Role.ADMIN], perm
    assert Permission.CAMERA_AGENT not in ADMIN_ONLY_PERMISSIONS          # агент камер работает из сети магазина


def test_every_route_with_an_admin_only_right_goes_through_the_listener_check():
    """Сервер, а не страница: каждый маршрут с правом «только администратор» получает его через require_permission или
    require_recent_auth (обе проверяют вход программы и сеанс). Список маршрутов — для документа 18, §13."""
    from app.core.security import ADMIN_ONLY_PERMISSIONS
    names = {p.name for p in ADMIN_ONLY_PERMISSIONS}
    found = {}
    for path in ROUTE_FILES:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"(require_permission|require_recent_auth)\(Permission\.(\w+)\)", text):
            if match.group(2) in names:
                found.setdefault(match.group(2), set()).add(path.name)
        # прямых проверок прав и ролей в обход зависимостей нет; право «только администратор» упоминается только в require_*
        assert ".has_permission(" not in text, path.name
        for name in names:
            assert text.count(f"Permission.{name}") == len(re.findall(rf"(?:require_permission|require_recent_auth)\(Permission\.{name}\)", text)), (path.name, name)
    assert set(found) == names - {"CAMERA_MANAGE"}, found               # маршрутов камер в этой версии ещё нет (право зарезервировано)
    assert "routes_catalog.py" in found["CATALOG_WRITE"]
    catalog = (API / "routes_catalog.py").read_text(encoding="utf-8")
    assert catalog.count("require_permission(Permission.CATALOG_WRITE)") == 7          # + PATCH …/images/{фото}, POST …/status (Р-7)


def test_password_change_pass_is_accepted_only_by_the_initial_password_route():
    """Пропуск «смена временного пароля» (документ 05, §12.2) принимает один маршрут; обычный сеанс при временном пароле не действует."""
    users = set()
    for path in ROUTE_FILES:
        for method, route, func, deps in _routes(path):
            if "get_password_changing_user" in deps:
                users.add((method, (_prefix(path) + route).replace("/api/v1", "")))
    assert users == {("post", "/auth/password/initial")}
