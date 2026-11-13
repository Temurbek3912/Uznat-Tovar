"""Составляет MANIFEST.json — список файлов, изменённых относительно прежних выпусков передачи, с контрольными суммами. По нему tools/apply_update.py
безопасно переносит обновление в рабочий проект: заменяет только изменённые файлы и не трогает те, что человек правил сам.

    python tools/make_update_manifest.py --tree . --old handoff_2026-10-25.zip --old handoff_2026-10-27.zip --label 2026-10-28 --out ../MANIFEST.json

Если прежних выпусков под рукой нет, а есть только последний выпуск и его MANIFEST.json, список можно «продолжить» (в нём уже записаны версии файлов из ещё более ранних выпусков):

    python tools/make_update_manifest.py --tree . --previous-release handoff_2026-10-29.zip --previous-manifest MANIFEST_2026-10-29.json --label 2026-10-30 --out ../MANIFEST.json

Файлы, которые были в прежнем выпуске, а теперь из проекта убраны, попадают в раздел «remove» (с их прежними версиями): apply_update.py переносит их в папку копий, но не стирает безвозвратно.

Для каждого файла проекта: нет ни в одном прежнем выпуске — «added»; отличается от какого-то прежнего ИЛИ есть не во всех прежних выпусках — «changed» (в old_sha256 перечислены все прежние
версии, которые считаются «своими старыми»: проект на любой из них обновляется без конфликта; пусто — файл появился в одном из прежних выпусков и с тех пор не менялся); совпадает во всех прежних — не попадает в список. Суммы считаются по тексту с переводами
строк, приведёнными к LF: на Windows файлы часто лежат с CRLF, и без этого каждый файл выглядел бы «изменённым человеком».
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import zipfile

TEXT_SUFFIXES = {".py", ".js", ".html", ".css", ".md", ".json", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".bat", ".ps1", ".example", ".env", ".sh", ".mako"}
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", "node_modules", ".git", ".venv", "venv"}
ARCHIVE_PREFIX = "retail-shelf-audit/"


def normalized_sha256(data: bytes, name: str) -> str:
    """SHA-256 содержимого; у текстовых файлов CRLF считается как LF."""
    if pathlib.PurePosixPath(name).suffix.lower() in TEXT_SUFFIXES or pathlib.PurePosixPath(name).name.startswith(".env"):
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def zip_hashes(zip_path: pathlib.Path, prefix: str = ARCHIVE_PREFIX) -> dict[str, str]:
    result: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.filename.endswith("/") or not info.filename.startswith(prefix):
                continue
            rel = info.filename[len(prefix):]
            if not any(part in SKIP_PARTS for part in rel.split("/")) and not rel.endswith(".pyc"):
                result[rel] = normalized_sha256(archive.read(info), rel)
    return result


def tree_hashes(tree: pathlib.Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(tree.rglob("*")):
        if not path.is_file() or path.suffix == ".pyc":
            continue
        rel = path.relative_to(tree).as_posix()
        if any(part in SKIP_PARTS for part in rel.split("/")):
            continue
        result[rel] = normalized_sha256(path.read_bytes(), rel)
    return result


def build_manifest(tree: dict[str, str], old_releases: list[dict[str, str]], label: str) -> dict:
    entries = []
    for rel, new_sha in sorted(tree.items()):
        olds = sorted({release[rel] for release in old_releases if rel in release} - {new_sha})
        in_every_release = all(rel in release for release in old_releases)
        if not any(rel in release for release in old_releases):
            entries.append({"path": rel, "status": "added", "new_sha256": new_sha})
        elif olds or not in_every_release:
            # «не в каждом прежнем выпуске» — файл появился в одном из них и с тех пор не менялся; проект на более раннем выпуске его не имеет, ему он нужен
            entries.append({"path": rel, "status": "changed", "old_sha256": olds, "new_sha256": new_sha})
    gone = sorted({rel for release in old_releases for rel in release} - set(tree))
    return {"label": label, "files": entries, "gone_since_previous": gone}


def extend_manifest(tree: dict[str, str], previous_release: dict[str, str], previous_manifest: dict, label: str) -> dict:
    """Продолжает цепочку: previous_release — суммы файлов последнего выпуска, previous_manifest — его список (в нём уже есть версии из более ранних выпусков).
    Файл попадает в новый список, если он был в прежнем списке (он нужен тем, кто его ещё не получил) или изменился с прошлого выпуска; все известные прежние версии сохраняются."""
    prev_entries = {entry["path"]: entry for entry in previous_manifest.get("files", [])}
    entries = []
    for rel, new_sha in sorted(tree.items()):
        prev_entry, prev_sha = prev_entries.get(rel), previous_release.get(rel)
        olds = set(prev_entry.get("old_sha256", [])) if prev_entry else set()
        if prev_sha is not None:
            olds.add(prev_sha)
        olds.discard(new_sha)
        if prev_entry is None and prev_sha == new_sha:
            continue                                   # не менялся ни в одном выпуске
        if prev_sha is None:
            entries.append({"path": rel, "status": "added", "new_sha256": new_sha})
        elif prev_entry is not None and prev_entry.get("status") == "added" and not olds:
            entries.append({"path": rel, "status": "added", "new_sha256": new_sha})     # так и остался «новым» для тех, у кого его не было
        else:
            entries.append({"path": rel, "status": "changed", "old_sha256": sorted(olds), "new_sha256": new_sha})
    removed: dict[str, set[str]] = {}
    for rel, sha in previous_release.items():
        if rel not in tree:
            removed.setdefault(rel, set()).add(sha)
            if rel in prev_entries:
                removed[rel] |= set(prev_entries[rel].get("old_sha256", []))
    for entry in previous_manifest.get("remove", []):   # то, что убирали ещё раньше, тоже остаётся в списке
        if entry["path"] not in tree:
            removed.setdefault(entry["path"], set()).update(entry.get("old_sha256", []))
    remove = [{"path": rel, "old_sha256": sorted(shas)} for rel, shas in sorted(removed.items())]
    return {"label": label, "files": entries, "remove": remove, "gone_since_previous": sorted(rel for rel in previous_release if rel not in tree)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree", required=True, help="папка проекта (с app/, frontend/, …)")
    parser.add_argument("--old", action="append", default=[], help="прежний выпуск передачи (zip); можно несколько раз")
    parser.add_argument("--previous-release", help="последний выпуск (zip), если прежних выпусков под рукой нет; вместе с --previous-manifest")
    parser.add_argument("--previous-manifest", help="MANIFEST.json последнего выпуска (в нём версии файлов из ещё более ранних выпусков)")
    parser.add_argument("--label", required=True, help="метка выпуска, например 2026-10-28")
    parser.add_argument("--out", required=True, help="куда записать MANIFEST.json")
    args = parser.parse_args(argv)
    tree = tree_hashes(pathlib.Path(args.tree))
    if args.previous_release or args.previous_manifest:
        if not (args.previous_release and args.previous_manifest) or args.old:
            parser.error("--previous-release и --previous-manifest нужны вместе и не совмещаются с --old")
        previous_manifest = json.loads(pathlib.Path(args.previous_manifest).read_text(encoding="utf-8"))
        manifest = extend_manifest(tree, zip_hashes(pathlib.Path(args.previous_release)), previous_manifest, args.label)
    else:
        manifest = build_manifest(tree, [zip_hashes(pathlib.Path(p)) for p in args.old], args.label)
    pathlib.Path(args.out).write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    added = sum(1 for e in manifest["files"] if e["status"] == "added")
    print(f"MANIFEST: {len(manifest['files'])} файлов (добавлено {added}, изменено {len(manifest['files']) - added}); убрать: {len(manifest.get('remove', []))}; исчезло с прошлого выпуска: {len(manifest['gone_since_previous'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
