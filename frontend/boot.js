/* Запуск модуля страницы: <script src="boot.js" data-start="Identify"></script> вызывает Identify.start(window).
   Отдельным файлом, а не встроенным <script>: политика безопасности страниц (CSP script-src 'self') встроенные скрипты запрещает.
   Имя модуля берётся только из атрибута своего же тега и должно быть одним словом; неизвестный модуль — ошибка в консоли. */
(function (w) {
  "use strict";
  var tag = w.document && w.document.currentScript;
  var name = tag ? tag.getAttribute("data-start") : null;
  if (!name || !/^[A-Z][A-Za-z]*$/.test(name)) throw new Error("boot.js: не указан модуль страницы (data-start)");
  var mod = w[name];
  if (!mod || typeof mod.start !== "function") throw new Error("boot.js: модуль " + name + " не загружен");
  mod.start(w);
})(window);
