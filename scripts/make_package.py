"""
Собрать архив программы для магазина: распаковать на компьютере магазина и дважды щёлкнуть «Установить.bat».

    python -m scripts.make_package                 # dist/uznat-tovar-<дата>-<коммит>.zip
    python -m scripts.make_package --out D:\\archives

В архив попадает последний коммит git (git archive) — без базы, фото, моделей, .env, журналов и резервных копий (их нет в git).
Тесты и архив старых документов не входят (.gitattributes, export-ignore). Незакоммиченные изменения в архив НЕ попадают —
об этом предупреждение.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "uznat-tovar/"
CLIENT_FILE = "google_client.json"


def package_name(commit: str, today: str) -> str:
    return f"uznat-tovar-{today}-{commit[:7]}.zip"


def main(argv: list[str] | None = None, run=subprocess.run, out=print, root: Path = ROOT) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=root / "dist")
    a = ap.parse_args(argv)
    git = lambda *args: run(["git", *args], cwd=str(root), capture_output=True, text=True)  # noqa: E731
    head = git("rev-parse", "HEAD")
    if head.returncode != 0:
        out("Это не папка git — архив собирается из git (git archive).")
        return 1
    dirty = [ln for ln in (git("status", "--porcelain").stdout or "").splitlines() if not ln.startswith("??")]
    if dirty:
        out(f"ВНИМАНИЕ: {len(dirty)} изменённых файлов не закоммичены — в архив попадёт последний коммит без них.")
    a.out.mkdir(parents=True, exist_ok=True)
    target = a.out / package_name(head.stdout.strip(), time.strftime("%Y%m%d"))
    res = git("archive", "--format=zip", f"--prefix={PREFIX}", "-o", str(target), "HEAD")
    if res.returncode != 0:
        out("Архив не собрался: " + (res.stderr or "").strip())
        return 1
    # Клиент программы у Google (кнопка «Подключить Google Диск», docs/GOOGLE_DRIVE_SETUP.md): в git его нет (.gitignore),
    # в архив для магазинов он кладётся отдельно. Google считает секрет такого клиента не секретным.
    client = root / CLIENT_FILE
    if client.is_file():
        with zipfile.ZipFile(target, "a") as z:
            z.write(client, PREFIX + CLIENT_FILE)
        out(f"Google Диск: {CLIENT_FILE} добавлен — в магазинах будет кнопка «Подключить Google Диск».")
    else:
        out(f"ВНИМАНИЕ: нет файла {CLIENT_FILE} — кнопки «Подключить Google Диск» в магазинах не будет "
            "(как его получить — docs/GOOGLE_DRIVE_SETUP.md).")
    out(f"Готово: {target} ({target.stat().st_size / 2**20:.1f} МБ). В магазине: распаковать, дважды щёлкнуть «Установить.bat».")
    return 0


if __name__ == "__main__":
    sys.exit(main())
