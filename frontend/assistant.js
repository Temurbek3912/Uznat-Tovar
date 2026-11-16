/* Страница «Помощник» (assistant.html): текстовый чат владельца с ИИ-помощником, этап A1 «только смотреть» (документ 16).
   Переписка хранится только в этой вкладке (sessionStorage) и уходит на сервер последними репликами; сервер её не сохраняет.
   Голоса нет (документ 16, §1.0). Текст ответа — только textContent: разметка из ответа модели не исполняется. */
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
  var EXAMPLES = ["assistant.example_photo", "assistant.example_dupes", "assistant.example_price"];

  /** Реплики из памяти вкладки -> только {role: user|assistant, content: строка}, не больше MAX_KEPT. */
  function cleanLog(raw) {
    if (!Array.isArray(raw)) return [];
    return raw.filter(function (m) {
      return m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string" && m.content.trim();
    }).map(function (m) {
      return { role: m.role, content: m.content, tools: Array.isArray(m.tools) ? m.tools.filter(function (x) { return typeof x === "string"; }) : [] };
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

  function start(win) {
    var doc = win.document, I18N = win.I18N, Auth = win.Auth, Icons = win.Icons, Theme = win.Theme;
    var $ = function (id) { return doc.getElementById(id); };
    var t = function (key, params) { return I18N.t(key, params); };
    var auth = Auth.create(win);
    var log = [], busy = false, status = null, limits = null;      // status: {key, params} | {text} — строка под перепиской

    function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
    function setHidden(el, hidden) { if (hidden) el.setAttribute("hidden", ""); else el.removeAttribute("hidden"); }
    function load() { try { log = cleanLog(JSON.parse(win.sessionStorage.getItem(STORE_KEY) || "null")); } catch (e) { log = []; } }
    function save() { try { win.sessionStorage.setItem(STORE_KEY, JSON.stringify(log)); } catch (e) { /* без запоминания */ } }
    function msgText(m) { return m ? (m.key ? t(m.key, m.params || {}) : m.text || "") : ""; }

    function note(key) { var n = $("note"); n.textContent = t(key); setHidden(n, false); setHidden($("chatArea"), true); }

    function render() {
      var host = $("log");
      clear(host);
      var mem = memory(log, limits);
      log.forEach(function (m, idx) {
        var item = doc.createElement("div");
        item.className = "chat__msg chat__msg--" + (m.role === "user" ? "user" : "bot") + (idx < mem.forgotten ? " chat__msg--forgotten" : "");
        var who = doc.createElement("div");
        who.className = "chat__who"; who.textContent = t(m.role === "user" ? "assistant.you" : "assistant.bot");
        var body = doc.createElement("div");
        body.className = "chat__text"; body.textContent = m.content;
        item.appendChild(who); item.appendChild(body);
        if (m.tools && m.tools.length) {
          var seen = doc.createElement("div");
          seen.className = "chat__seen";
          seen.textContent = t("assistant.checked", { what: m.tools.map(function (n) {
            return I18N.has("assistant.tool." + n) ? t("assistant.tool." + n) : n; }).join(", ") });
          item.appendChild(seen);
        }
        if (idx < mem.forgotten) {
          var gone = doc.createElement("div");
          gone.className = "chat__seen"; gone.textContent = t("assistant.forgotten");
          item.appendChild(gone);
        }
        host.appendChild(item);
      });
      var ex = $("examples");
      clear(ex);
      setHidden(ex, log.length > 0);
      EXAMPLES.forEach(function (key) {
        var b = doc.createElement("button");
        b.setAttribute("type", "button"); b.className = "btn btn--secondary chat__example"; b.textContent = t(key);
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
      if (host.lastChild && host.lastChild.scrollIntoView) host.lastChild.scrollIntoView({ block: "end" });
    }

    function errorOf(res) {
      return res.json().then(function (b) { return b && typeof b === "object" ? b : {}; }, function () { return {}; }).then(function (body) {
        if (res.status === 401) { auth.goLogin("expired"); return { key: "login.expired" }; }
        if (typeof body.code === "string" && I18N.has("server." + body.code)) return { key: "server." + body.code };
        if (typeof body.detail === "string" && body.detail) return { text: body.detail };
        return { key: "error.generic" };
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
          log.push({ role: "assistant", content: String(data.reply || ""),
                     tools: (Array.isArray(data.tools) ? data.tools : []).map(function (x) { return x && x.name; }).filter(Boolean) });
          save(); status = null;
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

    function init() {
      if (!auth.getToken()) { auth.goLogin(""); return Promise.resolve(); }
      return Promise.resolve().then(function () {
        return win.fetch(auth.apiBase() + "/api/v1/assistant/status", { headers: auth.authHeaders() });
      }).then(function (res) {
        if (res.status === 401) { auth.goLogin("expired"); return; }
        if (res.status === 403) { note("assistant.no_access"); return; }
        if (!res.ok) { note("error.generic"); return; }
        return res.json().then(function (data) {
          if (!data.available) { note("server.assistant_disabled"); return; }
          limits = data.memory && typeof data.memory === "object" ? data.memory : null;
          setHidden($("chatArea"), false); load(); render();
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
    return { ready: init(), send: send };
  }

  return { start: start, cleanLog: cleanLog, toSend: toSend, memory: memory, QUESTION_RESERVE: QUESTION_RESERVE, STORE_KEY: STORE_KEY };
});
