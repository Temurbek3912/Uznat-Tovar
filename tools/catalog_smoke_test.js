// Проверка поиска, фильтров, групп, сортировки и счётчиков списка товаров (frontend/catalog.js) без браузера.
// Данные выдуманные. Проверяется логика, а не вид списка. Запуск из корня проекта:  node tools/catalog_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const C = require(path.join(__dirname, "..", "frontend", "catalog.js"));

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }

const P = (sku, name, brand, category, price, photos, barcodes) => ({ sku, name, brand, category, expected_price: price, photo_count: photos === undefined ? 1 : photos, barcodes: barcodes || [] });
const ITEMS = [
  P("A1", "Ergo M200", "Nordic", "Мыши", 249000, 2, ["4006381333931"]),
  P("A2", "Ergo M200 Pro", "Nordic", "Мыши", 399000),
  P("A3", "Ёлка К-10", "Delta", "Клавиатуры", 150000, 0),
  P("A4", "Compact K75", "Delta", "Клавиатуры", null),
  P("A5", "Oʻrta H3", "Nordic", "Наушники", 89000),
  P("A6", "Pixel W2", "Orbit", "", 120000),
  P("A7", "Ergo M1000", "Orbit", "Мыши", 1200000)
];
const names = (list) => list.map((p) => p.sku).join(",");

(async () => {
  await test("нормализация: регистр, ё/е, виды апострофа и лишние пробелы не мешают поиску", async () => {
    assert.strictEqual(C.norm("  ЁЛКА   К-10 "), "елка к-10");
    assert.strictEqual(C.norm("Oʻzbek"), C.norm("o'zbek"));
    assert.strictEqual(C.norm("g\u02BCisht"), C.norm("G’ISHT"));
    assert.strictEqual(C.norm(null), "");
  });

  await test("поиск: все слова запроса должны найтись; ищет по названию, бренду, категории, артикулу и штрих-коду", async () => {
    const q = (s) => names(C.filterProducts(ITEMS, { q: s }));
    assert.strictEqual(q("ergo m200"), "A1,A2");
    assert.strictEqual(q("ergo pro"), "A2");
    assert.strictEqual(q("nordic мыши"), "A1,A2");
    assert.strictEqual(q("a5"), "A5");
    assert.strictEqual(q("4006381333931"), "A1");
    assert.strictEqual(q("елка"), "A3");
    assert.strictEqual(q("ёлка"), "A3");
    assert.strictEqual(q("orta"), "A5");
    assert.strictEqual(q("ergo ничего"), "");
    assert.strictEqual(q("   "), "A1,A2,A3,A4,A5,A6,A7");
  });

  await test("фильтры: категории и бренды (внутри измерения «или», между ними «и»)", async () => {
    assert.strictEqual(names(C.filterProducts(ITEMS, { categories: ["Мыши"] })), "A1,A2,A7");
    assert.strictEqual(names(C.filterProducts(ITEMS, { categories: ["Мыши", "Наушники"] })), "A1,A2,A5,A7");
    assert.strictEqual(names(C.filterProducts(ITEMS, { categories: ["Мыши"], brands: ["Nordic"] })), "A1,A2");
    assert.strictEqual(names(C.filterProducts(ITEMS, { categories: [""] })), "A6");
  });

  await test("цена: границы включительно; товары без цены не проходят, если граница задана; галочки «есть цена / фото / штрих-код»", async () => {
    assert.strictEqual(names(C.filterProducts(ITEMS, { priceMin: 120000, priceMax: 249000 })), "A1,A3,A6");
    assert.strictEqual(names(C.filterProducts(ITEMS, { priceMax: 100000 })), "A5");
    assert.strictEqual(names(C.filterProducts(ITEMS, { priceMin: "400000" })), "A7");
    assert.strictEqual(names(C.filterProducts(ITEMS, { onlyWithPrice: true })), "A1,A2,A3,A5,A6,A7");
    assert.strictEqual(names(C.filterProducts(ITEMS, { onlyWithPhoto: true })), "A1,A2,A4,A5,A6,A7");
    assert.strictEqual(names(C.filterProducts(ITEMS, { onlyWithBarcode: true })), "A1");
  });

  await test("счётчики вариантов: свой фильтр не учитывается, чужие — да; по убыванию, при равенстве по алфавиту", async () => {
    const f = C.facets(ITEMS, { brands: ["Nordic"] });
    assert.deepStrictEqual(f.categories.map((x) => `${x.value}:${x.count}`), ["Мыши:2", "Наушники:1"]);
    assert.deepStrictEqual(f.brands.map((x) => `${x.value}:${x.count}`), ["Nordic:3", "Delta:2", "Orbit:2"]);
    assert.strictEqual(f.total, 3);
    assert.deepStrictEqual(f.price, { min: 89000, max: 399000 });          // диапазон цен — для уже выбранного бренда
  });

  await test("счётчики при поиске: считаются по найденному; пустая категория видна как значение «»", async () => {
    const f = C.facets(ITEMS, { q: "orbit" });
    assert.deepStrictEqual(f.categories.map((x) => `${x.value}:${x.count}`), [":1", "Мыши:1"]);     // поровну — по алфавиту, пустое значение первым
    assert.strictEqual(f.total, 2);
    assert.strictEqual(C.facets([], {}).price, null);
  });

  await test("сортировка: по названию (числа по значению: M200 раньше M1000), по цене в обе стороны, «новые» = порядок сервера; без цены всегда в конце", async () => {
    const byName = C.sortProducts(ITEMS, "name", "en").map((p) => p.name);
    const at = (n) => byName.indexOf(n);
    assert.ok(at("Compact K75") < at("Ergo M200") && at("Ergo M200") < at("Ergo M200 Pro") && at("Ergo M200 Pro") < at("Ergo M1000"), byName.join(" | "));
    assert.strictEqual(byName.length, ITEMS.length);
    assert.strictEqual(names(C.sortProducts(ITEMS, "price_asc")), "A5,A6,A3,A1,A2,A7,A4");
    assert.strictEqual(names(C.sortProducts(ITEMS, "price_desc")), "A7,A2,A1,A3,A6,A5,A4");
    assert.strictEqual(names(C.sortProducts(ITEMS, "new")), "A1,A2,A3,A4,A5,A6,A7");
    assert.strictEqual(names(ITEMS), "A1,A2,A3,A4,A5,A6,A7");
  });

  await test("группы по категории и бренду: по алфавиту, «без значения» последней, порядок внутри группы сохраняется; «без групп» — одна группа", async () => {
    const g = C.groupProducts(ITEMS, "category", "ru");
    assert.deepStrictEqual(g.map((x) => `${x.key || "∅"}:${names(x.items)}`), ["Клавиатуры:A3,A4", "Мыши:A1,A2,A7", "Наушники:A5", "∅:A6"]);
    assert.deepStrictEqual(C.groupProducts(ITEMS, "brand", "ru").map((x) => x.key), ["Delta", "Nordic", "Orbit"]);
    const none = C.groupProducts(ITEMS, "none");
    assert.strictEqual(none.length, 1);
    assert.strictEqual(none[0].items.length, ITEMS.length);
    assert.deepStrictEqual(C.groupProducts([], "category"), []);
  });

  await test("значок «Фильтры (N)»: считает включённые фильтры, поиск не считает", async () => {
    assert.strictEqual(C.activeCount(C.emptyFilters()), 0);
    assert.strictEqual(C.activeCount({ q: "abc" }), 0);
    assert.strictEqual(C.activeCount({ categories: ["Мыши"], brands: ["Nordic"], priceMin: 1, onlyWithPhoto: true }), 4);
    assert.strictEqual(C.activeCount({ priceMin: 1, priceMax: 5 }), 1);
  });

  await test("устойчивость: нет полей, null, мусорные числа — без ошибок", async () => {
    const weird = [{ sku: "X" }, { sku: "Y", name: null, brand: null, expected_price: "abc", barcodes: null }, P("Z", "Нормальный", "B", "K", "1500")];
    assert.strictEqual(C.filterProducts(weird, { q: "нормальный" }).length, 1);
    assert.strictEqual(C.filterProducts(weird, { priceMin: 1000 }).length, 1);
    assert.strictEqual(C.facets(weird, {}).total, 3);
    assert.strictEqual(C.sortProducts(weird, "price_asc")[0].sku, "Z");
    assert.strictEqual(C.groupProducts(weird, "brand").length, 2);
  });

  await test("в catalog.js нет обращений к странице (DOM), сети и опасных вызовов", async () => {
    const src = fs.readFileSync(path.join(__dirname, "..", "frontend", "catalog.js"), "utf-8");
    for (const bad of ["document.", "window.", "innerHTML", "fetch(", "eval(", "new Function", "localStorage"]) assert.ok(!src.includes(bad), `найдено ${bad}`);
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
