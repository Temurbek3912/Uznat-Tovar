// Проверка экрана «Товары» (frontend/productlist.js) без браузера: поиск, сортировка, группы, фильтры со счётчиками, чипы выбранного, пустой результат,
// сворачивание групп, панель фильтров на телефоне, безопасность названий. Данные выдуманные. Запуск из корня проекта:  node tools/productlist_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { text: tx, prefix: px } = require(path.join(__dirname, "test_texts.js"));
const { FakeDocument } = require("./fake_dom.js");
const FRONT = path.join(__dirname, "..", "frontend");
const I18N = require(path.join(FRONT, "i18n.js"));
const Icons = require(path.join(FRONT, "icons.js"));
const ProductList = require(path.join(FRONT, "productlist.js"));

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }

const P = (sku, name, brand, category, price, photos, codes) => ({ id: sku, sku, name, brand, category, expected_price: price, photo_count: photos === undefined ? 1 : photos, barcodes: codes || [] });
const ITEMS = [
  P("A1", "Ergo M200", "Nordic", "Мыши", 249000, 2, ["4006381333931"]), P("A2", "Ergo M200 Pro", "Nordic", "Мыши", 399000),
  P("A3", "Compact K10", "Delta", "Клавиатуры", 150000, 0), P("A4", "Compact K75", "Delta", "Клавиатуры", null),
  P("A5", "Ergo H3", "Nordic", "Наушники", 89000), P("A6", "Pixel W2", "Orbit", "", 120000)
];

function setup(lang, items, extra) {
  const i18n = I18N.create({ navigator: { languages: [lang || "ru"] } });
  for (const c of ["ru", "uz", "en"]) i18n.add(c, require(path.join(FRONT, `i18n.${c}.js`)));
  const doc = new FakeDocument(), host = doc.createElement("div");
  doc.body.appendChild(host);
  const opened = [];
  const list = ProductList.create(doc, host, Object.assign({ t: (k, p) => i18n.t(k, p), icons: Icons, getLang: () => i18n.getLang(), onOpen: (p) => opened.push(p.sku) }, extra || {}));
  list.setItems(items || ITEMS);
  const api = {
    i18n, doc, host, list, opened,
    rows: () => host.byClass("prow").map((r) => r.byClass("prow__name")[0].textContent),
    found: () => host.byClass("plist__found")[0].textContent,
    groups: () => host.byClass("group__name").map((g) => g.textContent),
    byId: (id) => host.querySelector("#" + id),
    search: (text) => { const el = api.byId("listSearch"); el.value = text; el.dispatch("input"); },
    pick: (id, value) => { const el = api.byId(id); el.value = value; el.dispatch("change"); },
    check: (label, on) => { const row = host.byClass("check").find((r) => r.byClass("check__text")[0].textContent === label); const box = row.byClass("check__box")[0]; box.checked = on === undefined ? true : on; box.dispatch("change"); },
    counts: () => Object.fromEntries(host.byClass("check").filter((r) => r.byClass("check__n").length).map((r) => [r.byClass("check__text")[0].textContent, r.byClass("check__n")[0].textContent]))
  };
  return api;
}

(async () => {
  await test("первый показ: все товары, «Найдено: 6 товаров», без групп, строка — название, бренд · категория, цена или тире", async () => {
    const s = setup();
    assert.strictEqual(s.rows().length, 6);
    assert.strictEqual(s.found(), "Найдено: 6 товаров");
    assert.strictEqual(s.groups().length, 0);
    const first = s.host.byClass("prow")[0];
    assert.strictEqual(first.byClass("prow__meta")[0].textContent, "Nordic · Мыши");
    assert.strictEqual(first.byClass("prow__price")[0].textContent, "249\u00A0000\u00A0сум");
    assert.strictEqual(s.host.byClass("prow")[3].byClass("prow__price")[0].textContent, "\u2014");
    assert.strictEqual(s.host.byClass("prow__thumb")[0].textContent, "E");
  });

  await test("поиск набором слов фильтрует сразу и обновляет «Найдено»; строка поиска не пересоздаётся (не теряет фокус)", async () => {
    const s = setup();
    const box = s.byId("listSearch");
    s.search("ergo m200"); assert.deepStrictEqual(s.rows(), ["Ergo M200", "Ergo M200 Pro"]); assert.strictEqual(s.found(), "Найдено: 2 товара");
    s.search("ergo pro"); assert.deepStrictEqual(s.rows(), ["Ergo M200 Pro"]); assert.strictEqual(s.found(), "Найдено: 1 товар");
    assert.strictEqual(s.byId("listSearch"), box);
  });

  await test("сортировка: по названию, по цене в обе стороны (без цены — в конце), «новые» — порядок сервера", async () => {
    const s = setup();
    s.pick("listSort", "price_asc"); assert.deepStrictEqual(s.rows().slice(0, 3), ["Ergo H3", "Pixel W2", "Compact K10"]); assert.strictEqual(s.rows()[5], "Compact K75");
    s.pick("listSort", "price_desc"); assert.strictEqual(s.rows()[0], "Ergo M200 Pro");
    s.pick("listSort", "name"); assert.strictEqual(s.rows()[0], "Compact K10");
    s.pick("listSort", "new"); assert.strictEqual(s.rows()[0], "Ergo M200");
  });

  await test("группы по категории: заголовки с числом товаров, по алфавиту, «Без категории» последней; по бренду — свои группы", async () => {
    const s = setup();
    s.pick("listGroup", "category");
    assert.deepStrictEqual(s.groups(), ["Клавиатуры", "Мыши", "Наушники", "Без категории"]);
    assert.deepStrictEqual(s.host.byClass("group__count").map((c) => c.textContent), ["2", "2", "1", "1"]);
    s.pick("listGroup", "brand"); assert.deepStrictEqual(s.groups(), ["Delta", "Nordic", "Orbit"]);
    s.pick("listGroup", "none"); assert.strictEqual(s.groups().length, 0);
  });

  await test("группа сворачивается и разворачивается; состояние видно в aria-expanded; строки скрыты, пока свёрнута", async () => {
    const s = setup(); s.pick("listGroup", "category");
    const head = () => s.host.byClass("group__head")[1];
    assert.strictEqual(head().getAttribute("aria-expanded"), "true");
    head().click();
    assert.strictEqual(head().getAttribute("aria-expanded"), "false");
    assert.ok(!s.rows().includes("Ergo M200") && s.rows().includes("Ergo H3"));
    head().click();
    assert.ok(s.rows().includes("Ergo M200"));
  });

  await test("фильтр по категории: чекбокс отбирает товары, чип выбранного появляется и снимается, значок «Фильтры» показывает число", async () => {
    const s = setup();
    s.check("Мыши");
    assert.deepStrictEqual(s.rows(), ["Ergo M200", "Ergo M200 Pro"]);
    assert.strictEqual(s.host.byClass("chip--filter").length, 1);
    assert.strictEqual(s.host.byClass("plist__badge")[0].textContent, "1");
    s.host.byClass("chip--filter")[0].click();
    assert.strictEqual(s.rows().length, 6);
    assert.strictEqual(s.host.byClass("chip--filter").length, 0);
    assert.strictEqual(s.host.byClass("plist__badge").length, 0);
  });

  await test("счётчики у вариантов пересчитываются: после выбора бренда видно, сколько каких категорий останется", async () => {
    const s = setup();
    assert.deepStrictEqual(s.counts(), { "Клавиатуры": "2", "Мыши": "2", "Наушники": "1", "Без категории": "1", Nordic: "3", Delta: "2", Orbit: "1" });
    s.check("Nordic");
    assert.deepStrictEqual(s.counts(), { "Мыши": "2", "Наушники": "1", Nordic: "3", Delta: "2", Orbit: "1" });
  });

  await test("варианты без товаров скрыты, но уже выбранный не пропадает, даже если из-за поиска стал пустым (счётчик 0)", async () => {
    const s = setup();
    s.check("Мыши");
    assert.ok(!("Delta" in s.counts()), "у Delta нет мышей — варианта быть не должно");
    s.search("compact");
    assert.strictEqual(s.rows().length, 0);
    assert.strictEqual(s.counts()["Мыши"], "0");
    assert.strictEqual(s.counts()["Клавиатуры"], "2");
  });

  await test("цена: поля «от»/«до» применяются по change, чип «Цена: …», подсказка диапазона под полями", async () => {
    const s = setup();
    assert.ok(s.host.byClass("filters__range")[0].textContent.startsWith("89\u00A0000 \u2013 399\u00A0000"));
    s.pick("priceMin", "120000"); s.pick("priceMax", "250000");
    assert.deepStrictEqual(s.rows(), ["Ergo M200", "Compact K10", "Pixel W2"]);
    assert.ok(s.host.byClass("chip--filter")[0].textContent.includes("120\u00A0000"));
    s.pick("priceMin", ""); assert.ok(s.rows().includes("Ergo H3"));
  });

  await test("«Только»: с ценой / с фото / со штрих-кодом; несколько фильтров вместе; «Сбросить» очищает всё, кроме строки поиска", async () => {
    const s = setup();
    s.check("С ценой"); assert.strictEqual(s.rows().length, 5);
    s.check("С фото"); assert.strictEqual(s.rows().length, 4);
    s.check("Со штрих-кодом"); assert.deepStrictEqual(s.rows(), ["Ergo M200"]);
    assert.strictEqual(s.host.byClass("plist__badge")[0].textContent, "3");
    s.search("ergo");
    s.host.byClass("link").find((l) => l.textContent === "Сбросить").click();
    assert.strictEqual(s.host.byClass("plist__badge").length, 0);
    assert.deepStrictEqual(s.rows(), ["Ergo M200", "Ergo M200 Pro", "Ergo H3"]);
  });

  await test("«Не заполнено»: без цены / без фото / без штрих-кода; противоположное «С …» выключается; чип снимает фильтр; «Сбросить» очищает", async () => {
    const s = setup();
    s.check("Без цены"); assert.deepStrictEqual(s.rows(), ["Compact K75"]);
    assert.ok(s.host.byClass("chip--filter").some((c) => c.textContent === "Без цены"));
    s.check("С ценой");                                    // включили противоположное — «Без цены» выключилось
    assert.strictEqual(s.rows().length, 5);
    assert.strictEqual(s.host.byClass("plist__badge")[0].textContent, "1");
    s.check("Без цены");                                   // и наоборот
    assert.deepStrictEqual(s.rows(), ["Compact K75"]);
    assert.strictEqual(s.host.byClass("plist__badge")[0].textContent, "1");
    s.host.byClass("chip--filter").find((c) => c.textContent === "Без цены").click();
    assert.strictEqual(s.rows().length, 6);
    s.check("Без фото"); assert.deepStrictEqual(s.rows(), ["Compact K10"]);
    s.check("Без штрих-кода"); assert.deepStrictEqual(s.rows(), ["Compact K10"]);
    s.check("Без фото", false); assert.strictEqual(s.rows().length, 5);
    s.host.byClass("link").find((l) => l.textContent === "Сбросить").click();
    assert.strictEqual(s.rows().length, 6);
    const u = setup("uz");
    u.check("Surati yoʻq"); assert.deepStrictEqual(u.rows(), ["Compact K10"]);
    assert.ok(u.host.byClass("filters__legend").some((l) => l.textContent === "Toʻldirilmagan"));
  });

  await test("ничего не найдено: понятное сообщение и кнопка «Сбросить», которая очищает и поиск", async () => {
    const s = setup(); s.search("такого нет");
    assert.strictEqual(s.rows().length, 0);
    assert.ok(s.host.textContent.includes("Ничего не найдено") && s.host.textContent.includes("Измените запрос"));
    s.host.byClass("plist__empty")[0].byTag("button")[0].click();
    assert.strictEqual(s.rows().length, 6);
  });

  await test("панель фильтров на телефоне: «Фильтры» открывает, «Показать: N» и затемнение закрывают; aria-expanded меняется", async () => {
    const s = setup();
    const panel = s.byId("listFilters"), toggle = s.host.byClass("plist__toggle")[0];
    assert.ok(!panel.className.includes("is-open"));
    toggle.click(); assert.ok(panel.className.includes("is-open")); assert.strictEqual(toggle.getAttribute("aria-expanded"), "true");
    assert.ok(s.host.byClass("plist__backdrop")[0].className.includes("is-open"));
    s.check("Мыши"); assert.ok(panel.className.includes("is-open"));
    const show = s.host.byClass("filters__show")[0]; assert.strictEqual(show.textContent, "Показать: 2");
    show.click(); assert.ok(!panel.className.includes("is-open")); assert.strictEqual(toggle.getAttribute("aria-expanded"), "false");
    toggle.click(); s.host.byClass("plist__backdrop")[0].click(); assert.ok(!panel.className.includes("is-open"));
  });

  await test("фокус клавиатуры не теряется: после смены фильтра фокус возвращается на тот же переключатель, поле цены и заголовок группы", async () => {
    const s = setup();
    const boxOf = (label) => s.host.byClass("check").find((r) => r.byClass("check__text")[0].textContent === label).byClass("check__box")[0];
    const before = boxOf("Мыши"); before.focus(); s.check("Мыши");
    assert.notStrictEqual(boxOf("Мыши"), before, "панель должна была перерисоваться");
    assert.strictEqual(s.doc.activeElement, boxOf("Мыши"));
    s.byId("priceMin").focus(); s.pick("priceMin", "100000");
    assert.strictEqual(s.doc.activeElement, s.byId("priceMin"));
    s.pick("listGroup", "category");
    const head = s.host.byClass("group__head")[0]; head.focus(); head.click();
    assert.strictEqual(s.doc.activeElement.getAttribute("data-key"), head.getAttribute("data-key"));
  });

  await test("смена фильтра не пересоздаёт строку поиска, сортировку и кнопку «Фильтры» (они не теряют состояние и фокус)", async () => {
    const s = setup();
    const search = s.byId("listSearch"), sort = s.byId("listSort"), toggle = s.host.byClass("plist__toggle")[0];
    s.check("Мыши"); s.pick("priceMin", "1000");
    assert.strictEqual(s.byId("listSearch"), search); assert.strictEqual(s.byId("listSort"), sort);
    assert.strictEqual(s.host.byClass("plist__toggle")[0], toggle);
  });

  await test("тексты на трёх языках; смена языка и refresh перерисовывают подписи; открытие товара и фото", async () => {
    const s = setup("uz");
    assert.ok(s.found().startsWith(tx("uz", "list.found", { count: tx("uz", "items.count", { n: 6 }) })));
    assert.strictEqual(s.byId("listSearch").getAttribute("placeholder"), tx("uz", "list.search"));
    s.i18n.setLang("en"); s.list.refresh();
    assert.strictEqual(s.found(), "Found: 6 products");
    s.host.byClass("prow")[2].click(); assert.deepStrictEqual(s.opened, ["A3"]);
    const doc = new FakeDocument(), host = doc.createElement("div");
    ProductList.create(doc, host, { t: (k) => k, photoUrl: (p) => `/photo/${p.sku}` }).setItems(ITEMS.slice(0, 1));
    assert.strictEqual(host.byTag("img")[0].getAttribute("src"), "/photo/A1");
  });

  await test("безопасность: название, бренд и категория из базы — только текст, разметки нет; пустой список не падает", async () => {
    const evil = '<img src=x onerror="alert(1)">';
    const s = setup("ru", [P("X", evil, evil, evil, 1000)]);
    assert.strictEqual(s.host.byTag("img").length, 0);
    assert.strictEqual(s.rows()[0], evil);
    const empty = setup("ru", []);
    assert.strictEqual(empty.rows().length, 0);
    assert.ok(empty.host.textContent.includes("Ничего не найдено"));
    setup("ru", null);
  });

  await test("миниатюры, которые отдаются только с авторизацией (loadThumb): зовётся лишь для товаров с фото; пока ответа нет — буква; пришёл адрес — картинка; отказ и null — буква остаётся", async () => {
    const asked = [], answers = { A1: "blob:a1", A2: null, A5: "reject" };
    const loadThumb = (p) => { asked.push(p.sku); return answers[p.sku] === "reject" ? Promise.reject(new Error("нет")) : Promise.resolve(answers[p.sku] === undefined ? "blob:" + p.sku : answers[p.sku]); };
    const s = setup("ru", ITEMS, { loadThumb });
    const withPhotos = ITEMS.filter((p) => p.photo_count > 0).map((p) => p.sku).sort();
    assert.deepStrictEqual(asked.slice().sort(), withPhotos); assert.ok(!asked.includes("A3"), "у A3 фото нет — не спрашиваем");
    const thumb = (sku) => s.host.byClass("prow").find((r) => r.byClass("prow__name")[0].textContent === ITEMS.find((p) => p.sku === sku).name).byClass("prow__thumb")[0];
    assert.strictEqual(thumb("A1").textContent, "E", "пока ответа нет — первая буква");
    await new Promise((r) => setTimeout(r, 5));
    assert.strictEqual(thumb("A1").byTag("img")[0].getAttribute("src"), "blob:a1");
    assert.strictEqual(thumb("A2").byTag("img").length, 0, "null — картинки нет"); assert.strictEqual(thumb("A5").byTag("img").length, 0, "отказ — картинки нет");
    assert.strictEqual(thumb("A5").textContent, "E"); assert.strictEqual(thumb("A3").textContent, "C");
    const plain = setup("ru", ITEMS); assert.strictEqual(plain.host.byTag("img").length, 0, "без loadThumb ничего не загружается");
  });

  await test("с IntersectionObserver миниатюра грузится только когда строка подъехала к экрану, один раз; после перерисовки слежение начинается заново", async () => {
    let observers = [];
    class FakeIO {
      constructor(cb, opts) { this.cb = cb; this.opts = opts; this.watched = []; observers.push(this); }
      observe(el) { this.watched.push(el); } unobserve(el) { this.watched = this.watched.filter((x) => x !== el); } disconnect() { this.watched = []; }
      show(el) { this.cb([{ isIntersecting: true, target: el }]); }
    }
    globalThis.IntersectionObserver = FakeIO;
    try {
      const asked = [], s = setup("ru", ITEMS, { loadThumb: (p) => { asked.push(p.sku); return Promise.resolve("blob:" + p.sku); } });
      assert.strictEqual(asked.length, 0, "пока ни одна строка не на экране — ничего не грузим");
      const io = observers[0]; assert.ok(io.opts.rootMargin, "подгружаем чуть заранее");
      const withPhotos = ITEMS.filter((p) => p.photo_count > 0).length; assert.strictEqual(io.watched.length, withPhotos);
      io.show(io.watched[0]); assert.strictEqual(asked.length, 1); assert.strictEqual(io.watched.length, withPhotos - 1, "показанная снята с наблюдения");
      s.search("Compact"); assert.strictEqual(io.watched.length, 1, "после перерисовки следим только за новыми строками (у K10 фото нет, K75 — есть)");
      assert.strictEqual(observers.length, 1, "один наблюдатель на весь список");
    } finally { delete globalThis.IntersectionObserver; }
  });

  await test("в productlist.js нет innerHTML/outerHTML/insertAdjacentHTML/eval", async () => {
    const src = fs.readFileSync(path.join(FRONT, "productlist.js"), "utf-8");
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) assert.ok(!src.includes(bad), bad);
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
