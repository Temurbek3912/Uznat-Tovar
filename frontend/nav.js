/* Меню приложения: боковая панель на ноутбуке, нижняя панель разделов на телефоне (какая именно — решает app.css).
   Подключается на каждой странице: <div id="appNav"></div> + <script src="nav.js"></script>, <body data-page="...">.
   Чистая логика (какие пункты кому показывать) вынесена отдельно и проверяется tools/nav_smoke_test.js.
   Данные в разметку не подставляются строкой: всё строится через createElement/textContent (токен лежит в sessionStorage). */
(function (root) {
  "use strict";

  var TOKEN_KEY = "shelf_audit_token", ROLE_KEY = "shelf_audit_role", PERMS_KEY = "shelf_audit_perms";

  // Названия — простыми словами, без «OSA», «ревью» и «дашборда». needs — право доступа (имена как в app/core/security.py).
  var ITEMS = [
    { id: "home",      href: "index.html",     icon: "🏠", label: "Главная",            needs: null },
    { id: "identify",  href: "identify.html",  icon: "🔎", label: "Узнать товар",       needs: "catalog:read" },
    { id: "products",  href: "products.html",  icon: "🛒", label: "Товары",             needs: "catalog:write" },
    { id: "admin",     href: "admin.html",     icon: "⚙️", label: "Управление",         needs: "store:manage" }
  ];

  // Вошли, но права ещё не получены (идёт запрос /auth/me): показываем общие разделы, остальные появятся, когда права придут.
  // Не вошли — не показываем никаких разделов: они всё равно вели бы на форму входа и только сбивали бы с толку.
  var PUBLIC_IDS = ["home", "identify"];

  var ROLE_LABELS = {
    admin: "Администратор", merchandiser: "Мерчандайзер", regional_manager: "Региональный менеджер",
    analyst: "Аналитик", camera_agent: "Служебная учётная запись"
  };

  /** Пункты меню для набора прав. perms === null — права неизвестны (не вошли / ещё не загружены). */
  function itemsFor(perms) {
    return ITEMS.filter(function (item) {
      if (perms === null || perms === undefined) return PUBLIC_IDS.indexOf(item.id) !== -1;
      return item.needs === null || perms.indexOf(item.needs) !== -1;
    });
  }

  // Перевод, если на странице есть I18N (главная его подключает); иначе — русская подпись (так меню работает и без словарей).
  function tr(key, fallback) {
    var I = root.I18N;
    return I && typeof I.has === "function" && I.has(key) ? I.t(key) : fallback;
  }

  function roleLabel(role) { return ROLE_LABELS[role] ? tr("role." + role, ROLE_LABELS[role]) : ""; }

  function parsePerms(text) {
    if (!text) return null;
    try {
      var value = JSON.parse(text);
      return Array.isArray(value) ? value.filter(function (p) { return typeof p === "string"; }) : null;
    } catch (e) { return null; }
  }

  function apiBase(win) {
    var fromQuery = new URLSearchParams(win.location.search).get("api_base");
    if (fromQuery) return fromQuery;
    return /^https?:$/.test(win.location.protocol) ? win.location.origin : "http://localhost:8000";
  }

  // ------------------------------------------------------------------ хранилище
  // Источник правды — sessionStorage: страницы входят и выходят каждая своим кодом и пишут именно туда. Память — запасной вариант
  // ТОЛЬКО на случай, когда браузер вообще запретил sessionStorage (иначе устаревшая копия «оживляла» бы уже завершённую сессию).
  function makeStore(win) {
    var memory = {};
    return {
      get: function (key) {
        try { return win.sessionStorage.getItem(key); } catch (e) { return memory[key] || null; }
      },
      set: function (key, value) {
        try {
          if (value === null) win.sessionStorage.removeItem(key); else win.sessionStorage.setItem(key, value);
        } catch (e) {
          if (value === null) delete memory[key]; else memory[key] = value;
        }
      }
    };
  }

  // ------------------------------------------------------------------ разметка
  function el(doc, tag, attrs, text) {
    var node = doc.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function buildBrand(doc) {
    var link = el(doc, "a", { "class": "shell-brand", href: "index.html" });
    link.appendChild(el(doc, "img", { src: "icons/icon-192.png", alt: "", width: "30", height: "30" }));
    link.appendChild(el(doc, "span", {}, tr("app.name", "Узнать товар")));
    return link;
  }

  function buildLink(doc, item, activePage) {
    var cls = "shell-link" + (item.laptopOnly ? " laptop-only" : "");
    var link = el(doc, "a", { "class": cls, href: item.href });
    if (item.id === activePage) link.setAttribute("aria-current", "page");
    link.appendChild(el(doc, "span", { "class": "ico", "aria-hidden": "true" }, item.icon));
    link.appendChild(el(doc, "span", { "class": "lbl" }, tr(item.id === "identify" ? "app.name" : "nav." + item.id, item.label)));
    return link;
  }

  /** Рисует меню в контейнер. state: {perms, role, loggedIn}. onLogout вызывается по кнопке «Выйти». */
  function render(doc, container, activePage, state, onLogout) {
    while (container.firstChild) container.removeChild(container.firstChild);

    var top = el(doc, "header", { "class": "shell-top" });
    top.appendChild(buildBrand(doc));
    if (state.loggedIn) {
      var topOut = el(doc, "button", { "class": "shell-logout", type: "button" }, tr("settings.sign_out", "Выйти"));
      topOut.addEventListener("click", onLogout);
      top.appendChild(topOut);
    }
    container.appendChild(top);

    var shown = state.loggedIn ? itemsFor(state.perms) : [];
    var nav = el(doc, "nav", { "class": "shell-nav" + (shown.length ? "" : " no-links"), "aria-label": tr("nav.menu", "Разделы") });
    var side = el(doc, "div", { "class": "shell-side" });
    side.appendChild(buildBrand(doc));
    nav.appendChild(side);
    shown.forEach(function (item) { nav.appendChild(buildLink(doc, item, activePage)); });
    if (state.loggedIn) {
      var foot = el(doc, "div", { "class": "shell-foot shell-side" });
      var who = roleLabel(state.role);
      if (who) foot.appendChild(el(doc, "div", { "class": "shell-who" }, who));
      var out = el(doc, "button", { "class": "shell-logout", type: "button" }, tr("settings.sign_out", "Выйти"));
      out.addEventListener("click", onLogout);
      foot.appendChild(out);
      nav.appendChild(foot);
    }
    container.appendChild(nav);
  }

  // ------------------------------------------------------------------ запуск на странице
  function start(win, options) {
    var doc = win.document, container = doc.getElementById("appNav");
    if (!container) return null;
    var store = makeStore(win), page = doc.body.getAttribute("data-page") || "";
    var base = apiBase(win), pollMs = options && options.pollMs !== undefined ? options.pollMs : 1000;
    var drawn = null, asked = false, knownToken;     // knownToken: токен на прошлой проверке (undefined — ещё не проверяли)

    function state() {
      var token = store.get(TOKEN_KEY);
      return { loggedIn: !!token, role: store.get(ROLE_KEY), perms: token ? parsePerms(store.get(PERMS_KEY)) : null };
    }
    function signature(st) { return [st.loggedIn, st.role, st.perms === null ? "?" : st.perms.join(",")].join("|"); }
    function clear() { store.set(TOKEN_KEY, null); store.set(ROLE_KEY, null); store.set(PERMS_KEY, null); asked = false; }

    // Язык в профиле (миграция 0002): после входа язык из профиля применяется и на этом устройстве; выбор языка вошедшим
    // сотрудником сохраняется в профиль. Без входа ничего не отправляется.
    var I18 = win.I18N || root.I18N, languageFromServer = false;
    function applyProfileLanguage(lang) {
      if (!lang || !I18 || typeof I18.setLang !== "function" || I18.getLang() === lang) return;
      languageFromServer = true;
      try { I18.setLang(lang); } finally { languageFromServer = false; }
    }
    function saveLanguage(lang) {
      var token = store.get(TOKEN_KEY);
      if (languageFromServer || !token || !lang) return;
      win.fetch(base + "/api/v1/auth/me/language", {
        method: "PUT", headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify({ language: lang })
      }).catch(function () { /* нет связи: язык остаётся выбранным на этом устройстве */ });
    }

    function logout() {
      var token = store.get(TOKEN_KEY);
      clear();
      var done = function () { win.location.href = "index.html"; };
      if (!token) { done(); return; }
      win.fetch(base + "/api/v1/auth/logout", { method: "POST", headers: { Authorization: "Bearer " + token } }).then(done, done);
    }

    // Права неизвестны (вошли на этой же или на другой странице): один запрос /auth/me, дальше берём из sessionStorage.
    function askPermissions(token) {
      if (asked) return;
      asked = true;
      win.fetch(base + "/api/v1/auth/me", { headers: { Authorization: "Bearer " + token } })
        .then(function (res) {
          if (res.status === 401) { clear(); sync(); return null; }
          return res.ok ? res.json() : null;
        })
        .then(function (me) {
          if (!me) return;
          store.set(ROLE_KEY, me.role || null);
          store.set(PERMS_KEY, JSON.stringify(Array.isArray(me.permissions) ? me.permissions : []));
          applyProfileLanguage(me.language);
          sync();
        })
        .catch(function () { /* нет связи: остаются общие разделы, страница сама покажет ошибку */ });
    }

    // Страницы входят и выходят каждая своим кодом и о меню не знают, поэтому меню само замечает смену состояния.
    function sync() {
      var token = store.get(TOKEN_KEY);
      // Вошёл другой человек (токен сменился без «Выйти» через меню): права прошлого пользователя ему не принадлежат.
      if (knownToken !== undefined && token && token !== knownToken) { store.set(PERMS_KEY, null); asked = false; }
      knownToken = token;
      var st = state(), sig = signature(st);
      if (sig !== drawn) { drawn = sig; render(doc, container, page, st, logout); }
      if (token && st.perms === null) askPermissions(token);
      if (!token) asked = false;
    }

    sync();
    if (pollMs > 0 && win.setInterval) win.setInterval(sync, pollMs);
    // Смена языка: меню перерисовывается с новыми подписями.
    if (I18 && typeof I18.onChange === "function") I18.onChange(function (lang) { saveLanguage(lang); drawn = null; sync(); });
    return { sync: sync, logout: logout };
  }

  var api = { ITEMS: ITEMS, PUBLIC_IDS: PUBLIC_IDS, itemsFor: itemsFor, roleLabel: roleLabel, parsePerms: parsePerms, apiBase: apiBase,
              render: render, start: start, KEYS: { TOKEN_KEY: TOKEN_KEY, ROLE_KEY: ROLE_KEY, PERMS_KEY: PERMS_KEY } };
  root.AppNav = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document && root.document.getElementById) {
    var run = function () { api.instance = start(root); };
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", run); else run();
  }
})(typeof window !== "undefined" ? window : globalThis);
