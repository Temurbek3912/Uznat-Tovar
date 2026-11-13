/* Список товаров: поиск, фильтры, группы, сортировка и счётчики у вариантов — чистые функции без DOM (проверяются без браузера).
   Работает над товарами, которые уже загружены со страницы /api/v1/catalog/products (ProductSummary: sku, name, brand, category, expected_price,
   photo_count, barcodes). Для каталога в сотни–тысячи SKU этого достаточно и работает без сети; серверные фильтры — следующий шаг (см. docs/ROADMAP.md).
   Поиск не различает регистр, «е/ё» и виды апострофа (oʻzbek = o'zbek = ozbek); все слова запроса должны найтись среди названия, бренда, категории,
   артикула и штрих-кодов. Счётчики у вариантов считаются «как если бы вы добавили этот вариант»: остальные фильтры учитываются, свой — нет. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Catalog = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  function norm(value) {
    return String(value == null ? "" : value).toLocaleLowerCase("ru").replace(/ё/g, "е")
      .replace(/[\u02BB\u02BC\u2018\u2019'`\u00B4]/g, "").replace(/\s+/g, " ").trim();
  }
  function emptyFilters() {
    return { q: "", categories: [], brands: [], priceMin: null, priceMax: null, onlyWithPrice: false, onlyWithPhoto: false, onlyWithBarcode: false };
  }
  function num(v) { return v === null || v === undefined || v === "" || !isFinite(Number(v)) ? null : Number(v); }
  function hasPrice(p) { return num(p.expected_price) !== null; }

  function haystack(p) {
    return norm([p.name, p.brand, p.category, p.sku].concat(p.barcodes || []).join(" "));
  }
  function matchesQuery(p, q) {
    var words = norm(q).split(" ").filter(Boolean);
    if (!words.length) return true;
    var hay = haystack(p);
    return words.every(function (w) { return hay.indexOf(w) !== -1; });
  }
  function inList(list, value) { return !list || !list.length || list.indexOf(value || "") !== -1; }

  /** Пропустить товары через фильтры. skip — имя измерения, которое не учитывать ("categories" | "brands" | "price"): нужно для счётчиков. */
  function filterProducts(items, f, skip) {
    f = Object.assign(emptyFilters(), f || {});
    var lo = num(f.priceMin), hi = num(f.priceMax);
    return (items || []).filter(function (p) {
      if (!matchesQuery(p, f.q)) return false;
      if (skip !== "categories" && !inList(f.categories, p.category)) return false;
      if (skip !== "brands" && !inList(f.brands, p.brand)) return false;
      var price = num(p.expected_price);
      if (skip !== "price" && (lo !== null || hi !== null)) {
        if (price === null) return false;                      // цена задана в фильтре — товары без цены не подходят
        if (lo !== null && price < lo) return false;
        if (hi !== null && price > hi) return false;
      }
      if (f.onlyWithPrice && price === null) return false;
      if (f.onlyWithPhoto && !(Number(p.photo_count) > 0)) return false;
      if (f.onlyWithBarcode && !((p.barcodes || []).length > 0)) return false;
      return true;
    });
  }

  function collator(lang) {
    try { return new Intl.Collator(lang || "ru", { sensitivity: "base", numeric: true }); } catch (e) { return { compare: function (a, b) { return a < b ? -1 : a > b ? 1 : 0; } }; }
  }

  /** Варианты для чипов и чекбоксов: [{value, count}] — больше товаров выше, при равенстве по алфавиту. */
  function facetCounts(items, f, field, skipName, lang) {
    var counts = {};
    filterProducts(items, f, skipName).forEach(function (p) { var v = p[field] || ""; counts[v] = (counts[v] || 0) + 1; });
    var c = collator(lang);
    return Object.keys(counts).map(function (v) { return { value: v, count: counts[v] }; })
      .sort(function (a, b) { return b.count - a.count || c.compare(a.value, b.value); });
  }
  function facets(items, f, lang) {
    var prices = filterProducts(items, f, "price").map(function (p) { return num(p.expected_price); }).filter(function (v) { return v !== null; });
    return {
      categories: facetCounts(items, f, "category", "categories", lang),
      brands: facetCounts(items, f, "brand", "brands", lang),
      price: prices.length ? { min: Math.min.apply(null, prices), max: Math.max.apply(null, prices) } : null,
      total: filterProducts(items, f).length
    };
  }

  /** Сортировка копии списка: name | price_asc | price_desc | new (порядок как пришёл с сервера). Товары без цены всегда в конце. */
  function sortProducts(items, mode, lang) {
    var c = collator(lang);
    var indexed = (items || []).map(function (p, i) { return { p: p, i: i }; });
    indexed.sort(function (a, b) {
      var pa = num(a.p.expected_price), pb = num(b.p.expected_price), r = 0;
      if (mode === "name") r = c.compare(a.p.name || "", b.p.name || "");
      else if (mode === "price_asc" || mode === "price_desc") {
        if (pa === null || pb === null) return pa === pb ? a.i - b.i : (pa === null ? 1 : -1);
        r = mode === "price_asc" ? pa - pb : pb - pa;
      }
      return r || a.i - b.i;
    });
    return indexed.map(function (x) { return x.p; });
  }

  /** Группы: by = "category" | "brand" | "none". Группы по алфавиту, «без значения» — последней. Внутри группы порядок сохраняется. */
  function groupProducts(items, by, lang) {
    if (by !== "category" && by !== "brand") return [{ key: "", items: (items || []).slice() }];
    var map = {}, keys = [];
    (items || []).forEach(function (p) {
      var k = p[by] || "";
      if (!(k in map)) { map[k] = []; keys.push(k); }
      map[k].push(p);
    });
    var c = collator(lang);
    keys.sort(function (a, b) { return a === "" ? 1 : b === "" ? -1 : c.compare(a, b); });
    return keys.map(function (k) { return { key: k, items: map[k] }; });
  }

  /** Сколько фильтров включено (для значка «Фильтры (3)»). Строка поиска не считается. */
  function activeCount(f) {
    f = Object.assign(emptyFilters(), f || {});
    return (f.categories.length ? 1 : 0) + (f.brands.length ? 1 : 0) + ((num(f.priceMin) !== null || num(f.priceMax) !== null) ? 1 : 0)
      + (f.onlyWithPrice ? 1 : 0) + (f.onlyWithPhoto ? 1 : 0) + (f.onlyWithBarcode ? 1 : 0);
  }

  return { norm: norm, emptyFilters: emptyFilters, filterProducts: filterProducts, facets: facets, sortProducts: sortProducts, groupProducts: groupProducts, activeCount: activeCount };
});
