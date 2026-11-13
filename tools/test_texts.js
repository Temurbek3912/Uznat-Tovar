// Тексты словарей для проверок (подключается из *_smoke_test.js). Тесты берут формулировку ИЗ СЛОВАРЯ, а не вшивают её в код: узбекские тексты — черновик,
// переводчик вправе поменять слова, и проверки не должны от этого краснеть (проверяется, что текст ВОШЁЛ на экран, а не какими словами он написан).
// text(lang, key, params): подстановки {имя} заменяются значениями params; у форм множественного числа берётся форма other.
// prefix(lang, key): всё до первой подстановки, без пробелов по краям (удобно для startsWith).
"use strict";
const path = require("path");

const FRONT = path.join(__dirname, "..", "frontend");
const DICTS = { ru: require(path.join(FRONT, "i18n.ru.js")), uz: require(path.join(FRONT, "i18n.uz.js")), en: require(path.join(FRONT, "i18n.en.js")) };

function raw(lang, key) {
  const entry = DICTS[lang][key];
  if (entry === undefined) throw new Error(`нет ключа ${key} в словаре ${lang}`);
  return typeof entry === "string" ? entry : entry.other;
}
function text(lang, key, params) {
  return raw(lang, key).replace(/\{(\w+)\}/g, (m, name) => (params && name in params ? String(params[name]) : m));
}
function prefix(lang, key) {
  return raw(lang, key).split("{")[0].trim();
}

module.exports = { DICTS, text, prefix };
