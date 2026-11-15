// Единая страница входа (frontend/login.html + login.js, редизайн 2026-11-15) без браузера: настоящая разметка — в подделку страницы,
// сервер — подделка fetch. Проверяется: на этом компьютере — ПИН (несколько людей — выбор), с другого устройства — логин и пароль (и код);
// «Придумайте ПИН» по ссылке из окна программы (два ввода, несовпадение, ссылка не остаётся в адресе); возврат на ?next= только на свои
// страницы; тексты на всех языках; нет подстановки разметки.  Запуск из корня проекта:  node tools/login_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { FakeDocument, parseHtml, makeStorage } = require("./fake_dom.js");
const FRONT = path.join(__dirname, "..", "frontend");
const I18N = require(path.join(FRONT, "i18n.js")), Icons = require(path.join(FRONT, "icons.js")), Auth = require(path.join(FRONT, "auth.js"));
const Login = require(path.join(FRONT, "login.js"));
const HTML = fs.readFileSync(path.join(FRONT, "login.html"), "utf-8");
const SRC = fs.readFileSync(path.join(FRONT, "login.js"), "utf-8");
const DICTS = { ru: require(path.join(FRONT, "i18n.ru.js")), uz: require(path.join(FRONT, "i18n.uz.js")),
                "uz-Cyrl": require(path.join(FRONT, "i18n.uz-cyrl.js")), en: require(path.join(FRONT, "i18n.en.js")) };

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; console.log("ok  ", name); } catch (err) { console.error("FAIL:", name); throw err; } }
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0)); };
const json = (status, body, headers) => ({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body),
                                           headers: { get: (k) => (headers || {})[k] || null } });
const OWNER = { username: "admin", role: "admin", is_owner: true }, KASSIR = { username: "kassir", role: "merchandiser", is_owner: false };
const ADMIN_ME = { username: "admin", role: "admin", permissions: ["catalog:read", "catalog:write", "store:manage"] };

function boot(opts) {
  opts = opts || {};
  const doc = new FakeDocument(); parseHtml(doc, HTML);
  const i18n = I18N.create({ localStorage: makeStorage(), navigator: { languages: [opts.lang || "ru"] }, document: doc });
  for (const [c, d] of Object.entries(DICTS)) i18n.add(c, d);
  const calls = [], replaced = [], history = [];
  const server = Object.assign({
    "/auth/local": () => json(200, { this_computer: true, users: [OWNER], setup_needed: false }),
    "/auth/transport": () => json(200, { secure: true, required: true }),
    "/auth/me": () => json(200, ADMIN_ME),
  }, opts.server || {});
  const listeners = {};
  const win = {
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    fire: (type) => (listeners[type] || []).forEach((fn) => fn({})),
    document: doc, sessionStorage: makeStorage(opts.session), localStorage: makeStorage(),
    location: { protocol: "http:", origin: "http://localhost:8000", pathname: "/ui/login.html", search: opts.search || "", hash: opts.hash || "",
                replace: (u) => replaced.push(u), assign: (u) => replaced.push("assign:" + u) },
    history: { replaceState: (a, b, url) => { history.push(url); win.location.hash = ""; } },
    fetch: (url, init) => {
      const p = String(url).replace(/^.*\/api\/v1/, "");
      calls.push({ p, init: init || {} });
      const h = server[p];
      return h ? Promise.resolve(h(init || {})) : Promise.reject(new Error("нет ответа для " + p));
    },
  };
  const theme = { mode: "light", get() { return this.mode; }, next() { this.mode = "light"; } };
  const app = Login.start(win, { I18N: i18n, Auth, Icons, Theme: theme });
  const $ = (id) => doc.getElementById(id);
  return { doc, win, i18n, app, $, calls, replaced, history, shown: (id) => !$(id).hasAttribute("hidden"),
           ready: async () => { await app.ready; await flush(); } };
}
const submit = async (s, id) => { s.$(id).dispatch("submit", { preventDefault() {} }); await flush(); };
const type = (s, id, value) => { s.$(id).value = value; s.$(id).dispatch("input"); };

(async () => {
  await test("правила: ?next= — только свои страницы; стартовая страница по правам; токен ссылки; только цифры", () => {
    assert.strictEqual(Login.safeNext("?next=admin.html%23%2Fusers"), "admin.html#/users");
    assert.strictEqual(Login.safeNext("?next=products.html"), "products.html");
    for (const bad of ["?next=https://evil.example", "?next=//evil", "?next=shop.html", "?next=login.html", "?next=admin.html%23%2Fusers%22", ""])
      assert.strictEqual(Login.safeNext(bad), null, bad);
    assert.strictEqual(Login.startPage(["catalog:write", "store:manage"]), "products.html");
    assert.strictEqual(Login.startPage(["store:manage"]), "admin.html");
    assert.strictEqual(Login.startPage(["catalog:read"]), "identify.html");
    assert.strictEqual(Login.startPage(null), "identify.html");
    assert.strictEqual(Login.setupToken("#setup=AbC_123-xyzQ"), "AbC_123-xyzQ");
    assert.strictEqual(Login.setupToken("#setup=<b>"), null);
    assert.strictEqual(Login.onlyDigits(" 48-29 17x9"), "482917");
  });

  await test("разметка: id, которые трогает login.js, есть и не повторяются; нет встроенных скриптов и подстановки разметки; тексты во всех языках", () => {
    const markup = HTML.replace(/<script[\s\S]*?<\/script>/gi, "");
    const ids = [...markup.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
    assert.strictEqual(new Set(ids).size, ids.length);
    for (const id of new Set([...SRC.matchAll(/\$\("([A-Za-z][\w-]*)"\)/g)].map((m) => m[1]))) assert.ok(ids.includes(id), `в разметке нет #${id}`);
    assert.deepStrictEqual([...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)], []);
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"]) assert.ok(!SRC.includes(bad), bad);
    const keys = new Set([...HTML.matchAll(/data-i18n="([^"]+)"/g)].map((m) => m[1]).concat([...SRC.matchAll(/"((?:signin|login|server|error|theme)\.[a-z_]+)"/g)].map((m) => m[1])));
    for (const k of keys) for (const [c, d] of Object.entries(DICTS)) assert.ok(d[k] !== undefined, `${c}: нет ${k}`);
  });

  await test("этот компьютер: «Здравствуйте, владелец!», ввод 6 цифр сам отправляет ПИН, вход — на ?next=", async () => {
    const s = boot({ search: "?next=admin.html%23%2Fusers", server: { "/auth/pin": () => json(200, { access_token: "pin-tok" }) } });
    await s.ready();
    assert.ok(s.shown("pinView") && !s.shown("passView") && !s.shown("setupView") && !s.shown("userPicker"));
    assert.strictEqual(s.$("pinHello").textContent, "Здравствуйте, владелец!");
    type(s, "pinInput", "48a29"); assert.strictEqual(s.$("pinInput").value, "4829", "только цифры");
    type(s, "pinInput", "482917"); await flush();
    const pin = s.calls.find((c) => c.p === "/auth/pin");
    assert.deepStrictEqual(JSON.parse(pin.init.body), { username: "admin", pin: "482917" });
    assert.strictEqual(s.win.sessionStorage.getItem("shelf_audit_token"), "pin-tok");
    assert.deepStrictEqual(s.replaced, ["admin.html#/users"]);
    assert.strictEqual(s.$("pinInput").value, "", "ПИН не остаётся в поле");
  });

  await test("неверный ПИН — понятная строка (и на другом языке); много ошибок — «попробуйте через N мин»; без ?next= — страница по правам", async () => {
    let reply = json(401, { code: "pin_wrong" });
    const s = boot({ server: { "/auth/pin": () => reply } });
    await s.ready();
    type(s, "pinInput", "000001"); await flush();
    assert.strictEqual(s.$("pinError").textContent, DICTS.ru["server.pin_wrong"]);
    s.i18n.setLang("en"); await flush(); assert.strictEqual(s.$("pinError").textContent, DICTS.en["server.pin_wrong"]);
    reply = json(429, {}, { "Retry-After": "600" });
    type(s, "pinInput", "000002"); await flush();
    assert.strictEqual(s.$("pinError").textContent, DICTS.en["signin.wait"].replace("{min}", "10"));
    reply = json(200, { access_token: "t" });
    type(s, "pinInput", "482917"); await flush();
    assert.deepStrictEqual(s.replaced, ["products.html"]);
  });

  await test("несколько человек с ПИН: выбор кнопками, ПИН уходит для выбранного; «Забыли ПИН?» — подсказка про файл «Забыли ПИН»", async () => {
    const s = boot({ server: { "/auth/local": () => json(200, { this_computer: true, users: [KASSIR, OWNER] }), "/auth/pin": () => json(401, { code: "pin_wrong" }) } });
    await s.ready();
    assert.ok(s.shown("userPicker"));
    const chips = s.$("userPicker").byTag("button");
    assert.deepStrictEqual(chips.map((b) => b.textContent), ["kassir", "владелец"]);
    assert.strictEqual(chips[1].getAttribute("aria-checked"), "true", "по умолчанию — владелец");
    chips[0].click(); await flush();
    type(s, "pinInput", "482917"); await flush();
    assert.strictEqual(JSON.parse(s.calls.find((c) => c.p === "/auth/pin").init.body).username, "kassir");
    assert.ok(!s.shown("forgotHint")); s.$("forgotBtn").click(); assert.ok(s.shown("forgotHint"));
    assert.ok(s.$("forgotHint").textContent.includes("«Забыли ПИН»"));
  });

  await test("другое устройство: логин и пароль; администратору — код; вход паролем со своей страницы; на этом компьютере можно вернуться к ПИН", async () => {
    let step = 0;
    const s = boot({ server: { "/auth/local": () => json(200, { this_computer: false, users: [] }),
                               "/auth/login": () => json(200, { mfa_required: true, mfa_token: "pass-0123456789", expires_in: 300 }),
                               "/auth/login/2fa": () => (++step === 1 ? json(422, { code: "mfa_code_wrong", params: { attempts_left: 4 } }) : json(200, { access_token: "tok-2" })) } });
    await s.ready();
    assert.ok(s.shown("passView") && !s.shown("pinView") && !s.shown("toPin") && !s.shown("firstRunNote"));
    assert.strictEqual(s.$("whereNote").textContent, DICTS.ru["signin.other_device"]);
    s.$("loginUser").value = "admin"; s.$("loginPass").value = "secret-pass";
    await submit(s, "passView");
    assert.ok(s.shown("loginCodeField")); assert.strictEqual(s.$("passError").textContent, DICTS.ru["login.code_prompt"]);
    s.$("loginCode").value = "111111"; await submit(s, "passView");
    assert.ok(s.$("passError").textContent.includes("4"));
    s.$("loginCode").value = "123456"; await submit(s, "passView");
    assert.strictEqual(s.win.sessionStorage.getItem("shelf_audit_token"), "tok-2");
    assert.strictEqual(s.$("loginPass").value, "");
    assert.deepStrictEqual(s.replaced, ["products.html"]);
    const t = boot(); await t.ready();
    t.$("toPassword").click(); await flush(); assert.ok(t.shown("passView") && t.shown("toPin"));
    t.$("toPin").click(); await flush(); assert.ok(t.shown("pinView"));
  });

  await test("этот компьютер, ПИН ещё ни у кого нет: вход паролем и подсказка, как получить «Придумайте ПИН»", async () => {
    const s = boot({ server: { "/auth/local": () => json(200, { this_computer: true, users: [], setup_needed: true }) } });
    await s.ready();
    assert.ok(s.shown("passView") && s.shown("firstRunNote") && !s.shown("toPin"));
  });

  await test("«Придумайте ПИН» по ссылке: адрес очищен, два ввода, несовпадение — заново; совпало — вход и переход; слабый ПИН — ответ сервера", async () => {
    let setup = json(422, { code: "pin_too_simple" });
    const s = boot({ hash: "#setup=TOKEN_abcdef123", server: { "/auth/setup/check": () => json(200, { valid: true, username: "admin" }), "/auth/setup": () => setup } });
    await s.ready();
    assert.deepStrictEqual(s.history, ["/ui/login.html"], "токен ссылки убран из адреса и истории");
    assert.ok(s.shown("setupView") && !s.shown("pinView"));
    assert.ok(s.$("setupFor").textContent.includes("admin"));
    type(s, "setupPin", "48291"); await submit(s, "setupView");
    assert.strictEqual(s.$("setupError").textContent, DICTS.ru["signin.pin_rule_short"]);
    type(s, "setupPin", "482917"); await submit(s, "setupView");
    assert.strictEqual(s.$("setupPinLabel").textContent, DICTS.ru["signin.repeat_pin"]);
    type(s, "setupPin", "482918"); await submit(s, "setupView");
    assert.strictEqual(s.$("setupError").textContent, DICTS.ru["signin.mismatch"]);
    assert.strictEqual(s.$("setupPinLabel").textContent, DICTS.ru["signin.enter_new_pin"]);
    assert.ok(!s.calls.some((c) => c.p === "/auth/setup"), "несовпадение — на сервер не уходит");
    type(s, "setupPin", "111111"); await submit(s, "setupView"); type(s, "setupPin", "111111"); await submit(s, "setupView");
    assert.strictEqual(s.$("setupError").textContent, DICTS.ru["server.pin_too_simple"]);
    setup = json(200, { access_token: "setup-tok" });
    type(s, "setupPin", "482917"); await submit(s, "setupView"); type(s, "setupPin", "482917"); await submit(s, "setupView");
    assert.deepStrictEqual(JSON.parse(s.calls.filter((c) => c.p === "/auth/setup").pop().init.body), { token: "TOKEN_abcdef123", pin: "482917" });
    assert.strictEqual(s.win.sessionStorage.getItem("shelf_audit_token"), "setup-tok");
    assert.deepStrictEqual(s.replaced, ["products.html"]);
  });

  await test("ссылка устарела — обычный вход и объяснение; уже вошли — сразу дальше без формы; «вы долго не работали»", async () => {
    const s = boot({ hash: "#setup=OLD_token_12345", server: { "/auth/setup/check": () => json(200, { valid: false, username: null }) } });
    await s.ready();
    assert.ok(s.shown("pinView")); assert.strictEqual(s.$("pinError").textContent, DICTS.ru["server.setup_link_invalid"]);
    const t = boot({ session: { shelf_audit_token: "live" } }); await t.ready();
    assert.deepStrictEqual(t.replaced, ["products.html"]);
    const u = boot({ search: "?next=identify.html&why=expired" }); await u.ready();
    assert.strictEqual(u.$("pinError").textContent, DICTS.ru["signin.session_ended"]);
  });

  await test("ссылка «Придумайте ПИН» в уже открытой вкладке входа (меняется только #) — сразу «Придумайте ПИН»; прочие # — ничего", async () => {
    const s = boot({ server: { "/auth/setup/check": () => json(200, { valid: true, username: "admin" }) } });
    await s.ready();
    assert.ok(s.shown("pinView"));
    s.win.location.hash = "#other"; s.win.fire("hashchange"); await flush();
    assert.ok(s.shown("pinView") && !s.calls.some((c) => c.p === "/auth/setup/check"));
    s.win.location.hash = "#setup=TOKEN_abcdef123"; s.win.fire("hashchange");
    await s.app.ready; await flush();
    assert.ok(s.shown("setupView") && !s.shown("pinView"));
    assert.deepStrictEqual(s.history, ["/ui/login.html"], "токен убран из адреса");
    assert.strictEqual(s.$("setupPinLabel").textContent, DICTS.ru["signin.enter_new_pin"]);
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
