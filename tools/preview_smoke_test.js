// Проверка витрины интерфейса (frontend/preview.html + preview.js) без браузера: разметка берётся настоящая и разбирается в подделку страницы,
// поэтому забытый id, не тот порядок скриптов или внешний адрес ломают проверку. Проверяется: исходы на экране, язык и тема с запоминанием,
// разделы, параметры адреса, тема, список товаров, отсутствие озвучки. Не проверяется внешний вид (его смотрят по снимкам Chromium).
// Запуск из корня проекта:  node tools/preview_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { text: tx, prefix: px } = require(path.join(__dirname, "test_texts.js"));
const { FakeDocument, parseHtml, makeStorage } = require("./fake_dom.js");
const FRONT = path.join(__dirname, "..", "frontend");
const I18N = require(path.join(FRONT, "i18n.js")), Theme = require(path.join(FRONT, "theme.js")), Icons = require(path.join(FRONT, "icons.js"));
const Ticket = require(path.join(FRONT, "ticket.js")), Catalog = require(path.join(FRONT, "catalog.js")), ProductList = require(path.join(FRONT, "productlist.js"));
const Preview = require(path.join(FRONT, "preview.js"));
const HTML = fs.readFileSync(path.join(FRONT, "preview.html"), "utf-8");

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise((r) => setTimeout(r, 0)); };

function boot(opts) {
  opts = opts || {};
  const doc = new FakeDocument(); parseHtml(doc, HTML);
  const storage = opts.storage || makeStorage();
  const i18n = I18N.create({ localStorage: storage, navigator: { languages: [opts.lang || "ru"] }, document: doc });
  for (const c of ["ru", "uz", "en"]) i18n.add(c, require(path.join(FRONT, `i18n.${c}.js`)));
  const theme = Theme.create({ localStorage: storage, document: doc, matchMedia: () => ({ matches: false }) });
  const touched = [];     // озвучки в программе больше нет: любое обращение страницы к Voice или speechSynthesis записывается сюда
  const handlers = [];
  const win = { document: doc, location: { search: opts.search || "", hash: opts.hash || "" }, I18N: i18n, Theme: theme, Icons, Ticket, Catalog, ProductList, addEventListener: (type, fn) => { if (type === "hashchange") handlers.push(fn); } };
  for (const name of ["Voice", "speechSynthesis"]) Object.defineProperty(win, name, { get() { touched.push(name); return undefined; }, enumerable: true });
  const app = Preview.start(win);
  const $ = (id) => doc.getElementById(id);
  return {
    doc, storage, i18n, theme, touched, win, app, $,
    hash: (h) => { win.location.hash = h; handlers.forEach((fn) => fn()); },
    visible: (id) => !$(id).hasAttribute("hidden"),
    ticketText: () => $("ticket").textContent,
    langButton: (code) => $("langHost").byClass("lang-btn").find((b) => b.getAttribute("data-lang") === code),
    stateButton: (s) => $("demoStates").children.find((b) => b.getAttribute("data-state") === s),
    rows: () => $("list").byClass("prow")
  };
}

(async () => {
  await test("разметка: все 15 id на месте, три раздела, значки и подписи отмечены; скрипты подключены по порядку и без внешних адресов", async () => {
    const doc = new FakeDocument(); parseHtml(doc, HTML);
    for (const id of ["screenTitle", "langHost", "themeBtn", "demo", "demoStates", "viewIdentify", "stage", "photoBtn", "flipBtn", "ticket", "viewProducts", "list", "viewSettings", "themeTitle", "themeHost"]) assert.ok(doc.getElementById(id), `нет #${id}`);
    assert.strictEqual(doc.querySelectorAll("[data-view]").length, 3);
    const scripts = [...HTML.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]);
    const at = (n) => scripts.indexOf(n);
    assert.ok(at("i18n.js") < at("i18n.ru.js") && at("i18n.js") < at("i18n.en.js") && at("i18n.js") < at("i18n.uz.js"), "словари раньше ядра языка");
    assert.ok(at("icons.js") < at("ticket.js") && at("ticket.js") < at("productlist.js") && at("catalog.js") < at("productlist.js") && at("productlist.js") < at("preview.js"), "порядок компонентов");
    assert.strictEqual(at("voice.js"), -1, "озвучки больше нет: модуль голоса не подключается");
    assert.ok(!/(?:src|href)="(?:https?:)?\/\//.test(HTML), "внешний адрес");
    assert.ok(HTML.includes('href="tokens.css"') && HTML.indexOf("tokens.css") < HTML.indexOf("ui.css"));
    assert.ok(/<meta name="viewport"[^>]*viewport-fit=cover/.test(HTML));
  });

  await test("первый показ: «Узнать товар», найденный товар, рамки видоискателя в состоянии «найден», кнопка «Сделать фото», 6 кнопок исходов", async () => {
    const t = boot();
    assert.ok(t.visible("viewIdentify") && !t.visible("viewProducts") && !t.visible("viewSettings"));
    assert.strictEqual(t.$("ticket").getAttribute("data-state"), "matched");
    assert.strictEqual(t.$("stage").getAttribute("data-state"), "matched");
    assert.ok(t.ticketText().includes("Ergo M200") && t.ticketText().includes("Мыши") && t.ticketText().includes("249\u00A0000"));
    assert.strictEqual(t.$("photoBtn").textContent, "Сделать фото");
    assert.strictEqual(t.$("demoStates").children.length, 6);
    assert.strictEqual(t.stateButton("matched").getAttribute("aria-pressed"), "true");
    assert.strictEqual(t.doc.documentElement.getAttribute("lang"), "ru");
    assert.strictEqual(t.$("screenTitle").textContent, "Узнать товар");
    assert.strictEqual(t.doc.querySelector("[data-view=\"identify\"]").getAttribute("aria-current"), "page");
  });

  await test("кнопки исходов переключают и ценник, и рамки видоискателя; нажатая отмечена", async () => {
    const t = boot();
    for (const s of Preview.STATES) {
      t.stateButton(s).click();
      assert.strictEqual(t.$("ticket").getAttribute("data-state"), s);
      assert.strictEqual(t.$("stage").getAttribute("data-state"), s);
      assert.strictEqual(t.stateButton(s).getAttribute("aria-pressed"), "true");
      assert.strictEqual(t.$("demoStates").children.filter((b) => b.getAttribute("aria-pressed") === "true").length, 1);
    }
    t.stateButton("uncertain").click();
    assert.strictEqual(t.$("ticket").byClass("cand").length, 3);
    t.$("ticket").byClass("cand")[2].click();
    assert.strictEqual(t.$("ticket").getAttribute("data-state"), "matched");
    assert.ok(t.ticketText().includes("Glide G110"));
  });

  await test("язык: кнопка меняет всё сразу (страница, ценник, список, подписи), запоминается, <html lang> обновляется", async () => {
    const t = boot();
    t.langButton("uz").click();
    assert.strictEqual(t.storage._m.get("app_lang"), "uz");
    assert.strictEqual(t.doc.documentElement.getAttribute("lang"), "uz");
    assert.strictEqual(t.$("screenTitle").textContent, tx("uz", "app.name"));
    assert.ok(t.ticketText().includes("Sichqonchalar") && t.ticketText().includes(tx("uz", "ticket.match")));
    assert.strictEqual(t.$("photoBtn").textContent, tx("uz", "identify.take_photo"));
    assert.strictEqual(t.stateButton("unknown").textContent, tx("uz", "demo.state.unknown"));
    assert.strictEqual(t.langButton("uz").getAttribute("aria-pressed"), "true");
    assert.ok(t.rows()[0].byClass("prow__meta")[0].textContent.includes("Sichqonchalar"));
    assert.strictEqual(t.doc.title, tx("uz", "app.name"));
    t.langButton("en").click(); assert.ok(t.ticketText().includes("Mice") && t.ticketText().includes("Found"));
    const again = boot({ storage: t.storage });
    assert.strictEqual(again.i18n.getLang(), "en");
    assert.ok(again.ticketText().includes("Found"));
  });

  await test("озвучки нет: на ценнике нет кнопки «Озвучить» ни на одном языке, в настройках нет выбора голоса, страница не трогает Voice и speechSynthesis", async () => {
    const t = boot();
    for (const code of ["ru", "uz", "en"]) {
      t.langButton(code).click();
      assert.ok(!t.$("ticket").byTag("button").find((b) => /Озвучить|Ovozda|Read aloud/.test(b.textContent)), `кнопка озвучки на языке ${code}`);
    }
    for (const id of ["voiceHint", "voiceSelect", "voiceTest", "voiceInfo"]) assert.ok(t.$(id) === null, `#${id} должен исчезнуть`);
    assert.deepStrictEqual(t.touched, [], "страница обращалась к озвучке: " + t.touched);
  });

  await test("тема: кнопка переключает «светлая ↔ тёмная», ставит data-theme, запоминает, подпись кнопки читаема", async () => {
    const t = boot();
    t.$("themeBtn").click(); assert.strictEqual(t.doc.documentElement.getAttribute("data-theme"), "dark");
    assert.strictEqual(t.storage._m.get("app_theme"), "dark");
    assert.strictEqual(t.$("themeBtn").getAttribute("aria-label"), "Тема: тёмная");
    t.$("themeBtn").click(); assert.strictEqual(t.doc.documentElement.getAttribute("data-theme"), "light");
  });

  await test("разделы: адрес #products / #settings переключает раздел, заголовок, меню и прячет панель образцов", async () => {
    const t = boot();
    t.hash("#products");
    assert.ok(t.visible("viewProducts") && !t.visible("viewIdentify")); assert.strictEqual(t.$("screenTitle").textContent, "Товары");
    assert.ok(!t.visible("demo")); assert.strictEqual(t.doc.querySelector("[data-view=\"products\"]").getAttribute("aria-current"), "page");
    assert.strictEqual(t.doc.querySelector("[data-view=\"identify\"]").getAttribute("aria-current"), null);
    t.hash("#settings"); assert.ok(t.visible("viewSettings") && !t.visible("viewProducts")); assert.strictEqual(t.$("screenTitle").textContent, "Настройки");
    t.hash("#что-угодно"); assert.ok(t.visible("viewIdentify") && t.visible("demo"));
  });

  await test("параметры адреса для проверки: ?lang=en&theme=dark&state=unknown&view=products&group=category", async () => {
    assert.deepStrictEqual(Preview.parseQuery("?a=1&b=%D0%B0&c"), { a: "1", b: "а", c: "" });
    const t = boot({ search: "?lang=en&theme=dark&state=unknown&view=products&group=category&sort=name" });
    assert.strictEqual(t.i18n.getLang(), "en");
    assert.strictEqual(t.doc.documentElement.getAttribute("data-theme"), "dark");
    assert.strictEqual(t.$("ticket").getAttribute("data-state"), "unknown");
    assert.ok(t.visible("viewProducts"));
    assert.deepStrictEqual(t.$("list").byClass("group__name").map((g) => g.textContent), ["Headphones", "Keyboards", "Mice", "Webcams"]);
    assert.strictEqual(boot({ search: "?state=ерунда&lang=xx" }).$("ticket").getAttribute("data-state"), "matched");
  });

  await test("список товаров в витрине: 22 образца, смена языка переводит категории и сбрасывает прежние фильтры", async () => {
    const t = boot({ hash: "#products" });
    assert.strictEqual(t.rows().length, Preview.buildItems("ru").length);
    t.$("list").byClass("check").find((r) => r.byClass("check__text")[0].textContent === "Мыши").byClass("check__box")[0].checked = true;
    t.$("list").byClass("check").find((r) => r.byClass("check__text")[0].textContent === "Мыши").byClass("check__box")[0].dispatch("change");
    assert.strictEqual(t.rows().length, 6);
    t.langButton("en").click();
    assert.strictEqual(t.rows().length, 22);
    assert.ok(t.$("list").byClass("check__text").some((c) => c.textContent === "Mice"));
  });

  await test("настройки: раздел показывает только тему; две кнопки темы, выбранная помечена, выбор применяется", async () => {
    const t = boot({ hash: "#settings" }); await flush();
    assert.ok(t.visible("viewSettings")); assert.strictEqual(t.$("themeHost").byClass("lang-btn").length, 2);
    t.$("themeHost").byClass("lang-btn")[1].click();
    assert.strictEqual(t.doc.documentElement.getAttribute("data-theme"), "dark");
    assert.strictEqual(t.$("themeHost").byClass("lang-btn")[1].getAttribute("aria-pressed"), "true");
  });

  await test("в preview.js нет innerHTML/outerHTML/insertAdjacentHTML/eval; данные образцов вымышленные (нет настоящих брендов)", async () => {
    const src = fs.readFileSync(path.join(FRONT, "preview.js"), "utf-8");
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) assert.ok(!src.includes(bad), bad);
    for (const brand of ["Logitech", "Razer", "Samsung", "Apple", "Sony", "Asus", "Dell", "HP", "Corsair"]) assert.ok(!new RegExp("\\b" + brand + "\\b").test(src), brand);
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
