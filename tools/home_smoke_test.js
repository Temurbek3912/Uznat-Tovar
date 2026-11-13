// Проверка логики главной страницы (frontend/index.html + home.js) без браузера: берём скрипт страницы home.js и выполняем его
// в изолированном окружении с поддельным DOM и поддельным fetch. Запуск: node tools/home_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const html = fs.readFileSync(path.join(__dirname, "..", "frontend", "index.html"), "utf-8");
// Логика страницы — в home.js (встроенные <script> запрещены политикой CSP script-src 'self').
assert.ok(/<script src="nav\.js"><\/script>\s*<script src="home\.js"><\/script>/.test(html), "index.html: nav.js, затем home.js");
assert.ok(!/<script>/.test(html), "в index.html не должно быть встроенных <script>");
const script = fs.readFileSync(path.join(__dirname, "..", "frontend", "home.js"), "utf-8");
assert.ok(/<script src="i18n\.js"><\/script>(\s*<script src="i18n\.[a-z-]+\.js"><\/script>){4}\s*<script src="auth\.js"><\/script>\s*<script src="nav\.js">/.test(html),
          "index.html: словари (i18n.js и четыре языка) и общий вход auth.js подключены до nav.js и home.js");
const AuthMod = require(path.join(__dirname, "..", "frontend", "auth.js"));
const FRONT = path.join(__dirname, "..", "frontend");
const I18NMod = require(path.join(FRONT, "i18n.js"));
const DICTS = {ru: require(path.join(FRONT, "i18n.ru.js")), uz: require(path.join(FRONT, "i18n.uz.js")),
               "uz-Cyrl": require(path.join(FRONT, "i18n.uz-cyrl.js")), en: require(path.join(FRONT, "i18n.en.js"))};
function makeI18n(lang) {
  const mem = new Map();
  const inst = I18NMod.create({localStorage: {getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, String(v))},
                               navigator: {languages: [lang || "ru"]}});
  for (const [code, dict] of Object.entries(DICTS)) inst.add(code, dict);
  return inst;
}

// data-needs плиток берём из настоящей разметки: тест ловит и ошибку в правах на плитке
const NEEDS = {};
for (const m of html.matchAll(/<a class="tile[^"]*" id="(\w+)" data-needs="([^"]+)"/g)) NEEDS[m[1]] = m[2];

function makeElement(id) {
  let text = "";
  const el = {
    id, hidden: false, style: {}, value: "", disabled: false, className: "", children: [], listeners: {},
    href: "",
    addEventListener(type, fn) { this.listeners[type] = fn; },
    appendChild(child) { this.children.push(child); return child; },
    getAttribute(name) { return name === "data-needs" ? (NEEDS[id] || null) : null; },
  };
  Object.defineProperty(el, "textContent", {
    get() { return text; },
    set(v) { text = String(v); if (v === "" ) el.children = []; },     // как в настоящем DOM: пустая строка стирает потомков
  });
  return el;
}

function makeEnv({storage = {}, fetchImpl, port = "8000", lang = "ru"}) {
  const i18n = makeI18n(lang);
  const elements = {};
  const el = (id) => (elements[id] ||= makeElement(id));
  const store = {...storage};
  const calls = [];
  const ctx = {
    document: {getElementById: el, createElement: (tag) => { const e = makeElement(null); e.tag = tag; return e; }},
    window: {location: {search: "", protocol: "http:", port, origin: "http://srv:" + port}, I18N: i18n},
    sessionStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    },
    fetch: async (url, opts = {}) => { calls.push({url, opts}); return fetchImpl(url, opts, calls); },
    URLSearchParams, console, Date, setTimeout, clearTimeout,
  };
  // общий вход (auth.js) работает с тем же fetch и sessionStorage, что и страница
  ctx.window.Auth = {create: () => AuthMod.create({fetch: ctx.fetch, sessionStorage: ctx.sessionStorage, location: ctx.window.location})};
  vm.createContext(ctx);
  vm.runInContext(script, ctx);
  return {el, store, calls, ctx, i18n};
}

// Массивы из vm-окружения имеют другой Array.prototype — deepStrictEqual их не сравнит; Array.from даёт обычный массив.
const ids = (list) => Array.from(list, (st) => st.id);
const sleep = (ms = 15) => new Promise((r) => setTimeout(r, ms));
const json = (status, body, headers = {}) => {
  const lower = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {status, ok: status >= 200 && status < 300, json: async () => body,
          headers: {get: (n) => (n.toLowerCase() in lower ? lower[n.toLowerCase()] : null)}};
};

const ALL = ["snapshot:upload", "report:read", "review:manage", "catalog:read", "catalog:write", "user:manage", "store:manage", "camera:manage", "camera:agent"];
const ME = (role, perms, username = "admin") => ({username, role, permissions: perms, all_stores: role === "admin", store_ids: []});
const TILES = ["tileIdentify", "tileProducts", "tileAdmin"];
const PRODUCTS = (n) => Array.from({length: n}, (_, i) => ({id: "p" + i}));

// Маршрутизатор: что отвечает «сервер»
function server({me, catalog = [], fail = []}) {
  return async (url) => {
    const p = url.replace(/^.*\/api\/v1/, "");
    if (fail.some((f) => p.startsWith(f))) throw new Error("Failed to fetch");
    if (p === "/auth/me") return me;
    if (p.startsWith("/catalog/products")) return json(200, catalog);
    return json(404, {});
  };
}

let passed = 0;
async function test(name, fn) { await fn(); passed++; console.log("ok  ", name); }

(async () => {
  // ---------------- чистая логика ----------------
  const {ctx: L} = makeEnv({fetchImpl: () => { throw new Error("не должно быть fetch"); }});

  await test("canSee: без требований видно всем; с требованием — только при наличии права", async () => {
    assert.strictEqual(L.canSee([], null), true);
    assert.strictEqual(L.canSee(undefined, ""), true);
    assert.strictEqual(L.canSee(["a"], "a"), true);
    assert.strictEqual(L.canSee(["a"], "b"), false);
    assert.strictEqual(L.canSee(undefined, "a"), false);
    assert.strictEqual(L.canSee("a", "a"), false);               // не массив — прав нет
  });

  await test("плитки: у каждой есть право; разделов аудита полок на главной нет", async () => {
    assert.deepStrictEqual(Object.keys(NEEDS).sort(), [...TILES].sort());
    assert.strictEqual(NEEDS.tileIdentify, "catalog:read");       // узнавать товар может каждый, кто видит каталог
    assert.strictEqual(NEEDS.tileProducts, "catalog:write");      // добавлять — только администратор
    assert.strictEqual(NEEDS.tileAdmin, "store:manage");
    for (const gone of ["capture.html", "dashboard.html", "review.html", "setup.html", "Аудит полки", "/admin/overview"]) {
      assert.ok(!html.includes(gone), `на главной не должно остаться: ${gone}`);
    }
  });

  await test("приветствие: с именем и без", async () => {
    assert.strictEqual(L.greeting("Анна"), "Здравствуйте, Анна!");
    assert.strictEqual(L.greeting("  "), "Здравствуйте!");
    assert.strictEqual(L.greeting(null), "Здравствуйте!");
  });

  await test("шаги: пустой каталог — следующий шаг «добавьте первый товар» с кнопкой на страницу «Товары»", async () => {
    const steps = L.onboardingSteps({catalogCount: 0});
    assert.deepStrictEqual(ids(steps), ["catalog", "more"]);
    const next = L.nextStep(steps);
    assert.strictEqual(next.id, "catalog");
    assert.strictEqual(next.href, "products.html");
    assert.strictEqual(next.cta, "Добавить товар");
    assert.ok(!/установщик|терминал|папк/i.test(`${next.title} ${next.cta} ${next.note}`), "без техники: человек добавляет товар сам");
    assert.ok(fs.existsSync(path.join(__dirname, "..", "frontend", next.href)), "кнопка ведёт на существующую страницу");
    assert.ok(steps.every((s) => !s.done));
  });

  await test("шаги: один или два товара — следующий «добавьте ещё несколько»", async () => {
    for (const n of [1, 2]) {
      const steps = L.onboardingSteps({catalogCount: n});
      assert.strictEqual(steps.find((s) => s.id === "catalog").done, true);
      assert.strictEqual(L.nextStep(steps).id, "more");
    }
  });

  await test("шаги: три товара и больше — всё сделано, следующего шага нет", async () => {
    const steps = L.onboardingSteps({catalogCount: 3});
    assert.ok(steps.every((s) => s.done));
    assert.strictEqual(L.nextStep(steps), null);
  });

  await test("шаги: неизвестное не показывается — не говорим человеку неправду; странные данные не ломают", async () => {
    assert.deepStrictEqual(ids(L.onboardingSteps({catalogCount: null})), []);
    for (const weird of [undefined, null, {}, {catalogCount: "5"}, {catalogCount: NaN}]) {
      assert.doesNotThrow(() => L.onboardingSteps(weird));
    }
    assert.deepStrictEqual(ids(L.onboardingSteps({})), []);
    assert.strictEqual(L.nextStep([]), null);
  });

  await test("пример адреса для телефона берёт порт текущей страницы", async () => {
    assert.strictEqual(L.phoneExample("http:", "8080"), "http://IP-КОМПЬЮТЕРА:8080");
    assert.strictEqual(L.phoneExample("http:", ""), "http://IP-КОМПЬЮТЕРА:8000");
  });

  // ---------------- страница ----------------
  await test("без токена: форма входа, главная скрыта, сеть не трогаем", async () => {
    const e = makeEnv({fetchImpl: () => { throw new Error("не должно быть fetch"); }});
    await sleep();
    assert.strictEqual(e.calls.length, 0);
    assert.strictEqual(e.el("loginView").hidden, false);
    assert.strictEqual(e.el("homeView").hidden, true);
  });

  await test("администратор: все плитки, приветствие, права и роль записаны для общего меню", async () => {
    const e = makeEnv({storage: {shelf_audit_token: "t"}, fetchImpl: server({me: json(200, ME("admin", ALL))})});
    await sleep(); await sleep();
    assert.strictEqual(e.el("homeView").hidden, false);
    assert.strictEqual(e.el("loginView").hidden, true);
    assert.strictEqual(e.el("hello").textContent, "Здравствуйте, admin!");
    for (const id of TILES) assert.strictEqual(e.el(id).hidden, false, id);
    assert.strictEqual(e.store.shelf_audit_role, "admin");
    assert.deepStrictEqual(JSON.parse(e.store.shelf_audit_perms), ALL);
    assert.strictEqual(e.el("phoneUrl").textContent, "http://IP-КОМПЬЮТЕРА:8000");
  });

  await test("мерчандайзер: только «Узнать товар»; чек-лист не запрашивает каталог", async () => {
    const perms = ["snapshot:upload", "report:read", "review:manage", "catalog:read"];
    const e = makeEnv({storage: {shelf_audit_token: "t"}, fetchImpl: server({me: json(200, ME("merchandiser", perms, "m.karimov"))})});
    await sleep(); await sleep();
    assert.deepStrictEqual(TILES.filter((id) => !e.el(id).hidden), ["tileIdentify"]);
    assert.strictEqual(e.el("startCard").hidden, true);
    assert.deepStrictEqual(e.calls.map((c) => c.url.replace(/^.*\/api\/v1/, "")), ["/auth/me"]);
  });

  await test("аналитик: только «Узнать товар»", async () => {
    const e = makeEnv({storage: {shelf_audit_token: "t"}, fetchImpl: server({me: json(200, ME("analyst", ["report:read", "catalog:read"], "an"))})});
    await sleep(); await sleep();
    assert.deepStrictEqual(TILES.filter((id) => !e.el(id).hidden), ["tileIdentify"]);
  });

  await test("тот, кто добавляет товары (catalog:write), видит плитку «Товары» и чек-лист, но не «Управление»", async () => {
    const e = makeEnv({storage: {shelf_audit_token: "t"}, fetchImpl: server({me: json(200, ME("editor", ["catalog:read", "catalog:write"], "ed"))})});
    await sleep(); await sleep();
    assert.deepStrictEqual(TILES.filter((id) => !e.el(id).hidden), ["tileIdentify", "tileProducts"]);
    assert.strictEqual(e.el("startCard").hidden, false);
  });

  await test("язык: все надписи главной (data-i18n) есть в четырёх словарях; ключи home.* одинаковы во всех языках", async () => {
    const keys = [...html.matchAll(/data-i18n="([^"]+)"/g)].map((m) => m[1]);
    assert.ok(keys.length >= 15, "надписи главной размечены для перевода");
    for (const [code, dict] of Object.entries(DICTS)) for (const k of keys) assert.ok(typeof dict[k] === "string", `${code}: нет ${k}`);
    const homeKeys = (d) => Object.keys(d).filter((k) => k.startsWith("home.") || k.startsWith("role.")).sort();
    for (const code of ["uz", "uz-Cyrl", "en"]) assert.deepStrictEqual(homeKeys(DICTS[code]), homeKeys(DICTS.ru), code);
    const navSrc = fs.readFileSync(path.join(FRONT, "nav.js"), "utf-8");
    for (const src of [script, navSrc]) for (const k of [...src.matchAll(/\btr\("([a-z][\w.]+)"\s*[,)]/g)].map((m) => m[1])) {
      for (const [code, dict] of Object.entries(DICTS)) assert.ok(dict[k] !== undefined, `${code}: нет ключа ${k}`);
    }
    for (const id of ["home", "products", "admin"]) for (const [code, dict] of Object.entries(DICTS)) assert.ok(dict["nav." + id], `${code}: nav.${id}`);
  });

  await test("язык: приветствие, шаги, адрес для телефона и инструкция — на выбранном языке; смена языка перерисовывает их", async () => {
    const text = (node) => node.children.length ? Array.from(node.children, text).join("") : node.textContent;
    const e = makeEnv({lang: "uz", storage: {shelf_audit_token: "t"}, fetchImpl: server({me: json(200, ME("admin", ALL)), catalog: PRODUCTS(1)})});
    await sleep(); await sleep();
    assert.strictEqual(e.el("hello").textContent, "Assalomu alaykum, admin!");
    assert.strictEqual(e.el("phoneUrl").textContent, "http://KOMPYUTER-IP:8000");
    assert.strictEqual(text(e.el("phoneStep3")), "Telefon brauzerida http://KOMPYUTER-IP:8000 manzilini oching (KOMPYUTER-IP oʻrniga — sizning manzilingiz).");
    assert.ok(e.el("phoneStep3").children.includes(e.el("phoneUrl")), "адрес — тот самый элемент <code id=phoneUrl>, а не текст");
    const codes = e.el("phoneStep2").children.filter((c) => c.tag === "code").map((c) => c.textContent);
    assert.deepStrictEqual(codes, ["ipconfig", "192.168.1.50"]);
    e.i18n.setLang("en"); await sleep();
    assert.strictEqual(e.el("hello").textContent, "Hello, admin!");
    assert.strictEqual(e.el("phoneUrl").textContent, "http://COMPUTER-IP:8000");
    assert.ok(text(e.el("phoneStep2")).startsWith("On the computer, open the command prompt, type ipconfig"));
    assert.ok(JSON.stringify(e.el("checklist").children.map(text)).includes("Add a few more products"), "шаги перерисованы");
    e.i18n.setLang("uz-Cyrl"); await sleep();
    assert.strictEqual(e.el("hello").textContent, "Ассалому алайкум, admin!");
  });

  await test("язык: сообщение входа хранится ключом и переводится при смене языка", async () => {
    const e = makeEnv({lang: "en", fetchImpl: async () => json(429, {}, {"Retry-After": "600"})});
    await sleep();
    e.el("loginUser").value = "a"; e.el("loginPass").value = "b";
    await e.el("loginForm").listeners.submit({preventDefault() {}});
    assert.strictEqual(e.el("loginError").textContent, "Too many attempts. Try again in 10 min.");
    e.i18n.setLang("ru"); await sleep();
    assert.strictEqual(e.el("loginError").textContent, "Слишком много попыток. Повторите через 10 мин.");
  });

  await test("второй фактор на главной: после пароля появляется поле кода, второй шаг выдаёт токен и открывает главную", async () => {
    let step = 0;
    const e = makeEnv({fetchImpl: async (url, opts) => {
      const p = url.replace(/^.*\/api\/v1/, "");
      if (p === "/auth/login") return json(200, {mfa_required: true, mfa_token: "pass-0123456789", expires_in: 300, username: "admin"});
      if (p === "/auth/login/2fa") { step++; return step === 1 ? json(422, {code: "mfa_code_wrong", params: {attempts_left: 4}}) : json(200, {access_token: "tok-2fa", token_type: "bearer"}); }
      if (p === "/auth/me") return json(200, ME("admin", ALL));
      if (p.startsWith("/catalog/products")) return json(200, PRODUCTS(3));
      return json(404, {});
    }});
    await sleep();
    e.el("loginUser").value = "admin"; e.el("loginPass").value = "secret-pass";
    await e.el("loginForm").listeners.submit({preventDefault() {}});
    assert.strictEqual(e.el("loginCodeField").hidden, false, "поле кода показано");
    assert.ok(e.el("loginError").textContent.startsWith("Пароль верный. Введите 6 цифр"));
    assert.strictEqual(e.store.shelf_audit_token, undefined, "до кода токена нет");
    e.el("loginCode").value = "000000";
    await e.el("loginForm").listeners.submit({preventDefault() {}});
    assert.strictEqual(e.el("loginError").textContent, "Код не подошёл. Осталось попыток: 4.");
    e.el("loginCode").value = "123456";
    await e.el("loginForm").listeners.submit({preventDefault() {}}); await sleep(); await sleep();
    assert.strictEqual(e.store.shelf_audit_token, "tok-2fa");
    assert.strictEqual(e.el("loginCodeField").hidden, true);
    assert.strictEqual(e.el("homeView").hidden, false);
    const second = e.calls.find((c) => c.url.endsWith("/auth/login/2fa"));
    assert.ok(!String(second.opts.body).includes("secret-pass"), "пароль во второй шаг не уходит");
  });

  await test("имя пользователя с разметкой не интерпретируется как HTML (textContent)", async () => {
    const e = makeEnv({storage: {shelf_audit_token: "t"}, fetchImpl: server({me: json(200, ME("analyst", ["report:read"], "<img src=x onerror=alert(1)>"))})});
    await sleep(); await sleep();
    assert.strictEqual(e.el("hello").textContent, "Здравствуйте, <img src=x onerror=alert(1)>!");
  });

  await test("протухший токен (401 на /auth/me): токен и права стёрты, форма входа с понятным текстом", async () => {
    const e = makeEnv({storage: {shelf_audit_token: "old", shelf_audit_role: "admin", shelf_audit_perms: "[]"}, fetchImpl: server({me: json(401, {})})});
    await sleep(); await sleep();
    assert.strictEqual(e.store.shelf_audit_token, undefined);
    assert.strictEqual(e.store.shelf_audit_perms, undefined);
    assert.strictEqual(e.el("loginView").hidden, false);
    assert.match(e.el("loginError").textContent, /Сессия истекла/);
  });

  await test("сервер недоступен: сообщение «нет связи», а не пустая страница", async () => {
    const e = makeEnv({storage: {shelf_audit_token: "t"}, fetchImpl: () => { throw new Error("Failed to fetch"); }});
    await sleep(); await sleep();
    assert.strictEqual(e.el("loginView").hidden, false);
    assert.match(e.el("loginError").textContent, /Нет связи с сервером/);
  });

  await test("вход: неверный пароль, пауза после множества попыток (429), успешный вход", async () => {
    let mode = "401";
    const e = makeEnv({fetchImpl: async (url) => {
      if (url.endsWith("/auth/login")) {
        if (mode === "401") return json(401, {});
        if (mode === "429") return json(429, {}, {"Retry-After": "600"});
        return json(200, {access_token: "NEW", username: "admin", role: "admin"});
      }
      return server({me: json(200, ME("admin", ALL))})(url);
    }});
    await sleep();
    e.el("loginUser").value = "admin"; e.el("loginPass").value = "x";
    await e.el("loginForm").listeners.submit({preventDefault() {}});
    assert.strictEqual(e.el("loginError").textContent, "Неверный логин или пароль");
    assert.strictEqual(e.store.shelf_audit_token, undefined);
    mode = "429";
    await e.el("loginForm").listeners.submit({preventDefault() {}});
    assert.match(e.el("loginError").textContent, /через 10 мин/);
    mode = "ok";
    await e.el("loginForm").listeners.submit({preventDefault() {}});
    await sleep();
    assert.strictEqual(e.store.shelf_audit_token, "NEW");
    assert.strictEqual(e.el("homeView").hidden, false);
    assert.strictEqual(e.el("loginPass").value, "");               // пароль не остаётся в поле
    assert.strictEqual(e.el("loginBtn").disabled, false);
  });

  // ---------------- чек-лист на странице ----------------
  const steps = (e) => e.el("checklist").children;
  const cls = (li) => li.className.split(" ");

  await test("чек-лист: пустой каталог — 2 шага, следующий один и с кнопкой «Добавить товар»", async () => {
    const e = makeEnv({storage: {shelf_audit_token: "t"}, fetchImpl: server({me: json(200, ME("admin", ALL))})});
    await sleep(); await sleep();
    assert.strictEqual(e.el("startCard").hidden, false);
    assert.strictEqual(steps(e).length, 2);
    assert.ok(steps(e).every((li) => !cls(li).includes("done")));
    assert.deepStrictEqual(steps(e).map((li) => cls(li).includes("next")), [true, false]);
    const buttons = steps(e).flatMap((li) => li.children[1].children.filter((c) => c.tag === "a"));
    assert.strictEqual(buttons.length, 1);                          // кнопка только у следующего шага
    assert.strictEqual(buttons[0].textContent, "Добавить товар");
    assert.strictEqual(buttons[0].href, "products.html");
    assert.strictEqual(steps(e)[0].children[0].textContent, "1");
  });

  await test("чек-лист: первый товар есть — он отмечен галочкой, следующий «ещё несколько товаров»", async () => {
    const e = makeEnv({storage: {shelf_audit_token: "t"}, fetchImpl: server({me: json(200, ME("admin", ALL)), catalog: PRODUCTS(1)})});
    await sleep(); await sleep();
    assert.deepStrictEqual(steps(e).map((li) => cls(li).includes("done")), [true, false]);
    assert.strictEqual(steps(e)[0].children[0].textContent, "✓");
    assert.ok(cls(steps(e)[1]).includes("next"));
    const button = steps(e)[1].children[1].children.find((c) => c.tag === "a");
    assert.ok(button, "у следующего шага есть кнопка");
    assert.strictEqual(button.href, "products.html");
  });

  await test("чек-лист: три товара и больше — карточка скрыта", async () => {
    const e = makeEnv({storage: {shelf_audit_token: "t"}, fetchImpl: server({me: json(200, ME("admin", ALL)), catalog: PRODUCTS(3)})});
    await sleep(); await sleep();
    assert.strictEqual(e.el("startCard").hidden, true);
  });

  await test("чек-лист: каталог недоступен — карточка не показывается (не врём «товаров нет»), главная работает", async () => {
    const e = makeEnv({storage: {shelf_audit_token: "t"}, fetchImpl: server({me: json(200, ME("admin", ALL)), fail: ["/catalog"]})});
    await sleep(); await sleep();
    assert.strictEqual(e.el("startCard").hidden, true);
    assert.strictEqual(e.el("homeView").hidden, false);
  });

  await test("потеря токена и повторный init: показан вход, главная скрыта", async () => {
    const e = makeEnv({storage: {shelf_audit_token: "t"}, fetchImpl: server({me: json(200, ME("admin", ALL))})});
    await sleep(); await sleep();
    assert.strictEqual(e.el("startCard").hidden, false);
    e.ctx.sessionStorage.removeItem("shelf_audit_token");
    await e.ctx.init();
    assert.strictEqual(e.el("loginView").hidden, false);
    assert.strictEqual(e.el("homeView").hidden, true);
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
