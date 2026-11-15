/* Рамка приложения для сотрудников (редизайн 2026-11-15): одно меню на всех страницах управления — «Товары», «Добавить товар»,
   «Карточка покупателя», «Сотрудники», «Проверить по фото», «Состояние программы», «Настройки».
   На широком экране — колонка слева с подписями, на ноутбуке поуже — значки с короткими подписями, на телефоне — нижняя панель.
   Разметка: <nav class="rail" id="rail" data-shell></nav>; подключается после i18n, icons.js и auth.js.
   Пункты — по правам (permissions из /auth/me; права хранятся рядом с токеном в sessionStorage, как раньше в nav.js).
   Клавиши: «/» — поиск на странице (элемент с data-hotkey="/"), «N» — новый товар (data-hotkey="n"), Esc — закрыть окно (data-hotkey="esc").
   Данные в разметку строкой не подставляются: только createElement / textContent. Проверяется tools/shell_smoke_test.js. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Shell = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  var TOKEN_KEY = "shelf_audit_token", ROLE_KEY = "shelf_audit_role", PERMS_KEY = "shelf_audit_perms", NAME_KEY = "shelf_audit_name";

  // group: main — работа с товарами и магазином; tools — проверка и служебное. mobile — показывать в нижней панели телефона.
  var ITEMS = [
    { id: "products", href: "products.html", icon: "list", label: "nav.products", needs: "catalog:write", group: "main", mobile: true },
    { id: "add", href: "products.html#add", icon: "plus", label: "nav.add_product", needs: "catalog:write", group: "main" },
    { id: "card", href: "admin.html#/card", icon: "card", label: "nav.card", needs: "store:manage", group: "main" },
    { id: "users", href: "admin.html#/users", icon: "users", label: "nav.staff", needs: "store:manage", group: "main" },
    { id: "identify", href: "identify.html", icon: "viewfinder", label: "nav.check_photo", needs: "catalog:read", group: "tools", mobile: true },
    { id: "system", href: "admin.html#/system", icon: "activity", label: "nav.system", needs: "store:manage", group: "tools", mobile: true },
    { id: "settings", href: "settings.html", icon: "sliders", label: "nav.settings", needs: null, group: "tools", mobile: true }
  ];

  function itemsFor(perms) {
    perms = Array.isArray(perms) ? perms : [];
    return ITEMS.filter(function (i) { return i.needs === null || perms.indexOf(i.needs) !== -1; });
  }
  /** Какой пункт сейчас открыт: по имени страницы и разделу после # (admin.html#/users -> users, products.html#add -> add). */
  function activeId(path, hash) {
    var page = String(path || "").split("/").pop() || "";
    hash = hash || "";
    if (page === "products.html") return hash === "#add" ? "add" : "products";
    if (page === "admin.html") {
      var m = /^#\/([a-z]+)/.exec(hash);
      var sub = m ? m[1] : "system";
      return ["card", "users", "system"].indexOf(sub) !== -1 ? sub : (sub === "products" ? "products" : "system");
    }
    if (page === "identify.html") return "identify";
    if (page === "settings.html") return "settings";
    return null;
  }
  function parsePerms(text) {
    try { var v = JSON.parse(text || "null"); return Array.isArray(v) ? v : null; } catch (e) { return null; }
  }
  function typing(target) {
    var tag = target && target.tagName ? String(target.tagName).toLowerCase() : "";
    return tag === "input" || tag === "textarea" || tag === "select" || !!(target && target.isContentEditable);
  }

  function start(win, deps) {
    deps = deps || {};
    var doc = win.document, nav = doc.getElementById("rail");
    if (!nav || !nav.hasAttribute("data-shell")) return null;
    var I18N = deps.I18N || win.I18N, Icons = deps.Icons || win.Icons, Auth = deps.Auth || win.Auth;
    var auth = Auth.create(win);
    var t = function (k, p) { return I18N && I18N.t ? I18N.t(k, p) : k; };
    function store(key, value) {
      try { if (value === undefined) return win.sessionStorage.getItem(key); if (value === null) win.sessionStorage.removeItem(key); else win.sessionStorage.setItem(key, value); }
      catch (e) { /* без sessionStorage меню строится после запроса /auth/me */ }
      return null;
    }
    var me = { perms: parsePerms(store(PERMS_KEY)), role: store(ROLE_KEY), name: store(NAME_KEY) };

    function el(tag, cls, text) {
      var n = doc.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined && text !== null) n.textContent = text;
      return n;
    }
    function icon(name) { return Icons ? Icons.make(doc, name, { size: 22 }) : el("span"); }
    function render() {
      while (nav.firstChild) nav.removeChild(nav.firstChild);
      nav.setAttribute("aria-label", t("nav.menu"));
      if (!auth.getToken()) return;
      var brand = el("a", "rail__brand");
      brand.setAttribute("href", "products.html");
      var img = el("img"); img.setAttribute("src", "icons/icon-192.png"); img.setAttribute("alt", ""); img.setAttribute("width", "28"); img.setAttribute("height", "28");
      brand.appendChild(img); brand.appendChild(el("span", "rail__brandname", t("app.name")));
      nav.appendChild(brand);
      var active = activeId(win.location.pathname, win.location.hash), last = null;
      itemsFor(me.perms || []).forEach(function (item) {
        if (last && last !== item.group) nav.appendChild(el("div", "rail__sep"));
        last = item.group;
        var a = el("a", "rail__item" + (item.mobile ? "" : " rail__item--wide"));
        a.setAttribute("href", item.href);
        if (item.id === active) a.setAttribute("aria-current", "page");
        a.appendChild(icon(item.icon));
        a.appendChild(el("span", "rail__label", t(item.label)));
        nav.appendChild(a);
      });
      var foot = el("div", "rail__foot");
      if (me.name) {
        var who = el("div", "rail__who");
        who.appendChild(el("b", null, me.name));
        if (me.role) who.appendChild(el("span", null, t("role." + me.role)));
        foot.appendChild(who);
      }
      var out = el("button", "rail__item rail__item--wide");
      out.setAttribute("type", "button"); out.setAttribute("id", "shellLogout");
      out.appendChild(icon("log-out")); out.appendChild(el("span", "rail__label", t("settings.sign_out")));
      out.addEventListener("click", logout);
      foot.appendChild(out);
      nav.appendChild(foot);
    }
    function logout() {
      return auth.logout().then(function () {
        [ROLE_KEY, PERMS_KEY, NAME_KEY].forEach(function (k) { store(k, null); });
        win.location.replace("login.html");
      });
    }
    function refreshMe() {
      if (!auth.getToken()) return Promise.resolve(null);
      return auth.me().then(function (r) {
        if (r.status !== "ok") return null;
        me = { perms: Array.isArray(r.me.permissions) ? r.me.permissions : [], role: r.me.role || null, name: r.me.username || null };
        store(PERMS_KEY, JSON.stringify(me.perms)); store(ROLE_KEY, me.role || null); store(NAME_KEY, me.name || null);
        render();
        return r.me;
      });
    }
    function onKey(evt) {
      if (evt.ctrlKey || evt.metaKey || evt.altKey) return;
      var key = evt.key === "Escape" ? "esc" : String(evt.key || "").toLowerCase();
      if (key !== "esc" && typing(evt.target)) return;
      var map = { "/": "/", "n": "n", "т": "n", "esc": "esc" };          // «т» — та же клавиша N в русской раскладке
      if (!map[key]) return;
      var target = doc.querySelector('[data-hotkey="' + map[key] + '"]');
      if (!target || target.hasAttribute("hidden") || target.disabled) return;
      if (evt.preventDefault) evt.preventDefault();
      if (map[key] === "/" && target.focus) target.focus(); else if (target.click) target.click();
    }

    render();
    var ready = refreshMe();
    if (I18N && I18N.onChange) I18N.onChange(render);
    if (win.addEventListener) win.addEventListener("hashchange", render);
    if (doc.addEventListener) doc.addEventListener("keydown", onKey);
    return { ready: ready, render: render, logout: logout };
  }

  return { ITEMS: ITEMS, itemsFor: itemsFor, activeId: activeId, start: start,
           KEYS: { TOKEN_KEY: TOKEN_KEY, ROLE_KEY: ROLE_KEY, PERMS_KEY: PERMS_KEY, NAME_KEY: NAME_KEY } };
});
