/* «Ценник» — карточка ответа на экране «Узнать товар». Одна функция рисует все исходы узнавания; данные в разметку не подставляются
   строкой (только textContent), поэтому название товара из базы безопасно. Тексты берутся через deps.t (i18n.js), значки — через Icons.
   Ответ сервера (app/services/identify_service.py): {status: matched | uncertain | unknown | empty_catalog, product, candidates}.
   Необязательные поля товара: category (категория) и fields — [{label, value}], уже переведённые на язык интерфейса (у каждой категории свой набор).
   Ticket.render(document, hostElement, view, deps):
     view = {state, product, candidates, message}; state — idle | searching | matched | uncertain | unknown | empty_catalog | camera_error | error
     deps = {t, onPick(candidate), onAdd(), onRetry(), onPhoto(), onWrong(), onMore(product), maxSpecs}
     Нет deps.onAdd (у человека нет права добавлять товары) — кнопка «Добавить в каталог» не рисуется, подсказка другая. */
(function (root, factory) {
  "use strict";
  var api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Ticket = api;
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";
  var NBSP = "\u00A0";
  var STATES = ["idle", "searching", "matched", "uncertain", "unknown", "empty_catalog", "camera_error", "error"];
  var MAX_SPECS = 4;
  var MAX_CANDIDATES = 3;

  /** 249000 -> «249 000» (неразрывный пробел: число не рвётся на строки). Цена без значения — пустая строка. */
  function formatPrice(value) {
    if (value === null || value === undefined || value === "" || !isFinite(Number(value))) return "";
    var n = Math.round(Number(value));
    return (n < 0 ? "-" : "") + String(Math.abs(n)).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
  }

  function node(doc, tag, cls, text) {
    var el = doc.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }
  function call(fn, arg) { if (typeof fn === "function") fn(arg); }

  function render(doc, host, view, deps) {
    view = view || {};
    deps = deps || {};
    var t = deps.t || function (key) { return key; };
    var Icons = deps.icons || root.Icons;
    var state = STATES.indexOf(view.state) !== -1 ? view.state : "idle";

    function icon(name, size) { return Icons ? Icons.make(doc, name, { size: size || 18 }) : null; }
    function chip(kind, iconName, text) {
      var c = node(doc, "span", "chip chip--" + kind);
      var i = icon(iconName, 16); if (i) c.appendChild(i);
      c.appendChild(node(doc, "span", "", text));
      return c;
    }
    function button(kind, iconName, text, handler) {
      var b = node(doc, "button", "btn btn--" + kind);
      b.setAttribute("type", "button");
      var i = icon(iconName, 20); if (i) b.appendChild(i);
      b.appendChild(node(doc, "span", "", text));
      b.addEventListener("click", function () { handler(); });
      return b;
    }
    function link(text, handler) {
      var b = node(doc, "button", "link", text);
      b.setAttribute("type", "button");
      b.addEventListener("click", function () { handler(); });
      return b;
    }

    while (host.firstChild) host.removeChild(host.firstChild);
    host.setAttribute("data-state", state);
    host.setAttribute("aria-busy", state === "searching" ? "true" : "false");
    var card = node(doc, "article", "ticket ticket--" + state);
    var product = view.product || null;

    if (state === "matched" && product) {
      var head = node(doc, "div", "ticket__head");
      head.appendChild(node(doc, "span", "ticket__brand", product.brand || ""));
      head.appendChild(chip("ok", "check", t("ticket.match")));
      card.appendChild(head);
      card.appendChild(node(doc, "h2", "ticket__name", product.name || ""));
      if (product.category) card.appendChild(node(doc, "p", "ticket__category", product.category));
      var fields = Array.isArray(product.fields) ? product.fields : [];
      if (fields.length) {
        var specs = node(doc, "dl", "ticket__specs");
        fields.slice(0, deps.maxSpecs || MAX_SPECS).forEach(function (f) {
          var row = node(doc, "div", "spec");
          row.appendChild(node(doc, "dt", "", f.label));
          row.appendChild(node(doc, "dd", "", f.value));
          specs.appendChild(row);
        });
        card.appendChild(specs);
        if (fields.length > (deps.maxSpecs || MAX_SPECS)) card.appendChild(link(t("ticket.more"), function () { call(deps.onMore, product); }));
      }
      var priceText = formatPrice(product.expected_price);
      var price = node(doc, "div", priceText ? "ticket__price" : "ticket__price ticket__price--none");
      if (priceText) {
        price.appendChild(node(doc, "span", "ticket__amount", priceText));
        price.appendChild(node(doc, "span", "ticket__unit", t("ticket.price_unit")));
      } else price.appendChild(node(doc, "span", "", t("ticket.no_price")));
      card.appendChild(price);
      var actions = node(doc, "div", "ticket__actions");
      actions.appendChild(link(t("ticket.wrong"), function () { call(deps.onWrong); }));
      card.appendChild(actions);
    } else if (state === "uncertain") {
      card.appendChild(node(doc, "div", "ticket__head")).appendChild(chip("notice", "help", t("ticket.similar")));
      card.appendChild(node(doc, "h2", "ticket__title", t("ticket.pick")));
      var list = node(doc, "ul", "ticket__cands");
      list.setAttribute("role", "list");
      var cands = (Array.isArray(view.candidates) && view.candidates.length ? view.candidates : (product ? [product] : [])).slice(0, MAX_CANDIDATES);
      cands.forEach(function (c, index) {
        var li = node(doc, "li", "");
        var b = node(doc, "button", "cand" + (index === 0 ? " cand--top" : ""));
        b.setAttribute("type", "button");
        var text = node(doc, "span", "cand__text");
        text.appendChild(node(doc, "span", "cand__name", c.name || ""));
        if (c.brand) text.appendChild(node(doc, "span", "cand__brand", c.brand));
        b.appendChild(text);
        b.addEventListener("click", function () { call(deps.onPick, c); });
        li.appendChild(b);
        list.appendChild(li);
      });
      card.appendChild(list);
    } else if (state === "unknown") {
      card.appendChild(node(doc, "div", "ticket__head")).appendChild(chip("danger", "search-off", t("ticket.not_found")));
      card.appendChild(node(doc, "h2", "ticket__title", t("ticket.not_found_title")));
      var canAdd = typeof deps.onAdd === "function";            // нет права добавлять товары — кнопки нет, а подсказка говорит, к кому обратиться
      card.appendChild(node(doc, "p", "ticket__hint", t(canAdd ? "ticket.not_found_hint" : "ticket.not_found_hint_readonly")));
      var a1 = node(doc, "div", "ticket__actions");
      if (canAdd) a1.appendChild(button("primary", "plus", t("identify.add_product"), function () { call(deps.onAdd); }));
      a1.appendChild(button(canAdd ? "secondary" : "primary", "camera", t("ticket.retake"), function () { call(deps.onRetry); }));
      card.appendChild(a1);
    } else if (state === "empty_catalog") {
      card.appendChild(node(doc, "h2", "ticket__title", t("ticket.empty_title")));
      var canAddFirst = typeof deps.onAdd === "function";
      card.appendChild(node(doc, "p", "ticket__hint", t(canAddFirst ? "ticket.empty_hint" : "ticket.empty_hint_readonly")));
      if (canAddFirst) {
        var a2 = node(doc, "div", "ticket__actions");
        a2.appendChild(button("primary", "plus", t("identify.add_product"), function () { call(deps.onAdd); }));
        card.appendChild(a2);
      }
    } else if (state === "searching") {
      card.appendChild(node(doc, "p", "ticket__hint", t("identify.searching")));
      var sk = node(doc, "div", "ticket__sk");
      sk.setAttribute("aria-hidden", "true");
      ["skeleton sk-60", "skeleton sk-100", "skeleton sk-80"].forEach(function (c) { sk.appendChild(node(doc, "div", c)); });
      card.appendChild(sk);
    } else if (state === "camera_error" || state === "error") {
      var isCam = state === "camera_error";
      card.appendChild(node(doc, "div", "ticket__head")).appendChild(chip("danger", "alert", t(isCam ? "ticket.camera_title" : "ticket.error_title")));
      card.appendChild(node(doc, "p", "ticket__hint", view.message || t(isCam ? "error.camera_denied" : "error.generic")));
      var a3 = node(doc, "div", "ticket__actions");
      if (isCam) a3.appendChild(button("primary", "camera", t("identify.take_photo"), function () { call(deps.onPhoto); }));
      a3.appendChild(button(isCam ? "secondary" : "primary", "refresh", t("common.retry"), function () { call(deps.onRetry); }));
      card.appendChild(a3);
    } else {                                                  // idle (и matched без товара: показываем приглашение, а не пустую карточку)
      var glyph = node(doc, "div", "ticket__glyph");
      var g = icon("viewfinder", 28); if (g) glyph.appendChild(g);
      card.appendChild(glyph);
      card.appendChild(node(doc, "h2", "ticket__title", t("identify.show_to_camera")));
      card.appendChild(node(doc, "p", "ticket__hint", t("ticket.idle_hint")));
    }
    host.appendChild(card);
    return card;
  }

  return { render: render, formatPrice: formatPrice, STATES: STATES };
});
