// Проверка страницы «Помощник» (frontend/assistant.js) без браузера: память вкладки и индикатор памяти.
// Запуск из корня проекта:  node tools/assistant_smoke_test.js
"use strict";
const assert = require("assert");
const path = require("path");
const Assistant = require(path.join(__dirname, "..", "frontend", "assistant.js"));

let passed = 0;
function test(name, fn) { try { fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }

// те же числа, что отдаёт GET /api/v1/assistant/status (app/services/assistant.py: MAX_HISTORY, MAX_HISTORY_CHARS, MAX_MESSAGE_CHARS)
const LIMITS = { max_messages: 8, max_chars: 3000, max_message_chars: 1500 };
const u = (content) => ({ role: "user", content, tools: [] });
const a = (content) => ({ role: "assistant", content, tools: [] });
const pairs = (n, size) => { const out = []; for (let i = 0; i < n; i++) out.push(u("в".repeat(size)), a("о".repeat(size))); return out; };

test("cleanLog оставляет только реплики user/assistant с текстом; toSend — последние 8 без tools", () => {
  const log = Assistant.cleanLog([u("привет"), { role: "system", content: "x" }, a("  "), null, a("здравствуйте")]);
  assert.deepStrictEqual(log.map((m) => m.role), ["user", "assistant"]);
  assert.deepStrictEqual(Assistant.toSend(pairs(6, 1)).length, 8);
  assert.deepStrictEqual(Object.keys(Assistant.toSend(log)[0]), ["role", "content"]);
  assert.deepStrictEqual(Assistant.cleanLog("мусор"), []);
});

test("пустой разговор (и после очистки истории) — 0 %, ничего не забыто; без лимитов от сервера — тоже 0", () => {
  assert.deepStrictEqual(Assistant.memory([], LIMITS), { percent: 0, full: false, forgotten: 0 });
  assert.deepStrictEqual(Assistant.memory(pairs(2, 10), null), { percent: 0, full: false, forgotten: 0 });
});

test("короткие реплики считаются по числу мест: 7 мест на прошлый разговор, восьмое — новому вопросу", () => {
  assert.deepStrictEqual(Assistant.memory(pairs(1, 10), LIMITS), { percent: 29, full: false, forgotten: 0 });   // 2 из 7
  assert.deepStrictEqual(Assistant.memory(pairs(2, 10), LIMITS), { percent: 57, full: false, forgotten: 0 });   // 4 из 7
  assert.strictEqual(Assistant.memory(pairs(3, 10), LIMITS).full, true);                                      // 6 из 7 = 86 %
});

test("длинные реплики считаются по символам (каждая не длиннее 1500, как на сервере)", () => {
  // место под прошлый разговор: 3000 − 100 (новому вопросу) = 2900 символов
  assert.deepStrictEqual(Assistant.memory([u("в".repeat(580)), a("о".repeat(580))], LIMITS), { percent: 40, full: false, forgotten: 0 });
  assert.strictEqual(Assistant.memory([u("в".repeat(5000))], LIMITS).percent, 52);                            // обрезана до 1500
});

test("переполнение — 100 %, старые реплики помечены забытыми; память начинается с вопроса, как на сервере", () => {
  const many = pairs(5, 10);                                    // 10 реплик: в 7 последних мест первая — ответ, он тоже уходит
  assert.deepStrictEqual(Assistant.memory(many, LIMITS), { percent: 100, full: true, forgotten: 4 });
  const long = pairs(3, 700);                                   // 6 × 700: свежие 4 реплики = 2800, пятая уже не влезает в 2900
  assert.deepStrictEqual(Assistant.memory(long, LIMITS), { percent: 100, full: true, forgotten: 2 });
});

test("сверка с сервером: забытые реплики — ровно те, что clean_history отбросит при следующем вопросе обычной длины", () => {
  // повтор правил app/services/assistant.py:clean_history (последние 8, свежие важнее, не больше 3000, начало — с вопроса)
  function server(messages) {
    const out = messages.slice(-LIMITS.max_messages).map((m) => ({ role: m.role, content: m.content.trim().slice(0, LIMITS.max_message_chars) }));
    let total = 0; const kept = [];
    for (let i = out.length - 1; i >= 0; i--) { total += out[i].content.length; if (kept.length && total > LIMITS.max_chars) break; kept.unshift(out[i]); }
    while (kept.length && kept[0].role !== "user") kept.shift();
    return kept.length;
  }
  for (const size of [1, 50, 300, 700, 1200, 1450, 1500, 2000]) {
    for (let n = 1; n <= 8; n++) {
      const log = pairs(n, size);
      const mem = Assistant.memory(log, LIMITS);
      const remembered = server(log.concat([u("?".repeat(Assistant.QUESTION_RESERVE))])) - 1;
      assert.strictEqual(log.length - mem.forgotten, remembered, `пар ${n} по ${size} символов`);
      if (mem.forgotten) assert.strictEqual(mem.percent, 100);
    }
  }
});

// --- этап A2: карточка «Помощник предлагает» ---------------------------------------------------------------------------------
const RU = {};
const ruSrc = require("fs").readFileSync(path.join(__dirname, "..", "frontend", "i18n.ru.js"), "utf8");
for (const m of ruSrc.matchAll(/^\s*"([^"]+)": (".*?"),?$/gm)) RU[m[1]] = JSON.parse(m[2]);
const tr = (key, params) => {
  assert.ok(key in RU, "нет перевода: " + key);
  return RU[key].replace(/\{(\w+)\}/g, (_, k) => String((params || {})[k]));
};

test("карточка изменения: было → стало, пусто — «не указано», наличие словами, штрихкод без «было»", () => {
  const lines = Assistant.describe({ kind: "update", name: "Кока-кола", sku: "COLA", changes: { price: 15000, in_stock: false,
    description: null, add_barcode: "4780001234579" }, before: { price: null, in_stock: true, description: "Старое" } }, tr);
  assert.deepStrictEqual(lines, ["Товар «Кока-кола» (артикул COLA)", "Цена: не указано → 15000", "В наличии: да → нет",
                                 "Описание: Старое → не указано", "Добавить штрихкод: 4780001234579"]);
});

test("карточки архива и нового товара; у каждого поля, которое может предложить сервер, есть подпись", () => {
  assert.ok(Assistant.describe({ kind: "status", status: "archived", name: "Хлеб", sku: "BREAD" }, tr)[0].startsWith("Убрать в архив: «Хлеб»"));
  assert.ok(Assistant.describe({ kind: "status", status: "active", name: "Хлеб", sku: "BREAD" }, tr)[0].startsWith("Вернуть из архива"));
  const add = Assistant.describe({ kind: "add", name: "Сок", price: 12000, brand: "Не указан", category: "Без категории", barcode: null }, tr);
  assert.deepStrictEqual(add.slice(0, 3), ["Добавить новый товар:", "Название: Сок", "Цена: 12000"]);
  // тот же список, что EDIT_FIELDS в app/services/assistant_actions.py
  for (const f of ["name", "price", "brand", "category", "in_stock", "add_barcode", "remove_barcode", "net_content", "manufacturer",
                   "country", "ingredients", "description"]) tr("assistant.field." + f);
  assert.deepStrictEqual(Assistant.describe(null, tr), []);
});

test("память вкладки хранит карточку и смену языка/фона; «выполняется» после перезагрузки — снова вопрос; мусор отбрасывается", () => {
  const log = Assistant.cleanLog([
    u("цена"),
    { role: "assistant", content: "Предлагаю", tools: ["propose_update"], card: { action: { kind: "update", changes: {} }, state: "working" },
      ui: [{ kind: "theme", value: "dark" }, { kind: "sql", value: "x" }] },
    { role: "assistant", content: "x", card: { action: { kind: "delete" }, state: "ask" } },
    { role: "user", content: "y", card: { action: { kind: "update" }, state: "ask" } },
  ]);
  assert.strictEqual(log[1].card.state, "ask");
  assert.deepStrictEqual(log[1].ui, [{ kind: "theme", value: "dark" }]);
  assert.ok(!("card" in log[2]) && !("card" in log[3]));        // неизвестное действие и карточка у вопроса — отброшены
  assert.deepStrictEqual(Object.keys(Assistant.toSend(log)[1]), ["role", "content"]);   // на сервер карточка не уходит
});

test("предупреждения «ничего не изменилось» и «не предложено» сохраняются только у ответов помощника и только известные", () => {
  const log = Assistant.cleanLog([
    u("удали"), { role: "assistant", content: "Убран в архив", warn: "no_card" },
    u("цена"), { role: "assistant", content: "Нажмите", warn: "failed", why: "Цена — это число" },
    { role: "assistant", content: "x", warn: "hack" }, { role: "user", content: "y", warn: "no_card" },
  ]);
  assert.strictEqual(log[1].warn, "no_card");
  assert.deepStrictEqual([log[3].warn, log[3].why], ["failed", "Цена — это число"]);
  assert.ok(!("warn" in log[4]) && !("warn" in log[5]));
  tr("assistant.no_card"); tr("assistant.proposal_failed", { why: "x" }); tr("assistant.proposal_not_made");
});

test("сводка сканов сверху страницы: числа и советы на языке страницы, без модели; пусто и «ещё не считали» — словами", () => {
  const rep = {
    days: 30, scans: 30, started: true, counts: { matched: 13, uncertain: 7, not_in_catalog: 6, unknown: 4 },
    weak_total: 9, weak: [{ sku: "COLA", name: "Кока-кола", scans: 5, unsure: 3, photos: 1, tips: ["more_photos", "add_barcode", "hack"] }],
    missing_total: 2, missing: [{ name: "Pepsi 1 л", kind: null, times: 2 }, { name: null, kind: "drink", times: 1 }],
    many_unknown: false,
  };
  const sum = Assistant.scanSummary(rep, tr);
  assert.strictEqual(sum.line, "Сканов за 30 дн.: 30. Узнал уверенно — 13, неуверенно — 7; товара нет в каталоге — 6; не понял, что на фото, — 4.");
  assert.strictEqual(sum.weak.title, "Узнаются плохо — товаров: 9");
  assert.strictEqual(sum.weak.more, "Показаны первые 1 из 9.");
  assert.strictEqual(sum.weak.rows[0].text, "«Кока-кола» (артикул COLA): сканов 5, из них неуверенно 3; фото — 1");
  assert.deepStrictEqual(sum.weak.rows[0].tips, [tr("assistant.tip_code.more_photos"), tr("assistant.tip_code.add_barcode")]);
  assert.deepStrictEqual(sum.missing.rows.map((r) => r.text), ["Pepsi 1 л — раз: 2", "без названия (Напиток) — раз: 1"]);
  assert.strictEqual(sum.missing.more, "");
  assert.deepStrictEqual(sum.notes, []);
  const good = Assistant.scanSummary({ ...rep, weak: [], weak_total: 0, missing: [], many_unknown: true }, tr);
  assert.deepStrictEqual(good.notes, [tr("assistant.scans_all_good"), tr("assistant.scans_many_unknown")]);
  assert.deepStrictEqual(Assistant.scanSummary({ started: false, scans: 0 }, tr).notes, [tr("assistant.scans_not_started")]);
  assert.deepStrictEqual(Assistant.scanSummary({ started: true, scans: 0, days: 7 }, tr).notes, [tr("assistant.scans_none")]);
  assert.strictEqual(Assistant.scanSummary(null, tr).line, "");
  ["no_photo", "check_photos"].forEach((k) => tr("assistant.tip_code." + k));
  ["title", "days", "period", "missing_tip", "error"].forEach((k) => tr("assistant.scans_" + k, { n: 1 }));
});

test("массовое изменение: заголовок с числом и знаком, строки «было → станет», пропущенные и «всё вместе»; в архив — без «было»", () => {
  const action = { kind: "bulk", field: "price", change: "price_percent", value: 10, skipped: 1, where: "category", query: "Напитки",
                   rows: [{ sku: "COLA", name: "Кока-кола", before: 9000, after: 9900 }, { sku: "FANTA", name: "Фанта", before: 8000, after: 8800 }] };
  const lines = Assistant.describe(action, tr);
  assert.strictEqual(lines[0], "Изменить сразу несколько товаров (2): цена +10 %");
  assert.deepStrictEqual(lines.slice(1), ["«Кока-кола» (COLA): 9000 → 9900", "«Фанта» (FANTA): 8000 → 8800"]);
  assert.deepStrictEqual(Assistant.describeBulk(action, tr).notes, ["Выбраны товары, у которых в категории есть «Напитки».",
    tr("assistant.bulk_skipped", { n: 1 }), tr("assistant.bulk_together")]);
  assert.strictEqual(Assistant.describeBulk({ ...action, where: "sql" }, tr).notes.length, 2);       // неизвестный отбор не показывается
  const arch = Assistant.describe({ kind: "bulk", field: "status", change: "archive", rows: [{ sku: "A", name: "Хлеб", before: "active", after: "archived" }] }, tr);
  assert.deepStrictEqual(arch, ["Изменить сразу несколько товаров (1): убрать в архив (вернуть можно в любой момент)", "«Хлеб» (A)"]);
  assert.ok(Assistant.describe({ kind: "bulk", change: "price_add", value: -500, rows: [] }, tr)[0].endsWith("цена -500 сум"));
  assert.strictEqual(Assistant.describe({ kind: "bulk", change: "drop_table", rows: "x" }, tr)[0], "Изменить сразу несколько товаров (0): ");
  const kept = Assistant.cleanLog([u("цены"), { role: "assistant", content: "ok", card: { action, state: "done", undo: { kind: "bulk", rows: [] } } }]);
  assert.strictEqual(kept[1].card.action.kind, "bulk");                 // карточка переживает перезагрузку вкладки
  ["price_set", "category", "brand", "in_stock", "restore"].forEach((k) => tr("assistant.bulk." + k, { value: 1 }));
  tr("assistant.undo_all"); tr("assistant.tool.propose_bulk");
});

test("категория по-узбекски: карточка называет категории каталога, которые выбрала программа", () => {
  const action = { kind: "bulk", field: "status", change: "archive", where: "category", query: "gazak", categories: ["Снеки", 5, ""],
                   rows: [{ sku: "P", name: "Pringles", before: "active", after: "archived" }] };
  assert.deepStrictEqual(Assistant.describeBulk(action, tr).notes, ["Выбраны товары категорий: Снеки (по слову «gazak»)."]);
  assert.strictEqual(Assistant.describeBulk({ ...action, categories: [] }, tr).notes[0],
    "Выбраны товары, у которых в категории есть «gazak».");                 // старые карточки без списка — как раньше
  assert.strictEqual(Assistant.describeBulk({ ...action, where: "brand" }, tr).notes[0], "Выбраны товары, у которых в бренде есть «gazak».");
});

test("список «товар — цена»: строки «было → станет», нет в каталоге, ошибки по строкам; менять нечего — свой заголовок", () => {
  const action = { kind: "bulk", field: "price", change: "price_list", value: null, where: "list", query: "", skipped: 1, cut: 3,
                   rows: [{ sku: "F", name: "Fanta", before: 9000, after: 9500 }],
                   new: [{ line: "Sprite — 9000", name: "Sprite" }, "мусор"],
                   errors: [{ line: "Печенье 12000", code: "many", names: ["Oreo", "Prince"], more: 2 }, { line: "Хлеб", code: "rm -rf" }] };
  const lines = Assistant.describe(action, tr);
  assert.deepStrictEqual(lines, ["Изменить сразу несколько товаров (1): цены по вашему списку", "«Fanta» (F): 9000 → 9500"]);
  assert.deepStrictEqual(Assistant.describeBulk(action, tr).notes, [
    tr("assistant.list_new", { n: 1, list: "Sprite" }),
    "Строка «Печенье 12000»: подходит несколько товаров (Oreo, Prince, и ещё 2) — напишите название точнее или артикул.",
    tr("assistant.list_error.no_price", { line: "Хлеб" }),               // неизвестный код — как «нет цены»
    "Цена уже такая: 1.", tr("assistant.list_cut", { n: 3, max: 50 })]);
  assert.ok(!Assistant.describeBulk(action, tr).notes.includes(tr("assistant.bulk_skipped", { n: 1 })));
  assert.strictEqual(Assistant.describe({ ...action, rows: [] }, tr)[0], "По вашему списку менять в каталоге нечего.");
  ["bad_price", "archived", "repeat"].forEach((k) => tr("assistant.list_error." + k, { line: "x", list: "y" }));
  tr("assistant.list_hint");
});

test("нет в каталоге: кнопки «Добавить с фото» — карточка нового товара с названием и ценой из строки", () => {
  const action = { kind: "bulk", change: "price_list", rows: [],
                   new: [{ line: "Sprite 0,5 — 9000", name: " Sprite 0,5 ", price: 9000 }, "мусор", { name: "" },
                         { line: "Хлеб 0", name: "Хлеб", price: -5 }, { name: "x".repeat(300), price: 1 }] };
  const items = Assistant.newItems(action);
  assert.deepStrictEqual(items.map((i) => [i.index, i.name.length, i.price]), [[0, 10, 9000], [3, 4, null], [4, 120, 1]]);
  assert.deepStrictEqual(Assistant.addAction(items[0]),
    { kind: "add", name: "Sprite 0,5", price: 9000, brand: "", category: "", barcode: "" });
  assert.deepStrictEqual(Assistant.newItems({ ...action, change: "price_set" }), []);        // только список цен
  assert.deepStrictEqual(Assistant.newItems({ ...action, undo: true }), []);                 // обратное действие — без кнопок
  assert.deepStrictEqual(Assistant.newItems(null), []);
  // какие кнопки уже нажаты — помнит карточка (память вкладки), мусор отбрасывается
  assert.deepStrictEqual(Assistant.cleanCard({ action, state: "ask", opened: [0, "1", -1, 2.5, 3] }).opened, [0, 3]);
  assert.deepStrictEqual(Assistant.cleanCard({ action, state: "ask" }).opened, []);
  assert.deepStrictEqual(Assistant.describe(Assistant.addAction(items[0]), tr),
    ["Добавить новый товар:", "Название: Sprite 0,5", "Цена: 9000", "Бренд: не указано", "Категория: не указано"]);
  assert.strictEqual(tr("assistant.list_add", { name: "Sprite" }), "Добавить «Sprite» с фото");
  tr("assistant.list_add_reply", { name: "Sprite" });
});

console.log(`\n${passed} passed`);
