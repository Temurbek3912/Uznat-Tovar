"""Безопасное применение обновления (tools/apply_update.py + tools/make_update_manifest.py): заменяет только изменённые файлы, не трогает правленные человеком,
узнаёт несколько прежних версий, не путает CRLF с правкой, ничего не пишет без --apply, ловит повреждённый архив."""
from __future__ import annotations

import json
import zipfile

from tools import apply_update as au
from tools import make_update_manifest as mm

OLD, MID, NEW = "версия 1\n", "версия 2\n", "версия 3\n"


def _exits(call):
    """True, если вызов завершился SystemExit (так скрипт останавливается на повреждённом архиве); без pytest.raises, чтобы тест шёл и без pytest."""
    try:
        call()
    except SystemExit:
        return True
    return False


def _write(root, rel, text, crlf=False):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data.replace(b"\n", b"\r\n") if crlf else data)


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for rel, text in files.items():
            archive.writestr("retail-shelf-audit/" + rel, text)


def _release(tmp_path):
    """Выпуск из трёх версий: base (OLD) -> prev (MID) -> новая (NEW); файлы: a.py меняется, b.py не меняется, c.py добавлен позже, d.py менялся в prev и больше нет."""
    base, prev = tmp_path / "base.zip", tmp_path / "prev.zip"
    _zip(base, {"app/x.py": "x\n", "frontend/a.py": OLD, "frontend/b.py": "b\n", "frontend/d.py": OLD})
    _zip(prev, {"app/x.py": "x\n", "frontend/a.py": MID, "frontend/b.py": "b\n", "frontend/d.py": MID, "frontend/e.py": "добавлен в прошлом выпуске\n"})
    source = tmp_path / "pkg" / "retail-shelf-audit"
    for rel, text in {"app/x.py": "x\n", "frontend/a.py": NEW, "frontend/b.py": "b\n", "frontend/c.py": "новый\n", "frontend/d.py": MID, "frontend/e.py": "добавлен в прошлом выпуске\n"}.items():
        _write(source, rel, text)
    manifest = mm.build_manifest(mm.tree_hashes(source), [mm.zip_hashes(base), mm.zip_hashes(prev)], "t")
    (tmp_path / "pkg" / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return source, manifest


def _project(tmp_path, files, crlf=False):
    project = tmp_path / "proj" / "retail-shelf-audit"
    for rel, text in {"app/x.py": "x\n", **files}.items():
        _write(project, rel, text, crlf)
    return project


def _run(tmp_path, project, *flags):
    return au.main(["--project", str(project), "--source", str(tmp_path / "pkg" / "retail-shelf-audit"), "--manifest", str(tmp_path / "pkg" / "MANIFEST.json"), *flags])


def test_manifest_lists_only_changed_and_added_files_with_every_known_old_version(tmp_path):
    _source, manifest = _release(tmp_path)
    by_path = {e["path"]: e for e in manifest["files"]}
    assert set(by_path) == {"frontend/a.py", "frontend/c.py", "frontend/d.py", "frontend/e.py"}, "b.py и x.py не менялись ни в одном выпуске — в списке их нет"
    assert by_path["frontend/e.py"].get("old_sha256", []) == [], "e.py появился в прошлом выпуске и не менялся: тем, у кого его нет (исходная версия), он нужен, тем, у кого есть, — «уже применено»"
    assert by_path["frontend/c.py"]["status"] == "added"
    assert len(by_path["frontend/a.py"]["old_sha256"]) == 2, "проект на исходной версии и на прежнем выпуске обновляется без конфликта"
    assert len(by_path["frontend/d.py"]["old_sha256"]) == 1, "d.py совпадает с прежним выпуском, но нужен тем, кто его не применял"


def test_project_on_the_oldest_or_the_previous_release_is_updated_and_unrelated_files_are_left_alone(tmp_path):
    for index, old_text in enumerate((OLD, MID)):
        sandbox = tmp_path / f"scenario{index}"
        sandbox.mkdir()
        _release(sandbox)
        had_e = {"frontend/e.py": "добавлен в прошлом выпуске\n"} if old_text == MID else {}
        project = _project(sandbox, {"frontend/a.py": old_text, "frontend/b.py": "b\n", "tests/test_mine.py": "моё исправление\n", **had_e})
        assert _run(sandbox, project, "--apply") == 0
        assert (project / "frontend/e.py").read_text(encoding="utf-8") == "добавлен в прошлом выпуске\n", "файл из прошлого выпуска есть у всех, у кого раньше его не было, — в том числе на исходной версии"
        assert (project / "frontend/a.py").read_text(encoding="utf-8") == NEW and (project / "frontend/c.py").read_text(encoding="utf-8") == "новый\n"
        assert (project / "tests/test_mine.py").read_text(encoding="utf-8") == "моё исправление\n", "файл вне списка не тронут"
        kept = sandbox / "proj" / "retail-shelf-audit_before_update_t" / "frontend" / "a.py"
        assert kept.read_text(encoding="utf-8") == old_text, "прежняя версия сохранена перед заменой"


def test_a_file_the_user_edited_is_not_overwritten_and_the_new_version_is_put_beside_it(tmp_path):
    _release(tmp_path)
    project = _project(tmp_path, {"frontend/a.py": "моя правка\n"})
    assert _run(tmp_path, project, "--apply") == 2
    assert (project / "frontend/a.py").read_text(encoding="utf-8") == "моя правка\n"
    assert (project / "frontend/a.py.from_update").read_text(encoding="utf-8") == NEW
    assert (project / "frontend/c.py").exists(), "остальное применилось"
    assert not (tmp_path / "proj" / "retail-shelf-audit_before_update_t" / "frontend" / "a.py").exists(), "правленный файл в «до» не копируется: он остался на месте"


def test_windows_line_endings_are_not_mistaken_for_an_edit(tmp_path):
    _release(tmp_path)
    project = _project(tmp_path, {"frontend/a.py": OLD}, crlf=True)
    assert _run(tmp_path, project, "--apply") == 0
    assert (project / "frontend/a.py").read_text(encoding="utf-8") == NEW


def test_without_apply_nothing_is_written_and_a_second_apply_changes_nothing(tmp_path):
    _release(tmp_path)
    project = _project(tmp_path, {"frontend/a.py": OLD})
    assert _run(tmp_path, project) == 0
    assert (project / "frontend/a.py").read_text(encoding="utf-8") == OLD and not (project / "frontend/c.py").exists()
    assert not (tmp_path / "proj" / "retail-shelf-audit_before_update_t").exists()
    assert _run(tmp_path, project, "--apply") == 0
    snapshot = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert _run(tmp_path, project, "--apply") == 0, "повторное применение — «уже применено»"
    assert snapshot == {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*") if p.is_file()}


def test_broken_archive_or_wrong_folder_stops_before_writing_anything(tmp_path):
    _release(tmp_path)
    project = _project(tmp_path, {"frontend/a.py": OLD})
    (tmp_path / "pkg" / "retail-shelf-audit" / "frontend" / "c.py").write_text("испорчено\n", encoding="utf-8")
    assert _exits(lambda: _run(tmp_path, project, "--apply"))
    assert (project / "frontend/a.py").read_text(encoding="utf-8") == OLD, "до записи дело не дошло"
    (tmp_path / "pkg" / "retail-shelf-audit" / "frontend" / "c.py").unlink()
    assert _exits(lambda: _run(tmp_path, project)), "нет файла из списка — архив распакован не до конца"
    assert au.main(["--project", str(tmp_path / "нет-такой"), "--source", str(tmp_path / "pkg" / "retail-shelf-audit"), "--manifest", str(tmp_path / "pkg" / "MANIFEST.json")]) == 1


# ---------------------------------------------------------------- убранные файлы (выпуск 2026-10-30: озвучка удалена из проекта)
GONE = "модуль, который убрали\n"


def _chain(tmp_path):
    """Три выпуска: base -> prev (список построен от base) -> новый, где frontend/gone.js убран, a.py изменён, c.py добавлен; список нового продолжает цепочку от prev."""
    base, prev = tmp_path / "base.zip", tmp_path / "prev.zip"
    _zip(base, {"app/x.py": "x\n", "frontend/a.py": OLD, "frontend/b.py": "b\n", "frontend/gone.js": GONE})
    _zip(prev, {"app/x.py": "x\n", "frontend/a.py": MID, "frontend/b.py": "b\n", "frontend/gone.js": GONE, "frontend/e.py": "добавлен в прошлом выпуске\n"})
    prev_manifest = mm.build_manifest(mm.zip_hashes(prev), [mm.zip_hashes(base)], "prev")      # как был бы список прошлого выпуска
    source = tmp_path / "pkg" / "retail-shelf-audit"
    for rel, text in {"app/x.py": "x\n", "frontend/a.py": NEW, "frontend/b.py": "b\n", "frontend/c.py": "новый\n", "frontend/e.py": "добавлен в прошлом выпуске\n"}.items():
        _write(source, rel, text)
    manifest = mm.extend_manifest(mm.tree_hashes(source), mm.zip_hashes(prev), prev_manifest, "t")
    (tmp_path / "pkg" / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return source, manifest


def test_chained_manifest_keeps_every_older_version_and_lists_the_removed_file(tmp_path):
    _source, manifest = _chain(tmp_path)
    by_path = {e["path"]: e for e in manifest["files"]}
    assert set(by_path) == {"frontend/a.py", "frontend/c.py", "frontend/e.py"}, "b.py и x.py не менялись ни в одном выпуске — в списке их нет; gone.js убран — он в разделе remove"
    assert len(by_path["frontend/a.py"]["old_sha256"]) == 2, "версии из исходного и из прошлого выпуска сохранены — проект на любом обновляется без конфликта"
    assert by_path["frontend/e.py"]["status"] == "added" and not by_path["frontend/e.py"].get("old_sha256"), "добавленный в прошлом выпуске и с тех пор не менявшийся остаётся «новым» для тех, у кого его нет"
    assert [e["path"] for e in manifest["remove"]] == ["frontend/gone.js"] and len(manifest["remove"][0]["old_sha256"]) == 1
    assert manifest["gone_since_previous"] == ["frontend/gone.js"]


def test_a_removed_file_is_moved_to_the_backup_not_erased_and_a_second_run_changes_nothing(tmp_path):
    _chain(tmp_path)
    project = _project(tmp_path, {"frontend/a.py": MID, "frontend/gone.js": GONE, "frontend/e.py": "добавлен в прошлом выпуске\n"})
    assert _run(tmp_path, project) == 0 and (project / "frontend/gone.js").exists(), "без --apply ничего не убирается"
    assert _run(tmp_path, project, "--apply") == 0
    assert not (project / "frontend/gone.js").exists()
    kept = tmp_path / "proj" / "retail-shelf-audit_before_update_t" / "frontend" / "gone.js"
    assert kept.read_text(encoding="utf-8") == GONE, "убранный файл лежит в папке копий — его можно вернуть копированием"
    snapshot = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert _run(tmp_path, project, "--apply") == 0, "повторное применение: файла уже нет — это не ошибка"
    assert snapshot == {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*") if p.is_file()}


def test_a_removed_file_the_user_edited_stays_where_it_is_and_the_run_reports_a_conflict(tmp_path):
    _chain(tmp_path)
    project = _project(tmp_path, {"frontend/a.py": MID, "frontend/gone.js": "моя правка\n"})
    assert _run(tmp_path, project, "--apply") == 2
    assert (project / "frontend/gone.js").read_text(encoding="utf-8") == "моя правка\n", "правленный файл не удаляется"
    assert (project / "frontend/a.py").read_text(encoding="utf-8") == NEW, "остальное применилось"
    assert not (tmp_path / "proj" / "retail-shelf-audit_before_update_t" / "frontend" / "gone.js").exists()


def test_a_project_that_never_had_the_removed_file_and_windows_line_endings_are_both_fine(tmp_path):
    _chain(tmp_path)
    project = _project(tmp_path, {"frontend/a.py": OLD})
    assert _run(tmp_path, project, "--apply") == 0 and not (project / "frontend/gone.js").exists()
    sandbox = tmp_path / "crlf"
    sandbox.mkdir()
    _chain(sandbox)
    crlf_project = _project(sandbox, {"frontend/a.py": MID, "frontend/gone.js": GONE}, crlf=True)
    assert _run(sandbox, crlf_project, "--apply") == 0 and not (crlf_project / "frontend/gone.js").exists(), "CRLF — не правка человека"


def test_a_manifest_with_an_unsafe_path_stops_before_anything_is_written(tmp_path):
    _chain(tmp_path)
    manifest_path = tmp_path / "pkg" / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    outside = tmp_path / "proj" / "outside.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("не моё\n", encoding="utf-8")
    for bad in ("../outside.txt", "/etc/passwd", "C:/Windows/x.txt", "frontend/../../outside.txt"):
        broken = dict(manifest, remove=[{"path": bad, "old_sha256": []}])
        manifest_path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        project = _project(tmp_path, {"frontend/a.py": MID, "frontend/gone.js": GONE})
        assert _exits(lambda: _run(tmp_path, project, "--apply")), bad
        assert (project / "frontend/a.py").read_text(encoding="utf-8") == MID and (project / "frontend/gone.js").exists(), "до записи дело не дошло"
    assert outside.read_text(encoding="utf-8") == "не моё\n"
