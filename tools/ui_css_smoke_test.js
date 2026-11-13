// Проверка стилей (frontend/ui.css) без браузера: только токены вместо «зашитых» цветов, все используемые токены существуют, нажимаемое место ≥ 44 px,
// фокус виден везде (и на тёмном видоискателе), телефонная раскладка есть, каждый класс, который рисуют скрипты и разметка, имеет стиль.
// Не проверяется внешний вид (его смотрят по снимкам Chromium: tools/ui_screenshots). Запуск из корня проекта:  node tools/ui_css_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const FRONT = path.join(__dirname, "..", "frontend");
const read = (f) => fs.readFileSync(path.join(FRONT, f), "utf-8");
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "");
const ui = strip(read("ui.css")), tokens = strip(read("tokens.css"));

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }
/** Тело правила по точному началу селектора (первое совпадение). */
function rule(selector) {
  const i = ui.indexOf(selector + " {");
  assert.ok(i !== -1, `нет правила ${selector}`);
  return ui.slice(i, ui.indexOf("}", i));
}

(async () => {
  await test("цвета только из токенов: нет #rrggbb, rgb(), hsl(); допустимы лишь чёрные/белые полупрозрачные подложки (наведение, затемнение)", async () => {
    assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(ui), "найден «зашитый» цвет #…");
    assert.ok(!/\b(?:rgb|hsl|hsla)\(/.test(ui), "найден rgb()/hsl()");
    for (const m of ui.matchAll(/rgba\(([^)]*)\)/g)) assert.ok(/^\s*(0,\s*0,\s*0|255,\s*255,\s*255)\s*,/.test(m[1]), `недопустимый rgba(${m[1]})`);
  });

  await test("каждый var(--…) в ui.css определён в tokens.css (иначе стиль молча не работает)", async () => {
    const defined = new Set([...tokens.matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]));
    const used = new Set([...ui.matchAll(/var\((--[\w-]+)/g)].map((m) => m[1]));
    const missing = [...used].filter((v) => !defined.has(v) && !/^--(?:mark-color|mark-inset)$/.test(v));   // две переменные видоискателя ui.css задаёт сам
    assert.deepStrictEqual(missing, []);
    assert.ok(used.size >= 30, "подозрительно мало токенов используется");
  });

  await test("офлайн: нет внешних адресов, @import и @font-face", async () => {
    assert.ok(!/url\(|@import|@font-face|https?:\/\//i.test(ui));
  });

  await test("нажимаемое место ≥ 44 px (var(--hit)): кнопки, ссылки-кнопки, поля, переключатель языка, строки групп, кандидаты, значковые кнопки", async () => {
    for (const sel of [".btn", ".link", ".lang-btn", ".group__head", ".cand", ".rail__item"]) assert.ok(/min-height:\s*var\(--hit\)/.test(rule(sel)), `${sel}: нет min-height: var(--hit)`);
    assert.ok(/min-height:\s*var\(--hit\)/.test(rule(".input, .select")));
    assert.ok(/min-height:\s*var\(--hit\)/.test(rule(".check")));
    const icon = rule(".icon-btn"); assert.ok(/width:\s*var\(--hit\)/.test(icon) && /height:\s*var\(--hit\)/.test(icon));
    assert.ok(/min-width:\s*var\(--hit\)/.test(ui.slice(ui.indexOf(".lang-btn[data-short]"))), "компактная кнопка языка уже 44 px");
  });

  await test("фокус виден: общее кольцо через токен; на тёмном видоискателе кольцо цвета метки (иначе кобальт сливается)", async () => {
    assert.ok(/:focus-visible\s*\{\s*outline:\s*3px solid var\(--focus\)/.test(ui));
    assert.ok(/\.stage :focus-visible\s*\{\s*outline-color:\s*var\(--mark\)/.test(ui));
    assert.ok(!/outline:\s*(?:none|0)\b/.test(ui), "кольцо фокуса не должно отключаться");
  });

  await test("телефон: меню внизу, на всю ширину; панель фильтров выезжает снизу и скрыта (visibility) пока закрыта; планшет — ценник не шире var(--sheet-max)", async () => {
    const phone = ui.slice(ui.indexOf("@media (max-width: 899px)"));
    assert.ok(/\.rail\s*\{[^}]*position:\s*fixed[^}]*bottom:\s*0/.test(phone));
    assert.ok(/safe-area-inset-bottom/.test(phone));
    assert.ok(/\.plist__filters\s*\{[^}]*visibility:\s*hidden/.test(phone) && /\.plist__filters\.is-open\s*\{[^}]*visibility:\s*visible/.test(phone));
    assert.ok(/min-width:\s*600px\) and \(max-width:\s*899px\)[^}]*\{[^}]*max-width:\s*var\(--sheet-max\)/.test(ui));
    assert.ok(/max-width:\s*479px/.test(ui), "очень узкий экран предусмотрен");
  });

  await test("рамки видоискателя: состояния найден / похоже (пунктир) / не найден (белые) / поиск (движение) / камера (скрыты) заданы", async () => {
    for (const s of ["matched", "uncertain", "unknown", "searching", "camera_error"]) assert.ok(ui.includes(`.stage[data-state="${s}"]`), s);
    assert.ok(/\.stage\[data-state="uncertain"\] \.mark\s*\{[^}]*dashed/.test(ui));
    assert.ok(/\.stage\[data-state="matched"\]\s*\{[^}]*--mark-color:\s*var\(--mark\)/.test(ui));
  });

  await test("движение: длительности переходов берутся из токенов (их гасит «уменьшить анимацию»); повторяющиеся анимации — только у поиска и заглушек", async () => {
    for (const m of ui.matchAll(/transition:\s*([^;]+);/g)) assert.ok(/var\(--dur/.test(m[1]), `переход без токена: ${m[1]}`);
    const infinite = [...ui.matchAll(/([^{}]+)\{[^}]*animation:[^}]*infinite/g)].map((m) => m[1].trim());
    assert.deepStrictEqual(infinite, ['.stage[data-state="searching"] .stage__marks', ".skeleton"]);
  });

  await test("каждый класс, который создают скрипты и разметка витрины, имеет правило в ui.css (кроме намеренно «пустых»)", async () => {
    const sources = ["ticket.js", "productlist.js", "preview.js", "i18n.js"].map(read).join("\n");
    const html = read("preview.html");
    const classes = new Set();
    for (const re of [/node\(doc, "[a-z0-9]+", "([^"]+)"/g, /className = "([^"]+)"/g, /className: "([^"]+)"/g]) for (const m of sources.matchAll(re)) m[1].split(/\s+/).forEach((c) => c && classes.add(c));
    for (const m of html.matchAll(/class="([^"]+)"/g)) m[1].split(/\s+/).forEach((c) => c && classes.add(c));
    for (const m of sources.matchAll(/"((?:btn|chip|ticket)--[a-z_]+)"/g)) classes.add(m[1]);
    for (const c of [...classes]) if (c.endsWith("--")) classes.delete(c);                 // «chip--» + вид: достраивается скриптом, варианты проверены ниже явным списком
    for (const c of ["btn--primary", "btn--secondary", "chip--ok", "chip--notice", "chip--danger"]) classes.add(c);
    const unstyledOnPurpose = new Set(["group", "group__name", "check__text", "ticket--matched", "ticket--uncertain", "ticket--searching", "ticket--error", "ticket--unknown", "ticket--idle", "plist__results", "icon", "chip--btn", "ticket--camera_error"]);
    const missing = [...classes].filter((c) => !unstyledOnPurpose.has(c) && !new RegExp("\\." + c.replace(/[-_]/g, "[-_]") + "(?![\\w-])").test(ui));
    assert.deepStrictEqual(missing, []);
    assert.ok(classes.size > 60, `собрано классов: ${classes.size}`);
  });

  await test("состояния ценника, нужные для раскладки (пусто, нет камеры, ошибка, не найден), получили стили; цена и характеристики — цифры ровными столбцами", async () => {
    for (const s of ["ticket--idle", "ticket--unknown", "ticket--empty_catalog", "ticket--camera_error", "ticket--error"]) assert.ok(ui.includes("." + s), s);
    assert.ok(/\.ticket__amount\s*\{[^}]*tabular-nums/.test(ui) && /\.spec dd\s*\{[^}]*tabular-nums/.test(ui) && /\.prow__price\s*\{[^}]*tabular-nums/.test(ui));
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
