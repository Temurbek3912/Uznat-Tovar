"""scripts/bootstrap.py — решения первого запуска. Работа с реальной БД проверяется только на Postgres (см. docs/DOCKER.md)."""
from __future__ import annotations

from scripts import bootstrap as bs


# ------------------------------------------------------------------ кто станет админом

def test_existing_active_admin_wins_over_everything():
    plan = bs.plan_admin(1, {"ADMIN_USERNAME": "boss", "ADMIN_PASSWORD": "long-enough-pass"}, interactive=True)
    assert plan.action == "exists" and plan.password == ""


def test_credentials_from_env_are_used_without_questions():
    plan = bs.plan_admin(0, {"ADMIN_USERNAME": " Boss ", "ADMIN_PASSWORD": "long-enough-pass"}, interactive=False)
    assert (plan.action, plan.username, plan.password) == ("from_env", "Boss", "long-enough-pass")


def test_password_without_username_means_admin():
    assert bs.read_admin_env({"ADMIN_PASSWORD": "long-enough-pass"}) == ("admin", "long-enough-pass")


def test_username_without_password_is_not_enough():
    assert bs.read_admin_env({"ADMIN_USERNAME": "boss"}) == ("boss", "")
    assert bs.plan_admin(0, {"ADMIN_USERNAME": "boss"}, interactive=True) == bs.AdminPlan("prompt", "boss")


def test_no_env_and_terminal_means_prompt_otherwise_manual_instructions():
    assert bs.plan_admin(0, {}, interactive=True) == bs.AdminPlan("prompt", "admin")
    assert bs.plan_admin(0, {}, interactive=False).action == "manual"
    assert bs.plan_admin(0, {"ADMIN_USERNAME": "", "ADMIN_PASSWORD": ""}, interactive=False).action == "manual"


# ------------------------------------------------------------------ проверка логина/пароля

def test_policy_errors_are_reported_not_raised():
    assert bs.validate_credentials("admin", "short")[1] is not None            # < 8 символов
    assert bs.validate_credentials("a", "long-enough-pass")[1] is not None     # короткий логин
    assert bs.validate_credentials("Админ", "long-enough-pass")[1] is not None  # кириллица в логине


def test_good_credentials_pass_and_login_is_canonical():
    assert bs.validate_credentials("  Admin ", "correct-horse-battery") == ("admin", None)


def test_typical_and_login_equal_passwords_are_refused():
    for weak in ("password", "12345678", "Change-Me", "adminadmin"):
        assert bs.validate_credentials("admin", weak)[1] is not None, weak
    assert bs.validate_credentials("operator1", "operator1")[1] is not None


# ------------------------------------------------------------------ интерактивный ввод

def _scripted(inputs):
    it = iter(inputs)
    return lambda prompt="": next(it)


def test_prompt_accepts_default_username_on_empty_input():
    creds = bs.prompt_credentials("admin", input_fn=_scripted([""]), getpass_fn=_scripted(["correct-horse-battery"] * 2),
                                  out=lambda m: None)
    assert creds == ("admin", "correct-horse-battery")


def test_prompt_retries_after_mismatch_then_after_weak_password():
    messages = []
    creds = bs.prompt_credentials(
        "admin",
        input_fn=_scripted(["boss", "boss", "boss"]),
        getpass_fn=_scripted(["correct-horse-battery", "different-one-here",      # не совпали
                              "password", "password",                              # слабый
                              "correct-horse-battery", "correct-horse-battery"]),  # принят
        out=messages.append)
    assert creds == ("boss", "correct-horse-battery")
    assert len(messages) == 2 and "не совпадают" in messages[0]


def test_prompt_gives_up_after_three_bad_attempts():
    creds = bs.prompt_credentials("admin", input_fn=_scripted(["", "", ""]),
                                  getpass_fn=_scripted(["a", "b"] * 3), out=lambda m: None)
    assert creds is None


# ------------------------------------------------------------------ папки

def test_ensure_directories_creates_missing_and_reports_only_new(tmp_path):
    (tmp_path / "data").mkdir()
    created = bs.ensure_directories(tmp_path, ("data", "logs", "models/index"), extra=(tmp_path / "data" / "shelf-images",))
    assert {p.relative_to(tmp_path).as_posix() for p in created} == {"logs", "models/index", "data/shelf-images"}
    assert bs.ensure_directories(tmp_path, ("data", "logs", "models/index"), extra=(tmp_path / "data" / "shelf-images",)) == []


def test_runtime_dirs_cover_snapshots_index_weights_and_logs():
    assert {"data", "logs", "models/index", "models/detector", "models/embedder"} <= set(bs.RUNTIME_DIRS)


# ------------------------------------------------------------------ ожидание БД

def test_wait_until_succeeds_after_a_few_failures_and_counts_retries():
    calls = {"n": 0}
    seen = []

    def check():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("not yet")

    now = {"t": 0.0}
    ok = bs.wait_until(check, timeout=30, interval=2, sleep=lambda s: now.__setitem__("t", now["t"] + s),
                       clock=lambda: now["t"], on_retry=lambda n, e: seen.append((n, type(e).__name__)))
    assert ok and calls["n"] == 3 and seen == [(1, "ConnectionError"), (2, "ConnectionError")]


def test_wait_until_gives_up_at_the_deadline():
    now = {"t": 0.0}

    def check():
        raise OSError("down")

    ok = bs.wait_until(check, timeout=10, interval=3, sleep=lambda s: now.__setitem__("t", now["t"] + s),
                       clock=lambda: now["t"])
    assert ok is False and now["t"] >= 10


# ------------------------------------------------------------------ устаревшая схема

def test_schema_drift_lists_missing_columns_only_for_existing_tables():
    expected = {"stores": {"id", "name", "address"}, "users": {"id", "username"}, "new_table": {"id"}}
    actual = {"stores": {"id", "name"}, "users": {"id", "username", "legacy_extra"}}
    assert bs.find_schema_drift(expected, actual) == {"stores": ["address"]}


def test_no_drift_when_schema_matches():
    assert bs.find_schema_drift({"a": {"x"}}, {"a": {"x", "y"}}) == {}


# ------------------------------------------------------------------ сообщения bootstrap: ни одного совета удалить данные

def test_drift_report_never_suggests_deleting_the_database_and_points_to_backup_first():
    text = "\n".join(bs.drift_report_lines({"stores": ["region"], "shelves": ["x", "y"]}))
    assert "down -v" not in text and "пересоздавайте" in text and "не удаляйте" in text
    assert "backup_db.py" in text and text.index("backup_db.py") < text.index("ALTER TABLE")        # копия — раньше правки схемы
    assert "stores: region" in text and "shelves: x, y" in text and "данные НЕ тронуты" in text


def test_schema_summary_names_the_created_tables_and_says_existing_data_is_untouched():
    assert bs.schema_summary([]) == "уже актуальна"
    one = bs.schema_summary(["cameras", "agent_state", "camera_events"])
    assert "создано таблиц — 3: agent_state, camera_events, cameras" in one and "данные не затронуты" in one



def test_owner_protects_initial_creation_no_new_admin_when_an_owner_exists():
    """Владелец назначен, но все администраторы отключены: bootstrap не создаёт нового администратора ни из .env, ни по вопросу."""
    env = {"ADMIN_USERNAME": "intruder", "ADMIN_PASSWORD": "long-enough-pass"}
    assert bs.plan_admin(0, env, interactive=True, owner_exists=True) == bs.AdminPlan("owner_exists")
    assert bs.plan_admin(0, {}, interactive=True, owner_exists=True).action == "owner_exists"
    assert bs.plan_admin(0, env, interactive=False).action == "from_env"            # первая установка — как раньше
    assert bs.plan_admin(1, env, interactive=False, owner_exists=True).action == "exists"


def test_first_admin_becomes_the_owner_in_the_code_path():
    src = (bs.Path(bs.__file__)).read_text(encoding="utf-8")
    assert "if service.owner_id() is None:\n        service.set_owner(user.id)" in src
