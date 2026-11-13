// Проверка значков (frontend/icons.js) без браузера: у каждого значка есть фигуры, контуры написаны по правилам SVG (опечатку в цифрах человек не заметит
// без картинки), все точки внутри рамки 24×24, цвет берётся от текста, без подписи значок скрыт от чтения с экрана, неизвестное имя не роняет страницу.
// Запуск из корня проекта:  node tools/icons_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { FakeDocument } = require("./fake_dom.js");
const Icons = require(path.join(__dirname, "..", "frontend", "icons.js"));

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }

const ARGS = { M: 2, L: 2, T: 2, H: 1, V: 1, C: 6, S: 4, Q: 4, A: 7, Z: 0 };
const NUM = /[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?/gi;
/** Проверка строки контура: только допустимые команды, число аргументов кратно нужному, первая команда — M. Возвращает все числа. */
function checkPath(d) {
  assert.ok(/^[MmLlHhVvCcSsQqTtAaZz0-9eE+\-.,\s]+$/.test(d), `недопустимые символы в «${d}»`);
  assert.ok(/^\s*[Mm]/.test(d), `контур должен начинаться с M: «${d}»`);
  const nums = [];
  for (const seg of d.match(/[A-Za-z][^A-Za-z]*/g)) {
    const cmd = seg[0].toUpperCase(), found = seg.slice(1).match(NUM) || [];
    assert.ok(cmd in ARGS, `команда ${seg[0]}`);
    if (ARGS[cmd] === 0) assert.strictEqual(found.length, 0, `Z с числами: «${seg}»`);
    else assert.ok(found.length >= ARGS[cmd] && found.length % ARGS[cmd] === 0, `«${seg}»: ${found.length} чисел, нужно кратно ${ARGS[cmd]}`);
    nums.push(...found.map(Number));
  }
  return nums;
}

(async () => {
  const doc = new FakeDocument();
  const NAMES = Icons.names();

  await test("набор значков: нужные для интерфейса есть", async () => {
    for (const n of ["viewfinder", "list", "sliders", "camera", "refresh", "check", "help", "search-off", "plus", "x", "alert", "globe", "sun", "moon", "monitor", "chevron"]) assert.ok(Icons.has(n), n);
  });

  await test("у каждого значка есть фигуры, а контуры и координаты корректны и лежат внутри рамки 24×24", async () => {
    for (const name of NAMES) {
      const shapes = Icons.SHAPES[name];
      assert.ok(shapes.length > 0, name);
      for (const [tag, a] of shapes) {
        assert.ok(["path", "circle", "line", "polyline", "polygon", "rect"].includes(tag), `${name}: ${tag}`);
        let nums = [];
        if (tag === "path") nums = checkPath(a.d);
        else if (tag === "circle") { nums = [a.cx - a.r, a.cx + a.r, a.cy - a.r, a.cy + a.r]; assert.ok(a.r > 0, name); }
        else if (tag === "line") nums = [a.x1, a.y1, a.x2, a.y2];
        else if (tag === "rect") nums = [a.x, a.y, a.x + a.width, a.y + a.height];
        else { nums = a.points.match(NUM).map(Number); assert.strictEqual(nums.length % 2, 0, `${name}: нечётное число координат`); }
        if (tag !== "path") for (const n of nums) assert.ok(n >= -0.01 && n <= 24.01, `${name}: ${tag} выходит за рамку (${n})`);
      }
    }
  });

  await test("make: рамка 24×24, размер, линия 1.75, цвет от текста (currentColor), без заливки", async () => {
    const svg = Icons.make(doc, "check", { size: 28 });
    assert.strictEqual(svg.namespace, Icons.NS);
    assert.strictEqual(svg.getAttribute("viewBox"), "0 0 24 24");
    assert.strictEqual(svg.getAttribute("width"), "28");
    assert.strictEqual(svg.getAttribute("stroke"), "currentColor");
    assert.strictEqual(svg.getAttribute("fill"), "none");
    assert.strictEqual(svg.getAttribute("stroke-width"), "1.75");
    assert.strictEqual(Icons.make(doc, "check").getAttribute("width"), "20");
  });

  await test("доступность: без подписи значок скрыт от чтения с экрана, с подписью — картинка с названием", async () => {
    const plain = Icons.make(doc, "camera");
    assert.strictEqual(plain.getAttribute("aria-hidden"), "true");
    assert.strictEqual(plain.getAttribute("role"), null);
    const labelled = Icons.make(doc, "camera", { label: "Фото" });
    assert.strictEqual(labelled.getAttribute("role"), "img");
    assert.strictEqual(labelled.getAttribute("aria-label"), "Фото");
    assert.strictEqual(labelled.getAttribute("aria-hidden"), null);
    assert.strictEqual(plain.getAttribute("focusable"), "false");
  });

  await test("make строит все фигуры значка; неизвестное имя — пустой значок без ошибки", async () => {
    for (const name of NAMES) assert.strictEqual(Icons.make(doc, name).children.length, Icons.SHAPES[name].length, name);
    const unknown = Icons.make(doc, "нет-такого");
    assert.strictEqual(unknown.children.length, 0);
    assert.strictEqual(Icons.has("нет-такого"), false);
    assert.strictEqual(Icons.has("constructor"), false);
  });

  await test("в icons.js нет цветов, innerHTML и опасных вызовов (цвет задаёт только страница)", async () => {
    const src = fs.readFileSync(path.join(__dirname, "..", "frontend", "icons.js"), "utf-8");
    assert.ok(!/#[0-9a-fA-F]{3,6}\b/.test(src), "цвет в значках");
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"]) assert.ok(!src.includes(bad), bad);
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
