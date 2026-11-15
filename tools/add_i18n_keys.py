"""Добавить новые строки интерфейса сразу во все четыре словаря (ru, uz, uz-Cyrl, en).

    python tools/add_i18n_keys.py новые_строки.json

Файл: {"ключ": {"ru": "...", "uz": "...", "uz-Cyrl": "...", "en": "..."}, ...}. Уже существующий ключ заменяется (строка целиком),
новый вставляется перед закрывающей скобкой словаря. Узбекская кириллица — черновик для вычитки носителем (как и весь i18n.uz-cyrl.js).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "frontend"
FILES = {"ru": "i18n.ru.js", "uz": "i18n.uz.js", "uz-Cyrl": "i18n.uz-cyrl.js", "en": "i18n.en.js"}


def add(entries: dict[str, dict[str, str]]) -> None:
    for lang, name in FILES.items():
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        new_lines = []
        for key, values in entries.items():
            value = values[lang]
            line = f"    {json.dumps(key, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)},"
            pattern = re.compile(r"^    " + re.escape(json.dumps(key, ensure_ascii=False)) + r": .*,?$", re.M)
            if pattern.search(text):
                text = pattern.sub(lambda _m: line, text, count=1)
            else:
                new_lines.append(line)
        if new_lines:
            end = text.rindex("\n  };")
            before = text[:end].rstrip()
            if not before.endswith(","):
                before += ","
            text = before + "\n" + "\n".join(new_lines).rstrip(",") + text[end:]
        path.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    add(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
