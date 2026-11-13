"""
Создаёт на рабочем столе Windows ярлык «Узнать товар» с иконкой — двойной щелчок запускает всю демонстрацию
(start_demo.bat: Docker-сервисы, API, воркер, QR-код для телефона).

    python -m scripts.make_shortcut

Только для Windows. Рабочий стол определяется системой (учитывает перенос в OneDrive). Ярлык можно перетащить на панель задач.
Если переместили папку проекта — создайте ярлык заново.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHORTCUT_NAME = "Узнать товар"


def ps_quote(value: str) -> str:
    """Строка в одинарных кавычках PowerShell: внутри кавычки удваиваются, подстановок нет."""
    return "'" + str(value).replace("'", "''") + "'"


def build_ps_script(*, target: Path, workdir: Path, icon: Path, name: str = SHORTCUT_NAME,
                    description: str = "Запуск демонстрации: учёт товаров на полках") -> str:
    return "\r\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$desktop = [Environment]::GetFolderPath('Desktop')",
        f"$path = Join-Path $desktop {ps_quote(name + '.lnk')}",
        "$ws = New-Object -ComObject WScript.Shell",
        "$s = $ws.CreateShortcut($path)",
        f"$s.TargetPath = {ps_quote(str(target))}",
        f"$s.WorkingDirectory = {ps_quote(str(workdir))}",
        f"$s.IconLocation = {ps_quote(str(icon) + ',0')}",
        f"$s.Description = {ps_quote(description)}",
        "$s.WindowStyle = 1",
        "$s.Save()",
        "Write-Output $path",
    ])


def main(argv: list[str] | None = None, *, platform: str = sys.platform, run=subprocess.run,
         root: Path = ROOT, out=print) -> int:
    if not platform.startswith("win"):
        out("Ярлык создаётся только в Windows. На других системах запускайте: python -m scripts.start_demo")
        return 1
    target, icon = root / "start_demo.bat", root / "frontend" / "icons" / "app.ico"
    for p in (target, icon):
        if not p.is_file():
            out(f"Не найден файл {p}. Проверьте, что вы запускаете скрипт из папки проекта.")
            return 1
    script = build_ps_script(target=target, workdir=root, icon=icon)
    with tempfile.TemporaryDirectory() as tmp:
        ps1 = Path(tmp) / "make_shortcut.ps1"
        ps1.write_text(script, encoding="utf-8-sig")        # BOM: иначе PowerShell 5 читает кириллицу в старой кодировке
        res = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                  capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        out("Не удалось создать ярлык:\n" + (res.stderr or res.stdout or "").strip())
        return 1
    out(f"Ярлык «{SHORTCUT_NAME}» создан на рабочем столе: {(res.stdout or '').strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
