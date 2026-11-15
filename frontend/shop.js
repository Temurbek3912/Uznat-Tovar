/* Страница покупателя «Узнать товар» (QR-код магазина, без входа; документ 04, §1). Сфотографировать товар -> карточка.
   Кадр никуда, кроме сервера магазина, не уходит и на телефоне не сохраняется: снимок уменьшается в памяти (canvas) и отправляется
   телом запроса в POST /api/v1/public/identify; предпросмотр — временная ссылка blob:, которая освобождается при следующем снимке.
   Данные в разметку не подставляются строкой: карточку рисует ticket.js (только textContent). Чистые функции — Shop.helpers
   (проверяются tools/shop_smoke_test.js). */
(function (root) {
  "use strict";

  var MAX_SIDE = 1600;            // длинная сторона снимка перед отправкой: штрихкод читается, а передача быстрая в Wi-Fi магазина
  var JPEG_QUALITY = 0.85;
  var MAX_SEND_BYTES = 9 * 1024 * 1024;   // сервер принимает до 10 МБ

  // Тексты карточки для покупателя: где карточка сотрудника говорит «добавьте в каталог», покупателю — «спросите сотрудника».
  var CUSTOMER_TEXT = {
    "ticket.not_found_title": "shop.not_found_title",
    "ticket.not_found_hint_readonly": "shop.not_found_hint",
    "ticket.empty_hint_readonly": "shop.not_found_hint",
    "ticket.idle_hint": "shop.idle_hint",
    "identify.show_to_camera": "shop.idle_title",
    "ticket.retake": "shop.retake",
    "ticket.wrong": "shop.wrong",
  };

  /** Ответ сервера -> вид карточки ticket.js. Цена у покупателя — поле price; карточка ждёт expected_price. */
  function toView(body) {
    var card = function (p) { return p ? { name: p.name, brand: p.brand, category: p.category, expected_price: p.price } : null; };
    var status = body && body.status;
    if (status === "matched" && body.product) return { state: "matched", product: card(body.product) };
    if (status === "uncertain" && body.product) {
      var list = (Array.isArray(body.candidates) && body.candidates.length ? body.candidates : [body.product]).map(card);
      return { state: "uncertain", product: card(body.product), candidates: list };
    }
    if (status === "not_in_catalog" && body.guess && (body.guess.name || body.guess.kind)) {
      // название товара вне базы или хотя бы его вид («Это напиток», документ 07, §10.7)
      return { state: "not_in_catalog", guess: { name: String(body.guess.name || ""), brand: String(body.guess.brand || ""), kind: String(body.guess.kind || "") } };
    }
    return { state: "unknown" };
  }

  /** Ошибка ответа -> ключ текста (с параметрами). Технических подробностей покупателю не показываем. */
  function errorKey(status, body) {
    var code = body && typeof body.code === "string" ? body.code : "";
    if (status === 429 || code === "public_rate_limited") {
      var wait = body && body.params && Number(body.params.retry_after);
      return { key: "shop.err_rate", params: { seconds: wait > 0 ? Math.ceil(wait) : 30 } };
    }
    if (status === 413 || code === "frame_too_large") return { key: "shop.err_too_large" };
    if (status === 422 || status === 415) return { key: "shop.err_photo" };
    if (status === 0) return { key: "shop.err_network" };
    return { key: "shop.err_unavailable" };
  }

  /** Размер после уменьшения: длинная сторона не больше max, пропорции сохраняются, маленькие снимки не увеличиваются. */
  function fitSize(width, height, max) {
    var w = Math.max(1, Math.round(width || 1)), h = Math.max(1, Math.round(height || 1));
    var scale = Math.min(1, max / Math.max(w, h));
    return { width: Math.max(1, Math.round(w * scale)), height: Math.max(1, Math.round(h * scale)) };
  }

  function start(win) {
    var doc = win.document, I18N = win.I18N, Ticket = win.Ticket, Icons = win.Icons;
    var $ = function (id) { return doc.getElementById(id); };
    var t = function (key, params) {
      var mapped = CUSTOMER_TEXT[key];
      return I18N.t(mapped && I18N.has(mapped) ? mapped : key, params);
    };
    var state = { view: { state: "idle" }, busy: false, previewUrl: null, lastFile: null };

    function draw() {
      Ticket.render(doc, $("ticketHost"), state.view, {
        t: t, icons: Icons,
        onPick: function (c) { state.view = { state: "matched", product: c }; draw(); },
        onRetry: function () { $("cameraInput").click(); },
        onPhoto: function () { $("cameraInput").click(); },
        onWrong: function () { state.view = { state: "unknown" }; draw(); },
      });
    }

    function showPreview(file) {
      if (state.previewUrl && win.URL && win.URL.revokeObjectURL) win.URL.revokeObjectURL(state.previewUrl);
      state.previewUrl = win.URL && win.URL.createObjectURL ? win.URL.createObjectURL(file) : null;
      if (state.previewUrl) { $("preview").src = state.previewUrl; $("previewBox").hidden = false; }
    }

    // Уменьшение в памяти: canvas -> JPEG Blob. Не получилось (старый браузер, необычный формат) — отправляем как есть.
    function shrink(file) {
      return new Promise(function (resolve) {
        if (!win.createImageBitmap || !doc.createElement("canvas").toBlob) { resolve(file); return; }
        win.createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bitmap) {
          var size = fitSize(bitmap.width, bitmap.height, MAX_SIDE);
          var canvas = doc.createElement("canvas");
          canvas.width = size.width; canvas.height = size.height;
          canvas.getContext("2d").drawImage(bitmap, 0, 0, size.width, size.height);
          if (bitmap.close) bitmap.close();
          canvas.toBlob(function (blob) { resolve(blob || file); }, "image/jpeg", JPEG_QUALITY);
        }).catch(function () { resolve(file); });
      });
    }

    function fail(err) {
      state.view = { state: "error", message: t(err.key, err.params) };
      draw();
    }

    function send(file) {
      if (state.busy || !file) return Promise.resolve();
      state.busy = true;
      state.lastFile = file;
      showPreview(file);
      state.view = { state: "searching" };
      draw();
      return shrink(file).then(function (blob) {
        if (blob.size > MAX_SEND_BYTES) { fail({ key: "shop.err_too_large" }); return; }
        var lang = I18N.getLang ? I18N.getLang() : "ru";
        return win.fetch("/api/v1/public/identify?lang=" + encodeURIComponent(lang), {
          method: "POST", body: blob, headers: { "Content-Type": blob.type || "image/jpeg" }, credentials: "omit", cache: "no-store",
        }).then(function (res) {
          return res.json().catch(function () { return null; }).then(function (body) {
            if (!res.ok) { fail(errorKey(res.status, body)); return; }
            state.view = toView(body);
            draw();
          });
        }, function () { fail(errorKey(0, null)); });
      }).then(function () { state.busy = false; }, function () { state.busy = false; fail(errorKey(0, null)); });
    }

    function onChosen(input) {
      var file = input.files && input.files[0];
      input.value = "";                                  // тот же снимок можно выбрать снова
      return send(file);
    }

    Array.prototype.forEach.call(doc.querySelectorAll("[data-icon]"), function (host) {
      if (Icons) host.appendChild(Icons.make(doc, host.getAttribute("data-icon"), { size: 22 }));
    });
    I18N.mountSwitcher($("langHost"));
    I18N.apply();
    I18N.onChange(function () { draw(); });
    $("cameraInput").addEventListener("change", function () { onChosen($("cameraInput")); });
    $("fileInput").addEventListener("change", function () { onChosen($("fileInput")); });
    draw();

    win.fetch("/api/v1/public/store", { credentials: "omit" }).then(function (res) { return res.ok ? res.json() : null; })
      .then(function (info) {
        if (info && info.name) { $("storeName").textContent = info.name; $("storeName").hidden = false; }
      }, function () { /* без названия магазина страница работает так же */ });

    return { send: send, state: state, draw: draw };
  }

  var api = { start: start, helpers: { toView: toView, errorKey: errorKey, fitSize: fitSize, CUSTOMER_TEXT: CUSTOMER_TEXT } };
  root.Shop = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
