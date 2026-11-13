"""Проверка узбекских текстов интерфейса носителем языка: выгрузка в таблицу Excel и безопасный возврат правок в frontend/i18n.uz.js.

Зачем: узбекские тексты написаны как ЧЕРНОВИК (так сказано и в шапке i18n.uz.js). Носителю языка не нужно открывать код: ему отдают таблицу, он заполняет колонку
«Исправление», таблица возвращается, правки вносятся в словарь одной командой — со всеми проверками (подстановки {имя}, апострофы, кириллица, длина).

  python tools/uz_review.py export uz_review.xlsx              выгрузить все тексты (нужен openpyxl: pip install openpyxl)
  python tools/uz_review.py import uz_review.xlsx              ПРОВЕРКА: показать, что изменится, и ошибки; ничего не записывает
  python tools/uz_review.py import uz_review.xlsx --apply      внести правки в frontend/i18n.uz.js (прежний файл сохраняется рядом: i18n.uz.js.before_review)

Чистая логика (разбор словаря, подстановки, апострофы, проверка и внесение правок, поиск мест, где виден текст) не требует openpyxl и проверяется tests/test_uz_review.py.
После --apply запустите `python tools/run_js_tests.py`: тест словарей проверит, что ключи и подстановки во всех трёх языках по-прежнему совпадают.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"
UZ_FILE = FRONT / "i18n.uz.js"
SHEET_TEXTS, SHEET_HELP = "Тексты", "Как заполнять"
COLUMNS = ["Ключ", "Где виден", "Подстановки", "Русский (смысл)", "English (для сверки)", "Oʻzbekcha сейчас", "Знаков сейчас", "Исправление", "Комментарий"]

# Какой экран показывает текст (по имени файла, который его использует). Старые страницы и прежнее меню, где тексты зашиты в код, сюда не входят.
SCREENS = {
    "identify.html": "Узнать товар", "identify.js": "Узнать товар", "ticket.js": "Ценник на «Узнать товар» и «Товарах»",
    "products.html": "Товары", "products.js": "Товары", "productlist.js": "Список товаров", "catalog.js": "Товары (общее)",
    "settings.html": "Настройки", "settings.js": "Настройки", "auth.js": "Вход (все страницы)", "i18n.js": "Переключатель языка", "theme.js": "Тема оформления",
    "preview.html": "Витрина интерфейса (preview.html)", "preview.js": "Витрина интерфейса (preview.html)",
}

_KEY_LINE = re.compile(r'^(\s*)"([\w.]+)"(\s*:\s*)(.*?)(,?)\s*$')


# ------------------------------------------------------------------ разбор словаря (чистая логика)
def parse_dictionary(source: str) -> dict:
    """Словарь из текста i18n.<язык>.js: ключ -> строка или {форма: строка}. Формат файла — по одному ключу в строке."""
    result: dict = {}
    for line in source.split("\n"):
        m = _KEY_LINE.match(line)
        if not m:
            continue
        value = m.group(4)
        if value.startswith("{"):                                   # формы множественного числа: ключи форм бывают с кавычками и без — приводим к JSON
            value = re.sub(r'\b(one|few|many|other)"?\s*:', r'"\1":', value.replace('"one"', "one").replace('"few"', "few").replace('"many"', "many").replace('"other"', "other"))
        result[m.group(2)] = json.loads(value)
    return result


def flatten(value) -> str:
    """Для таблицы: форма множественного числа — «форма: текст | форма: текст», обычная строка — как есть."""
    return value if isinstance(value, str) else " | ".join(f"{form}: {text}" for form, text in value.items())


def placeholders(text: str) -> list[str]:
    return sorted(set(re.findall(r"\{(\w+)\}", text)))


def normalize_apostrophes(text: str) -> tuple[str, list[str]]:
    """Узбекская латиница: после o/g — ʻ (U+02BB: oʻzbek, gʻoya), между другими буквами — ʼ (U+02BC: maʼlumot). На клавиатуре набирают ' или ’ — приводим к правильным."""
    notes: list[str] = []

    def fix(m: re.Match) -> str:
        before, mark = m.group(1), m.group(2)
        right = "\u02BB" if before in "oOgG" else "\u02BC"
        if mark != right:
            notes.append(f"{before}{mark} → {before}{right}")
        return before + right

    return re.sub(r"([A-Za-z])(['\u2019\u2018`\u00B4\u02B9\u02BB\u02BC])(?=[A-Za-z])", fix, text), notes


def validate_correction(key: str, current, reference_ru, correction: str) -> tuple[str, list[str], list[str]]:
    """Проверяет одну правку. Возвращает (текст для записи, замечания-уведомления, ОШИБКИ). Ошибки — правку не вносим."""
    errors: list[str] = []
    text = correction.strip()
    text = re.sub(r"[ \t\u00A0]{2,}", " ", text)
    if not text:
        return "", [], ["пусто"]
    if re.search(r"[\u0400-\u04FF]", text):
        errors.append("есть кириллица (узбекский здесь — латиницей)")
    if re.search(r"[\u0000-\u001F\u2028\u2029]", text):
        errors.append("в тексте перенос строки или управляющий знак")
    ref = reference_ru if isinstance(reference_ru, str) else reference_ru.get("other", "")
    need = placeholders(ref)
    if placeholders(text) != need:
        errors.append(f"подстановки не совпадают: нужны {need or 'никаких'}, в правке {placeholders(text) or 'никаких'} (слова в фигурных скобках не переводят и не меняют)")
    if isinstance(current, dict) and "{n}" not in text:
        errors.append("в тексте со счётом должно быть {n}")
    text, notes = normalize_apostrophes(text)
    cur = current if isinstance(current, str) else current.get("other", "")
    if len(text) > 2.2 * len(cur) + 10:
        notes.append(f"правка намного длиннее прежнего ({len(text)} против {len(cur)} знаков): проверьте, помещается ли в кнопку или строку")
    return text, notes, errors


def apply_corrections(source: str, corrections: dict[str, str], reference_ru: dict) -> tuple[str, list[dict], dict[str, list[str]]]:
    """Вносит правки в текст i18n.uz.js. Возвращает (новый текст, список изменений, ошибки по ключам). При любой ошибке текст не меняется вовсе."""
    current = parse_dictionary(source)
    errors: dict[str, list[str]] = {}
    changes: list[dict] = []
    clean: dict[str, str] = {}
    for key, raw in corrections.items():
        if key not in current:
            errors[key] = ["такого ключа нет в словаре (ключ в таблице менять нельзя)"]
            continue
        text, notes, errs = validate_correction(key, current[key], reference_ru[key], raw)
        if errs:
            errors[key] = errs
            continue
        old_text = current[key] if isinstance(current[key], str) else current[key].get("other", "")
        if text == old_text:
            continue                                            # правка совпала с тем, что уже есть
        clean[key] = text
        changes.append({"key": key, "old": old_text, "new": text, "notes": notes})
    if errors:
        return source, [], errors

    lines = source.split("\n")
    for key, text in clean.items():
        for i, line in enumerate(lines):
            m = _KEY_LINE.match(line)
            if not m or m.group(2) != key:
                continue
            encoded = json.dumps(text, ensure_ascii=False)
            if m.group(4).startswith("{"):
                new_value, n = re.subn(r'((?:"other"|other)\s*:\s*)"(?:[^"\\]|\\.)*"', lambda x: x.group(1) + encoded, m.group(4), count=1)
                if n != 1:
                    raise ValueError(f"{key}: не нашёл форму other в строке словаря")
            else:
                new_value = encoded
            lines[i] = f"{m.group(1)}\"{key}\"{m.group(3)}{new_value}{m.group(5)}"
            break
        else:
            raise ValueError(f"{key}: не нашёл строку в словаре")
    result = "\n".join(lines)

    # самопроверка: ключи те же, изменились только исправленные значения
    after, before = parse_dictionary(result), current
    if set(after) != set(before):
        raise ValueError("после правки набор ключей изменился — запись отменена")
    for key in before:
        expected = before[key]
        if key in clean:
            expected = clean[key] if isinstance(expected, str) else {**expected, "other": clean[key]}
        if after[key] != expected:
            raise ValueError(f"{key}: после правки значение не то, что ожидалось — запись отменена")
    return result, changes, {}


# ------------------------------------------------------------------ где виден текст (чистая логика)
def usage_map(files: dict[str, str], keys: list[str]) -> dict[str, list[str]]:
    """key -> имена экранов. files: имя файла -> его текст. Находит data-i18n="…", строки "ключ" в коде и динамические ключи вида "префикс." + значение."""
    found: dict[str, set[str]] = {k: set() for k in keys}
    for name, text in files.items():
        screen = SCREENS.get(name)
        if not screen:
            continue
        literal = set(re.findall(r'data-i18n="([\w.]+)"', text)) | set(re.findall(r'"([a-z_]+\.[\w.]+)"', text))
        for pairs in re.findall(r'data-i18n-attr="([^"]+)"', text):                       # «атрибут:ключ;атрибут:ключ»
            literal |= {pair.split(":", 1)[1].strip() for pair in pairs.split(";") if ":" in pair}
        prefixes = set(re.findall(r'"([a-z_]+(?:\.[a-z_]+)*\.)"', text))                  # «theme.» + режим, а также префикс, переданный аргументом
        for key in keys:
            if key in literal or any(key.startswith(p) for p in prefixes):
                found[key].add(screen)
    return {k: sorted(v) for k, v in found.items()}


def read_frontend_texts() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(FRONT.iterdir()) if p.suffix in {".html", ".js"} and p.name in SCREENS}


# ------------------------------------------------------------------ таблица Excel (нужен openpyxl)
def _need_openpyxl():
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        raise SystemExit("Нужна библиотека openpyxl: pip install openpyxl")


def export_workbook(path: pathlib.Path) -> int:
    _need_openpyxl()
    from openpyxl import Workbook
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    dicts = {lang: parse_dictionary((FRONT / f"i18n.{lang}.js").read_text(encoding="utf-8")) for lang in ("ru", "uz", "en")}
    keys = list(dicts["uz"])
    usage = usage_map(read_frontend_texts(), keys)
    order = sorted(range(len(keys)), key=lambda i: (", ".join(usage[keys[i]]) or "яя", i))       # по экранам, внутри — как в словаре
    last = len(keys) + 1

    wb = Workbook()
    help_ws = wb.active
    help_ws.title = SHEET_HELP
    ws = wb.create_sheet(SHEET_TEXTS)
    font, bold = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True)
    head_fill, input_fill = PatternFill("solid", fgColor="D9E2EC"), PatternFill("solid", fgColor="FFF4C2")
    thin = Side(style="thin", color="C9D1D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for c, name in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=c, value=name)
        cell.font, cell.fill, cell.border = bold, input_fill if name in ("Исправление", "Комментарий") else head_fill, border
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for r, i in enumerate(order, 2):
        key = keys[i]
        row = [key, ", ".join(usage[key]) or "сейчас нигде не показывается — можно пропустить", ", ".join("{" + p + "}" for p in placeholders(flatten(dicts["ru"][key]))) or "—",
               flatten(dicts["ru"][key]), flatten(dicts["en"][key]), flatten(dicts["uz"][key]), f"=LEN(F{r})", None, None]
        for c, value in enumerate(row, 1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font, cell.border = font, border
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if c in (8, 9):
                cell.fill = input_fill
    for letter, width in zip("ABCDEFGHI", (26, 24, 14, 38, 38, 42, 9, 42, 30)):
        ws.column_dimensions[letter].width = width
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{last}"
    ws.conditional_formatting.add(f"A2:G{last}", FormulaRule(formula=['$H2<>""'], fill=PatternFill("solid", bgColor="DFF2E1")))

    help_ws.column_dimensions["A"].width = 30
    help_ws.column_dimensions["B"].width = 100
    lines = [
        ("Проверка узбекских текстов интерфейса", None),
        ("Что это", "Это тексты программы для учёта и поиска товаров. Узбекский вариант написан как черновик: нужен ваш взгляд носителя языка. Менять код и открывать программу не нужно."),
        ("Что делать", "Откройте лист «Тексты». В колонке «Oʻzbekcha сейчас» — текущий перевод. Если всё верно — оставьте строку как есть. Если нужно исправить — впишите правильный вариант в жёлтую колонку «Исправление»."),
        ("Если сомневаетесь", "Напишите, что смутило, в колонку «Комментарий» (можно и без исправления). Если не читаете по-русски — ориентируйтесь на колонку English."),
        ("Правила", "1) Слова в фигурных скобках — {name}, {n}, {code} — не переводить и не менять: программа сама подставит имя, число или номер. Они перечислены в колонке «Подстановки»."),
        ("", "2) Пишите латиницей. Апостроф можно набирать обычным ' — при внесении правок программа сама поставит правильные знаки (oʻ, gʻ, maʼlumot)."),
        ("", "3) Кнопки и подписи должны быть короткими: сравните «Знаков сейчас» с вашей правкой. Очень длинная правка может не поместиться на телефоне."),
        ("", "4) Обращение к человеку — вежливое «Siz» (так сейчас). Если у вас принято иначе — напишите в комментарии."),
        ("", "5) Колонку «Ключ» менять нельзя: по ней правка находит своё место."),
        ("Где виден текст", "Колонка «Где виден» показывает экран: «Узнать товар», «Товары», «Настройки» и т. д. Строки отсортированы по экранам."),
        ("Пример (не вносите в таблицу)", "Ключ: example.save  |  Русский: «Сохранить»  |  сейчас: «Saqlash»  |  Исправление: (пусто — всё верно)  или  «Saqlab qoʻyish»  |  Комментарий: «так говорят чаще»"),
        ("Сколько строк", f'=COUNTA({SHEET_TEXTS}!A2:A{last})'),
        ("Заполнено исправлений", f'=COUNTA({SHEET_TEXTS}!H2:H{last})'),
        ("Заполнено комментариев", f'=COUNTA({SHEET_TEXTS}!I2:I{last})'),
        ("Готово — что дальше", "Сохраните файл и верните его тому, кто его прислал. Спасибо!"),
    ]
    for r, (a, b) in enumerate(lines, 1):
        ca, cb = help_ws.cell(row=r, column=1, value=a or None), help_ws.cell(row=r, column=2, value=b)
        ca.font = Font(name="Arial", size=14 if r == 1 else 10, bold=True)
        cb.font = font
        for cell in (ca, cb):
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(path)
    return len(keys)


def read_corrections(path: pathlib.Path) -> tuple[dict[str, str], dict[str, str]]:
    _need_openpyxl()
    from openpyxl import load_workbook

    ws = load_workbook(path, data_only=True)[SHEET_TEXTS]
    header = {str(c.value).strip(): i for i, c in enumerate(ws[1]) if c.value is not None}
    for need in ("Ключ", "Исправление", "Комментарий"):
        if need not in header:
            raise SystemExit(f"В листе «{SHEET_TEXTS}» нет колонки «{need}»: таблица изменена или это не та таблица.")
    corrections: dict[str, str] = {}
    comments: dict[str, str] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        key = row[header["Ключ"]]
        if not key:
            continue
        key = str(key).strip()
        fix, note = row[header["Исправление"]], row[header["Комментарий"]]
        if fix is not None and str(fix).strip():
            corrections[key] = str(fix)
        if note is not None and str(note).strip():
            comments[key] = str(note).strip()
    return corrections, comments


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export"); e.add_argument("file")
    i = sub.add_parser("import"); i.add_argument("file"); i.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "export":
        n = export_workbook(pathlib.Path(args.file))
        print(f"Готово: {n} текстов выгружено в {args.file}. Отдайте файл носителю языка.")
        return 0
    corrections, comments = read_corrections(pathlib.Path(args.file))
    ru = parse_dictionary((FRONT / "i18n.ru.js").read_text(encoding="utf-8"))
    source = UZ_FILE.read_text(encoding="utf-8")
    new_source, changes, errors = apply_corrections(source, corrections, ru)
    print(f"Исправлений в таблице: {len(corrections)}; комментариев: {len(comments)}.")
    for key, errs in errors.items():
        for err in errs:
            print(f"  ОШИБКА  {key}: {err}")
    for ch in changes:
        print(f"  изменить {ch['key']}: «{ch['old']}» → «{ch['new']}»")
        for note in ch["notes"]:
            print(f"           замечание: {note}")
    for key, note in comments.items():
        print(f"  комментарий к {key}: {note}")
    if errors:
        print("\nНичего не записано: исправьте ошибки в таблице (или попросите проверяющего) и запустите снова.")
        return 2
    if not changes:
        print("\nИзменений в тексты вносить не нужно.")
        return 0
    if not args.apply:
        print(f"\nЭто проверка: файл не изменён. Для записи добавьте --apply ({len(changes)} правок).")
        return 0
    shutil.copy2(UZ_FILE, str(UZ_FILE) + ".before_review")
    UZ_FILE.write_text(new_source, encoding="utf-8", newline="")
    print(f"\nЗаписано: {len(changes)} правок в {UZ_FILE.name} (прежний файл — {UZ_FILE.name}.before_review). Теперь: python tools/run_js_tests.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
