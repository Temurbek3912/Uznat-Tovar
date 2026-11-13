/* Тема оформления: «как в системе» (по умолчанию) / светлая / тёмная. Выбор запоминается (localStorage, запас — память страницы).
   Применяется атрибутом <html data-theme="light|dark">; «как в системе» — атрибута нет, решает настройка устройства (tokens.css).
   Подключение: <script src="theme.js"></script>, затем Theme.apply() как можно раньше, чтобы страница не мигала не той темой. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Theme = api.create({
    get localStorage() { return root.localStorage; },
    document: root.document,
    matchMedia: root.matchMedia ? root.matchMedia.bind(root) : null
  });
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  var KEY = "app_theme";
  var MODES = ["system", "light", "dark"];

  function create(env) {
    env = env || {};
    var current = null, memory = null, listeners = [];

    function readStored() {
      try { var v = env.localStorage ? env.localStorage.getItem(KEY) : null; return v == null ? memory : v; } catch (e) { return memory; }
    }
    function writeStored(mode) {
      memory = mode;
      try { if (env.localStorage) env.localStorage.setItem(KEY, mode); } catch (e) { /* без записи на диск — до закрытия страницы */ }
    }
    function get() {
      if (!current) current = MODES.indexOf(readStored()) !== -1 ? readStored() : "system";
      return current;
    }
    /** Какая тема реально видна сейчас: light или dark. */
    function effective() {
      var mode = get();
      if (mode !== "system") return mode;
      try { return env.matchMedia && env.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"; } catch (e) { return "light"; }
    }
    function apply() {
      var el = env.document && env.document.documentElement;
      if (!el) return;
      var mode = get();
      if (mode === "system") { if (el.removeAttribute) el.removeAttribute("data-theme"); }
      else el.setAttribute("data-theme", mode);
    }
    function set(mode) {
      if (MODES.indexOf(mode) === -1) return false;
      current = mode; writeStored(mode); apply();
      listeners.slice().forEach(function (fn) { try { fn(mode); } catch (e) { /* слушатель не должен ломать остальных */ } });
      return true;
    }
    /** Переключение по кругу: как в системе → светлая → тёмная → как в системе. */
    function next() { return set(MODES[(MODES.indexOf(get()) + 1) % MODES.length]); }
    function onChange(fn) { listeners.push(fn); return function () { var i = listeners.indexOf(fn); if (i !== -1) listeners.splice(i, 1); }; }

    return { get: get, set: set, next: next, apply: apply, effective: effective, onChange: onChange, modes: MODES.slice() };
  }
  return { create: create, MODES: MODES, KEY: KEY };
});
