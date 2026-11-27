/* Экран «Товары»: список с поиском, фильтрами (категория, бренд, цена, «с фото / ценой / штрих-кодом» и «без …»), группировкой и сортировкой.
   Логика — catalog.js; здесь только отрисовка. Всё из базы попадает на страницу через textContent. На телефоне панель фильтров открывается
   поверх списка («Фильтры (N)»), на ноутбуке стоит слева. Группы сворачиваются. Цена в полях вводится числом, применяется по Enter/уходу из поля.
   ProductList.create(document, hostElement, {t, onOpen(product), photoUrl(product), loadThumb(product), getLang()}) -> {setItems, refresh, setState, state}.
   photoUrl — готовый адрес картинки (если открывается без авторизации). loadThumb — когда фото отдаётся только с заголовком авторизации: функция вернёт обещание
   с адресом (blob:) или null; зовётся ЛЕНИВО — когда строка подъехала к экрану (IntersectionObserver; без него — сразу), только для товаров с photo_count > 0. */
(function (root, factory) {
  "use strict";
  var api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ProductList = api;
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";
  var Catalog = root.Catalog || (typeof require === "function" ? require("./catalog.js") : null);
  var Ticket = root.Ticket || (typeof require === "function" ? require("./ticket.js") : null);
  var SORTS = ["new", "name", "price_asc", "price_desc"];
  var GROUPS = ["none", "category", "brand"];
  // «без …»: [поле, подпись, противоположное поле «только с …»]
  var MISSING = [["missingPrice", "filters.no_price", "onlyWithPrice"], ["missingPhoto", "filters.no_photo", "onlyWithPhoto"], ["missingBarcode", "filters.no_barcode", "onlyWithBarcode"]];

  function node(doc, tag, cls, text) {
    var el = doc.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

  function create(doc, host, deps) {
    deps = deps || {};
    var t = deps.t || function (k) { return k; };
    var Icons = deps.icons || root.Icons;
    var lang = function () { return deps.getLang ? deps.getLang() : "ru"; };
    var state = { items: [], filters: Catalog.emptyFilters(), sort: "new", group: "none", collapsed: {}, filtersOpen: false };
    var el = {};                                              // ссылки на части страницы

    function icon(name, size) { return Icons ? Icons.make(doc, name, { size: size || 18 }) : null; }
    function catName(v) { var I = root.I18N; return I && I.category ? I.category(v) : v; }   // «Сладости» -> на языке человека
    function labelOf(kind, value) {
      if (!value) return t(kind === "category" ? "list.no_category" : "list.no_brand");
      return kind === "category" ? catName(value) : value;
    }

    function select(id, labelKey, values, keyOf, current, onChange) {
      var wrap = node(doc, "label", "field field--select");
      wrap.appendChild(node(doc, "span", "field__label", t(labelKey)));
      var s = node(doc, "select", "select");
      s.setAttribute("id", id);
      values.forEach(function (v) {
        var o = node(doc, "option", "", t(keyOf + v));
        o.setAttribute("value", v);
        if (v === current) o.setAttribute("selected", "selected");
        s.appendChild(o);
      });
      s.value = current;
      s.addEventListener("change", function () { onChange(s.value); });
      wrap.appendChild(s);
      return wrap;
    }

    function checkbox(text, count, checked, onChange, key) {
      var row = node(doc, "label", "check");
      var box = node(doc, "input", "check__box");
      box.setAttribute("type", "checkbox");
      box.setAttribute("data-key", key);
      if (checked) box.setAttribute("checked", "checked");
      box.checked = !!checked;
      box.addEventListener("change", function () { onChange(box.checked); });
      row.appendChild(box);
      row.appendChild(node(doc, "span", "check__text", text));
      if (count !== null && count !== undefined) row.appendChild(node(doc, "span", "check__n", count));
      return row;
    }

    function toggleIn(list, value, on) {
      var next = list.filter(function (v) { return v !== value; });
      if (on) next.push(value);
      return next;
    }

    function priceInput(id, placeholder, value, onCommit) {
      var i = node(doc, "input", "input");
      i.setAttribute("id", id);
      i.setAttribute("data-key", id);
      i.setAttribute("type", "number");
      i.setAttribute("inputmode", "numeric");
      i.setAttribute("min", "0");
      i.setAttribute("aria-label", placeholder);
      i.setAttribute("placeholder", placeholder);
      i.value = value === null || value === undefined ? "" : String(value);
      i.addEventListener("change", function () { onCommit(i.value === "" ? null : Number(i.value)); });
      return i;
    }

    function renderFilters(facets) {
      var f = state.filters, panel = el.filters;
      clear(panel);
      var head = node(doc, "div", "filters__head");
      head.appendChild(node(doc, "h2", "filters__title", t("filters.title")));
      var reset = node(doc, "button", "link", t("filters.reset"));
      reset.setAttribute("type", "button");
      reset.addEventListener("click", function () { var q = state.filters.q; state.filters = Catalog.emptyFilters(); state.filters.q = q; refreshBody(); });   // фильтры сбрасываются, запрос остаётся
      head.appendChild(reset);
      panel.appendChild(head);

      [["category", "categories", "filters.category"], ["brand", "brands", "filters.brand"]].forEach(function (g) {
        var options = facets[g[1]].slice();
        f[g[1]].forEach(function (v) { if (!options.some(function (o) { return o.value === v; })) options.push({ value: v, count: 0 }); });   // выбранный вариант не пропадает, даже если стал пустым
        if (!options.length) return;
        var set = node(doc, "fieldset", "filters__group");
        set.appendChild(node(doc, "legend", "filters__legend", t(g[2])));
        options.forEach(function (o) {
          set.appendChild(checkbox(labelOf(g[0], o.value), o.count, f[g[1]].indexOf(o.value) !== -1, function (on) {
            state.filters[g[1]] = toggleIn(state.filters[g[1]], o.value, on); refreshBody();
          }, g[1] + ":" + o.value));
        });
        panel.appendChild(set);
      });

      var price = node(doc, "fieldset", "filters__group");
      price.appendChild(node(doc, "legend", "filters__legend", t("filters.price")));
      var pair = node(doc, "div", "filters__pair");
      pair.appendChild(priceInput("priceMin", t("filters.price_from"), f.priceMin, function (v) { state.filters.priceMin = v; refreshBody(); }));
      pair.appendChild(priceInput("priceMax", t("filters.price_to"), f.priceMax, function (v) { state.filters.priceMax = v; refreshBody(); }));
      price.appendChild(pair);
      if (facets.price) price.appendChild(node(doc, "p", "filters__range", Ticket.formatPrice(facets.price.min) + " \u2013 " + Ticket.formatPrice(facets.price.max) + "\u00A0" + t("ticket.price_unit")));
      panel.appendChild(price);

      var only = node(doc, "fieldset", "filters__group");
      only.appendChild(node(doc, "legend", "filters__legend", t("filters.only")));
      [["onlyWithPrice", "filters.with_price"], ["onlyWithPhoto", "filters.with_photo"], ["onlyWithBarcode", "filters.with_barcode"]].forEach(function (o) {
        only.appendChild(checkbox(t(o[1]), null, f[o[0]], function (on) {
          state.filters[o[0]] = on;
          MISSING.forEach(function (m) { if (on && m[2] === o[0]) state.filters[m[0]] = false; });
          refreshBody();
        }, "only:" + o[0]));
      });
      panel.appendChild(only);

      var missing = node(doc, "fieldset", "filters__group");     // «без …»: противоположен «только с …» — включить одно значит выключить другое
      missing.appendChild(node(doc, "legend", "filters__legend", t("filters.missing")));
      MISSING.forEach(function (o) {
        missing.appendChild(checkbox(t(o[1]), null, f[o[0]], function (on) {
          state.filters[o[0]] = on;
          if (on) state.filters[o[2]] = false;
          refreshBody();
        }, "missing:" + o[0]));
      });
      panel.appendChild(missing);

      var show = node(doc, "button", "btn btn--primary filters__show", t("filters.show", { count: facets.total }));
      show.setAttribute("type", "button");
      show.addEventListener("click", function () { state.filtersOpen = false; syncOpen(); });
      panel.appendChild(show);
    }

    var observer = null;                                    // следит за миниатюрами, которые ещё не подъехали к экрану
    function putImage(thumb, url) {
      clear(thumb);
      var img = node(doc, "img", "");
      img.setAttribute("src", url); img.setAttribute("alt", ""); img.setAttribute("loading", "lazy");
      thumb.appendChild(img);
    }
    /* Есть ли миниатюра: фото у товара есть, и, если фото учтены в базе (reference_count не null), сервер выбрал витринное
       (cover_image_id). Все фото выведены из эталонов — миниатюры нет, остаётся буква, лишнего запроса с ответом 404 нет. */
    function hasThumb(p) {
      if (!(p.photo_count > 0)) return false;
      return p.reference_count === null || p.reference_count === undefined || Boolean(p.cover_image_id);
    }

    function lazyThumb(thumb, p) {
      var go = function () {
        var pending;
        try { pending = deps.loadThumb(p); } catch (e) { return; }
        if (pending && typeof pending.then === "function") pending.then(function (url) { if (url) putImage(thumb, url); }, function () { /* без миниатюры остаётся буква */ });
      };
      var Observer = root.IntersectionObserver;
      if (!Observer) { go(); return; }
      if (!observer) observer = new Observer(function (entries) {
        entries.forEach(function (entry) { if (entry.isIntersecting) { observer.unobserve(entry.target); if (entry.target._thumbGo) entry.target._thumbGo(); } });
      }, { rootMargin: "240px" });
      thumb._thumbGo = go;
      observer.observe(thumb);
    }

    function row(p) {
      var li = node(doc, "li", "");
      var b = node(doc, "button", "prow");
      b.setAttribute("type", "button");
      var thumb = node(doc, "span", "prow__thumb");
      var url = deps.photoUrl ? deps.photoUrl(p) : null;
      if (url) putImage(thumb, url);
      else {
        thumb.textContent = String(p.name || "?").trim().charAt(0).toUpperCase();
        if (deps.loadThumb && hasThumb(p)) lazyThumb(thumb, p);
      }
      b.appendChild(thumb);
      var text = node(doc, "span", "prow__text");
      text.appendChild(node(doc, "span", "prow__name", p.name || ""));
      text.appendChild(node(doc, "span", "prow__meta", [p.brand, p.category ? catName(p.category) : ""].filter(Boolean).join(" · ") || p.sku || ""));
      b.appendChild(text);
      var price = Ticket.formatPrice(p.expected_price);
      var amount = node(doc, "span", price ? "prow__price" : "prow__price prow__price--none");
      amount.textContent = price ? price + "\u00A0" + t("ticket.price_unit") : "\u2014";
      b.appendChild(amount);
      b.addEventListener("click", function () { if (deps.onOpen) deps.onOpen(p); });
      li.appendChild(b);
      return li;
    }

    function renderResults(shown) {
      var box = el.results;
      if (observer) observer.disconnect();                  // старые строки уходят из списка — следить за ними не нужно
      clear(box);
      var f = state.filters, active = [];
      var summary = node(doc, "div", "plist__summary");
      summary.appendChild(node(doc, "p", "plist__found", t("list.found", { count: t("items.count", { n: shown.length }) })));
      var chips = node(doc, "div", "plist__chips");
      f.categories.forEach(function (v) { active.push([labelOf("category", v), function () { state.filters.categories = toggleIn(f.categories, v, false); }]); });
      f.brands.forEach(function (v) { active.push([labelOf("brand", v), function () { state.filters.brands = toggleIn(f.brands, v, false); }]); });
      if (f.priceMin !== null || f.priceMax !== null) active.push([t("filters.price") + ": " + (f.priceMin !== null ? Ticket.formatPrice(f.priceMin) : "…") + "–" + (f.priceMax !== null ? Ticket.formatPrice(f.priceMax) : "…"), function () { state.filters.priceMin = null; state.filters.priceMax = null; }]);
      [["onlyWithPrice", "filters.with_price"], ["onlyWithPhoto", "filters.with_photo"], ["onlyWithBarcode", "filters.with_barcode"]].forEach(function (o) {
        if (f[o[0]]) active.push([t(o[1]), function () { state.filters[o[0]] = false; }]);
      });
      MISSING.forEach(function (o) {
        if (f[o[0]]) active.push([t(o[1]), function () { state.filters[o[0]] = false; }]);
      });
      active.forEach(function (a) {
        var chip = node(doc, "button", "chip chip--filter");
        chip.setAttribute("type", "button");
        chip.appendChild(node(doc, "span", "", a[0]));
        var x = icon("x", 14); if (x) chip.appendChild(x);
        chip.addEventListener("click", function () { a[1](); refreshBody(); });
        chips.appendChild(chip);
      });
      if (active.length) summary.appendChild(chips);
      box.appendChild(summary);

      if (!shown.length) {
        var empty = node(doc, "div", "plist__empty");
        empty.appendChild(node(doc, "h2", "ticket__title", t("list.empty_title")));
        empty.appendChild(node(doc, "p", "ticket__hint", t("list.empty_hint")));
        var again = node(doc, "button", "btn btn--secondary", t("filters.reset"));
        again.setAttribute("type", "button");
        again.addEventListener("click", function () { state.filters = Catalog.emptyFilters(); if (el.search) el.search.value = ""; refreshBody(); });
        empty.appendChild(again);
        box.appendChild(empty);
        return;
      }
      Catalog.groupProducts(Catalog.sortProducts(shown, state.sort, lang()), state.group, lang()).forEach(function (g) {
        var section = node(doc, "section", "group");
        var ul = node(doc, "ul", "group__list");
        ul.setAttribute("role", "list");
        if (state.group !== "none") {
          var collapsed = !!state.collapsed[state.group + ":" + g.key];
          var h = node(doc, "button", "group__head");
          h.setAttribute("type", "button");
          h.setAttribute("aria-expanded", collapsed ? "false" : "true");
          h.setAttribute("aria-label", t("list.toggle_group") + ": " + labelOf(state.group, g.key));
          h.appendChild(node(doc, "span", "group__name", labelOf(state.group, g.key)));
          h.appendChild(node(doc, "span", "group__count", g.items.length));
          var chev = icon("chevron", 18); if (chev) h.appendChild(chev);
          h.setAttribute("data-key", "group:" + state.group + ":" + g.key);
          h.addEventListener("click", function () { state.collapsed[state.group + ":" + g.key] = !collapsed; refreshBody(); });
          section.appendChild(h);
          if (collapsed) { box.appendChild(section); return; }
        }
        g.items.forEach(function (p) { ul.appendChild(row(p)); });
        section.appendChild(ul);
        box.appendChild(section);
      });
    }

    function syncOpen() {
      el.filters.className = "plist__filters" + (state.filtersOpen ? " is-open" : "");
      el.backdrop.className = "plist__backdrop" + (state.filtersOpen ? " is-open" : "");
      el.toggle.setAttribute("aria-expanded", state.filtersOpen ? "true" : "false");
    }

    function renderBar() {
      clear(el.bar);
      var search = node(doc, "input", "input input--search");
      search.setAttribute("data-hotkey", "/");                 // клавиша «/» — к поиску (shell.js)
      search.setAttribute("type", "search");
      search.setAttribute("id", "listSearch");
      search.setAttribute("placeholder", t("list.search"));
      search.setAttribute("aria-label", t("list.search"));
      search.value = state.filters.q;
      search.addEventListener("input", function () { state.filters.q = search.value; refreshBody(); });
      el.search = search;
      el.bar.appendChild(search);
      el.bar.appendChild(select("listSort", "list.sort", SORTS, "sort.", state.sort, function (v) { state.sort = v; refreshBody(); }));
      el.bar.appendChild(select("listGroup", "list.group", GROUPS, "group.", state.group, function (v) { state.group = v; refreshBody(); }));
      var toggle = node(doc, "button", "btn btn--secondary plist__toggle");
      toggle.setAttribute("type", "button");
      toggle.setAttribute("aria-controls", "listFilters");
      var fi = icon("sliders", 20); if (fi) toggle.appendChild(fi);
      toggle.appendChild(node(doc, "span", "", t("filters.title")));
      var n = Catalog.activeCount(state.filters);
      if (n) toggle.appendChild(node(doc, "span", "plist__badge", n));
      toggle.addEventListener("click", function () { state.filtersOpen = !state.filtersOpen; syncOpen(); });
      el.toggle = toggle;
      el.bar.appendChild(toggle);
    }

    // Перерисовка пересоздаёт элементы, поэтому фокус клавиатуры возвращаем на тот же по смыслу элемент (data-key): иначе человек с клавиатурой «вылетает» из списка.
    function activeKey() { var a = doc.activeElement; return a && a.getAttribute ? a.getAttribute("data-key") : null; }
    function restoreFocus(key) {
      if (!key) return;
      var nodes = host.querySelectorAll("[data-key]");
      for (var i = 0; i < nodes.length; i++) if (nodes[i].getAttribute("data-key") === key) { nodes[i].focus(); return; }
    }
    function refreshBody() {
      var key = activeKey();
      var facets = Catalog.facets(state.items, state.filters, lang());
      var shown = Catalog.filterProducts(state.items, state.filters);
      renderFilters(facets);
      renderResults(shown);
      restoreFocus(key);
      var n = Catalog.activeCount(state.filters);
      var badge = el.toggle.lastChild;
      if (badge && badge.className === "plist__badge") el.toggle.removeChild(badge);
      if (n) el.toggle.appendChild(node(doc, "span", "plist__badge", n));
      syncOpen();
    }
    function refresh() { renderBar(); refreshBody(); }

    clear(host);
    var root_ = node(doc, "div", "plist");
    el.bar = node(doc, "div", "plist__bar");
    var body = node(doc, "div", "plist__body");
    el.filters = node(doc, "aside", "plist__filters");
    el.filters.setAttribute("id", "listFilters");
    el.backdrop = node(doc, "div", "plist__backdrop");
    el.backdrop.addEventListener("click", function () { state.filtersOpen = false; syncOpen(); });
    el.results = node(doc, "div", "plist__results");
    el.results.setAttribute("aria-live", "polite");
    body.appendChild(el.filters); body.appendChild(el.results);
    root_.appendChild(el.bar); root_.appendChild(el.backdrop); root_.appendChild(body);
    host.appendChild(root_);

    return {
      state: state,
      setItems: function (items) { state.items = Array.isArray(items) ? items : []; refresh(); },
      refresh: refresh,
      setState: function (patch) { Object.assign(state, patch || {}); refresh(); }
    };
  }
  return { create: create, SORTS: SORTS, GROUPS: GROUPS };
});
