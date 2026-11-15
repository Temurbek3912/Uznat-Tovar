"""
Статические проверки фронтенда (frontend/*.html, ui.css, shell.js) — без браузера и без fastapi.

Зачем они нужны. JS-тесты страниц (tools/*_smoke_test.js) работают с ПОДДЕЛЬНЫМ DOM, где любой id «существует» сам собой: потеряв при
перестройке разметки нужный скрипту элемент, они бы этого не заметили, а в браузере страница упала бы на `null.addEventListener`.
Здесь это проверяется по настоящей разметке. Заодно — единый вид (tokens.css + ui.css), одно меню (shell.js) и ОДНА страница входа
(login.html) на всех страницах сотрудников, и отсутствие внешних ресурсов (в магазине может не быть интернета).

Редизайн 2026-11-15 (владелец: «слишком длинный пароль, всё непонятно»): прежние главная (home.js, app.css, nav.js) и своё меню
управления убраны; вход — только на login.html (на этом компьютере ПИН, с другого устройства — логин и пароль).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

# Страницы сотрудников: общий вид, общее меню (shell.js), вход — на login.html. module — логика страницы, start — имя модуля для boot.js.
STAFF_PAGES = {"identify.html": {"page_id": "identify", "module": "identify.js", "start": "Identify"},
               "products.html": {"page_id": "products", "module": "products.js", "start": "Products"},
               "settings.html": {"page_id": "settings", "module": "settings.js", "start": "Settings"},
               "admin.html": {"page_id": "admin", "module": "admin.js", "start": None}}      # admin.js запускается сам
# Вход и главная (главная только переадресует): без меню.
ENTRY_PAGES = {"login.html": {"module": "login.js", "start": "Login"}, "index.html": {"module": "index.js", "start": "Home"}}
ALL_PAGES = [*STAFF_PAGES, *ENTRY_PAGES]
OWN_SCRIPTS = {"theme.js", "theme-apply.js", "i18n.js", "i18n.ru.js", "i18n.uz.js", "i18n.uz-cyrl.js", "i18n.en.js", "icons.js", "auth.js",
               "ticket.js", "boot.js", "catalog.js", "productlist.js", "shell.js", "identify.js", "products.js", "settings.js", "admin.js",
               "login.js", "index.js"}
SCRIPT_ORDER = [("i18n.js", "i18n.ru.js"), ("i18n.js", "i18n.uz.js"), ("i18n.js", "i18n.uz-cyrl.js"), ("i18n.js", "i18n.en.js"),
                ("i18n.en.js", "auth.js"), ("icons.js", "ticket.js"), ("ticket.js", "productlist.js"), ("catalog.js", "productlist.js"),
                ("auth.js", "shell.js"), ("auth.js", "login.js")]

# Как скрипты страниц обращаются к элементам по id (хелперы у страниц свои: $, show, setMsg, updateCount, setStatus ...)
_ID_CALLS = [
    r'\$\("([A-Za-z][\w-]*)"\)',
    r'getElementById\("([A-Za-z][\w-]*)"\)',
    r'\bshow\("([A-Za-z][\w-]*)"',
    r'\bsetMsg\("([A-Za-z][\w-]*)"',
    r'\bsetStatus\("([A-Za-z][\w-]*)"',
    r'\bupdateCount\("([A-Za-z][\w-]*)",\s*"([A-Za-z][\w-]*)"',
]


def _html(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def _module(name: str) -> str:
    spec = STAFF_PAGES.get(name) or ENTRY_PAGES[name]
    return (FRONTEND / spec["module"]).read_text(encoding="utf-8")


def _inline_scripts(html: str) -> list[str]:
    """Встроенные скрипты (без src). С 2026-11-07 их нет ни на одной странице: CSP script-src 'self' их запрещает."""
    return re.findall(r"<script>(.*?)</script>", html, re.S)


def _scripts(html: str) -> list[str]:
    return re.findall(r'<script\s+src="([^"]+)"', html)


def _ids_used_by_script(script: str) -> set[str]:
    used: set[str] = set()
    for pattern in _ID_CALLS:
        for match in re.finditer(pattern, script):
            used.update(group for group in match.groups() if group)
    for body in re.findall(r'for \(const id of \[([^\]]+)\]\)', script):
        used.update(re.findall(r'"([A-Za-z][\w-]*)"', body))
    return used


def _ids_in_markup(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


def test_the_helper_finds_every_way_pages_address_elements():
    script = '$("a"); document.getElementById("b"); show("c", true); setMsg("d", "x"); setStatus("e"); updateCount("f", "g"); for (const id of ["h", "i"]) $(id);'
    assert _ids_used_by_script(script) == set("abcdefghi")
    assert _ids_in_markup('<div id="x"></div><input id="y" type="text">') == {"x", "y"}
    assert _inline_scripts('<script src="a.js"></script><script>var a;</script>') == ["var a;"]


def test_every_element_id_a_page_script_uses_exists_in_that_pages_markup():
    problems = []
    for name in ALL_PAGES:
        markup = re.sub(r"<script>.*?</script>", "", _html(name), flags=re.S)
        missing = sorted(_ids_used_by_script(_module(name)) - _ids_in_markup(markup))
        if missing:
            problems.append(f"{name}: в разметке нет id {missing}")
    assert not problems, "\n".join(problems)


def test_element_ids_are_unique_within_a_page():
    for name in ALL_PAGES:
        ids = re.findall(r'\bid="([^"]+)"', re.sub(r"<script>.*?</script>", "", _html(name), flags=re.S))
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        assert not duplicates, f"{name}: повторяются id {duplicates}"


def test_no_page_has_inline_scripts_or_handlers():
    """CSP script-src 'self' (app/core/security_headers.py) запрещает встроенные скрипты и обработчики on…="…"."""
    for name in ALL_PAGES:
        html = _html(name)
        assert _inline_scripts(html) == [], f"{name}: встроенный <script>"
        assert not re.search(r"\son[a-z]+\s*=", html, re.I), f"{name}: обработчик события в атрибуте"
        assert "javascript:" not in html.lower(), name


def test_staff_pages_share_one_look_one_menu_and_start_their_module():
    """Единый вид и одно меню: tokens.css + ui.css, <nav class="rail" data-shell> (рисует shell.js), без прежних app.css/nav.js."""
    for name, spec in STAFF_PAGES.items():
        html = _html(name)
        assert '<link rel="stylesheet" href="tokens.css">' in html and '<link rel="stylesheet" href="ui.css">' in html, name
        assert html.index("tokens.css") < html.index("ui.css"), f"{name}: токены раньше компонентов"
        assert "app.css" not in html and "nav.js" not in html and "appNav" not in html, f"{name}: старая оболочка"
        assert f'<body data-page="{spec["page_id"]}">' in html, name
        assert re.search(r'<nav class="rail" id="rail" data-shell aria-label="[^"]+"></nav>', html), f"{name}: меню — общее (пустое, его рисует shell.js)"
        assert '<script src="theme.js"></script>\n<script src="theme-apply.js"></script>' in html, f"{name}: тема сразу после theme.js"
        scripts = _scripts(html)
        for first, second in SCRIPT_ORDER:
            if first in scripts and second in scripts:
                assert scripts.index(first) < scripts.index(second), f"{name}: {first} должен идти раньше {second}"
        assert spec["module"] in scripts and "shell.js" in scripts, name
        assert scripts.index("auth.js") < scripts.index(spec["module"]), name
        assert '<script src="boot.js" data-start="Shell"></script>' in html, f"{name}: меню запускается через boot.js"
        if spec["start"]:
            assert f'<script src="boot.js" data-start="{spec["start"]}"></script>' in html, f"{name}: запуск модуля через boot.js"
        assert 'lang="ru"' in html and 'name="viewport"' in html and "width=device-width" in html, name


def test_there_is_exactly_one_sign_in_form_and_it_lives_on_login_html():
    """Владелец: «четыре разных входа — непонятно». Логин и пароль спрашивает только login.html; остальные страницы отправляют туда."""
    for name in ALL_PAGES:
        html = _html(name)
        has_form = 'id="loginUser"' in html or 'autocomplete="username"' in html
        assert has_form == (name == "login.html"), f"{name}: форма логина и пароля должна быть только на login.html"
    login = _html("login.html")
    assert login.count('autocomplete="current-password"') == 1 and 'id="pinInput"' in login and 'id="setupPin"' in login
    assert 'inputmode="numeric"' in login, "ПИН — цифровая клавиатура на телефоне и планшете"
    for name in ("identify.js", "products.js", "settings.js", "admin.js"):
        source = (FRONTEND / name).read_text(encoding="utf-8")
        assert "goLogin(" in source, f"{name}: без сеанса — на общую страницу входа"
        assert "/auth/login" not in source and "auth.login(" not in source and "auth().login(" not in source, f"{name}: свой вход паролем"


def test_settings_page_works_without_signing_in_and_offers_the_pin():
    html = _html("settings.html")
    assert '<form id="loginPanel" class="settings__form" hidden>' in html, "кнопка «Войти» ведёт на login.html"
    assert re.search(r'<section class="settings">', html) and not re.search(r'<section class="settings"[^>]*hidden', html)
    assert 'id="accountInfo"' in html and 'id="logoutBtn"' in html
    assert 'id="pinGroup"' in html and 'id="pinForm"' in html
    # «новый пароль» — только в формах смены пароля; «новый ПИН» — в форме ПИН-кода
    forms = re.findall(r'<form id="(?:passwordForm|initialPanel|pinForm)"[\s\S]*?</form>', html)
    assert sum(f.count('autocomplete="new-password"') for f in forms) == html.count('autocomplete="new-password"')


def test_every_class_on_the_pages_is_styled():
    css = (FRONTEND / "ui.css").read_text(encoding="utf-8") + (FRONTEND / "admin.css").read_text(encoding="utf-8") \
        + (FRONTEND / "shop.css").read_text(encoding="utf-8")
    for name in ALL_PAGES:
        classes = {c for m in re.findall(r'\bclass="([^"]+)"', _html(name)) for c in m.split()}
        missing = sorted(c for c in classes if not re.search(r"\." + re.escape(c) + r"(?![\w-])", css))
        assert not missing, f"{name}: нет правил для классов {missing}"
    shell = (FRONTEND / "shell.js").read_text(encoding="utf-8")
    built = {c for m in re.findall(r'el\("[a-z]+", "([^"]+)"', shell) for c in m.split()} | {"rail__item--wide"}
    missing = sorted(c for c in built if not re.search(r"\." + re.escape(c) + r"(?![\w-])", css))
    assert not missing, f"меню: нет правил для классов {missing}"


def test_menu_is_a_wide_sidebar_on_big_screens_and_a_bottom_bar_on_phones():
    css = (FRONTEND / "ui.css").read_text(encoding="utf-8")
    wide = css[css.index("@media (min-width: 1100px)"):]
    assert re.search(r"grid-template-columns:\s*var\(--rail-w-wide\)", wide), "широкий экран: колонка меню с подписями"
    assert re.search(r"\.rail__item\s*\{[^}]*flex-direction:\s*row", wide), "значок и подпись — в строку"
    phone = css[css.index("@media (max-width: 899px)"):]
    assert re.search(r"\.rail\s*\{[^}]*position:\s*fixed[^}]*bottom:\s*0", phone), "телефон: меню внизу"
    assert re.search(r"\.rail__item--wide[^{]*\{\s*display:\s*none", css), "на телефоне — только главные пункты"


def test_token_and_login_request_live_only_in_the_shared_auth_module():
    """Токен, адрес сервера и запросы входа — только в auth.js; страницы им пользуются. Тест не даёт дублю вернуться."""
    for module in ("identify.js", "products.js", "settings.js", "login.js", "shell.js", "index.js"):
        source = (FRONTEND / module).read_text(encoding="utf-8")
        for trace in ("/auth/login", "/auth/logout", "/auth/pin", "Bearer ", "api_base"):
            assert trace not in source, f"{module}: «{trace}» — это работа auth.js, не страницы"
    for module in ("identify.js", "products.js", "settings.js", "login.js"):
        assert "sessionStorage" not in (FRONTEND / module).read_text(encoding="utf-8"), module
    auth = (FRONTEND / "auth.js").read_text(encoding="utf-8")
    assert "sessionStorage" in auth and "/auth/login" in auth and "/auth/logout" in auth and "/auth/me" in auth and "/auth/pin" in auth
    assert "localStorage" not in auth, "токен не хранится в localStorage: он пережил бы закрытие вкладки"


def test_the_interface_has_no_speech_output_anywhere():
    """Озвучку убрали из проекта полностью (решение владельца, 2026-10-30). Тест не даёт ей вернуться по недосмотру."""
    assert not (FRONTEND / "voice.js").exists(), "frontend/voice.js должен быть удалён"
    traces = re.compile(r"\bVoice\b|speechSynthesis|SpeechSynthesis|voice\.js|speechSummary|nextSpeech")
    offenders = []
    for path in sorted(FRONTEND.iterdir()):
        if path.suffix in {".html", ".js", ".css", ".webmanifest"}:
            hit = traces.search(path.read_text(encoding="utf-8"))
            if hit:
                offenders.append(f"{path.name}: «{hit.group(0)}»")
    assert not offenders, "следы озвучки: " + "; ".join(offenders)
    key = re.compile(r'"((?:voice|speech)\.[\w.]+|identify\.speak\w*|settings\.voice_hint)"\s*:')
    for lang in ("ru", "uz", "en"):
        leftovers = key.findall((FRONTEND / f"i18n.{lang}.js").read_text(encoding="utf-8"))
        assert not leftovers, f"i18n.{lang}.js: остались тексты озвучки {leftovers}"


def test_old_shell_files_are_gone():
    for gone in ("nav.js", "home.js", "home.css", "app.css"):
        assert not (FRONTEND / gone).exists(), f"frontend/{gone} — прежняя оболочка, удалена при редизайне 2026-11-15"


def test_pages_load_nothing_from_other_sites_stores_can_be_offline():
    external_attr = re.compile(r'(?:href|src)="(?:https?:)?//', re.I)
    for name in [*ALL_PAGES, "tokens.css", "ui.css", "admin.css", *sorted(OWN_SCRIPTS)]:
        text = _html(name)
        assert not external_attr.search(text), f"{name}: внешний ресурс в href/src"
        assert "fonts.googleapis" not in text and "fonts.gstatic" not in text, f"{name}: шрифты с чужого сайта"
        assert "@import" not in text, name
        assert not re.search(r"url\(\s*['\"]?https?:", text), f"{name}: внешний url() в CSS"
    for name in ALL_PAGES:
        sources = _scripts(_html(name))
        assert set(sources) <= OWN_SCRIPTS, f"{name}: подключены скрипты {sources}"
        assert all((FRONTEND / src).is_file() for src in sources), f"{name}: нет файла среди {sources}"


def test_buttons_and_fields_are_finger_sized():
    css = (FRONTEND / "ui.css").read_text(encoding="utf-8")
    assert re.search(r"\.btn\s*\{[^}]*min-height:\s*var\(--hit\)", css)
    assert re.search(r"\.pin-input\s*\{[^}]*min-height:\s*calc\(var\(--hit\)", css), "поле ПИН — крупное"
    assert re.search(r"--hit:\s*44px", (FRONTEND / "tokens.css").read_text(encoding="utf-8"))
