// Смоук-тест админ-панели (frontend/admin.html) в Node.js без браузера — тем же приёмом, что остальные tools/*_smoke_test.js:
// настоящий <script> выполняется на минимальном фейковом DOM. Фейковый DOM СТРОГИЙ: запись в innerHTML бросает ошибку,
// поэтому любое место, где данные сервера могли бы попасть в разметку, проявится сразу.
// Не проверяется: вёрстка, реальный браузер, сервер (ответы API подменены).
// Запуск из корня проекта:  node tools/admin_smoke_test.js
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const html = fs.readFileSync(process.env.ADMIN_HTML || path.join(__dirname, "..", "frontend", "admin.html"), "utf-8");
// Логика страницы — в admin.js (встроенные <script> запрещены политикой CSP script-src 'self'); страница подключает её сама.
assert.ok(/<script src="admin\.js"><\/script>/.test(html), "admin.html должна подключать admin.js");
assert.ok(!/<script>/.test(html), "в admin.html не должно быть встроенных <script>");
const script = fs.readFileSync(process.env.ADMIN_JS || path.join(__dirname, "..", "frontend", "admin.js"), "utf-8");
{
  const order = [...html.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]), at = (n) => order.indexOf(n);
  assert.ok(/<script src="i18n\.js"><\/script>(\s*<script src="i18n\.[a-z-]+\.js"><\/script>){4}/.test(html) && at("i18n.en.js") < at("auth.js")
            && at("auth.js") < at("shell.js") && at("shell.js") < at("admin.js") && at("admin.js") < order.lastIndexOf("boot.js"),
            "admin.html: словари, общий вход auth.js и меню shell.js подключены до admin.js; меню запускает boot.js");
  assert.ok(!/id="loginView"|id="loginForm"|id="loginUser"/.test(html), "своей формы входа больше нет — вход на login.html (2026-11-15)");
  assert.ok(/<nav class="rail" id="rail" data-shell/.test(html) && /href="tokens\.css"/.test(html) && /href="ui\.css"/.test(html), "общий вид и общее меню");
}
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

// ---------------------------------------------------------------- фейковый DOM
class TextNode { constructor(t) { this.nodeType = 3; this.textContent = String(t); this.parent = null; } }
function makeDom() {
  const registry = {};
  class El {
    constructor(tag) {
      this.nodeType = 1; this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.style = {};
      this.className = ""; this.parent = null; this.value = ""; this.checked = false; this.disabled = false; this.files = null;
    }
    appendChild(c) { if (c.parent) c.parent.removeChild(c); c.parent = this; this.children.push(c); return c; }
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parent = null; }
    replaceChildren(...nodes) { this.children.forEach((c) => { c.parent = null; }); this.children = []; nodes.forEach((n) => this.appendChild(n)); }
    remove() { if (this.parent) this.parent.removeChild(this); }
    setAttribute(k, v) { this.attrs[k] = String(v); if (k === "id") registry[String(v)] = this; if (k === "class") this.className = String(v); }
    getAttribute(k) { return this.attrs[k]; }
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
    get textContent() { return this.children.map((c) => c.textContent).join(""); }
    set textContent(v) { this.children = []; this.appendChild(new TextNode(v)); }
    set innerHTML(v) { throw new Error(`innerHTML запрещён в этом тесте (значение: ${String(v).slice(0, 40)})`); }
    get innerHTML() { return ""; }
    get firstChild() { return this.children[0] || null; }
    click() { return click(this); }
  }
  const body = new El("body");
  const document = {
    body,
    getElementById: (id) => registry[id] || (registry[id] = Object.assign(new El("div"), {attrs: {id}})),
    createElement: (tag) => new El(tag),
    createTextNode: (t) => new TextNode(t),
  };
  return {El, document, registry, body};
}
async function click(el) {
  // Как в браузере: обработчик события НЕ ждут (иначе клик по «Удалить» зависал бы на диалоге подтверждения, которого
  // ждёт следующий клик). Отказ обработчика не должен остаться незамеченным — он станет ошибкой теста.
  if (el.disabled) return;
  for (const fn of el.listeners.click || []) {
    Promise.resolve(fn({preventDefault() {}})).catch((e) => { unhandled.push(e); });
  }
  await sleep(8);
}
const unhandled = [];
function walk(node, visit) { visit(node); (node.children || []).forEach((c) => walk(c, visit)); }
function findAll(root, pred) { const out = []; walk(root, (n) => { if (n.nodeType === 1 && pred(n)) out.push(n); }); return out; }
function button(root, text) {
  const hit = findAll(root, (n) => n.tag === "button" && n.textContent.includes(text));
  assert.ok(hit.length, `кнопка «${text}» не найдена; есть: ${findAll(root, (n) => n.tag === "button").map((b) => b.textContent).join(" | ")}`);
  return hit[0];
}
const sleep = (ms = 15) => new Promise((r) => setTimeout(r, ms));
const json = (status, body) => ({status, ok: status >= 200 && status < 300, json: async () => body, blob: async () => ({size: 3})});
const router = (routes) => async (url, opts, calls) => {
  for (const [matcher, handler] of routes) {
    if (typeof matcher === "string" ? url.includes(matcher) : matcher.test(url)) return handler(url, opts || {}, calls);
  }
  throw new Error(`unhandled fetch: ${url}`);
};

function makeEnv({token = null, hash = "", fetchImpl, lang = "ru"}) {
  const i18n = makeI18n(lang);
  const dom = makeDom();
  const session = token ? {shelf_audit_token: token} : {};
  const calls = [];
  const windowListeners = {}, redirects = [];
  const ctx = {
    document: dom.document,
    window: {location: {search: "", hash, protocol: "http:", origin: "http://srv:8000", pathname: "/ui/admin.html", replace: (u) => redirects.push(u)}, addEventListener: (t, fn) => { windowListeners[t] = fn; }, I18N: i18n},
    sessionStorage: {getItem: (k) => (k in session ? session[k] : null), setItem: (k, v) => { session[k] = String(v); }, removeItem: (k) => { delete session[k]; }},
    fetch: async (url, opts = {}) => { calls.push({url, opts}); return fetchImpl(url, opts, calls); },
    URL: {createObjectURL: () => "blob:fake", revokeObjectURL() {}},
    FormData: class { constructor() { this.parts = []; } append(k, v) { this.parts.push([k, v]); } },
    URLSearchParams, console, Date, Math, JSON, Object, Array, Promise, Set, String, Number, Error, isNaN, parseInt, encodeURIComponent,
    setTimeout, clearTimeout, Intl,
  };
  ctx.window.Auth = {create: () => AuthMod.create({fetch: ctx.fetch, sessionStorage: ctx.sessionStorage, location: ctx.window.location})};
  vm.createContext(ctx);
  vm.runInContext(script, ctx);
  return {i18n, dom, session, calls, ctx, redirects, main: dom.document.getElementById("main"), windowListeners, run: (code) => vm.runInContext(code, ctx), el: (id) => dom.registry[id] || dom.document.getElementById(id)};
}

const ME_ADMIN = {username: "admin", role: "admin", permissions: ["store:manage", "camera:manage", "user:manage"], all_stores: true, store_ids: []};
const ME_MERCH = {username: "m", role: "merchandiser", permissions: ["report:read", "snapshot:upload"], all_stores: false, store_ids: ["s1"]};
const STORES = [{id: "s1", name: "Альфа", address: "ул. 1", format: "supermarket", shelves: []}];
const EMPTY_PRODUCTS = {total: 0, limit: 50, offset: 0, items: []};
const baseRoutes = (extra = []) => [
  ["/auth/me", () => json(200, ME_ADMIN)],
  ...extra,
  [/\/api\/v1\/stores$/, () => json(200, STORES)],
];

let passed = 0, failed = 0, finished = false;
// Если цикл событий опустел раньше времени (зависшее обещание), Node завершается с кодом 0 и без итога — для CI это ложный успех.
process.on("exit", () => { if (!finished) { console.log("ПРОГОН ПРЕРВАН: тест завис (обещание не завершилось) — это ошибка"); process.exitCode = 1; } });
async function test(name, fn) {
  try { await fn(); passed++; console.log(`ok   ${name}`); } catch (e) { failed++; console.log(`FAIL ${name}\n     ${e && e.stack || e}`); }
}

(async () => {
  const pure = makeEnv({fetchImpl: router([])});
  const run = (code) => pure.run(code);
  const J = (v) => JSON.parse(JSON.stringify(v));

  // ================================================================ чистая логика
  await test("pageFromHash: известные страницы, остальное — «Состояние программы»; разделов аудита больше нет", () => {
    assert.strictEqual(run(`pageFromHash("#/system")`), "system");
    assert.strictEqual(run(`pageFromHash("#/users?x=1")`), "users");
    for (const gone of ["cameras", "overview", "stores", "evil"]) assert.strictEqual(run(`pageFromHash("#/${gone}")`), "system", gone);
    assert.strictEqual(run(`pageFromHash("")`), "system");
    assert.strictEqual(run(`pageFromHash("#/products")`), "products", "прежний раздел товаров открывается по прямому адресу");
  });
  await test("карточка покупателя: в настройке только известные поля по порядку, срока годности нет и подделать нельзя", () => {
    assert.strictEqual(run(`JSON.stringify(buildCardFields(["description", "price", "expiry_date", "country"]))`),
      JSON.stringify({fields: ["price", "country", "description"]}));
    assert.strictEqual(run(`JSON.stringify(buildCardFields(null))`), JSON.stringify({fields: []}));
    assert.ok(!/expir|srok/i.test(run(`CARD_FIELDS.join(",")`)));
  });
  await test("форматтер размера: нет данных не превращается в 0", () => {
    assert.strictEqual(run(`fmtBytes(0)`), "0 Б");
    assert.strictEqual(run(`fmtBytes(1536)`), "1.5 КБ");
    assert.strictEqual(run(`fmtBytes(null)`), "—");
  });
  await test("меню — общее (shell.js): разделы управления — пункты «Карточка покупателя», «Сотрудники», «Состояние программы»; ссылок на удалённые страницы нет", () => {
    const Shell = require(path.join(FRONT, "shell.js"));
    assert.deepStrictEqual(Shell.ITEMS.filter((i) => i.href.startsWith("admin.html")).map((i) => i.href), ["admin.html#/card", "admin.html#/users", "admin.html#/system"]);
    for (const gone of ["capture.html", "dashboard.html", "review.html", "setup.html", "#/overview", "#/cameras", "#/stores"]) {
      assert.ok(!html.includes(gone), `в админке не должно остаться: ${gone}`);
    }
    assert.ok(Shell.ITEMS.some((i) => i.href === "identify.html") && Shell.ITEMS.some((i) => i.href === "products.html"));
  });
  await test("describeError: тексты сервера для 409/422/503, длинный список ошибок обрезается", () => {
    assert.strictEqual(run(`describeError(409, "Удалить нельзя: есть связанные записи")`), "Удалить нельзя: есть связанные записи");
    assert.strictEqual(run(`describeError(503, "Очередь недоступна")`), "Очередь недоступна");
    assert.strictEqual(run(`describeError(403)`), "Недостаточно прав");
    assert.strictEqual(run(`describeError(500)`), "Ошибка сервера, попробуйте позже");
    const many = {message: "Не применено", errors: Array.from({length: 20}, (_, i) => `e${i}`)};
    const text = run(`describeError(422, ${JSON.stringify(many)})`);
    assert.ok(text.startsWith("Не применено\n• e0") && text.includes("• e14") && !text.includes("• e15") && text.endsWith("… и ещё 5"));
  });
  await test("buildUserCreatePayload / buildUserUpdatePayload", () => {
    const create = (f) => J(run(`buildUserCreatePayload(${JSON.stringify(f)})`));
    // пароль сотруднику выдаёт сервер (временный, документ 05, §12.2): в запросе создания пароля нет, даже если его передали
    assert.deepStrictEqual(create({username: " m.karimov ", password: "long-enough-1", role: "merchandiser", store_ids: ["s1"]}).payload,
      {username: "m.karimov", role: "merchandiser", store_ids: ["s1"]});
    assert.ok(create({username: "a", role: "admin"}).error.includes("Логин"));
    assert.ok(create({username: "ok-user", role: "root"}).error.includes("роль"));
    const orig = {role: "merchandiser", is_active: true, store_ids: ["s1", "s2"]};
    const upd = (f) => J(run(`buildUserUpdatePayload(${JSON.stringify(f)}, ${JSON.stringify(orig)})`));
    assert.deepStrictEqual(upd({role: "merchandiser", is_active: true, store_ids: ["s2", "s1"], password: ""}).error, "Ничего не изменено");
    assert.deepStrictEqual(upd({role: "analyst", is_active: false, store_ids: ["s1"], password: "new-long-pass"}).payload, {role: "analyst", is_active: false, store_ids: ["s1"]});
    assert.deepStrictEqual(upd({role: "merchandiser", is_active: true, store_ids: ["s1", "s2"], password: "typed-by-admin"}).error, "Ничего не изменено");
  });
  await test("«доступ до»: выбранный день действует целиком, туда-обратно без сдвига; снять срок — null; значки состояния", () => {
    const iso = run(`accessUntilFromDate("2026-12-31")`);
    assert.strictEqual(new Date(iso).getTime(), new Date(2027, 0, 1).getTime());          // до полуночи следующего дня по часам компьютера
    assert.strictEqual(run(`dateFromAccessUntil(${JSON.stringify(iso)})`), "2026-12-31");
    assert.strictEqual(run(`dateFromAccessUntil(${JSON.stringify(iso.replace("Z", ""))})`), "2026-12-31");   // сервер отдаёт UTC без «Z»
    assert.strictEqual(run(`accessUntilFromDate("")`), null);
    assert.strictEqual(run(`dateFromAccessUntil(null)`), "");
    const create = J(run(`buildUserCreatePayload({username: "temp.worker", role: "merchandiser", store_ids: [], access_until_date: "2026-12-31"})`));
    assert.strictEqual(create.payload.access_until, iso);
    const orig = {role: "merchandiser", is_active: true, store_ids: [], access_until: iso};
    const upd = (f) => J(run(`buildUserUpdatePayload(${JSON.stringify(f)}, ${JSON.stringify(orig)})`));
    assert.strictEqual(upd({role: "merchandiser", is_active: true, store_ids: [], access_until_date: "2026-12-31"}).error, "Ничего не изменено");
    assert.deepStrictEqual(upd({role: "merchandiser", is_active: true, store_ids: [], access_until_date: ""}).payload, {access_until: null});
    const badgeText = (u) => run(`userStatusBadge(${JSON.stringify(u)}).textContent`);
    assert.strictEqual(badgeText({status: "archived", is_active: false}), "в архиве");
    assert.strictEqual(badgeText({status: "blocked", is_active: false, block_reason: "потерян телефон"}), "заблокирован: потерян телефон");
    assert.strictEqual(badgeText({status: "expired", is_active: true}), "срок доступа истёк");
    assert.strictEqual(badgeText({is_active: false}), "отключён");                       // прежний сервер без status
  });
  // ================================================================ вход и права
  await test("без токена — на страницу входа (с возвратом сюда) и ни одного запроса", async () => {
    const e = makeEnv({hash: "#/users", fetchImpl: router([])});
    await sleep();
    assert.deepStrictEqual(e.redirects, ["login.html?next=admin.html%23%2Fusers"]);
    assert.strictEqual(e.calls.length, 0);
  });
  await test("не администратор: «Нужны права администратора», запросов к админ-API нет", async () => {
    const e = makeEnv({token: "t", fetchImpl: router([["/auth/me", () => json(200, ME_MERCH)]])});
    await sleep();
    assert.ok(e.main.textContent.includes("Нужны права администратора"));
    assert.ok(!e.calls.some((c) => c.url.includes("/admin/") || c.url.includes("/cameras") || c.url.includes("/users")));
  });
  await test("без раздела — «Состояние программы»; 401 посреди работы: токен стёрт, на страницу входа с причиной", async () => {
    const e = makeEnv({token: "old", hash: "#/products", fetchImpl: router([["/auth/me", () => json(200, ME_ADMIN)], ["/admin/products", () => json(401, {})]])});
    await sleep(40);
    assert.strictEqual(e.session.shelf_audit_token, undefined);
    assert.deepStrictEqual(e.redirects, ["login.html?next=admin.html%23%2Fproducts&why=expired"]);
    assert.strictEqual(e.run("pageFromHash('')"), "system");
    assert.strictEqual(e.run("pageFromHash('#/nope')"), "system");
  });

  // ================================================================ пользователи, товары, система
  await test("пользователи: создание отправляет проверенные данные; админ и аналитик — «вся сеть»", async () => {
    const users = [{id: "u1", username: "admin", role: "admin", is_active: true, store_ids: [], created_at: "x"},
                   {id: "u2", username: "m.karimov", role: "merchandiser", is_active: true, store_ids: ["s1"], created_at: "x"},
                   {id: "u3", username: "old", role: "merchandiser", is_active: false, store_ids: [], created_at: "x"}];
    const e = makeEnv({token: "t", hash: "#/users", fetchImpl: router(baseRoutes([[/\/api\/v1\/users$/, (u, o) => o.method === "POST" ? json(201, users[1]) : json(200, users)]]))});
    await sleep(40);
    const t = e.main.textContent;
    assert.ok(t.includes("вся сеть") && t.includes("Альфа") && t.includes("нет доступа") && t.includes("отключён"));
    await e.el("addUser").click();
    assert.ok(!e.dom.registry.usPass && e.dom.registry.usName, "поля пароля в форме нет — пароль выдаёт сервер");
    e.el("usName").value = "x"; e.el("usRole").value = "merchandiser";
    await e.el("usSave").click();
    assert.ok(!e.calls.some((c) => c.opts.method === "POST") && e.el("toasts").textContent.includes("Логин"));
    e.el("usName").value = "new.user";
    await e.el("usSave").click(); await sleep(20);
    const post = e.calls.find((c) => c.opts.method === "POST");
    assert.deepStrictEqual(JSON.parse(post.opts.body), {username: "new.user", role: "merchandiser", store_ids: []});
  });
  await test("пользователи: правка отправляет только изменённое; пустая правка не уходит", async () => {
    const users = [{id: "u2", username: "m.karimov", role: "merchandiser", is_active: true, store_ids: ["s1"], created_at: "x"}];
    const e = makeEnv({token: "t", hash: "#/users", fetchImpl: router(baseRoutes([[/\/users\/u2$/, () => json(200, users[0])], [/\/api\/v1\/users$/, () => json(200, users)]]))});
    await sleep(40);
    await button(findAll(e.main, (n) => n.tag === "tr" && n.textContent.includes("m.karimov"))[0], "Изменить").click();
    await e.el("usSave").click();
    assert.ok(!e.calls.some((c) => c.opts.method === "PATCH") && e.el("toasts").textContent.includes("Ничего не изменено"));
    e.el("usRole").value = "analyst";
    await e.el("usSave").click(); await sleep();
    assert.deepStrictEqual(JSON.parse(e.calls.find((c) => c.opts.method === "PATCH").opts.body), {role: "analyst"});
  });
  await test("история сотрудника: журнал по нему, действия по-человечески, «кто» — логин или консоль; блокировка с причиной", async () => {
    const users = [{id: "u2", username: "m.karimov", role: "merchandiser", is_active: true, store_ids: ["s1"], created_at: "x", status: "active"}];
    const journal = [{at: "2026-11-14T10:00:00", action: "user.blocked", actor: "admin", via: "api", reason: "проверка"},
                     {at: "2026-11-14T09:00:00", action: "user.created", actor: null, via: "console", reason: null},
                     {at: "2026-11-14T08:00:00", action: "something.new", actor: null, via: "system", reason: null}];
    const e = makeEnv({token: "t", hash: "#/users", fetchImpl: router(baseRoutes([[/\/audit\?target_id=u2/, () => json(200, journal)],
      [/\/users\/u2$/, () => json(200, users[0])], [/\/api\/v1\/users$/, () => json(200, users)]]))});
    await sleep(40);
    const row = () => findAll(e.main, (n) => n.tag === "tr" && n.textContent.includes("m.karimov"))[0];
    await button(row(), "История").click(); await sleep(20);
    const text = e.el("modalRoot").textContent;
    assert.ok(text.includes("История: m.karimov") && text.includes("Заблокирован") && text.includes("проверка"), text.slice(0, 400));
    assert.ok(text.includes("консоль сервера") && text.includes("something.new"));        // неизвестное действие — как есть, не пропадает
    await e.el("historyClose").click();
    await button(row(), "Отключить").click(); await sleep(10);
    e.el("reasonInput").value = "потерян телефон";
    await e.el("confirmOk").click(); await sleep(20);
    assert.deepStrictEqual(JSON.parse(e.calls.find((c) => c.opts.method === "PATCH").opts.body), {is_active: false, block_reason: "потерян телефон"});
  });
  await test("временный пароль: создание показывает пароль сервера один раз; «ждёт первого входа»; выдача нового — с подтверждением", async () => {
    const users = [{id: "u1", username: "admin", role: "admin", is_active: true, store_ids: [], created_at: "x"},
                   {id: "u2", username: "m.karimov", role: "merchandiser", is_active: true, store_ids: ["s1"], created_at: "x",
                    must_change_password: true, temp_password_expires_at: "2026-11-09T10:00:00"}];
    const e = makeEnv({token: "t", hash: "#/users", fetchImpl: router(baseRoutes([
      [/\/users\/u2\/temporary-password$/, () => json(200, {temporary_password: "7KQM-X4HP-9TDW", expires_at: "2026-11-09T12:00:00"})],
      [/\/api\/v1\/users$/, (u, o) => o.method === "POST" ? json(201, {id: "u9", username: "new.user", role: "merchandiser", is_active: true,
        store_ids: [], created_at: "x", must_change_password: true, temporary_password: "ABCD-EFGH-JKMN", temp_password_expires_at: null}) : json(200, users)]]))});
    await sleep(40);
    const row = findAll(e.main, (n) => n.tag === "tr" && n.textContent.includes("m.karimov"))[0];
    assert.ok(row.textContent.includes("ждёт первого входа"));
    const self = findAll(e.main, (n) => n.tag === "tr" && n.textContent.includes("admin") && !n.textContent.includes("m.karimov"))[0];
    assert.ok(!self.textContent.includes("Временный пароль"), "себе временный пароль не выдаётся");
    await e.el("addUser").click();
    e.el("usName").value = "new.user"; await e.el("usSave").click(); await sleep(30);
    assert.strictEqual(e.el("tempPassValue").textContent, "ABCD-EFGH-JKMN");
    assert.ok(!e.el("toasts").textContent.includes("ABCD-EFGH-JKMN"), "пароль — только в окне, не в уведомлении");
    await e.el("tempPassClose").click();
    const click = button(findAll(e.main, (n) => n.tag === "tr" && n.textContent.includes("m.karimov"))[0], "Временный пароль").click(); await sleep();
    assert.ok(!e.calls.some((c) => c.url.endsWith("/temporary-password")), "без подтверждения — ничего");
    await e.el("confirmOk").click(); await click; await sleep(30);
    const issue = e.calls.find((c) => c.url.endsWith("/users/u2/temporary-password"));
    assert.ok(issue && issue.opts.method === "POST");
    assert.strictEqual(e.el("tempPassValue").textContent, "7KQM-X4HP-9TDW");
  });
  await test("товары: таблица и пагинация; подсказка ведёт на страницу «Товары», понятий планограммы нет", async () => {
    const e = makeEnv({token: "t", hash: "#/products", fetchImpl: router(baseRoutes([["/admin/products", () => json(200, {total: 120, limit: 50, offset: 0,
      items: [{id: "p1", sku: "COLA", name: "Кола", brand: "X", category: "напитки", expected_price: 9900}, {id: "p2", sku: "OLD", name: "Старый", brand: "Y", category: "z", expected_price: null}]})]]))});
    await sleep(40);
    const t = e.main.textContent;
    assert.ok(t.includes("COLA") && t.includes("Старый") && t.includes("Всего: 120 · страница 1 из 3"));
    assert.strictEqual(e.el("prPrev").disabled, true);
    assert.ok(t.includes("«Товары»") && !t.includes("import_catalog") && !/планограмм/i.test(t));
  });
  await test("система: секретов нет в выводе, параметры только для чтения, предупреждения видны", async () => {
    const sys = {server_time_utc: "x", server_time_local: "2026-10-10 17:00:00", timezone: "Asia/Tashkent", database: "ok", queue: "ok",
      worker: {online: false, nodes: []},
      storage: {path: "data", usage: {exists: true, bytes: 5368709120, files: 1234, truncated: false}, disk: {total_bytes: 1e12, free_bytes: 5e11}},
      settings: [{key: "environment", label: "Окружение", value: "development", source: ".env: ENVIRONMENT", editable: false}], settings_note: "Параметры только для чтения: меняются в .env"};
    const e = makeEnv({token: "t", hash: "#/system", fetchImpl: router(baseRoutes([["/admin/system", () => json(200, sys)]]))});
    await sleep(40);
    const t = e.main.textContent;
    assert.ok(t.includes("не отвечает") && t.includes("5.0 ГБ") && t.includes("1234") && t.includes("development") && t.includes("только для чтения") && t.includes("Срока хранения фото в системе нет"));
    assert.ok(!/агент камер|снимков/i.test(t), "про агента камер и снимки на странице системы больше нет");
    assert.strictEqual(findAll(e.main, (n) => n.tag === "input").length, 0);       // ни одного поля ввода: настройки не редактируются через веб
  });

  // ================================================================ язык
  await test("язык: все надписи страницы (data-i18n) есть в четырёх словарях; ключи admin.* одинаковы во всех языках", () => {
    const keys = [...html.matchAll(/data-i18n="([^"]+)"/g)].map((m) => m[1]);
    assert.ok(keys.length >= 2);
    for (const [code, dict] of Object.entries(DICTS)) for (const k of keys) assert.ok(typeof dict[k] === "string", `${code}: нет ${k}`);
    const ak = (d) => Object.keys(d).filter((k) => k.startsWith("admin.")).sort();
    for (const code of ["uz", "uz-Cyrl", "en"]) assert.deepStrictEqual(ak(DICTS[code]), ak(DICTS.ru), code);
    for (const k of [...script.matchAll(/\btr\("([a-z][\w.]+)"\s*[,)]/g)].map((m) => m[1])) {
      for (const [code, dict] of Object.entries(DICTS)) assert.ok(dict[k] !== undefined, `${code}: нет ключа ${k} из admin.js`);
    }
  });
  await test("язык: раздел «Система» и роль на английском; смена языка перерисовывает раздел; подпись параметра — по ключу", async () => {
    const sys = {server_time_utc: "x", server_time_local: "2026-10-10 17:00:00", timezone: "Asia/Tashkent", database: "ok", queue: "ok",
      worker: {online: false, nodes: []}, storage: {path: "data", usage: {exists: true, bytes: 5368709120, files: 7, truncated: true}, disk: null},
      settings: [{key: "media_root", label: "Папка фото", value: "data", source: ".env", editable: false},
                 {key: "future_key", label: "Новый параметр", value: 1, source: "config.yaml", editable: false}], settings_note: "по-русски"};
    const e = makeEnv({lang: "en", token: "t", hash: "#/system", fetchImpl: router(baseRoutes([["/admin/system", () => json(200, sys)]]))});
    await sleep(40);
    let t = e.main.textContent;
    assert.ok(t.includes("not responding") && t.includes("5.0 GB") && t.includes("files: 7+") && t.includes("(incomplete count)"), t);
    assert.ok(t.includes("Photo folder") && t.includes("Новый параметр"), "известный ключ переведён, неизвестный — подпись сервера");
    assert.ok(t.includes("Settings are read-only") && !t.includes("по-русски"));
    assert.strictEqual(e.el("pageTitle").textContent, DICTS.en["nav.system"]);
    e.i18n.setLang("uz-Cyrl"); await sleep(40);
    t = e.main.textContent;
    assert.ok(t.includes("жавоб бермаяпти") && t.includes("5.0 ГБ") && t.includes("Суратлар папкаси"), t);
    assert.strictEqual(e.el("pageTitle").textContent, DICTS["uz-Cyrl"]["nav.system"]);
  });
  await test("язык: ошибка сервера с кодом — на языке человека (запрет удаления с причинами), без кода — текст сервера", () => {
    const body = {detail: "Удалить нельзя: ...", code: "delete_blocked", params: {reasons: [{key: "server.blocker_store_users", params: {n: 2}}]}};
    pure.i18n.setLang("en");
    assert.strictEqual(run(`describeError(409, ${JSON.stringify(body.detail)}, ${JSON.stringify(body)})`),
                       "Cannot delete: users are assigned to the store (2) — remove their access in “Users”");
    assert.strictEqual(run(`describeError(409, "Свой текст", {code: "store_input_invalid"})`), "Свой текст", "запасной код без перевода — текст сервера");
    assert.strictEqual(run(`describeError(403)`), "Not enough permissions");
    pure.i18n.setLang("ru");
  });
  await test("важная операция: сервер просит подтвердить — окно пароля и кода, подтверждение, повтор запроса; отказ — запрос не повторяется", async () => {
    const users = [{id: "u1", username: "admin", role: "admin", is_active: true, store_ids: [], created_at: "x"},
                   {id: "u2", username: "m.karimov", role: "merchandiser", is_active: true, store_ids: ["s1"], created_at: "x"}];
    let resets = 0, confirmed = false;
    const e = makeEnv({token: "t", hash: "#/users", fetchImpl: router(baseRoutes([
      ["/users/u2/2fa/reset", () => { resets++; return confirmed ? json(200, {status: "reset"}) : json(403, {detail: "…", code: "reauth_required", params: {}}); }],
      ["/auth/reauth", (u, o) => { const b = JSON.parse(o.body); if (b.code !== "123456") return json(422, {code: "mfa_code_wrong", params: {}}); confirmed = true; return json(200, {status: "confirmed"}); }],
      [/\/api\/v1\/users$/, () => json(200, users)]]))});
    await sleep(40);
    const row = findAll(e.main, (n) => n.tag === "tr" && n.textContent.includes("m.karimov"))[0];
    assert.ok(!findAll(findAll(e.main, (n) => n.tag === "tr" && n.textContent.includes("admin"))[0], (n) => n.tag === "button" && n.textContent.includes("2FA")).length,
              "себе сбросить 2FA кнопкой нельзя");
    const click = button(row, "Сбросить 2FA").click();
    await sleep(); await e.el("confirmOk").click(); await sleep();
    assert.strictEqual(resets, 1);
    e.el("reauthPass").value = "pw"; e.el("reauthCode").value = "000000";
    await e.el("reauthOk").click(); await sleep();
    assert.strictEqual(e.el("reauthError").textContent, DICTS.ru["server.mfa_code_wrong"]);
    e.el("reauthCode").value = "123456";
    await e.el("reauthOk").click(); await click; await sleep(20);
    assert.strictEqual(resets, 2, "после подтверждения запрос повторён один раз");
    assert.ok(e.el("toasts").textContent.includes("Второй фактор сброшен"));
  });

  await test("адрес для сотрудников (порт 8000): вместо управления — объяснение, куда идти; запросов управления нет", async () => {
    const e = makeEnv({token: "t", hash: "#/users", fetchImpl: router([["/auth/me", () => json(200, Object.assign({}, ME_ADMIN, {admin_listener: false, is_owner: true}))]])});
    await sleep(40);
    assert.ok(e.main.textContent.includes("Здесь управление недоступно") && e.main.textContent.includes("http://localhost:8001/ui/admin.html"));
    assert.ok(!e.calls.some((c) => /\/users|\/stores|\/admin\//.test(c.url)));
  });

  await test("важная операция на этом компьютере: у человека есть ПИН — подтверждение ПИН-кодом, не паролем", async () => {
    const users = [{id: "u1", username: "admin", role: "admin", is_active: true, store_ids: [], created_at: "x"},
                   {id: "u2", username: "m.karimov", role: "merchandiser", is_active: true, store_ids: ["s1"], created_at: "x"}];
    let resets = 0, confirmed = false, body = null;
    const e = makeEnv({token: "t", hash: "#/users", fetchImpl: router(baseRoutes([
      ["/auth/local", () => json(200, {this_computer: true, users: [{username: "admin", role: "admin", is_owner: true}], setup_needed: false})],
      ["/users/u2/2fa/reset", () => { resets++; return confirmed ? json(200, {status: "reset"}) : json(403, {detail: "…", code: "reauth_required", params: {}}); }],
      ["/auth/reauth", (u, o) => { body = JSON.parse(o.body); if (body.pin !== "482917") return json(422, {code: "pin_wrong", params: {}}); confirmed = true; return json(200, {status: "confirmed"}); }],
      [/\/api\/v1\/users$/, () => json(200, users)]]))});
    await sleep(40);
    const row = findAll(e.main, (n) => n.tag === "tr" && n.textContent.includes("m.karimov"))[0];
    const click = button(row, "Сбросить 2FA").click();
    await sleep(); await e.el("confirmOk").click(); await sleep(30);
    assert.ok(e.dom.registry.reauthPin && !e.dom.registry.reauthPass, "окно спрашивает ПИН-код, а не пароль");
    e.el("reauthPin").value = "000001"; await e.el("reauthOk").click(); await sleep();
    assert.strictEqual(e.el("reauthError").textContent, DICTS.ru["server.pin_wrong"]);
    e.el("reauthPin").value = "482917"; await e.el("reauthOk").click(); await click; await sleep(20);
    assert.deepStrictEqual(body, {pin: "482917"}); assert.strictEqual(resets, 2);
  });

  if (unhandled.length) { failed++; console.log(`FAIL необработанные ошибки обработчиков:\n     ${unhandled.map((e) => e && e.stack || e).join("\n     ")}`); }
  finished = true;
  console.log(`\n${passed} passed${failed ? `, ${failed} FAILED` : ""}`);
  process.exit(failed ? 1 : 0);
})();
