// Список товаров покупателя (frontend/shop-list.html + shop-list.js) без браузера: настоящая разметка разбирается в подделку страницы,
// сервер — подделка fetch. Проверяется: адреса запросов, поиск и категории, «показать ещё», карточка с полями магазина (без цены, если
// магазин её не показывает), отсутствие подстановки разметки, тексты на всех языках, страница открыта на входе покупателя.
// Запуск из корня проекта:  node tools/shoplist_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { FakeDocument, parseHtml, makeStorage } = require("./fake_dom.js");
const FRONT = path.join(__dirname, "..", "frontend");
const I18N = require(path.join(FRONT, "i18n.js")), Icons = require(path.join(FRONT, "icons.js")), Ticket = require(path.join(FRONT, "ticket.js"));
const Shop = require(path.join(FRONT, "shop.js")), ShopList = require(path.join(FRONT, "shop-list.js"));
const HTML = fs.readFileSync(path.join(FRONT, "shop-list.html"), "utf-8");
const SRC = fs.readFileSync(path.join(FRONT, "shop-list.js"), "utf-8");
const GATE = fs.readFileSync(path.join(__dirname, "..", "app", "core", "customer_gate.py"), "utf-8");
const H = ShopList.helpers;

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; console.log("ok  ", name); } catch (err) { console.error("FAIL:", name); throw err; } }
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0)); };

const ITEMS = [
  { ref: "r1", name: "Кола 0,5 л", brand: "Coca-Cola", category: "Напитки", price: 9000, has_photo: true, details: { country: "Узбекистан", in_stock: true } },
  { ref: "r2", name: "Вода", brand: "", category: "Напитки", price: null, has_photo: false, details: {} },
];

function boot(opts) {
  opts = opts || {};
  const doc = new FakeDocument(); parseHtml(doc, HTML);
  const i18n = I18N.create({ localStorage: makeStorage(), navigator: { languages: ["ru"] }, document: doc });
  for (const c of ["ru", "uz", "en"]) i18n.add(c, require(path.join(FRONT, `i18n.${c}.js`)));
  const calls = [];
  const reply = opts.reply || ((url) => {
    if (url.startsWith("/api/v1/public/store")) return { name: "Магазин у дома", card_fields: ["price"] };
    if (url.startsWith("/api/v1/public/categories")) return { items: [{ name: "Напитки", count: 2 }, { name: "<b>x</b>", count: 1 }] };
    if (url.startsWith("/api/v1/public/products")) {
      const items = opts.items || ITEMS;
      const offset = Number((/offset=(\d+)/.exec(url) || [0, 0])[1]);
      const pageItems = opts.paged ? items.slice(offset, offset + 1) : items;
      const next = opts.paged && offset + 1 < items.length ? offset + 1 : null;
      return { total: items.length, items: pageItems, next_offset: next };
    }
    return null;
  });
  const timers = [];
  const win = {
    document: doc, I18N: i18n, Icons, Ticket, Shop,
    setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout: () => {},
    fetch: (url, options) => {
      calls.push({ url, options });
      if (opts.offline) return Promise.reject(new Error("offline"));
      const body = reply(url);
      const status = opts.status && url.startsWith("/api/v1/public/products") ? opts.status : 200;
      return Promise.resolve({ ok: status === 200, status, json: () => Promise.resolve(body) });
    },
  };
  const app = ShopList.start(win);
  const $ = (id) => doc.getElementById(id);
  return { doc, win, app, $, calls, runTimers: () => { timers.splice(0).forEach((fn) => fn()); } };
}

(async () => {
  await test("адреса: пустые параметры не уходят, поиск обрезан до 100 знаков и закодирован; фото — по ref", () => {
    assert.strictEqual(H.listUrl("", "", 0), "/api/v1/public/products?limit=30");
    assert.strictEqual(H.listUrl(" кола ", "Напитки", 30), "/api/v1/public/products?q=%D0%BA%D0%BE%D0%BB%D0%B0&category=%D0%9D%D0%B0%D0%BF%D0%B8%D1%82%D0%BA%D0%B8&offset=30&limit=30");
    assert.ok(H.listUrl("x".repeat(300), "", 0).length < 140);
    assert.strictEqual(H.photoUrl("a/b"), "/api/v1/public/photo/a%2Fb");
    assert.deepStrictEqual(H.parseList(null), { items: [], total: 0, next: null });
    assert.deepStrictEqual(H.parseList({ items: [{ name: "A" }, null, { x: 1 }], total: 5, next_offset: 1 }), { items: [{ name: "A" }], total: 5, next: 1 });
  });

  await test("открытие: магазин, категории (текст, а не разметка), товары с фото и ценой; запросы без cookies", async () => {
    const p = boot(); await flush();
    assert.strictEqual(p.$("storeName").textContent, "Магазин у дома");
    const chips = p.$("catHost").byClass("chip--filter");
    assert.deepStrictEqual(chips.map((c) => c.textContent), ["Все", "Напитки · 2", "<b>x</b> · 1"]);
    assert.strictEqual(chips[0].getAttribute("aria-pressed"), "true");
    const tiles = p.$("listHost").byClass("shoplist__item");
    assert.strictEqual(tiles.length, 2);
    assert.ok(tiles[0].textContent.includes("Кола 0,5 л") && tiles[0].textContent.includes("9 000"));
    assert.strictEqual(tiles[0].querySelector("img").getAttribute("src"), "/api/v1/public/photo/r1");
    assert.strictEqual(tiles[1].querySelector("img"), null, "нет фото — нет картинки");
    assert.ok(!tiles[1].textContent.includes("Цена не указана") && !tiles[1].textContent.includes("сум"), "нет цены — на плитке о цене ничего");
    assert.ok(p.$("countNote").textContent.includes("2"));
    assert.strictEqual(p.$("moreBtn").hidden, true);
    assert.ok(p.calls.every((c) => c.options && c.options.credentials === "omit"));
  });

  await test("магазин не показывает цену (нет ключа price) — ни в списке, ни в карточке нет ни цены, ни «цена не указана»", async () => {
    const p = boot({ items: [{ ref: "r9", name: "Хлеб", brand: "", category: "", has_photo: false, details: { manufacturer: "Нон" } }] }); await flush();
    const tile = p.$("listHost").byClass("shoplist__item")[0];
    assert.ok(!tile.textContent.includes("Цена") && !tile.textContent.includes("сум"));
    tile.click();
    const card = p.$("ticketHost").textContent;
    assert.ok(card.includes("Хлеб") && card.includes("Производитель") && card.includes("Нон"));
    assert.ok(!card.includes("Цена не указана") && !card.includes("сум"));
  });

  await test("нажатие — та же карточка: поля магазина с подписями, без «Узнали» и «Это не тот товар»; закрытие возвращает фокус", async () => {
    const p = boot(); await flush();
    const tile = p.$("listHost").byClass("shoplist__item")[0];
    tile.click();
    assert.strictEqual(p.$("sheet").hidden, false);
    assert.strictEqual(p.doc.body.getAttribute("data-sheet"), "open");
    assert.strictEqual(p.$("sheetImg").getAttribute("src"), "/api/v1/public/photo/r1");
    const card = p.$("ticketHost").textContent;
    for (const want of ["Кола 0,5 л", "Coca-Cola", "Страна", "Узбекистан", "Наличие", "Есть в наличии", "9 000"]) assert.ok(card.includes(want), want);
    assert.ok(!card.includes("Узнали") && !card.includes("не тот товар"));
    p.$("sheetClose").click();
    assert.strictEqual(p.$("sheet").hidden, true);
    assert.strictEqual(p.doc.body.getAttribute("data-sheet"), null);
    assert.strictEqual(p.doc.activeElement, tile);
  });

  await test("под фото из открытой базы — чьё оно (CC BY-SA); у своего фото и без фото подписи нет", async () => {
    const items = [{ ...ITEMS[0], photo_credit: "Open Food Facts, CC BY-SA" }, { ...ITEMS[1] }, { ...ITEMS[0], ref: "r3", name: "Своё" }];
    const p = boot({ items }); await flush();
    const tiles = p.$("listHost").byClass("shoplist__item");
    tiles[0].click();
    assert.strictEqual(p.$("sheetCredit").hidden, false);
    assert.strictEqual(p.$("sheetCredit").textContent, "Фото: Open Food Facts, CC BY-SA");
    p.$("sheetClose").click(); tiles[1].click();
    assert.strictEqual(p.$("sheetCredit").hidden, true);
    p.$("sheetClose").click(); tiles[2].click();
    assert.strictEqual(p.$("sheetCredit").hidden, true);
  });

  await test("поиск (с задержкой) и категория уходят на сервер; «показать ещё» добавляет следующую часть", async () => {
    const p = boot({ paged: true }); await flush();
    assert.strictEqual(p.$("listHost").byClass("shoplist__item").length, 1);
    assert.strictEqual(p.$("moreBtn").hidden, false);
    p.$("moreBtn").click(); await flush();
    assert.strictEqual(p.$("listHost").byClass("shoplist__item").length, 2);
    assert.ok(p.calls.some((c) => c.url.includes("offset=1")));
    assert.strictEqual(p.$("moreBtn").hidden, true);
    p.$("searchInput").value = "кола"; p.$("searchInput").dispatch("input"); p.runTimers(); await flush();
    assert.ok(p.calls.some((c) => c.url.includes("q=%D0%BA%D0%BE%D0%BB%D0%B0")));
    p.$("catHost").byClass("chip--filter")[1].click(); await flush();
    const last = p.calls.filter((c) => c.url.startsWith("/api/v1/public/products")).pop().url;
    assert.ok(last.includes("category=%D0%9D%D0%B0%D0%BF%D0%B8%D1%82%D0%BA%D0%B8") && last.includes("q="), last);
    assert.strictEqual(p.$("catHost").byClass("chip--filter")[1].getAttribute("aria-pressed"), "true");
  });

  await test("ошибки понятными словами: частота, нет связи, сервер; пусто — «ничего не нашли»", async () => {
    let p = boot({ status: 429 }); await flush();
    assert.strictEqual(p.$("listError").hidden, false); assert.ok(p.$("listError").textContent.includes("Слишком много"));
    p = boot({ offline: true }); await flush();
    assert.ok(p.$("listError").textContent.includes("Нет связи"));
    p = boot({ status: 503 }); await flush();
    assert.ok(p.$("listError").textContent.includes("не получается показать"));
    p = boot({ items: [] }); await flush();
    assert.ok(p.$("countNote").textContent.includes("нет товаров"));
  });

  await test("без innerHTML/eval/хранилища и внешних адресов; страница и скрипт открыты на входе покупателя; тексты на всех языках", () => {
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "localStorage", "sessionStorage"]) {
      assert.ok(!SRC.includes(bad), `shop-list.js: найдено ${bad}`);
    }
    assert.ok(!/https?:\/\//.test(SRC.replace(/\/\*[\s\S]*?\*\//g, "")), "shop-list.js: внешний адрес");
    assert.ok(!/<script>(?!<\/script>)/.test(HTML) && !HTML.includes("auth.js") && !HTML.includes("password"));
    assert.ok(GATE.includes('"shop-list.html"') && GATE.includes('"shop-list.js"'));
    const keys = new Set();
    for (const m of HTML.matchAll(/data-i18n="([^"]+)"/g)) keys.add(m[1]);
    for (const m of HTML.matchAll(/data-i18n-attr="([^"]+)"/g)) m[1].split(";").forEach((pair) => keys.add(pair.split(":")[1]));
    for (const m of SRC.matchAll(/"((?:shoplist|shop|card|ticket)\.[a-z_]+)"/g)) keys.add(m[1]);
    for (const lang of ["ru", "uz", "uz-cyrl", "en"]) {
      const dict = fs.readFileSync(path.join(FRONT, `i18n.${lang}.js`), "utf8");
      for (const k of keys) assert.ok(dict.includes(`"${k}"`), `${lang}: нет ${k}`);
    }
  });

  console.log(`\n${passed} passed`);
})().catch((e) => { console.error(e); process.exit(1); });
