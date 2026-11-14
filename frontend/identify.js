/* Экран «Узнать товар» (identify.html): камера, запрос к серверу, ценник, вход. Логика вынесена из страницы в модуль, как у витрины
   (preview.js): страница только подключает файлы и вызывает Identify.start(window) — поэтому всё проверяется на настоящей разметке
   (tools/identify_smoke_test.js). Чистые функции (когда отправлять кадр и др.) перенесены из прежней страницы без изменений.
   Тексты — только через I18N.t, ответ рисует Ticket, вход и токен ведёт общий модуль Auth (auth.js). Данные в разметку строкой не подставляются:
   только textContent / setAttribute. Сервер: POST /api/v1/catalog/identify -> {status: matched | uncertain | unknown | empty_catalog, product, candidates}. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Identify = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  var FRAME_SIDE = 1280, THUMB_W = 32, THUMB_H = 24;     // 1280: на кадре поменьше мелкий штрих-код не прочитать
  // still / changed — средняя разница яркости (0–255): «картинка замерла» и «показали другое»; tickMs — как часто смотрим на камеру
  var TUNING = { still: 4, changed: 9, tickMs: 1200 };
  var SETTINGS_KEY = "identify_settings";
  var HISTORY_MAX = 8;
  var NO_BRAND = "Не указан";                            // так сервер подписывает товар без бренда

  // ---------------------------------------------------------------- чистая логика
  function cleanSettings(raw) {
    var out = { auto: true };                                 // старые записи с полями speak/price (озвучки больше нет) просто игнорируются
    if (raw && typeof raw === "object" && typeof raw.auto === "boolean") out.auto = raw.auto;
    return out;
  }
  function fitSize(width, height, max) {
    var longest = Math.max(width, height);
    if (longest <= max) return { w: width, h: height };
    var k = max / longest;
    return { w: Math.max(1, Math.round(width * k)), h: Math.max(1, Math.round(height * k)) };
  }
  function frameDiff(a, b) {
    if (!a || !b || a.length !== b.length || a.length === 0) return Infinity;
    var sum = 0;
    for (var i = 0; i < a.length; i++) sum += Math.abs(a[i] - b[i]);
    return sum / a.length;
  }
  // Отправлять кадр, когда картинка замерла (рука убралась) И отличается от уже отправленной: так не распознаём «руку в движении» и не шлём
  // одно и то же. prevTick — прошлый кадр с таймера, lastSent — кадр, по которому уже спрашивали (null — ещё не спрашивали).
  function shouldSend(prevTick, current, lastSent, tuning) {
    var k = tuning || TUNING;
    if (frameDiff(prevTick, current) > k.still) return false;
    return lastSent === null || frameDiff(lastSent, current) > k.changed;
  }
  function ticketProduct(p) {
    var brand = p && typeof p.brand === "string" && p.brand !== NO_BRAND ? p.brand : "";
    return {
      id: p.id, name: p.name || "", brand: brand, expected_price: p.expected_price === undefined ? null : p.expected_price,
      category: typeof p.category === "string" ? p.category : "", fields: Array.isArray(p.fields) ? p.fields : []
    };
  }
  /** Ответ сервера -> то, что рисует Ticket (логическое состояние; тексты подставляются при рисовании). */
  function viewFor(result) {
    var status = result && result.status, product = result && result.product;
    if (status === "matched" && product) return { state: "matched", product: ticketProduct(product) };
    if (status === "uncertain" && product) {
      var list = [ticketProduct(product)];
      (result.candidates || []).forEach(function (c) { if (c && c.id !== product.id) list.push(ticketProduct(c)); });
      return { state: "uncertain", product: list[0], candidates: list };
    }
    if (status === "empty_catalog") return { state: "empty_catalog" };
    if (status === "not_in_catalog" && result.guess && result.guess.name) {
      return { state: "not_in_catalog", guess: { name: String(result.guess.name), brand: String(result.guess.brand || "") } };
    }
    return { state: "unknown" };
  }
  /** Ключ текста про камеру. secure === false — страница открыта не по https и не как localhost: камеру браузер не даст. */
  function cameraErrorKey(err, secure) {
    if (secure === false) return "error.camera_insecure";
    var name = err && err.name;
    if (name === "NotAllowedError" || name === "PermissionDeniedError") return "error.camera_denied";
    if (name === "NotFoundError" || name === "DevicesNotFoundError") return "error.camera_missing";
    if (name === "NotReadableError" || name === "TrackStartError") return "error.camera_busy";
    if (name === "SecurityError") return "error.camera_blocked";
    return "error.camera_failed";
  }
  /** Ошибка сервера -> {key, params} (код ошибки, для которого есть перевод: сообщение на языке приложения), {text} (готовая фраза
      сервера по-русски — если перевода нет) или {key} (наш общий текст). body — всё тело ответа ({detail, code, params}), has(key) —
      есть ли перевод ключа (I18N.has). */
  function describeApiError(status, detail, body, has) {
    if (body && typeof body.code === "string" && /^[a-z_]+$/.test(body.code) && has && has("server." + body.code)) {
      return { key: "server." + body.code, params: body.params && typeof body.params === "object" ? body.params : {} };
    }
    if (typeof detail === "string" && detail) return { text: detail };
    if (detail && typeof detail === "object" && typeof detail.message === "string") return { text: detail.message };
    if (status === 504) return { key: "error.timeout" };
    if (status >= 500) return { key: "error.recognize_failed" };
    return { key: "error.generic" };
  }
  function timeLabel(date) { var p = function (n) { return String(n).padStart(2, "0"); }; return p(date.getHours()) + ":" + p(date.getMinutes()) + ":" + p(date.getSeconds()); }

  // ---------------------------------------------------------------- страница
  function start(win, options) {
    options = options || {};
    var doc = win.document, I18N = win.I18N, Auth = win.Auth, Ticket = win.Ticket, Icons = win.Icons, Theme = win.Theme;
    var tuning = Object.assign({}, TUNING, options.tuning || {});
    var $ = function (id) { return doc.getElementById(id); };
    var t = function (key, params) { return I18N.t(key, params); };
    // Обработчики событий не должны ронять страницу, но и молчать об ошибке нельзя: она уходит в options.onError (проверки) или в консоль.
    var report = options.onError || function (e) { if (win.console && win.console.error) win.console.error(e); };
    var guard = function (fn) { return function () { var r = fn.apply(null, arguments); if (r && r.catch) r.catch(report); return r; }; };

    // ----- состояние
    var stream = null, tickTimer = null, inflight = false;
    var prevTick = null, lastSent = null;
    var canAdd = false;
    var lastView = { state: "idle" }, lastResult = null;
    var linkTarget = null, barcodeInfo = null, linkedDone = null, errorState = null, loginState = null;
    var history = [];

    // ----- вход и доступ к серверу (токен, адрес и запрос входа — в общем модуле Auth)
    var auth = Auth.create(win);
    var getToken = auth.getToken, setToken = auth.setToken, authHeaders = auth.authHeaders, apiBase = auth.apiBase;
    function request(url, opts) { return win.fetch(url, opts); }

    // ----- мелкие помощники разметки
    function setHidden(el, hidden) { if (hidden) el.setAttribute("hidden", ""); else el.removeAttribute("hidden"); }
    function show(id, on) { setHidden($(id), !on); }
    function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
    function label(button, iconName, text) {
      clear(button);
      if (iconName) button.appendChild(Icons.make(doc, iconName, { size: 20 }));
      var span = doc.createElement("span"); span.textContent = text; button.appendChild(span);
    }

    // ----- настройки (запоминаются на этом устройстве)
    function readSettings() {
      try { return cleanSettings(JSON.parse(win.localStorage.getItem(SETTINGS_KEY) || "null")); } catch (e) { return cleanSettings(null); }
    }
    function currentSettings() { return { auto: !!$("autoToggle").checked }; }
    function storeSettings() { try { win.localStorage.setItem(SETTINGS_KEY, JSON.stringify(currentSettings())); } catch (e) { /* без запоминания — начнёт с умолчаний */ } }
    function applySettings(cfg) { $("autoToggle").checked = cfg.auto; }

    // ----- ценник
    function builtView() {
      var v = Object.assign({}, lastView);
      if (v.messageKey) v.message = t(v.messageKey);
      return v;
    }
    function renderTicket() {
      Ticket.render(doc, $("ticket"), builtView(), deps);
      $("stage").setAttribute("data-state", lastView.state);
    }
    function setTicket(view) { lastView = view; renderTicket(); }
    function scrollToTicket() {
      var box = $("ticket");                       // на телефоне ответ ниже камеры: сам подъезжает в поле зрения
      if (!box.scrollIntoView) return;
      var calm = win.matchMedia && win.matchMedia("(prefers-reduced-motion: reduce)").matches;
      box.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "nearest" });
    }
    function pick(candidate) {
      setTicket({ state: "matched", product: candidate });
      pushHistory({ status: "matched", product: candidate });
    }
    function wrong() {
      var all = lastResult && Array.isArray(lastResult.candidates) ? lastResult.candidates.filter(Boolean) : [];
      var current = lastView.product;
      var others = current ? all.filter(function (c) { return c.id !== current.id; }) : all;
      if (others.length) setTicket({ state: "uncertain", product: ticketProduct(others[0]), candidates: others.map(ticketProduct) });
      else setTicket({ state: "unknown" });
    }
    function retry() {
      if (lastView.state === "camera_error") { startCamera(); return; }
      if (stream) snapNow(); else $("fileInput").click();
    }
    function goAdd() {
      var code = barcodeInfo && barcodeInfo.code && !barcodeInfo.known ? "?barcode=" + encodeURIComponent(barcodeInfo.code) : "";
      win.location.href = "products.html" + code;
    }
    var deps = {
      t: t, icons: Icons,
      onPick: pick, onWrong: wrong, onRetry: retry, onPhoto: function () { $("fileInput").click(); },
      onMore: function () { /* характеристики появятся вместе с гибкой карточкой товара */ }
    };
    function syncAddHandler() { if (canAdd) deps.onAdd = goAdd; else delete deps.onAdd; }

    // ----- строки под ценником: штрих-код, ошибка, история
    function renderBarcode() {
      var note = $("barcodeNote"), btn = $("linkBtn"), text = "";
      if (linkedDone) text = t("barcode.linked", linkedDone);
      else if (barcodeInfo) text = t(barcodeInfo.known ? "barcode.code" : "barcode.unlinked", { code: barcodeInfo.code });
      note.textContent = text; setHidden(note, !text);
      if (linkTarget && !linkedDone) { btn.textContent = t("barcode.link", { name: linkTarget.name }); setHidden(btn, false); } else setHidden(btn, true);
    }
    function renderError() {
      var el = $("resError"), text = "";
      if (errorState) text = errorState.text || t(errorState.key, errorState.params);
      el.textContent = text; setHidden(el, !text);
    }
    function fail(state) { errorState = state; renderError(); renderTicket(); }
    function renderHistory() {
      var list = $("histList");
      clear(list);
      history.forEach(function (h) {
        var li = doc.createElement("li");
        var name = doc.createElement("span"); name.textContent = h.maybe ? t("identify.history_maybe", { name: h.name }) : h.name;
        var time = doc.createElement("span"); time.className = "hist__t"; time.textContent = h.time;
        li.appendChild(name); li.appendChild(time); list.appendChild(li);
      });
      show("histEmpty", history.length === 0);
    }
    function pushHistory(result) {
      if (!result.product || (result.status !== "matched" && result.status !== "uncertain")) return;
      history.unshift({ name: result.product.name, maybe: result.status === "uncertain", time: timeLabel(new Date()) });
      if (history.length > HISTORY_MAX) history.length = HISTORY_MAX;
      renderHistory();
    }

    // ----- камера
    function cameraSupported() { var n = win.navigator; return !!(n && n.mediaDevices && typeof n.mediaDevices.getUserMedia === "function"); }
    function setLive(on) {
      $("stage").setAttribute("data-live", on ? "true" : "false");
      setHidden($("video"), !on); setHidden($("camPlaceholder"), on);
      label($("camBtn"), on ? "x" : "viewfinder", t(on ? "identify.camera_off" : "identify.camera_on"));
      $("camBtn").className = on ? "btn btn--secondary" : "btn btn--primary";      // главной всегда становится та кнопка, что сейчас нужна
      $("snapBtn").className = on ? "btn btn--primary" : "btn btn--secondary";
      $("snapBtn").disabled = !on;
    }
    function cameraProblem(key) { setTicket({ state: "camera_error", messageKey: key }); }
    function startCamera() {
      if (!cameraSupported() || win.isSecureContext === false) { cameraProblem(cameraErrorKey(null, win.isSecureContext === false ? false : undefined)); return Promise.resolve(); }
      return win.navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 } }, audio: false }).then(function (s) {
        stream = s;
        var video = $("video"); video.srcObject = stream;
        var played = video.play ? Promise.resolve().then(function () { return video.play(); }).catch(function () { /* автозапуск мог быть уже выполнен */ }) : Promise.resolve();
        return played.then(function () {
          setLive(true);
          if (lastView.state === "camera_error") setTicket({ state: "idle" });
          prevTick = null; lastSent = null;
          scheduleTicks();
        });
      }, function (e) { stream = null; cameraProblem(cameraErrorKey(e, true)); });
    }
    function stopCamera() {
      if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
      if (stream) { stream.getTracks().forEach(function (tr) { tr.stop(); }); stream = null; }
      $("video").srcObject = null;
      setLive(false);
    }
    function drawFrame(maxSide) {
      var video = $("video"), vw = video.videoWidth || 0, vh = video.videoHeight || 0;
      if (!vw || !vh) return null;
      var size = fitSize(vw, vh, maxSide), canvas = $("grab");
      canvas.width = size.w; canvas.height = size.h;
      var ctx = canvas.getContext("2d");
      ctx.drawImage(video, 0, 0, size.w, size.h);
      return { canvas: canvas, ctx: ctx, w: size.w, h: size.h };
    }
    function grabBlob() {
      var frame = drawFrame(FRAME_SIDE);
      if (!frame) return Promise.resolve(null);
      return new Promise(function (resolve) { frame.canvas.toBlob(resolve, "image/jpeg", 0.85); });
    }
    function grabSmall() {                         // крошечная серая копия кадра для сравнения «что-то изменилось?»
      var video = $("video");
      if (!video.videoWidth) return null;
      var canvas = $("grab");
      canvas.width = THUMB_W; canvas.height = THUMB_H;
      var ctx = canvas.getContext("2d");
      ctx.drawImage(video, 0, 0, THUMB_W, THUMB_H);
      var data = ctx.getImageData(0, 0, THUMB_W, THUMB_H).data, gray = new Array(THUMB_W * THUMB_H);
      for (var i = 0; i < gray.length; i++) gray[i] = (data[i * 4] + data[i * 4 + 1] + data[i * 4 + 2]) / 3;
      return gray;
    }
    function scheduleTicks() { if (tickTimer) clearInterval(tickTimer); tickTimer = setInterval(guard(tick), tuning.tickMs); }
    function tick() {
      if (!stream || inflight || !$("autoToggle").checked) return Promise.resolve();
      var current = grabSmall();
      if (!current) return Promise.resolve();
      var previous = prevTick;
      prevTick = current;
      if (previous === null || !shouldSend(previous, current, lastSent, tuning)) return Promise.resolve();
      lastSent = current;
      return grabBlob().then(function (blob) { return blob ? identifyBlob(blob, { manual: false }) : undefined; });
    }

    // ----- распознавание
    function setBusy(on, manual) {
      inflight = on;
      $("stage").setAttribute("data-busy", on ? "true" : "false");
      if (on) {
        $("stage").setAttribute("data-state", "searching");                 // рамки сходятся; ценник заменяем «Ищу…» только когда человек сам попросил
        if (manual) Ticket.render(doc, $("ticket"), { state: "searching" }, deps);
      } else renderTicket();
    }
    function handleResult(result) {
      lastResult = result; errorState = null; linkedDone = null; renderError();
      var bc = result.barcode;
      barcodeInfo = bc && bc.code ? { code: bc.code, known: !!bc.known } : null;
      linkTarget = null;
      if (barcodeInfo && !barcodeInfo.known && canAdd && result.status === "matched" && result.product) {    // привязывать можно только к УВЕРЕННО узнанному товару
        linkTarget = { id: result.product.id, name: result.product.name, code: barcodeInfo.code };
      }
      renderBarcode();
      setHidden($("demoBanner"), !result.demo_mode);
      setTicket(viewFor(result));
      pushHistory(result);
      scrollToTicket();
    }
    function readError(res) { return res.json().then(function (b) { return b && typeof b === "object" ? b : {}; }, function () { return {}; }); }
    function apiError(status, body) { return describeApiError(status, body.detail, body, I18N.has); }
    function identifyBlob(blob, opts) {
      if (inflight) return Promise.resolve();
      var manual = !!(opts && opts.manual);
      setBusy(true, manual); errorState = null; renderError();
      var fd = new win.FormData(); fd.append("file", blob, "frame.jpg");
      return request(apiBase() + "/api/v1/catalog/identify?lang=" + encodeURIComponent(I18N.getLang ? I18N.getLang() : "ru"), { method: "POST", headers: authHeaders(), body: fd }).then(function (res) {
        if (res.status === 401) { setToken(null); stopCamera(); showLogin("login.expired"); return null; }
        if (res.status === 403) { stopCamera(); resetViews(); show("accessBanner", true); return null; }
        if (!res.ok) return readError(res).then(function (b) { fail(apiError(res.status, b)); return null; });
        return res.json().then(function (result) { setBusy(false); handleResult(result); return null; },
          function () { fail({ key: "error.recognize_failed" }); return null; });          // ответ не разобрался — человеку понятная фраза
      }, function () { fail({ key: "error.network" }); return null; })
        .then(function () { if (inflight) setBusy(false); }, function (e) { if (inflight) setBusy(false); throw e; });
    }
    function snapNow() {
      if (!stream || inflight) return Promise.resolve();
      return grabBlob().then(function (blob) {
        if (!blob) return undefined;
        lastSent = grabSmall() || lastSent;
        return identifyBlob(blob, { manual: true });
      });
    }
    function fromFile() {
      var input = $("fileInput"), file = input.files && input.files[0];
      input.value = "";
      if (!file) return Promise.resolve();
      var prepared = typeof win.createImageBitmap === "function"
        ? win.createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bitmap) {
          var size = fitSize(bitmap.width, bitmap.height, FRAME_SIDE), canvas = $("grab");
          canvas.width = size.w; canvas.height = size.h; canvas.getContext("2d").drawImage(bitmap, 0, 0, size.w, size.h);
          if (bitmap.close) bitmap.close();
          return new Promise(function (resolve) { canvas.toBlob(resolve, "image/jpeg", 0.85); });
        }).then(function (b) { return b || file; }, function () { return file; })     // не вышло уменьшить — отправляем как есть, сервер проверит
        : Promise.resolve(file);
      return prepared.then(function (blob) { return identifyBlob(blob, { manual: true }); });
    }
    function linkBarcode() {
      var target = linkTarget;
      if (!target) return Promise.resolve();
      var btn = $("linkBtn"); btn.disabled = true;
      return request(apiBase() + "/api/v1/catalog/products/" + encodeURIComponent(target.id), {
        method: "PATCH", headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()), body: JSON.stringify({ add_barcode: target.code })
      }).then(function (res) {
        if (res.status === 401) { setToken(null); stopCamera(); showLogin("login.expired"); return null; }
        if (!res.ok) return readError(res).then(function (b) { errorState = apiError(res.status, b); renderError(); return null; });
        linkedDone = { code: target.code, name: target.name }; linkTarget = null; errorState = null; renderError(); renderBarcode();
        return null;
      }, function () { errorState = { key: "error.network" }; renderError(); return null; }).then(function () { btn.disabled = false; });
    }

    // ----- вход, доступ, запуск
    function resetViews() { show("loginPanel", false); show("accessBanner", false); show("mainArea", false); }
    function renderLoginError() {
      var err = $("loginError"), text = loginState ? t(loginState.key, loginState.params) : "";
      err.textContent = text; setHidden(err, !text);
    }
    function showLogin(messageKey) {
      resetViews(); show("loginPanel", true);
      loginState = messageKey ? { key: messageKey } : null; renderLoginError();
      var u = $("loginUser"); if (u && u.focus) u.focus();
    }
    // Второй фактор (документ 18): поле кода появляется, когда сервер попросил код после верного пароля.
    function codeField(on) {
      var f = $("loginCodeField"), c = $("loginCode");
      if (f) { if (on) f.removeAttribute("hidden"); else f.setAttribute("hidden", ""); }
      if (c) { if (!on) c.value = ""; else if (c.focus) c.focus(); }
    }
    function doLogin(evt) {
      if (evt && evt.preventDefault) evt.preventDefault();
      var btn = $("loginBtn");
      loginState = null; renderLoginError(); btn.disabled = true;
      function problem(key, params) { loginState = { key: key, params: params }; renderLoginError(); }
      return auth.login($("loginUser").value, $("loginPass").value, ($("loginCode") || {}).value || "").then(function (r) {
        codeField(!!(r && r.needCode));
        if (r && (r.needEnroll || r.needPasswordChange)) { win.location.assign("settings.html"); return null; }   // и временный пароль: смена — тоже в «Настройках»     // администратор без 2FA: настройка — в «Настройках»
        if (!r.ok) { problem(r.key, r.params); return null; }
        $("loginPass").value = ""; return init();
      }).catch(function (e) { problem("login.failed", { reason: e.message }); }).then(function () { btn.disabled = false; });
    }
    function init() {
      $("footerApiBase").textContent = apiBase();
      if (!getToken()) { showLogin(""); return Promise.resolve(); }
      return request(apiBase() + "/api/v1/auth/me", { headers: authHeaders() }).then(function (res) {
        if (res.status === 401) { setToken(null); showLogin("login.expired"); return null; }
        if (!res.ok) { showLogin("login.check_failed"); return null; }
        return res.json();
      }, function () { showLogin("error.network"); return null; }).then(function (me) {
        if (!me) return;
        var perms = Array.isArray(me.permissions) ? me.permissions : [];
        if (perms.indexOf("catalog:read") === -1) { resetViews(); show("accessBanner", true); return; }
        canAdd = perms.indexOf("catalog:write") !== -1;
        syncAddHandler();
        setHidden($("railProducts"), !canAdd);
        setHidden($("railAdmin"), perms.indexOf("store:manage") === -1);
        resetViews(); show("mainArea", true);
        applySettings(readSettings());
        if (!cameraSupported()) cameraProblem(cameraErrorKey(null, win.isSecureContext === false ? false : undefined));
        else renderTicket();
      });
    }

    // ----- постоянные части: значки, переключатели, подписи на языке приложения
    function relabel() {
      var on = !!stream;
      label($("camBtn"), on ? "x" : "viewfinder", t(on ? "identify.camera_off" : "identify.camera_on"));
      label($("snapBtn"), null, t("identify.snap"));
      label($("fileBtn"), "camera", t("identify.take_photo"));
      var mode = Theme.get(), theme = $("themeBtn");
      clear(theme);
      theme.appendChild(Icons.make(doc, mode === "dark" ? "moon" : mode === "light" ? "sun" : "monitor", { size: 20 }));
      theme.setAttribute("aria-label", t("theme.label", { mode: t("theme." + mode) }));
      doc.title = t("app.name");
    }
    Array.prototype.forEach.call(doc.querySelectorAll("[data-icon]"), function (host) { host.appendChild(Icons.make(doc, host.getAttribute("data-icon"), { size: 22 })); });
    I18N.mountSwitcher($("langHost"));
    I18N.apply();
    $("rail").setAttribute("aria-label", t("nav.menu"));
    relabel();
    setLive(false);
    I18N.onChange(function () {
      relabel(); renderTicket(); renderBarcode(); renderError(); renderLoginError(); renderHistory();
      $("rail").setAttribute("aria-label", t("nav.menu"));
    });

    $("loginPanel").addEventListener("submit", guard(doLogin));
    $("camBtn").addEventListener("click", function () { if (stream) stopCamera(); else startCamera(); });
    $("snapBtn").addEventListener("click", guard(snapNow));
    $("fileBtn").addEventListener("click", function () { $("fileInput").click(); });
    $("fileInput").addEventListener("change", guard(fromFile));
    $("autoToggle").addEventListener("change", storeSettings);
    $("linkBtn").addEventListener("click", guard(linkBarcode));
    $("themeBtn").addEventListener("click", function () { Theme.next(); relabel(); });
    if (win.addEventListener) win.addEventListener("pagehide", stopCamera);
    renderHistory(); renderBarcode(); renderError();

    return {
      ready: init(),
      state: function () { return { view: lastView, inflight: inflight, canAdd: canAdd, live: !!stream, history: history.slice() }; }
    };
  }

  return {
    start: start, cleanSettings: cleanSettings, fitSize: fitSize, frameDiff: frameDiff, shouldSend: shouldSend,
    viewFor: viewFor, ticketProduct: ticketProduct, cameraErrorKey: cameraErrorKey, describeApiError: describeApiError,
    timeLabel: timeLabel, TUNING: TUNING, SETTINGS_KEY: SETTINGS_KEY, HISTORY_MAX: HISTORY_MAX, FRAME_SIDE: FRAME_SIDE
  };
});
