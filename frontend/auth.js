/* Общий вход: токен, адрес сервера, запросы входа, проверки «кто я» и выхода. Подключается страницами «Узнать товар», «Товары» и «Настройки»
   (раньше один и тот же код был написан в каждой из них отдельно). Проверяется tools/auth_smoke_test.js.
   Токен лежит в sessionStorage под тем же ключом, что у старого меню (nav.js), поэтому вход на любой странице виден остальным; если браузер
   не даёт sessionStorage, токен живёт в памяти страницы. Пароль нигде не сохраняется.
   Тексты здесь не живут: функции возвращают КЛЮЧИ словаря (login.wrong, login.throttled, …), переводит их сама страница.
   Auth.create(win) -> { apiBase(), getToken(), setToken(token), authHeaders(), login(user, pass, code?), me(), logout(), awaitingCode() }
     login(user, pass, code?) -> Promise<{ok: true} | {ok: false, key, params?, needCode?, retryAfter?}>   (при успехе токен сохранён)
       Второй фактор (документ 18): если сервер ответил «нужен код» — {ok: false, needCode: true, key: "login.code_prompt"}; пропуск
       второго шага хранится только в памяти этого объекта. Следующий вызов с тем же логином и кодом отправляет второй шаг;
       неверный код — needCode: true и params.attempts_left; пропуск истёк или попытки кончились — needCode: false (вход заново).
     me()              -> Promise<{status: "anon"} | {status: "expired"} | {status: "error", key} | {status: "ok", me}>
                          (expired: сервер отказал токену — он уже стёрт; me — ответ /auth/me: username, role, permissions)
     logout()          -> Promise<void>   (токен, роль и права стираются сразу; сервер уведомляется по возможности)
     Решения владельца 2026-11-08 (документ 18): администратору без второго фактора сервер даёт пропуск настройки —
       login -> {ok: false, needPasswordChange: true, key: "login.password_change_prompt"} — вход временным паролем (документ 05,
              §12.2): пропуск «смена пароля» хранится отдельно (passwordChange()), сеанса нет; страница ведёт в «Настройки»;
       login -> {ok: false, needEnroll: true, key: "login.enroll_prompt"}; пропуск хранится отдельно (enrollToken()), НЕ как сеанс,
       настройку ведёт страница «Настройки». Вход по коду восстановления -> {ok: true, recoveryLeft: n} (и recoveryLeft()).
     refresh()  -> Promise<{ok, key?}>   обмен токена на новый (предел 8 ч от входа не сдвигается; 401 — войти заново)
     reauth(password, code) -> Promise<{ok, key?, params?}>   подтверждение для важных операций
     logoutEverywhere() -> Promise<{ok: boolean, status?}>  (выйти на всех устройствах: сервер завершает ВСЕ сеансы, затем токен
                          стирается здесь; при отказе сервера токен остаётся — человек увидит, что выйти везде не удалось) */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Auth = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  var TOKEN_KEY = "shelf_audit_token", ROLE_KEY = "shelf_audit_role", PERMS_KEY = "shelf_audit_perms";

  /** Адрес сервера: параметр ?api_base=…, иначе адрес самой страницы (http/https), иначе локальный сервер по умолчанию. */
  function apiBaseOf(loc) {
    loc = loc || {};
    var fromQuery = new URLSearchParams(loc.search || "").get("api_base");
    if (fromQuery) return fromQuery;
    return /^https?:$/.test(loc.protocol || "") ? loc.origin : "http://localhost:8000";
  }

  /** Адрес единой страницы входа с возвратом на эту страницу (редизайн 2026-11-15): login.html?next=products.html&why=expired. */
  function loginUrlOf(loc, why) {
    loc = loc || {};
    var page = String(loc.pathname || "").split("/").pop() || "";
    var next = /^[a-z-]+\.html$/.test(page) && page !== "login.html" ? page + (/^#\/[a-z]+$/.test(loc.hash || "") ? loc.hash : "") : "";
    var q = [];
    if (next) q.push("next=" + encodeURIComponent(next));
    if (why) q.push("why=" + encodeURIComponent(why));
    return "login.html" + (q.length ? "?" + q.join("&") : "");
  }

  function create(win) {
    var memoryToken = null;
    /** Нет сеанса или он закончился — на страницу входа (вернёт обратно после входа). */
    function goLogin(why) {
      var url = loginUrlOf(win.location, why);
      if (win.location && typeof win.location.replace === "function") win.location.replace(url);
      else if (win.location) win.location.href = url;
      return url;
    }
    var pending = null;            // {mfaToken, username, until} — пароль уже проверен сервером, ждём код из приложения

    function getToken() { try { return win.sessionStorage.getItem(TOKEN_KEY) || memoryToken; } catch (e) { return memoryToken; } }
    function setToken(token) {
      memoryToken = token || null;
      try { if (token) win.sessionStorage.setItem(TOKEN_KEY, token); else win.sessionStorage.removeItem(TOKEN_KEY); } catch (e) { /* память */ }
    }
    function authHeaders(opts) {
      // opts.enroll: при отсутствии сеанса подставить пропуск настройки второго фактора (только для страницы «Настройки», документ 18)
      var tk = getToken() || (opts && opts.enroll ? enrollToken() : "");
      return tk ? { Authorization: "Bearer " + tk } : {};
    }
    function apiBase() { return apiBaseOf(win.location); }

    /** Стирает токен и сведения о правах, которые старое меню (nav.js) запоминает рядом с ним. */
    function clearSession() {
      setToken(null);
      try { win.sessionStorage.removeItem(ROLE_KEY); win.sessionStorage.removeItem(PERMS_KEY); } catch (e) { /* память */ }
    }

    var ENROLL_KEY = "shelf_audit_enroll", RECOVERY_KEY = "shelf_audit_recovery_left", PWCHANGE_KEY = "shelf_audit_pwchange";
    var memoryPw = null;
    /** Пропуск «смена временного пароля»: {token, username, codeRequired} или null. */
    function passwordChange() {
      try { var v = win.sessionStorage.getItem(PWCHANGE_KEY); if (v) return JSON.parse(v); } catch (e) { /* память */ }
      return memoryPw;
    }
    function setPasswordChange(value) {
      memoryPw = value || null;
      try { if (value) win.sessionStorage.setItem(PWCHANGE_KEY, JSON.stringify(value)); else win.sessionStorage.removeItem(PWCHANGE_KEY); } catch (e) { /* память */ }
    }
    var memoryEnroll = null;
    function enrollToken() {
      try { var v = win.sessionStorage.getItem(ENROLL_KEY); if (v) return v; } catch (e) { /* память */ }
      return memoryEnroll;
    }
    function setEnrollToken(token) {
      memoryEnroll = token || null;
      try { if (token) win.sessionStorage.setItem(ENROLL_KEY, token); else win.sessionStorage.removeItem(ENROLL_KEY); } catch (e) { /* память */ }
    }
    function recoveryLeft() {
      try { var v = win.sessionStorage.getItem(RECOVERY_KEY); return v === null ? null : parseInt(v, 10); } catch (e) { return null; }
    }
    function setRecoveryLeft(n) {
      try { if (n === null || n === undefined) win.sessionStorage.removeItem(RECOVERY_KEY); else win.sessionStorage.setItem(RECOVERY_KEY, String(n)); } catch (e) { /* память */ }
    }

    function retryAfterOf(res) {
      var raw = res.headers && typeof res.headers.get === "function" ? res.headers.get("Retry-After") : null;
      var secs = parseInt(raw || "", 10);
      return secs > 0 ? secs : null;
    }
    function throttled(res, extra) {
      var out = Object.assign({ ok: false, key: "login.throttled" }, extra || {});
      var secs = retryAfterOf(res);
      if (secs) out.retryAfter = secs;
      return out;
    }
    function bodyOf(res) { return res.json().then(function (b) { return b && typeof b === "object" ? b : {}; }, function () { return {}; }); }
    function serverKey(body, fallback) {
      return body && typeof body.code === "string" && /^[a-z_]+$/.test(body.code) ? "server." + body.code : fallback;
    }

    function awaitingCode() { return !!pending && Date.now() < pending.until; }

    function secondStep(code) {
      var p = pending;
      return Promise.resolve().then(function () {
        return win.fetch(apiBase() + "/api/v1/auth/login/2fa", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mfa_token: p.mfaToken, code: String(code) })
        });
      }).then(function (res) {
        if (res.ok) {
          return res.json().then(function (data) {
            if (!data || !data.access_token) throw new Error("no access_token in response");
            pending = null; setToken(data.access_token);
            if (typeof data.recovery_left === "number") { setRecoveryLeft(data.recovery_left); return { ok: true, recoveryLeft: data.recovery_left }; }
            setRecoveryLeft(null);
            return { ok: true };
          });
        }
        return bodyOf(res).then(function (body) {
          if (res.status === 422) return { ok: false, needCode: true, key: "login.code_wrong", params: body.params || {} };
          if (res.status === 429) return throttled(res, { needCode: true });
          pending = null;                               // 401: пропуск истёк или попытки кончились; 503: нет ключа сервера
          if (res.status === 401) return { ok: false, key: "login.code_expired" };
          return { ok: false, key: serverKey(body, "login.failed"), params: res.status >= 500 && !body.code ? { reason: "HTTP " + res.status } : (body.params || {}) };
        });
      });
    }

    // Решение владельца 2026-11-08: пароль, код и токен — только по HTTPS или через VPN (документ 18, §14). Перед отправкой пароля
    // страница спрашивает сервер, защищён ли канал; по незащищённому пароль НЕ отправляется. Сервер без этого адреса (старый) — как раньше.
    var transportOk = false;
    function checkTransport() {
      if (transportOk) return Promise.resolve(null);
      return Promise.resolve().then(function () { return win.fetch(apiBase() + "/api/v1/auth/transport", { method: "GET" }); }).then(function (res) {
        if (!res.ok) return null;
        return res.json().then(function (t) {
          if (t && t.required && t.secure === false) {
            var url = typeof t.https_url === "string" && /^https:\/\//.test(t.https_url) ? t.https_url : "";
            return url ? { ok: false, insecure: true, key: "login.insecure_transport", params: { url: url } }
                       : { ok: false, insecure: true, key: "login.insecure_transport_nourl" };
          }
          transportOk = true; return null;
        }, function () { return null; });
      }, function () { return null; });
    }
    function login(username, password, code) {
      if (pending && String(code || "").trim() && pending.username === username && awaitingCode()) {
        return secondStep(String(code).trim()).catch(function (e) { return { ok: false, needCode: true, key: "login.failed", params: { reason: e.message } }; });
      }
      pending = null;
      return checkTransport().then(function (refused) {
        if (refused) return refused;
        return loginRequest(username, password);
      });
    }
    function loginRequest(username, password) {
      return Promise.resolve().then(function () {
        return win.fetch(apiBase() + "/api/v1/auth/login", {
          method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ username: username, password: password }).toString()
        });
      }).then(function (res) {
        if (res.status === 401) return { ok: false, key: "login.wrong" };
        if (res.status === 429) return throttled(res);
        if (res.status === 403) return bodyOf(res).then(function (body) { return { ok: false, key: serverKey(body, "admin.err_forbidden") }; });
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json().then(function (data) {
          if (data && data.password_change_required && typeof data.password_token === "string") {
            clearSession(); setEnrollToken(null);
            setPasswordChange({ token: data.password_token, username: data.username || username, codeRequired: !!data.code_required });
            return { ok: false, needPasswordChange: true, key: "login.password_change_prompt" };
          }
          if (data && data.mfa_enrollment_required && typeof data.enroll_token === "string") {
            clearSession(); setEnrollToken(data.enroll_token);
            return { ok: false, needEnroll: true, key: "login.enroll_prompt" };
          }
          if (data && data.mfa_required && typeof data.mfa_token === "string") {
            var secs = data.expires_in > 0 ? data.expires_in : 300;
            pending = { mfaToken: data.mfa_token, username: username, until: Date.now() + secs * 1000 };
            return { ok: false, needCode: true, key: "login.code_prompt" };
          }
          if (!data || !data.access_token) throw new Error("no access_token in response");
          setToken(data.access_token); return { ok: true };
        });
      }).catch(function (e) { return { ok: false, key: "login.failed", params: { reason: e.message } }; });
    }

    function me() {
      if (!getToken()) return Promise.resolve({ status: "anon" });
      return Promise.resolve().then(function () {
        return win.fetch(apiBase() + "/api/v1/auth/me", { headers: authHeaders() });
      }).then(function (res) {
        if (res.status === 401) { clearSession(); return { status: "expired" }; }
        if (!res.ok) return { status: "error", key: "login.check_failed" };
        return res.json().then(function (data) { return { status: "ok", me: data }; });
      }, function () { return { status: "error", key: "error.network" }; })
        .catch(function () { return { status: "error", key: "login.check_failed" }; });
    }

    function logout() {
      var token = getToken();
      clearSession();
      if (!token) return Promise.resolve();
      return Promise.resolve().then(function () {
        return win.fetch(apiBase() + "/api/v1/auth/logout", { method: "POST", headers: { Authorization: "Bearer " + token } });
      }).then(function () { /* ответ не важен: токен уже стёрт здесь */ }, function () { /* нет связи — тоже не страшно */ });
    }

    function postJson(path, body) {
      var headers = authHeaders(); headers["Content-Type"] = "application/json";
      return win.fetch(apiBase() + path, { method: "POST", headers: headers, body: JSON.stringify(body || {}) });
    }

    function refresh() {
      if (!getToken()) return Promise.resolve({ ok: false, key: "login.expired" });
      return Promise.resolve().then(function () { return postJson("/api/v1/auth/refresh"); }).then(function (res) {
        if (res.ok) return res.json().then(function (data) { if (data && data.access_token) setToken(data.access_token); return { ok: !!(data && data.access_token) }; });
        return bodyOf(res).then(function (body) {
          if (res.status === 401) { clearSession(); return { ok: false, key: serverKey(body, "login.expired") }; }
          return { ok: false, key: serverKey(body, "login.failed"), params: body.params || {} };
        });
      }, function () { return { ok: false, key: "error.network" }; });
    }

    function reauth(password, code) {
      return Promise.resolve().then(function () { return postJson("/api/v1/auth/reauth", { password: String(password || ""), code: String(code || "") }); })
        .then(function (res) {
          if (res.ok) return { ok: true };
          return bodyOf(res).then(function (body) {
            if (res.status === 401) { clearSession(); return { ok: false, key: "login.expired" }; }
            if (res.status === 429) return throttled(res);
            return { ok: false, key: serverKey(body, "login.failed"), params: body.params || {} };
          });
        }, function () { return { ok: false, key: "error.network" }; });
    }

    /** Установка своего пароля вместо временного. -> {ok, signedIn} или {ok:false, key, params}. При signedIn сеанс уже сохранён;
        иначе (нужен второй фактор или его настройка) — войти с новым паролем. Пропуск после успеха стирается. */
    function setInitialPassword(newPassword, code) {
      var pass = passwordChange();
      if (!pass || !pass.token) return Promise.resolve({ ok: false, key: "login.expired" });
      var body = { new_password: String(newPassword || "") };
      if (code) body.code = String(code).trim();
      return Promise.resolve().then(function () {
        return win.fetch(apiBase() + "/api/v1/auth/password/initial", {
          method: "POST", headers: { Authorization: "Bearer " + pass.token, "Content-Type": "application/json" }, body: JSON.stringify(body)
        });
      }).then(function (res) {
        if (res.ok) return res.json().then(function (data) {
          setPasswordChange(null);
          if (data && typeof data.access_token === "string" && data.access_token) { setToken(data.access_token); return { ok: true, signedIn: true }; }
          return { ok: true, signedIn: false };
        });
        return bodyOf(res).then(function (b) {
          if (res.status === 401) { setPasswordChange(null); return { ok: false, key: "login.expired" }; }
          if (res.status === 429) return throttled(res);
          return { ok: false, key: serverKey(b, "login.failed"), params: b.params || {} };
        });
      }, function () { return { ok: false, key: "error.network" }; });
    }

    // ---------------- ПИН на этом компьютере (решение владельца 2026-11-15; сервер — app/core/local_pin.py)
    function jsonPost(path, body, withAuth) {
      var headers = withAuth ? authHeaders() : {};
      headers["Content-Type"] = "application/json";
      return win.fetch(apiBase() + path, { method: "POST", headers: headers, body: JSON.stringify(body || {}) });
    }
    /** Ответ с токеном сеанса -> {ok:true} (токен сохранён), иначе {ok:false, key, params, retryAfter?}. */
    function sessionResult(res) {
      if (res.ok) return res.json().then(function (data) {
        if (!data || !data.access_token) throw new Error("no access_token in response");
        setToken(data.access_token); return { ok: true };
      });
      return bodyOf(res).then(function (body) {
        if (res.status === 429) return throttled(res);
        return { ok: false, status: res.status, key: serverKey(body, "login.failed"), params: body.params || {} };
      });
    }
    function failed(e) { return { ok: false, key: "error.network", params: { reason: e && e.message } }; }
    /** {this_computer, users:[{username, role, is_owner}], setup_needed} — что показать на экране входа. Ошибка — «не этот компьютер». */
    function localInfo() {
      return Promise.resolve().then(function () { return win.fetch(apiBase() + "/api/v1/auth/local", { method: "GET" }); })
        .then(function (res) { return res.ok ? res.json() : null; }, function () { return null; })
        .then(function (d) { return d && typeof d === "object" ? d : { this_computer: false, users: [], setup_needed: false }; },
              function () { return { this_computer: false, users: [], setup_needed: false }; });
    }
    function pinLogin(username, pin) {
      pending = null;
      return Promise.resolve().then(function () { return jsonPost("/api/v1/auth/pin", { username: String(username || ""), pin: String(pin || "") }); })
        .then(sessionResult, failed);
    }
    /** Ссылка «Придумайте ПИН»: {valid, username}. */
    function setupCheck(token) {
      return Promise.resolve().then(function () { return jsonPost("/api/v1/auth/setup/check", { token: String(token || "") }); })
        .then(function (res) { return res.ok ? res.json() : { valid: false, username: null }; }, function () { return { valid: false, username: null, offline: true }; });
    }
    function setupPin(token, pin) {
      return Promise.resolve().then(function () { return jsonPost("/api/v1/auth/setup", { token: String(token || ""), pin: String(pin || "") }); })
        .then(sessionResult, failed);
    }
    function reauthPin(pin) {
      return Promise.resolve().then(function () { return jsonPost("/api/v1/auth/reauth", { pin: String(pin || "") }, true); })
        .then(function (res) {
          if (res.ok) return { ok: true };
          return bodyOf(res).then(function (body) {
            if (res.status === 401) { clearSession(); return { ok: false, key: "login.expired" }; }
            if (res.status === 429) return throttled(res);
            return { ok: false, key: serverKey(body, "login.failed"), params: body.params || {} };
          });
        }, failed);
    }
    /** Задать или сменить свой ПИН (current — текущий, если уже был). */
    function setPin(pin, current) {
      var body = { pin: String(pin || "") };
      if (current) body.current_pin = String(current);
      return Promise.resolve().then(function () {
        var headers = authHeaders(); headers["Content-Type"] = "application/json";
        return win.fetch(apiBase() + "/api/v1/auth/pin", { method: "PUT", headers: headers, body: JSON.stringify(body) });
      }).then(function (res) {
        if (res.ok) return { ok: true };
        return bodyOf(res).then(function (b) {
          if (res.status === 401) { clearSession(); return { ok: false, key: "login.expired" }; }
          if (res.status === 429) return throttled(res);
          return { ok: false, key: serverKey(b, "login.failed"), params: b.params || {} };
        });
      }, failed);
    }

    function logoutEverywhere() {
      var token = getToken();
      if (!token) return Promise.resolve({ ok: true });
      return Promise.resolve().then(function () {
        return win.fetch(apiBase() + "/api/v1/auth/logout-all", { method: "POST", headers: { Authorization: "Bearer " + token } });
      }).then(function (res) {
        if (res.ok || res.status === 401) { clearSession(); return { ok: true }; }       // 401 — сеанса уже нет: тоже «вышли»
        return { ok: false, status: res.status };
      }, function () { return { ok: false }; });
    }

    return { apiBase: apiBase, getToken: getToken, setToken: setToken, authHeaders: authHeaders, login: login, me: me, logout: logout,
             logoutEverywhere: logoutEverywhere, awaitingCode: awaitingCode, enrollToken: enrollToken, setEnrollToken: setEnrollToken,
             recoveryLeft: recoveryLeft, setRecoveryLeft: setRecoveryLeft, refresh: refresh, reauth: reauth,
             passwordChange: passwordChange, setPasswordChange: setPasswordChange, setInitialPassword: setInitialPassword,
             checkTransport: checkTransport, localInfo: localInfo, pinLogin: pinLogin, setupCheck: setupCheck, setupPin: setupPin,
             reauthPin: reauthPin, setPin: setPin, goLogin: goLogin };
  }

  return { TOKEN_KEY: TOKEN_KEY, ROLE_KEY: ROLE_KEY, PERMS_KEY: PERMS_KEY, apiBaseOf: apiBaseOf, loginUrlOf: loginUrlOf, create: create };
});
