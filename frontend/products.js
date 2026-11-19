/* Экран «Товары» (products.html): добавить товар (название + фото), фото к существующему, правка названия/цены/штрих-кодов, список с поиском и фильтрами.
   Как у «Узнать товар» (identify.js): страница только подключает файлы и вызывает Products.start(window); вся логика здесь и проверяется на настоящей разметке
   (tools/products_smoke_test.js). Список рисует ProductList (поиск, фильтры, группы — на странице, над уже загруженными товарами; сервер отдаёт по 100 за запрос,
   страница догружает порциями). Фото на сервере отдаются только с заголовком авторизации, поэтому миниатюры грузятся через fetch и показываются как blob: (лениво, по мере
   появления строки на экране). Вход и токен ведёт общий модуль Auth (auth.js). Тексты — только через I18N.t; сообщения хранятся как «ключ + параметры» и перерисовываются при смене языка. Данные из базы в разметку строкой
   не попадают: только textContent / setAttribute. Сервер: GET /api/v1/catalog/products, POST …/products/upload (202 + task_id), POST …/{id}/photos, GET …/tasks/{id},
   PATCH …/{id}, GET …/{id}/photo?size=thumb; фото товара из базы — GET …/{id}/images, PATCH …/{id}/images/{фото} (вывести из эталонов, вернуть,
   сделать витринным или эталоном; фото не удаляются), миниатюра фото — GET …/{id}/photo?size=thumb&image_id=. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Products = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  var MAX_PHOTOS = 8, GOOD_PHOTOS = 3, MAX_SIDE = 1600, PAGE_SIZE = 100, MAX_ITEMS = 5000;   // PAGE_SIZE — больше 100 сервер за раз не отдаёт
  var TUNING = { pollLimitMs: 180000, pollScale: 1 };                                      // тест ставит маленькие числа, чтобы не ждать секунды

  // ---------------------------------------------------------------- чистая логика
  function fitSize(width, height, max) {
    var longest = Math.max(width, height);
    if (longest <= max) return { w: width, h: height };
    var k = max / longest;
    return { w: Math.max(1, Math.round(width * k)), h: Math.max(1, Math.round(height * k)) };
  }
  /** Подсказка про число фото: {key, n, level}; текст собирается при показе (язык может смениться). */
  function photosHint(count) {
    if (count <= 0) return { key: "products.photos_none", n: 0, level: "" };
    if (count >= MAX_PHOTOS) return { key: "products.photos_max", n: count, level: "good" };
    if (count < GOOD_PHOTOS) return { key: "products.photos_few", n: count, level: "soso" };
    return { key: "products.photos_good", n: count, level: "good" };
  }
  function submitState(name, photoCount, busy) {
    if (busy) return { ok: false, whyKey: "" };
    if (!String(name || "").trim()) return { ok: false, whyKey: "products.need_name" };
    if (photoCount < 1) return { ok: false, whyKey: "products.need_photo" };
    return { ok: true, whyKey: "" };
  }
  /** Ошибка сервера -> {key, params} (код ошибки, для которого есть перевод: сообщение на языке приложения), {text} (готовая фраза
      сервера по-русски — если перевода нет) или {key} (наш общий текст). body — всё тело ответа ({detail, code, params}), has(key) —
      есть ли перевод ключа (I18N.has). */
  function describeApiError(status, detail, body, has) {
    if (body && typeof body.code === "string" && /^[a-z_]+$/.test(body.code) && has && has("server." + body.code)) {
      return { key: "server." + body.code, params: body.params && typeof body.params === "object" ? body.params : {} };
    }
    var text = "";
    if (typeof detail === "string") text = detail;
    else if (detail && typeof detail === "object" && !Array.isArray(detail) && typeof detail.message === "string") text = detail.message;
    else if (Array.isArray(detail)) return { key: "error.form" };
    if (text) return { text: text };
    if (status === 401) return { key: "login.expired" };
    if (status === 403) return { key: "error.forbidden" };
    if (status === 413) return { key: "error.too_large" };
    if (status >= 500) return { key: "error.server" };
    return { key: "error.generic" };
  }
  function pollDelay(attempt, scale) { return Math.round(Math.min(1000 + attempt * 400, 4000) * (scale === undefined ? 1 : scale)); }
  /** Штрих-код из адреса (?barcode=…): только 8–14 цифр, остальное — мусор. */
  function barcodeFromSearch(search) {
    var code = new URLSearchParams(search || "").get("barcode");
    return code && /^[0-9]{8,14}$/.test(code) ? code : "";
  }
  /** Тело PATCH при правке: пустая цена — «убрать цену» (clear_price), пустой код не отправляется. */
  function buildEditBody(name, priceText, newCode) {
    var body = { name: name }, price = String(priceText || "").trim(), code = String(newCode || "").trim();
    if (price) body.expected_price = price; else body.clear_price = true;
    if (code) body.add_barcode = code;
    return body;
  }
  // Сведения для покупателя (миграция 0007): поле формы -> поле товара. Пустая строка — «убрать» (сервер хранит пусто).
  var DETAIL_INPUTS = { net_content: "editNetContent", manufacturer: "editManufacturer", country: "editCountry",
                        ingredients: "editIngredients", description: "editDescription" };
  /** Сведения из формы -> добавка к телу PATCH: только изменённые относительно товара (original). Пустая строка — убрать.
      Наличие: "yes"/"no" -> in_stock, "" -> clear_in_stock («не указано»). */
  function buildDetailsBody(values, original) {
    var body = {}, was = original || {};
    values = values || {};
    Object.keys(DETAIL_INPUTS).forEach(function (code) {
      var now = String(values[code] || "").trim();
      if (now !== String(was[code] || "").trim()) body[code] = now;
    });
    var stock = String(values.in_stock || "");
    if (stock !== stockValue(was.in_stock)) {
      if (stock === "yes") body.in_stock = true; else if (stock === "no") body.in_stock = false; else body.clear_in_stock = true;
    }
    return body;
  }
  function stockValue(v) { return v === true ? "yes" : v === false ? "no" : ""; }
  function photoName(file, index) { return file && file.name ? file.name : "photo-" + (index + 1) + ".jpg"; }

  // ---------------------------------------------------------------- страница
  function start(win, options) {
    options = options || {};
    var doc = win.document, I18N = win.I18N, Auth = win.Auth, Icons = win.Icons, Theme = win.Theme, ProductList = win.ProductList;
    var tuning = Object.assign({}, TUNING, options.tuning || {});
    var $ = function (id) { return doc.getElementById(id); };
    var t = function (key, params) { return I18N.t(key, params); };
    var report = options.onError || function (e) { if (win.console && win.console.error) win.console.error(e); };
    var guard = function (fn) { return function () { var r = fn.apply(null, arguments); if (r && r.catch) r.catch(report); return r; }; };
    var sleep = function (ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); };

    // ----- состояние
    var chosen = [], busy = false, addOpen = false, editingId = null, editingCodes = [], editingDetails = {}, addPhotosFor = null;
    var progress = null;                       // null | {type: "uploading" | "studying" | "slow" | "fail" | "done", ...}
    var lastTask = null, items = [], capped = false, dupState = null, editingName = "";
    var msgs = {};                             // id -> {key, params} | {text}: сообщения хранятся логически и перерисовываются при смене языка
    var listNoteKind = "";                     // "", "ok" или "err" — вид строки под списком
    var thumbCache = {}, loaded = false;
    var view = "active", editingStatus = "active", editingVersion = null;   // раздел списка: каталог или архив (миграция 0006)
    var editingImages = null, imagesFailed = false, imageUrls = {}, imageBusy = false;   // фото товара в карточке правки (из базы)

    // ----- вход (токен, адрес и запрос входа — в общем модуле Auth)
    var auth = Auth.create(win);
    var getToken = auth.getToken, setToken = auth.setToken, authHeaders = auth.authHeaders, apiBase = auth.apiBase;
    function NetError(key) { var e = new Error(key); e.name = "NetError"; e.key = key; return e; }
    function isNetError(e) { return !!e && e.name === "NetError"; }
    function apiFetch(path, opts) {
      return Promise.resolve().then(function () {
        return win.fetch(apiBase() + path, Object.assign({}, opts, { headers: Object.assign({}, authHeaders(), (opts && opts.headers) || {}) }));
      }).then(function (res) {
        if (res.status === 401) { setToken(null); showLogin("login.expired"); throw NetError("login.expired"); }
        return res;
      }, function (e) { if (isNetError(e)) throw e; throw NetError("error.network"); });
    }
    function readError(res) { return res.json().then(function (b) { return b && typeof b === "object" ? b : {}; }, function () { return {}; }); }
    function apiError(status, body) { return describeApiError(status, body.detail, body, I18N.has); }

    // ----- мелкие помощники разметки
    function setHidden(el, hidden) { if (hidden) el.setAttribute("hidden", ""); else el.removeAttribute("hidden"); }
    function show(id, on) { setHidden($(id), !on); }
    function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
    function label(button, iconName, text) {
      clear(button);
      if (iconName) button.appendChild(Icons.make(doc, iconName, { size: 20 }));
      var span = doc.createElement("span"); span.textContent = text; button.appendChild(span);
    }
    function scrollTo(id) {
      var el = $(id);
      if (!el.scrollIntoView) return;
      var calm = win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches;
      el.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "start" });
    }

    function scrollNear(id) {                          // сообщение под кнопкой отправки не должно остаться за краем экрана
      var el = $(id);
      if (!el.scrollIntoView) return;
      var calm = win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches;
      el.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "center" });
    }

    // ----- сообщения («ключ + параметры» или готовый текст сервера)
    function paintMsg(id) {
      var el = $(id), m = msgs[id], text = m ? (m.text !== undefined ? m.text : t(m.key, m.params)) : "";
      el.textContent = text; setHidden(el, !text);
      if (id === "listNote") el.className = listNoteKind === "err" ? "notice notice--danger" : listNoteKind === "ok" ? "hint hint--good" : "hint";
    }
    function setMsg(id, m, kind) {
      if (m) msgs[id] = m; else delete msgs[id];
      if (id === "listNote") listNoteKind = m ? (kind || "") : "";
      paintMsg(id);
    }
    function paintAllMsgs() { ["formError", "editError", "listError", "listNote", "editPhotosNote"].forEach(paintMsg); }

    // ----- выбор и подготовка фото
    function shrinkPhoto(file) {
      // Снимок с телефона — 3–8 МБ. Уменьшаем в браузере до 1600 px: быстрее по Wi-Fi, а система всё равно смотрит на 224 px.
      // Любая неудача (старый браузер, HEIC) — отправляем как есть: сервер сам проверит файл и скажет понятно.
      if (!file.type || file.type.indexOf("image/") !== 0 || typeof win.createImageBitmap !== "function") return Promise.resolve(file);
      return win.createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bitmap) {
        var size = fitSize(bitmap.width, bitmap.height, MAX_SIDE);
        if (size.w === bitmap.width && size.h === bitmap.height && file.size < 1500000) { if (bitmap.close) bitmap.close(); return file; }
        var canvas = doc.createElement("canvas");
        canvas.width = size.w; canvas.height = size.h;
        canvas.getContext("2d").drawImage(bitmap, 0, 0, size.w, size.h);
        if (bitmap.close) bitmap.close();
        return new Promise(function (resolve) { canvas.toBlob(resolve, "image/jpeg", 0.9); }).then(function (blob) { return blob || file; });
      }).catch(function () { return file; });
    }
    function renderStrip() {
      var strip = $("photoStrip");
      clear(strip);
      chosen.forEach(function (item, index) {
        var box = doc.createElement("div"); box.className = "ph";
        var img = doc.createElement("img"); img.setAttribute("src", item.url); img.setAttribute("alt", t("products.photo_alt", { n: index + 1 }));
        var rm = doc.createElement("button"); rm.setAttribute("type", "button"); rm.className = "icon-btn icon-btn--stage ph__rm";
        rm.appendChild(Icons.make(doc, "x", { size: 18 }));
        rm.setAttribute("aria-label", t("products.photo_remove", { n: index + 1 }));
        rm.addEventListener("click", function () { win.URL.revokeObjectURL(item.url); chosen.splice(chosen.indexOf(item), 1); refreshForm(); });
        box.appendChild(img); box.appendChild(rm); strip.appendChild(box);
      });
      var hint = photosHint(chosen.length), el = $("photosHint");
      el.textContent = t(hint.key, { count: t("photos.count", { n: hint.n }) });
      el.className = "hint" + (hint.level === "good" ? " hint--good" : "");
    }
    function refreshForm() {
      renderStrip();
      var state = submitState($("nameInput").value, chosen.length, busy);
      $("submitBtn").disabled = !state.ok;
      $("submitHint").textContent = state.whyKey ? t(state.whyKey) : "";
    }
    function addChosen(fileList) {
      var files = Array.prototype.slice.call(fileList || []), index = 0;
      function next() {
        if (index >= files.length) { refreshForm(); return Promise.resolve(); }
        if (chosen.length >= MAX_PHOTOS) { setMsg("formError", { key: "products.photos_limit", params: { max: MAX_PHOTOS } }); refreshForm(); return Promise.resolve(); }
        var file = files[index++];
        return shrinkPhoto(file).then(function (blob) {
          chosen.push({ blob: blob, name: photoName(file, chosen.length), url: win.URL.createObjectURL(blob) });
          return next();
        });
      }
      return next();
    }

    // ----- панели: добавление, ожидание, правка
    function syncPanels() {
      show("addCard", addOpen && !progress);
      show("openAdd", !addOpen && !progress);
      show("progressCard", !!progress);
    }
    function renderProgress() {
      var p = progress;
      syncPanels();
      if (!p) return;
      var working = p.type === "uploading" || p.type === "studying";
      show("progressRow", working);
      if (working) $("progressText").textContent = t(p.type === "uploading" ? "products.uploading" : "products.studying");
      show("doneBlock", p.type === "done"); show("failBlock", p.type === "fail"); show("slowBlock", p.type === "slow");
      if (p.type === "done") {
        var ctx = p.ctx;
        $("doneText").textContent = ctx.kind === "photos" ? t("products.done_photos", { name: ctx.name, n: ctx.photos }) : t("products.done_new", { name: ctx.name });
        $("warnList").textContent = (ctx.warnings || []).map(function (w) { return w.key ? t(w.key, w.params || {}) : w.photo ? t("products.warn_photo", { n: w.photo, message: w.message }) : w.message; }).join("\n");
      }
      if (p.type === "fail") {
        var code = p.task.code;                // причина отказа на языке человека, если сервер прислал известный код
        $("failText").textContent = (typeof code === "string" && /^[a-z_]+$/.test(code) && I18N.has("server." + code))
          ? t("server." + code, p.task.params && typeof p.task.params === "object" ? p.task.params : {})
          : (p.task.message || t("products.fail_default"));
        $("failDetail").textContent = p.task.detail || t("products.fail_nodetail");
      }
      if (p.type === "slow") $("slowText").textContent = t(p.offline ? "products.slow_offline" : "products.slow");
    }
    function setProgress(p) { progress = p; renderProgress(); if (p) scrollTo("progressCard"); }
    function backToForm() { progress = null; renderProgress(); }
    // listenerOnly: администратор на адресе для сотрудников — права правки каталога действуют только на административном входе (В-5).
    function showAccess(listenerOnly) {
      resetViews(); show("accessBanner", true);
      setHidden($("accessText"), !!listenerOnly); setHidden($("accessListener"), !listenerOnly);
    }
    function openAddPanel(on) { addOpen = on; syncPanels(); if (on) { var n = $("nameInput"); if (n.focus) n.focus(); } }

    function renderDuplicate() {
      var d = dupState;
      show("dupNotice", !!d);
      if (!d) return;
      var m = d.message;                      // {key, params} — перевод по коду сервера; {text} — прежний русский текст
      $("dupTitle").textContent = (m && (m.text !== undefined ? m.text : t(m.key, m.params))) || t("products.dup_default");
      show("dupYes", !!d.target);
    }
    function showDuplicate(body) {
      var b = body && typeof body === "object" ? body : {};
      var d = b.detail && typeof b.detail === "object" ? b.detail : {};
      var known = typeof b.code === "string" && /^[a-z_]+$/.test(b.code) && I18N.has("server." + b.code);
      var message = known ? { key: "server." + b.code, params: b.params && typeof b.params === "object" ? b.params : {} }
        : (typeof d.message === "string" && d.message ? { text: d.message } : null);
      dupState = { message: message, target: d.product_id ? { id: d.product_id, name: d.product_name || "" } : null };
      addPhotosFor = dupState.target;
      renderDuplicate(); scrollNear("dupNotice");
    }
    function hideDuplicate() { dupState = null; renderDuplicate(); }

    function formWith(files) {
      var fd = new win.FormData();
      files.forEach(function (item, i) { fd.append("files", item.blob, item.name || "photo-" + (i + 1) + ".jpg"); });
      return fd;
    }
    function netMessage(e, fallbackKey) { return isNetError(e) ? { key: e.key } : { key: fallbackKey || "error.generic" }; }

    function submitNew() {
      if (busy) return Promise.resolve();
      var name = $("nameInput").value;
      if (!submitState(name, chosen.length, false).ok) return Promise.resolve();
      busy = true; refreshForm();
      setMsg("formError", null); hideDuplicate();
      var fd = formWith(chosen);
      fd.append("name", name); fd.append("price", $("priceInput").value);
      fd.append("brand", $("brandInput").value); fd.append("category", $("categoryInput").value);
      fd.append("barcode", $("barcodeInput").value);
      setProgress({ type: "uploading" });
      return apiFetch("/api/v1/catalog/products/upload", { method: "POST", body: fd }).then(function (res) {
        if (res.status === 403) { showAccess(); return null; }
        if (res.status === 409) return readError(res).then(function (b) { backToForm(); showDuplicate(b); return null; });
        if (res.status !== 202 && !res.ok) return readError(res).then(function (b) { backToForm(); setMsg("formError", apiError(res.status, b)); scrollNear("formError"); return null; });
        return res.json().then(function (data) { return waitForTask(data.task_id, { kind: "new", id: data.product_id, name: data.name, photos: data.photos, warnings: data.warnings || [] }); });
      }).catch(function (e) { backToForm(); setMsg("formError", netMessage(e)); scrollNear("formError"); })
        .then(function () { busy = false; refreshForm(); });
    }
    function waitForTask(taskId, ctx) {
      lastTask = { taskId: taskId, ctx: ctx };
      setProgress({ type: "studying" });
      var started = Date.now();
      function poll(attempt) {
        return apiFetch("/api/v1/catalog/products/tasks/" + encodeURIComponent(taskId)).then(function (res) {
          if (res.status === 403) { showAccess(); return null; }
          var next = function () {
            if (Date.now() - started > tuning.pollLimitMs) { setProgress({ type: "slow", offline: false }); return null; }
            return sleep(pollDelay(attempt, tuning.pollScale)).then(function () { return poll(attempt + 1); });
          };
          if (!res.ok) return next();
          return res.json().then(function (task) {
            if (task.state === "done") return finishOk(ctx, task);
            if (task.state === "failed") { setProgress({ type: "fail", task: task }); return null; }
            return next();
          });
        }, function () { setProgress({ type: "slow", offline: true }); return null; });
      }
      return poll(0);
    }
    function finishOk(ctx, task) {
      // Код с фото успели привязать к другому товару, пока задача шла: товар добавлен без него — сказать об этом (notes).
      var notes = task && Array.isArray(task.notes) ? task.notes : [];
      notes.forEach(function (n) {
        if (!n || typeof n !== "object") return;
        var known = typeof n.code === "string" && /^[a-z_]+$/.test(n.code) && I18N.has("server." + n.code);
        ctx.warnings = (ctx.warnings || []).concat([known ? { key: "server." + n.code, params: n.params && typeof n.params === "object" ? n.params : {} }
                                                          : { message: typeof n.message === "string" ? n.message : "" }]);
      });
      setProgress({ type: "done", ctx: ctx });
      if (ctx.kind === "photos" && ctx.id) forgetThumb(ctx.id);
      return loadProducts();
    }
    function resetForm() {
      chosen.forEach(function (item) { win.URL.revokeObjectURL(item.url); }); chosen = [];
      ["nameInput", "priceInput", "brandInput", "categoryInput", "barcodeInput"].forEach(function (id) { $(id).value = ""; });
      setMsg("formError", null); hideDuplicate(); addPhotosFor = null;
      progress = null; addOpen = true; renderProgress(); refreshForm();
    }

    // ----- фото к существующему товару
    function sendPhotosTo(target, photos) {
      if (busy || !photos.length) return Promise.resolve();
      busy = true;
      setProgress({ type: "uploading" });
      return apiFetch("/api/v1/catalog/products/" + encodeURIComponent(target.id) + "/photos", { method: "POST", body: formWith(photos) }).then(function (res) {
        if (res.status === 403) { showAccess(); return null; }
        if (!res.ok) return readError(res).then(function (b) { backToForm(); setMsg("listNote", apiError(res.status, b), "err"); return null; });
        return res.json().then(function (data) { return waitForTask(data.task_id, { kind: "photos", id: target.id, name: data.name || target.name, photos: data.photos, warnings: data.warnings || [] }); });
      }).catch(function (e) { backToForm(); setMsg("listNote", netMessage(e), "err"); })
        .then(function () { busy = false; refreshForm(); });
    }

    // ----- список
    function forgetThumb(id) { var url = thumbCache[id]; if (url) { win.URL.revokeObjectURL(url); delete thumbCache[id]; } }
    function loadThumb(product) {
      return apiFetch("/api/v1/catalog/products/" + encodeURIComponent(product.id) + "/photo?size=thumb").then(function (res) {
        if (!res.ok) return null;
        return res.blob().then(function (blob) { var url = win.URL.createObjectURL(blob); thumbCache[product.id] = url; return url; });
      }).catch(function () { return null; });              // без миниатюры остаётся буква
    }
    function renderListState() {                        // до первой удачной загрузки не показываем ни список, ни «пока пусто»: это не пустой каталог, а ожидание или сбой
      show("listEmpty", loaded && items.length === 0);
      $("listEmpty").textContent = t(view === "archived" ? "products.archive_empty" : "products.list_empty");
      show("productList", loaded && items.length > 0);
    }
    function loadProducts() {
      setMsg("listError", null);
      var all = [];
      function page() {
        return apiFetch("/api/v1/catalog/products?limit=" + PAGE_SIZE + "&offset=" + all.length + "&sort=new&status=" + view).then(function (res) {
          if (res.status === 403) { showAccess(); return false; }
          if (!res.ok) { setMsg("listError", { key: "products.list_error" }); return false; }
          return res.json().then(function (batch) {
            if (!Array.isArray(batch)) { setMsg("listError", { key: "products.list_error" }); return false; }
            batch.forEach(function (p) { all.push(p); });
            if (batch.length < PAGE_SIZE) return true;
            if (all.length >= MAX_ITEMS) { capped = true; return true; }
            return page();
          });
        });
      }
      capped = false;
      return page().then(function (ok) {
        if (!ok) { renderListState(); return; }
        items = all; loaded = true; list.setItems(items); renderListState();
        if (capped) setMsg("listNote", { key: "products.list_capped", params: { n: MAX_ITEMS } }); else if (listNoteKind === "" && msgs.listNote && msgs.listNote.key === "products.list_capped") setMsg("listNote", null);
        if (!items.length && !progress && view === "active") openAddPanel(true);
      }, function (e) { if (!isNetError(e) || e.key !== "login.expired") setMsg("listError", isNetError(e) ? { key: e.key } : { key: "products.list_error" }); renderListState(); });
    }
    var list = ProductList.create(doc, $("productList"), {
      t: t, icons: Icons, getLang: function () { return I18N.getLang(); },
      onOpen: function (p) { openEdit(p); }, photoUrl: function (p) { return thumbCache[p.id] || null; }, loadThumb: loadThumb
    });

    // ----- правка
    function renderEditCodes() {
      var box = $("editCodes");
      clear(box);
      editingCodes.forEach(function (code) {
        var tag = doc.createElement("span"); tag.className = "tag";
        var text = doc.createElement("span"); text.textContent = code;
        var rm = doc.createElement("button"); rm.setAttribute("type", "button"); rm.className = "icon-btn";
        rm.appendChild(Icons.make(doc, "x", { size: 18 }));
        rm.setAttribute("aria-label", t("products.code_remove", { code: code }));
        rm.addEventListener("click", guard(function () { return removeBarcode(code); }));
        tag.appendChild(text); tag.appendChild(rm); box.appendChild(tag);
      });
      if (!editingCodes.length) { var none = doc.createElement("span"); none.className = "hint"; none.textContent = t("products.codes_none"); box.appendChild(none); }
    }
    function renderEditTitle() { $("editTitle").textContent = t("products.edit_title", { name: editingName }); }
    function openEdit(p) {
      editingId = p.id; editingName = p.name || "";
      editingStatus = p.status || "active"; editingVersion = typeof p.version === "number" ? p.version : null; relabel();
      editingCodes = Array.isArray(p.barcodes) ? p.barcodes.slice() : [];
      renderEditCodes(); renderEditTitle(); $("editBarcode").value = "";
      $("editName").value = p.name || "";
      $("editPrice").value = p.expected_price === null || p.expected_price === undefined ? "" : String(p.expected_price).replace(".", ",");
      editingDetails = { in_stock: p.in_stock };
      Object.keys(DETAIL_INPUTS).forEach(function (code) {
        editingDetails[code] = p[code] || "";
        if ($(DETAIL_INPUTS[code])) $(DETAIL_INPUTS[code]).value = p[code] || "";
      });
      if ($("editInStock")) $("editInStock").value = stockValue(p.in_stock);
      setMsg("editError", null); show("editCard", true);
      forgetImages(); loadImages(p.id);
      var first = $("editName"); if (first.focus) first.focus();
      scrollTo("editCard");
    }
    function closeEdit() { editingId = null; show("editCard", false); forgetImages(); }

    // ----- фото товара в карточке правки: эталоны, витринное, выведенные (из базы; файлы не удаляются никогда)
    function forgetImages() {
      Object.keys(imageUrls).forEach(function (id) { if (imageUrls[id]) win.URL.revokeObjectURL(imageUrls[id]); });
      imageUrls = {}; editingImages = null; imagesFailed = false; setMsg("editPhotosNote", null);
    }
    function imageState(img) {
      if (img.role === "display") return { key: img.is_cover ? "products.photo_state_cover" : "products.photo_state_display", ok: false };
      if (!img.active) return { key: "products.photo_state_off", ok: false };
      return { key: "products.photo_state_ref", ok: true };
    }
    function imageActions(img) {                        // какие кнопки у фото: [ключ текста, что отправить]
      if (img.role === "display") return [["products.photo_make_ref", { role: "reference", active: true }]];
      if (!img.active) return [["products.photo_activate", { active: true }]];
      return [["products.photo_deactivate", { active: false }], ["products.photo_make_display", { role: "display" }]];
    }
    function putPic(host, url, img) {
      clear(host);
      var pic = doc.createElement("img"); pic.setAttribute("src", url);
      pic.setAttribute("alt", t("products.photo_alt", { n: editingImages ? editingImages.indexOf(img) + 1 : 1 }));
      host.appendChild(pic);
    }
    function loadImageThumb(productId, img, host) {
      if (!img.has_file) return;
      if (imageUrls[img.id]) { putPic(host, imageUrls[img.id], img); return; }
      apiFetch("/api/v1/catalog/products/" + encodeURIComponent(productId) + "/photo?size=thumb&image_id=" + encodeURIComponent(img.id)).then(function (res) {
        if (!res.ok) return null;
        return res.blob().then(function (blob) {
          if (editingId !== productId) return;                                  // карточку уже закрыли или открыли другой товар
          imageUrls[img.id] = win.URL.createObjectURL(blob); putPic(host, imageUrls[img.id], img);
        });
      }).catch(function () { /* без миниатюры остаётся подпись */ });
    }
    function hintLine(box, key) { var p = doc.createElement("p"); p.className = "hint"; p.textContent = t(key); box.appendChild(p); }
    function renderEditPhotos() {
      var box = $("editPhotos");
      clear(box);
      if (imagesFailed) { hintLine(box, "products.photos_load_failed"); return; }
      if (!editingImages) { hintLine(box, "common.loading"); return; }
      if (!editingImages.length) { hintLine(box, "products.photos_db_none"); return; }
      var productId = editingId;
      editingImages.forEach(function (img, index) {
        var card = doc.createElement("div"); card.className = img.in_recognition ? "gph" : "gph gph--off";
        var pic = doc.createElement("div"); pic.className = "gph__pic";
        pic.textContent = img.has_file ? t("products.photo_alt", { n: index + 1 }) : t("products.photo_no_file");
        card.appendChild(pic);
        var st = imageState(img), state = doc.createElement("span");
        state.className = st.ok ? "gph__state gph__state--ok" : "gph__state"; state.textContent = t(st.key);
        card.appendChild(state);
        if (img.credit) {                                   // чьё фото (Open Food Facts, CC BY-SA) — как у покупателя
          var credit = doc.createElement("span"); credit.className = "gph__credit";
          credit.textContent = t("common.photo_credit", { src: img.credit }); card.appendChild(credit);
        }
        imageActions(img).forEach(function (a) {
          var b = doc.createElement("button"); b.setAttribute("type", "button"); b.className = "btn btn--secondary gph__btn";
          b.textContent = t(a[0]); if (imageBusy) b.disabled = true;
          b.addEventListener("click", guard(function () { return changeImage(productId, img, a[1]); }));
          card.appendChild(b);
        });
        box.appendChild(card);
        loadImageThumb(productId, img, pic);
      });
    }
    function loadImages(productId) {
      editingImages = null; imagesFailed = false; renderEditPhotos();
      return apiFetch("/api/v1/catalog/products/" + encodeURIComponent(productId) + "/images").then(function (res) {
        if (editingId !== productId) return null;
        if (!res.ok) { imagesFailed = true; renderEditPhotos(); return null; }
        return res.json().then(function (data) {
          if (editingId !== productId) return;
          editingImages = data && Array.isArray(data.images) ? data.images : [];
          renderEditPhotos();
        });
      }).catch(function (e) { if (editingId === productId && !(isNetError(e) && e.key === "login.expired")) { imagesFailed = true; renderEditPhotos(); } });
    }
    function changeImage(productId, img, body) {
      if (imageBusy) return Promise.resolve();
      imageBusy = true; setMsg("editPhotosNote", null); renderEditPhotos();
      return apiFetch("/api/v1/catalog/products/" + encodeURIComponent(productId) + "/images/" + encodeURIComponent(img.id), {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
      }).then(function (res) {
        if (res.status === 403) { showAccess(); return null; }
        if (!res.ok) return readError(res).then(function (b) { setMsg("editPhotosNote", apiError(res.status, b)); return null; });
        return res.json().then(function (data) {
          if (editingId !== productId) return null;
          editingImages = data && Array.isArray(data.images) ? data.images : editingImages;
          var refs = data && typeof data.reference_count === "number" ? data.reference_count : null;
          setMsg("editPhotosNote", refs === 0 ? { key: "products.photos_no_refs" } : { key: "products.photo_changed" });
          forgetThumb(productId);                                   // миниатюра в списке могла смениться (витринное фото)
          return loadProducts();
        });
      }).catch(function (e) { setMsg("editPhotosNote", netMessage(e, "products.save_failed")); })
        .then(function () { imageBusy = false; if (editingId === productId) renderEditPhotos(); });
    }
    function removeBarcode(code) {
      var id = editingId;
      setMsg("editError", null);
      return apiFetch("/api/v1/catalog/products/" + encodeURIComponent(id), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ remove_barcode: code }) }).then(function (res) {
        if (res.status === 403) { showAccess(); return null; }
        if (!res.ok) return readError(res).then(function (b) { setMsg("editError", apiError(res.status, b)); return null; });
        return res.json().then(function (updated) { editingCodes = updated.barcodes || []; renderEditCodes(); return loadProducts(); });
      }).catch(function (e) { setMsg("editError", netMessage(e, "products.unlink_failed")); });
    }
    function saveEdit() {
      if (!editingId) return Promise.resolve();
      var id = editingId, body = buildEditBody($("editName").value, $("editPrice").value, $("editBarcode").value);
      if (editingVersion !== null) body.expected_version = editingVersion;   // товар успели изменить — сервер ответит 409, а не перезапишет
      if ($("editInStock")) {
        var values = { in_stock: $("editInStock").value };
        Object.keys(DETAIL_INPUTS).forEach(function (code) { values[code] = $(DETAIL_INPUTS[code]) ? $(DETAIL_INPUTS[code]).value : ""; });
        Object.assign(body, buildDetailsBody(values, editingDetails));
      }
      setMsg("editError", null); $("editSave").disabled = true;
      return apiFetch("/api/v1/catalog/products/" + encodeURIComponent(id), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(function (res) {
        if (res.status === 403) { showAccess(); return null; }
        if (!res.ok) return readError(res).then(function (b) { setMsg("editError", apiError(res.status, b)); return null; });
        closeEdit(); setMsg("listNote", { key: "products.saved" }, "ok");
        return loadProducts();
      }).catch(function (e) { setMsg("editError", netMessage(e, "products.save_failed")); })
        .then(function () { $("editSave").disabled = false; });
    }

    // Архив (миграция 0006): товар не удаляется — перестаёт узнаваться и не виден покупателю; можно вернуть.
    function toggleArchive() {
      if (!editingId) return Promise.resolve();
      var id = editingId, next = editingStatus === "archived" ? "active" : "archived";
      if (next === "archived" && win.confirm && !win.confirm(t("products.archive_confirm", { name: editingName }))) return Promise.resolve();
      var body = { status: next };
      if (editingVersion !== null) body.expected_version = editingVersion;
      setMsg("editError", null); $("editArchive").disabled = true;
      return apiFetch("/api/v1/catalog/products/" + encodeURIComponent(id) + "/status", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(function (res) {
        if (res.status === 403) { showAccess(); return null; }
        if (!res.ok) return readError(res).then(function (b) { setMsg("editError", apiError(res.status, b)); return null; });
        closeEdit(); setMsg("listNote", { key: next === "archived" ? "products.archived_done" : "products.restored_done" }, "ok");
        return loadProducts();
      }).catch(function (e) { setMsg("editError", netMessage(e, "products.save_failed")); })
        .then(function () { $("editArchive").disabled = false; });
    }
    function switchView() {
      view = view === "archived" ? "active" : "archived";
      closeEdit(); setMsg("listNote", null); relabel();
      loaded = false; renderListState();
      return loadProducts();
    }

    // ----- вход, доступ, запуск
    function resetViews() { show("accessBanner", false); show("mainArea", false); }
    // Вход — на единой странице login.html (редизайн 2026-11-15): без сеанса или после его окончания — туда, с возвратом сюда.
    function showLogin(messageKey) {
      resetViews();
      auth.goLogin(messageKey === "login.expired" ? "expired" : "");
    }
    function init() {
      $("footerApiBase").textContent = apiBase();
      if (!getToken()) { showLogin(""); return Promise.resolve(); }
      return apiFetch("/api/v1/auth/me").then(function (res) {
        if (!res.ok) { showLogin("login.check_failed"); return null; }
        return res.json();
      }, function (e) { if (getToken()) showLogin(isNetError(e) ? e.key : "login.check_failed"); return null; }).then(function (me) {
        if (!me) return undefined;
        var perms = Array.isArray(me.permissions) ? me.permissions : [];
        if (perms.indexOf("catalog:write") === -1) { showAccess(me.role === "admin" && me.admin_listener === false); return undefined; }
        resetViews(); show("mainArea", true);
        refreshForm(); renderProgress(); renderListState();
        var code = barcodeFromSearch((win.location || {}).search);                 // со страницы «Узнать товар»: «Добавить товар со штрих-кодом»
        if (code) { $("barcodeInput").value = code; $("moreDetails").setAttribute("open", ""); openAddPanel(true); }
        else if ((win.location || {}).hash === "#add") openAddPanel(true);       // пункт меню «Добавить товар»
        var missing = win.Catalog ? win.Catalog.missingFromSearch((win.location || {}).search) : "";   // из подсказок помощника: «Открыть товары» без фото / цены / штрих-кода
        if (missing) { var mf = {}; mf[missing] = true; list.state.filters = Object.assign(win.Catalog.emptyFilters(), mf); list.refresh(); }
        return loadProducts();
      }, function (e) { if (!isNetError(e)) throw e; });
    }

    // ----- постоянные части: значки, подписи на языке приложения
    function relabel() {
      label($("openAdd"), "plus", t("products.add"));
      label($("pickBtn"), "camera", t("products.pick_photos"));
      label($("submitBtn"), null, t("products.add"));
      label($("editAddPhotos"), "camera", t("products.add_photos"));
      label($("editArchive"), null, t(editingStatus === "archived" ? "products.restore" : "products.archive"));
      label($("listViewBtn"), null, t(view === "archived" ? "products.show_catalog" : "products.show_archive"));
      $("listTitle").textContent = t(view === "archived" ? "products.show_archive" : "products.list_title");
      var mode = Theme.get(), theme = $("themeBtn");
      clear(theme);
      theme.appendChild(Icons.make(doc, mode === "dark" ? "moon" : "sun", { size: 20 }));
      theme.setAttribute("aria-label", t("theme.label", { mode: t("theme." + mode) }));
      doc.title = t("nav.products") + " — " + t("app.name");
    }
    Array.prototype.forEach.call(doc.querySelectorAll("[data-icon]"), function (host) { host.appendChild(Icons.make(doc, host.getAttribute("data-icon"), { size: 22 })); });
    I18N.mountSwitcher($("langHost"));
    I18N.apply();
    $("rail").setAttribute("aria-label", t("nav.menu"));
    relabel();
    I18N.onChange(function () {
      relabel(); refreshForm(); renderProgress(); renderDuplicate(); renderEditCodes(); renderEditTitle(); paintAllMsgs(); list.refresh();
      if (editingId) renderEditPhotos();
      $("rail").setAttribute("aria-label", t("nav.menu"));
    });

    $("openAdd").addEventListener("click", function () { openAddPanel(true); scrollTo("addCard"); });
    if (win.addEventListener) win.addEventListener("hashchange", function () {      // «Добавить товар» в меню, когда страница уже открыта
      if (win.location.hash === "#add" && !$("mainArea").hasAttribute("hidden")) { openAddPanel(true); scrollTo("addCard"); }
    });
    $("addCancel").addEventListener("click", function () { openAddPanel(false); });
    $("pickBtn").addEventListener("click", function () { setMsg("formError", null); $("photoInput").click(); });
    $("photoInput").addEventListener("change", guard(function () { var input = $("photoInput"); return addChosen(input.files).then(function () { input.value = ""; }); }));
    $("nameInput").addEventListener("input", function () { hideDuplicate(); refreshForm(); });
    $("submitBtn").addEventListener("click", guard(submitNew));
    $("dupYes").addEventListener("click", guard(function () {
      var target = addPhotosFor; if (!target) return Promise.resolve();
      hideDuplicate();
      return sendPhotosTo(target, chosen.slice());
    }));
    $("dupNo").addEventListener("click", function () {
      hideDuplicate(); addPhotosFor = null;
      setMsg("formError", { key: "products.dup_rename" });
      var n = $("nameInput"); if (n.focus) n.focus();
    });
    $("againBtn").addEventListener("click", resetForm);
    $("failBack").addEventListener("click", function () { backToForm(); refreshForm(); });
    $("slowCheck").addEventListener("click", guard(function () { return lastTask ? waitForTask(lastTask.taskId, lastTask.ctx) : Promise.resolve(); }));
    $("editAddPhotos").addEventListener("click", function () { if (!editingId) return; addPhotosFor = { id: editingId, name: editingName }; $("addPhotoInput").click(); });
    $("addPhotoInput").addEventListener("change", guard(function () {
      var input = $("addPhotoInput"), target = addPhotosFor, files = Array.prototype.slice.call(input.files || []);
      input.value = "";
      if (!target || !files.length) return Promise.resolve();
      setMsg("listNote", null);
      var photos = [], index = 0;
      function next() {
        if (index >= Math.min(files.length, MAX_PHOTOS)) return sendPhotosTo(target, photos);
        var file = files[index], i = index++;
        return shrinkPhoto(file).then(function (blob) { photos.push({ blob: blob, name: photoName(file, i), url: "" }); return next(); });
      }
      return next();
    }));
    $("editSave").addEventListener("click", guard(saveEdit));
    $("editCancel").addEventListener("click", closeEdit);
    $("editArchive").addEventListener("click", guard(toggleArchive));
    $("listViewBtn").addEventListener("click", guard(switchView));
    $("themeBtn").addEventListener("click", function () { Theme.next(); relabel(); });
    if (win.addEventListener) win.addEventListener("pagehide", function () { Object.keys(thumbCache).forEach(forgetThumb); forgetImages(); });
    syncPanels(); renderDuplicate(); paintAllMsgs();

    return {
      ready: init(),
      state: function () { return { chosen: chosen.length, busy: busy, items: items.length, editingId: editingId, progress: progress && progress.type, addOpen: addOpen, view: view }; }
    };
  }

  return {
    start: start, fitSize: fitSize, photosHint: photosHint, submitState: submitState, describeApiError: describeApiError, pollDelay: pollDelay,
    barcodeFromSearch: barcodeFromSearch, buildEditBody: buildEditBody, buildDetailsBody: buildDetailsBody, stockValue: stockValue, photoName: photoName,
    MAX_PHOTOS: MAX_PHOTOS, GOOD_PHOTOS: GOOD_PHOTOS, MAX_SIDE: MAX_SIDE, PAGE_SIZE: PAGE_SIZE, MAX_ITEMS: MAX_ITEMS, TUNING: TUNING
  };
});
