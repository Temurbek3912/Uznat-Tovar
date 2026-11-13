// Проверка выбора темы (frontend/theme.js) без браузера. Запуск из корня проекта:  node tools/theme_smoke_test.js
"use strict";
const assert = require("assert");
const path = require("path");
const { FakeDocument, makeStorage } = require("./fake_dom.js");
const Theme = require(path.join(__dirname, "..", "frontend", "theme.js"));

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }
const mm = (dark) => () => ({ matches: dark });

(async () => {
  await test("по умолчанию «как в системе»: атрибута нет, видимая тема берётся из настройки устройства", async () => {
    const doc = new FakeDocument();
    const th = Theme.create({ localStorage: makeStorage(), document: doc, matchMedia: mm(true) });
    th.apply();
    assert.strictEqual(th.get(), "system");
    assert.strictEqual(doc.documentElement.getAttribute("data-theme"), null);
    assert.strictEqual(th.effective(), "dark");
    assert.strictEqual(Theme.create({ matchMedia: mm(false) }).effective(), "light");
    assert.strictEqual(Theme.create({}).effective(), "light");
  });

  await test("выбор светлой/тёмной ставит <html data-theme>, запоминается и переживает «перезапуск»", async () => {
    const storage = makeStorage(), doc = new FakeDocument();
    const th = Theme.create({ localStorage: storage, document: doc });
    assert.ok(th.set("dark"));
    assert.strictEqual(doc.documentElement.getAttribute("data-theme"), "dark");
    assert.strictEqual(storage._m.get("app_theme"), "dark");
    const doc2 = new FakeDocument();
    const again = Theme.create({ localStorage: storage, document: doc2 });
    again.apply();
    assert.strictEqual(again.get(), "dark");
    assert.strictEqual(doc2.documentElement.getAttribute("data-theme"), "dark");
    assert.strictEqual(again.effective(), "dark");
  });

  await test("возврат к «как в системе» убирает атрибут", async () => {
    const doc = new FakeDocument(), th = Theme.create({ localStorage: makeStorage(), document: doc });
    th.set("light"); assert.strictEqual(doc.documentElement.getAttribute("data-theme"), "light");
    th.set("system"); assert.strictEqual(doc.documentElement.getAttribute("data-theme"), null);
  });

  await test("несуществующая тема отвергается; мусор в хранилище игнорируется", async () => {
    const th = Theme.create({ localStorage: makeStorage({ app_theme: "neon" }) });
    assert.strictEqual(th.get(), "system");
    assert.strictEqual(th.set("neon"), false);
    assert.strictEqual(th.get(), "system");
  });

  await test("next: по кругу system -> light -> dark -> system", async () => {
    const th = Theme.create({ localStorage: makeStorage(), document: new FakeDocument() });
    const seen = []; for (let i = 0; i < 4; i++) { th.next(); seen.push(th.get()); }
    assert.deepStrictEqual(seen, ["light", "dark", "system", "light"]);
  });

  await test("слушатели: вызываются при выборе, отписка работает, сломанный не мешает", async () => {
    const th = Theme.create({ localStorage: makeStorage(), document: new FakeDocument() });
    const seen = [];
    th.onChange(() => { throw new Error("сломанный"); });
    const off = th.onChange((m) => seen.push(m));
    th.set("dark"); off(); th.set("light");
    assert.deepStrictEqual(seen, ["dark"]);
  });

  await test("хранилище недоступно — тема работает в памяти; matchMedia бросает ошибку — светлая", async () => {
    const broken = { getItem() { throw new Error("x"); }, setItem() { throw new Error("y"); } };
    const th = Theme.create({ localStorage: broken, document: new FakeDocument() });
    assert.ok(th.set("dark")); assert.strictEqual(th.get(), "dark");
    assert.strictEqual(Theme.create({ matchMedia() { throw new Error("z"); } }).effective(), "light");
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
