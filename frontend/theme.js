/* Тема оформления: светлая / тёмная (два режима — владелец: «достаточно тёмного и светлого»). Пока человек сам не выбрал,
   берётся та, что включена на устройстве. Выбор запоминается (localStorage, запас — память страницы).
   Применяется атрибутом <html data-theme="light|dark">. Прежнее сохранённое «system» считается «не выбрано».
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
  var MODES = ["light", "dark"];

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
    function device() {
      try { return env.matchMedia && env.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"; } catch (e) { return "light"; }
    }
    function get() {
      if (!current) { var s = readStored(); current = MODES.indexOf(s) !== -1 ? s : device(); }
      return current;
    }
    /** Какая тема видна сейчас: light или dark (то же, что get). */
    function effective() { return get(); }
    function apply() {
      var el = env.document && env.document.documentElement;
      if (el) el.setAttribute("data-theme", get());
    }
    function set(mode) {
      if (MODES.indexOf(mode) === -1) return false;
      current = mode; writeStored(mode); apply();
      listeners.slice().forEach(function (fn) { try { fn(mode); } catch (e) { /* слушатель не должен ломать остальных */ } });
      return true;
    }
    /** Переключение: светлая <-> тёмная. */
    function next() { return set(get() === "dark" ? "light" : "dark"); }
    function onChange(fn) { listeners.push(fn); return function () { var i = listeners.indexOf(fn); if (i !== -1) listeners.splice(i, 1); }; }

    return { get: get, set: set, next: next, apply: apply, effective: effective, onChange: onChange, modes: MODES.slice() };
  }
  return { create: create, MODES: MODES, KEY: KEY };
});
