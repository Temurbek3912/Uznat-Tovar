"""
app/core/cors.py — какие источники допущены в API (CORS). Чистая логика, выполняется офлайн.
Плюс статические проверки проводки: app/main.py, Settings, docker-compose.yml, .env.example (fastapi здесь не нужен).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

from app.core.cors import describe_cors, is_production, resolve_cors_origins

ROOT = Path(__file__).resolve().parent.parent


def _error_text(raw: str, environment: str = "development") -> str:
    with pytest.raises(ValueError) as info:
        resolve_cors_origins(raw, environment)
    return str(info.value)


# ------------------------------------------------------------------ значения по умолчанию

def test_development_without_value_keeps_the_old_wide_open_behavior():
    for raw in (None, "", "   ", " , ,"):
        assert resolve_cors_origins(raw, "development") == ["*"], repr(raw)


def test_production_without_value_closes_cross_origin_access():
    for raw in (None, "", "   ", " , ,"):
        assert resolve_cors_origins(raw, "production") == [], repr(raw)


def test_only_exact_production_counts_as_production_like_the_docs_switch_in_main():
    assert is_production("production") is True
    for env in ("development", "staging", "Production", "", None):
        assert is_production(env) is False, env
    assert resolve_cors_origins("", "staging") == ["*"]


# ------------------------------------------------------------------ явный список

def test_listed_origins_are_returned_as_is_in_both_modes():
    raw = "https://audit.example.com,http://192.168.1.50:8000"
    for env in ("development", "production"):
        assert resolve_cors_origins(raw, env) == ["https://audit.example.com", "http://192.168.1.50:8000"], env


def test_spaces_case_trailing_slash_and_duplicates_are_normalized():
    raw = " HTTPS://Audit.Example.com/ , http://192.168.1.50:8000 ,https://audit.example.com "
    assert resolve_cors_origins(raw, "production") == ["https://audit.example.com", "http://192.168.1.50:8000"]


def test_ipv6_and_localhost_origins_are_accepted():
    assert resolve_cors_origins("http://[::1]:8000,http://localhost:3000", "production") == \
        ["http://[::1]:8000", "http://localhost:3000"]


def test_explicit_star_means_any_origin_in_both_modes():
    assert resolve_cors_origins("*", "production") == ["*"]
    assert resolve_cors_origins(" * ", "development") == ["*"]


def test_star_cannot_be_mixed_with_specific_origins():
    assert "«*»" in _error_text("*,https://a.example.com")
    assert "«*»" in _error_text("https://a.example.com,*", "production")


# ------------------------------------------------------------------ ошибки в значении

def test_wildcard_inside_an_address_is_rejected_with_a_hint():
    text = _error_text("https://*.example.com")
    assert "подстановочные" in text and "https://*.example.com" in text


def test_addresses_with_path_query_or_without_scheme_are_rejected():
    for bad in ("https://a.example.com/ui", "https://a.example.com/?x=1", "a.example.com", "ftp://a.example.com",
                "http://", "http://a b.example.com", "http://a.example.com:80a", "null"):
        assert "не адрес источника" in _error_text(bad), bad


def test_port_must_be_in_valid_range():
    for bad in ("http://a.example.com:0", "http://a.example.com:65536", "http://a.example.com:99999"):
        assert "порт" in _error_text(bad), bad
    assert resolve_cors_origins("http://a.example.com:1,http://b.example.com:65535", "production") == \
        ["http://a.example.com:1", "http://b.example.com:65535"]


def test_one_bad_item_rejects_the_whole_value_instead_of_being_silently_dropped():
    assert "https://oops/path" in _error_text("https://ok.example.com,https://oops/path", "production")


# ------------------------------------------------------------------ строка для журнала

def test_describe_covers_all_three_states_and_warns_about_star_in_production():
    assert "выключен" in describe_cors([], "production")
    assert "любой" in describe_cors(["*"], "development").lower() and "небезопасно" not in describe_cors(["*"], "development")
    assert "небезопасно" in describe_cors(["*"], "production")
    text = describe_cors(["https://a.example.com", "http://b:1"], "production")
    assert "https://a.example.com" in text and "http://b:1" in text and "небезопасно" not in text


# ------------------------------------------------------------------ проводка (статически: fastapi здесь не нужен)

def _main_tree() -> ast.Module:
    return ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))


def test_main_takes_origins_from_settings_not_from_a_hardcoded_star():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "resolve_cors_origins(settings.cors_allow_origins, settings.environment)" in source
    assert 'allow_origins=["*"]' not in source and "allow_origins=['*']" not in source


def _is_cors_add(call) -> bool:
    """app.add_middleware(CORSMiddleware, ...). Другие промежуточные звенья (заголовки безопасности, 2026-11-07) ставятся всегда."""
    return (isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "add_middleware" and bool(call.args)
            and isinstance(call.args[0], ast.Name) and call.args[0].id == "CORSMiddleware")


def test_cors_middleware_is_added_only_when_the_origin_list_is_not_empty():
    # app.add_middleware(CORSMiddleware, ...) должен стоять внутри `if _cors_origins:` — иначе в production без значения
    # middleware всё равно подключится (с пустым списком), а главное — условие можно потерять при правке.
    guarded = False
    for node in _main_tree().body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "_cors_origins":
            for sub in ast.walk(node):
                if _is_cors_add(sub):
                    guarded = True
    assert guarded
    for node in _main_tree().body:                                       # и вне условия вызова нет
        if isinstance(node, ast.Expr) and _is_cors_add(node.value):
            raise AssertionError("add_middleware(CORSMiddleware) вызван вне `if _cors_origins:`")


def test_middleware_gets_the_resolved_list_and_delete_is_allowed_for_the_admin_panel():
    call = next(c for c in ast.walk(_main_tree()) if _is_cors_add(c))
    kwargs = {k.arg: k.value for k in call.keywords}
    assert isinstance(kwargs["allow_origins"], ast.Name) and kwargs["allow_origins"].id == "_cors_origins"
    methods = {el.value for el in kwargs["allow_methods"].elts}
    assert methods == {"GET", "POST", "PATCH", "DELETE"}
    assert "allow_credentials" not in kwargs                              # токен в заголовке, не в cookie


def test_settings_have_an_empty_default_for_cors_origins():
    tree = ast.parse((ROOT / "app" / "core" / "config.py").read_text(encoding="utf-8"))
    settings_cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings")
    fields = {n.target.id: n.value for n in settings_cls.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    assert isinstance(fields["cors_allow_origins"], ast.Constant) and fields["cors_allow_origins"].value == ""


def test_compose_passes_cors_origins_to_api_only_with_empty_default():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]

    def env(name: str) -> dict:
        value = compose[name].get("environment") or {}
        return value if isinstance(value, dict) else dict(item.split("=", 1) for item in value)

    for http_service in ("api", "admin-api"):                     # оба входа программы (решение В-1) — одна настройка
        assert env(http_service)["CORS_ALLOW_ORIGINS"] == "${CORS_ALLOW_ORIGINS:-}"
    for name in compose:
        if name not in ("api", "admin-api"):
            assert "CORS_ALLOW_ORIGINS" not in env(name), name


def test_env_example_documents_the_variable_and_stays_commented_out():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^#CORS_ALLOW_ORIGINS=$", text, re.M)
    assert not re.search(r"^CORS_ALLOW_ORIGINS=", text, re.M)             # по умолчанию не задана


def test_docs_no_longer_claim_that_cors_is_open_by_default_everywhere():
    for rel in ("docs/DOCKER.md", "README.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "CORS_ALLOW_ORIGINS" in text, rel
        assert "по умолчанию разрешает любые источники" not in text, rel
