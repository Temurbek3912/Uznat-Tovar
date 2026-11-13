// Проверка общего меню (frontend/nav.js) без браузера: чистая логика + отрисовка в поддельном DOM. Запуск: node tools/nav_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const FRONT = path.join(__dirname, "..", "frontend");
const navPath = path.join(FRONT, "nav.js");
const navSource = fs.readFileSync(navPath, "utf-8");
const Nav = require(navPath);

function makeNode(tag) {
  const node = {
    tag, attrs: {}, children: [], listeners: {}, _text: "",
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    get firstChild() { return this.children[0] || null; },
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); },
    set textContent(v) { this._text = String(v); },
  };
  return node;
}
const walk = (node, out = []) => { out.push(node); node.children.forEach((c) => walk(c, out)); return out; };
const byClass = (node, cls) => walk(node).filter((n) => (n.attrs.class || "").split(" ").includes(cls));
const links = (container) => walk(container).filter((n) => n.tag === "a" && (n.attrs.class || "").includes("shell-link"));
const labels = (container) => links(container).map((a) => a.children[1].textContent);

function makeWin({page = "home", storage = {}, fetchImpl, search = ""} = {}) {
  const container = makeNode("div");
  const store = {...storage};
  const calls = [];
  const win = {
    document: {getElementById: (id) => (id === "appNav" ? container : null), body: {getAttribute: (k) => (k === "data-page" ? page : null)},
               createElement: makeNode},
    location: {search, protocol: "http:", origin: "http://srv:8000", href: ""},
    sessionStorage: {getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: (k) => { delete store[k]; }},
    fetch: (url, opts = {}) => { calls.push({url, opts}); return fetchImpl ? fetchImpl(url, opts) : Promise.reject(new Error("offline")); },
    setInterval: () => 0,
  };
  return {win, container, store, calls};
}
const json = (status, body) => Promise.resolve({status, ok: status >= 200 && status < 300, json: async () => body});
const sleep = (ms = 10) => new Promise((r) => setTimeout(r, ms));
const ADMIN = ["snapshot:upload", "report:read", "review:manage", "catalog:read", "catalog:write", "user:manage", "store:manage", "camera:manage", "camera:agent"];
const MERCH = ["snapshot:upload", "report:read", "review:manage", "catalog:read"];

let passed = 0;
async function test(name, fn) { await fn(); passed++; console.log("ok  ", name); }
const ids = (list) => Array.from(list, (i) => i.id);

(async () => {
  await test("пункты меню: не вошли / вошли без прав / мерчандайзер / аналитик / администратор", async () => {
    assert.deepStrictEqual(ids(Nav.itemsFor(null)), ["home", "identify"]);
    assert.deepStrictEqual(ids(Nav.itemsFor(undefined)), ["home", "identify"]);
    assert.deepStrictEqual(ids(Nav.itemsFor([])), ["home"]);
    assert.deepStrictEqual(ids(Nav.itemsFor(MERCH)), ["home", "identify"]);
    assert.deepStrictEqual(ids(Nav.itemsFor(["report:read", "catalog:read"])), ["home", "identify"]);
    assert.deepStrictEqual(ids(Nav.itemsFor(ADMIN)), ["home", "identify", "products", "admin"]);
    // Разделов аудита полок в меню больше нет ни у одной роли.
    for (const gone of ["capture", "dashboard", "review", "setup"]) {
      assert.ok(!Nav.ITEMS.some((i) => i.id === gone), `раздел ${gone} должен быть убран`);
    }
    // «Товары» (добавление) — только тем, кто может писать в каталог; «Узнать товар» — всем, кто видит каталог.
    assert.ok(!ids(Nav.itemsFor(MERCH)).includes("products") && ids(Nav.itemsFor(["catalog:write"])).includes("products"));
  });

  await test("каждый пункт ведёт на существующую страницу, id не повторяются", async () => {
    const seen = new Set();
    for (const item of Nav.ITEMS) {
      assert.ok(fs.existsSync(path.join(FRONT, item.href)), `нет файла ${item.href}`);
      assert.ok(!seen.has(item.id), `дубль id ${item.id}`);
      seen.add(item.id);
      assert.ok(item.label && item.icon, item.id);
    }
    // На телефоне пунктов больше, чем влезает: нижняя панель прокручивается (app.css: .shell-nav { overflow-x:auto }, проверяет
    // tests/test_frontend_pages.py). Больше 7 — уже неудобно: человек не заметит, что панель прокручивается.
    assert.ok(Nav.ITEMS.filter((i) => !i.laptopOnly).length <= 7, "в нижней панели телефона больше 7 пунктов");
  });

  await test("названия пунктов — простыми словами, без жаргона", async () => {
    const all = Nav.ITEMS.map((i) => i.label).join(" | ");
    for (const bad of ["OSA", "Ревью", "Дашборд", "Compliance", "API", "Планограмм"]) assert.ok(!all.includes(bad), `жаргон «${bad}» в меню: ${all}`);
  });

  await test("права для пунктов совпадают с серверными именами (app/core/security.py)", async () => {
    const security = fs.readFileSync(path.join(FRONT, "..", "app", "core", "security.py"), "utf-8");
    const serverPerms = new Set([...security.matchAll(/=\s*"([a-z]+:[a-z]+)"/g)].map((m) => m[1]));
    for (const item of Nav.ITEMS.filter((i) => i.needs)) assert.ok(serverPerms.has(item.needs), `права «${item.needs}» нет на сервере`);
  });

  await test("parsePerms: мусор → null, не массив → null, нестроки отбрасываются", async () => {
    assert.strictEqual(Nav.parsePerms(null), null);
    assert.strictEqual(Nav.parsePerms(""), null);
    assert.strictEqual(Nav.parsePerms("{oops"), null);
    assert.strictEqual(Nav.parsePerms('{"a":1}'), null);
    assert.deepStrictEqual(Array.from(Nav.parsePerms('["a",1,null,"b"]')), ["a", "b"]);
    assert.deepStrictEqual(Array.from(Nav.parsePerms("[]")), []);
  });

  await test("роль человеческим языком; неизвестная роль не показывается", async () => {
    assert.strictEqual(Nav.roleLabel("admin"), "Администратор");
    assert.strictEqual(Nav.roleLabel("merchandiser"), "Мерчандайзер");
    assert.strictEqual(Nav.roleLabel("<img src=x>"), "");
    assert.strictEqual(Nav.roleLabel(undefined), "");
  });

  await test("адрес API: ?api_base важнее, иначе адрес страницы, иначе localhost:8000 (файл на диске)", async () => {
    assert.strictEqual(Nav.apiBase({location: {search: "?api_base=http://x:1", protocol: "http:", origin: "http://srv"}}), "http://x:1");
    assert.strictEqual(Nav.apiBase({location: {search: "", protocol: "https:", origin: "https://srv"}}), "https://srv");
    assert.strictEqual(Nav.apiBase({location: {search: "", protocol: "file:", origin: "null"}}), "http://localhost:8000");
  });

  // ---------------- отрисовка и запуск ----------------
  await test("не вошли: разделов в меню нет (они вели бы на форму входа), остаётся название; кнопки «Выйти» нет; запросов нет", async () => {
    const {win, container, calls} = makeWin({page: "identify"});
    Nav.start(win, {pollMs: 0});
    assert.deepStrictEqual(Array.from(labels(container)), []);
    assert.strictEqual(byClass(container, "shell-brand").length, 2);                 // название сверху (телефон) и в колонке (ноутбук)
    assert.ok(byClass(container, "shell-nav")[0].attrs.class.includes("no-links"));
    assert.strictEqual(byClass(container, "shell-logout").length, 0);
    assert.strictEqual(calls.length, 0);
  });

  await test("вошли: активная страница отмечена ровно одна, класс no-links снят", async () => {
    const {win, container} = makeWin({page: "identify", storage: {shelf_audit_token: "t", shelf_audit_perms: JSON.stringify(MERCH)}});
    Nav.start(win, {pollMs: 0});
    const active = links(container).filter((a) => a.attrs["aria-current"] === "page");
    assert.strictEqual(active.length, 1);
    assert.strictEqual(active[0].attrs.href, "identify.html");
    assert.ok(!byClass(container, "shell-nav")[0].attrs.class.includes("no-links"));
  });

  await test("вошли, права уже известны: один набор пунктов, без лишнего запроса /auth/me", async () => {
    const {win, container, calls} = makeWin({page: "home", storage: {shelf_audit_token: "t", shelf_audit_role: "admin", shelf_audit_perms: JSON.stringify(ADMIN)}});
    Nav.start(win, {pollMs: 0});
    assert.deepStrictEqual(Array.from(labels(container)), ["Главная", "Узнать товар", "Товары", "Управление"]);
    assert.strictEqual(calls.length, 0);
    assert.strictEqual(byClass(container, "shell-logout").length, 2);        // сверху (телефон) и внизу меню (ноутбук)
    assert.ok(byClass(container, "shell-who").some((n) => n.textContent === "Администратор"));
  });

  await test("вошли, прав нет в памяти: один запрос /auth/me с Bearer, затем меню перерисовано", async () => {
    const {win, container, calls, store} = makeWin({page: "identify", storage: {shelf_audit_token: "tok"},
      fetchImpl: (url) => json(200, {role: "admin", permissions: ADMIN})});
    Nav.start(win, {pollMs: 0});
    assert.deepStrictEqual(Array.from(labels(container)), ["Главная", "Узнать товар"]);   // вошли, права в пути — общие
    await sleep();
    assert.strictEqual(calls.length, 1);
    assert.strictEqual(calls[0].url, "http://srv:8000/api/v1/auth/me");
    assert.strictEqual(calls[0].opts.headers.Authorization, "Bearer tok");
    assert.ok(Array.from(labels(container)).includes("Управление"));
    assert.strictEqual(store.shelf_audit_role, "admin");
    assert.deepStrictEqual(JSON.parse(store.shelf_audit_perms), ADMIN);
  });

  await test("/auth/me ответил 401: токен и права стёрты, меню как у вышедшего", async () => {
    const {win, container, store} = makeWin({storage: {shelf_audit_token: "old", shelf_audit_role: "admin"}, fetchImpl: () => json(401, {})});
    Nav.start(win, {pollMs: 0});
    await sleep();
    assert.strictEqual(store.shelf_audit_token, undefined);
    assert.strictEqual(byClass(container, "shell-logout").length, 0);
    assert.deepStrictEqual(Array.from(labels(container)), []);
  });

  await test("нет связи при запросе прав: меню остаётся с общими пунктами, ошибок наружу нет", async () => {
    const {win, container} = makeWin({storage: {shelf_audit_token: "t"}, fetchImpl: () => Promise.reject(new Error("offline"))});
    Nav.start(win, {pollMs: 0});
    await sleep();
    assert.deepStrictEqual(Array.from(labels(container)), ["Главная", "Узнать товар"]);
  });

  await test("меню замечает вход на странице (токен появился) и выход (токен исчез) без участия страницы", async () => {
    const {win, container, store, calls} = makeWin({fetchImpl: () => json(200, {role: "merchandiser", permissions: MERCH})});
    const nav = Nav.start(win, {pollMs: 0});
    assert.strictEqual(byClass(container, "shell-logout").length, 0);
    store.shelf_audit_token = "fresh";                       // страница вошла своим кодом
    nav.sync();
    await sleep();
    assert.strictEqual(calls.length, 1);
    assert.strictEqual(byClass(container, "shell-logout").length, 2);
    assert.ok(byClass(container, "shell-who").some((n) => n.textContent === "Мерчандайзер"));
    delete store.shelf_audit_token; delete store.shelf_audit_role; delete store.shelf_audit_perms;   // страница вышла
    nav.sync();
    assert.strictEqual(byClass(container, "shell-logout").length, 0);
  });

  await test("после выхода и нового входа права запрашиваются заново (не наследуются от прошлого пользователя)", async () => {
    let role = "admin";
    const {win, container, store, calls} = makeWin({storage: {shelf_audit_token: "a"}, fetchImpl: () => json(200, {role, permissions: role === "admin" ? ADMIN : MERCH})});
    const nav = Nav.start(win, {pollMs: 0});
    await sleep();
    assert.ok(Array.from(labels(container)).includes("Управление"));
    delete store.shelf_audit_token; delete store.shelf_audit_role; delete store.shelf_audit_perms;
    nav.sync();
    role = "merchandiser"; store.shelf_audit_token = "b";
    nav.sync();
    await sleep();
    assert.strictEqual(calls.length, 2);
    assert.ok(!Array.from(labels(container)).includes("Управление"));
  });

  await test("страница сама завершила сессию (убрала токен из sessionStorage): меню не «оживляет» старую сессию из памяти", async () => {
    const {win, container, store} = makeWin({storage: {shelf_audit_token: "tok", shelf_audit_role: "admin", shelf_audit_perms: JSON.stringify(ADMIN)}});
    const nav = Nav.start(win, {pollMs: 0});
    assert.ok(Array.from(labels(container)).includes("Управление"));
    delete store.shelf_audit_token;                           // как делает setToken(null) в коде страницы (по 401)
    nav.sync();
    assert.strictEqual(byClass(container, "shell-logout").length, 0);
    assert.ok(!Array.from(labels(container)).includes("Управление"));
  });

  await test("сменился токен без «Выйти» (вошёл другой человек): права прошлого пользователя сбрасываются и запрашиваются заново", async () => {
    const {win, container, store, calls} = makeWin({storage: {shelf_audit_token: "A", shelf_audit_role: "admin", shelf_audit_perms: JSON.stringify(ADMIN)},
      fetchImpl: () => json(200, {role: "merchandiser", permissions: MERCH})});
    const nav = Nav.start(win, {pollMs: 0});
    assert.ok(Array.from(labels(container)).includes("Управление"));
    store.shelf_audit_token = "B";
    nav.sync();
    await sleep();
    assert.strictEqual(calls.length, 1);
    assert.ok(!Array.from(labels(container)).includes("Управление"));
    assert.ok(byClass(container, "shell-who").some((n) => n.textContent === "Мерчандайзер"));
  });

  await test("sessionStorage запрещён браузером: меню работает на запасном варианте и не падает", async () => {
    const {win, container} = makeWin({});
    win.sessionStorage = {getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); }};
    assert.doesNotThrow(() => Nav.start(win, {pollMs: 0}));
    assert.deepStrictEqual(Array.from(labels(container)), []);
  });

  await test("«Выйти»: токен, роль и права стёрты сразу; на сервер уходит logout с Bearer; затем переход на главную", async () => {
    const {win, container, store, calls} = makeWin({storage: {shelf_audit_token: "tok", shelf_audit_role: "admin", shelf_audit_perms: JSON.stringify(ADMIN)},
      fetchImpl: () => json(200, {})});
    Nav.start(win, {pollMs: 0});
    byClass(container, "shell-logout")[0].listeners.click();
    assert.strictEqual(store.shelf_audit_token, undefined);
    assert.strictEqual(store.shelf_audit_perms, undefined);
    await sleep();
    assert.strictEqual(calls.length, 1);
    assert.strictEqual(calls[0].url, "http://srv:8000/api/v1/auth/logout");
    assert.strictEqual(calls[0].opts.method, "POST");
    assert.strictEqual(calls[0].opts.headers.Authorization, "Bearer tok");
    assert.strictEqual(win.location.href, "index.html");
  });

  await test("«Выйти» при недоступном сервере всё равно выходит и ведёт на главную", async () => {
    const {win, container, store} = makeWin({storage: {shelf_audit_token: "tok", shelf_audit_perms: "[]"}, fetchImpl: () => Promise.reject(new Error("offline"))});
    Nav.start(win, {pollMs: 0});
    byClass(container, "shell-logout")[1].listeners.click();
    await sleep();
    assert.strictEqual(store.shelf_audit_token, undefined);
    assert.strictEqual(win.location.href, "index.html");
  });

  await test("нет контейнера #appNav (страница без меню): start ничего не делает и не падает", async () => {
    const win = {document: {getElementById: () => null}};
    assert.strictEqual(Nav.start(win, {pollMs: 0}), null);
  });

  await test("опрос состояния включается только если pollMs > 0 и не накапливается", async () => {
    const {win} = makeWin({});
    let timers = 0; win.setInterval = () => { timers++; return 1; };
    Nav.start(win, {pollMs: 0});
    assert.strictEqual(timers, 0);
    Nav.start(win);                                           // по умолчанию — один таймер на запуск
    assert.strictEqual(timers, 1);
  });

  await test("в nav.js нет innerHTML/outerHTML/insertAdjacentHTML/eval — меню строится только через DOM-методы", async () => {
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) {
      assert.ok(!navSource.includes(bad), `в nav.js найдено ${bad}`);
    }
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
