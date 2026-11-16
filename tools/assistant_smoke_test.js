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

console.log(`\n${passed} passed`);
