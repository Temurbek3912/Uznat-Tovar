// Проверка токенов дизайна (frontend/tokens.css) без браузера: читается сам CSS, контраст считается по формуле WCAG 2.
// Проверяется: полнота цветов, одинаковость двух записей тёмной темы, контраст всех пар «текст на фоне» и границ в обеих темах,
// размеры (нажимаемое место ≥ 44 px, шрифт ≥ 12 px), движение при «уменьшить анимацию», отсутствие внешних адресов и шрифтов.
// Не проверяется: как выглядит на настоящем экране (это смотрит человек по скриншотам) и контраст цветов, заданных не токенами.
// Запуск из корня проекта:  node tools/tokens_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const css = fs.readFileSync(path.join(__dirname, "..", "frontend", "tokens.css"), "utf-8").replace(/\/\*[\s\S]*?\*\//g, "");

let passed = 0;
async function test(name, fn) {
  try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; }
}

// ---------------------------------------------------------------- разбор CSS (достаточно для файла, где только переменные)
function blockAfter(text, headerRe) {
  const m = headerRe.exec(text);
  if (!m) return null;
  let i = m.index + m[0].length, depth = 1;
  const start = i;
  while (i < text.length && depth > 0) { if (text[i] === "{") depth++; else if (text[i] === "}") depth--; i++; }
  return text.slice(start, i - 1);
}
function decls(block) {
  const out = {};
  const re = /(--[\w-]+)\s*:\s*([^;]+);/g;
  let m;
  while ((m = re.exec(block || ""))) out[m[1]] = m[2].trim();
  return out;
}
const base = decls(blockAfter(css, /^:root\s*\{/m));
const darkMedia = decls(blockAfter(blockAfter(css, /@media \(prefers-color-scheme: dark\)\s*\{/), /:root:not\(\[data-theme="light"\]\)\s*\{/));
const darkForced = decls(blockAfter(css, /:root\[data-theme="dark"\]\s*\{/));
const reduced = blockAfter(css, /@media \(prefers-reduced-motion: reduce\)\s*\{/);

// ---------------------------------------------------------------- контраст WCAG 2
function luminance(hex) {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
function ratio(a, b) { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); }
const isHex = (v) => /^#[0-9A-Fa-f]{6}$/.test(v);

// [передний, задний, минимум, что это]: 4.5 — обычный текст, 3 — границы и значки (WCAG 1.4.3 / 1.4.11)
const PAIRS = [
  ["--ink", "--bg", 4.5, "основной текст на фоне"], ["--ink", "--surface", 4.5, "основной текст на ценнике"],
  ["--ink", "--surface-2", 4.5, "основной текст на вторичной заливке"],
  ["--ink-muted", "--bg", 4.5, "второстепенный текст на фоне"], ["--ink-muted", "--surface", 4.5, "второстепенный текст на ценнике"],
  ["--ink-muted", "--surface-2", 4.5, "второстепенный текст на вторичной заливке"],
  ["--link", "--surface", 4.5, "ссылка на ценнике"], ["--link", "--bg", 4.5, "ссылка на фоне"],
  ["--on-action", "--action", 4.5, "текст основной кнопки"],
  ["--danger", "--surface", 4.5, "ошибка на ценнике"], ["--danger", "--danger-soft", 4.5, "ошибка на своём фоне"], ["--ink", "--danger-soft", 4.5, "текст на фоне ошибки"],
  ["--ok", "--surface", 4.5, "«хорошо» на ценнике"], ["--ok", "--ok-soft", 4.5, "«хорошо» на своём фоне"], ["--ink", "--ok-soft", 4.5, "текст на фоне «хорошо»"],
  ["--ink", "--notice-soft", 4.5, "текст на фоне подсказки"],
  ["--on-stage", "--stage", 4.5, "текст на видоискателе"], ["--mark", "--stage", 3, "рамка-метка на видоискателе"],
  ["--line-strong", "--surface", 3, "граница поля ввода"], ["--line-strong", "--bg", 3, "граница контрола на фоне"],
  ["--action-edge", "--surface", 3, "рамка основной кнопки на ценнике"], ["--action-edge", "--bg", 3, "рамка основной кнопки на фоне"],
  ["--focus", "--bg", 3, "кольцо фокуса на фоне"], ["--focus", "--surface", 3, "кольцо фокуса на ценнике"]
];

const COLOR_TOKENS = ["--bg", "--surface", "--surface-2", "--ink", "--ink-muted", "--line", "--line-strong", "--action", "--on-action", "--action-edge",
  "--link", "--focus", "--danger", "--danger-soft", "--ok", "--ok-soft", "--notice-soft", "--stage", "--on-stage", "--mark"];

(async () => {
  await test("формула контраста верна: чёрное на белом 21, одинаковые 1, #777 на белом ≈ 4.48 (известное значение WCAG)", async () => {
    assert.strictEqual(Math.round(ratio("#000000", "#FFFFFF") * 100) / 100, 21);
    assert.strictEqual(ratio("#336699", "#336699"), 1);
    assert.strictEqual(Math.round(ratio("#777777", "#FFFFFF") * 100) / 100, 4.48);
  });

  await test("светлая тема: все цветовые токены есть и записаны как #RRGGBB", async () => {
    for (const t of COLOR_TOKENS) assert.ok(isHex(base[t] || ""), `${t}: ${base[t]}`);
  });

  await test("тёмная тема задаёт ВСЕ цветовые токены, и лишних имён нет", async () => {
    for (const t of COLOR_TOKENS) assert.ok(isHex(darkMedia[t] || ""), `по системе: нет ${t}`);
    for (const t of Object.keys(darkMedia)) assert.ok(t in base, `по системе: лишний ${t}`);
    for (const t of Object.keys(darkForced)) assert.ok(t in base, `принудительно: лишний ${t}`);
  });

  await test("две записи тёмной темы (по системе и по выбору человека) совпадают до последнего значения", async () => {
    assert.deepStrictEqual(darkForced, darkMedia);
  });

  for (const [theme, tokens] of [["светлая", base], ["тёмная", darkMedia]]) {
    await test(`контраст, ${theme} тема: все ${PAIRS.length} пар не ниже нормы AA`, async () => {
      const low = [];
      for (const [fg, bg, min, what] of PAIRS) {
        const r = ratio(tokens[fg], tokens[bg]);
        if (r < min) low.push(`${what}: ${r.toFixed(2)} < ${min} (${fg} ${tokens[fg]} на ${bg} ${tokens[bg]})`);
      }
      assert.deepStrictEqual(low, []);
    });
  }

  await test("размеры: нажимаемое место ≥ 44 px, основной шрифт 1rem, самый мелкий не меньше 12 px", async () => {
    assert.ok(parseInt(base["--hit"], 10) >= 44);
    assert.strictEqual(base["--fs-m"], "1rem");
    assert.ok(parseFloat(base["--fs-xs"]) >= 0.75);
  });

  await test("отступы растут по порядку; скруглений не одно на всё (≥ 4 разных)", async () => {
    const sp = ["--sp-1", "--sp-2", "--sp-3", "--sp-4", "--sp-5", "--sp-6", "--sp-7"].map((t) => parseFloat(base[t]));
    assert.ok(sp.every((v, i) => i === 0 || v > sp[i - 1]), sp.join(","));
    const radii = new Set(["--r-s", "--r-m", "--r-tag", "--r-l"].map((t) => base[t]));
    assert.ok(radii.size >= 4, [...radii].join(","));
  });

  await test("«уменьшить анимацию»: все длительности ≈ 0 и анимации/переходы отключаются", async () => {
    assert.ok(reduced, "нет блока prefers-reduced-motion");
    const d = decls(reduced);
    for (const t of ["--dur-fast", "--dur", "--dur-slow"]) assert.ok(parseFloat(d[t]) <= 1, `${t}: ${d[t]}`);
    assert.ok(/animation-duration:\s*0\.01ms/.test(reduced) && /transition-duration:\s*0\.01ms/.test(reduced));
  });

  await test("офлайн: нет внешних адресов, @import и веб-шрифтов; шрифт системный с запасными", async () => {
    assert.ok(!/url\(|@import|https?:\/\//i.test(css));
    for (const f of ["system-ui", "Segoe UI", "Roboto", "sans-serif"]) assert.ok(base["--font-sans"].includes(f), f);
  });

  await test("файл содержит только переменные: допустимы лишь :root, тёмные блоки и общее правило для «уменьшить анимацию»", async () => {
    const headers = [...css.matchAll(/([^{}]+)\{/g)].map((m) => m[1].trim());
    assert.deepStrictEqual(headers, [
      ":root", "@media (prefers-color-scheme: dark)", ":root:not([data-theme=\"light\"])", ":root[data-theme=\"dark\"]",
      "@media (prefers-reduced-motion: reduce)", ":root", "*, *::before, *::after"
    ]);
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
