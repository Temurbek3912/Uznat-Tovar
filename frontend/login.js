/* Единая страница входа (редизайн 2026-11-15): вместо четырёх разных форм на разных страницах — одна.
   На ЭТОМ компьютере (сервер подтверждает: GET /auth/local) — ПИН-код из 6 цифр; с другого устройства — логин и пароль (у администратора
   ещё код из приложения). Первый вход и «забыли ПИН» — одноразовая ссылка из окна программы: login.html#setup=… -> «Придумайте ПИН».
   После входа — на страницу из ?next= (только свои страницы из списка) или на первую по правам. Проверяется tools/login_smoke_test.js.
   Данные в разметку строкой не подставляются: только textContent / setAttribute. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Login = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  var PAGES = ["products.html", "identify.html", "settings.html", "admin.html"];
  var PIN_LEN = 6;

  /** ?next= -> безопасная страница (только свои, без чужих адресов) или null. Раздел управления после # сохраняется. */
  function safeNext(search) {
    var raw = new URLSearchParams(search || "").get("next") || "";
    var m = /^([a-z-]+\.html)(#\/[a-z]+)?$/.exec(raw);
    return m && PAGES.indexOf(m[1]) !== -1 ? raw : null;
  }
  /** Куда идти после входа без ?next=: управление товарами — тем, кто может их менять; остальным — «Узнать товар». */
  function startPage(perms) {
    perms = Array.isArray(perms) ? perms : [];
    if (perms.indexOf("catalog:write") !== -1) return "products.html";
    if (perms.indexOf("store:manage") !== -1) return "admin.html";
    return "identify.html";
  }
  /** «#setup=ТОКЕН» -> токен или null. */
  function setupToken(hash) {
    var m = /^#setup=([A-Za-z0-9_\-]{10,200})$/.exec(hash || "");
    return m ? m[1] : null;
  }
  function onlyDigits(value) { return String(value || "").replace(/\D+/g, "").slice(0, PIN_LEN); }
  function displayName(user, t) { return user && user.is_owner ? t("signin.owner") : (user ? user.username : ""); }

  function start(win, deps) {
    deps = deps || {};
    var doc = win.document, I18N = deps.I18N || win.I18N, Auth = deps.Auth || win.Auth, Theme = deps.Theme || win.Theme;
    var Icons = deps.Icons || win.Icons;
    var auth = Auth.create(win), t = function (k, p) { return I18N.t(k, p); };
    var $ = function (id) { return doc.getElementById(id); };
    var state = { view: "loading", local: null, user: null, setup: null, setupStep: 1, firstPin: "", msgs: {} };

    function show(id, on) { var n = $(id); if (!n) return; if (on) n.removeAttribute("hidden"); else n.setAttribute("hidden", ""); }
    function setView(view) {
      state.view = view;
      ["loadingView", "setupView", "pinView", "passView"].forEach(function (id) { show(id, id === view + "View"); });
      paint();
      var focus = { setup: "setupPin", pin: "pinInput", pass: "loginUser" }[view];
      if (focus && $(focus) && $(focus).focus) $(focus).focus();
    }
    // Сообщения хранятся ключами и переводятся заново при смене языка.
    function setMsg(id, msg) { state.msgs[id] = msg || null; paintMsg(id); }
    function paintMsg(id) {
      var m = state.msgs[id], node = $(id);
      if (!node) return;
      node.textContent = m ? t(m.key, m.params || {}) : "";
      show(id, !!m);
    }
    function paint() {
      var name = displayName(state.user, t);
      $("pinHello").textContent = name ? t("signin.hello", { name: name }) : t("signin.title");
      if (state.setup) {
        $("setupFor").textContent = t("signin.setup_for", { name: state.setup.username || "" });
        $("setupPinLabel").textContent = t(state.setupStep === 1 ? "signin.enter_new_pin" : "signin.repeat_pin");
        $("setupBtn").textContent = t(state.setupStep === 1 ? "signin.next" : "signin.done");
      }
      var local = state.local || {};
      show("toPin", !!(local.this_computer && local.users && local.users.length));
      show("firstRunNote", !!(local.this_computer && local.setup_needed));
      $("whereNote").textContent = !state.local ? "" : local.this_computer ? t("signin.this_computer") : t("signin.other_device");
      Object.keys(state.msgs).forEach(paintMsg);
      paintUsers();
      var mode = Theme ? Theme.get() : "system", theme = $("themeBtn");
      if (theme && Icons) {
        while (theme.firstChild) theme.removeChild(theme.firstChild);
        theme.appendChild(Icons.make(doc, mode === "dark" ? "moon" : mode === "light" ? "sun" : "monitor", { size: 20 }));
        theme.setAttribute("aria-label", t("theme.label", { mode: t("theme." + mode) }));
      }
      doc.title = t("signin.title") + " — " + t("app.name");
    }
    function paintUsers() {
      var host = $("userPicker"), users = (state.local && state.local.users) || [];
      while (host.firstChild) host.removeChild(host.firstChild);
      show("userPicker", users.length > 1);
      users.forEach(function (u) {
        var b = doc.createElement("button");
        b.type = "button"; b.className = "chip chip--filter";
        b.setAttribute("role", "radio"); b.setAttribute("aria-checked", state.user && state.user.username === u.username ? "true" : "false");
        b.textContent = displayName(u, t);
        b.addEventListener("click", function () { state.user = u; setMsg("pinError", null); paint(); if ($("pinInput").focus) $("pinInput").focus(); });
        host.appendChild(b);
      });
    }

    function goNext() {
      var next = safeNext(win.location.search);
      if (next) { win.location.replace(next); return Promise.resolve(); }
      return auth.me().then(function (r) {
        win.location.replace(startPage(r && r.status === "ok" ? r.me.permissions : []));
      });
    }
    function busy(id, on) { var b = $(id); if (b) b.disabled = !!on; }
    function keyOf(r) { return r.key === "login.throttled" && r.retryAfter ? { key: "signin.wait", params: { min: Math.max(1, Math.ceil(r.retryAfter / 60)) } } : { key: r.key, params: r.params }; }

    // ----- ПИН
    function submitPin(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      var pin = onlyDigits($("pinInput").value);
      if (pin.length !== PIN_LEN) { setMsg("pinError", { key: "signin.pin_rule_short" }); return Promise.resolve(); }
      if (!state.user) return Promise.resolve();
      busy("pinBtn", true); setMsg("pinError", null);
      return auth.pinLogin(state.user.username, pin).then(function (r) {
        $("pinInput").value = "";
        if (r.ok) return goNext();
        if (r.status === 403) { state.local = { this_computer: false, users: [] }; setView("pass"); }
        setMsg(r.status === 403 ? "passError" : "pinError", keyOf(r));
        return null;
      }).then(function () { busy("pinBtn", false); });
    }

    // ----- «Придумайте ПИН»: два шага (ввод и повтор), затем вход
    function submitSetup(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      var pin = onlyDigits($("setupPin").value);
      if (pin.length !== PIN_LEN) { setMsg("setupError", { key: "signin.pin_rule_short" }); return Promise.resolve(); }
      if (state.setupStep === 1) {
        state.firstPin = pin; state.setupStep = 2; $("setupPin").value = ""; setMsg("setupError", null); paint();
        if ($("setupPin").focus) $("setupPin").focus();
        return Promise.resolve();
      }
      $("setupPin").value = "";
      if (pin !== state.firstPin) {
        state.firstPin = ""; state.setupStep = 1; setMsg("setupError", { key: "signin.mismatch" }); paint();
        return Promise.resolve();
      }
      busy("setupBtn", true);
      return auth.setupPin(state.setup.token, pin).then(function (r) {
        state.firstPin = "";
        if (r.ok) return goNext();
        state.setupStep = 1; paint();
        setMsg("setupError", keyOf(r));
        return null;
      }).then(function () { busy("setupBtn", false); });
    }

    // ----- логин и пароль (+ код)
    function submitPassword(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      busy("passBtn", true); setMsg("passError", null);
      return auth.login($("loginUser").value, $("loginPass").value, ($("loginCode") || {}).value || "").then(function (r) {
        var code = !!(r && r.needCode);
        show("loginCodeField", code);
        if (code && $("loginCode").focus) $("loginCode").focus();
        if (r && (r.needEnroll || r.needPasswordChange)) { win.location.assign("settings.html"); return null; }
        if (r.ok) { $("loginPass").value = ""; return goNext(); }
        if (!code) $("loginPass").value = "";
        setMsg("passError", keyOf(r));
        return null;
      }).then(function () { busy("passBtn", false); }, function () { busy("passBtn", false); setMsg("passError", { key: "error.network" }); });
    }

    function digitsOnly(id, autoSubmit) {
      var input = $(id);
      input.addEventListener("input", function () {
        var v = onlyDigits(input.value);
        if (v !== input.value) input.value = v;
        if (v.length === PIN_LEN && autoSubmit) autoSubmit();
      });
    }

    function boot() {
      var token = setupToken(win.location.hash);
      if (token) {
        // Токен не остаётся в адресной строке и истории.
        try { win.history.replaceState(null, "", win.location.pathname + (win.location.search || "")); } catch (e) { /* не страшно */ }
        return auth.setupCheck(token).then(function (r) {
          if (r && r.valid) { state.setup = { token: token, username: r.username }; return setView("setup"); }
          return loadLocal().then(function () { setMsg(state.view === "pin" ? "pinError" : "passError", { key: "server.setup_link_invalid" }); });
        });
      }
      var why = new URLSearchParams(win.location.search || "").get("why");
      return loadLocal().then(function () {
        if (why === "expired") setMsg(state.view === "pin" ? "pinError" : "passError", { key: "signin.session_ended" });
      });
    }
    function loadLocal() {
      return auth.localInfo().then(function (local) {
        state.local = local;
        var users = local.users || [];
        if (local.this_computer && users.length) {
          state.user = users.filter(function (u) { return u.is_owner; })[0] || users[0];
          return setView("pin");
        }
        return setView("pass");
      });
    }

    // ----- запуск
    I18N.mountSwitcher($("langHost"));
    I18N.apply();
    I18N.onChange(paint);
    if ($("themeBtn") && Theme) $("themeBtn").addEventListener("click", function () { Theme.next(); paint(); });
    digitsOnly("pinInput", function () { submitPin(); });
    digitsOnly("setupPin", null);
    $("pinView").addEventListener("submit", submitPin);
    $("setupView").addEventListener("submit", submitSetup);
    $("passView").addEventListener("submit", submitPassword);
    $("toPassword").addEventListener("click", function () { setMsg("pinError", null); setView("pass"); });
    $("toPin").addEventListener("click", function () { setMsg("passError", null); setView("pin"); });
    $("forgotBtn").addEventListener("click", function () { show("forgotHint", true); });
    // Ссылку «Придумайте ПИН» открыли в уже открытой вкладке входа: меняется только «#setup=…», страница не перезагружается.
    var ctl = { state: state };
    if (win.addEventListener) win.addEventListener("hashchange", function () {
      if (!setupToken(win.location.hash)) return;
      state.setup = null; state.setupStep = 1; state.firstPin = "";
      setMsg("setupError", null); setMsg("pinError", null); setMsg("passError", null);
      ctl.ready = boot();
    });
    // Уже вошли (вкладка с сеансом): сразу дальше, без повторного входа.
    var ready = auth.getToken() ? auth.me().then(function (r) { return r.status === "ok" ? goNext() : boot(); }) : boot();
    ctl.ready = ready;
    return ctl;
  }

  return { start: start, safeNext: safeNext, startPage: startPage, setupToken: setupToken, onlyDigits: onlyDigits, PAGES: PAGES };
});
