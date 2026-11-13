/* Язык интерфейса: русский / узбекский (латиница) / узбекский (кириллица) / английский.
   Коды: "ru", "uz" (= uz-Latn, латиница; код прежних версий сохранён), "uz-Cyrl", "en".
   Что делает: запоминает выбор человека (localStorage; если браузер его не даёт — хотя бы до закрытия страницы),
   при первом запуске берёт язык браузера, переводит строки по ключам, склоняет «1 товар / 2 товара / 5 товаров»,
   проставляет <html lang> и рисует переключатель языков.
   Чужие данные в разметку строкой не подставляются: только textContent / setAttribute (проверяет tools/i18n_smoke_test.js).
   Словари лежат отдельно (i18n.ru.js, i18n.uz.js, i18n.uz-cyrl.js, i18n.en.js): их удобно отдавать на проверку носителю языка.
   i18n.uz-cyrl.js генерируется из i18n.uz.js (tools/uz_translit.py) — черновик для вычитки носителем.
   Подключение: <script src="i18n.js"></script><script src="i18n.ru.js"></script>… затем I18N.apply(). */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.I18N = api.create({
    get localStorage() { return root.localStorage; },   // геттер: в закрытых режимах браузер бросает ошибку при обращении
    navigator: root.navigator,
    document: root.document
  });
  if (typeof window !== "undefined") root.I18N.create = api.create;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  var STORAGE_KEY = "app_lang";
  var LANGS = ["ru", "uz", "uz-Cyrl", "en"];
  var DEFAULT_LANG = "ru";
  // Название языка всегда на самом языке: человек, не понимающий текущий язык, всё равно найдёт свой.
  var NAMES = { ru: "Русский", uz: "O\u02BBzbekcha", "uz-Cyrl": "\u040E\u0437\u0431\u0435\u043A\u0447\u0430", en: "English" };
  // Короткая подпись кнопки на узком экране.
  var SHORT = { ru: "RU", uz: "UZ", "uz-Cyrl": "\u040E\u0417", en: "EN" };
  // Если строки нет на языке человека: узбекскому читателю русский понятнее английского, остальным — наоборот.
  // Кириллице сначала — та же строка латиницей (тот же язык), и только потом русский.
  var FALLBACKS = { ru: ["en"], uz: ["ru", "en"], "uz-Cyrl": ["uz", "ru", "en"], en: ["ru"] };

  // "uz", "uz-UZ", "uz-Latn" -> "uz"; "uz-Cyrl", "uz_cyrl_UZ" -> "uz-Cyrl"; "ru-RU" -> "ru"; неизвестное -> null.
  function normalize(code) {
    if (typeof code !== "string") return null;
    var parts = code.trim().toLowerCase().split(/[-_]/);
    var base = parts[0];
    if (base === "uz") return parts.indexOf("cyrl") !== -1 ? "uz-Cyrl" : "uz";
    return base !== "uz" && LANGS.indexOf(base) !== -1 ? base : null;
  }

  function pluralCategory(lang, n) {
    n = Number(n);
    if (!isFinite(n)) return "other";
    try { return new Intl.PluralRules(lang).select(n); } catch (e) { /* старый движок: простые правила ниже */ }
    if (lang === "ru") {
      var m10 = Math.abs(n) % 10, m100 = Math.abs(n) % 100;
      if (m10 === 1 && m100 !== 11) return "one";
      if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return "few";
      return "many";
    }
    if (lang === "uz" || lang === "uz-Cyrl") return "other";
    return n === 1 ? "one" : "other";
  }

  function format(text, params) {
    if (!params) return text;
    return String(text).replace(/\{(\w+)\}/g, function (whole, name) {
      return Object.prototype.hasOwnProperty.call(params, name) && params[name] != null ? String(params[name]) : whole;
    });
  }

  function each(list, fn) { if (list) for (var i = 0; i < list.length; i++) fn(list[i]); }

  /** env: { localStorage, navigator, document } — всё необязательно; так модуль проверяется в Node без браузера. */
  function create(env) {
    env = env || {};
    var dicts = { ru: {}, uz: {}, "uz-Cyrl": {}, en: {} };
    var listeners = [];
    var current = null;
    var memory = null;

    function readStored() {
      try {
        var v = env.localStorage ? env.localStorage.getItem(STORAGE_KEY) : null;
        return v == null ? memory : v;
      } catch (e) { return memory; }
    }
    function writeStored(code) {
      memory = code;
      try { if (env.localStorage) env.localStorage.setItem(STORAGE_KEY, code); } catch (e) { /* без запоминания на диск — до закрытия страницы */ }
    }

    // Порядок: сохранённый выбор -> языки браузера по порядку -> русский. Определённый «сам» язык на диск НЕ пишем:
    // запоминается только то, что человек выбрал руками.
    function detect() {
      var saved = normalize(readStored());
      if (saved) return saved;
      var nav = env.navigator || {};
      var list = nav.languages && nav.languages.length ? nav.languages : [nav.language];
      for (var i = 0; i < list.length; i++) {
        var found = normalize(list[i]);
        if (found) return found;
      }
      return DEFAULT_LANG;
    }

    function getLang() { if (!current) current = detect(); return current; }

    function lookup(lang, key) {
      var d = dicts[lang];
      return d && Object.prototype.hasOwnProperty.call(d, key) ? d[key] : undefined;
    }

    /** Есть ли перевод ключа хоть на одном языке цепочки (для сообщений сервера: нет ключа — показать текст сервера). */
    function has(key, langOverride) {
      var lang = normalize(langOverride) || getLang();
      var chain = [lang].concat(FALLBACKS[lang] || []);
      for (var i = 0; i < chain.length; i++) if (lookup(chain[i], key) !== undefined) return true;
      return false;
    }

    // Параметр вида {key, params} — вложенное сообщение (причина от сервера: «Фото 2: <причина>»): переводится тем же языком.
    function resolveParams(params, lang, depth) {
      if (!params || typeof params !== "object") return params;
      var out = {};
      for (var name in params) {
        if (!Object.prototype.hasOwnProperty.call(params, name)) continue;
        out[name] = resolveValue(params[name], lang, depth);
      }
      return out;
    }

    /** Значение параметра: вложенная причина {key, params} переводится; список (причины запрета удаления) — каждый элемент
        переводится и соединяется через «; ». Глубина ограничена всей цепочкой — зацикленные данные не вешают страницу. */
    function resolveValue(v, lang, depth) {
      if (Array.isArray(v)) {
        if (depth >= 3) return "";
        return v.map(function (item) { var r = resolveValue(item, lang, depth + 1); return r === null || r === undefined ? "" : String(r); })
          .filter(function (s) { return s !== ""; }).join("; ");
      }
      if (v && typeof v === "object" && typeof v.key === "string" && depth < 3) return translate(v.key, resolveParams(v.params, lang, depth + 1), lang);
      return v;
    }

    /** Перевод по ключу. params.n выбирает форму множественного числа. Нет ключа нигде — возвращается сам ключ (видно, чего не хватает). */
    function t(key, params, langOverride) {
      var lang = normalize(langOverride) || getLang();
      return translate(key, resolveParams(params, lang, 0), lang);
    }

    function translate(key, params, lang) {
      var chain = [lang].concat(FALLBACKS[lang] || []);
      for (var i = 0; i < chain.length; i++) {
        var value = lookup(chain[i], key);
        if (value === undefined) continue;
        if (value !== null && typeof value === "object") {
          value = value[pluralCategory(chain[i], params && params.n)] || value.other;
          if (value === undefined) continue;
        }
        return format(value, params);
      }
      return key;
    }

    function add(lang, entries) {
      var code = normalize(lang);
      if (!code || !entries) return;
      for (var k in entries) if (Object.prototype.hasOwnProperty.call(entries, k)) dicts[code][k] = entries[k];
    }

    function apply(scope) {
      var doc = env.document;
      if (!doc) return;
      var lang = getLang();
      if (doc.documentElement && doc.documentElement.setAttribute) doc.documentElement.setAttribute("lang", lang);
      var area = scope || doc;
      if (!area.querySelectorAll) return;
      each(area.querySelectorAll("[data-i18n]"), function (el) { el.textContent = t(el.getAttribute("data-i18n")); });
      // data-i18n-attr="placeholder:key1;aria-label:key2"
      each(area.querySelectorAll("[data-i18n-attr]"), function (el) {
        String(el.getAttribute("data-i18n-attr")).split(";").forEach(function (pair) {
          var cut = pair.indexOf(":");
          if (cut > 0) el.setAttribute(pair.slice(0, cut).trim(), t(pair.slice(cut + 1).trim()));
        });
      });
    }

    function notify(lang) {
      listeners.slice().forEach(function (fn) { try { fn(lang); } catch (e) { /* один сломанный слушатель не должен мешать остальным */ } });
    }

    function onChange(fn) {
      listeners.push(fn);
      return function () { var i = listeners.indexOf(fn); if (i !== -1) listeners.splice(i, 1); };
    }

    /** Выбор человека: запоминается всегда (даже если совпал с определённым автоматически). false — такого языка нет. */
    function setLang(code) {
      var lang = normalize(code);
      if (!lang) return false;
      var changed = lang !== getLang();
      writeStored(lang);
      current = lang;
      apply();
      if (changed) notify(lang);
      return true;
    }

    /** Переключатель из четырёх кнопок. Строится через DOM-методы; aria-pressed показывает выбранный язык. */
    function mountSwitcher(container) {
      var doc = env.document;
      if (!doc || !container) return null;
      var group = doc.createElement("div");
      group.className = "lang-switch";
      group.setAttribute("role", "group");
      var buttons = LANGS.map(function (code) {
        var b = doc.createElement("button");
        b.setAttribute("type", "button");
        b.className = "lang-btn";
        b.setAttribute("data-lang", code);
        b.setAttribute("lang", code);
        b.setAttribute("data-short", SHORT[code]);      // на узком экране кнопка показывает «RU», полное название остаётся для чтения с экрана
        b.textContent = NAMES[code];
        b.addEventListener("click", function () { setLang(code); });
        group.appendChild(b);
        return b;
      });
      function refresh() {
        group.setAttribute("aria-label", t("lang.title"));
        buttons.forEach(function (b) { b.setAttribute("aria-pressed", b.getAttribute("data-lang") === getLang() ? "true" : "false"); });
      }
      refresh();
      onChange(refresh);
      container.appendChild(group);
      return group;
    }

    return {
      t: t, has: has, add: add, apply: apply, getLang: getLang, setLang: setLang, onChange: onChange, mountSwitcher: mountSwitcher,
      langs: LANGS.slice(), names: NAMES, pluralCategory: pluralCategory
    };
  }

  return { create: create, LANGS: LANGS, NAMES: NAMES, DEFAULT_LANG: DEFAULT_LANG, STORAGE_KEY: STORAGE_KEY, normalize: normalize };
});
