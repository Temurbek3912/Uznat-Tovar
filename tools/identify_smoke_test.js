// Проверка страницы «Узнать товар» (frontend/identify.html + identify.js) без браузера: настоящая разметка разбирается в подделку страницы,
// настоящий identify.js работает на ней; камера и сервер — подделки, которыми управляет тест. Проверяется ЛОГИКА: когда отправлять кадр,
// что и когда говорить, как показывать ответ, права, вход, ошибки, привязка штрих-кода, язык на лету, настройки. Не проверяется: настоящая камера
// и её шум, настоящий сервер и модель, внешний вид (его смотрят по снимкам Chromium: tools/ui_screenshots/identify_shots.py).
// Запуск из корня проекта:  node tools/identify_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { FakeDocument, parseHtml, makeStorage } = require("./fake_dom.js");
const FRONT = path.join(__dirname, "..", "frontend");
const I18N = require(path.join(FRONT, "i18n.js")), Theme = require(path.join(FRONT, "theme.js")), Icons = require(path.join(FRONT, "icons.js"));
const Ticket = require(path.join(FRONT, "ticket.js")), Identify = require(path.join(FRONT, "identify.js")), Auth = require(path.join(FRONT, "auth.js"));
const HTML = fs.readFileSync(process.env.IDENTIFY_HTML || path.join(FRONT, "identify.html"), "utf-8");
const SCRIPT = fs.readFileSync(path.join(FRONT, "identify.js"), "utf-8");
const CSS = fs.readFileSync(path.join(FRONT, "ui.css"), "utf-8");
const DICTS = { ru: require(path.join(FRONT, "i18n.ru.js")), uz: require(path.join(FRONT, "i18n.uz.js")), en: require(path.join(FRONT, "i18n.en.js")) };

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const flush = async () => { for (let i = 0; i < 6; i++) await sleep(0); };
const json = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });

const COLA = { id: "p-cola", name: "Кола 0,5 л", brand: "BrandCola", expected_price: 9900, score: 0.93 };
const LIME = { id: "p-lime", name: "Лимонад лайм", brand: "Не указан", expected_price: null, score: 0.88 };
const MATCHED = (p) => ({ status: "matched", product: p, score: p.score, candidates: [p], demo_mode: false });
const UNCERTAIN = (p, other) => ({ status: "uncertain", product: p, score: p.score, candidates: [p, other], demo_mode: false });
const BY_BARCODE = (p) => Object.assign(MATCHED(p), { by: "barcode", barcode: { code: "4780001234562", known: true } });
const UNKNOWN = { status: "unknown", product: null, score: 0.2, candidates: [{ id: "x", name: "Запасной вариант", score: 0.3 }], demo_mode: false };
const EMPTY = { status: "empty_catalog", product: null, score: 0, candidates: [], demo_mode: false };
const ME_ADMIN = { username: "a", role: "admin", permissions: ["catalog:read", "catalog:write", "store:manage"] };
const ME_MERCH = { username: "m", role: "merchandiser", permissions: ["catalog:read"] };
const ME_NONE = { username: "x", role: "analyst", permissions: ["report:read"] };

// ---------------------------------------------------------------- стенд
function boot(opts) {
  opts = opts || {};
  const doc = new FakeDocument(); parseHtml(doc, HTML);
  const local = opts.local || makeStorage(opts.settings === undefined ? { identify_settings: JSON.stringify({ auto: true }) } : (opts.settings ? { identify_settings: opts.settings } : {}));
  if (opts.lang) local.setItem("app_lang", opts.lang);
  const session = makeStorage(opts.token === null ? {} : { shelf_audit_token: opts.token || "tok" });
  session.removeItem = (k) => { session._m.delete(k); };                       // в настоящем браузере removeItem есть
  const i18n = I18N.create({ localStorage: local, navigator: { languages: ["ru"] }, document: doc });
  for (const c of ["ru", "uz", "en"]) i18n.add(c, DICTS[c]);
  const theme = Theme.create({ localStorage: local, document: doc, matchMedia: () => ({ matches: false }) });
  const calls = [], errors = [], cam = { scene: 50, stops: 0, constraints: null, widths: [], alternate: false, fail: null, starts: 0 };
  const me = opts.me === undefined ? ME_ADMIN : opts.me;
  const routes = [
    [/\/auth\/me$/, () => (typeof me === "function" ? me() : json(200, me))],
    [/\/auth\/login$/, (u, o) => (opts.login ? opts.login(u, o) : json(200, { access_token: "new-token" }))],
    [/\/catalog\/identify$/, (u, o) => { const a = typeof opts.answer === "function" ? opts.answer(u, o) : opts.answer; return Promise.resolve(a).then((r) => (r && r.status && r.ok !== undefined ? r : json(200, r))); }],
    [/\/catalog\/products\//, (u, o) => (opts.patch ? opts.patch(u, o) : json(200, {}))]
  ];
  const fetch = async (url, o) => {
    calls.push({ url, opts: o || {} });
    for (const [re, h] of routes) if (re.test(url)) return h(url, o || {});
    throw new Error("unhandled fetch: " + url);
  };
  class FormDataFake { constructor() { this.items = []; } append(k, v, n) { this.items.push([k, v, n]); } }
  const navigator = { languages: ["ru"] };
  if (opts.camera !== "none") navigator.mediaDevices = { getUserMedia: async (c) => { cam.constraints = c; cam.starts++; if (cam.fail) throw cam.fail; return { getTracks: () => [{ stop: () => { cam.stops++; } }] }; } };
  const win = {
    document: doc, I18N: i18n, Theme: theme, Auth, Ticket, Icons, fetch, FormData: FormDataFake, localStorage: local, sessionStorage: session, navigator,
    isSecureContext: opts.secure === undefined ? true : opts.secure, location: { search: opts.search || "", protocol: "http:", origin: "http://app", href: "" },
    addEventListener(type, fn) { (win._ev[type] = win._ev[type] || []).push(fn); }, _ev: {}, matchMedia: () => ({ matches: false })
  };
  // озвучки в программе больше нет: любое обращение страницы к синтезатору речи или к модулю Voice — ошибка (попадает в s.errors)
  const forbidden = (name) => ({ get() { errors.push(new Error("страница обратилась к win." + name + ": озвучки больше нет")); return undefined; }, enumerable: true });
  Object.defineProperties(win, { Voice: forbidden("Voice"), speechSynthesis: forbidden("speechSynthesis") });
  const $ = (id) => doc.getElementById(id);
  const video = $("video"); video.videoWidth = 1920; video.videoHeight = 1080; video.play = async () => {};
  const grab = $("grab");
  grab.getContext = () => ({ drawImage() {}, getImageData: (x, y, w, h) => { if (cam.alternate) cam.scene = cam.scene === 40 ? 220 : 40; return { data: new Uint8ClampedArray(w * h * 4).fill(cam.scene) }; } });
  grab.toBlob = (cb, type) => { cam.widths.push(grab.width); cb({ size: 5, type }); };
  const app = Identify.start(win, { tuning: Object.assign({ tickMs: 15 }, opts.tuning || {}), onError: (e) => errors.push(e) });
  const s = {
    doc, win, i18n, theme, calls, errors, cam, local, session, app, $,
    ready: () => app.ready.then(flush),
    shown: (id) => !$(id).hasAttribute("hidden"),
    ticket: () => $("ticket").textContent,
    kind: () => $("ticket").getAttribute("data-state"),
    stage: () => $("stage").getAttribute("data-state"),
    identifyCalls: () => calls.filter((c) => /catalog\/identify$/.test(c.url)),
    hist: () => $("histList").children.map((li) => li.children[0].textContent),
    byText: (text) => $("ticket").byTag("button").find((b) => b.textContent.includes(text)),
    file: async (name) => { const input = $("fileInput"); input.files = [{ name: name || "a.jpg", size: 10, type: "image/jpeg" }]; input.dispatch("change"); await sleep(25); await flush(); },
    click: async (id) => { $(id).click(); await sleep(15); await flush(); },
    camOn: async () => { await s.click("camBtn"); },
    dispose: () => { if (s.app.state().live) $("camBtn").click(); }
  };
  return s;
}
// текст первого элемента ценника с таким классом
const $t = (s, cls) => s.$("ticket").byClass(cls)[0].textContent;
async function withPage(opts, fn) { const s = boot(opts); try { await s.ready(); await fn(s); } finally { s.dispose(); } }

(async () => {
  // ============================================================ разметка и подключения
  await test("разметка: каждый id, который трогает identify.js, есть на странице, id не повторяются, скрытые части скрыты", async () => {
    const markup = HTML.replace(/<script[\s\S]*?<\/script>/gi, "");
    const ids = [...markup.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
    assert.strictEqual(new Set(ids).size, ids.length, "повторяются id");
    const used = new Set([...SCRIPT.matchAll(/\$\("([A-Za-z][\w-]*)"\)/g)].map((m) => m[1]).concat([...SCRIPT.matchAll(/\bshow\("([A-Za-z][\w-]*)"/g)].map((m) => m[1])));
    for (const id of used) assert.ok(ids.includes(id), `в разметке нет #${id}`);
    const s = boot({ token: null });
    for (const id of ["accessBanner", "mainArea", "demoBanner", "barcodeNote", "linkBtn", "resError", "railProducts", "railAdmin", "video"]) assert.ok(!s.shown(id), `#${id} должен быть скрыт до нужды`);
    assert.ok(s.shown("loginPanel"), "без токена видна форма входа");
  });

  await test("порядок и состав скриптов: язык и словари раньше значков, ценника и страницы; озвучки нет; без внешних адресов; страница только запускает модуль", async () => {
    const scripts = [...HTML.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]);
    const at = (n) => scripts.indexOf(n);
    for (const n of ["theme.js", "i18n.js", "i18n.ru.js", "i18n.uz.js", "i18n.en.js", "icons.js", "auth.js", "ticket.js", "identify.js"]) assert.ok(at(n) !== -1, n);
    assert.strictEqual(at("voice.js"), -1, "озвучки больше нет: модуль голоса не подключается");
    assert.ok(at("i18n.js") < at("i18n.ru.js") && at("i18n.js") < at("i18n.uz.js") && at("i18n.js") < at("i18n.en.js"));
    assert.ok(at("icons.js") < at("ticket.js") && at("auth.js") < at("identify.js") && at("ticket.js") < at("identify.js"));
    assert.ok(HTML.indexOf('href="tokens.css"') < HTML.indexOf('href="ui.css"') && HTML.indexOf("ui.css") < HTML.indexOf("theme.js"));
    assert.ok(!/(?:src|href)=\"(?:https?:)?\/\//.test(HTML), "внешний адрес");
    assert.deepStrictEqual([...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)], [], "встроенных скриптов нет (CSP script-src 'self'); своей логики в странице нет — она в identify.js");
    assert.ok(at("theme.js") + 1 === at("theme-apply.js"), "тема применяется сразу после theme.js");
    assert.ok(/<script src="boot\.js" data-start="Identify"><\/script>/.test(HTML) && scripts[scripts.length - 1] === "boot.js" && at("identify.js") < at("boot.js"));
  });

  await test("тексты: каждый ключ, на который ссылаются страница и identify.js, есть во всех трёх словарях (иначе человек увидит «error.camera_x»)", async () => {
    const prefixes = new Set(Object.keys(DICTS.ru).map((k) => k.split(".")[0]));
    const keys = new Set([...SCRIPT.matchAll(/"([a-z_]+\.[a-z_]+)"/g)].map((m) => m[1]).filter((k) => prefixes.has(k.split(".")[0]) && !/\.(html|js|css|jpg|png|json)$/.test(k)));   // «products.html» — адрес страницы, а не ключ текста
    for (const m of HTML.matchAll(/data-i18n="([^"]+)"/g)) keys.add(m[1]);
    for (const m of HTML.matchAll(/data-i18n-attr="[^:]+:([^";]+)"/g)) keys.add(m[1]);
    assert.ok(keys.size > 30, "ключей подозрительно мало: " + keys.size);
    for (const k of keys) for (const lang of ["ru", "uz", "en"]) assert.ok(Object.prototype.hasOwnProperty.call(DICTS[lang], k), `нет ключа «${k}» в ${lang}`);
  });

  await test("стили: у каждого класса страницы и identify.js есть правило в ui.css; значки страницы существуют", async () => {
    const classes = new Set();
    for (const m of HTML.matchAll(/\bclass="([^"]+)"/g)) m[1].split(/\s+/).forEach((c) => classes.add(c));
    for (const m of SCRIPT.matchAll(/className = [^"]*"([^"]+)"/g)) m[1].split(/\s+/).forEach((c) => classes.add(c));
    for (const m of SCRIPT.matchAll(/\.className = "([^"]+)"/g)) m[1].split(/\s+/).forEach((c) => classes.add(c));
    classes.delete("");
    const missing = [...classes].filter((c) => !new RegExp("\\." + c.replace(/[-_]/g, "[-_]") + "(?![\\w-])").test(CSS));
    assert.deepStrictEqual(missing, [], "нет правила в ui.css: " + missing.join(", "));
    for (const m of HTML.matchAll(/data-icon="([^"]+)"/g)) assert.ok(Icons.has(m[1]), "нет значка " + m[1]);
    for (const n of ["x", "viewfinder", "camera", "moon", "sun", "monitor"]) assert.ok(Icons.has(n), "нет значка " + n);
  });

  await test("в identify.js нет innerHTML/outerHTML/insertAdjacentHTML/eval — данные из базы и сервера попадают только как текст", async () => {
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) assert.ok(!SCRIPT.includes(bad), bad);
  });

  // ============================================================ чистая логика (перенесена из прежней страницы без изменений)
  await test("frameDiff: одинаковые кадры — 0, разные — средняя разница, несравнимые — бесконечность", async () => {
    assert.strictEqual(Identify.frameDiff([1, 2, 3], [1, 2, 3]), 0);
    assert.strictEqual(Identify.frameDiff([0, 0], [10, 20]), 15);
    assert.strictEqual(Identify.frameDiff([1], [1, 2]), Infinity);
    assert.strictEqual(Identify.frameDiff(null, [1]), Infinity);
    assert.strictEqual(Identify.frameDiff([], []), Infinity);
  });

  await test("shouldSend: не шлём «руку в движении» и одно и то же; шлём, когда картинка замерла и стала другой", async () => {
    const a = new Array(10).fill(50), b = new Array(10).fill(200), c = new Array(10).fill(52);
    assert.strictEqual(Identify.shouldSend(a, b, null), false, "кадр ещё движется");
    assert.strictEqual(Identify.shouldSend(b, b, null), true, "замер, раньше не спрашивали");
    assert.strictEqual(Identify.shouldSend(b, b, b), false, "то же, что уже отправляли");
    assert.strictEqual(Identify.shouldSend(a, a, b), true, "замер и отличается от отправленного");
    assert.strictEqual(Identify.shouldSend(a, c, a), false, "мелкое дрожание не считается новым товаром");
  });

  await test("viewFor: уверенно / похоже (запасные варианты после главного, без дублей) / не знаю / пустой каталог; бренд «Не указан» скрыт", async () => {
    const m = Identify.viewFor(MATCHED(COLA));
    assert.strictEqual(m.state, "matched"); assert.strictEqual(m.product.name, "Кола 0,5 л"); assert.strictEqual(m.product.brand, "BrandCola");
    assert.strictEqual(Identify.viewFor(MATCHED(LIME)).product.brand, "", "«Не указан» не показываем как бренд");
    const u = Identify.viewFor(UNCERTAIN(COLA, LIME));
    assert.strictEqual(u.state, "uncertain"); assert.deepStrictEqual(u.candidates.map((c) => c.id), ["p-cola", "p-lime"]);
    assert.deepStrictEqual(Identify.viewFor({ status: "uncertain", product: COLA, candidates: [COLA, COLA, LIME] }).candidates.map((c) => c.id), ["p-cola", "p-lime"]);
    assert.strictEqual(Identify.viewFor(UNKNOWN).state, "unknown");
    assert.strictEqual(Identify.viewFor(EMPTY).state, "empty_catalog");
    assert.strictEqual(Identify.viewFor(null).state, "unknown");
  });

  await test("настройки: умолчание — автоузнавание включено; мусор и поля старых записей (speak, price) отбрасываются, страницу не ломают", async () => {
    assert.deepStrictEqual(Identify.cleanSettings(null), { auto: true });
    assert.deepStrictEqual(Identify.cleanSettings({ speak: true, auto: false, price: false, lang: "uz" }), { auto: false });
    assert.deepStrictEqual(Identify.cleanSettings({ speak: "да", auto: 1, price: null }), { auto: true });
    assert.deepStrictEqual(Identify.cleanSettings("мусор"), { auto: true });
    assert.strictEqual(Identify.nextSpeech, undefined); assert.strictEqual(Identify.speechFor, undefined);
  });

  await test("cameraErrorKey: у каждой причины свой текст, не https — отдельный; describeApiError: текст сервера важнее, 504 и 5xx — запасные", async () => {
    assert.strictEqual(Identify.cameraErrorKey(null, false), "error.camera_insecure");
    assert.strictEqual(Identify.cameraErrorKey({ name: "NotAllowedError" }, true), "error.camera_denied");
    assert.strictEqual(Identify.cameraErrorKey({ name: "NotFoundError" }, true), "error.camera_missing");
    assert.strictEqual(Identify.cameraErrorKey({ name: "NotReadableError" }, true), "error.camera_busy");
    assert.strictEqual(Identify.cameraErrorKey({ name: "SecurityError" }, true), "error.camera_blocked");
    assert.strictEqual(Identify.cameraErrorKey(new Error("?"), true), "error.camera_failed");
    for (const k of ["insecure", "denied", "missing", "busy", "blocked", "failed"]) for (const l of ["ru", "uz", "en"]) assert.ok(DICTS[l]["error.camera_" + k].length > 10);
    assert.deepStrictEqual(Identify.describeApiError(400, "Файл слишком большой"), { text: "Файл слишком большой" });
    assert.deepStrictEqual(Identify.describeApiError(400, { message: "Не изображение" }), { text: "Не изображение" });
    assert.deepStrictEqual(Identify.describeApiError(504, undefined), { key: "error.timeout" });
    assert.deepStrictEqual(Identify.describeApiError(500, ""), { key: "error.recognize_failed" });
    assert.deepStrictEqual(Identify.describeApiError(400, undefined), { key: "error.generic" });
    // код ошибки с переводом -> сообщение на языке приложения; без перевода или без функции has -> прежний текст сервера
    const has = (k) => Object.prototype.hasOwnProperty.call(DICTS.en, k);
    assert.deepStrictEqual(Identify.describeApiError(413, "Кадр: файл больше 10 МБ.", { code: "frame_too_large", params: { mb: 10 } }, has),
      { key: "server.frame_too_large", params: { mb: 10 } });
    assert.deepStrictEqual(Identify.describeApiError(422, "Странная ошибка", { code: "no_such_code" }, has), { text: "Странная ошибка" });
    assert.deepStrictEqual(Identify.describeApiError(422, "Текст", { code: "frame_too_large" }), { text: "Текст" });
    assert.deepStrictEqual(Identify.describeApiError(422, "Текст", { code: "../evil" }, () => true), { text: "Текст" });
  });

  // ============================================================ вход и права
  await test("без входа — форма входа; без права на каталог — «Нет доступа»; с правом — страница; меню зависит от прав", async () => {
    await withPage({ token: null }, async (s) => {
      assert.ok(s.shown("loginPanel") && !s.shown("mainArea") && !s.shown("accessBanner"));
      assert.strictEqual(s.calls.length, 0, "без токена сервер не спрашиваем");
    });
    await withPage({ me: ME_NONE }, async (s) => { assert.ok(s.shown("accessBanner") && !s.shown("mainArea") && !s.shown("loginPanel")); });
    await withPage({ me: ME_ADMIN }, async (s) => {
      assert.ok(s.shown("mainArea") && !s.shown("loginPanel") && !s.shown("accessBanner"));
      assert.ok(s.shown("railProducts") && s.shown("railAdmin"), "администратору видны «Товары» и «Управление»");
      assert.strictEqual(s.kind(), "idle", "в начале — приглашение показать товар");
      assert.ok(s.ticket().includes("Покажите товар камере"));
      assert.strictEqual(s.calls[0].opts.headers.Authorization, "Bearer tok");
    });
    await withPage({ me: ME_MERCH }, async (s) => { assert.ok(s.shown("mainArea") && !s.shown("railProducts") && !s.shown("railAdmin"), "мерчандайзеру только «Узнать товар»"); });
  });

  await test("вход: неверный пароль — сообщение, успех — токен сохранён, форма скрыта, страница открыта; сообщение меняет язык вместе с приложением", async () => {
    await withPage({ token: null, login: () => json(401, {}) }, async (s) => {
      s.$("loginUser").value = "u"; s.$("loginPass").value = "bad";
      s.$("loginPanel").dispatch("submit", { preventDefault() {} }); await flush(); await sleep(10);
      assert.ok(s.shown("loginError") && s.$("loginError").textContent === "Неверный логин или пароль");
      s.i18n.setLang("en"); await flush();
      assert.strictEqual(s.$("loginError").textContent, "Wrong username or password");
      assert.strictEqual(s.$("loginBtn").disabled, false);
    });
    await withPage({ token: null, login: () => json(429, {}) }, async (s) => {
      s.$("loginPanel").dispatch("submit", { preventDefault() {} }); await flush(); await sleep(10);
      assert.ok(s.$("loginError").textContent.includes("Слишком много"));
    });
    await withPage({ token: null }, async (s) => {
      s.$("loginUser").value = "u"; s.$("loginPass").value = "secret";
      s.$("loginPanel").dispatch("submit", { preventDefault() {} }); await sleep(25); await flush();
      const login = s.calls.find((c) => /auth\/login$/.test(c.url));
      assert.ok(login.opts.body.includes("username=u") && login.opts.body.includes("password=secret"));
      assert.strictEqual(s.session.getItem("shelf_audit_token"), "new-token");
      assert.strictEqual(s.$("loginPass").value, "", "пароль не остаётся в поле");
      assert.ok(s.shown("mainArea") && !s.shown("loginPanel"));
    });
  });

  await test("сессия истекла (401 на «me» или на узнавании) — вход и остановка камеры; 403 на узнавании — баннер «Нет доступа»", async () => {
    await withPage({ me: () => json(401, {}) }, async (s) => { assert.ok(s.shown("loginPanel") && s.$("loginError").textContent.includes("Сессия истекла")); });
    await withPage({ answer: json(401, {}) }, async (s) => {
      await s.camOn(); await s.click("snapBtn");
      assert.ok(s.shown("loginPanel") && !s.shown("mainArea")); assert.strictEqual(s.cam.stops, 1, "камера остановлена");
      assert.strictEqual(s.session.getItem("shelf_audit_token"), null);
    });
    await withPage({ answer: json(403, {}) }, async (s) => { await s.file(); assert.ok(s.shown("accessBanner") && !s.shown("mainArea")); });
  });

  // ============================================================ ответы на экране
  await test("показали товар (фото): название крупно на ценнике, бренд, цена, «Найдено»; добавлено в «что уже показывали»; рамки «найден»", async () => {
    await withPage({ answer: MATCHED(COLA) }, async (s) => {
      await s.file();
      assert.strictEqual(s.kind(), "matched"); assert.strictEqual(s.stage(), "matched");
      assert.strictEqual($t(s, "ticket__name"), "Кола 0,5 л"); assert.strictEqual($t(s, "ticket__brand"), "BrandCola");
      assert.strictEqual($t(s, "ticket__amount"), "9\u00A0900"); assert.ok(s.ticket().includes("Найдено"));
      assert.ok(!s.byText("Озвучить"), "кнопки «Озвучить» больше нет");
      assert.deepStrictEqual(s.hist(), ["Кола 0,5 л"]); assert.ok(!s.shown("histEmpty"));
      const post = s.identifyCalls()[0]; assert.strictEqual(post.opts.method, "POST");
      assert.strictEqual(post.opts.body.items[0][0], "file"); assert.strictEqual(post.opts.headers.Authorization, "Bearer tok");
    });
  });

  await test("несколько товаров подряд: новый сверху истории, одинаковые подряд показываются, история не длиннее 8", async () => {
    let n = 0; const seq = [COLA, LIME, COLA, COLA];
    await withPage({ answer: () => MATCHED(seq[Math.min(n++, seq.length - 1)]) }, async (s) => {
      for (let i = 0; i < 4; i++) await s.file();
      assert.deepStrictEqual(s.hist(), ["Кола 0,5 л", "Кола 0,5 л", "Лимонад лайм", "Кола 0,5 л"]);
    });
    let k = 0;
    await withPage({ answer: () => MATCHED({ id: "p" + k, name: "Т" + k++, brand: "", expected_price: 1 }) }, async (s) => {
      for (let i = 0; i < 11; i++) await s.file();
      assert.strictEqual(s.hist().length, Identify.HISTORY_MAX); assert.strictEqual(s.hist()[0], "Т10");
    });
  });

  await test("неизвестный товар: «Не найдено», рамки «не найден», кнопка «Добавить в каталог» только тем, кто может; в историю не попадает", async () => {
    await withPage({ answer: UNKNOWN }, async (s) => {
      await s.file();
      assert.strictEqual(s.kind(), "unknown"); assert.strictEqual(s.stage(), "unknown");
      assert.ok(s.ticket().includes("Этого товара нет в каталоге") && s.byText("Добавить в каталог") && s.byText("Снять ещё раз"));
      assert.strictEqual(s.hist().length, 0);
      s.byText("Добавить в каталог").click(); assert.strictEqual(s.win.location.href, "products.html");
    });
    await withPage({ answer: UNKNOWN, me: ME_MERCH }, async (s) => {
      await s.file();
      assert.ok(!s.byText("Добавить в каталог"), "без права записи кнопки нет"); assert.ok(s.ticket().includes("Попросите того, кто ведёт каталог"));
    });
  });

  await test("«Похоже»: пунктирные рамки, список вариантов; выбор варианта показывает его как найденный и пишет в историю; «Не тот товар» открывает остальные варианты", async () => {
    await withPage({ answer: UNCERTAIN(COLA, LIME) }, async (s) => {
      await s.file();
      assert.strictEqual(s.kind(), "uncertain"); assert.deepStrictEqual(s.$("ticket").byClass("cand__name").map((n) => n.textContent), ["Кола 0,5 л", "Лимонад лайм"]);
      assert.deepStrictEqual(s.hist(), ["Кола 0,5 л (похоже)"]);
      s.$("ticket").byClass("cand")[1].click();
      assert.strictEqual(s.kind(), "matched"); assert.strictEqual($t(s, "ticket__name"), "Лимонад лайм"); assert.strictEqual(s.hist()[0], "Лимонад лайм");
      s.byText("Не тот товар").click();
      assert.strictEqual(s.kind(), "uncertain", "остались варианты — показываем их"); assert.deepStrictEqual(s.$("ticket").byClass("cand__name").map((n) => n.textContent), ["Кола 0,5 л"]);
    });
    await withPage({ answer: MATCHED(COLA) }, async (s) => { await s.file(); s.byText("Не тот товар").click(); assert.strictEqual(s.kind(), "unknown", "вариантов нет — предлагаем добавить"); });
  });

  await test("пустой каталог и учебный режим: сказано прямо; ответ заглушки не выдаётся за настоящий", async () => {
    await withPage({ answer: EMPTY }, async (s) => { await s.file(); assert.strictEqual(s.kind(), "empty_catalog"); assert.ok(s.ticket().includes("В каталоге пока нет товаров")); });
    await withPage({ answer: Object.assign(MATCHED(COLA), { demo_mode: true }) }, async (s) => { assert.ok(!s.shown("demoBanner")); await s.file(); assert.ok(s.shown("demoBanner")); });
    await withPage({ answer: MATCHED(COLA) }, async (s) => { await s.file(); assert.ok(!s.shown("demoBanner")); });
  });

  // ============================================================ настройки на странице
  await test("переключатель «Узнавать сама»: выбор запоминается и возвращается; испорченные настройки — умолчания; старые поля speak/price из памяти не мешают", async () => {
    await withPage({ settings: "", answer: MATCHED(COLA) }, async (s) => {      // первый запуск: память пуста
      assert.ok(s.$("autoToggle").checked, "по умолчанию включено");
      s.$("autoToggle").checked = false; s.$("autoToggle").dispatch("change");
      assert.deepStrictEqual(JSON.parse(s.local.getItem("identify_settings")), { auto: false });
      await s.file(); assert.ok(s.ticket().includes("Кола 0,5 л"));
    });
    await withPage({ settings: JSON.stringify({ auto: false }) }, async (s) => { assert.ok(!s.$("autoToggle").checked); });
    await withPage({ settings: JSON.stringify({ speak: true, auto: false, price: false }) }, async (s) => { assert.ok(!s.$("autoToggle").checked, "поле auto из старой записи читается"); assert.deepStrictEqual(s.errors, []); });
    await withPage({ settings: "{не json" }, async (s) => { assert.ok(s.$("autoToggle").checked && s.shown("mainArea")); });
  });

  await test("озвучки нет: на странице нет ни переключателей голоса, ни кнопок «Озвучить» и «Проверить звук»; страница не трогает Voice и speechSynthesis", async () => {
    await withPage({ answer: MATCHED(COLA) }, async (s) => {
      for (const id of ["speakToggle", "speakPrice", "soundBtn", "voiceOpts", "speechHint"]) assert.ok(s.$(id) === null, `#${id} должен исчезнуть`);
      await s.file(); assert.ok(!s.byText("Озвучить"));
      s.i18n.setLang("uz"); await flush(); s.i18n.setLang("en"); await flush();
      assert.deepStrictEqual(s.errors, [], "ни одного обращения к озвучке: " + s.errors);
    });
    assert.ok(!/Voice|speechSynthesis|speechSummary|nextSpeech/.test(SCRIPT), "в identify.js не осталось следов озвучки");
  });

  await test("смена языка на лету: ценник, кнопки, подписи, история, штрих-код и ошибки перерисовываются; выбор запоминается", async () => {
    await withPage({ answer: Object.assign(UNCERTAIN(COLA, LIME), { barcode: { code: "4780001234562", known: true } }) }, async (s) => {
      await s.file();
      assert.ok(s.ticket().includes("Похоже") && s.$("barcodeNote").textContent === "Штрих-код 4780001234562");
      s.i18n.setLang("en"); await flush();
      assert.ok(s.ticket().includes("Similar") || s.ticket().includes("one of these"), s.ticket());
      assert.strictEqual(s.$("barcodeNote").textContent, "Barcode 4780001234562");
      assert.strictEqual(s.hist()[0], "Кола 0,5 л (likely)", "подпись истории тоже переведена");
      assert.ok(s.$("camBtn").textContent.includes("Turn on camera") && s.$("fileBtn").textContent.includes("Take photo") && s.$("snapBtn").textContent.includes("Identify product"));
      assert.strictEqual(s.doc.title, "Identify Product");
      s.i18n.setLang("uz"); await flush();
      assert.ok(s.$("camBtn").textContent.includes(DICTS.uz["identify.camera_on"])); assert.strictEqual(s.local.getItem("app_lang"), "uz");
    });
  });

  // ============================================================ камера
  await test("камера включается (просит заднюю), «Узнать товар» делает снимок и называет, повторное нажатие выключает и освобождает камеру", async () => {
    await withPage({ answer: MATCHED(COLA), tuning: { tickMs: 100000 } }, async (s) => {
      assert.ok(s.$("snapBtn").disabled && s.$("camBtn").textContent.includes("Включить камеру") && s.$("camBtn").className.includes("btn--primary"));
      await s.camOn();
      assert.deepStrictEqual(s.cam.constraints.video.facingMode, { ideal: "environment" }); assert.strictEqual(s.cam.constraints.audio, false);
      assert.ok(!s.$("snapBtn").disabled && s.shown("video") && !s.shown("camPlaceholder") && s.$("stage").getAttribute("data-live") === "true");
      assert.ok(s.$("camBtn").textContent.includes("Выключить камеру") && s.$("camBtn").className.includes("btn--secondary") && s.$("snapBtn").className.includes("btn--primary"));
      await s.click("snapBtn");
      assert.strictEqual(s.kind(), "matched");
      await s.click("camBtn");
      assert.strictEqual(s.cam.stops, 1); assert.ok(s.$("snapBtn").disabled && !s.shown("camPlaceholder") === false && !s.shown("video"));
      assert.strictEqual(s.$("video").srcObject, null);
    });
  });

  await test("кадры для узнавания крупные (1280 px по длинной стороне): на кадре поменьше мелкий штрих-код не прочитать", async () => {
    await withPage({ answer: MATCHED(COLA), tuning: { tickMs: 100000 } }, async (s) => {
      await s.camOn(); await s.click("snapBtn");
      assert.ok(s.cam.widths.includes(1280), "ширина кадра: " + s.cam.widths);
    });
    assert.deepStrictEqual(Identify.fitSize(1920, 1080, 1280), { w: 1280, h: 720 }); assert.deepStrictEqual(Identify.fitSize(640, 480, 1280), { w: 640, h: 480 });
    assert.deepStrictEqual(Identify.fitSize(1080, 1920, 1280), { w: 720, h: 1280 });
  });

  await test("камера недоступна: у каждой причины своё сообщение в ценнике, есть запасной путь «Сделать фото»; камера не включена; повтор после исправления снимает ошибку", async () => {
    const cases = [["NotAllowedError", "Доступ к камере закрыт"], ["NotFoundError", "Камера не найдена"], ["NotReadableError", "занимает другая программа"], ["SecurityError", "не разрешил камеру"], ["Weird", "Не удалось включить камеру"]];
    for (const [name, text] of cases) await withPage({}, async (s) => {
      s.cam.fail = Object.assign(new Error(name), { name }); await s.camOn();
      assert.strictEqual(s.kind(), "camera_error", name); assert.ok(s.ticket().includes(text), name + ": " + s.ticket());
      assert.ok(s.byText("Сделать фото"), "запасной путь"); assert.ok(!s.shown("video") && s.$("snapBtn").disabled);
      s.cam.fail = null; s.byText("Повторить").click(); await sleep(15); await flush();
      assert.strictEqual(s.kind(), "idle", "камера включилась — ошибка ушла"); assert.ok(s.app.state().live);
    });
    await withPage({ secure: false }, async (s) => { await s.camOn(); assert.ok(s.ticket().includes("только по защищённому адресу")); assert.strictEqual(s.cam.starts, 0, "на небезопасном адресе камеру даже не просим"); });
    await withPage({ camera: "none", secure: false }, async (s) => { assert.strictEqual(s.kind(), "camera_error", "телефон по http: сразу объясняем и даём «Сделать фото»"); assert.ok(s.byText("Сделать фото")); });
  });

  await test("файл работает без камеры: «Сделать фото» открывает выбор файла; пустой выбор ничего не отправляет", async () => {
    await withPage({ camera: "none", answer: MATCHED(COLA) }, async (s) => {
      let opened = 0; s.$("fileInput").click = () => { opened++; };
      await s.click("fileBtn"); assert.strictEqual(opened, 1);
      s.byText("Сделать фото").click(); assert.strictEqual(opened, 2);
      s.$("fileInput").files = []; s.$("fileInput").dispatch("change"); await flush(); assert.strictEqual(s.identifyCalls().length, 0);
      await s.file(); assert.strictEqual(s.kind(), "matched");
    });
  });

  await test("повторное «Узнать» во время ответа не создаёт параллельный запрос; пока идёт запрос, рамки «ищем», а у ручного запроса — «Ищу…»", async () => {
    let release; const gate = new Promise((r) => { release = r; });
    await withPage({ answer: () => gate.then(() => json(200, MATCHED(COLA))), tuning: { tickMs: 100000 } }, async (s) => {
      await s.camOn(); s.$("snapBtn").click(); await sleep(15); await flush();
      assert.strictEqual(s.stage(), "searching"); assert.strictEqual(s.$("stage").getAttribute("data-busy"), "true"); assert.ok(s.ticket().includes("Ищу"));
      s.$("snapBtn").click(); s.$("snapBtn").click(); await sleep(15); await flush();
      await s.file();                                          // выбрали файл, пока запрос ещё идёт: другой путь к тому же запросу
      assert.strictEqual(s.identifyCalls().length, 1, "во время запроса новый не создаётся ни кнопкой, ни файлом");
      release(); await sleep(25); await flush();
      assert.strictEqual(s.identifyCalls().length, 1); assert.strictEqual(s.kind(), "matched"); assert.strictEqual(s.$("stage").getAttribute("data-busy"), "false");
    });
  });

  await test("автоматически: пустой кадр → показали товар → убрали → показали другой; не шлёт одно и то же", async () => {
    const answer = (u, o) => { const sc = s_.cam.scene; return sc === 200 ? MATCHED(COLA) : sc === 120 ? MATCHED(LIME) : UNKNOWN; };
    let s_;
    s_ = boot({ answer });
    try {
      await s_.ready(); await s_.camOn();
      await sleep(80); assert.strictEqual(s_.identifyCalls().length, 1, "пустой кадр спросили один раз"); assert.strictEqual(s_.kind(), "unknown");
      s_.cam.scene = 200; await sleep(90); assert.strictEqual(s_.identifyCalls().length, 2); assert.strictEqual(s_.kind(), "matched"); assert.strictEqual($t(s_, "ticket__name"), "Кола 0,5 л");
      await sleep(80); assert.strictEqual(s_.identifyCalls().length, 2, "то же самое не шлём");
      s_.cam.scene = 50; await sleep(90); assert.strictEqual(s_.identifyCalls().length, 3, "товар убрали"); assert.strictEqual(s_.kind(), "unknown");
      s_.cam.scene = 120; await sleep(90); assert.strictEqual($t(s_, "ticket__name"), "Лимонад лайм"); assert.deepStrictEqual(s_.hist(), ["Лимонад лайм", "Кола 0,5 л"]);
    } finally { s_.dispose(); }
  });

  await test("автоматический режим выключен переключателем — сама не отправляет; движущаяся картинка (рука перед камерой) не отправляется", async () => {
    await withPage({ settings: JSON.stringify({ auto: false }), answer: MATCHED(COLA) }, async (s) => {
      await s.camOn(); s.cam.scene = 200; await sleep(100); assert.strictEqual(s.identifyCalls().length, 0);
    });
    await withPage({ answer: MATCHED(COLA) }, async (s) => { s.cam.alternate = true; await s.camOn(); await sleep(150); assert.strictEqual(s.identifyCalls().length, 0, "кадр дёргается — не шлём"); });
  });

  // ============================================================ ошибки
  await test("ошибки сервера и сети понятны и не ломают страницу: прежний ответ остаётся, текст ошибки под ним, следующая попытка очищает", async () => {
    let mode = "ok";
    const answer = () => { if (mode === "net") throw new Error("offline"); if (mode === "500") return json(500, { detail: "" }); if (mode === "504") return json(504, {}); if (mode === "text") return json(400, { detail: "Файл не картинка" }); if (mode === "bad") return { status: 200, ok: true, json: async () => { throw new Error("bad json"); } }; return json(200, MATCHED(COLA)); };
    await withPage({ answer }, async (s) => {
      await s.file(); assert.strictEqual(s.kind(), "matched");
      for (const [m, text] of [["net", "Нет связи с сервером"], ["500", "Не получилось распознать"], ["504", "не успела ответить"], ["text", "Файл не картинка"], ["bad", "Не получилось распознать"]]) {
        mode = m; await s.file();
        assert.ok(s.shown("resError") && s.$("resError").textContent.includes(text), m + ": " + s.$("resError").textContent);
        assert.strictEqual(s.kind(), "matched", "прежний ответ не стёрт"); assert.strictEqual(s.app.state().inflight, false, "после ошибки можно пробовать снова");
      }
      mode = "ok"; await s.file(); assert.ok(!s.shown("resError"), "удачный ответ убирает ошибку");
      assert.deepStrictEqual(s.errors, [], "необработанных ошибок нет: " + s.errors);
    });
  });

  // ============================================================ штрих-код
  await test("узнан по штрих-коду: номер кода виден; кода нет — строки нет; следующий ответ без кода убирает прежние сообщения и ссылку", async () => {
    let next = BY_BARCODE(COLA);
    await withPage({ answer: () => next }, async (s) => {
      await s.file(); assert.strictEqual(s.$("barcodeNote").textContent, "Штрих-код 4780001234562"); assert.ok(!s.shown("linkBtn"));
      next = MATCHED(LIME); await s.file(); assert.ok(!s.shown("barcodeNote") && !s.shown("linkBtn"));
    });
  });

  await test("код прочитан, но товара с ним нет: «Не найдено», кнопка «Добавить» ведёт на страницу товаров с этим кодом", async () => {
    const r = { status: "unknown", product: null, score: 0.1, candidates: [], demo_mode: false, barcode: { code: "4780009999990", known: false } };
    await withPage({ answer: r }, async (s) => {
      await s.file(); assert.strictEqual(s.kind(), "unknown"); assert.ok(s.$("barcodeNote").textContent.includes("пока не привязан"));
      s.byText("Добавить в каталог").click(); assert.strictEqual(s.win.location.href, "products.html?barcode=4780009999990");
    });
  });

  await test("товар узнан по виду, а код ему не привязан: предложено привязать; по кнопке уходит PATCH и сообщается «Готово»", async () => {
    const r = Object.assign(MATCHED(COLA), { barcode: { code: "4780001234562", known: false } });
    await withPage({ answer: r }, async (s) => {
      await s.file(); assert.ok(s.shown("linkBtn") && s.$("linkBtn").textContent === "Привязать этот штрих-код к «Кола 0,5 л»");
      await s.click("linkBtn");
      const patch = s.calls.find((c) => c.opts.method === "PATCH");
      assert.ok(patch.url.endsWith("/api/v1/catalog/products/p-cola")); assert.deepStrictEqual(JSON.parse(patch.opts.body), { add_barcode: "4780001234562" });
      assert.strictEqual(patch.opts.headers.Authorization, "Bearer tok");
      assert.ok(s.$("barcodeNote").textContent.startsWith("Готово: штрих-код 4780001234562 привязан к «Кола 0,5 л»") && !s.shown("linkBtn"));
      s.i18n.setLang("en"); await flush(); assert.ok(s.$("barcodeNote").textContent.startsWith("Done: barcode 4780001234562"));
    });
  });

  await test("привязка: отказ сервера (код у другого товара) показан понятно, кнопка остаётся; нет связи — сообщение; без права записи кнопки нет; «похоже» не привязывается", async () => {
    const r = Object.assign(MATCHED(COLA), { barcode: { code: "4780001234562", known: false } });
    await withPage({ answer: r, patch: () => json(409, { detail: "Штрих-код уже у товара «Пепси»" }) }, async (s) => {
      await s.file(); await s.click("linkBtn");
      assert.ok(s.$("resError").textContent.includes("уже у товара «Пепси»") && s.shown("linkBtn") && !s.$("linkBtn").disabled);
    });
    await withPage({ answer: r, patch: () => { throw new Error("offline"); } }, async (s) => { await s.file(); await s.click("linkBtn"); assert.ok(s.$("resError").textContent.includes("Нет связи с сервером") && s.shown("linkBtn")); });
    await withPage({ answer: r, me: ME_MERCH }, async (s) => { await s.file(); assert.ok(!s.shown("linkBtn"), "без catalog:write привязывать нельзя"); });
    await withPage({ answer: Object.assign(UNCERTAIN(COLA, LIME), { barcode: { code: "4780001234562", known: false } }) }, async (s) => { await s.file(); assert.ok(!s.shown("linkBtn"), "только к УВЕРЕННО узнанному товару"); });
  });

  // ============================================================ тема и меню
  await test("тема: кнопка переключает системная → светлая → тёмная, подпись и значок меняются, выбор запоминается; страница закрывается — камера останавливается", async () => {
    await withPage({ tuning: { tickMs: 100000 } }, async (s) => {
      const label = () => s.$("themeBtn").getAttribute("aria-label");
      assert.ok(label().includes("как в системе")); await s.click("themeBtn"); assert.ok(label().includes("светлая")); await s.click("themeBtn"); assert.ok(label().includes("тёмная"));
      assert.strictEqual(s.local.getItem("app_theme"), "dark");
      await s.camOn(); (s.win._ev.pagehide || []).forEach((fn) => fn()); assert.strictEqual(s.cam.stops, 1);
    });
  });

  await test("меню: «Узнать товар» помечено текущим, ссылки ведут на настоящие страницы; название приложения в заголовке и на вкладке", async () => {
    await withPage({}, async (s) => {
      const links = s.$("rail").byTag("a"); assert.deepStrictEqual(links.map((a) => a.getAttribute("href")), ["identify.html", "products.html", "settings.html", "admin.html"]);
      assert.strictEqual(links[0].getAttribute("aria-current"), "page");
      assert.strictEqual(s.$("rail").getAttribute("aria-label"), "Разделы"); assert.strictEqual(s.doc.title, "Узнать товар");
      assert.strictEqual(s.$("langHost").byClass("lang-btn").length, 4, "переключатель языка из четырёх кнопок");
    });
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
