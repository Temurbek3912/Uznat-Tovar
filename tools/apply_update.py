"""Безопасно переносит обновление (распакованный архив передачи) в рабочий проект. Заменяет ТОЛЬКО файлы из MANIFEST.json и только если в проекте лежит одна из
прежних версий этого файла; файл, который вы правили сами, не трогается — новая версия кладётся рядом с суффиксом .from_update. Файлы из раздела «remove» (то, что убрано
из проекта) не стираются, а ПЕРЕНОСЯТСЯ в папку копий — и только если лежит прежняя версия; правленный вами файл остаётся на месте. По умолчанию ничего не меняет (проверка);
запись — только с --apply. Перед заменой или удалением каждый затрагиваемый файл копируется в папку «<проект>_before_update_<метка>» с той же структурой. Только стандартная библиотека.

    python apply_update.py --project D:\\retail-shelf-audit                 # посмотреть, что будет (ничего не пишет)
    python apply_update.py --project D:\\retail-shelf-audit --apply         # применить

Папка с новыми файлами — «retail-shelf-audit» рядом со скриптом (или --source). Коды выхода: 0 — всё чисто, 2 — есть конфликты (их файлы не тронуты), 1 — ошибка.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import sys

TEXT_SUFFIXES = {".py", ".js", ".html", ".css", ".md", ".json", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".bat", ".ps1", ".example", ".env", ".sh", ".mako"}


def normalized_sha256(data: bytes, name: str) -> str:
    """SHA-256 содержимого; у текстовых файлов CRLF считается как LF (на Windows файлы часто лежат с CRLF). Совпадает с tools/make_update_manifest.py."""
    path = pathlib.PurePosixPath(name)
    if path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith(".env"):
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def classify(entry: dict, project: pathlib.Path) -> str:
    """'add' — в проекте файла нет; 'replace' — лежит прежняя версия; 'same' — уже применено; 'conflict' — файл отличается от всех известных версий (правили вы)."""
    target = project / entry["path"]
    if not target.is_file():
        return "add"
    current = normalized_sha256(target.read_bytes(), entry["path"])
    if current == entry["new_sha256"]:
        return "same"
    if current in entry.get("old_sha256", []):
        return "replace"
    return "conflict"


def safe_relative(path: str) -> str:
    """Путь из MANIFEST.json должен быть относительным и не выходить из проекта (никаких «..» и абсолютных путей)."""
    parts = pathlib.PurePosixPath(path.replace("\\", "/")).parts
    if not path or pathlib.PurePosixPath(path).is_absolute() or ":" in path or ".." in parts:
        raise SystemExit(f"В MANIFEST.json небезопасный путь «{path}» — архив повреждён или изменён. Распакуйте его заново.")
    return path


def classify_removal(entry: dict, project: pathlib.Path) -> str:
    """'gone' — файла в проекте уже нет; 'remove' — лежит прежняя версия, можно убрать; 'keep' — файл отличается от всех известных версий (правили вы): не трогаем."""
    target = project / entry["path"]
    if not target.is_file():
        return "gone"
    return "remove" if normalized_sha256(target.read_bytes(), entry["path"]) in entry.get("old_sha256", []) else "keep"


def plan_removals(manifest: dict, project: pathlib.Path) -> list[tuple[str, dict]]:
    return [(classify_removal(entry, project), entry) for entry in manifest.get("remove", []) if safe_relative(entry["path"])]


def apply_removals(removals: list[tuple[str, dict]], project: pathlib.Path, backup: pathlib.Path) -> None:
    """Убираемый файл сначала копируется в папку копий (с той же структурой), потом удаляется из проекта: вернуть его можно простым копированием."""
    for action, entry in removals:
        if action != "remove":
            continue
        target = project / entry["path"]
        kept = backup / entry["path"]
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, kept)
        target.unlink()


def plan(manifest: dict, project: pathlib.Path, source: pathlib.Path) -> list[tuple[str, dict]]:
    """Проверяет целостность архива (каждый новый файл на месте и с нужной суммой) и раскладывает файлы по действиям."""
    result = []
    for entry in manifest["files"]:
        safe_relative(entry["path"])
        src = source / entry["path"]
        if not src.is_file():
            raise SystemExit(f"В архиве нет файла {entry['path']} — архив распакован не до конца или повреждён.")
        if normalized_sha256(src.read_bytes(), entry["path"]) != entry["new_sha256"]:
            raise SystemExit(f"Файл {entry['path']} в архиве не совпадает с MANIFEST.json — архив повреждён или изменён. Распакуйте его заново.")
        result.append((classify(entry, project), entry))
    return result


def apply(actions: list[tuple[str, dict]], project: pathlib.Path, source: pathlib.Path, backup: pathlib.Path) -> None:
    for action, entry in actions:
        rel, target, src = entry["path"], project / entry["path"], source / entry["path"]
        if action in ("add", "replace"):
            if action == "replace":
                kept = backup / rel
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, kept)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        elif action == "conflict":
            shutil.copyfile(src, target.with_name(target.name + ".from_update"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="рабочий проект, например D:\\retail-shelf-audit")
    parser.add_argument("--source", help="папка retail-shelf-audit из распакованного архива (по умолчанию — рядом со скриптом)")
    parser.add_argument("--manifest", help="MANIFEST.json (по умолчанию — рядом со скриптом)")
    parser.add_argument("--apply", action="store_true", help="записать изменения (без этого — только проверка)")
    parser.add_argument("--backup-dir", help="куда сложить заменяемые файлы (по умолчанию — рядом с проектом)")
    args = parser.parse_args(argv)

    here = pathlib.Path(__file__).resolve().parent
    project = pathlib.Path(args.project)
    source = pathlib.Path(args.source) if args.source else here / "retail-shelf-audit"
    manifest_path = pathlib.Path(args.manifest) if args.manifest else here / "MANIFEST.json"
    if not (project / "app").is_dir() or not (project / "frontend").is_dir():
        print(f"«{project}» не похоже на проект (нет папок app и frontend). Проверьте путь.")
        return 1
    if not manifest_path.is_file() or not source.is_dir():
        print(f"Не найдены MANIFEST.json ({manifest_path}) или папка с новыми файлами ({source}). Запускайте скрипт из распакованного архива.")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actions = plan(manifest, project, source)
    removals = plan_removals(manifest, project)

    labels = {"add": "добавить ", "replace": "заменить ", "same": "уже есть", "conflict": "КОНФЛИКТ "}
    for kind in ("conflict", "replace", "add"):
        for action, entry in actions:
            if action == kind:
                note = "  (вы правили этот файл сами — не трогаю, новая версия будет рядом: .from_update)" if kind == "conflict" else ""
                print(f"  {labels[kind]} {entry['path']}{note}")
    for action, entry in removals:
        if action == "remove":
            print(f"  убрать     {entry['path']}  (будет перенесён в папку копий, не стёрт)")
        elif action == "keep":
            print(f"  КОНФЛИКТ   {entry['path']}  (убран из проекта, но вы правили этот файл сами — не трогаю; удалите его сами или пришлите мне)")
    counts = {kind: sum(1 for action, _ in actions if action == kind) for kind in labels}
    removed = sum(1 for action, _ in removals if action == "remove")
    kept_edited = sum(1 for action, _ in removals if action == "keep")
    counts["conflict"] += kept_edited
    print(f"\nВыпуск {manifest['label']}: заменить {counts['replace']}, добавить {counts['add']}, убрать {removed}, уже применено {counts['same']}, конфликтов {counts['conflict']}.")

    if not args.apply:
        print("Это проверка: ничего не изменено. Для применения добавьте --apply.")
        return 2 if counts["conflict"] else 0
    backup = pathlib.Path(args.backup_dir) if args.backup_dir else project.parent / f"{project.name}_before_update_{manifest['label']}"
    apply(actions, project, source, backup)
    apply_removals(removals, project, backup)
    print(f"Готово. Заменённые и убранные файлы сохранены в «{backup}»." if counts["replace"] or removed else "Готово. Заменять было нечего.")
    if counts["conflict"]:
        print("Есть конфликты: эти файлы НЕ изменены. Сравните каждый с его .from_update и перенесите нужное вручную (или пришлите мне оба).")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
