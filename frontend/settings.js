/* Экран «Настройки» (settings.html): язык, тема, переключатель страницы «Узнать товар», вход владельца и администратора. Настройки хранятся на этом устройстве (localStorage),
   сервер им не нужен, поэтому страница работает без входа; вход (токен, запросы) ведёт общий модуль Auth (auth.js), здесь только форма и состояние. Как у identify.js и products.js: страница только подключает файлы и вызывает Settings.start(window); логика здесь и проверяется на настоящей разметке
   (tools/settings_smoke_test.js). Переключатель «Узнавать сама» пишет в тот же ключ, что и сама страница «Узнать товар» (identify_settings),
   с теми же правилами разбора. Данные в разметку строкой не попадают: только textContent / setAttribute. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Settings = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  var SETTINGS_KEY = "identify_settings";

  /** Те же правила, что у Identify.cleanSettings (тест следит, чтобы они не разошлись): автоузнавание по умолчанию включено; лишние поля старых записей отбрасываются. */
  function cleanSettings(raw) {
    var out = { auto: true };
    if (raw && typeof raw === "object" && typeof raw.auto === "boolean") out.auto = raw.auto;
    return out;
  }

  function start(win, options) {
    options = options || {};
    var doc = win.document, I18N = win.I18N, Auth = win.Auth, Icons = win.Icons, Theme = win.Theme;
    var $ = function (id) { return doc.getElementById(id); };
    var t = function (key, params) { return I18N.t(key, params); };
    var report = options.onError || function (e) { if (win.console && win.console.error) win.console.error(e); };
    var guard = function (fn) { return function () { var r = fn.apply(null, arguments); if (r && r.catch) r.catch(report); return r; }; };
    var auth = Auth.create(win);

    function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
    function setHidden(el, hidden) { if (hidden) el.setAttribute("hidden", ""); else el.removeAttribute("hidden"); }

    // ----- тема
    function renderThemeSwitch() {
      var host = $("themeHost");
      clear(host);
      Theme.modes.forEach(function (mode) {
        var b = doc.createElement("button");
        b.setAttribute("type", "button"); b.className = "lang-btn lang-btn--cap"; b.textContent = t("theme." + mode);
        b.setAttribute("data-mode", mode); b.setAttribute("aria-pressed", Theme.get() === mode ? "true" : "false");
        b.addEventListener("click", function () { Theme.set(mode); relabel(); renderThemeSwitch(); });
        host.appendChild(b);
      });
    }

    // ----- переключатели страницы «Узнать товар»
    function readSettings() { try { return cleanSettings(JSON.parse(win.localStorage.getItem(SETTINGS_KEY) || "null")); } catch (e) { return cleanSettings(null); } }
    function storeSettings() {
      var cfg = { auto: !!$("autoToggle").checked };
      try { win.localStorage.setItem(SETTINGS_KEY, JSON.stringify(cfg)); } catch (e) { /* без запоминания — начнёт с умолчаний */ }
    }

    // ----- вход: форма или «Вы вошли как …»; меню показывает «Товары» и «Управление» по правам (без входа лишние пункты просто не видны)
    var account = { name: null, known: !auth.getToken() };      // name — кто вошёл (null — никто); known: false — ждём ответ сервера, пока не показываем ни форму, ни имя
    var loginMsg = null;                                         // {key, params} | null — строка ошибки под формой, хранится ключом и перерисовывается при смене языка
    // Временный пароль (документ 05, §12.2–§12.3): после входа им — не сеанс, а пропуск; здесь сотрудник задаёт свой пароль.
    var initialMsg = null;
    function changingPassword() { return !auth.getToken() && !!auth.passwordChange(); }
    function renderInitial() {
      var pass = auth.passwordChange(), on = changingPassword();
      setHidden($("initialPanel"), !on);
      if (!on) return;
      $("initialPrompt").textContent = t("login.password_change_prompt");
      $("initialNewLabel").textContent = t("security.password_new", { min: MIN_PASSWORD });
      setHidden($("initialCodeField"), !(pass && pass.codeRequired));
      var err = $("initialError"), text = initialMsg ? t(initialMsg.key, initialMsg.params) : "";
      err.textContent = text; setHidden(err, !text);
    }
    function setInitial(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      var next = $("initialNew").value, again = $("initialRepeat").value, code = $("initialCode").value;
      if (next !== again) { $("initialRepeat").value = ""; initialMsg = { key: "security.password_mismatch" }; renderInitial(); return Promise.resolve(null); }
      $("initialNew").value = ""; $("initialRepeat").value = ""; $("initialCode").value = "";
      var btn = $("initialBtn"); btn.disabled = true;
      return auth.setInitialPassword(next, code).then(function (r) {
        if (!r.ok) { initialMsg = r.params ? { key: r.key, params: r.params } : { key: r.key }; renderAccount(); return null; }
        initialMsg = null;
        if (r.signedIn) return refreshAccount();
        loginMsg = { key: "login.password_set_sign_in" }; renderAccount();                 // нужен второй фактор — вход с новым паролем
        var u = $("loginUser"); if (u && u.focus) u.focus();
        return null;
      }).then(function (x) { btn.disabled = false; return x; }, function (e) { btn.disabled = false; throw e; });
    }
    function renderAccount() {
      var signedIn = account.name !== null;
      setHidden($("loginPanel"), signedIn || !account.known || changingPassword()); setHidden($("accountInfo"), !signedIn);
      renderInitial();
      if (signedIn) $("accountName").textContent = t("settings.signed_in", { name: account.name });
      var err = $("loginError"), text = loginMsg ? t(loginMsg.key, loginMsg.params) : "";
      err.textContent = text; setHidden(err, !text);
      renderSecurity();
    }
    function revealRail(perms) {
      setHidden($("railProducts"), perms.indexOf("catalog:write") === -1);
      setHidden($("railAdmin"), perms.indexOf("store:manage") === -1);
    }
    function refreshAccount() {
      return auth.me().then(function (r) {
        account.known = true;
        if (r.status === "ok") {
          var me = r.me || {};
          account.name = String(me.username || "—"); loginMsg = null;
          revealRail(Array.isArray(me.permissions) ? me.permissions : []);
          loadSecurity().catch(report);
        } else {
          account.name = null; revealRail([]); resetSecurity();
          loginMsg = r.status === "expired" ? { key: "login.expired" } : r.status === "error" ? { key: r.key } : null;
        }
        renderAccount();
        return r;
      });
    }
    // Второй фактор (документ 18): поле кода появляется, когда сервер попросил код после верного пароля.
    function codeField(on) {
      var f = $("loginCodeField"), c = $("loginCode");
      if (f) { if (on) f.removeAttribute("hidden"); else f.setAttribute("hidden", ""); }
      if (c) { if (!on) c.value = ""; else if (c.focus) c.focus(); }
    }
    function doLogin(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      var btn = $("loginBtn");
      loginMsg = null; renderAccount(); btn.disabled = true;
      return auth.login($("loginUser").value, $("loginPass").value, ($("loginCode") || {}).value || "").then(function (r) {
        codeField(!!(r && r.needCode));
        if (r && r.insecure) $("loginPass").value = "";          // пароль не отправлен (незащищённый канал) — и в поле не остаётся
        if (r && r.needEnroll) { loginMsg = null; $("loginPass").value = ""; renderAccount(); return loadSecurity(); }
        if (r && r.needPasswordChange) { loginMsg = null; initialMsg = null; $("loginPass").value = ""; renderAccount(); var n = $("initialNew"); if (n && n.focus) n.focus(); return null; }
        if (!r.ok) { loginMsg = r.params ? { key: r.key, params: r.params } : { key: r.key }; renderAccount(); return null; }
        $("loginPass").value = "";
        return refreshAccount().then(function (me) { if (me.status !== "ok") auth.setToken(null); });      // вход прошёл, но проверить, кто это, не вышло — токен не оставляем
      }).catch(function (e) { loginMsg = { key: "login.failed", params: { reason: e.message } }; renderAccount(); })
        .then(function () { btn.disabled = false; });
    }
    function doLogout() {
      var done = auth.logout();                                  // токен стирается сразу, сервер уведомляется по возможности
      account.name = null; account.known = true; loginMsg = null; revealRail([]); resetSecurity(); renderAccount();
      var u = $("loginUser"); if (u && u.focus) u.focus();
      return done;
    }

    // ----- безопасность входа: второй фактор и сеансы (документ 18). Только после входа; данные — с сервера, тексты — ключами.
    // enrolling: администратор без второго фактора вошёл паролем и получил пропуск настройки (решение В-3) — сеанса ещё нет,
    // запросы идут с пропуском; recovery: коды восстановления, показанные один раз (в памяти страницы, не в хранилище).
    var security = { mfa: null, sessions: [], step: "idle", msg: null, done: null, secret: null, uri: null, recovery: null };
    var MIN_PASSWORD = 8;                                        // как MIN_PASSWORD_LENGTH в app/core/security.py (сервер проверяет сам)
    var LOCALES = { ru: "ru", uz: "uz-Latn", "uz-Cyrl": "uz-Cyrl", en: "en" };
    var KIND_KEYS = { phone: "security.kind_phone", tablet: "security.kind_tablet", computer: "security.kind_computer" };
    var SOURCE_KEYS = { local: "security.source_local", lan: "security.source_lan", remote: "security.source_remote" };
    function resetSecurity() { security = { mfa: null, sessions: [], step: "idle", msg: null, done: null, secret: null, uri: null, recovery: null }; renderSecurity(); }
    function enrolling() { return !auth.getToken() && !!auth.enrollToken(); }
    function api(path, method, body) {
      var headers = auth.authHeaders({ enroll: true });
      var init = { method: method || "GET", headers: headers };
      if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
      return win.fetch(auth.apiBase() + path, init).then(function (res) {
        return res.json().then(function (data) { return { res: res, data: data }; }, function () { return { res: res, data: null }; });
      });
    }
    function problemOf(r) {
      var code = r.data && typeof r.data.code === "string" && /^[a-z_]+$/.test(r.data.code) ? r.data.code : null;
      if (code && I18N.has("server." + code)) return { key: "server." + code, params: r.data.params || {} };
      if (r.res.status === 401) return { key: "login.expired" };
      return { key: "login.failed", params: { reason: "HTTP " + r.res.status } };
    }
    function when(iso) {
      if (!iso) return "—";
      var d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");          // сервер отдаёт время UTC без пояса
      if (isNaN(d.getTime())) return "—";
      try { return new Intl.DateTimeFormat(LOCALES[I18N.getLang()] || "ru", { dateStyle: "medium", timeStyle: "short" }).format(d); }
      catch (e) { return d.toISOString().slice(0, 16).replace("T", " "); }
    }
    function deviceText(s) {
      if (s.device_label) return s.device_label;
      var parts = [t(KIND_KEYS[s.device_kind] || "security.kind_unknown")];
      if (s.device_os) parts.push(s.device_os);
      if (s.device_browser) parts.push(s.device_browser);
      return parts.join(" · ");
    }
    function loadSecurity() {
      if (enrolling()) {
        return api("/api/v1/auth/2fa").then(function (r) {
          if (r.res.status === 401) { auth.setEnrollToken(null); loginMsg = { key: "login.code_expired" }; renderAccount(); return null; }
          if (r.res.ok) security.mfa = r.data || null; else security.msg = problemOf(r);
          renderSecurity(); return null;
        }, function () { security.msg = { key: "error.network" }; renderSecurity(); });
      }
      return Promise.all([api("/api/v1/auth/2fa"), api("/api/v1/auth/sessions")]).then(function (rs) {
        if (rs[0].res.ok) security.mfa = rs[0].data || null; else security.msg = problemOf(rs[0]);
        if (rs[1].res.ok) security.sessions = Array.isArray(rs[1].data) ? rs[1].data : []; else security.msg = problemOf(rs[1]);
        renderSecurity();
      }, function () { security.msg = { key: "error.network" }; renderSecurity(); });
    }
    function renderSecurity() {
      var enroll = enrolling(), on = account.name !== null || enroll;
      setHidden($("securityGroup"), !on);
      if (!on) return;
      var mfa = security.mfa || {}, idle = security.step === "idle";
      setHidden($("enrollPanel"), !enroll || security.step !== "idle");
      var left = auth.recoveryLeft(), warn = $("recoveryWarning");
      warn.textContent = left !== null && !enroll ? t("security.recovery_login_warning", { n: left }) : ""; setHidden(warn, !warn.textContent);
      setHidden($("mfaRequired"), !mfa.enabled || !mfa.required);
      var leftEl = $("recoveryLeft");
      leftEl.textContent = mfa.enabled && typeof mfa.recovery_left === "number" ? t("security.recovery_left", { n: mfa.recovery_left }) : "";
      setHidden(leftEl, !leftEl.textContent);
      setHidden($("recoveryNewBtn"), !mfa.enabled || !idle || enroll);
      setHidden($("passwordButtons"), !idle || enroll || !security.mfa);
      setHidden($("passwordForm"), security.step !== "password");
      setHidden($("pwCodeField"), !mfa.enabled);
      $("pwNewLabel").textContent = t("security.password_new", { min: MIN_PASSWORD });
      setHidden($("recoveryForm"), security.step !== "recovery");
      var codes = Array.isArray(security.recovery) ? security.recovery : [], list0 = $("recoveryList");
      clear(list0);
      codes.forEach(function (c) { var li = doc.createElement("li"); li.textContent = String(c); list0.appendChild(li); });
      setHidden($("recoveryBox"), !codes.length);
      setHidden($("sessionsTitle"), enroll); setHidden($("sessionList"), enroll); setHidden($("sessionButtons"), enroll);
      $("mfaStatus").textContent = security.mfa ? (mfa.enabled ? t("security.mfa_on", { date: when(mfa.enabled_at) }) : t("security.mfa_off")) : "";
      setHidden($("mfaUnavailable"), !security.mfa || mfa.enabled || mfa.available !== false);
      setHidden($("mfaEnableBtn"), !security.mfa || mfa.enabled || mfa.available === false || !idle || enroll);
      setHidden($("mfaDisableBtn"), !security.mfa || !mfa.enabled || !idle || !!mfa.required);
      setHidden($("mfaSetupForm"), security.step !== "setup");
      setHidden($("mfaConfirmForm"), security.step !== "confirm");
      setHidden($("mfaDisableForm"), security.step !== "disable");
      $("mfaSecret").textContent = security.step === "confirm" ? (security.secret || "") : "";
      $("mfaLink").setAttribute("href", security.step === "confirm" && /^otpauth:\/\//.test(security.uri || "") ? security.uri : "#");
      var err = $("securityError"), errText = security.msg ? t(security.msg.key, security.msg.params) : "";
      err.textContent = errText; setHidden(err, !errText);
      var done = $("securityDone"), doneText = security.done ? t(security.done.key, security.done.params) : "";
      done.textContent = doneText; setHidden(done, !doneText);
      var list = $("sessionList");
      clear(list);
      security.sessions.forEach(function (s) {
        var li = doc.createElement("li"); li.className = "sess";
        var text = doc.createElement("div"); text.className = "sess__text";
        var name = doc.createElement("b"); name.textContent = deviceText(s); text.appendChild(name);
        var meta = doc.createElement("span"); meta.className = "sess__meta";
        meta.textContent = t(SOURCE_KEYS[s.source] || "security.source_unknown") + " · " +
          t("security.signed_in_at", { time: when(s.created_at) }) + " · " + t("security.active_at", { time: when(s.last_used_at) });
        text.appendChild(meta); li.appendChild(text);
        if (s.current) { var cur = doc.createElement("span"); cur.className = "chip chip--ok"; cur.textContent = t("security.this_session"); li.appendChild(cur); }
        if (s.mfa) { var two = doc.createElement("span"); two.className = "chip chip--notice"; two.textContent = t("security.with_mfa"); li.appendChild(two); }
        var end = doc.createElement("button"); end.className = "btn btn--secondary"; end.setAttribute("type", "button");
        end.setAttribute("data-session", String(s.id)); end.textContent = s.current ? t("settings.sign_out") : t("security.end_session");
        end.addEventListener("click", guard(function () { return endSession(s); }));
        li.appendChild(end); list.appendChild(li);
      });
    }
    function securityStep(step) { security.step = step; security.msg = null; security.done = null; renderSecurity(); }
    function act(promise, onOk) {
      return promise.then(function (r) {
        if (r.res.status === 401 && enrolling()) { auth.setEnrollToken(null); resetSecurity(); loginMsg = { key: "login.code_expired" }; renderAccount(); return null; }
        if (r.res.status === 401) { auth.setToken(null); return refreshAccount(); }
        if (!r.res.ok) { security.msg = problemOf(r); renderSecurity(); return null; }
        security.msg = null; return onOk(r.data || {});
      }, function () { security.msg = { key: "error.network" }; renderSecurity(); });
    }
    function startSetup(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      var pass = $("mfaSetupPass").value; $("mfaSetupPass").value = "";
      return act(api("/api/v1/auth/2fa/setup", "POST", { password: pass }), function (data) {
        security.secret = String(data.secret_grouped || data.secret || ""); security.uri = String(data.otpauth_uri || "");
        security.step = "confirm"; renderSecurity();
        var c = $("mfaConfirmCode"); if (c && c.focus) c.focus();
      });
    }
    function confirmSetup(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      var code = $("mfaConfirmCode").value; $("mfaConfirmCode").value = "";
      return act(api("/api/v1/auth/2fa/confirm", "POST", { code: code }), function (data) {
        security.step = "idle"; security.secret = security.uri = null;
        security.recovery = Array.isArray(data.recovery_codes) ? data.recovery_codes.map(String) : null;
        security.done = { key: "security.mfa_enabled_done", params: { n: Number(data.other_sessions_ended) || 0 } };
        if (typeof data.access_token === "string" && data.access_token) {         // пропуск настройки сменился на обычный сеанс
          auth.setEnrollToken(null); auth.setToken(data.access_token);
          return refreshAccount();
        }
        return loadSecurity();
      });
    }
    function startEnroll() {
      return act(api("/api/v1/auth/2fa/setup", "POST", {}), function (data) {
        security.secret = String(data.secret_grouped || data.secret || ""); security.uri = String(data.otpauth_uri || "");
        security.step = "confirm"; renderSecurity();
        var c = $("mfaConfirmCode"); if (c && c.focus) c.focus();
      });
    }
    function newRecoveryCodes(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      var pass = $("recoveryPass").value, code = $("recoveryCode").value;
      $("recoveryPass").value = ""; $("recoveryCode").value = "";
      return act(api("/api/v1/auth/2fa/recovery-codes", "POST", { password: pass, code: code }), function (data) {
        security.step = "idle"; security.recovery = Array.isArray(data.recovery_codes) ? data.recovery_codes.map(String) : null;
        security.done = { key: "security.recovery_new_done" }; auth.setRecoveryLeft(null);
        return loadSecurity();
      });
    }
    // Смена своего пароля (документ 05, §12.11): текущий пароль, новый дважды, код — если включён второй фактор. Сервер завершает
    // все сеансы и выдаёт этому устройству новый — его токен заменяет прежний.
    function changePassword(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      var cur = $("pwCurrent").value, next = $("pwNew").value, again = $("pwRepeat").value, code = $("pwCode").value;
      if (next !== again) { $("pwRepeat").value = ""; security.msg = { key: "security.password_mismatch" }; renderSecurity(); return Promise.resolve(null); }
      $("pwCurrent").value = ""; $("pwNew").value = ""; $("pwRepeat").value = ""; $("pwCode").value = "";
      var body = { current_password: cur, new_password: next };
      if (code) body.code = code;
      return act(api("/api/v1/auth/password", "POST", body), function (data) {
        if (typeof data.access_token === "string" && data.access_token) auth.setToken(data.access_token);
        security.step = "idle"; security.done = { key: "security.password_changed_done" };
        return loadSecurity();
      });
    }
    function disableMfa(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      var pass = $("mfaDisablePass").value, code = $("mfaDisableCode").value;
      $("mfaDisablePass").value = ""; $("mfaDisableCode").value = "";
      return act(api("/api/v1/auth/2fa/disable", "POST", { password: pass, code: code }), function () {
        security.step = "idle"; security.done = { key: "security.mfa_disabled_done" };
        return loadSecurity();
      });
    }
    function endSession(s) {
      if (s.current) return doLogout();
      return act(api("/api/v1/auth/sessions/" + encodeURIComponent(s.id), "DELETE"), function () {
        security.done = { key: "security.session_ended" }; return loadSecurity();
      });
    }
    function endOthers() {
      return act(api("/api/v1/auth/sessions/revoke-others", "POST"), function (data) {
        security.done = { key: "security.ended_others", params: { n: Number(data.count) || 0 } }; return loadSecurity();
      });
    }
    function logoutEverywhere() {
      return auth.logoutEverywhere().then(function (r) {
        if (!r.ok) { security.msg = r.status ? { key: "login.failed", params: { reason: "HTTP " + r.status } } : { key: "error.network" }; renderSecurity(); return null; }
        account.name = null; account.known = true; loginMsg = null; revealRail([]); resetSecurity(); renderAccount();
        return null;
      });
    }

    function relabel() {
      var mode = Theme.get(), theme = $("themeBtn");
      clear(theme);
      theme.appendChild(Icons.make(doc, mode === "dark" ? "moon" : mode === "light" ? "sun" : "monitor", { size: 20 }));
      theme.setAttribute("aria-label", t("theme.label", { mode: t("theme." + mode) }));
      doc.title = t("nav.settings") + " — " + t("app.name");
    }

    Array.prototype.forEach.call(doc.querySelectorAll("[data-icon]"), function (host) { host.appendChild(Icons.make(doc, host.getAttribute("data-icon"), { size: 22 })); });
    I18N.mountSwitcher($("langHost")); I18N.mountSwitcher($("langHost2"));
    I18N.apply();
    $("rail").setAttribute("aria-label", t("nav.menu"));
    var cfg = readSettings();
    $("autoToggle").checked = cfg.auto;
    relabel(); renderThemeSwitch();
    renderAccount();
    I18N.onChange(function () { relabel(); renderThemeSwitch(); renderAccount(); renderSecurity(); $("rail").setAttribute("aria-label", t("nav.menu")); });

    $("themeBtn").addEventListener("click", function () { Theme.next(); relabel(); renderThemeSwitch(); });
    $("autoToggle").addEventListener("change", storeSettings);
    $("loginPanel").addEventListener("submit", guard(doLogin));
    $("initialPanel").addEventListener("submit", guard(setInitial));
    $("logoutBtn").addEventListener("click", guard(doLogout));
    $("mfaEnableBtn").addEventListener("click", function () { securityStep("setup"); var p = $("mfaSetupPass"); if (p && p.focus) p.focus(); });
    $("mfaDisableBtn").addEventListener("click", function () { securityStep("disable"); var p = $("mfaDisablePass"); if (p && p.focus) p.focus(); });
    $("mfaSetupForm").addEventListener("submit", guard(startSetup));
    $("mfaConfirmForm").addEventListener("submit", guard(confirmSetup));
    $("mfaDisableForm").addEventListener("submit", guard(disableMfa));
    $("endOthersBtn").addEventListener("click", guard(endOthers));
    $("enrollStartBtn").addEventListener("click", guard(startEnroll));
    $("recoveryNewBtn").addEventListener("click", function () { securityStep("recovery"); var p = $("recoveryPass"); if (p && p.focus) p.focus(); });
    $("recoveryForm").addEventListener("submit", guard(newRecoveryCodes));
    $("logoutAllBtn").addEventListener("click", guard(logoutEverywhere));
    // Коды восстановления показаны один раз; после записи их убирают с экрана (общий компьютер магазина), обратно не показать.
    $("recoveryHideBtn").addEventListener("click", function () { security.recovery = null; renderSecurity(); });
    $("passwordChangeBtn").addEventListener("click", function () { securityStep("password"); var p = $("pwCurrent"); if (p && p.focus) p.focus(); });
    $("passwordForm").addEventListener("submit", guard(changePassword));

    return { ready: refreshAccount().then(function (me) { if (enrolling()) return loadSecurity(); return me; }).catch(report) };
  }

  return { start: start, cleanSettings: cleanSettings, SETTINGS_KEY: SETTINGS_KEY };
});
