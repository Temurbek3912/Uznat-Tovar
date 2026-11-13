"""Диагностика БД, администрирование пользователей, охрана seed_catalog, шапка preflight, резервная копия."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from scripts import backup_db as bk
from scripts import diagnose as dg
from scripts import preflight as pf
from scripts import user_admin as ua


# ------------------------------------------------------------------ diagnose.analyze

def _inputs(**kw):
    base = dict(counts={"users": 1, "active_admins": 1, "stores": 1, "shelves": 1, "products": 3, "planogram_slots": 4, "snapshots": 2},
                snapshot_status={"done": 2}, shelves=[("s1", "A")], snapshot_pairs=[("A", "s1")], planogram_shelf_ids=["s1"],
                users=[("admin", "admin", True, 0)])
    base.update(kw)
    return dg.Inputs(**base)


def _texts(findings, level=None):
    return [f.text for f in findings if level is None or f.level == level]


def test_healthy_data_has_only_an_ok_finding():
    findings = dg.analyze(_inputs())
    assert [f.level for f in findings] == ["ok"]


def test_empty_stores_is_explained_as_not_a_connection_error():
    findings = dg.analyze(_inputs(counts={"users": 1, "active_admins": 1, "stores": 0}, shelves=[], snapshot_pairs=[], planogram_shelf_ids=[]))
    text = " ".join(_texts(findings, "info"))
    assert "НЕ ошибка подключения" in text and "магазины не нужны" in text and "setup.html" not in text


def test_no_users_and_no_active_admin_are_warnings_with_the_right_command():
    none = dg.analyze(_inputs(counts={"users": 0, "stores": 1}))
    assert any("bootstrap" in t for t in _texts(none, "warn"))
    inactive = dg.analyze(_inputs(counts={"users": 2, "active_admins": 0, "stores": 1}))
    assert any("user_admin activate" in t for t in _texts(inactive, "warn"))


def test_snapshots_for_unregistered_shelf_are_found_with_the_exact_code():
    findings = dg.analyze(_inputs(snapshot_pairs=[("A", "s1"), ("A", "Shelf-X")]))
    warn = " ".join(_texts(findings, "warn"))
    assert "Shelf-X" in warn and "прежнего аудита" in warn


def test_snapshot_store_mismatch_is_detected():
    findings = dg.analyze(_inputs(snapshot_pairs=[("B", "s1")]))             # полка s1 заведена в магазине A, снимок — с магазином B
    assert any("другим магазином" in t and "s1" in t for t in _texts(findings, "warn"))


def test_planogram_without_shelf_and_shelves_without_planogram():
    findings = dg.analyze(_inputs(planogram_shelf_ids=["ghost"], shelves=[("s1", "A"), ("s2", "A")]))
    assert any("ghost" in t for t in _texts(findings, "warn"))
    assert any("Полок без планограммы: 2 из 2" in t for t in _texts(findings, "info"))


def test_failed_and_stuck_snapshots_are_reported():
    findings = dg.analyze(_inputs(snapshot_status={"failed": 3, "queued": 1, "processing": 2, "done": 5}))
    assert any("с ошибкой обработки: 3" in t for t in _texts(findings, "warn"))
    assert any("в очереди/обработке: 3" in t for t in _texts(findings, "info"))


def test_render_never_prints_password_hashes_and_marks_disabled_users():
    data = _inputs(users=[("admin", "admin", True, 0), ("old", "merchandiser", False, 2)])
    text = dg.render("postgresql+psycopg://postgres:***@db:5432/shelf_audit", True, [], data, dg.analyze(data))
    assert "ОТКЛЮЧЁН" in text and "***" in text and "scrypt" not in text and "$" not in text


def test_render_for_missing_tables_points_to_bootstrap():
    text = dg.render("x", False, ["users"], None, [])
    assert "bootstrap" in text and "не трогает" in text


# ------------------------------------------------------------------ user_admin

USERS = [("admin", "admin", True), ("boss2", "admin", False), ("m1", "merchandiser", True)]


def test_last_active_admin_cannot_be_deactivated():
    assert "последний активный администратор" in ua.deactivate_blocker(USERS, "admin")
    assert ua.deactivate_blocker(USERS + [("admin3", "admin", True)], "admin") is None


def test_deactivating_non_admin_or_already_inactive_admin_is_allowed_and_unknown_is_refused():
    assert ua.deactivate_blocker(USERS, "m1") is None
    assert ua.deactivate_blocker(USERS, "boss2") is None
    assert "не найден" in ua.deactivate_blocker(USERS, "nobody")


def _script(values):
    it = iter(values)
    return lambda prompt="": next(it)


def test_new_password_must_match_and_pass_policy():
    msgs = []
    pw = ua.read_new_password(_script(["abc", "abc", "good-long-pass-1", "other-long-pass-2", "correct-horse-battery", "correct-horse-battery"]),
                              out=msgs.append)
    assert pw == "correct-horse-battery" and len(msgs) == 2


def test_new_password_gives_up_after_attempts():
    assert ua.read_new_password(_script(["a", "b"] * 3), out=lambda m: None) is None


def test_username_confirmation_requires_exact_name():
    assert ua.confirm_username("admin", lambda p: " Admin ") is True
    assert ua.confirm_username("admin", lambda p: "y") is False


# ------------------------------------------------------------------ seed_catalog и preflight

def test_seed_refuses_non_empty_database_and_lists_reasons():
    from scripts.seed_catalog import seed_blockers
    assert seed_blockers({"stores": 0, "products": 0, "shelves": 0, "planogram_slots": 0}) == []
    reasons = seed_blockers({"stores": 2, "products": 40, "shelves": 0, "planogram_slots": 0})
    assert len(reasons) == 2 and "магазины: 2" in reasons[0] and "(SKU): 40" in reasons[1]


def test_seed_requires_explicit_flag():
    from scripts.seed_catalog import main as seed_main
    assert seed_main([]) == 1                                  # без флага ничего не происходит (и БД не трогается)


def test_preflight_banner_says_host_vs_docker_and_does_not_blame_python_315():
    host = "\n".join(pf.environment_banner(r"C:\Python315\python.exe", "3.15.0", in_docker=False))
    assert "НА ХОСТЕ" in host and "docker compose exec api python -m scripts.preflight" in host
    assert "НЕ означает" in host and "3.14" in host and "не мешает Docker-запуску" in host
    docker = "\n".join(pf.environment_banner("/usr/local/bin/python", "3.12.5", in_docker=True))
    assert "ВНУТРИ Docker-контейнера" in docker and "НА ХОСТЕ" not in docker


def test_preflight_banner_on_old_enough_python_has_no_version_advice():
    assert "3.14 (py -3.14)" not in "\n".join(pf.environment_banner("python", "3.14.6", in_docker=False))


# ------------------------------------------------------------------ backup_db

class FakeRunner:
    """Подменяет subprocess.run: pg_dump пишет в файл-приёмник, pg_restore --list возвращает заданный текст."""
    def __init__(self, dump_rc=0, dump_bytes=b"PGDMP-content", list_rc=0, listing=b"; Archive created at 2026\n1; 1259 16385 TABLE public users postgres\n"):
        self.dump_rc, self.dump_bytes, self.list_rc, self.listing, self.calls = dump_rc, dump_bytes, list_rc, listing, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if "pg_dump" in " ".join(cmd):
            kw["stdout"].write(self.dump_bytes)
            return SimpleNamespace(returncode=self.dump_rc, stderr=b"connection refused")
        return SimpleNamespace(returncode=self.list_rc, stdout=self.listing, stderr=b"")


NOW = lambda: datetime(2026, 10, 9, 14, 5, 7)


def test_backup_name_and_commands_use_non_tty_exec_and_container_variables():
    assert bk.backup_name(NOW()) == "shelf-audit-20261009-140507.dump"
    cmd = bk.dump_command()
    assert cmd[:4] == ["docker", "compose", "exec", "-T"] and "$POSTGRES_USER" in cmd[-1] and "-Fc" in cmd[-1]
    assert bk.dump_command("x.yml")[:4] == ["docker", "compose", "-f", "x.yml"]
    assert bk.verify_command()[-2:] == ["pg_restore", "--list"]


def test_successful_backup_is_verified_renamed_and_nothing_else_is_deleted(tmp_path):
    old = tmp_path / "shelf-audit-20250101-000000.dump"
    old.write_bytes(b"older backup")
    msgs = []
    assert bk.run_backup(tmp_path, runner=FakeRunner(), now=NOW, out=msgs.append) == 0
    final = tmp_path / "shelf-audit-20261009-140507.dump"
    assert final.read_bytes() == b"PGDMP-content" and not list(tmp_path.glob("*.part"))
    assert old.read_bytes() == b"older backup"                              # старые копии не трогаются
    assert any("SHA-256" in m for m in msgs) and any("data" in m for m in msgs)


def test_failed_dump_leaves_no_files_and_warns_against_destructive_steps(tmp_path):
    msgs = []
    rc = bk.run_backup(tmp_path, runner=FakeRunner(dump_rc=1), now=NOW, out=msgs.append)
    assert rc == 1 and list(tmp_path.iterdir()) == []
    assert any("down -v" in m for m in msgs) and any("connection refused" in m for m in msgs)


def test_empty_dump_and_unreadable_dump_are_rejected(tmp_path):
    assert bk.run_backup(tmp_path, runner=FakeRunner(dump_bytes=b""), now=NOW, out=lambda m: None) == 1
    assert bk.run_backup(tmp_path, runner=FakeRunner(listing=b"garbage"), now=NOW, out=lambda m: None) == 1
    assert bk.run_backup(tmp_path, runner=FakeRunner(list_rc=1), now=NOW, out=lambda m: None) == 1
    assert list(tmp_path.iterdir()) == []                                    # ни итогового файла, ни .part


def test_docker_missing_is_reported_clearly(tmp_path):
    def no_docker(cmd, **kw):
        raise FileNotFoundError("docker")
    msgs = []
    assert bk.run_backup(tmp_path, runner=no_docker, now=NOW, out=msgs.append) == 1
    assert any("Docker Desktop" in m for m in msgs) and list(tmp_path.iterdir()) == []
