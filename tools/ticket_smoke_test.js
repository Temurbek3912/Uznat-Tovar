// Проверка «ценника» (frontend/ticket.js) без браузера: все исходы узнавания, тексты на трёх языках, безопасность названий из базы,
// вызовы кнопок, цена. Данные выдуманные. Запуск из корня проекта:  node tools/ticket_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { text: tx, prefix: px } = require(path.join(__dirname, "test_texts.js"));
const { FakeDocument } = require("./fake_dom.js");
const FRONT = path.join(__dirname, "..", "frontend");
const I18N = require(path.join(FRONT, "i18n.js"));
const Icons = require(path.join(FRONT, "icons.js"));
const Ticket = require(path.join(FRONT, "ticket.js"));

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }

function setup(lang) {
  const i18n = I18N.create({ navigator: { languages: [lang || "ru"] } });
  for (const code of ["ru", "uz", "en"]) i18n.add(code, require(path.join(FRONT, `i18n.${code}.js`)));
  const doc = new FakeDocument();
  const host = doc.createElement("div");
  doc.body.appendChild(host);
  const calls = [];
  const deps = { t: (k, p) => i18n.t(k, p), icons: Icons };
  for (const n of ["onPick", "onAdd", "onRetry", "onPhoto", "onWrong", "onMore"]) deps[n] = (arg) => calls.push([n, arg]);
  return { i18n, doc, host, calls, deps, render: (view, extra) => Ticket.render(doc, host, view, Object.assign({}, deps, extra)), text: () => host.textContent };
}
const FIELDS = [1, 2, 3, 4, 5, 6].map((n) => ({ label: `Поле ${n}`, value: `Значение ${n}` }));
const PRODUCT = { id: "p1", name: "Ergo M200", brand: "Nordic", category: "Мыши", expected_price: 249000, fields: FIELDS };
const buttons = (host) => host.byTag("button");
const byText = (host, text) => buttons(host).find((b) => b.textContent.includes(text));

(async () => {
  await test("найден: бренд, название, категория, цена с разделителем тысяч, «Найдено», кнопки «Озвучить» больше нет", async () => {
    const s = setup("ru"); s.render({ state: "matched", product: PRODUCT });
    assert.strictEqual(s.host.getAttribute("data-state"), "matched");
    assert.strictEqual(s.host.byClass("ticket__name")[0].textContent, "Ergo M200");
    assert.strictEqual(s.host.byClass("ticket__brand")[0].textContent, "Nordic");
    assert.strictEqual(s.host.byClass("ticket__category")[0].textContent, "Мыши");
    assert.strictEqual(s.host.byClass("ticket__amount")[0].textContent, "249\u00A0000");
    assert.strictEqual(s.host.byClass("ticket__unit")[0].textContent, "сум");
    assert.ok(s.host.byClass("chip--ok")[0].textContent.includes("Найдено"));
    assert.ok(!byText(s.host, "Озвучить"), "озвучки нет: кнопки «Озвучить» нет");
    assert.ok(byText(s.host, "Не тот товар"), "остаётся ссылка «Не тот товар»");
  });

  await test("характеристики: первые 4 строками «название — значение», остальное за ссылкой «Все характеристики»", async () => {
    const s = setup("ru"); s.render({ state: "matched", product: PRODUCT });
    const rows = s.host.byClass("spec");
    assert.strictEqual(rows.length, 4);
    assert.strictEqual(rows[0].byTag("dt")[0].textContent, "Поле 1");
    assert.strictEqual(rows[0].byTag("dd")[0].textContent, "Значение 1");
    byText(s.host, "Все характеристики").click();
    assert.deepStrictEqual(s.calls.pop(), ["onMore", PRODUCT]);
    s.render({ state: "matched", product: Object.assign({}, PRODUCT, { fields: FIELDS.slice(0, 3) }) });
    assert.strictEqual(s.host.byClass("spec").length, 3);
    assert.ok(!byText(s.host, "Все характеристики"));
    s.render({ state: "matched", product: Object.assign({}, PRODUCT, { fields: undefined, category: "" }) });
    assert.strictEqual(s.host.byClass("ticket__specs").length, 0);
    assert.strictEqual(s.host.byClass("ticket__category").length, 0);
  });

  await test("цена не указана: вместо числа «Цена не указана» (а не 0 и не пустота)", async () => {
    const s = setup("ru");
    for (const price of [null, undefined, ""]) {
      s.render({ state: "matched", product: Object.assign({}, PRODUCT, { expected_price: price }) });
      assert.strictEqual(s.host.byClass("ticket__amount").length, 0);
      assert.ok(s.host.byClass("ticket__price--none")[0].textContent.includes("Цена не указана"));
    }
    s.render({ state: "matched", product: Object.assign({}, PRODUCT, { expected_price: 0 }) });
    assert.strictEqual(s.host.byClass("ticket__amount")[0].textContent, "0");
  });

  await test("кнопка найденного товара вызывает нужное действие: «Не тот товар»", async () => {
    const s = setup("ru"); s.render({ state: "matched", product: PRODUCT });
    byText(s.host, "Не тот товар").click();
    assert.deepStrictEqual(s.calls, [["onWrong", undefined]]);
  });

  await test("похоже: до трёх кандидатов кнопками, первый выделен, нажатие отдаёт выбранный товар", async () => {
    const s = setup("ru");
    const cands = [1, 2, 3, 4].map((n) => ({ id: `c${n}`, name: `Модель ${n}`, brand: `Бренд ${n}` }));
    s.render({ state: "uncertain", product: cands[0], candidates: cands });
    const items = s.host.byClass("cand");
    assert.strictEqual(items.length, 3);
    assert.ok(items[0].className.includes("cand--top") && !items[1].className.includes("cand--top"));
    assert.ok(s.host.byClass("chip--notice")[0].textContent.includes("Похоже"));
    assert.ok(s.text().includes("Это один из этих?"));
    items[1].click();
    assert.deepStrictEqual(s.calls, [["onPick", cands[1]]]);
    s.render({ state: "uncertain", product: cands[0], candidates: [] });
    assert.strictEqual(s.host.byClass("cand").length, 1);
  });

  await test("не найден и пустой каталог: причина простыми словами и главное действие «Добавить в каталог»", async () => {
    const s = setup("ru"); s.render({ state: "unknown" });
    assert.ok(s.host.byClass("chip--danger")[0].textContent.includes("Не найдено"));
    assert.ok(s.text().includes("Этого товара нет в каталоге"));
    byText(s.host, "Добавить в каталог").click(); byText(s.host, "Снять ещё раз").click();
    assert.deepStrictEqual(s.calls.map((c) => c[0]), ["onAdd", "onRetry"]);
    s.render({ state: "empty_catalog" });
    assert.ok(s.text().includes("В каталоге пока нет товаров"));
    assert.ok(byText(s.host, "Добавить в каталог").className.includes("btn--primary"));
  });

  await test("нет права добавлять товары (нет onAdd): кнопки «Добавить в каталог» нет, подсказка говорит, к кому обратиться, «Снять ещё раз» становится главной", async () => {
    const s = setup("ru"); s.render({ state: "unknown" }, { onAdd: undefined });
    assert.ok(!byText(s.host, "Добавить в каталог"));
    assert.ok(s.text().includes("Попросите того, кто ведёт каталог"));
    assert.ok(byText(s.host, "Снять ещё раз").className.includes("btn--primary"));
    s.render({ state: "empty_catalog" }, { onAdd: undefined });
    assert.ok(!byText(s.host, "Добавить в каталог"));
    assert.strictEqual(s.host.byClass("ticket__actions").length, 0, "пустого блока кнопок быть не должно");
    assert.ok(s.text().includes("Каталог ещё не заполнен"));
    const en = setup("en"); en.render({ state: "unknown" }, { onAdd: undefined });
    assert.ok(!en.text().includes("Add to catalog") && en.text().toLowerCase().includes("ask whoever"), en.text());
  });

  await test("идёт поиск: «Ищу…», заглушки скрыты от чтения, ценник помечен занятым; приглашение в начале", async () => {
    const s = setup("ru"); s.render({ state: "searching" });
    assert.strictEqual(s.host.getAttribute("aria-busy"), "true");
    assert.ok(s.text().includes("Ищу"));
    assert.strictEqual(s.host.byClass("ticket__sk")[0].getAttribute("aria-hidden"), "true");
    s.render({ state: "idle" });
    assert.strictEqual(s.host.getAttribute("aria-busy"), "false");
    assert.ok(s.text().includes("Покажите товар камере"));
  });

  await test("камера недоступна и ошибка: своя причина или стандартная, действия «Сделать фото» и «Повторить»", async () => {
    const s = setup("ru"); s.render({ state: "camera_error" });
    assert.ok(s.text().includes("Камера недоступна") && s.text().includes("Доступ к камере закрыт"));
    byText(s.host, "Сделать фото").click(); byText(s.host, "Повторить").click();
    assert.deepStrictEqual(s.calls.map((c) => c[0]), ["onPhoto", "onRetry"]);
    s.render({ state: "camera_error", message: "Камера занята другой программой" });
    assert.ok(s.text().includes("Камера занята другой программой"));
    s.render({ state: "error" });
    assert.ok(s.text().includes("Не удалось узнать товар") && s.text().includes("Что-то пошло не так"));
    assert.ok(!byText(s.host, "Сделать фото"));
  });

  await test("тексты на всех трёх языках; смена языка и повторная отрисовка меняют надпись", async () => {
    const s = setup("ru");
    s.render({ state: "unknown" }); assert.ok(s.text().includes("Не найдено"));
    s.i18n.setLang("uz"); s.render({ state: "unknown" }); assert.ok(s.text().includes(tx("uz", "ticket.not_found")) && s.text().includes(tx("uz", "identify.add_product")));
    s.i18n.setLang("en"); s.render({ state: "unknown" }); assert.ok(s.text().includes("Not found") && s.text().includes("Add to catalog"));
    s.render({ state: "matched", product: PRODUCT }); assert.ok(s.text().includes("Found") && s.text().includes("UZS") && !s.text().includes("Read aloud"));
  });

  await test("безопасность: название и бренд из базы попадают как текст — разметка не создаётся", async () => {
    const s = setup("ru"), evil = '<img src=x onerror="alert(1)">';
    s.render({ state: "matched", product: Object.assign({}, PRODUCT, { name: evil, brand: evil, category: evil, fields: [{ label: evil, value: evil }] }) });
    assert.strictEqual(s.host.byTag("img").length, 0);
    assert.strictEqual(s.host.byClass("ticket__name")[0].textContent, evil);
    s.render({ state: "uncertain", candidates: [{ name: evil, brand: evil }] });
    assert.strictEqual(s.host.byTag("img").length, 0);
  });

  await test("перерисовка стирает прежнее содержимое; неизвестное состояние показывает приглашение; пустой view не падает", async () => {
    const s = setup("ru");
    s.render({ state: "matched", product: PRODUCT }); s.render({ state: "unknown" });
    assert.strictEqual(s.host.children.length, 1);
    assert.strictEqual(s.host.byClass("ticket__name").length, 0);
    s.render({ state: "что-то-новое" }); assert.strictEqual(s.host.getAttribute("data-state"), "idle");
    s.render({ state: "matched" }); assert.ok(s.text().includes("Покажите товар камере"));
    s.render(undefined); assert.strictEqual(s.host.getAttribute("data-state"), "idle");
  });

  await test("цена: formatPrice (тысячи неразрывным пробелом, округление, пусто для не-чисел); фразы для озвучки больше не строятся", async () => {
    assert.strictEqual(Ticket.formatPrice(1290000.4), "1\u00A0290\u00A0000");
    assert.strictEqual(Ticket.formatPrice(999), "999");
    assert.strictEqual(Ticket.formatPrice(null), ""); assert.strictEqual(Ticket.formatPrice("abc"), "");
    assert.strictEqual(Ticket.speechSummary, undefined, "озвучки нет: speechSummary удалён");
  });

  await test("нет в базе, известен только вид: «Что это» + «Напиток» + «название определить не удалось»; на узбекском — свой текст", async () => {
    const s = setup("ru"); s.render({ state: "not_in_catalog", guess: { name: "", brand: "", kind: "drink" } }, { onAdd: undefined });
    assert.strictEqual(s.host.byClass("ticket__name")[0].textContent, "Напиток");
    assert.ok(s.text().includes("Что это") && s.text().includes("Точное название определить не удалось"));
    const u = setup("uz"); u.render({ state: "not_in_catalog", guess: { name: "", kind: "footwear" } }, { onAdd: undefined });
    assert.strictEqual(u.host.byClass("ticket__name")[0].textContent, "Poyabzal");
  });

  await test("нет в базе с названием: название крупно и строка «Вид товара: …»; неизвестный код вида не показывается", async () => {
    const s = setup("ru"); s.render({ state: "not_in_catalog", guess: { name: "Santal Artesian Water", brand: "Santal", kind: "drink" } });
    assert.strictEqual(s.host.byClass("ticket__name")[0].textContent, "Santal Artesian Water");
    assert.ok(s.text().includes("Вид товара: Напиток"));
    const x = setup("ru"); x.render({ state: "not_in_catalog", guess: { name: "Снек", kind: "weapons" } });
    assert.ok(!x.text().includes("Вид товара") && !x.text().includes("kind."));
  });

  await test("в ticket.js нет innerHTML/outerHTML/insertAdjacentHTML/eval", async () => {
    const src = fs.readFileSync(path.join(FRONT, "ticket.js"), "utf-8");
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) assert.ok(!src.includes(bad), bad);
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
