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

console.log(`\n${passed} passed`);
