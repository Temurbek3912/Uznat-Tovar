"""
Статические проверки фронтенда (frontend/*.html, app.css, nav.js) — без браузера и без fastapi.

Зачем они нужны. JS-тесты страниц (tools/*_smoke_test.js) работают с ПОДДЕЛЬНЫМ DOM, где любой id «существует» сам собой: потеряв при
перестройке разметки нужный скрипту элемент, они бы этого не заметили, а в браузере страница упала бы на `null.addEventListener`.
Здесь это проверяется по настоящей разметке. Заодно — единое оформление и меню на всех страницах и отсутствие внешних ресурсов
(в магазине может не быть интернета: страница, ждущая шрифт с чужого сайта, зависла бы на таймауте).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

# Страницы с общей оболочкой (меню nav.js + app.css). У admin.html своё боковое меню — оно проверяется отдельно.
SHELL_PAGES = {"index.html": "home"}
# Страницы нового вида (tokens.css + ui.css, свои модули вместо nav.js/app.css; логика страницы лежит в модуле .js, а не в самой странице).
# Переводятся по одной (этап D плана): «Узнать товар» и «Товары».
# login: "page" — без входа страница не открывается, форма входа — отдельная карточка на всю страницу (<form id="loginPanel" class="login">);
#        "inline" — страница работает без входа (настройки хранятся на устройстве), форма входа — одна из групп на ней (class="settings__form").
#        Токен и запрос входа ведёт общий модуль auth.js — он подключается на каждой из этих страниц до её модуля.
NEW_PAGES = {"identify.html": {"page_id": "identify", "module": "identify.js", "start": "Identify.start(window);", "login": "page"},
             "products.html": {"page_id": "products", "module": "products.js", "start": "Products.start(window);", "login": "page"},
             "settings.html": {"page_id": "settings", "module": "settings.js", "start": "Settings.start(window);", "login": "inline"}}
NEW_PAGE_SCRIPTS = {"theme.js", "i18n.js", "i18n.ru.js", "i18n.uz.js", "i18n.uz-cyrl.js", "i18n.en.js", "icons.js", "auth.js", "ticket.js", "theme-apply.js", "boot.js", "catalog.js", "productlist.js", "identify.js", "products.js", "settings.js"}
# «что раньше чего»: пара проверяется, только если на странице есть оба файла
NEW_PAGE_SCRIPT_ORDER = [("i18n.js", "i18n.ru.js"), ("i18n.js", "i18n.uz.js"), ("i18n.js", "i18n.uz-cyrl.js"), ("i18n.js", "i18n.en.js"), ("i18n.js", "icons.js"),
                         ("icons.js", "ticket.js"), ("ticket.js", "productlist.js"), ("catalog.js", "productlist.js"), ("icons.js", "productlist.js")]
ALL_PAGES = [*SHELL_PAGES, *NEW_PAGES, "admin.html"]

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


def _inline_scripts(html: str) -> list[str]:
    """Встроенные скрипты (без src). С 2026-11-07 их нет ни на одной странице: CSP script-src 'self' их запрещает."""
    return re.findall(r"<script>(.*?)</script>", html, re.S)


# Скрипт с логикой страницы (раньше был встроенным <script>; вынесен в файл ради CSP script-src 'self').
PAGE_SCRIPT = {"index.html": "home.js", "admin.html": "admin.js"}


def _page_scripts(name: str) -> list[str]:
    """Тексты скриптов, отвечающих за разметку страницы: файл логики старой страницы или модуль новой."""
    files = [PAGE_SCRIPT[name]] if name in PAGE_SCRIPT else [NEW_PAGES[name]["module"]] if name in NEW_PAGES else []
    return [(FRONTEND / f).read_text(encoding="utf-8") for f in files]


def _ids_used_by_script(script: str) -> set[str]:
    used: set[str] = set()
    for pattern in _ID_CALLS:
        for match in re.finditer(pattern, script):
            used.update(group for group in match.groups() if group)
    # for (const id of ["tileA", "tileB"]) $(id)... — перечисление id списком
    for body in re.findall(r'for \(const id of \[([^\]]+)\]\)', script):
        used.update(re.findall(r'"([A-Za-z][\w-]*)"', body))
    return used


def _ids_in_markup(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


def test_the_helper_finds_every_way_pages_address_elements():
    script = '$("a"); document.getElementById("b"); show("c", true); setMsg("d", "x"); setStatus("e"); updateCount("f", "g"); for (const id of ["h", "i"]) $(id);'
    assert _ids_used_by_script(script) == set("abcdefghi")
    assert _ids_in_markup('<div id="x"></div><input id="y" type="text">') == {"x", "y"}
    assert _inline_scripts('<script src="nav.js"></script><script>var a;</script>') == ["var a;"]


def test_every_element_id_a_page_script_uses_exists_in_that_pages_markup():
    problems = []
    for name in ALL_PAGES:
        html = _html(name)
        markup = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
        scripts = _page_scripts(name)
        assert scripts, f"{name}: нет скрипта страницы"
        used = set().union(*(_ids_used_by_script(s) for s in scripts))
        missing = sorted(used - _ids_in_markup(markup))
        if missing:
            problems.append(f"{name}: в разметке нет id {missing}")
    assert not problems, "\n".join(problems)


def test_element_ids_are_unique_within_a_page():
    for name in ALL_PAGES:
        markup = re.sub(r"<script>.*?</script>", "", _html(name), flags=re.S)
        ids = re.findall(r'\bid="([^"]+)"', markup)
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        assert not duplicates, f"{name}: повторяются id {duplicates}"


def test_no_page_has_inline_scripts_and_old_pages_load_their_logic_after_the_menu_script():
    """CSP script-src 'self' (app/core/security_headers.py) запрещает встроенные скрипты и обработчики on…="…"."""
    for name in ALL_PAGES:
        html = _html(name)
        assert _inline_scripts(html) == [], f"{name}: встроенный <script>"
        assert not re.search(r"\son[a-z]+\s*=", html, re.I), f"{name}: обработчик события в атрибуте"
        assert "javascript:" not in html.lower(), name
    for name in SHELL_PAGES:
        html = _html(name)
        assert html.index('<script src="nav.js"></script>') < html.index(f'<script src="{PAGE_SCRIPT[name]}"></script>'), name


def test_shell_pages_use_the_shared_stylesheet_menu_container_and_matching_page_id():
    nav_ids = set(re.findall(r'\{ id: "(\w+)"', (FRONTEND / "nav.js").read_text(encoding="utf-8")))
    for name, page_id in SHELL_PAGES.items():
        html = _html(name)
        assert '<link rel="stylesheet" href="app.css">' in html, name
        assert '<div id="appNav"></div>' in html, name
        assert f'<body data-page="{page_id}">' in html, name
        assert page_id in nav_ids, f"{name}: data-page «{page_id}» нет среди пунктов меню {sorted(nav_ids)}"
        assert '<main class="page">' in html, name


def test_admin_keeps_its_own_sidebar_and_links_to_home_with_plain_names():
    html = _html("admin.html")
    assert 'href="index.html"' in html
    for jargon in ("Дашборд OSA", "Ревью распознаваний", "Массовая настройка"):
        assert jargon not in html, jargon
    for plain in ("Узнать товар", "Добавить товары и фото"):
        assert plain in html, plain
    for gone in ("Результаты проверки", "Проверка товаров", "Снять полку", "Создать магазины и полки"):
        assert gone not in html, gone


def test_new_pages_use_tokens_and_components_and_only_start_their_module():
    """Страница нового вида: токены и компоненты, меню-колонка, без старых app.css/nav.js; своей логики в странице нет — только запуск модуля."""
    for name, spec in NEW_PAGES.items():
        html = _html(name)
        assert '<link rel="stylesheet" href="tokens.css">' in html and '<link rel="stylesheet" href="ui.css">' in html, name
        assert html.index("tokens.css") < html.index("ui.css"), f"{name}: токены раньше компонентов"
        assert "app.css" not in html and "nav.js" not in html and '<div id="appNav"></div>' not in html, f"{name}: старая оболочка"
        assert f'<body data-page="{spec["page_id"]}">' in html, name
        assert '<nav class="rail"' in html and '<main class="main">' in html, name
        assert _inline_scripts(html) == [], f"{name}: встроенный <script>"
        assert '<script src="theme.js"></script>\n<script src="theme-apply.js"></script>' in html, f"{name}: тема сразу после theme.js"
        module_name = spec["start"].split(".")[0]
        assert f'<script src="boot.js" data-start="{module_name}"></script>' in html, f"{name}: запуск модуля через boot.js"
        scripts = re.findall(r'<script\s+src="([^"]+)"', html)
        for first, second in NEW_PAGE_SCRIPT_ORDER:
            if first in scripts and second in scripts:
                assert scripts.index(first) < scripts.index(second), f"{name}: {first} должен идти раньше {second}"
        assert scripts[-2:] == [spec["module"], "boot.js"], f"{name}: модуль страницы {spec['module']} и затем boot.js — последними"
        assert {"i18n.js", "icons.js", "auth.js"} <= set(scripts), f"{name}: нужны язык, значки и общий вход"
        assert scripts.index("auth.js") < scripts.index(spec["module"]), f"{name}: auth.js должен идти раньше {spec['module']}"
        assert 'lang="ru"' in html and 'name="viewport"' in html, name


def test_every_element_id_a_new_page_module_uses_exists_in_that_pages_markup():
    """JS-тест страницы работает на настоящей разметке, но это же правило закреплено и здесь: потерянный id роняет страницу на null.addEventListener."""
    for name, spec in NEW_PAGES.items():
        markup = re.sub(r"<script>.*?</script>", "", _html(name), flags=re.S)
        used = _ids_used_by_script((FRONTEND / spec["module"]).read_text(encoding="utf-8"))
        assert used, f"{spec['module']}: ни одного id не найдено — хелпер поиска устарел?"
        missing = sorted(used - _ids_in_markup(markup))
        assert not missing, f"{name}: в разметке нет id {missing} (их использует {spec['module']})"


def test_new_page_login_form_and_notices_are_styled_in_ui_css():
    css = (FRONTEND / "ui.css").read_text(encoding="utf-8")
    for name, spec in NEW_PAGES.items():
        html = _html(name)
        if spec["login"] == "page":
            assert '<form id="loginPanel" class="login"' in html, name
        else:
            assert spec["login"] == "inline", name
            assert '<form id="loginPanel" class="settings__form" hidden>' in html, f"{name}: форма входа встроена в страницу и сначала скрыта"
            assert re.search(r'<section class="settings">', html) and not re.search(r'<section class="settings"[^>]*hidden', html), f"{name}: сама страница не должна ждать входа"
            assert 'id="accountInfo"' in html and 'id="logoutBtn"' in html, f"{name}: после входа — имя и «Выйти»"
        login_form = re.search(r'<form id="loginPanel"[\s\S]*?</form>', html).group(0)
        assert login_form.count('type="password"') == 1 and 'autocomplete="current-password"' in login_form, \
            f"{name}: в форме входа ровно одно поле пароля с подсказкой для менеджера паролей"
        # остальные поля пароля (подтверждение для второго фактора, документ 18) — тоже «текущий пароль»; «новый пароль» — только
        # в форме смены своего пароля (документ 05, §12.11)
        new_fields = html.count('autocomplete="new-password"')
        assert html.count('type="password"') == html.count('autocomplete="current-password"') + new_fields, name
        if new_fields:                         # смена своего пароля и установка вместо временного (документ 05, §12.3)
            forms = re.findall(r'<form id="(?:passwordForm|initialPanel)"[\s\S]*?</form>', html)
            assert sum(f.count('autocomplete="new-password"') for f in forms) == new_fields == 4, name
        classes = {c for m in re.findall(r'\bclass="([^"]+)"', html) for c in m.split()}
        missing = sorted(c for c in classes if not re.search(r"\." + re.escape(c) + r"(?![\w-])", css))
        assert not missing, f"{name}: нет правил в ui.css для классов {missing}"
    assert re.search(r"\.login \.btn\s*\{[^}]*width:\s*100%", css), "кнопка входа во всю ширину формы"
    assert re.search(r"\.settings__form\s*\{[^}]*flex-direction:\s*column", css), "встроенная форма входа — колонкой"


def test_token_and_login_request_live_only_in_the_shared_auth_module():
    """Раньше токен, адрес сервера и запрос входа были написаны заново в каждой странице. Теперь — только в auth.js; страницы им пользуются.
    Тест не даёт дублю вернуться (например, при переносе куска кода из старой копии)."""
    for module in ("identify.js", "products.js", "settings.js"):
        source = (FRONTEND / module).read_text(encoding="utf-8")
        for trace in ("sessionStorage", "shelf_audit_token", "/auth/login", "/auth/logout", "Bearer ", "api_base"):
            assert trace not in source, f"{module}: «{trace}» — это работа auth.js, не страницы"
    auth = (FRONTEND / "auth.js").read_text(encoding="utf-8")
    assert "sessionStorage" in auth and "/auth/login" in auth and "/auth/logout" in auth and "/auth/me" in auth
    assert "localStorage" not in auth, "токен не хранится в localStorage: он пережил бы закрытие вкладки"


def test_the_interface_has_no_speech_output_anywhere():
    """Озвучку убрали из проекта полностью (решение владельца, 2026-10-30): нет модуля голоса, обращений к синтезатору речи, кнопок и текстов про голос.
    Тест не даёт ей вернуться по недосмотру (например, из старой копии страницы)."""
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


def test_pages_load_nothing_from_other_sites_stores_can_be_offline():
    external_attr = re.compile(r'(?:href|src)="(?:https?:)?//', re.I)
    for name in [*ALL_PAGES, "app.css", "nav.js", "tokens.css", "ui.css", *sorted(NEW_PAGE_SCRIPTS)]:
        text = _html(name)
        assert not external_attr.search(text), f"{name}: внешний ресурс в href/src"
        assert "fonts.googleapis" not in text and "fonts.gstatic" not in text, f"{name}: шрифты с чужого сайта"
        assert "@import" not in text, name
        assert not re.search(r"url\(\s*['\"]?https?:", text), f"{name}: внешний url() в CSS"
    for name in ALL_PAGES:
        sources = re.findall(r'<script\s+src="([^"]+)"', _html(name))
        if name in NEW_PAGES:                                         # новые страницы: только свои файлы из списка (каждый проверен выше на чужие адреса)
            assert set(sources) <= NEW_PAGE_SCRIPTS and NEW_PAGES[name]["module"] in sources, f"{name}: подключены скрипты {sources}"
            assert all((FRONTEND / src).is_file() for src in sources), f"{name}: нет файла среди {sources}"
        else:
            expected = ["nav.js", PAGE_SCRIPT[name]] if name in SHELL_PAGES else [PAGE_SCRIPT[name]]   # admin.html — без общего меню
            if name in ("index.html", "admin.html"):                  # переведены: сначала словари (свои файлы)
                expected = ["i18n.js", "i18n.ru.js", "i18n.uz.js", "i18n.uz-cyrl.js", "i18n.en.js", "auth.js", *expected]   # + общий вход
            assert sources == expected, f"{name}: подключены скрипты {sources}"
            assert all((FRONTEND / src).is_file() for src in sources), f"{name}: нет файла среди {sources}"


def test_every_class_the_menu_builds_is_styled_in_app_css():
    css = (FRONTEND / "app.css").read_text(encoding="utf-8")
    nav = (FRONTEND / "nav.js").read_text(encoding="utf-8")
    built = set()
    for match in re.finditer(r'"class":\s*"([^"]+)"|class:\s*"([^"]+)"|"(shell-[\w-]+)"', nav):
        for group in match.groups():
            if group:
                built.update(group.replace("shell-link\" + (item.laptopOnly", "shell-link").split())
    built |= {"shell-link", "laptop-only", "ico", "no-links"}
    built.discard("lbl")                                       # «крючок» без собственных стилей — наследует шрифт ссылки
    built = {c for c in built if re.fullmatch(r"[\w-]+", c)}
    # Класс должен быть частью селектора, а не подстрокой другого (.shell-nav внутри .shell-navx): граница слова обязательна.
    missing = sorted(c for c in built if not re.search(rf"\.{re.escape(c)}(?![\w-])", css))
    assert not missing, f"в app.css нет стилей для классов меню: {missing}"


def _rule(css: str, selector: str) -> str:
    """Тело первого правила с точно таким селектором (например, '.shell-nav'); пусто, если правила нет."""
    match = re.search(rf"(?:^|[}}\s]){re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    return match.group(1) if match else ""


def _block(css: str, start: int) -> str:
    """Текст {…}-блока, открывающегося после позиции start (с учётом вложенных скобок)."""
    depth, i = 0, css.index("{", start)
    begin = i
    while i < len(css):
        depth += css[i] == "{"
        depth -= css[i] == "}"
        i += 1
        if depth == 0:
            return css[begin:i]
    raise AssertionError("не закрыта скобка медиа-запроса")


def test_block_helper_returns_only_the_matching_media_block():
    css = "a{x:1} @media (m) { .p { y:2 } .q { z:3 } } @media print { .r { w:4 } }"
    assert _block(css, css.index("@media (m)")) == "{ .p { y:2 } .q { z:3 } }"


def test_app_css_switches_between_phone_bar_and_laptop_sidebar_at_900px():
    css = (FRONTEND / "app.css").read_text(encoding="utf-8")
    assert "@media (min-width: 900px)" in css
    cut = css.index("@media (min-width: 900px)")
    base, laptop = css[:cut], _block(css, cut)           # именно блок ноутбучного медиа-запроса, а не «всё до конца файла»
    # телефон: панель разделов закреплена внизу экрана и не уходит под «челку»/жестовую полосу iPhone
    nav = _rule(base, ".shell-nav")
    assert re.search(r"position:\s*fixed", nav) and re.search(r"bottom:\s*0", nav), nav
    assert "env(safe-area-inset-bottom)" in nav, nav
    # не вошли (нет разделов): на телефоне панели нет совсем, на ноутбуке остаётся колонка с названием
    assert re.search(r"display:\s*none", _rule(base, ".shell-nav.no-links"))
    assert re.search(r"display:\s*flex", _rule(laptop, ".shell-nav.no-links"))
    # …а содержимое страницы не прячется за этой панелью
    assert "env(safe-area-inset-bottom)" in _rule(base, ".page")
    # ноутбук: боковое меню слева шириной 268px, верхняя полоса телефона не нужна, панель перестаёт быть «прибитой» к низу
    assert re.search(r"grid-template-columns:\s*268px", laptop)
    assert re.search(r"\.shell-top\s*\{\s*display:\s*none", laptop)
    assert re.search(r"\.shell-nav\s*\{[^}]*position:\s*sticky", laptop)


def test_phone_bar_scrolls_sideways_when_there_are_more_sections_than_fit():
    # Пунктов меню у администратора больше, чем помещается в ширину телефона: панель прокручивается, а не сжимает подписи до нечитаемых.
    css = (FRONTEND / "app.css").read_text(encoding="utf-8")
    cut = css.index("@media (min-width: 900px)")
    base, laptop = css[:cut], _block(css, cut)
    assert re.search(r"overflow-x:\s*auto", _rule(base, ".shell-nav")), _rule(base, ".shell-nav")
    basis = re.search(r"flex:\s*1\s+0\s+(\d+)px", _rule(base, ".shell-link"))
    assert basis and int(basis.group(1)) >= 76, _rule(base, ".shell-link")      # подпись «Результаты» (10 букв) должна помещаться без наезда на соседнюю
    assert re.search(r"overflow-x:\s*visible", laptop)                    # на ноутбуке колонка слева, прокрутки вбок нет


def test_page_content_is_not_limited_to_a_phone_sized_column():
    # Исходная жалоба: на ноутбуке страницы были узкой полоской как для телефона (main { max-width:520px }). Это не должно вернуться:
    # узкими могут быть только карточка входа и счётчик полок, остальное — не уже 700px.
    narrow_allowed = (".login-card", ".stepper")
    for name in SHELL_PAGES:
        # 2026-11-08: встроенный <style> вынесен в файл страницы (CSP style-src 'self'): index.html -> home.css
        sheet = {"index.html": "home.css", "admin.html": "admin.css"}.get(name)
        style = (FRONTEND / sheet).read_text(encoding="utf-8") if sheet else re.search(r"<style>(.*?)</style>", _html(name), re.S).group(1)
        style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)                # комментарии — не селекторы
        for selector, width in re.findall(r"([^{}]+)\{[^{}]*?\bmax-width:\s*(\d+)px", style):
            assert int(width) >= 700 or selector.strip() in narrow_allowed, f"{name}: «{selector.strip()}» ограничен {width}px"
    css = (FRONTEND / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.page\s*\{[^}]*max-width:\s*1[12]\d\dpx", css), ".page должна быть шириной под ноутбук (1100–1299px)"


def test_phone_friendly_basics_viewport_and_touch_sized_controls():
    css = (FRONTEND / "app.css").read_text(encoding="utf-8")
    for name in ALL_PAGES:
        assert 'name="viewport"' in _html(name) and "width=device-width" in _html(name), name
    assert "min-height:46px" in css and "min-height:50px" in css       # поля и кнопки не мельче пальца
    assert "font-size:16px" in css                                      # 16px у полей: iOS не приближает страницу при вводе


def test_buttons_in_the_login_card_fill_the_card_like_the_fields():
    css = (FRONTEND / "app.css").read_text(encoding="utf-8")
    body = _rule(css, ".login-card button, .login-card .btn")
    assert re.search(r"width:\s*100%", body), body
    for name in SHELL_PAGES:                                        # и у каждой страницы форма входа лежит в такой карточке
        assert 'class="card login-card"' in _html(name) or 'class="card login-card' in _html(name), name
