"""Диагностика конфликтов обновления. ТОЛЬКО ЧТЕНИЕ: в проекте ничего не меняется, пишется один отчёт (текстовый файл).
Для каждого «конфликтного» файла (apply_update.py не решился его заменить) объясняет ПРИЧИНУ и подсказывает, что с ним делать.
Только стандартная библиотека. Лежит рядом с apply_update.py и MANIFEST.json в распакованном архиве.

  python diagnose_conflicts.py --project D:\\retail-shelf-audit
  python diagnose_conflicts.py --project D:\\retail-shelf-audit --old-archive D:\\Downloads\\handoff_2026-10-27.zip --old-archive D:\\Downloads\\handoff_2026-10-28.zip
  python diagnose_conflicts.py --project D:\\retail-shelf-audit --with-diffs        # добавить в отчёт построчные отличия (в них будет ваш код)

Отчёт (по умолчанию «conflicts_report.txt» рядом со скриптом) содержит имена файлов, размеры, суммы и числа строк — без содержимого файлов,
если не указан --with-diffs. Его можно целиком прислать в чат.
Вердикты по каждому файлу:
  A  ЭТО НОВАЯ ВЕРСИЯ    — файл уже новый, различия только в оформлении (BOM, кодировка, пробелы в конце строк): ничего делать не нужно
  B  ЭТО СТАРАЯ ВЕРСИЯ   — известная прежняя версия с оформлением, как у вас: заменять безопасно
  C  ВАШИ ПРАВКИ         — в файле есть содержательные отличия: нужно слияние (сравните с «базой», если она найдена)
  D  НЕИЗВЕСТНАЯ ВЕРСИЯ  — файл ни на что известное не похож (сходство с новой версией ниже 50 %)
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import json
import pathlib
import sys
import zipfile

TEXT_SUFFIXES = {".py", ".js", ".html", ".css", ".md", ".json", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".bat", ".ps1", ".example", ".env", ".sh", ".mako"}
ROOT_IN_ZIP = "retail-shelf-audit/"


def is_text(name: str) -> bool:
    path = pathlib.PurePosixPath(name)
    return path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith(".env")


def digest(data: bytes, name: str) -> str:
    """Та же сумма, что у apply_update.py: у текстовых файлов CRLF считается как LF."""
    if is_text(name):
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def decode(data: bytes) -> tuple[str, str]:
    """(текст, кодировка). Пробует UTF-8 (с BOM и без), UTF-16 с BOM, затем cp1251 (так сохраняет Блокнот в «ANSI»)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "UTF-8 с BOM"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace"), "UTF-16"
    try:
        return data.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        return data.decode("cp1251", "replace"), "cp1251 (не UTF-8)"


def canonical(data: bytes) -> bytes:
    """Содержимое без «оформления»: BOM, кодировка и концы строк приведены к UTF-8 + LF."""
    text, _ = decode(data)
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def lenient_lines(data: bytes) -> list[str]:
    """Строки без пробелов и табуляций в конце и без пустых строк в конце файла (так иногда «чистит» редактор)."""
    lines = [line.rstrip(" \t") for line in canonical(data).decode("utf-8", "replace").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def line_ending(data: bytes) -> str:
    crlf, lf = data.count(b"\r\n"), data.count(b"\n") - data.count(b"\r\n")
    if crlf and lf:
        return f"смешанные (CRLF {crlf}, LF {lf})"
    return "CRLF" if crlf else ("LF" if lf else "одна строка")


def similarity(a: list[str], b: list[str]) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def diff_counts(old: list[str], new: list[str]) -> tuple[int, int]:
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def classify(entry: dict, project: pathlib.Path) -> str:
    target = project / entry["path"]
    if not target.is_file():
        return "add"
    current = digest(target.read_bytes(), entry["path"])
    if current == entry["new_sha256"]:
        return "same"
    return "replace" if current in entry.get("old_sha256", []) else "conflict"


def load_old_archives(paths: list[str]) -> list[tuple[str, zipfile.ZipFile]]:
    result = []
    for p in paths:
        try:
            result.append((pathlib.Path(p).name, zipfile.ZipFile(p)))
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"  (старый архив {p} не открылся: {exc}; пропускаю)")
    return result


def old_version(zf: zipfile.ZipFile, path: str) -> bytes | None:
    for prefix in (ROOT_IN_ZIP, ""):
        try:
            return zf.read(prefix + path)
        except KeyError:
            continue
    return None


def diagnose_file(entry: dict, project: pathlib.Path, source: pathlib.Path, olds: list, with_diffs: bool) -> dict:
    path = entry["path"]
    data = (project / path).read_bytes()
    new_data = (source / path).read_bytes()
    _, encoding = decode(data)
    info: dict = {"path": path, "size": len(data), "ending": line_ending(data), "encoding": encoding, "bom": data.startswith(b"\xef\xbb\xbf"),
                  "mtime": datetime.datetime.fromtimestamp((project / path).stat().st_mtime).strftime("%Y-%m-%d %H:%M"), "sum": digest(data, path)[:12], "notes": []}
    if not is_text(path):
        info.update(verdict="C", why="не текстовый файл (картинка, шрифт и т. п.) — отличается побайтно")
        return info
    # 1) известная версия, если не считать оформление: BOM / кодировка / концы строк
    formatted = hashlib.sha256(canonical(data)).hexdigest()
    if formatted == entry["new_sha256"]:
        info.update(verdict="A", why=f"это НОВАЯ версия; отличается только оформлением ({encoding}, концы строк {info['ending']})")
        return info
    if formatted in entry.get("old_sha256", []):
        info.update(verdict="B", why=f"это известная СТАРАЯ версия; отличается только оформлением ({encoding}, концы строк {info['ending']}): заменять безопасно")
        return info
    # 2) новая версия, если ещё и убрать пробелы в конце строк
    mine, theirs = lenient_lines(data), lenient_lines(new_data)
    if mine == theirs:
        info.update(verdict="A", why="это НОВАЯ версия; отличаются только пробелы в конце строк или пустые строки в конце файла")
        return info
    ratio = similarity(mine, theirs)
    added, removed = diff_counts(theirs, mine)
    info["vs_new"] = f"сходство с новой версией {ratio:.0%}; относительно новой: +{added} / −{removed} строк"
    # 3) старые архивы пользователя: ищем «базу» — версию, из которой выросли его правки
    best = None
    for name, zf in olds:
        old = old_version(zf, path)
        if old is None:
            continue
        if hashlib.sha256(canonical(old)).hexdigest() == formatted:
            info.update(verdict="B", why=f"совпадает с версией из архива {name} (с точностью до оформления): заменять безопасно")
            return info
        old_lines = lenient_lines(old)
        if old_lines == mine:
            info.update(verdict="B", why=f"совпадает с версией из архива {name}, отличаются только пробелы в конце строк: заменять безопасно")
            return info
        r = similarity(old_lines, mine)
        if best is None or r > best[0]:
            best = (r, name, old_lines)
    if best:
        add_b, rem_b = diff_counts(best[2], mine)
        info["base"] = f"ближайшая «база» — версия из {best[1]} (сходство {best[0]:.0%}); ваши правки относительно неё: +{add_b} / −{rem_b} строк"
    if ratio < 0.5 and (best is None or best[0] < 0.5):
        info.update(verdict="D", why="файл не похож ни на что известное (сходство с новой версией ниже 50 %)")
    else:
        info.update(verdict="C", why="в файле есть содержательные отличия: нужно слияние ваших правок с новой версией")
    if with_diffs:
        diff = list(difflib.unified_diff(theirs, mine, "новая версия (из архива)", "ваш файл", lineterm="", n=2))
        info["diff"] = diff[:120] + ([f"… ещё {len(diff) - 120} строк"] if len(diff) > 120 else [])
    return info


ADVICE = {
    "A": "ничего делать не нужно: файл уже новый, различия только в оформлении.",
    "B": "заменять безопасно: это известная прежняя версия (будет заменена после резервной копии).",
    "C": "нужно слияние: ваши правки сохраняются, новое из архива добавляется (это делаем вместе: пришлите отчёт, файлы не нужны, пока я не попрошу).",
    "D": "нужен ваш взгляд: файл ни на что известное не похож — возможно, он из другой ветки проекта или сильно переписан.",
}


def main(argv: list[str] | None = None) -> int:
    here = pathlib.Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Диагностика конфликтов обновления (только чтение)")
    ap.add_argument("--project", required=True)
    ap.add_argument("--archive", default=str(here), help="папка с MANIFEST.json и retail-shelf-audit (по умолчанию — рядом со скриптом)")
    ap.add_argument("--old-archive", action="append", default=[], help="ваш прежний архив передачи (zip); можно несколько раз — так находится «база» ваших правок")
    ap.add_argument("--report", default=str(here / "conflicts_report.txt"))
    ap.add_argument("--with-diffs", action="store_true", help="добавить в отчёт построчные отличия (в них будет содержимое ваших файлов)")
    args = ap.parse_args(argv)
    archive, project = pathlib.Path(args.archive), pathlib.Path(args.project)
    manifest_path, source = archive / "MANIFEST.json", archive / "retail-shelf-audit"
    if not manifest_path.is_file() or not source.is_dir():
        print(f"В папке {archive} нет MANIFEST.json или папки retail-shelf-audit: положите скрипт в распакованный архив или укажите --archive.")
        return 1
    if not project.is_dir():
        print(f"Проект не найден: {project}")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["files"]
    olds = load_old_archives(args.old_archive)
    kinds = {"add": 0, "replace": 0, "same": 0, "conflict": 0}
    conflicts = []
    for entry in entries:
        kind = classify(entry, project)
        kinds[kind] += 1
        if kind == "conflict":
            conflicts.append(diagnose_file(entry, project, source, olds, args.with_diffs))
    removals = manifest.get("remove", [])

    out = [f"ДИАГНОСТИКА КОНФЛИКТОВ — архив «{manifest['label']}», записей в списке: {len(entries)}, на удаление: {len(removals)}",
           f"проект: {project}", f"создан: {datetime.datetime.now():%Y-%m-%d %H:%M}",
           f"старых архивов для поиска «базы»: {[n for n, _ in olds] or 'не указаны'}", "",
           f"Сводка: заменить {kinds['replace']}, добавить {kinds['add']}, уже применено {kinds['same']}, конфликтов {kinds['conflict']}."]
    if not conflicts:
        out += ["", "Конфликтов нет: обновление можно применять по инструкции (сначала без --apply, потом с ним)."]
    else:
        verdicts: dict[str, int] = {}
        for item in conflicts:
            verdicts[item["verdict"]] = verdicts.get(item["verdict"], 0) + 1
        out += ["Из конфликтов: " + ", ".join(f"{v}: {n}" for v, n in sorted(verdicts.items())) + "  (A — уже новый, B — старый, безопасно заменять, C — ваши правки, D — неизвестная версия)", ""]
        if len(conflicts) >= 5 and verdicts.get("A", 0) + verdicts.get("B", 0) < len(conflicts) / 2:
            out += [f"ВНИМАНИЕ: сразу много конфликтов с правками ({len(conflicts)}). Частая причина — проверка по более СТАРОМУ архиву, чем ваш проект: тогда его новые файлы выглядят «вашими правками». "
                    f"Метка этого архива: «{manifest['label']}». Возьмите самый свежий архив, распакуйте его в новую папку и повторите первую проверку (apply_update.py без --apply): "
                    "если конфликтов станет меньше, разбирать файлы по одному не нужно.", ""]
        for item in sorted(conflicts, key=lambda x: (x["verdict"], x["path"])):
            out += [f"[{item['verdict']}] {item['path']}", f"    {item['why']}",
                    f"    размер {item['size']} байт; концы строк: {item['ending']}; кодировка: {item['encoding']}; изменён: {item['mtime']}; сумма: {item['sum']}…"]
            for key in ("vs_new", "base"):
                if key in item:
                    out.append(f"    {item[key]}")
            if "diff" in item:
                out += ["    --- отличия (ваш файл против новой версии) ---"] + [f"    {line}" for line in item["diff"]]
            out.append("")
        out += ["ЧТО ДАЛЬШЕ"] + [f"  {v}: {ADVICE[v]}" for v in sorted(verdicts)]
    report = "\n".join(out) + "\n"
    pathlib.Path(args.report).write_text(report, encoding="utf-8-sig")
    print(report)
    print(f"Отчёт сохранён: {args.report}  (в нём нет содержимого файлов{', кроме построчных отличий' if args.with_diffs else ''})")
    return 0 if not conflicts else 2


if __name__ == "__main__":
    sys.exit(main())
