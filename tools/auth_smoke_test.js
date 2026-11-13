// Проверка общего входа (frontend/auth.js) без браузера и сервера: адрес сервера, хранение токена (в том числе когда браузер не даёт sessionStorage),
// все исходы запроса входа, проверка «кто я», выход. Сервер и хранилище — подделки, которыми управляет тест. Данные выдуманные.
// Не проверяется: настоящий сервер и настоящие браузеры (sessionStorage в приватном режиме и т. п.).
// Запуск из корня проекта:  node tools/auth_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const Auth = require(path.join(__dirname, "..", "frontend", "auth.js"));
const AppNav = require(path.join(__dirname, "..", "frontend", "nav.js"));

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }

/** Подделка окна: sessionStorage (можно сломать), адрес страницы и сервер, отвечающий по очереди заготовленными ответами. */
function makeWin(opts = {}) {
  const store = new Map(), calls = [], queue = (opts.replies || []).slice();
  const sessionStorage = opts.brokenStorage ? {
    getItem() { throw new Error("storage denied"); }, setItem() { throw new Error("storage denied"); }, removeItem() { throw new Error("storage denied"); }
  } : {
    getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => { store.set(k, String(v)); }, removeItem: (k) => { store.delete(k); }
  };
  const win = {
    sessionStorage, store, calls,
    location: opts.location || { protocol: "https:", origin: "https://catalog.example.uz", search: "" },
    transportCalls: 0,
    fetch(url, init) {
      // «Можно ли отправлять пароль отсюда» (документ 18, §14) — отдельный ответ, не из очереди: по умолчанию канал защищён.
      if (String(url).endsWith("/api/v1/auth/transport")) {
        win.transportCalls += 1;
        const t = opts.transport === undefined ? { secure: true, required: true, https_url: null } : opts.transport;
        if (t instanceof Error) return Promise.reject(t);
        if (typeof t === "number") return Promise.resolve({ status: t, ok: false, json: () => Promise.resolve({}) });
        return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve(t) });
      }
      calls.push({ url, init: init || {} });
      const next = queue.shift();
      if (next === undefined) return Promise.reject(new Error("неожиданный запрос: " + url));
      if (next instanceof Error) return Promise.reject(next);
      return Promise.resolve({
        status: next.status, ok: next.status >= 200 && next.status < 300,
        json: () => (next.badJson ? Promise.reject(new SyntaxError("Unexpected token")) : Promise.resolve(next.json))
      });
    }
  };
  return win;
}
const R = (status, json, extra) => Object.assign({ status, json }, extra || {});

(async () => {
  await test("адрес сервера: ?api_base= главнее всего; иначе адрес самой страницы (http/https); иначе локальный сервер", async () => {
    assert.strictEqual(Auth.apiBaseOf({ protocol: "https:", origin: "https://a.uz", search: "?api_base=http://10.0.0.5:8000" }), "http://10.0.0.5:8000");
    assert.strictEqual(Auth.apiBaseOf({ protocol: "http:", origin: "http://192.168.1.50:8000", search: "" }), "http://192.168.1.50:8000");
    assert.strictEqual(Auth.apiBaseOf({ protocol: "file:", origin: "null", search: "" }), "http://localhost:8000", "страница открыта файлом");
    assert.strictEqual(Auth.apiBaseOf({ protocol: "file:", search: "?api_base=" }), "http://localhost:8000", "пустой api_base не считается");
    assert.strictEqual(Auth.apiBaseOf(undefined), "http://localhost:8000");
    assert.strictEqual(Auth.create(makeWin()).apiBase(), "https://catalog.example.uz");
  });

  await test("токен: запись, чтение, заголовок Authorization; пустое значение стирает; ключи те же, что у старого меню (nav.js)", async () => {
    const win = makeWin(), a = Auth.create(win);
    assert.strictEqual(a.getToken(), null); assert.deepStrictEqual(a.authHeaders(), {});
    a.setToken("tok-1");
    assert.strictEqual(win.store.get("shelf_audit_token"), "tok-1"); assert.strictEqual(a.getToken(), "tok-1");
    assert.deepStrictEqual(a.authHeaders(), { Authorization: "Bearer tok-1" });
    a.setToken(null); assert.strictEqual(a.getToken(), null); assert.ok(!win.store.has("shelf_audit_token"));
    assert.strictEqual(Auth.TOKEN_KEY, AppNav.KEYS.TOKEN_KEY); assert.strictEqual(Auth.ROLE_KEY, AppNav.KEYS.ROLE_KEY); assert.strictEqual(Auth.PERMS_KEY, AppNav.KEYS.PERMS_KEY);
  });

  await test("браузер не даёт sessionStorage: токен живёт в памяти страницы, ничего не падает", async () => {
    const a = Auth.create(makeWin({ brokenStorage: true }));
    a.setToken("tok-2"); assert.strictEqual(a.getToken(), "tok-2"); assert.deepStrictEqual(a.authHeaders(), { Authorization: "Bearer tok-2" });
    a.setToken(null); assert.strictEqual(a.getToken(), null);
  });

  await test("вход: успех — запрос POST в форме, логин и пароль закодированы, токен сохранён, пароль нигде не остаётся", async () => {
    const win = makeWin({ replies: [R(200, { access_token: "tok-3", token_type: "bearer" })] }), a = Auth.create(win);
    const r = await a.login("м.каримов", "p&ss=wo rd+1");
    assert.deepStrictEqual(r, { ok: true });
    assert.strictEqual(a.getToken(), "tok-3"); assert.strictEqual(win.store.get("shelf_audit_token"), "tok-3");
    const c = win.calls[0];
    assert.strictEqual(c.url, "https://catalog.example.uz/api/v1/auth/login"); assert.strictEqual(c.init.method, "POST");
    assert.strictEqual(c.init.headers["Content-Type"], "application/x-www-form-urlencoded");
    const body = new URLSearchParams(c.init.body);
    assert.strictEqual(body.get("username"), "м.каримов"); assert.strictEqual(body.get("password"), "p&ss=wo rd+1", "спецсимволы доходят целыми");
    assert.ok(![...win.store.values()].some((v) => v.includes("p&ss")), "пароль не сохраняется");
  });

  await test("вход: неверный пароль, слишком много попыток, ошибка сервера, нет связи, битый ответ, ответ без токена — ключи словаря, токен не сохраняется", async () => {
    const cases = [
      [R(401, {}), { ok: false, key: "login.wrong" }],
      [R(429, {}), { ok: false, key: "login.throttled" }],
      [R(500, {}), { ok: false, key: "login.failed", params: { reason: "HTTP 500" } }],
      [new Error("Failed to fetch"), { ok: false, key: "login.failed", params: { reason: "Failed to fetch" } }],
      [R(200, null, { badJson: true }), { ok: false, key: "login.failed", params: { reason: "Unexpected token" } }],
      [R(200, { token_type: "bearer" }), { ok: false, key: "login.failed", params: { reason: "no access_token in response" } }]
    ];
    for (const [reply, want] of cases) {
      const win = makeWin({ replies: [reply] }), a = Auth.create(win);
      assert.deepStrictEqual(await a.login("u", "p"), want); assert.strictEqual(a.getToken(), null, "токен не появился");
    }
  });

  await test("второй фактор: пароль → «нужен код» (токена нет), код → второй шаг JSON с пропуском, токен сохранён", async () => {
    const win = makeWin({ replies: [R(200, { mfa_required: true, mfa_token: "pass-123456789", expires_in: 300, username: "boss" }),
                                    R(200, { access_token: "tok-2fa", token_type: "bearer", expires_in: 60 })] });
    const a = Auth.create(win);
    assert.deepStrictEqual(await a.login("boss", "secret-pass"), { ok: false, needCode: true, key: "login.code_prompt" });
    assert.strictEqual(a.getToken(), null); assert.ok(a.awaitingCode());
    assert.deepStrictEqual(await a.login("boss", "secret-pass", " 123 456 "), { ok: true });
    assert.strictEqual(a.getToken(), "tok-2fa"); assert.ok(!a.awaitingCode());
    const second = win.calls[1];
    assert.ok(second.url.endsWith("/api/v1/auth/login/2fa") && second.init.method === "POST");
    assert.strictEqual(second.init.headers["Content-Type"], "application/json");
    assert.deepStrictEqual(JSON.parse(second.init.body), { mfa_token: "pass-123456789", code: "123 456" });
    assert.ok(!second.init.body.includes("secret-pass"), "пароль во второй шаг не уходит");
  });

  await test("второй фактор: неверный код — снова «нужен код» с остатком попыток; истёк или попытки кончились — вход заново с паролем", async () => {
    const win = makeWin({ replies: [R(200, { mfa_required: true, mfa_token: "pass-123456789", expires_in: 300 }),
                                    R(422, { detail: "Код не подошёл", code: "mfa_code_wrong", params: { attempts_left: 4 } }),
                                    R(401, { detail: "Время истекло", code: "mfa_expired", params: {} }),
                                    R(401, {})] });
    const a = Auth.create(win);
    await a.login("boss", "p");
    assert.deepStrictEqual(await a.login("boss", "p", "000000"), { ok: false, needCode: true, key: "login.code_wrong", params: { attempts_left: 4 } });
    assert.deepStrictEqual(await a.login("boss", "p", "111111"), { ok: false, key: "login.code_expired" });
    assert.ok(!a.awaitingCode());
    assert.deepStrictEqual(await a.login("boss", "wrong"), { ok: false, key: "login.wrong" }, "без пропуска — снова первый шаг");
    assert.ok(win.calls[3].url.endsWith("/api/v1/auth/login"));
  });

  await test("второй фактор: другой логин или пустой код — первый шаг заново; отказ «настройте 2FA на компьютере» — ключ кода сервера", async () => {
    const win = makeWin({ replies: [R(200, { mfa_required: true, mfa_token: "pass-123456789", expires_in: 300 }),
                                    R(200, { access_token: "tok-other" }),
                                    R(403, { detail: "…", code: "mfa_enrollment_required", params: {} })] });
    const a = Auth.create(win);
    await a.login("boss", "p");
    assert.deepStrictEqual(await a.login("other", "p2", "123456"), { ok: true });
    assert.ok(win.calls[1].url.endsWith("/api/v1/auth/login"), "код к чужому пропуску не прикладывается");
    assert.deepStrictEqual(await a.login("admin", "p"), { ok: false, key: "server.mfa_enrollment_required" });
  });

  await test("выйти на всех устройствах: POST /auth/logout-all с текущим токеном, затем токен стёрт; отказ сервера — токен остаётся", async () => {
    const win = makeWin({ replies: [R(200, {}), R(500, {})] }), a = Auth.create(win);
    a.setToken("tok-1");
    assert.deepStrictEqual(await a.logoutEverywhere(), { ok: true });
    assert.ok(win.calls[0].url.endsWith("/api/v1/auth/logout-all") && win.calls[0].init.headers.Authorization === "Bearer tok-1");
    assert.strictEqual(a.getToken(), null);
    a.setToken("tok-2");
    assert.deepStrictEqual(await a.logoutEverywhere(), { ok: false, status: 500 }); assert.strictEqual(a.getToken(), "tok-2");
    const none = makeWin(); assert.deepStrictEqual(await Auth.create(none).logoutEverywhere(), { ok: true }); assert.strictEqual(none.calls.length, 0);
  });

  await test("администратор без второго фактора: пропуск настройки хранится отдельно, сеансом не считается", async () => {
    const win = makeWin({ replies: [R(200, { mfa_enrollment_required: true, enroll_token: "enroll-123", expires_in: 600, username: "boss" })] });
    const a = Auth.create(win);
    a.setToken("old-token");
    assert.deepStrictEqual(await a.login("boss", "p"), { ok: false, needEnroll: true, key: "login.enroll_prompt" });
    assert.strictEqual(a.getToken(), null, "прежний токен стёрт, пропуск — не сеанс");
    assert.strictEqual(a.enrollToken(), "enroll-123");
    assert.deepStrictEqual(a.authHeaders(), {}, "пропуск в обычные запросы не подставляется");
    assert.deepStrictEqual(a.authHeaders({ enroll: true }), { Authorization: "Bearer enroll-123" }, "только по явной просьбе страницы настроек");
    a.setToken("tok-full");
    assert.deepStrictEqual(a.authHeaders({ enroll: true }), { Authorization: "Bearer tok-full" }, "при живом сеансе — всегда сеанс");
    a.setToken(null);
    a.setEnrollToken(null); assert.strictEqual(a.enrollToken(), null);
  });

  await test("временный пароль: пропуск «смена пароля» отдельно от сеанса; установка пароля — с пропуском; дальше сеанс или вход заново", async () => {
    const win = makeWin({ replies: [R(200, { password_change_required: true, password_token: "pw-pass-1", expires_in: 900, username: "m.karimov", code_required: true }),
                                    R(422, { detail: "x", code: "password_same", params: {} }),
                                    R(200, { status: "password_set", next: "login" }),
                                    R(200, { password_change_required: true, password_token: "pw-pass-2", expires_in: 900, username: "m.karimov" }),
                                    R(200, { access_token: "tok-new", token_type: "bearer" }),
                                    R(200, { password_change_required: true, password_token: "pw-pass-3", expires_in: 900, username: "x" }),
                                    R(401, { detail: "x", code: "auth_required", params: {} })] });
    const a = Auth.create(win);
    a.setToken("old");
    assert.deepStrictEqual(await a.login("m.karimov", "7KQM-X4HP-9TDW"), { ok: false, needPasswordChange: true, key: "login.password_change_prompt" });
    assert.strictEqual(a.getToken(), null, "пропуск — не сеанс, прежний токен стёрт");
    assert.deepStrictEqual(a.passwordChange(), { token: "pw-pass-1", username: "m.karimov", codeRequired: true });
    assert.deepStrictEqual(a.authHeaders(), {}, "в обычные запросы пропуск не подставляется");
    assert.deepStrictEqual(await a.setInitialPassword("7KQM-X4HP-9TDW", "123456"), { ok: false, key: "server.password_same", params: {} });
    const call = win.calls[1];
    assert.ok(call.url.endsWith("/api/v1/auth/password/initial") && call.init.headers.Authorization === "Bearer pw-pass-1");
    assert.deepStrictEqual(JSON.parse(call.init.body), { new_password: "7KQM-X4HP-9TDW", code: "123456" });
    assert.ok(a.passwordChange(), "после отказа пропуск остаётся — можно попробовать ещё");
    assert.deepStrictEqual(await a.setInitialPassword("my-own-pass-1", " 654321 "), { ok: true, signedIn: false });
    assert.strictEqual(a.passwordChange(), null); assert.strictEqual(a.getToken(), null, "нужен второй фактор — сеанса нет, вход заново");
    await a.login("m.karimov", "7KQM");
    assert.deepStrictEqual(await a.setInitialPassword("my-own-pass-2"), { ok: true, signedIn: true });
    assert.strictEqual(a.getToken(), "tok-new");
    assert.deepStrictEqual(JSON.parse(win.calls[4].init.body), { new_password: "my-own-pass-2" }, "без второго фактора код не шлётся");
    await a.login("x", "y");
    assert.deepStrictEqual(await a.setInitialPassword("whatever-123"), { ok: false, key: "login.expired" });
    assert.strictEqual(a.passwordChange(), null, "истёкший пропуск стёрт");
    assert.deepStrictEqual(await Auth.create(makeWin()).setInitialPassword("p"), { ok: false, key: "login.expired" });
  });

  await test("незащищённый канал (HTTP через Wi-Fi): пароль не отправляется вовсе; сервер подсказывает защищённый адрес; старый сервер — как раньше", async () => {
    const insecure = makeWin({ transport: { secure: false, required: true, https_url: "https://192.168.1.10:8000" } });
    const a = Auth.create(insecure);
    assert.deepStrictEqual(await a.login("boss", "secret-pass"), { ok: false, insecure: true, key: "login.insecure_transport", params: { url: "https://192.168.1.10:8000" } });
    assert.strictEqual(insecure.calls.length, 0, "запроса с паролем не было");
    const nourl = makeWin({ transport: { secure: false, required: true, https_url: "javascript:alert(1)" } });
    assert.deepStrictEqual(await Auth.create(nourl).login("boss", "p"), { ok: false, insecure: true, key: "login.insecure_transport_nourl" });
    const notRequired = makeWin({ transport: { secure: false, required: false }, replies: [R(200, { access_token: "tok" })] });
    assert.deepStrictEqual(await Auth.create(notRequired).login("boss", "p"), { ok: true });
    const old = makeWin({ transport: 404, replies: [R(200, { access_token: "tok" })] });
    assert.deepStrictEqual(await Auth.create(old).login("boss", "p"), { ok: true });
    const ok = makeWin({ replies: [R(200, { access_token: "t1" }), R(200, { access_token: "t2" })] }), b = Auth.create(ok);
    await b.login("a", "p"); await b.login("a", "p");
    assert.strictEqual(ok.transportCalls, 1, "защищённый канал проверяется один раз за страницу");
  });

  await test("вход по коду восстановления: сколько кодов осталось — в ответе и в хранилище вкладки; обычный вход это стирает", async () => {
    const win = makeWin({ replies: [R(200, { mfa_required: true, mfa_token: "pass-123456789", expires_in: 300 }),
                                    R(200, { access_token: "tok-r", recovery_left: 7 }),
                                    R(200, { mfa_required: true, mfa_token: "pass-987654321", expires_in: 300 }),
                                    R(200, { access_token: "tok-n" })] });
    const a = Auth.create(win);
    await a.login("boss", "p");
    assert.deepStrictEqual(await a.login("boss", "p", "7KQ4M-P2XJR"), { ok: true, recoveryLeft: 7 });
    assert.strictEqual(a.recoveryLeft(), 7);
    await a.login("boss", "p"); assert.deepStrictEqual(await a.login("boss", "p", "123456"), { ok: true });
    assert.strictEqual(a.recoveryLeft(), null);
  });

  await test("продление сеанса: новый токен вместо старого; предел достигнут — токен стёрт; подтверждение для важных операций", async () => {
    const win = makeWin({ replies: [R(200, { access_token: "tok-2" }), R(401, { code: "session_max_age" }),
                                    R(200, { status: "confirmed" }), R(422, { code: "mfa_code_wrong" })] });
    const a = Auth.create(win);
    a.setToken("tok-1");
    assert.deepStrictEqual(await a.refresh(), { ok: true }); assert.strictEqual(a.getToken(), "tok-2");
    assert.ok(win.calls[0].url.endsWith("/api/v1/auth/refresh") && win.calls[0].init.headers.Authorization === "Bearer tok-1");
    assert.deepStrictEqual(await a.refresh(), { ok: false, key: "server.session_max_age" }); assert.strictEqual(a.getToken(), null);
    a.setToken("tok-3");
    assert.deepStrictEqual(await a.reauth("pw", "123456"), { ok: true });
    assert.deepStrictEqual(JSON.parse(win.calls[2].init.body), { password: "pw", code: "123456" });
    assert.deepStrictEqual(await a.reauth("pw", "000000"), { ok: false, key: "server.mfa_code_wrong", params: {} });
    assert.strictEqual(a.getToken(), "tok-3", "неверный код не стирает сеанс");
  });

  await test("«кто я»: без токена запроса нет; с токеном — Bearer и ответ сервера; отказ 401 стирает токен, роль и права; сбои — ключи словаря", async () => {
    const none = makeWin(), a0 = Auth.create(none);
    assert.deepStrictEqual(await a0.me(), { status: "anon" }); assert.strictEqual(none.calls.length, 0);

    const ok = makeWin({ replies: [R(200, { username: "admin1", role: "admin", permissions: ["catalog:read"] })] }), a1 = Auth.create(ok);
    a1.setToken("tok-4");
    assert.deepStrictEqual(await a1.me(), { status: "ok", me: { username: "admin1", role: "admin", permissions: ["catalog:read"] } });
    assert.strictEqual(ok.calls[0].url, "https://catalog.example.uz/api/v1/auth/me"); assert.deepStrictEqual(ok.calls[0].init.headers, { Authorization: "Bearer tok-4" });

    const exp = makeWin({ replies: [R(401, {})] }), a2 = Auth.create(exp);
    a2.setToken("old"); exp.store.set("shelf_audit_role", "admin"); exp.store.set("shelf_audit_perms", "[\"x\"]");
    assert.deepStrictEqual(await a2.me(), { status: "expired" });
    assert.ok(exp.store.size === 0 && a2.getToken() === null, "после 401 токен, роль и права стёрты");

    for (const [reply, want] of [[R(500, {}), { status: "error", key: "login.check_failed" }], [new Error("down"), { status: "error", key: "error.network" }], [R(200, null, { badJson: true }), { status: "error", key: "login.check_failed" }]]) {
      const w = makeWin({ replies: [reply] }), a = Auth.create(w); a.setToken("t");
      assert.deepStrictEqual(await a.me(), want); assert.strictEqual(a.getToken(), "t", "при сбое связи токен не стирается");
    }
  });

  await test("выход: токен, роль и права стираются сразу (до ответа сервера), серверу уходит прежний токен; сбой сервера выход не ломает", async () => {
    let release; const gate = new Promise((r) => { release = r; });
    const win = makeWin({ replies: [R(200, { ok: true })] });
    const realFetch = win.fetch; win.fetch = (u, i) => gate.then(() => realFetch(u, i));
    const a = Auth.create(win); a.setToken("tok-5"); win.store.set("shelf_audit_role", "admin"); win.store.set("shelf_audit_perms", "[]");
    const pending = a.logout();
    assert.strictEqual(a.getToken(), null, "стёрт сразу"); assert.strictEqual(win.store.size, 0);
    release(); await pending;
    assert.strictEqual(win.calls.length, 1); assert.strictEqual(win.calls[0].url, "https://catalog.example.uz/api/v1/auth/logout");
    assert.strictEqual(win.calls[0].init.method, "POST"); assert.deepStrictEqual(win.calls[0].init.headers, { Authorization: "Bearer tok-5" });

    const down = makeWin({ replies: [new Error("offline")] }), b = Auth.create(down); b.setToken("t");
    await b.logout(); assert.strictEqual(b.getToken(), null);
    const nobody = makeWin(), c = Auth.create(nobody); await c.logout(); assert.strictEqual(nobody.calls.length, 0, "не входили — серверу не пишем");
  });

  await test("в разметку и память браузера лишнего не попадает: токен не в localStorage, нет innerHTML/eval и вывода в консоль", async () => {
    const src = fs.readFileSync(path.join(__dirname, "..", "frontend", "auth.js"), "utf-8");
    for (const bad of ["localStorage", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "console."]) assert.ok(!src.includes(bad), bad);
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
