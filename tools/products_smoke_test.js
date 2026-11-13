// Проверка страницы «Товары» (frontend/products.html + products.js + ProductList) без браузера: настоящая разметка разбирается в подделку страницы, настоящий
// products.js работает на ней; сервер, файлы и время — подделки, которыми управляет тест. Проверяется ЛОГИКА: вход и права, список (порции по 100, пусто, сбой),
// добавление (фото, отправка, ожидание, сбой воркера, дубль, нет связи, сессия истекла), фото к существующему, правка (цена, штрих-коды), язык на лету, безопасность названий.
// Не проверяется: настоящие фото и камера, настоящий сервер, внешний вид (его смотрят по снимкам Chromium: tools/ui_screenshots/products_shots.py).
// Запуск из корня проекта:  node tools/products_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { text: tx, prefix: px } = require(path.join(__dirname, "test_texts.js"));
const { FakeDocument, parseHtml, makeStorage } = require("./fake_dom.js");
const FRONT = path.join(__dirname, "..", "frontend");
const load = (f) => require(path.join(FRONT, f));
const I18N = load("i18n.js"), Theme = load("theme.js"), Icons = load("icons.js"), Ticket = load("ticket.js"), Catalog = load("catalog.js"), ProductList = load("productlist.js"), Products = load("products.js"), Auth = load("auth.js");
const HTML = fs.readFileSync(path.join(FRONT, "products.html"), "utf-8");
const SCRIPT = fs.readFileSync(path.join(FRONT, "products.js"), "utf-8");
const CSS = fs.readFileSync(path.join(FRONT, "ui.css"), "utf-8");
const DICTS = { ru: load("i18n.ru.js"), uz: load("i18n.uz.js"), en: load("i18n.en.js") };

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const flush = async () => { for (let i = 0; i < 8; i++) await sleep(0); };
const json = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body, blob: async () => ({ size: 3 }) });

const P = (n, over) => Object.assign({ id: "p" + n, sku: "S" + n, name: "Товар " + n, brand: n % 2 ? "Nordic" : "Delta", category: n % 2 ? "Мыши" : "Клавиатуры", expected_price: 1000 * n, photo_count: 2, barcodes: [] }, over || {});
const MANY = (count) => Array.from({ length: count }, (_, i) => P(i + 1));
const ME_ADMIN = { username: "a", role: "admin", permissions: ["catalog:read", "catalog:write", "store:manage"] };
const ME_WRITER = { username: "w", role: "editor", permissions: ["catalog:read", "catalog:write"] };
const ME_READER = { username: "m", role: "merchandiser", permissions: ["catalog:read"] };

// ---------------------------------------------------------------- стенд
function boot(opts) {
  opts = opts || {};
  const doc = new FakeDocument(); parseHtml(doc, HTML);
  const local = makeStorage({}); if (opts.lang) local.setItem("app_lang", opts.lang);
  const session = makeStorage(opts.token === null ? {} : { shelf_audit_token: opts.token || "tok" });
  session.removeItem = (k) => { session._m.delete(k); };
  const i18n = I18N.create({ localStorage: local, navigator: { languages: ["ru"] }, document: doc });
  for (const c of ["ru", "uz", "en"]) i18n.add(c, DICTS[c]);
  const theme = Theme.create({ localStorage: local, document: doc, matchMedia: () => ({ matches: false }) });
  const calls = [], errors = [], urls = { made: 0, revoked: [] };
  let list = opts.list === undefined ? [] : opts.list;
  const me = opts.me === undefined ? ME_ADMIN : opts.me;
  const routes = [
    [/\/auth\/me$/, () => (typeof me === "function" ? me() : json(200, me))],
    [/\/auth\/login$/, (u, o) => (opts.login ? opts.login(u, o) : json(200, { access_token: "new-token" }))],
    [/\/catalog\/products\/upload$/, (u, o) => (opts.upload ? opts.upload(u, o) : json(202, { task_id: "t1", name: "Новый", photos: 3, warnings: [] }))],
    [/\/catalog\/products\/tasks\//, (u, o) => (opts.task ? opts.task(u, o) : json(200, { state: "done" }))],
    [/\/catalog\/products\/[^/?]+\/photos$/, (u, o) => (opts.photos ? opts.photos(u, o) : json(202, { task_id: "t2", name: "Товар 1", photos: 2, warnings: [] }))],
    [/\/catalog\/products\/[^/?]+\/photo\?/, () => (opts.thumb ? opts.thumb() : json(200, {}))],
    [/\/catalog\/products\/[^/?]+$/, (u, o) => (opts.patch ? opts.patch(u, o) : json(200, Object.assign(P(1), { barcodes: [] })))],
    [/\/catalog\/products\?/, (u) => (opts.listHandler ? opts.listHandler(u) : (() => { const q = new URLSearchParams(u.split("?")[1]); const off = +q.get("offset"), lim = +q.get("limit"); return json(200, list.slice(off, off + lim)); })())]
  ];
  const fetch = async (url, o) => {
    calls.push({ url, opts: o || {} });
    for (const [re, h] of routes) if (re.test(url) && (!/\/catalog\/products\/[^/?]+$/.test(url) || (o && o.method) === "PATCH" || re !== routes[6][0])) return h(url, o || {});
    throw new Error("unhandled fetch: " + url);
  };
  class FormDataFake { constructor() { this.items = []; } append(k, v, n) { this.items.push([k, v, n]); } get(k) { const f = this.items.find((x) => x[0] === k); return f ? f[1] : null; } all(k) { return this.items.filter((x) => x[0] === k); } }
  const win = {
    document: doc, I18N: i18n, Theme: theme, Auth, Icons, Ticket, ProductList, fetch, FormData: FormDataFake, localStorage: local, sessionStorage: session, navigator: { languages: ["ru"] },
    location: { search: opts.search || "", protocol: "http:", origin: "http://app", href: "" }, matchMedia: () => ({ matches: false }),
    URL: { createObjectURL: () => "blob:u" + (++urls.made), revokeObjectURL: (u) => { urls.revoked.push(u); } },
    addEventListener(type, fn) { (win._ev[type] = win._ev[type] || []).push(fn); }, _ev: {}
  };
  const $ = (id) => doc.getElementById(id);
  const app = Products.start(win, { tuning: Object.assign({ pollScale: 0.001, pollLimitMs: 5000 }, opts.tuning || {}), onError: (e) => errors.push(e) });
  const s = {
    doc, win, i18n, theme, calls, errors, urls, local, session, app, $, setList: (l) => { list = l; },
    ready: () => app.ready.then(flush),
    shown: (id) => !$(id).hasAttribute("hidden"),
    text: (id) => $(id).textContent,
    rows: () => $("productList").byClass("prow").map((r) => r.byClass("prow__name")[0].textContent),
    listCalls: () => calls.filter((c) => /\/catalog\/products\?/.test(c.url)),
    by: (method, re) => calls.filter((c) => (c.opts.method || "GET") === method && re.test(c.url)),
    type: (id, value) => { $(id).value = value; $(id).dispatch("input"); },
    choose: async (inputId, files) => { const input = $(inputId); input.files = files; input.dispatch("change"); await sleep(10); await flush(); },
    click: async (id) => { $(id).click(); await sleep(15); await flush(); },
    photos: (n) => Array.from({ length: n }, (_, i) => ({ name: `p${i + 1}.jpg`, size: 100, type: "image/jpeg" })),
    openRow: (i) => { $("productList").byClass("prow")[i || 0].click(); },
    fill: async (name, n) => { await s.click("openAdd"); s.type("nameInput", name === undefined ? "Мышь M200" : name); await s.choose("photoInput", s.photos(n === undefined ? 3 : n)); }
  };
  return s;
}
async function withPage(opts, fn) { const s = boot(opts); await s.ready(); await fn(s); assert.deepStrictEqual(s.errors, [], "необработанные ошибки страницы: " + s.errors); }
const sorted = (a) => a.slice().sort();

(async () => {
  // ============================================================ разметка и подключения
  await test("разметка: id, которые трогает products.js, есть на странице и не повторяются; всё, что должно быть скрыто до нужды, скрыто", async () => {
    const markup = HTML.replace(/<script[\s\S]*?<\/script>/gi, "");
    const ids = [...markup.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
    assert.strictEqual(new Set(ids).size, ids.length, "повторяются id");
    const used = new Set([...SCRIPT.matchAll(/\$\("([A-Za-z][\w-]*)"\)/g)].map((m) => m[1]).concat([...SCRIPT.matchAll(/\bshow\("([A-Za-z][\w-]*)"/g)].map((m) => m[1])).concat([...SCRIPT.matchAll(/\b(?:scrollTo|scrollNear)\("([A-Za-z][\w-]*)"\)/g)].map((m) => m[1])));
    for (const id of used) assert.ok(ids.includes(id), `в разметке нет #${id}`);
    const s = boot({ token: null });
    for (const id of ["accessBanner", "mainArea", "addCard", "progressCard", "editCard", "dupNotice", "formError", "editError", "listError", "listNote", "loginError", "listEmpty", "productList", "railAdmin"]) assert.ok(!s.shown(id), `#${id} должен быть скрыт до нужды`);
    assert.ok(s.shown("loginPanel"));
  });

  await test("скрипты: язык и словари раньше значков, ценника, каталога, списка и страницы; без внешних адресов; страница только запускает модуль", async () => {
    const scripts = [...HTML.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]), at = (n) => scripts.indexOf(n);
    for (const n of ["theme.js", "i18n.js", "i18n.ru.js", "i18n.uz.js", "i18n.en.js", "icons.js", "auth.js", "ticket.js", "catalog.js", "productlist.js", "products.js"]) assert.ok(at(n) !== -1, n);
    assert.ok(at("i18n.js") < at("i18n.ru.js") && at("icons.js") < at("productlist.js") && at("ticket.js") < at("productlist.js") && at("catalog.js") < at("productlist.js") && at("productlist.js") < at("products.js") && at("auth.js") < at("products.js"));
    assert.ok(HTML.indexOf('href="tokens.css"') < HTML.indexOf('href="ui.css"') && !/(?:src|href)="(?:https?:)?\/\//.test(HTML));
    assert.deepStrictEqual([...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)], []);
    assert.ok(at("theme.js") + 1 === at("theme-apply.js") && /<script src="boot\.js" data-start="Products"><\/script>/.test(HTML) && scripts[scripts.length - 1] === "boot.js");
  });

  await test("тексты: каждый ключ, на который ссылаются страница и products.js, есть во всех трёх словарях (иначе человек увидит «products.xxx»)", async () => {
    const prefixes = new Set(Object.keys(DICTS.ru).map((k) => k.split(".")[0]));
    const keys = new Set([...SCRIPT.matchAll(/"([a-z_]+\.[a-z_]+)"/g)].map((m) => m[1]).filter((k) => prefixes.has(k.split(".")[0]) && !/\.(html|js|css|jpg|png|json)$/.test(k)));   // «products.html» — адрес страницы, а не ключ текста
    for (const m of HTML.matchAll(/data-i18n="([^"]+)"/g)) keys.add(m[1]);
    for (const m of HTML.matchAll(/data-i18n-attr="([^"]+)"/g)) m[1].split(";").forEach((pair) => keys.add(pair.slice(pair.indexOf(":") + 1).trim()));
    assert.ok(keys.size > 50, "ключей подозрительно мало: " + keys.size);
    for (const k of keys) for (const lang of ["ru", "uz", "en"]) assert.ok(Object.prototype.hasOwnProperty.call(DICTS[lang], k), `нет ключа «${k}» в ${lang}`);
  });

  await test("стили: у каждого класса страницы и products.js есть правило в ui.css; значки существуют; нет innerHTML/eval", async () => {
    const classes = new Set();
    for (const m of HTML.matchAll(/\bclass="([^"]+)"/g)) m[1].split(/\s+/).forEach((c) => classes.add(c));
    for (const m of SCRIPT.matchAll(/(?:className = |setAttribute\("class", )"([^"]+)"/g)) m[1].split(/\s+/).forEach((c) => classes.add(c));
    classes.delete("");
    const missing = [...classes].filter((c) => !new RegExp("\\." + c.replace(/[-_]/g, "[-_]") + "(?![\\w-])").test(CSS));
    assert.deepStrictEqual(missing, [], "нет правила в ui.css: " + missing.join(", "));
    for (const m of HTML.matchAll(/data-icon="([^"]+)"/g)) assert.ok(Icons.has(m[1]), m[1]);
    for (const n of ["plus", "camera", "x", "moon", "sun", "monitor"]) assert.ok(Icons.has(n), n);
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) assert.ok(!SCRIPT.includes(bad), bad);
  });

  // ============================================================ чистая логика (перенесена из прежней страницы)
  await test("fitSize не увеличивает и сохраняет пропорции; photosHint: 0, мало, хорошо, максимум; submitState: название и фото, не во время отправки", async () => {
    assert.deepStrictEqual(Products.fitSize(800, 600, 1600), { w: 800, h: 600 });
    assert.deepStrictEqual(Products.fitSize(4000, 3000, 1600), { w: 1600, h: 1200 }); assert.deepStrictEqual(Products.fitSize(3000, 4000, 1600), { w: 1200, h: 1600 });
    assert.deepStrictEqual(Products.photosHint(0), { key: "products.photos_none", n: 0, level: "" });
    assert.strictEqual(Products.photosHint(1).level, "soso"); assert.strictEqual(Products.photosHint(2).key, "products.photos_few");
    assert.strictEqual(Products.photosHint(3).key, "products.photos_good"); assert.strictEqual(Products.photosHint(Products.MAX_PHOTOS).key, "products.photos_max");
    assert.deepStrictEqual(Products.submitState("Кола", 1, false), { ok: true, whyKey: "" });
    assert.strictEqual(Products.submitState("   ", 2, false).whyKey, "products.need_name"); assert.strictEqual(Products.submitState("Кола", 0, false).whyKey, "products.need_photo");
    assert.strictEqual(Products.submitState("Кола", 2, true).ok, false);
  });

  await test("describeApiError: текст сервера, словарь с message, список ошибок формы, запасные; pollDelay растёт и не выше 4 секунд; barcodeFromSearch и buildEditBody", async () => {
    assert.deepStrictEqual(Products.describeApiError(409, "Уже есть"), { text: "Уже есть" });
    assert.deepStrictEqual(Products.describeApiError(409, { message: "Код занят" }), { text: "Код занят" });
    assert.deepStrictEqual(Products.describeApiError(422, [{ loc: ["x"] }]), { key: "error.form" });
    const has = (k) => Object.prototype.hasOwnProperty.call(DICTS.ru, k);
    const nested = { code: "photo_numbered", params: { n: 2, reason: { key: "server.photo_too_small", params: { width: 40, height: 30 } } } };
    assert.deepStrictEqual(Products.describeApiError(422, "Фото 2: Фото слишком маленькое (40×30). Снимите крупнее.", nested, has),
      { key: "server.photo_numbered", params: nested.params });
    assert.deepStrictEqual(Products.describeApiError(404, "Товар не найден.", { code: "unknown_code" }, has), { text: "Товар не найден." });
    assert.deepStrictEqual(Products.describeApiError(401), { key: "login.expired" }); assert.deepStrictEqual(Products.describeApiError(403), { key: "error.forbidden" });
    assert.deepStrictEqual(Products.describeApiError(413), { key: "error.too_large" }); assert.deepStrictEqual(Products.describeApiError(500), { key: "error.server" });
    assert.deepStrictEqual(Products.describeApiError(400), { key: "error.generic" });
    assert.ok(Products.pollDelay(1) > Products.pollDelay(0)); assert.ok(Products.pollDelay(99) <= 4000); assert.strictEqual(Products.pollDelay(0, 0.001), 1);
    assert.strictEqual(Products.barcodeFromSearch("?barcode=4780001234562"), "4780001234562");
    for (const bad of ["?barcode=abc", "?barcode=123", "?barcode=47800012345621234", "?barcode=<script>", "", "?x=1"]) assert.strictEqual(Products.barcodeFromSearch(bad), "", bad);
    assert.deepStrictEqual(Products.buildEditBody("А", "", ""), { name: "А", clear_price: true });
    assert.deepStrictEqual(Products.buildEditBody("А", " 1500,5 ", " 4780001234562 "), { name: "А", expected_price: "1500,5", add_barcode: "4780001234562" });
  });

  // ============================================================ вход и права
  await test("без входа — форма входа, остальное скрыто и сервер не спрашивается; нет права на запись — «Нет доступа», а не форма; администратору виден «Управление»", async () => {
    await withPage({ token: null }, async (s) => { assert.ok(s.shown("loginPanel") && !s.shown("mainArea") && !s.shown("accessBanner")); assert.strictEqual(s.calls.length, 0); });
    await withPage({ me: ME_READER }, async (s) => { assert.ok(s.shown("accessBanner") && !s.shown("mainArea") && !s.shown("loginPanel")); assert.strictEqual(s.listCalls().length, 0, "список не грузим тем, кому страница не нужна"); });
    await withPage({ me: ME_READER }, async (s) => { assert.ok(s.shown("accessText") && !s.shown("accessListener")); });
    // В-5: администратор на адресе для сотрудников — сервер не перечисляет catalog:write; объяснение — «только на компьютере или через VPN»
    await withPage({ me: { username: "boss", role: "admin", admin_listener: false, permissions: ["catalog:read", "report:read"] } }, async (s) => {
      assert.ok(s.shown("accessBanner") && s.shown("accessListener") && !s.shown("accessText") && !s.shown("mainArea"));
      assert.strictEqual(s.listCalls().length, 0);
    });
    await withPage({ me: ME_ADMIN }, async (s) => { assert.ok(s.shown("mainArea") && s.shown("railAdmin")); assert.strictEqual(s.calls[0].opts.headers.Authorization, "Bearer tok"); });
    await withPage({ me: ME_WRITER }, async (s) => { assert.ok(s.shown("mainArea") && !s.shown("railAdmin")); });
  });

  await test("вход по паролю: токен сохраняется, затем открывается страница; неверный пароль и «слишком много попыток» — понятное сообщение, которое меняет язык вместе с приложением", async () => {
    await withPage({ token: null, login: () => json(401, {}) }, async (s) => {
      s.$("loginPanel").dispatch("submit", { preventDefault() {} }); await sleep(10); await flush();
      assert.strictEqual(s.text("loginError"), "Неверный логин или пароль"); s.i18n.setLang("en"); await flush(); assert.strictEqual(s.text("loginError"), "Wrong username or password");
      assert.strictEqual(s.$("loginBtn").disabled, false);
    });
    await withPage({ token: null, login: () => json(429, {}) }, async (s) => { s.$("loginPanel").dispatch("submit", { preventDefault() {} }); await sleep(10); await flush(); assert.ok(s.text("loginError").includes("Слишком много")); });
    await withPage({ token: null, list: MANY(2) }, async (s) => {
      s.$("loginUser").value = "u"; s.$("loginPass").value = "secret";
      s.$("loginPanel").dispatch("submit", { preventDefault() {} }); await sleep(30); await flush();
      const login = s.calls.find((c) => /auth\/login$/.test(c.url)); assert.ok(login.opts.body.includes("username=u") && login.opts.body.includes("password=secret"));
      assert.strictEqual(s.session.getItem("shelf_audit_token"), "new-token"); assert.strictEqual(s.$("loginPass").value, "");
      assert.ok(s.shown("mainArea") && !s.shown("loginPanel")); assert.strictEqual(s.rows().length, 2);
    });
  });

  await test("сессия истекла: на «me» и на запросе списка и при отправке — показан вход, токен забыт", async () => {
    await withPage({ me: () => json(401, {}) }, async (s) => { assert.ok(s.shown("loginPanel") && s.text("loginError").includes("Сессия истекла")); });
    await withPage({ list: [], listHandler: () => json(401, {}) }, async (s) => { assert.ok(s.shown("loginPanel") && !s.shown("mainArea")); assert.strictEqual(s.session.getItem("shelf_audit_token"), null); });
    await withPage({ list: MANY(2), upload: () => json(401, {}) }, async (s) => { await s.fill(); await s.click("submitBtn"); assert.ok(s.shown("loginPanel") && !s.shown("mainArea")); });
  });

  // ============================================================ список
  await test("список: названия, цена; пустой каталог — подсказка и форма добавления открыта сама; до загрузки не показывается «пока пусто»; сбой загрузки — сообщение, а не пустой список", async () => {
    await withPage({ list: MANY(3) }, async (s) => {
      assert.deepStrictEqual(s.rows(), ["Товар 1", "Товар 2", "Товар 3"]); assert.ok(s.shown("productList") && !s.shown("listEmpty") && !s.shown("addCard") && s.shown("openAdd"));
      assert.ok(s.$("productList").textContent.includes("Найдено: 3 товара"));
    });
    await withPage({ list: [] }, async (s) => { assert.ok(s.shown("listEmpty") && !s.shown("productList") && s.shown("addCard") && !s.shown("openAdd")); assert.ok(s.text("listEmpty").includes("Пока нет ни одного товара")); });
    await withPage({ listHandler: () => json(500, {}) }, async (s) => { assert.ok(s.shown("listError") && s.text("listError").includes("Не удалось загрузить список") && !s.shown("listEmpty") && !s.shown("productList")); });
    await withPage({ listHandler: () => json(200, { не: "список" }) }, async (s) => { assert.ok(s.shown("listError") && !s.shown("listEmpty")); });
    await withPage({ listHandler: () => json(403, {}) }, async (s) => { assert.ok(s.shown("accessBanner") && !s.shown("mainArea")); });
    const s = boot({ list: MANY(1) }); assert.ok(!s.shown("listEmpty") && !s.shown("productList"), "пока список не пришёл — ни список, ни «пусто»"); await s.ready();
  });

  await test("много товаров: страница догружает порциями по 100 (sort=new), пока не придёт неполная порция; ровно 100 — ещё один запрос; поиск и фильтры работают над всеми", async () => {
    await withPage({ list: MANY(250) }, async (s) => {
      const calls = s.listCalls(); assert.strictEqual(calls.length, 3);
      assert.deepStrictEqual(calls.map((c) => new URLSearchParams(c.url.split("?")[1]).get("offset")), ["0", "100", "200"]);
      assert.ok(calls.every((c) => /limit=100/.test(c.url) && /sort=new/.test(c.url))); assert.strictEqual(s.rows().length, 250); assert.ok(s.$("productList").textContent.includes("Найдено: 250 товаров"));
      const search = s.$("productList").querySelector("#listSearch"); search.value = "Товар 7"; search.dispatch("input"); await sleep(400); await flush();
      assert.ok(s.rows().length > 0 && s.rows().length < 250 && s.rows().every((n) => n.includes("7")), "поиск ищет слова (как в витрине): «Товар 17» подходит под «Товар 7»; работает на странице, без запроса к серверу"); assert.strictEqual(s.listCalls().length, 3);
    });
    await withPage({ list: MANY(100) }, async (s) => { assert.strictEqual(s.listCalls().length, 2, "ровно 100 — возможно, есть ещё"); assert.strictEqual(s.rows().length, 100); });
    await withPage({ list: MANY(5200), listHandler: null }, async (s) => { assert.strictEqual(s.listCalls().length, 50, "потолок страницы — 5000 товаров"); assert.strictEqual(s.rows().length, 5000); assert.ok(s.text("listNote").includes("первые 5000")); });
  });

  await test("данные из базы попадают на страницу только текстом: название с тегами и кавычками показывается как есть, разметка не создаётся", async () => {
    const evil = '<img src=x onerror="alert(1)"><script>alert(2)</script>"\'';
    await withPage({ list: [P(1, { name: evil, brand: "<b>Бренд</b>", category: "<i>К</i>" })] }, async (s) => {
      assert.deepStrictEqual(s.rows(), [evil]); assert.strictEqual(s.$("productList").byTag("script").length, 0); assert.ok(!s.$("productList").byTag("img").some((i) => (i.getAttribute("src") || "").includes("x")));
      s.openRow(0); assert.strictEqual(s.$("editName").value, evil); assert.ok(s.text("editTitle").includes(evil)); assert.strictEqual(s.$("editCard").byTag("script").length, 0);
    });
  });

  await test("миниатюры: грузятся с авторизацией только для товаров с фото, повторная загрузка списка берёт их из памяти; после добавления фото к товару его миниатюра обновляется", async () => {
    let thumbs = 0; const items = [P(1), P(2, { photo_count: 0 }), P(3)];
    await withPage({ list: items, thumb: () => { thumbs++; return json(200, {}); } }, async (s) => {
      const asked = s.by("GET", /\/photo\?size=thumb/); assert.deepStrictEqual(sorted(asked.map((c) => c.url.match(/products\/([^/]+)\/photo/)[1])), ["p1", "p3"]);
      assert.ok(asked.every((c) => c.opts.headers.Authorization === "Bearer tok")); assert.strictEqual(s.$("productList").byTag("img").length, 2);
      const before = thumbs; await s.fill("Новый", 1); await s.click("submitBtn"); await sleep(30); await flush();       // добавили товар -> список перезагружен
      assert.strictEqual(thumbs, before, "миниатюры из памяти, повторно не качаем");
      const revokedBefore = s.urls.revoked.length;
      s.openRow(0); await s.click("editAddPhotos"); await s.choose("addPhotoInput", s.photos(1)); await sleep(30); await flush();
      assert.ok(s.by("POST", /products\/p1\/photos$/).length === 1, "фото ушли именно к p1");
      assert.strictEqual(s.urls.revoked.length, revokedBefore + 1, "старая миниатюра p1 освобождена"); assert.strictEqual(thumbs, before + 1, "и перезагружена только она (p3 осталась из памяти)");
    });
  });

  // ============================================================ добавление
  await test("выбор фото: «Добавить» включается только когда есть и название, и фото; подсказка про число фото; × убирает фото и освобождает память; больше 8 за раз не берём — сказано почему", async () => {
    await withPage({ list: MANY(2) }, async (s) => {
      assert.ok(!s.shown("addCard") && s.shown("openAdd")); await s.click("openAdd"); assert.ok(s.shown("addCard") && !s.shown("openAdd"));
      assert.ok(s.$("submitBtn").disabled && s.text("submitHint") === "Напишите название товара."); assert.ok(s.text("photosHint").includes("Добавьте хотя бы одно фото"));
      s.type("nameInput", "Мышь"); assert.ok(s.$("submitBtn").disabled && s.text("submitHint") === "Добавьте хотя бы одно фото.");
      await s.choose("photoInput", s.photos(1)); assert.ok(!s.$("submitBtn").disabled && s.text("submitHint") === ""); assert.ok(s.text("photosHint").includes("1 фото. Лучше 3–5"));
      await s.choose("photoInput", s.photos(2)); assert.ok(s.text("photosHint").includes("3 фото — хорошо") && s.$("photosHint").className.includes("hint--good"));
      const strip = s.$("photoStrip"); assert.strictEqual(strip.byClass("ph").length, 3); assert.strictEqual(strip.byTag("img")[0].getAttribute("alt"), "Фото 1");
      strip.byClass("ph__rm")[0].click(); assert.strictEqual(strip.byClass("ph").length, 2); assert.strictEqual(s.urls.revoked.length, 1);
      strip.byClass("ph__rm")[0].click(); strip.byClass("ph__rm")[0].click(); assert.ok(s.$("submitBtn").disabled, "без фото кнопка снова выключена");
      await s.choose("photoInput", s.photos(11));
      assert.strictEqual(s.$("photoStrip").byClass("ph").length, Products.MAX_PHOTOS); assert.ok(s.text("formError").includes("не больше 8 фото")); assert.ok(s.text("photosHint").includes("максимум"));
      assert.strictEqual(s.$("photoInput").value, "", "поле выбора очищено — тот же файл можно выбрать снова");
    });
  });

  await test("успешное добавление: фото и поля формы уходят ОДНИМ запросом, идёт ожидание, затем «Готово», список обновлён; «Проверить» ведёт на «Узнать товар»; «Добавить ещё» очищает форму", async () => {
    let polls = 0; const items = MANY(2);
    await withPage({ list: items, task: () => json(200, { state: ++polls < 3 ? "running" : "done" }), upload: () => { items.unshift(P(99, { name: "Мышь M200" })); return json(202, { task_id: "t1", name: "Мышь M200", photos: 3, warnings: [{ photo: 2, message: "размыто" }] }); } }, async (s) => {
      await s.fill("Мышь M200", 3); s.type("priceInput", "249000"); s.type("brandInput", "Nordic"); s.type("categoryInput", "Мыши"); s.type("barcodeInput", "4780001234562");
      await s.click("submitBtn");
      const post = s.by("POST", /products\/upload$/); assert.strictEqual(post.length, 1); const fd = post[0].opts.body;
      assert.strictEqual(fd.all("files").length, 3); assert.deepStrictEqual(["name", "price", "brand", "category", "barcode"].map((k) => fd.get(k)), ["Мышь M200", "249000", "Nordic", "Мыши", "4780001234562"]);
      await sleep(40); await flush();
      assert.ok(polls >= 3, "ждали, пока воркер закончит"); assert.ok(s.shown("progressCard") && s.shown("doneBlock") && !s.shown("progressRow") && !s.shown("addCard"));
      assert.ok(s.text("doneText").includes("Готово: «Мышь M200» добавлен в каталог.")); assert.ok(s.text("warnList").includes("Фото 2: размыто"));
      assert.ok(s.rows().includes("Мышь M200"), "список обновился"); assert.strictEqual(s.$("tryLink").getAttribute("href"), "identify.html");
      await s.click("againBtn");
      assert.ok(s.shown("addCard") && !s.shown("progressCard")); for (const id of ["nameInput", "priceInput", "brandInput", "categoryInput", "barcodeInput"]) assert.strictEqual(s.$(id).value, "", id);
      assert.strictEqual(s.$("photoStrip").byClass("ph").length, 0); assert.ok(s.$("submitBtn").disabled);
    });
  });

  await test("повторное нажатие «Добавить» во время отправки не создаёт второй запрос; пока идёт отправка, кнопка выключена", async () => {
    let release; const gate = new Promise((r) => { release = r; });
    await withPage({ list: MANY(1), upload: () => gate.then(() => json(202, { task_id: "t1", name: "Н", photos: 1, warnings: [] })) }, async (s) => {
      await s.fill("Мышь", 1); s.$("submitBtn").click(); s.$("submitBtn").click(); await sleep(15); await flush();
      assert.strictEqual(s.by("POST", /products\/upload$/).length, 1); assert.ok(s.$("submitBtn").disabled && s.shown("progressRow") && s.text("progressText").includes("Загружаем фото"));
      release(); await sleep(40); await flush(); assert.ok(s.shown("doneBlock"));
    });
  });

  await test("сбой воркера: понятная причина на виду, подробности — под «Для специалиста»; можно вернуться и форма с введённым цела", async () => {
    await withPage({ list: MANY(1), task: () => json(200, { state: "failed", message: "Не удалось прочитать фото 2.", detail: "ValueError: cannot identify image file" }) }, async (s) => {
      await s.fill("Мышь", 2); await s.click("submitBtn"); await sleep(20); await flush();
      assert.ok(s.shown("failBlock") && !s.shown("doneBlock") && !s.shown("progressRow")); assert.strictEqual(s.text("failText"), "Не удалось прочитать фото 2."); assert.ok(s.text("failDetail").includes("cannot identify image"));
      await s.click("failBack"); assert.ok(s.shown("addCard") && !s.shown("progressCard")); assert.strictEqual(s.$("nameInput").value, "Мышь"); assert.strictEqual(s.$("photoStrip").byClass("ph").length, 2); assert.ok(!s.$("submitBtn").disabled);
    });
    await withPage({ list: MANY(1), task: () => json(200, { state: "failed" }) }, async (s) => { await s.fill(); await s.click("submitBtn"); await sleep(20); await flush(); assert.strictEqual(s.text("failText"), "Не получилось добавить товар."); assert.strictEqual(s.text("failDetail"), "Подробностей нет."); });
  });

  await test("коды ошибок: повтор (409) и сбой задачи переводятся по коду на язык человека и перерисовываются при смене языка; неизвестный код — прежний текст", async () => {
    const dup = { detail: { message: "Товар «Мышь» уже есть в каталоге.", product_id: "p1", product_name: "Мышь" }, code: "product_duplicate", params: { name: "Мышь" } };
    await withPage({ list: MANY(2), upload: () => json(409, dup), lang: "en" }, async (s) => {
      await s.fill("Мышь", 1); await s.click("submitBtn");
      assert.strictEqual(s.text("dupTitle"), "“Мышь” is already in the catalog."); assert.ok(s.shown("dupYes"), "product_id из detail по-прежнему работает");
      s.i18n.setLang("uz"); await flush(); assert.strictEqual(s.text("dupTitle"), "«Мышь» mahsuloti katalogda allaqachon bor.");
    });
    const taken = { detail: { message: "Штрих-код 4780001234562 уже у товара «Пепси»." }, code: "barcode_taken", params: { code: "4780001234562", name: "Пепси" } };
    await withPage({ list: MANY(2), upload: () => json(409, taken), lang: "en" }, async (s) => {
      await s.fill("X", 1); await s.click("submitBtn"); assert.strictEqual(s.text("dupTitle"), "Barcode 4780001234562 is already linked to “Пепси”."); assert.ok(!s.shown("dupYes"));
    });
    await withPage({ list: MANY(2), upload: () => json(409, { detail: { message: "Старый текст" }, code: "no_such_code" }), lang: "en" }, async (s) => {
      await s.fill("X", 1); await s.click("submitBtn"); assert.strictEqual(s.text("dupTitle"), "Старый текст");
    });
    await withPage({ list: MANY(1), lang: "en", task: () => json(200, { state: "failed", message: "Фото слишком маленькое (10×20).", code: "photo_too_small", params: { width: 10, height: 20 }, detail: null }) }, async (s) => {
      await s.fill("Мышь", 1); await s.click("submitBtn"); await sleep(20); await flush();
      assert.strictEqual(s.text("failText"), "The photo is too small (10×20). Take it closer.");
      s.i18n.setLang("ru"); await flush(); assert.strictEqual(s.text("failText"), "Фото слишком маленькое (10×20). Снимите крупнее.");
    });
    await withPage({ list: MANY(1), task: () => json(200, { state: "failed", message: "Свой текст", code: "catalog_error" }) }, async (s) => {
      await s.fill(); await s.click("submitBtn"); await sleep(20); await flush(); assert.strictEqual(s.text("failText"), "Свой текст", "запасной код без перевода — конкретный текст сервера");
    });
  });

  await test("ошибка формы от сервера (422/400): текст сервера в форме, ввод не потерян; нет связи: сообщение, форма остаётся; 5xx и 413 — запасные тексты", async () => {
    for (const [status, detail, text] of [[422, "Название слишком длинное", "Название слишком длинное"], [422, [{ loc: ["name"] }], "Проверьте, что поля формы заполнены верно."], [500, undefined, "Что-то пошло не так на сервере"], [413, undefined, "Файл слишком большой."]]) {
      await withPage({ list: MANY(1), upload: () => json(status, detail === undefined ? {} : { detail }) }, async (s) => {
        await s.fill("Мышь", 2); await s.click("submitBtn");
        assert.ok(s.shown("addCard") && !s.shown("progressCard") && s.shown("formError") && s.text("formError").includes(text), s.text("formError")); assert.strictEqual(s.$("nameInput").value, "Мышь"); assert.strictEqual(s.$("photoStrip").byClass("ph").length, 2);
        assert.ok(!s.$("submitBtn").disabled && !s.app.state().busy, "можно пробовать снова");
      });
    }
    await withPage({ list: MANY(1), upload: () => { throw new Error("offline"); } }, async (s) => { await s.fill(); await s.click("submitBtn"); assert.ok(s.shown("addCard") && s.text("formError").includes("Нет связи с сервером")); s.i18n.setLang("en"); await flush(); assert.ok(s.text("formError").startsWith("Can\u2019t reach the server"), s.text("formError")); });
    await withPage({ list: MANY(1), upload: () => json(403, {}) }, async (s) => { await s.fill(); await s.click("submitBtn"); assert.ok(s.shown("accessBanner") && !s.shown("mainArea")); });
  });

  await test("слишком долгое ожидание: честное «дольше обычного» и кнопка «Проверить ещё раз», не бесконечное ожидание; нет связи при опросе — отдельный текст; повторная проверка доводит до «Готово»", async () => {
    let mode = "running";
    await withPage({ list: MANY(1), tuning: { pollLimitMs: 30 }, task: () => { if (mode === "net") throw new Error("offline"); return json(200, { state: mode }); } }, async (s) => {
      await s.fill(); await s.click("submitBtn"); await sleep(60); await flush();
      assert.ok(s.shown("slowBlock") && !s.shown("progressRow") && s.text("slowText").includes("дольше обычного")); assert.ok(s.$("slowCheck"));
      mode = "done"; await s.click("slowCheck"); await sleep(20); await flush(); assert.ok(s.shown("doneBlock") && !s.shown("slowBlock"));
    });
    await withPage({ list: MANY(1), task: () => { throw new Error("offline"); } }, async (s) => { await s.fill(); await s.click("submitBtn"); await sleep(20); await flush(); assert.ok(s.shown("slowBlock") && s.text("slowText").includes("Нет связи с сервером")); });
  });

  await test("такое название уже есть (409): предложено добавить фото к существующему; «да» отправляет ТЕ ЖЕ фото к нему; «Это другой товар» — подсказка как уточнить, ввод сохранён; смена названия прячет уведомление", async () => {
    const dup = { detail: { message: "Товар «Мышь M200» уже есть в каталоге.", product_id: "p1", product_name: "Мышь M200" } };
    await withPage({ list: MANY(2), upload: () => json(409, dup) }, async (s) => {
      await s.fill("Мышь M200", 3); await s.click("submitBtn");
      assert.ok(s.shown("dupNotice") && s.shown("addCard") && s.text("dupTitle") === "Товар «Мышь M200» уже есть в каталоге." && s.shown("dupYes"));
      await s.click("dupYes"); await sleep(30); await flush();
      const sent = s.by("POST", /products\/p1\/photos$/); assert.strictEqual(sent.length, 1); assert.strictEqual(sent[0].opts.body.all("files").length, 3, "те же фото");
      assert.ok(s.shown("doneBlock") && !s.shown("dupNotice")); assert.strictEqual(s.text("doneText"), "Готово: к «Товар 1» добавлено фото: 2.");
    });
    await withPage({ list: MANY(2), upload: () => json(409, dup) }, async (s) => {
      await s.fill("Мышь M200", 2); await s.click("submitBtn"); await s.click("dupNo");
      assert.ok(!s.shown("dupNotice") && s.text("formError").includes("Уточните название")); assert.strictEqual(s.$("nameInput").value, "Мышь M200"); assert.strictEqual(s.$("photoStrip").byClass("ph").length, 2);
      await s.click("submitBtn"); assert.ok(s.shown("dupNotice")); s.type("nameInput", "Мышь M200 Pro"); assert.ok(!s.shown("dupNotice"), "название изменили — уведомление о повторе убрано");
    });
    await withPage({ list: MANY(2), upload: () => json(409, { detail: { message: "Штрих-код 4780001234562 уже у товара «Пепси»." } }) }, async (s) => {
      await s.fill("Что-то", 1); s.type("barcodeInput", "4780001234562"); await s.click("submitBtn");
      assert.ok(s.shown("dupNotice") && s.text("dupTitle").includes("Штрих-код 4780001234562") && !s.shown("dupYes"), "без product_id предложить «добавить к этому» нельзя");
    });
    await withPage({ list: MANY(2), upload: () => json(409, {}), lang: "en" }, async (s) => { await s.fill("X", 1); await s.click("submitBtn"); assert.strictEqual(s.text("dupTitle"), "This product is already in the catalog."); s.i18n.setLang("ru"); await flush(); assert.strictEqual(s.text("dupTitle"), "Такой товар уже есть в каталоге."); });
  });

  await test("ошибка в штрих-коде (опечатка) показана простыми словами рядом с формой, ввод не потерян; со страницы «Узнать товар» адрес ?barcode= открывает форму с кодом, мусор в адресе игнорируется", async () => {
    await withPage({ list: MANY(1), upload: () => json(422, { detail: "Штрих-код: только цифры, 8–14 знаков." }) }, async (s) => {
      await s.fill("Мышь", 1); s.type("barcodeInput", "47800ABC"); await s.click("submitBtn");
      assert.ok(s.text("formError").includes("только цифры") && s.$("barcodeInput").value === "47800ABC" && s.$("nameInput").value === "Мышь");
    });
    await withPage({ list: MANY(2), search: "?barcode=4780001234562" }, async (s) => { assert.strictEqual(s.$("barcodeInput").value, "4780001234562"); assert.ok(s.shown("addCard") && s.$("moreDetails").hasAttribute("open")); });
    await withPage({ list: MANY(2), search: "?barcode=<script>1</script>" }, async (s) => { assert.strictEqual(s.$("barcodeInput").value, ""); assert.ok(!s.shown("addCard")); });
  });

  // ============================================================ фото к существующему, правка
  await test("«Добавить фото» в правке: открывает выбор файлов и отправляет их именно этому товару; не больше 8; ошибка показывается рядом со списком, а не теряется", async () => {
    await withPage({ list: MANY(3) }, async (s) => {
      s.openRow(1); let opened = 0; s.$("addPhotoInput").click = () => { opened++; };
      await s.click("editAddPhotos"); assert.strictEqual(opened, 1);
      await s.choose("addPhotoInput", s.photos(10)); await sleep(30); await flush();
      const sent = s.by("POST", /products\/p2\/photos$/); assert.strictEqual(sent.length, 1); assert.strictEqual(sent[0].opts.body.all("files").length, 8);
      assert.ok(s.shown("doneBlock") && s.text("doneText").includes("добавлено фото")); assert.ok(s.shown("progressCard"));
    });
    await withPage({ list: MANY(3), photos: () => json(413, {}) }, async (s) => {
      s.openRow(0); await s.click("editAddPhotos"); await s.choose("addPhotoInput", s.photos(1));
      assert.ok(s.shown("listNote") && s.text("listNote").includes("Файл слишком большой.") && s.$("listNote").className.includes("notice--danger")); assert.ok(!s.shown("progressCard"));
    });
    await withPage({ list: MANY(3), photos: () => { throw new Error("offline"); } }, async (s) => { s.openRow(0); await s.click("editAddPhotos"); await s.choose("addPhotoInput", s.photos(1)); assert.ok(s.text("listNote").includes("Нет связи с сервером")); });
    await withPage({ list: MANY(3) }, async (s) => { s.openRow(0); await s.click("editAddPhotos"); await s.choose("addPhotoInput", []); assert.strictEqual(s.by("POST", /photos$/).length, 0, "пустой выбор ничего не отправляет"); });
  });

  await test("правка: поля заполнены текущими значениями, «Сохранить» шлёт PATCH, панель закрывается, список обновлён, «Сохранено.»; пустая цена — clear_price, а не пустая строка; отмена закрывает без запроса", async () => {
    const items = [P(1, { name: "Старое", expected_price: 1500.5 }), P(2, { expected_price: null })];
    await withPage({ list: items, patch: () => { items[0].name = "Новое"; return json(200, items[0]); } }, async (s) => {
      s.openRow(0); assert.ok(s.shown("editCard")); assert.strictEqual(s.$("editName").value, "Старое"); assert.strictEqual(s.$("editPrice").value, "1500,5"); assert.strictEqual(s.text("editTitle"), "Изменить: Старое");
      s.$("editName").value = "Новое"; await s.click("editSave");
      const patch = s.by("PATCH", /products\/p1$/)[0]; assert.deepStrictEqual(JSON.parse(patch.opts.body), { name: "Новое", expected_price: "1500,5" }); assert.strictEqual(patch.opts.headers["Content-Type"], "application/json");
      assert.ok(!s.shown("editCard") && s.text("listNote") === "Сохранено." && s.$("listNote").className.includes("hint--good")); assert.ok(s.rows().includes("Новое"));
      s.openRow(1); assert.strictEqual(s.$("editPrice").value, "", "у товара без цены поле цены пустое"); assert.strictEqual(s.text("editTitle"), "Изменить: Товар 2");
    });
    await withPage({ list: [P(1, { expected_price: 100 })] }, async (s) => {
      s.openRow(0); s.$("editPrice").value = "   "; await s.click("editSave"); assert.deepStrictEqual(JSON.parse(s.by("PATCH", /p1$/)[0].opts.body), { name: "Товар 1", clear_price: true });
    });
    await withPage({ list: MANY(2) }, async (s) => { s.openRow(0); await s.click("editCancel"); assert.ok(!s.shown("editCard")); assert.strictEqual(s.by("PATCH", /./).length, 0); await s.click("editSave"); assert.strictEqual(s.by("PATCH", /./).length, 0, "после закрытия «Сохранить» ничего не шлёт"); });
  });

  await test("правка: отказ сервера (название занято) показан в панели правки, панель остаётся открытой, ввод цел; нет связи — сообщение; кнопка снова доступна", async () => {
    await withPage({ list: MANY(2), patch: () => json(409, { detail: "Товар с таким названием уже есть." }) }, async (s) => {
      s.openRow(0); s.$("editName").value = "Занято"; await s.click("editSave");
      assert.ok(s.shown("editCard") && s.text("editError") === "Товар с таким названием уже есть." && s.$("editName").value === "Занято" && !s.$("editSave").disabled);
      s.i18n.setLang("en"); await flush(); assert.strictEqual(s.text("editError"), "Товар с таким названием уже есть.", "готовый текст сервера не переводится");
    });
    await withPage({ list: MANY(2), patch: () => { throw new Error("offline"); } }, async (s) => { s.openRow(0); await s.click("editSave"); assert.ok(s.shown("editCard") && s.text("editError").includes("Нет связи с сервером")); });
  });

  await test("штрих-коды в правке: показаны; новый код уходит как add_barcode вместе с остальным; пустое поле не отправляется; «Штрих-кодов пока нет»; «×» отвязывает именно этот код (remove_barcode), список кодов обновляется; отказ сервера — текст в панели", async () => {
    const item = P(1, { barcodes: ["4780001234562", "4006381333931"] });
    await withPage({ list: [item, P(2)], patch: (u, o) => { const b = JSON.parse(o.body); if (b.remove_barcode) item.barcodes = item.barcodes.filter((c) => c !== b.remove_barcode); return json(200, item); } }, async (s) => {
      s.openRow(0); assert.deepStrictEqual(s.$("editCodes").byClass("tag").map((t) => t.children[0].textContent), ["4780001234562", "4006381333931"]);
      s.$("editBarcode").value = "4780009999990"; await s.click("editSave"); assert.deepStrictEqual(JSON.parse(s.by("PATCH", /./)[0].opts.body), { name: "Товар 1", expected_price: "1000", add_barcode: "4780009999990" });
      s.openRow(0); const rm = s.$("editCodes").byClass("icon-btn")[0]; assert.strictEqual(rm.getAttribute("aria-label"), "Отвязать штрих-код 4780001234562"); rm.click(); await sleep(20); await flush();
      assert.deepStrictEqual(JSON.parse(s.by("PATCH", /./)[1].opts.body), { remove_barcode: "4780001234562" }); assert.deepStrictEqual(s.$("editCodes").byClass("tag").map((t) => t.children[0].textContent), ["4006381333931"]); assert.ok(s.shown("editCard"));
      s.i18n.setLang("en"); await flush(); assert.strictEqual(s.$("editCodes").byClass("icon-btn")[0].getAttribute("aria-label"), "Unlink barcode 4006381333931");
    });
    await withPage({ list: [P(1), P(2)] }, async (s) => { s.openRow(0); assert.ok(s.text("editCodes").includes("Штрих-кодов пока нет.")); await s.click("editSave"); assert.ok(!("add_barcode" in JSON.parse(s.by("PATCH", /./)[0].opts.body)), "пустой код не отправляется"); });
    await withPage({ list: [P(1, { barcodes: ["4780001234562"] })], patch: () => json(409, { detail: "Штрих-код уже у товара «Пепси»." }) }, async (s) => {
      s.openRow(0); s.$("editCodes").byClass("icon-btn")[0].click(); await sleep(20); await flush(); assert.ok(s.shown("editCard") && s.text("editError").includes("уже у товара «Пепси»"));
    });
    await withPage({ list: [P(1, { barcodes: ["4780001234562"] })], patch: () => { throw new Error("offline"); } }, async (s) => { s.openRow(0); s.$("editCodes").byClass("icon-btn")[0].click(); await sleep(20); await flush(); assert.ok(s.text("editError").includes("Нет связи с сервером")); });
  });

  // ============================================================ язык, тема, меню
  await test("смена языка на лету: подсказки, ожидание, «Готово», заголовок правки, подпись кнопок и список перерисовываются; выбор запоминается; названия товаров не переводятся", async () => {
    await withPage({ list: MANY(2) }, async (s) => {
      await s.fill("Мышь", 2); await s.click("submitBtn"); await sleep(30); await flush();
      assert.ok(s.text("doneText").includes("Готово")); s.i18n.setLang("en"); await flush();
      assert.strictEqual(s.text("doneText"), "Done: “Новый” was added to the catalog."); assert.ok(s.$("openAdd").textContent.includes("Add product")); assert.ok(s.$("productList").textContent.includes("Found: 2 products"));
      assert.strictEqual(s.doc.title, "Products — Identify Product"); s.i18n.setLang("uz"); await flush();
      assert.ok(s.text("doneText").startsWith(px("uz", "products.done_new"))); assert.strictEqual(s.local.getItem("app_lang"), "uz"); assert.ok(s.rows().includes("Товар 1"));
      s.openRow(0); assert.ok(s.text("editTitle").startsWith(px("uz", "products.edit_title"))); s.i18n.setLang("ru"); await flush(); assert.strictEqual(s.text("editTitle"), "Изменить: Товар 1");
    });
    await withPage({ list: MANY(2) }, async (s) => {
      await s.click("openAdd"); await s.choose("photoInput", s.photos(2)); assert.ok(s.text("photosHint").startsWith("2 фото.")); s.i18n.setLang("uz"); await flush(); assert.ok(s.text("photosHint").startsWith(tx("uz", "photos.count", { n: 2 }) + "."), s.text("photosHint"));
      assert.strictEqual(s.$("photoStrip").byTag("img")[0].getAttribute("alt"), tx("uz", "products.photo_alt", { n: 1 })); assert.strictEqual(s.$("photoStrip").byClass("ph__rm")[0].getAttribute("aria-label"), tx("uz", "products.photo_remove", { n: 1 }));
    });
  });

  await test("тема переключается кнопкой и запоминается; меню: «Товары» помечено текущим; ссылки на настоящие страницы; при закрытии страницы миниатюры из памяти освобождаются", async () => {
    await withPage({ list: MANY(2) }, async (s) => {
      const label = () => s.$("themeBtn").getAttribute("aria-label"); assert.ok(label().includes("как в системе")); await s.click("themeBtn"); assert.ok(label().includes("светлая")); await s.click("themeBtn"); assert.strictEqual(s.local.getItem("app_theme"), "dark");
      const links = s.$("rail").byTag("a"); assert.deepStrictEqual(links.map((a) => a.getAttribute("href")), ["identify.html", "products.html", "settings.html", "admin.html"]); assert.strictEqual(links[1].getAttribute("aria-current"), "page");
      assert.strictEqual(s.$("langHost").byClass("lang-btn").length, 4); assert.strictEqual(s.doc.title, "Товары — Узнать товар");
      const made = s.urls.made; assert.ok(made >= 2); (s.win._ev.pagehide || []).forEach((fn) => fn()); assert.strictEqual(s.urls.revoked.length, made, "все блоб-адреса миниатюр освобождены");
    });
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
