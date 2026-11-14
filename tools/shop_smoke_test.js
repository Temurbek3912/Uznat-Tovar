// Страница покупателя (frontend/shop.js): чистые помощники и защита от подстановки разметки. node tools/shop_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const Shop = require("../frontend/shop.js");
const src = fs.readFileSync(path.join(__dirname, "..", "frontend", "shop.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "..", "frontend", "shop.html"), "utf8");
const H = Shop.helpers;
let passed = 0;
function test(name, fn) { fn(); passed++; console.log("ok  ", name); }

test("ответ сервера -> карточка: цена покупателя (price) попадает в поле карточки, служебных полей нет", () => {
  const v = H.toView({ status: "matched", by: "barcode", product: { name: "Кола", brand: "B", category: "Напитки", price: 9900 } });
  assert.deepStrictEqual(v, { state: "matched", product: { name: "Кола", brand: "B", category: "Напитки", expected_price: 9900 } });
  const u = H.toView({ status: "uncertain", product: { name: "A", price: null }, candidates: [{ name: "A" }, { name: "B" }] });
  assert.strictEqual(u.state, "uncertain"); assert.deepStrictEqual(u.candidates.map((c) => c.name), ["A", "B"]);
  assert.deepStrictEqual(H.toView({ status: "unknown" }), { state: "unknown" });
  assert.deepStrictEqual(H.toView(null), { state: "unknown" });
  assert.deepStrictEqual(H.toView({ status: "matched", product: null }), { state: "unknown" });
});

test("ошибки — понятными словами: частота с секундами, размер, фото, связь, прочее", () => {
  assert.deepStrictEqual(H.errorKey(429, { code: "public_rate_limited", params: { retry_after: 41 } }), { key: "shop.err_rate", params: { seconds: 41 } });
  assert.deepStrictEqual(H.errorKey(429, null), { key: "shop.err_rate", params: { seconds: 30 } });
  assert.strictEqual(H.errorKey(413, null).key, "shop.err_too_large");
  assert.strictEqual(H.errorKey(422, { code: "photo_not_image" }).key, "shop.err_photo");
  assert.strictEqual(H.errorKey(415, null).key, "shop.err_photo");
  assert.strictEqual(H.errorKey(0, null).key, "shop.err_network");
  assert.strictEqual(H.errorKey(503, { code: "public_unavailable" }).key, "shop.err_unavailable");
  assert.strictEqual(H.errorKey(500, null).key, "shop.err_unavailable");
});

test("уменьшение снимка: длинная сторона не больше предела, пропорции сохраняются, мелкие не увеличиваются", () => {
  assert.deepStrictEqual(H.fitSize(4000, 3000, 1600), { width: 1600, height: 1200 });
  assert.deepStrictEqual(H.fitSize(3000, 4000, 1600), { width: 1200, height: 1600 });
  assert.deepStrictEqual(H.fitSize(800, 600, 1600), { width: 800, height: 600 });
  assert.deepStrictEqual(H.fitSize(0, 0, 1600), { width: 1, height: 1 });
});

test("тексты покупателя есть на всех четырёх языках; подмены карточки ведут на существующие ключи", () => {
  const keys = new Set();
  for (const m of html.matchAll(/data-i18n="([^"]+)"/g)) keys.add(m[1]);
  for (const m of src.matchAll(/"(shop\.[a-z_]+)"/g)) keys.add(m[1]);
  Object.values(H.CUSTOMER_TEXT).forEach((k) => keys.add(k));
  for (const lang of ["ru", "uz", "uz-cyrl", "en"]) {
    const dict = fs.readFileSync(path.join(__dirname, "..", "frontend", `i18n.${lang}.js`), "utf8");
    for (const k of keys) assert.ok(dict.includes(`"${k}"`), `${lang}: нет ${k}`);
  }
});

test("без innerHTML/eval; снимок уходит только на свой сервер, без cookies; на странице нет форм входа и встроенных скриптов", () => {
  for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "localStorage"]) {
    assert.ok(!src.includes(bad), `shop.js: найдено ${bad}`);
  }
  assert.ok(src.includes('win.fetch("/api/v1/public/identify?lang="') && src.includes('credentials: "omit"'));
  assert.ok(!/https?:\/\//.test(src.replace(/\/\*[\s\S]*?\*\//g, "")), "shop.js: внешний адрес");
  assert.ok(!/<script>(?!<\/script>)/.test(html) && !html.includes("password") && !html.includes("auth.js"));
  assert.ok(html.includes('capture="environment"'), "камера телефона по <input capture> — работает без HTTPS");
});

console.log(`\n${passed} passed`);
