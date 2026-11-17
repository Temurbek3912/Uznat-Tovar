/* Страница «Помощник» (assistant.html): текстовый чат владельца с ИИ-помощником (документ 16).
   Этап A1 — смотрит и считает. Этап A2 — ПРЕДЛАГАЕТ изменение карточкой «Сделать / Отмена»: база меняется только нажатием
   владельца (POST /assistant/act — те же проверки, что на странице «Товары»), после — «Отменить». Язык и фон помощник меняет
   сразу (это настройки этого браузера). Переписка хранится только в этой вкладке (sessionStorage) и уходит на сервер последними
   репликами; сервер её не сохраняет. Голоса нет (документ 16, §1.0). Текст — только textContent: разметка из ответа модели
   не исполняется. */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Assistant = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  var STORE_KEY = "assistant_chat";
  var MAX_KEPT = 40;           // реплик в памяти вкладки
  var MAX_SENT = 8;            // реплик уходит на сервер (сервер всё равно возьмёт не больше)
  var QUESTION_RESERVE = 100;  // символов памяти оставить новому вопросу (обычный вопрос короче)
  var BUSY_POLL_MS = 30000;    // как часто узнавать, заняты ли покупатели распознаванием
  var EXAMPLES = ["assistant.example_scans", "assistant.example_photo", "assistant.example_dupes", "assistant.example_price", "assistant.example_theme"];
  var CARD_STATES = ["ask", "working", "done", "cancelled", "undone", "error"];
  var KINDS = ["update", "status", "add"];
  // инструменты-действия: в строке «Смотрел: …» не показываются (их видно по карточке и по строке «Сделано»)
  var ACTION_TOOLS = ["propose_update", "propose_archive", "propose_add", "set_language", "set_theme"];

  function isObj(x) { return !!x && typeof x === "object" && !Array.isArray(x); }

  /** Карточка предложения из памяти вкладки -> проверенная копия или null. «Выполняется» после перезагрузки — снова вопрос
      (двойного изменения не будет: сервер сверяет версию товара, повтор названия не пропустит). */
  function cleanCard(card) {
    if (!isObj(card) || !isObj(card.action) || KINDS.indexOf(card.action.kind) === -1) return null;
    var state = CARD_STATES.indexOf(card.state) === -1 || card.state === "working" ? "ask" : card.state;
    return { action: card.action, state: state, undo: isObj(card.undo) ? card.undo : null,
             error: typeof card.error === "string" ? card.error : "", note: typeof card.note === "string" ? card.note : "" };
  }

  function cleanUi(list) {
    return (Array.isArray(list) ? list : []).filter(function (u) {
      return isObj(u) && (u.kind === "lang" || u.kind === "theme") && typeof u.value === "string";
    });
  }

  /** Реплики из памяти вкладки -> только {role: user|assistant, content: строка, ...}, не больше MAX_KEPT. */
  function cleanLog(raw) {
    if (!Array.isArray(raw)) return [];
    return raw.filter(function (m) {
      return m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string" && m.content.trim();
    }).map(function (m) {
      var out = { role: m.role, content: m.content, tools: Array.isArray(m.tools) ? m.tools.filter(function (x) { return typeof x === "string"; }) : [] };
      if (m.role === "assistant") {
        var card = cleanCard(m.card), ui = cleanUi(m.ui);
        if (card) out.card = card;
        if (ui.length) out.ui = ui;
        if (m.warn === "no_card") out.warn = "no_card";
        else if (m.warn === "failed" && typeof m.why === "string") { out.warn = "failed"; out.why = m.why; }
      }
      return out;
    }).slice(-MAX_KEPT);
  }
  function toSend(log) { return log.slice(-MAX_SENT).map(function (m) { return { role: m.role, content: m.content }; }); }

  /** Сколько разговора помощник запомнит к следующему вопросу — по тем же правилам, что сервер (app/services/assistant.py,
      clean_history): последние max_messages реплик вместе с новым вопросом, не больше max_chars символов (свежие важнее),
      разговор в памяти начинается с вопроса. Новому вопросу оставлено место: одна реплика и QUESTION_RESERVE символов.
      limits — из GET /assistant/status (memory). Возвращает
      {percent: 0..100, full: заполнено ≥ 80 %, forgotten: сколько первых реплик log помощник уже не помнит}. */
  function memory(log, limits) {
    var lim = limits || {}, maxMsg = lim.max_messages | 0, maxChars = lim.max_chars | 0, maxOne = lim.max_message_chars | 0;
    var room = maxChars - Math.min(QUESTION_RESERVE, Math.floor(maxChars / 2));
    if (!log.length || maxMsg < 2 || room < 1) return { percent: 0, full: false, forgotten: 0 };
    var start = Math.max(0, log.length - (maxMsg - 1)), total = 0, i;        // одно место — новому вопросу
    for (i = log.length - 1; i >= start; i--) {
      var len = String(log[i].content).trim().length;
      if (maxOne > 0) len = Math.min(len, maxOne);
      if (total + len > room) break;
      total += len;
    }
    var first = i + 1;
    while (first < log.length && log[first].role !== "user") first++;      // сервер выбрасывает ответы без вопроса в начале
    var kept = log.length - first;
    var percent = first > 0 ? 100 : Math.min(100, Math.round(100 * Math.max(total / room, kept / (maxMsg - 1))));
    return { percent: percent, full: percent >= 80, forgotten: first };
  }

  /** Значение поля для карточки: пусто — «не указано», да/нет — словами. */
  function fmtValue(value, t) {
    if (value === null || value === undefined || value === "") return t("assistant.value_empty");
    if (value === true) return t("assistant.value_yes");
    if (value === false) return t("assistant.value_no");
    return String(value);
  }

  /** Предложение -> строки карточки «Помощник предлагает: …» (что было -> что станет). t — перевод. */
  function describe(action, t) {
    var lines = [];
    if (!isObj(action)) return lines;
    var label = function (field) { return t("assistant.field." + field); };
    if (action.kind === "update") {
      lines.push(t("assistant.card_product", { name: action.name || "", sku: action.sku || "" }));
      var changes = isObj(action.changes) ? action.changes : {}, before = isObj(action.before) ? action.before : {};
      Object.keys(changes).forEach(function (field) {
        if (field === "add_barcode" || field === "remove_barcode") {
          lines.push(label(field) + ": " + String(changes[field]));
        } else {
          lines.push(label(field) + ": " + fmtValue(before[field], t) + " → " + fmtValue(changes[field], t));
        }
      });
    } else if (action.kind === "status") {
      lines.push(t(action.status === "active" ? "assistant.card_restore" : "assistant.card_archive",
                   { name: action.name || "", sku: action.sku || "" }));
    } else if (action.kind === "add") {
      lines.push(t("assistant.card_add"));
      ["name", "price", "brand", "category", "barcode"].forEach(function (field) {
        if (field === "barcode" && !action.barcode) return;      // штрихкод с фото привяжется сам при добавлении
        lines.push(label(field === "barcode" ? "add_barcode" : field) + ": " + fmtValue(action[field], t));
      });
    }
    return lines;
  }

  function start(win) {
    var doc = win.document, I18N = win.I18N, Auth = win.Auth, Icons = win.Icons, Theme = win.Theme;
    var $ = function (id) { return doc.getElementById(id); };
    var t = function (key, params) { return I18N.t(key, params); };
    var auth = Auth.create(win);
    var log = [], busy = false, status = null, limits = null;      // status: {key, params} | {text} — строка под перепиской
    var customersBusy = false, pollTimer = null;

    function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
    function setHidden(el, hidden) { if (hidden) el.setAttribute("hidden", ""); else el.removeAttribute("hidden"); }
    function load() { try { log = cleanLog(JSON.parse(win.sessionStorage.getItem(STORE_KEY) || "null")); } catch (e) { log = []; } }
    function save() { try { win.sessionStorage.setItem(STORE_KEY, JSON.stringify(log)); } catch (e) { /* без запоминания */ } }
    function msgText(m) { return m ? (m.key ? t(m.key, m.params || {}) : m.text || "") : ""; }
    function el(tag, cls, text) {
      var x = doc.createElement(tag);
      if (cls) x.className = cls;
      if (text !== undefined) x.textContent = text;
      return x;
    }
    function button(cls, key, onClick) {
      var b = el("button", "btn " + cls, t(key));
      b.setAttribute("type", "button");
      b.addEventListener("click", onClick);
      return b;
    }

    function note(key) { var n = $("note"); n.textContent = t(key); setHidden(n, false); setHidden($("chatArea"), true); }

    function renderTip() {
      var title = $("tipTitle");
      clear(title);
      title.appendChild(Icons.make(doc, "users", { size: 18 }));
      title.appendChild(doc.createTextNode(" " + t("assistant.tip_title")));
      $("tip").className = "notice assistant-tip" + (customersBusy ? " assistant-tip--busy" : "");
      setHidden($("tipBusy"), !customersBusy);
    }

    function errorOf(res) {
      return res.json().then(function (b) { return b && typeof b === "object" ? b : {}; }, function () { return {}; }).then(function (body) {
        if (res.status === 401) { auth.goLogin("expired"); return { key: "login.expired" }; }
        if (res.status === 503 && body.code === "assistant_busy") customersBusy = true;
        if (typeof body.code === "string" && I18N.has("server." + body.code)) return { key: "server." + body.code };
        if (typeof body.detail === "string" && body.detail) return { text: body.detail };
        return { key: "error.generic" };
      });
    }

    // --- карточка «Помощник предлагает» --------------------------------------------------------------------------------------

    function post(path, payload) {
      var headers = auth.authHeaders();
      var opts = { method: "POST", headers: headers };
      if (payload instanceof (win.FormData || function () {})) opts.body = payload;
      else { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(payload); }
      return win.fetch(auth.apiBase() + path, opts);
    }

    function finish(card, res, onOk) {
      if (res.ok) return res.json().then(function (data) { onOk(data || {}); save(); render(); });
      return errorOf(res).then(function (err) { card.state = "error"; card.error = msgText(err); save(); render(); });
    }

    function netError(card) { card.state = "error"; card.error = t("error.network"); save(); render(); }

    function watchTask(card, taskId, tries) {             // новый товар: фото проверяются в фоне — узнать, чем кончилось
      if (!taskId || tries <= 0) return;
      win.setTimeout(function () {
        win.fetch(auth.apiBase() + "/api/v1/catalog/products/tasks/" + encodeURIComponent(taskId), { headers: auth.authHeaders() })
          .then(function (res) { return res.ok ? res.json() : null; }).then(function (data) {
            if (!data || data.state === "working") { watchTask(card, taskId, tries - 1); return; }
            card.note = data.state === "done" ? t("assistant.add_done", { name: card.action.name || "" })
                                              : (data.message || t("error.generic"));
            save(); render();
          }, function () { watchTask(card, taskId, tries - 1); });
      }, 2000);
    }

    function confirm(card, files) {
      if (card.state !== "ask" && card.state !== "error") return;
      card.state = "working"; card.error = ""; render();
      if (card.action.kind === "add") {
        var form = new win.FormData();
        ["name", "price", "brand", "category", "barcode"].forEach(function (f) {
          var v = card.action[f];
          form.append(f, v === null || v === undefined ? "" : String(v));
        });
        Array.prototype.forEach.call(files || [], function (file) { form.append("files", file); });
        return post("/api/v1/assistant/act/add", form).then(function (res) {
          return finish(card, res, function (data) {
            card.state = "done"; card.note = t("assistant.add_queued");
            watchTask(card, data.task_id, 30);
          });
        }, function () { netError(card); });
      }
      return post("/api/v1/assistant/act", { action: card.action }).then(function (res) {
        return finish(card, res, function (data) { card.state = "done"; card.undo = isObj(data.undo) ? data.undo : null; });
      }, function () { netError(card); });
    }

    function undo(card) {
      if (card.state !== "done" || !card.undo) return;
      var back = card.undo;
      card.state = "working"; render();
      return post("/api/v1/assistant/act", { action: back }).then(function (res) {
        return finish(card, res, function () { card.state = "undone"; card.undo = null; });
      }, function () { netError(card); });
    }

    function renderCard(card) {
      var box = el("div", "chat__card chat__card--" + card.state);
      box.appendChild(el("div", "chat__card-title", t("assistant.card_title")));
      var list = el("ul", "chat__card-list");
      describe(card.action, t).forEach(function (line) { list.appendChild(el("li", "", line)); });
      box.appendChild(list);
      var files = null;
      if (card.action.kind === "add" && (card.state === "ask" || card.state === "error")) {
        var lab = el("label", "field");
        lab.appendChild(el("span", "field__label", t("assistant.add_photos")));
        files = el("input", "input");
        files.setAttribute("type", "file"); files.setAttribute("accept", "image/*"); files.setAttribute("multiple", "");
        lab.appendChild(files);
        box.appendChild(lab);
      }
      var row = el("div", "chat__actions");
      if (card.state === "ask" || card.state === "error") {
        row.appendChild(button("btn--primary", "assistant.do", function () {
          if (files && (!files.files || !files.files.length)) { card.state = "error"; card.error = t("assistant.add_need_photo"); render(); return; }
          confirm(card, files && files.files);
        }));
        row.appendChild(button("btn--secondary", "assistant.cancel", function () { card.state = "cancelled"; card.error = ""; save(); render(); }));
      } else if (card.state === "done" && card.undo) {
        row.appendChild(button("btn--secondary", "assistant.undo", function () { undo(card); }));
      }
      var says = {
        working: "assistant.card_working", done: card.action.kind === "add" ? "" : "assistant.card_done",
        cancelled: "assistant.card_cancelled", undone: "assistant.card_undone"
      }[card.state];
      var text = card.state === "error" ? card.error : (card.note || (says ? t(says) : ""));
      if (text) box.appendChild(el("p", "chat__card-state" + (card.state === "error" ? " chat__card-state--error" : ""), text));
      if (row.firstChild) box.appendChild(row);
      return box;
    }

    // --- переписка -----------------------------------------------------------------------------------------------------------

    function render() {
      var host = $("log");
      clear(host);
      var mem = memory(log, limits);
      log.forEach(function (m, idx) {
        var item = el("div", "chat__msg chat__msg--" + (m.role === "user" ? "user" : "bot") + (idx < mem.forgotten ? " chat__msg--forgotten" : ""));
        item.appendChild(el("div", "chat__who", t(m.role === "user" ? "assistant.you" : "assistant.bot")));
        item.appendChild(el("div", "chat__text", m.content));
        var seenTools = (m.tools || []).filter(function (n) { return ACTION_TOOLS.indexOf(n) === -1; });
        if (seenTools.length) {
          item.appendChild(el("div", "chat__seen", t("assistant.checked", { what: seenTools.map(function (n) {
            return I18N.has("assistant.tool." + n) ? t("assistant.tool." + n) : n; }).join(", ") })));
        }
        (m.ui || []).forEach(function (u) {
          var what = u.kind === "lang" ? (I18N.names && I18N.names[u.value]) || u.value : t("theme." + u.value);
          item.appendChild(el("div", "chat__seen", t(u.kind === "lang" ? "assistant.ui_lang" : "assistant.ui_theme", { what: what })));
        });
        if (m.warn) {             // «сделано» без карточки или предложить не вышло — сказать прямо: в базе ничего не изменилось
          item.appendChild(el("p", "chat__card-state chat__card-state--error",
                              m.warn === "failed" ? t("assistant.proposal_failed", { why: m.why }) : t("assistant.no_card")));
        }
        if (m.card) item.appendChild(renderCard(m.card));
        if (idx < mem.forgotten) item.appendChild(el("div", "chat__seen", t("assistant.forgotten")));
        host.appendChild(item);
      });
      var ex = $("examples");
      clear(ex);
      setHidden(ex, log.length > 0);
      EXAMPLES.forEach(function (key) {
        var b = el("button", "btn btn--secondary chat__example", t(key));
        b.setAttribute("type", "button");
        b.addEventListener("click", function () { $("question").value = t(key); send(); });
        ex.appendChild(b);
      });
      var bar = $("memBar");
      bar.style.width = mem.percent + "%";
      $("memMeter").setAttribute("aria-valuenow", String(mem.percent));
      $("memory").className = "chat__memory" + (mem.full ? " chat__memory--full" : "");
      $("memText").textContent = mem.full ? t("assistant.memory_full", { percent: mem.percent })
                                          : t("assistant.memory", { percent: mem.percent });
      setHidden($("memory"), !limits);
      var st = $("status");
      st.textContent = msgText(status); setHidden(st, !status);
      $("sendBtn").disabled = busy; $("question").disabled = busy;
      renderTip();
      if (host.lastChild && host.lastChild.scrollIntoView) host.lastChild.scrollIntoView({ block: "end" });
    }

    /** Язык и фон — сразу (настройки этого браузера, вернуть — одной кнопкой вверху страницы). */
    function applyUi(list) {
      list.forEach(function (u) {
        if (u.kind === "theme") { Theme.set(u.value); relabel(); }
        else if (u.kind === "lang") I18N.setLang(u.value);          // onChange сам перерисует страницу
      });
    }

    function send() {
      var q = $("question"), text = String(q.value || "").trim();
      if (!text || busy) return Promise.resolve();
      log.push({ role: "user", content: text, tools: [] }); save();
      q.value = ""; busy = true; status = { key: "assistant.thinking" }; render();
      var headers = auth.authHeaders(); headers["Content-Type"] = "application/json";
      return Promise.resolve().then(function () {
        return win.fetch(auth.apiBase() + "/api/v1/assistant/chat", { method: "POST", headers: headers,
          body: JSON.stringify({ messages: toSend(log), lang: I18N.getLang() }) });
      }).then(function (res) {
        if (!res.ok) return errorOf(res).then(function (err) { status = err; });
        return res.json().then(function (data) {
          var card = isObj(data.proposal) ? cleanCard({ action: data.proposal, state: "ask" }) : null;
          var ui = cleanUi(data.ui);
          var reply = String(data.reply || "").trim() ||
                      t(card ? "assistant.proposal_default" : ui.length ? "assistant.ui_default" :
                        data.proposal_error ? "assistant.proposal_not_made" : "assistant.empty_reply");
          var msg = { role: "assistant", content: reply,
                      tools: (Array.isArray(data.tools) ? data.tools : []).map(function (x) { return x && x.name; }).filter(Boolean) };
          if (card) msg.card = card;
          if (ui.length) msg.ui = ui;
          if (!card && typeof data.proposal_error === "string" && data.proposal_error) { msg.warn = "failed"; msg.why = data.proposal_error; }
          else if (!card && data.no_card === true) msg.warn = "no_card";
          log.push(msg); save(); status = null;
          if (ui.length) applyUi(ui);
        });
      }, function () { status = { key: "error.network" }; }).then(function () {
        if (status && log.length && log[log.length - 1].role === "user") {     // вопрос без ответа — вернуть в поле, чтобы спросить ещё раз
          q.value = log.pop().content; save();
        }
        busy = false; render();
        if (q.focus) q.focus();
      });
    }

    function relabel() {
      var mode = Theme.get(), theme = $("themeBtn");
      clear(theme);
      theme.appendChild(Icons.make(doc, mode === "dark" ? "moon" : "sun", { size: 20 }));
      theme.setAttribute("aria-label", t("theme.label", { mode: t("theme." + mode) }));
      doc.title = t("nav.assistant") + " — " + t("app.name");
    }

    /** Заняты ли покупатели распознаванием — для напоминания «сейчас ответ может задержаться». */
    function readStatus() {
      return win.fetch(auth.apiBase() + "/api/v1/assistant/status", { headers: auth.authHeaders() });
    }
    function poll() {
      if (doc.hidden) return;
      readStatus().then(function (res) { return res.ok ? res.json() : null; }).then(function (data) {
        if (data && customersBusy !== !!data.customers_busy) { customersBusy = !!data.customers_busy; renderTip(); }
      }, function () { /* сеть — в следующий раз */ });
    }

    function init() {
      if (!auth.getToken()) { auth.goLogin(""); return Promise.resolve(); }
      return Promise.resolve().then(readStatus).then(function (res) {
        if (res.status === 401) { auth.goLogin("expired"); return; }
        if (res.status === 403) { note("assistant.no_access"); return; }
        if (!res.ok) { note("error.generic"); return; }
        return res.json().then(function (data) {
          if (!data.available) { note("server.assistant_disabled"); return; }
          limits = data.memory && typeof data.memory === "object" ? data.memory : null;
          customersBusy = !!data.customers_busy;
          setHidden($("chatArea"), false); load(); render();
          if (win.setInterval) pollTimer = win.setInterval(poll, BUSY_POLL_MS);
        });
      }, function () { note("error.network"); });
    }

    I18N.mountSwitcher($("langHost"));
    I18N.apply();
    relabel();
    I18N.onChange(function () { relabel(); render(); });
    $("themeBtn").addEventListener("click", function () { Theme.next(); relabel(); });
    $("form").addEventListener("submit", function (e) { e.preventDefault(); send(); });
    $("question").addEventListener("keydown", function (e) {            // Enter — отправить, Shift+Enter — новая строка
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    });
    $("newBtn").addEventListener("click", function () {                  // очистить историю: больше 2 реплик — сначала спросить
      if (busy) return;
      if (log.length > 2 && win.confirm && !win.confirm(t("assistant.clear_confirm"))) return;
      log = []; save(); status = null; render();
      if ($("question").focus) $("question").focus();
    });
    return { ready: init(), send: send, poll: poll, stop: function () { if (pollTimer) win.clearInterval(pollTimer); } };
  }

  return { start: start, cleanLog: cleanLog, toSend: toSend, memory: memory, describe: describe, fmtValue: fmtValue,
           QUESTION_RESERVE: QUESTION_RESERVE, STORE_KEY: STORE_KEY };
});
