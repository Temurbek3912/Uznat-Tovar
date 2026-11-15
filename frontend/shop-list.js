/* Страница покупателя «Товары магазина» (shop-list.html, шаг 3 владельца 2026-11-15): поиск, категории, фото; нажатие на товар —
   та же карточка, что после снимка (ticket.js), с полями, которые включил магазин (сервер отдаёт только их; срока годности нет
   никогда). Без входа и без персональных данных. Данные в разметку не подставляются строкой — только textContent и атрибуты.
   Чистые функции — ShopList.helpers (проверяются tools/shoplist_smoke_test.js). */
(function (root) {
  "use strict";

  var PAGE = 30;
  var SEARCH_DELAY_MS = 300;

  /** Адрес запроса списка: пустые параметры не отправляются. */
  function listUrl(q, category, offset) {
    var params = [];
    q = String(q || "").trim();
    if (q) params.push("q=" + encodeURIComponent(q.slice(0, 100)));
    if (category) params.push("category=" + encodeURIComponent(category));
    if (offset) params.push("offset=" + Math.max(0, Math.floor(offset)));
    params.push("limit=" + PAGE);
    return "/api/v1/public/products?" + params.join("&");
  }

  function photoUrl(ref) { return "/api/v1/public/photo/" + encodeURIComponent(String(ref || "")); }

  /** Ответ сервера -> {items, total, next}. Испорченный ответ — пустой список, а не ошибка страницы. */
  function parseList(body) {
    var items = body && Array.isArray(body.items) ? body.items.filter(function (x) { return x && typeof x.name === "string"; }) : [];
    var total = body && isFinite(Number(body.total)) ? Number(body.total) : items.length;
    var next = body && typeof body.next_offset === "number" ? body.next_offset : null;
    return { items: items, total: total, next: next };
  }

  function start(win) {
    var doc = win.document, I18N = win.I18N, Ticket = win.Ticket, Icons = win.Icons, Shop = win.Shop;
    var $ = function (id) { return doc.getElementById(id); };
    var t = function (key, params) { return I18N.t(key, params); };
    var H = Shop.helpers;
    var state = { q: "", category: "", items: [], total: 0, next: null, loading: false, seq: 0, open: null, categories: [], timer: null, lastFocus: null };

    function el(tag, cls, text) {
      var n = doc.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined && text !== null) n.textContent = String(text);
      return n;
    }
    function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }

    function drawCategories() {
      var host = $("catHost");
      clear(host);
      var all = [{ name: "", label: t("shoplist.all") }].concat(state.categories.map(function (c) { return { name: c.name, label: c.name + " · " + c.count }; }));
      all.forEach(function (c) {
        var b = el("button", "chip chip--filter", c.label);
        b.setAttribute("type", "button");
        b.setAttribute("aria-pressed", state.category === c.name ? "true" : "false");
        b.addEventListener("click", function () { state.category = c.name; drawCategories(); reload(); });
        host.appendChild(b);
      });
      host.hidden = !state.categories.length;
    }

    function tile(item) {
      var li = el("li", "");
      var b = el("button", "shoplist__item");
      b.setAttribute("type", "button");
      var pic = el("span", "shoplist__pic");
      if (item.has_photo && item.ref) {
        var img = doc.createElement("img");
        img.setAttribute("src", photoUrl(item.ref));
        img.setAttribute("alt", "");
        img.setAttribute("loading", "lazy");
        img.setAttribute("decoding", "async");
        img.addEventListener("error", function () { if (img.parentNode) img.parentNode.removeChild(img); });
        pic.appendChild(img);
      }
      b.appendChild(pic);
      var text = el("span", "shoplist__text");
      if (item.brand) text.appendChild(el("span", "shoplist__brand", item.brand));
      text.appendChild(el("span", "shoplist__name", item.name));
      if (Object.prototype.hasOwnProperty.call(item, "price")) {
        var price = Ticket.formatPrice(item.price);
        // Нет цены — на плитке ничего не пишем (редизайн 2026-11-15: «Цена не указана» на каждой плитке только мешала искать товар).
        if (price) text.appendChild(el("span", "shoplist__price", price + " " + t("ticket.price_unit")));
      }
      b.appendChild(text);
      b.addEventListener("click", function () { openCard(item, b); });
      li.appendChild(b);
      return li;
    }

    function drawList() {
      var host = $("listHost");
      clear(host);
      state.items.forEach(function (item) { host.appendChild(tile(item)); });
      var note = $("countNote");
      if (state.loading && !state.items.length) note.textContent = t("shoplist.loading");
      else if (!state.items.length) note.textContent = t(state.q || state.category ? "shoplist.nothing" : "shoplist.empty");
      else note.textContent = t("shoplist.count", { shown: state.items.length, total: state.total });
      $("moreBtn").hidden = state.next === null;
      $("moreBtn").disabled = state.loading;
    }

    function fail(status) {
      var box = $("listError");
      box.textContent = t(status === 429 ? "shoplist.err_rate" : status === 0 ? "shop.err_network" : "shoplist.err_unavailable");
      box.hidden = false;
    }

    function load(offset) {
      var seq = ++state.seq;                         // ответ на устаревший запрос (человек печатает дальше) не рисуется
      state.loading = true;
      $("listError").hidden = true;
      drawList();
      return win.fetch(listUrl(state.q, state.category, offset), { credentials: "omit" }).then(function (res) {
        return res.json().catch(function () { return null; }).then(function (body) {
          if (seq !== state.seq) return;
          state.loading = false;
          if (!res.ok) { drawList(); fail(res.status); return; }
          var page = parseList(body);
          state.items = offset ? state.items.concat(page.items) : page.items;
          state.total = page.total; state.next = page.next;
          drawList();
        });
      }, function () {
        if (seq !== state.seq) return;
        state.loading = false; drawList(); fail(0);
      });
    }
    function reload() { state.items = []; state.next = null; return load(0); }

    function drawCard() {
      if (!state.open) return;
      var item = state.open;
      Ticket.render(doc, $("ticketHost"), H.localize({ state: "matched", product: H.toProduct(item) }, t),
        { t: t, icons: Icons, maxSpecs: H.MAX_SPECS, browse: true });
    }
    function openCard(item, opener) {
      state.open = item; state.lastFocus = opener || null;
      var photo = $("sheetPhoto");
      if (item.has_photo && item.ref) { $("sheetImg").setAttribute("src", photoUrl(item.ref)); photo.hidden = false; }
      else { $("sheetImg").removeAttribute("src"); photo.hidden = true; }
      drawCard();
      $("sheet").hidden = false;
      doc.body.setAttribute("data-sheet", "open");
      $("sheetClose").focus();
    }
    function closeCard() {
      state.open = null;
      $("sheet").hidden = true;
      doc.body.removeAttribute("data-sheet");
      if (state.lastFocus && state.lastFocus.focus) state.lastFocus.focus();
    }

    Array.prototype.forEach.call(doc.querySelectorAll("[data-icon]"), function (host) {
      if (Icons) host.appendChild(Icons.make(doc, host.getAttribute("data-icon"), { size: 22 }));
    });
    I18N.mountSwitcher($("langHost"));
    I18N.apply();
    I18N.onChange(function () { drawCategories(); drawList(); drawCard(); });
    $("searchInput").addEventListener("input", function () {
      win.clearTimeout(state.timer);
      state.timer = win.setTimeout(function () { state.q = $("searchInput").value; reload(); }, SEARCH_DELAY_MS);
    });
    $("searchInput").addEventListener("keydown", function (e) {
      if (e.key === "Enter") { win.clearTimeout(state.timer); state.q = $("searchInput").value; reload(); }
    });
    $("moreBtn").addEventListener("click", function () { if (state.next !== null && !state.loading) load(state.next); });
    $("sheetClose").addEventListener("click", closeCard);
    $("sheet").addEventListener("click", function (e) { if (e.target === $("sheet")) closeCard(); });
    $("sheet").addEventListener("keydown", function (e) { if (e.key === "Escape" && state.open) closeCard(); });

    win.fetch("/api/v1/public/store", { credentials: "omit" }).then(function (res) { return res.ok ? res.json() : null; })
      .then(function (info) {
        if (info && info.name) { $("storeName").textContent = info.name; $("storeName").hidden = false; }
      }, function () { /* без названия магазина страница работает так же */ });
    win.fetch("/api/v1/public/categories", { credentials: "omit" }).then(function (res) { return res.ok ? res.json() : null; })
      .then(function (body) {
        state.categories = body && Array.isArray(body.items) ? body.items.filter(function (c) { return c && typeof c.name === "string" && c.name; }) : [];
        drawCategories();
      }, function () { drawCategories(); });
    drawCategories();
    var done = reload();

    return { state: state, reload: reload, load: load, openCard: openCard, closeCard: closeCard, ready: done };
  }

  var api = { start: start, helpers: { listUrl: listUrl, photoUrl: photoUrl, parseList: parseList, PAGE: PAGE } };
  root.ShopList = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
