"""Диагностика конфликтов обновления (tools/diagnose_conflicts.py): объясняет, ПОЧЕМУ apply_update.py счёл файл конфликтным — только оформление (BOM, кодировка, пробелы),
известная прежняя версия, правки человека (и из какой версии они выросли) или неизвестный файл; ничего не меняет в проекте и не выдаёт содержимое файлов без --with-diffs."""
from __future__ import annotations

import json
import zipfile

from tools import diagnose_conflicts as dc

OLD = "# версия 1\nline a\nline b\nline c\n"
NEW = "# версия 2\nline a\nline b changed\nline c\nline d\n"


def _archive(tmp_path, files: dict[str, tuple[str | None, str]]):
    """files: путь -> (старый текст или None, новый текст). Возвращает папку архива."""
    archive = tmp_path / "archive"
    entries = []
    for rel, (old, new) in files.items():
        target = archive / "retail-shelf-audit" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, encoding="utf-8", newline="")
        entry = {"path": rel, "status": "changed", "new_sha256": dc.digest(new.encode("utf-8"), rel), "old_sha256": [dc.digest(old.encode("utf-8"), rel)] if old is not None else []}
        entries.append(entry)
    (archive / "MANIFEST.json").write_text(json.dumps({"label": "t", "files": entries, "remove": []}), encoding="utf-8")
    return archive


def _project(tmp_path, files: dict[str, bytes]):
    project = tmp_path / "proj"
    for rel, data in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_bytes(data)
    return project


def _old_zip(tmp_path, files: dict[str, str], name="handoff_old.zip"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        for rel, text in files.items():
            z.writestr("retail-shelf-audit/" + rel, text)
    return path


def _run(tmp_path, archive, project, *extra):
    report = tmp_path / "report.txt"
    code = dc.main(["--project", str(project), "--archive", str(archive), "--report", str(report), *extra])
    text = report.read_text(encoding="utf-8-sig")
    verdicts = {line.split("] ", 1)[1]: line[1] for line in text.split("\n") if line.startswith("[")}
    return code, verdicts, text


def test_a_project_holding_only_known_versions_has_no_conflicts(tmp_path):
    archive = _archive(tmp_path, {"a.py": (OLD, NEW), "b.js": (OLD, NEW), "c.md": (None, NEW)})
    project = _project(tmp_path, {"a.py": OLD.encode(), "b.js": NEW.encode()})
    code, verdicts, text = _run(tmp_path, archive, project)
    assert code == 0 and verdicts == {} and "конфликтов 0" in text and "заменить 1, добавить 1, уже применено 1" in text


def test_format_only_differences_are_recognised_without_any_old_archive(tmp_path):
    archive = _archive(tmp_path, {"old_bom.py": (OLD, NEW), "new_utf16.js": (OLD, NEW), "old_cp1251.css": (OLD, NEW), "new_spaces.txt": (OLD, NEW)})
    project = _project(tmp_path, {
        "old_bom.py": b"\xef\xbb\xbf" + OLD.encode("utf-8"),                       # BOM в начале
        "new_utf16.js": NEW.encode("utf-16"),                                       # Блокнот сохранил как Unicode
        "old_cp1251.css": OLD.encode("cp1251"),                                     # сохранён как «ANSI»
        "new_spaces.txt": NEW.replace("\n", "  \n").encode("utf-8"),                # редактор оставил пробелы в конце строк
    })
    code, verdicts, text = _run(tmp_path, archive, project)
    assert code == 2 and verdicts == {"old_bom.py": "B", "new_utf16.js": "A", "old_cp1251.css": "B", "new_spaces.txt": "A"}, verdicts
    assert "UTF-8 с BOM" in text and "UTF-16" in text and "cp1251" in text


def test_a_version_with_trailing_spaces_needs_the_old_archive_to_be_recognised(tmp_path):
    body = "".join(f"row {i}\n" for i in range(40))                   # у настоящих файлов десятки и сотни строк: версии похожи друг на друга
    old, new = "# v1\n" + body, "# v2\n" + body.replace("row 7\n", "row seven\n")
    archive = _archive(tmp_path, {"x.md": (old, new)})
    project = _project(tmp_path, {"x.md": old.replace("\n", "   \n").encode()})
    assert _run(tmp_path, archive, project)[1] == {"x.md": "C"}, "без старого архива пробелы в конце не отличить от правки"
    old = _old_zip(tmp_path, {"x.md": old})
    code, verdicts, text = _run(tmp_path, archive, project, "--old-archive", str(old))
    assert verdicts == {"x.md": "B"} and "handoff_old.zip" in text and "заменять безопасно" in text


def test_real_edits_are_reported_with_the_closest_base_and_the_size_of_the_edit(tmp_path):
    archive = _archive(tmp_path, {"worker.py": (OLD, NEW)})
    mine = OLD + "my_extra_1 = 1\nmy_extra_2 = 2\nmy_extra_3 = 3\n"
    project = _project(tmp_path, {"worker.py": mine.encode()})
    old = _old_zip(tmp_path, {"worker.py": OLD})
    code, verdicts, text = _run(tmp_path, archive, project, "--old-archive", str(old))
    assert code == 2 and verdicts == {"worker.py": "C"}
    assert "ваши правки относительно неё: +3 / −0 строк" in text and "ближайшая «база»" in text
    assert "нужно слияние" in text


def test_an_unrelated_or_binary_file_is_not_called_a_known_version(tmp_path):
    archive = _archive(tmp_path, {"mystery.py": (OLD, NEW)})
    (archive / "retail-shelf-audit" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    manifest = json.loads((archive / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"].append({"path": "logo.png", "status": "changed", "new_sha256": dc.digest((archive / "retail-shelf-audit" / "logo.png").read_bytes(), "logo.png"), "old_sha256": []})
    (archive / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    project = _project(tmp_path, {"mystery.py": ("совсем другое содержимое\n" * 40).encode(), "logo.png": b"\x89PNG\r\n\x1a\n" + b"\x01" * 40})
    code, verdicts, text = _run(tmp_path, archive, project)
    assert verdicts == {"mystery.py": "D", "logo.png": "C"} and "не текстовый файл" in text


def test_many_conflicts_with_edits_point_at_a_stale_archive_but_a_few_do_not(tmp_path):
    """Если проект новее архива, его файлы выглядят «правками»: так и бывает при проверке по старому архиву. Много таких конфликтов сразу — повод проверить метку архива."""
    many = {f"f{i}.py": (OLD, NEW) for i in range(6)}
    archive = _archive(tmp_path, many)
    project = _project(tmp_path, {rel: (NEW + f"# новее архива {i}\n").encode() for i, rel in enumerate(many)})
    code, verdicts, text = _run(tmp_path, archive, project)
    assert code == 2 and len(verdicts) == 6 and "ВНИМАНИЕ: сразу много конфликтов" in text and "СТАРОМУ архиву" in text and "«t»" in text
    few_dir = tmp_path / "few"
    few_dir.mkdir()
    archive2 = _archive(few_dir, {f"g{i}.py": (OLD, NEW) for i in range(2)})
    project2 = _project(few_dir, {f"g{i}.py": (NEW + "# моя правка\n").encode() for i in range(2)})
    assert "ВНИМАНИЕ" not in _run(few_dir, archive2, project2)[2], "два конфликта — ещё не повод подозревать старый архив"


def test_the_report_has_no_file_contents_unless_diffs_are_requested_and_the_project_is_untouched(tmp_path):
    archive = _archive(tmp_path, {"secret.py": (OLD, NEW)})
    mine = OLD + "PASSWORD_SENTINEL = 'hunter2'\n"
    project = _project(tmp_path, {"secret.py": mine.encode()})
    before = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    _, _, plain = _run(tmp_path, archive, project)
    assert "hunter2" not in plain and "PASSWORD_SENTINEL" not in plain
    _, _, detailed = _run(tmp_path, archive, project, "--with-diffs")
    assert "PASSWORD_SENTINEL" in detailed
    after = {p.relative_to(project).as_posix(): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert before == after, "диагностика только читает: проект не изменился, новых файлов в нём нет"


def test_missing_archive_or_project_stops_with_a_message_instead_of_a_traceback(tmp_path):
    archive = _archive(tmp_path, {"a.py": (OLD, NEW)})
    project = _project(tmp_path, {"a.py": OLD.encode()})
    assert dc.main(["--project", str(tmp_path / "нет"), "--archive", str(archive), "--report", str(tmp_path / "r.txt")]) == 1
    assert dc.main(["--project", str(project), "--archive", str(tmp_path / "пусто"), "--report", str(tmp_path / "r.txt")]) == 1
