/* Главная (редизайн 2026-11-15): сама ничего не показывает — ведёт туда, где работают. Не вошли — на страницу входа; вошли — на первую
   страницу по правам (Login.startPage: товары, управление или «Узнать товар»). Адрес программы без пути (/) открывает эту страницу. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Home = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  function start(win, deps) {
    deps = deps || {};
    var Auth = deps.Auth || win.Auth, Login = deps.Login || win.Login, I18N = deps.I18N || win.I18N;
    if (I18N && I18N.apply) I18N.apply();
    var auth = Auth.create(win);
    if (!auth.getToken()) { win.location.replace("login.html"); return Promise.resolve("login.html"); }
    return auth.me().then(function (r) {
      var target = r.status === "ok" ? Login.startPage(r.me.permissions) : "login.html";
      win.location.replace(target);
      return target;
    });
  }
  return { start: start };
});
