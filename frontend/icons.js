/* Значки интерфейса: свои векторные вместо эмодзи (эмодзи выглядят по-разному на каждом устройстве).
   Контуры в стиле набора Feather (лицензия MIT, © Cole Bemis); линия 1.75, цвет берётся от текста (currentColor), размер — от шрифта.
   Icons.make(document, "check", {size: 20, label: "Готово"}) -> элемент <svg>. Без label значок декоративный (aria-hidden).
   Неизвестное имя даёт пустой значок, а не ошибку: интерфейс не должен падать из-за картинки. Строится только через DOM-методы. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Icons = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var L = function (x1, y1, x2, y2) { return ["line", { x1: x1, y1: y1, x2: x2, y2: y2 }]; };
  var P = function (d) { return ["path", { d: d }]; };
  var C = function (cx, cy, r) { return ["circle", { cx: cx, cy: cy, r: r }]; };

  var SHAPES = {
    viewfinder: [P("M3 8V5a2 2 0 0 1 2-2h3"), P("M16 3h3a2 2 0 0 1 2 2v3"), P("M21 16v3a2 2 0 0 1-2 2h-3"), P("M8 21H5a2 2 0 0 1-2-2v-3"), C(12, 12, 2.5)],
    list: [L(8, 6, 21, 6), L(8, 12, 21, 12), L(8, 18, 21, 18), L(3, 6, 3.01, 6), L(3, 12, 3.01, 12), L(3, 18, 3.01, 18)],
    sliders: [L(4, 21, 4, 14), L(4, 10, 4, 3), L(12, 21, 12, 12), L(12, 8, 12, 3), L(20, 21, 20, 16), L(20, 12, 20, 3), L(1, 14, 7, 14), L(9, 8, 15, 8), L(17, 16, 23, 16)],
    camera: [P("M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"), C(12, 13, 4)],
    refresh: [["polyline", { points: "23 4 23 10 17 10" }], ["polyline", { points: "1 20 1 14 7 14" }], P("M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15")],
    check: [["polyline", { points: "20 6 9 17 4 12" }]],
    help: [C(12, 12, 10), P("M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"), L(12, 17, 12.01, 17)],
    "search-off": [C(11, 11, 8), L(21, 21, 16.65, 16.65), L(8.5, 8.5, 13.5, 13.5), L(13.5, 8.5, 8.5, 13.5)],
    plus: [L(12, 5, 12, 19), L(5, 12, 19, 12)],
    x: [L(18, 6, 6, 18), L(6, 6, 18, 18)],
    alert: [P("M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"), L(12, 9, 12, 13), L(12, 17, 12.01, 17)],
    globe: [C(12, 12, 10), L(2, 12, 22, 12), P("M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z")],
    sun: [C(12, 12, 5), L(12, 1, 12, 3), L(12, 21, 12, 23), L(4.22, 4.22, 5.64, 5.64), L(18.36, 18.36, 19.78, 19.78), L(1, 12, 3, 12), L(21, 12, 23, 12), L(4.22, 19.78, 5.64, 18.36), L(18.36, 5.64, 19.78, 4.22)],
    moon: [P("M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z")],
    chevron: [["polyline", { points: "6 9 12 15 18 9" }]],
    monitor: [["rect", { x: 2, y: 3, width: 20, height: 14, rx: 2, ry: 2 }], L(8, 21, 16, 21), L(12, 17, 12, 21)],
    // редизайн 2026-11-15: меню управления
    card: [["rect", { x: 3, y: 4, width: 18, height: 16, rx: 2, ry: 2 }], L(7, 9, 17, 9), L(7, 13, 14, 13), L(7, 17, 11, 17)],
    users: [P("M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"), C(9, 7, 4), P("M23 21v-2a4 4 0 0 0-3-3.87"), P("M16 3.13a4 4 0 0 1 0 7.75")],
    activity: [["polyline", { points: "22 12 18 12 15 21 9 3 6 12 2 12" }]],
    "log-out": [P("M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"), ["polyline", { points: "16 17 21 12 16 7" }], L(21, 12, 9, 12)],
    search: [C(11, 11, 8), L(21, 21, 16.65, 16.65)],
    lock: [["rect", { x: 3, y: 11, width: 18, height: 11, rx: 2, ry: 2 }], P("M7 11V7a5 5 0 0 1 10 0v4")]
  };

  function has(name) { return Object.prototype.hasOwnProperty.call(SHAPES, name); }
  function names() { return Object.keys(SHAPES); }

  function make(doc, name, opts) {
    opts = opts || {};
    var svg = doc.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", String(opts.size || 20));
    svg.setAttribute("height", String(opts.size || 20));
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.75");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("class", "icon icon-" + name);
    if (opts.label) { svg.setAttribute("role", "img"); svg.setAttribute("aria-label", opts.label); }
    else svg.setAttribute("aria-hidden", "true");
    (has(name) ? SHAPES[name] : []).forEach(function (shape) {
      var el = doc.createElementNS(NS, shape[0]);
      for (var k in shape[1]) if (Object.prototype.hasOwnProperty.call(shape[1], k)) el.setAttribute(k, String(shape[1][k]));
      svg.appendChild(el);
    });
    return svg;
  }

  return { make: make, has: has, names: names, SHAPES: SHAPES, NS: NS };
});
