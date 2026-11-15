/* Витрина интерфейса: открывается двойным щелчком по preview.html, сервер не нужен. Данные ВЫМЫШЛЕННЫЕ (бренды и модели несуществующие).
   Показывает: экран «Узнать товар» во всех исходах, список «Товары» с фильтрами и группами, настройки (тема),
   три языка, светлую/тёмную тему. Параметры адреса для проверки: ?lang=uz&theme=dark&state=unknown#products
   Тот же код компонентов (ticket.js, productlist.js, catalog.js, i18n.js) потом подключается к настоящим данным. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Preview = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  var CATEGORIES = {
    mouse: { ru: "Мыши", uz: "Sichqonchalar", en: "Mice" },
    keyboard: { ru: "Клавиатуры", uz: "Klaviaturalar", en: "Keyboards" },
    headset: { ru: "Наушники", uz: "Quloqchinlar", en: "Headphones" },
    webcam: { ru: "Веб-камеры", uz: "Veb-kameralar", en: "Webcams" }
  };
  // [артикул, название, бренд, категория, цена, фото, штрих-код]
  var BASE = [
    ["NM-200", "Ergo M200", "Nordic", "mouse", 249000, 2, "4006381333931"], ["NM-201", "Ergo M200 Pro", "Nordic", "mouse", 399000, 1, ""],
    ["OR-110", "Glide G110", "Orbit", "mouse", 129000, 1, ""], ["OR-1000", "Glide G1000", "Orbit", "mouse", 1190000, 3, "5012345678900"],
    ["DL-15", "Tap T15", "Delta", "mouse", null, 0, ""], ["KP-30", "Lumen L30", "Kappa", "mouse", 189000, 1, ""],
    ["DL-K10", "Compact K10", "Delta", "keyboard", 159000, 1, ""], ["DL-K75", "Compact K75", "Delta", "keyboard", 689000, 2, "4006381333948"],
    ["NK-60", "Ergo K60", "Nordic", "keyboard", 459000, 1, ""], ["KP-K2", "Lumen K2", "Kappa", "keyboard", 329000, 0, ""],
    ["OR-K9", "Glide K9", "Orbit", "keyboard", null, 1, ""], ["NK-87", "Ergo K87 Wireless", "Nordic", "keyboard", 899000, 2, ""],
    ["NH-3", "Ergo H3", "Nordic", "headset", 89000, 1, ""], ["KP-H8", "Lumen H8", "Kappa", "headset", 279000, 1, "5012345678917"],
    ["OR-H5", "Glide H5 Pro", "Orbit", "headset", 549000, 2, ""], ["DL-H1", "Tap H1", "Delta", "headset", 59000, 0, ""],
    ["KP-H12", "Lumen H12 ANC", "Kappa", "headset", 1290000, 3, ""], ["NH-9", "Ergo H9", "Nordic", "headset", 349000, 1, ""],
    ["OR-C1", "Glide C1", "Orbit", "webcam", 219000, 1, ""], ["NC-4", "Ergo C4K", "Nordic", "webcam", 799000, 2, ""],
    ["KP-C2", "Lumen C2", "Kappa", "webcam", 359000, 1, ""], ["DL-C7", "Tap C7", "Delta", "webcam", null, 0, ""]
  ];
  var SAMPLE_FIELDS = {
    ru: [{ label: "Подключение", value: "Bluetooth, USB-приёмник" }, { label: "Датчик", value: "4000 DPI" }, { label: "Гарантия", value: "12 мес." }, { label: "Вес", value: "92 г" }, { label: "Цвет", value: "Графит" }],
    uz: [{ label: "Ulanish", value: "Bluetooth, USB-qabul qilgich" }, { label: "Sensor", value: "4000 DPI" }, { label: "Kafolat", value: "12 oy" }, { label: "Og\u02BBirligi", value: "92 g" }, { label: "Rangi", value: "Grafit" }],
    en: [{ label: "Connection", value: "Bluetooth, USB receiver" }, { label: "Sensor", value: "4000 DPI" }, { label: "Warranty", value: "12 mo." }, { label: "Weight", value: "92 g" }, { label: "Colour", value: "Graphite" }]
  };
  var STATES = ["matched", "uncertain", "unknown", "searching", "camera_error", "empty_catalog"];
  var VIEWS = ["identify", "products", "settings"];

  function parseQuery(search) {
    var out = {};
    String(search || "").replace(/^\?/, "").split("&").forEach(function (pair) {
      if (!pair) return;
      var i = pair.indexOf("=");
      out[decodeURIComponent(i === -1 ? pair : pair.slice(0, i))] = i === -1 ? "" : decodeURIComponent(pair.slice(i + 1));
    });
    return out;
  }
  function buildItems(lang) {
    return BASE.map(function (b) {
      return { id: b[0], sku: b[0], name: b[1], brand: b[2], category: CATEGORIES[b[3]][lang], expected_price: b[4], photo_count: b[5], barcodes: b[6] ? [b[6]] : [] };
    });
  }

  function start(win) {
    var doc = win.document, I18N = win.I18N, Theme = win.Theme, Icons = win.Icons, Ticket = win.Ticket, ProductList = win.ProductList;
    var $ = function (id) { return doc.getElementById(id); };
    var t = function (key, params) { return I18N.t(key, params); };
    var query = parseQuery(win.location && win.location.search);
    if (query.lang) I18N.setLang(query.lang);
    if (query.theme) Theme.set(query.theme);
    var st = { state: STATES.indexOf(query.state) !== -1 ? query.state : "matched", view: "identify" };

    function product(lang) {
      return { id: "NM-200", name: "Ergo M200", brand: "Nordic", category: CATEGORIES.mouse[lang], expected_price: 249000, fields: SAMPLE_FIELDS[lang] };
    }
    function viewFor(state, lang) {
      var items = buildItems(lang);
      if (state === "matched") return { state: "matched", product: product(lang) };
      if (state === "uncertain") return { state: "uncertain", product: items[0], candidates: [items[0], items[1], items[2]] };
      return { state: state, message: state === "camera_error" ? t("error.camera_denied") : "" };
    }
    var deps = {
      t: t, icons: Icons,
      onPick: function (c) { setState("matched", Object.assign({}, c)); },
      onRetry: function () { setState("searching"); },
      onAdd: function () {}, onPhoto: function () {}, onWrong: function () { setState("uncertain"); }, onMore: function () {}
    };
    var override = null;
    function setState(state, picked) {
      st.state = state; override = picked || null; renderIdentify();
    }
    function renderIdentify() {
      var lang = I18N.getLang();
      var view = viewFor(st.state, lang);
      if (override) view = { state: "matched", product: Object.assign({}, override, { fields: SAMPLE_FIELDS[lang], category: override.category }) };
      Ticket.render(doc, $("ticket"), view, deps);
      $("stage").setAttribute("data-state", view.state);
      Array.prototype.forEach.call($("demoStates").children, function (b) { b.setAttribute("aria-pressed", b.getAttribute("data-state") === st.state ? "true" : "false"); });
    }

    // постоянные части: значки, кнопки, переключатели
    Array.prototype.forEach.call(doc.querySelectorAll("[data-icon]"), function (host) { host.appendChild(Icons.make(doc, host.getAttribute("data-icon"), { size: 22 })); });
    I18N.mountSwitcher($("langHost"));
    STATES.forEach(function (s) {
      var b = doc.createElement("button");
      b.setAttribute("type", "button"); b.className = "chip chip--filter"; b.setAttribute("data-state", s);
      b.addEventListener("click", function () { setState(s); });
      $("demoStates").appendChild(b);
    });
    function labelButtons() {
      Array.prototype.forEach.call($("demoStates").children, function (b) { b.textContent = t("demo.state." + b.getAttribute("data-state")); });
      var photo = $("photoBtn"), flip = $("flipBtn");
      while (photo.firstChild) photo.removeChild(photo.firstChild);
      photo.appendChild(Icons.make(doc, "camera", { size: 20 })); photo.appendChild(doc.createTextNode(t("identify.take_photo")));
      while (flip.firstChild) flip.removeChild(flip.firstChild);
      flip.appendChild(Icons.make(doc, "refresh", { size: 20 })); flip.setAttribute("aria-label", t("common.retry"));
      var theme = $("themeBtn"), mode = Theme.get();
      while (theme.firstChild) theme.removeChild(theme.firstChild);
      theme.appendChild(Icons.make(doc, mode === "dark" ? "moon" : "sun", { size: 20 }));
      theme.setAttribute("aria-label", t("theme.label", { mode: t("theme." + mode) }));
      $("themeTitle").textContent = t("theme.label", { mode: t("theme." + mode) });
      doc.title = t("app.name");
    }
    $("themeBtn").addEventListener("click", function () { Theme.next(); labelButtons(); renderThemeSwitch(); });

    // настройки: тема
    function renderThemeSwitch() {
      var host = $("themeHost");
      while (host.firstChild) host.removeChild(host.firstChild);
      Theme.modes.forEach(function (m) {
        var b = doc.createElement("button");
        b.setAttribute("type", "button"); b.className = "lang-btn lang-btn--cap"; b.textContent = t("theme." + m);
        b.setAttribute("aria-pressed", Theme.get() === m ? "true" : "false");
        b.addEventListener("click", function () { Theme.set(m); labelButtons(); renderThemeSwitch(); });
        host.appendChild(b);
      });
    }
    // список товаров
    var list = ProductList.create(doc, $("list"), { t: t, icons: Icons, getLang: function () { return I18N.getLang(); } });
    list.setItems(buildItems(I18N.getLang()));

    // разделы
    function showView(name) {
      st.view = VIEWS.indexOf(name) !== -1 ? name : "identify";
      VIEWS.forEach(function (v) {
        var section = $("view" + v.charAt(0).toUpperCase() + v.slice(1));
        if (v === st.view) section.removeAttribute("hidden"); else section.setAttribute("hidden", "");
      });
      Array.prototype.forEach.call(doc.querySelectorAll("[data-view]"), function (a) {
        if (a.getAttribute("data-view") === st.view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
      });
      if ($("demo")) { if (st.view === "identify") $("demo").removeAttribute("hidden"); else $("demo").setAttribute("hidden", ""); }
      var title = { identify: "app.name", products: "list.title", settings: "nav.settings" }[st.view];
      $("screenTitle").textContent = t(title);
    }
    function hashView() { return String((win.location && win.location.hash) || "").replace(/^#/, ""); }
    if (win.addEventListener) win.addEventListener("hashchange", function () { showView(hashView()); });

    I18N.onChange(function () {
      I18N.apply();
      labelButtons(); renderThemeSwitch(); renderIdentify();
      list.setState({ items: buildItems(I18N.getLang()), filters: win.Catalog.emptyFilters() });
      showView(st.view);
    });
    I18N.apply();
    labelButtons(); renderThemeSwitch(); renderIdentify();
    if (query.group || query.sort) list.setState({ group: ProductList.GROUPS.indexOf(query.group) !== -1 ? query.group : "none", sort: ProductList.SORTS.indexOf(query.sort) !== -1 ? query.sort : "new" });
    showView(query.view || hashView() || "identify");
    return { state: st, list: list, showView: showView, setState: setState };
  }
  return { start: start, parseQuery: parseQuery, buildItems: buildItems, STATES: STATES, VIEWS: VIEWS, CATEGORIES: CATEGORIES };
});
