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
  var KINDS = ["update", "status", "add", "bulk"];
  var BULK_CHANGES = ["price_percent", "price_add", "price_set", "category", "brand", "in_stock", "archive", "restore", "price_list"];
  // инструменты-действия: в строке «Смотрел: …» не показываются (их видно по карточке и по строке «Сделано»)
  var ACTION_TOOLS = ["propose_update", "propose_archive", "propose_add", "propose_bulk", "set_language", "set_theme", "price_list"];
  var SCAN_DAYS = [7, 30, 90];          // сводка сканов: за сколько дней (сервер хранит сканы 90 дней)
  var SCAN_DAYS_KEY = "assistant_scan_days";
  var ERROR_CODES = ["no_price", "bad_price", "many", "archived", "repeat"];   // ошибки строк списка «товар — цена»
  var LIST_MAX = 50;
  var TIPS = ["no_photo", "more_photos", "check_photos", "add_barcode"];
  // Подсказки сверху страницы (GET /assistant/hints, документ 16, §10.11): код -> готовый вопрос помощнику (кнопка «Спросить»).
  var HINT_ASK = { no_photo: "assistant.hint_ask.no_photo", no_price: "assistant.example_price",
                   no_barcode: "assistant.hint_ask.no_barcode", same_name: "assistant.example_dupes",
                   weak: "assistant.example_scans", missing: "assistant.example_scans" };
  // «Открыть товары» у подсказки: сразу с фильтром «без …» на странице «Товары» (products.html?missing=…); у остальных — весь список.
  var HINT_OPEN = { no_photo: "photo", no_price: "price", no_barcode: "barcode" };
  var HINT_CODES = ["empty", "no_photo", "no_price", "no_barcode", "same_name", "weak", "missing"];
  // Тексты, которые пишет сама страница (а не модель), хранятся ключом: после смены языка они переводятся, а модель видит
  // их на языке нового вопроса (на прежнем языке она и сама отвечала не на том).
  var SAY_KEYS = ["assistant.list_reply", "assistant.proposal_default", "assistant.ui_default", "assistant.proposal_not_made",
                  "assistant.empty_reply", "assistant.list_add_reply", "assistant.add_done", "assistant.add_queued", "error.generic"];

  function isObj(x) { return !!x && typeof x === "object" && !Array.isArray(x); }

  /** {key, name?} — текст страницы по ключу; чужой ключ или мусор -> null. */
  function cleanSay(say) {
    if (!isObj(say) || SAY_KEYS.indexOf(say.key) === -1) return null;
    var out = { key: say.key };
    if (typeof say.name === "string") out.name = say.name.slice(0, 200);
    return out;
  }

  /** Текст реплики или заметки: по ключу — на языке страницы сейчас, иначе как сохранён. */
  function sayText(say, text, t) { return say && t ? t(say.key, { name: say.name || "" }) : text; }

  /** Карточка предложения из памяти вкладки -> проверенная копия или null. «Выполняется» после перезагрузки — снова вопрос
      (двойного изменения не будет: сервер сверяет версию товара, повтор названия не пропустит). */
  function cleanCard(card) {
    if (!isObj(card) || !isObj(card.action) || KINDS.indexOf(card.action.kind) === -1) return null;
    var state = CARD_STATES.indexOf(card.state) === -1 || card.state === "working" ? "ask" : card.state;
    var out = { action: card.action, state: state, undo: isObj(card.undo) ? card.undo : null,
                error: typeof card.error === "string" ? card.error : "", note: typeof card.note === "string" ? card.note : "",
                opened: (Array.isArray(card.opened) ? card.opened : []).filter(function (i) { return i === (i | 0) && i >= 0; }) };
    var say = cleanSay(card.say);
    if (say) out.say = say;
    return out;
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
        var card = cleanCard(m.card), ui = cleanUi(m.ui), say = cleanSay(m.say);
        if (say) out.say = say;
        if (card) out.card = card;
        if (ui.length) out.ui = ui;
        if (m.warn === "no_card") out.warn = "no_card";
        else if (m.warn === "failed" && typeof m.why === "string") { out.warn = "failed"; out.why = m.why; }
      }
      return out;
    }).slice(-MAX_KEPT);
  }
  function toSend(log, t) {
    return log.slice(-MAX_SENT).map(function (m) { return { role: m.role, content: sayText(m.say, m.content, t) }; });
  }

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
    } else if (action.kind === "bulk") {
      lines = describeBulk(action, t).lines;
    }
    return lines;
  }

  /** Массовое изменение (этап A3) -> {lines: [заголовок, строки «было → станет»…], notes: [пропущено, «всё вместе»]}.
      Строки считает сервер; страница только показывает их. */
  function describeBulk(action, t) {
    var rows = Array.isArray(action.rows) ? action.rows.filter(isObj) : [];
    var change = BULK_CHANGES.indexOf(action.change) !== -1 ? action.change : "";
    var value = action.value;
    if ((change === "price_percent" || change === "price_add") && typeof value === "number" && value > 0) value = "+" + value;
    var what = change ? t("assistant.bulk." + change, { value: fmtValue(value, t) }) : "";
    var status = action.field === "status";
    var list = change === "price_list";
    var lines = [list && !rows.length ? t("assistant.card_list_nothing") : t("assistant.card_bulk", { n: rows.length, what: what })];
    rows.forEach(function (r) {
      var p = { name: String(r.name || ""), sku: String(r.sku || "") };
      if (status) lines.push(t("assistant.bulk_row_status", p));
      else lines.push(t("assistant.bulk_row", { name: p.name, sku: p.sku, before: fmtValue(r.before, t), after: fmtValue(r.after, t) }));
    });
    var notes = [];
    // по чему выбраны товары — так владелец сразу видит, если модель поняла категорию не так («gazak» -> «Газированные…»)
    // категорию по-узбекски сопоставляет программа («gazak» -> «Снеки»): показываем, какие категории каталога вышли
    var cats = Array.isArray(action.categories) ? action.categories.filter(function (c) { return typeof c === "string" && c; }) : [];
    if (action.where === "category" && action.query && cats.length) {
      notes.push(t("assistant.bulk_where.categories", { query: String(action.query), list: cats.join(", ") }));
    } else if (["category", "brand", "name"].indexOf(action.where) !== -1 && action.query) {
      notes.push(t("assistant.bulk_where." + action.where, { query: String(action.query) }));
    }
    if (list) notes = notes.concat(listNotes(action, t));
    else if (num(action.skipped) > 0) notes.push(t("assistant.bulk_skipped", { n: num(action.skipped) }));
    if (rows.length > 1 && !action.undo) notes.push(t("assistant.bulk_together"));
    return { lines: lines, notes: notes };
  }

  /** Строки списка «товар — цена», которых нет в каталоге -> [{index, name, price}] для кнопок «Добавить с фото».
      Товар без фото не создаётся (покупатель не узнал бы его по снимку): кнопка открывает обычную карточку нового товара
      (kind=add) с названием и ценой из строки — фото владелец добавляет в ней, «Сделать» без фото не отправляется. */
  function newItems(action) {
    if (!isObj(action) || action.kind !== "bulk" || action.change !== "price_list" || action.undo) return [];
    var out = [];
    (Array.isArray(action["new"]) ? action["new"] : []).forEach(function (r, index) {
      if (!isObj(r) || typeof r.name !== "string" || !r.name.trim()) return;
      var price = typeof r.price === "number" && isFinite(r.price) && r.price > 0 ? r.price : null;
      out.push({ index: index, name: r.name.trim().slice(0, 120), price: price });
    });
    return out;
  }

  /** Строка «нет в каталоге» -> предложение нового товара (как propose_add): бренд и категорию владелец допишет на странице. */
  function addAction(item) {
    return { kind: "add", name: item.name, price: item.price, brand: "", category: "", barcode: "" };
  }

  /** Список «товар — цена» (сервер разобрал его по строкам): чего нет в каталоге, ошибки по строкам, сколько уже с такой ценой. */
  function listNotes(action, t) {
    var notes = [], str = function (x) { return typeof x === "string" ? x : ""; };
    var fresh = (Array.isArray(action["new"]) ? action["new"] : []).filter(isObj).map(function (r) { return str(r.name) || str(r.line); });
    if (fresh.length) notes.push(t("assistant.list_new", { n: fresh.length, list: fresh.join(", ") }));
    (Array.isArray(action.errors) ? action.errors : []).filter(isObj).forEach(function (e) {
      var code = ERROR_CODES.indexOf(e.code) !== -1 ? e.code : "no_price";
      var names = (Array.isArray(e.names) ? e.names : []).map(str).filter(Boolean);
      if (num(e.more) > 0) names.push(t("assistant.list_more", { n: num(e.more) }));
      notes.push(t("assistant.list_error." + code, { line: str(e.line), list: names.join(", ") }));
    });
    if (num(action.skipped) > 0) notes.push(t("assistant.list_same", { n: num(action.skipped) }));
    if (num(action.cut) > 0) notes.push(t("assistant.list_cut", { n: num(action.cut), max: LIST_MAX }));
    return notes;
  }

  function num(x) { return typeof x === "number" && isFinite(x) ? x : 0; }

  /** Сводка сканов покупателей (GET /assistant/scans) -> строки на языке страницы. Считает программа, модель не нужна.
      Возвращает {line, weak: {title, rows: [{text, tips}], more} | null, missing: {title, rows, more, tip} | null, notes: []}. */
  function scanSummary(rep, t) {
    var out = { line: "", weak: null, missing: null, notes: [] };
    if (!isObj(rep)) return out;
    var c = isObj(rep.counts) ? rep.counts : {}, scans = num(rep.scans);
    if (!rep.started) { out.notes.push(t("assistant.scans_not_started")); return out; }
    if (!scans) { out.notes.push(t("assistant.scans_none")); return out; }
    out.line = t("assistant.scans_line", { days: num(rep.days), scans: scans, matched: num(c.matched), uncertain: num(c.uncertain),
                                           missing: num(c.not_in_catalog), unknown: num(c.unknown) });
    var more = function (shown, total) { return total > shown ? t("assistant.scans_more", { shown: shown, total: total }) : ""; };
    var weak = Array.isArray(rep.weak) ? rep.weak.filter(isObj) : [];
    if (weak.length) {
      out.weak = {
        title: t("assistant.scans_weak", { n: Math.max(num(rep.weak_total), weak.length) }),
        more: more(weak.length, num(rep.weak_total)),
        rows: weak.map(function (w) {
          return {
            text: t("assistant.scans_weak_row", { name: String(w.name || ""), sku: String(w.sku || ""), scans: num(w.scans),
                                                  unsure: num(w.unsure), photos: num(w.photos) }),
            tips: (Array.isArray(w.tips) ? w.tips : []).filter(function (x) { return TIPS.indexOf(x) !== -1; })
              .map(function (x) { return t("assistant.tip_code." + x); })
          };
        })
      };
    } else if (num(c.matched) + num(c.uncertain) > 0) {
      out.notes.push(t("assistant.scans_all_good"));
    }
    var missing = Array.isArray(rep.missing) ? rep.missing.filter(isObj) : [];
    if (missing.length) {
      out.missing = {
        title: t("assistant.scans_missing", { n: Math.max(num(rep.missing_total), missing.length) }),
        more: more(missing.length, num(rep.missing_total)), tip: t("assistant.scans_missing_tip"),
        rows: missing.map(function (m) {
          var name = m.name ? String(m.name) : t("assistant.scans_no_name", { kind: m.kind ? t("kind." + m.kind) : "?" });
          return { text: t("assistant.scans_missing_row", { name: name, times: num(m.times) }), tips: [] };
        })
      };
    }
    if (rep.many_unknown) out.notes.push(t("assistant.scans_many_unknown"));
    return out;
  }

  /** Подсказки (GET /assistant/hints) -> строки на языке страницы. Считает программа, модель не нужна; помощник ничего не меняет.
      canAsk — есть ли модель (тогда у подсказки кнопка с готовым вопросом). Возвращает {rows: [{code, text, names, ask}], none}:
      names — «Fanta», «Sprite» и ещё 3 (или ""); ask — ключ вопроса или ""; open — адрес «Товаров» (с фильтром «без …», если он есть); none — текст «подсказок нет» или "". */
  function hintList(rep, t, canAsk) {
    var out = { rows: [], none: "" };
    if (!isObj(rep)) return out;
    (Array.isArray(rep.hints) ? rep.hints : []).filter(function (h) { return isObj(h) && HINT_CODES.indexOf(h.code) !== -1; })
      .forEach(function (h) {
        var names = (Array.isArray(h.names) ? h.names : []).filter(function (x) { return typeof x === "string" && x; });
        var n = num(h.n), list = names.map(function (x) { return "«" + x + "»"; }).join(", ");
        if (list && n > names.length) list += " " + t("assistant.hint_more", { n: n - names.length });
        out.rows.push({ code: h.code, text: t("assistant.hint." + h.code, { n: n, days: num(rep.days) }), names: list,
                        ask: canAsk && HINT_ASK[h.code] ? HINT_ASK[h.code] : "",
                        open: "products.html" + (HINT_OPEN[h.code] ? "?missing=" + HINT_OPEN[h.code] : "") });
      });
    if (!out.rows.length && num(rep.active) > 0) out.none = t("assistant.hints_none");
    return out;
  }

  function start(win) {
    var doc = win.document, I18N = win.I18N, Auth = win.Auth, Icons = win.Icons, Theme = win.Theme;
    var $ = function (id) { return doc.getElementById(id); };
    var t = function (key, params) { return I18N.t(key, params); };
    var auth = Auth.create(win);
    var log = [], busy = false, status = null, limits = null;      // status: {key, params} | {text} — строка под перепиской
    var customersBusy = false, pollTimer = null;
    var scanRep = null, scanFailed = false, scanDays = 30;
    var hintRep = null, canAsk = false;

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

    // --- сводка сканов покупателей (сверху страницы; видна и без модели) ------------------------------------------------------

    function renderScans() {
      var host = $("scanBody"), sel = $("scanDays");
      clear(host);
      clear(sel);
      SCAN_DAYS.forEach(function (d) {
        var o = el("option", "", t("assistant.scans_period", { n: d }));
        o.value = String(d);
        if (d === scanDays) o.selected = true;
        sel.appendChild(o);
      });
      if (scanFailed) { host.appendChild(el("p", "scan-summary__tip", t("assistant.scans_error"))); return; }
      if (!scanRep) return;
      var sum = scanSummary(scanRep, t);
      if (sum.line) host.appendChild(el("p", "", sum.line));
      [sum.weak, sum.missing].forEach(function (group) {
        if (!group) return;
        host.appendChild(el("p", "scan-summary__group", group.title));
        var list = el("ul", "scan-summary__list");
        group.rows.forEach(function (row) {
          list.appendChild(el("li", "", row.text + (row.tips.length ? " — " + row.tips.join("; ") : "")));
        });
        host.appendChild(list);
        if (group.more) host.appendChild(el("p", "scan-summary__tip", group.more));
        if (group.tip) host.appendChild(el("p", "scan-summary__tip", group.tip));
      });
      sum.notes.forEach(function (n) { host.appendChild(el("p", "", n)); });
    }

    function loadScans() {
      return Promise.resolve().then(function () {
        return win.fetch(auth.apiBase() + "/api/v1/assistant/scans?days=" + scanDays, { headers: auth.authHeaders() });
      }).then(function (res) {
        if (res.status === 403) { setHidden($("scans"), true); return; }      // нет права смотреть каталог — сводки нет
        if (!res.ok) { scanFailed = true; return; }
        return res.json().then(function (data) { scanRep = data; scanFailed = false; });
      }, function () { scanFailed = true; }).then(renderScans);
    }

    function showScans() {
      try {
        var d = parseInt(win.localStorage.getItem(SCAN_DAYS_KEY), 10);
        if (SCAN_DAYS.indexOf(d) !== -1) scanDays = d;
      } catch (e) { /* по умолчанию 30 дней */ }
      setHidden($("scans"), false);
      renderScans();
      return loadScans();
    }

    // --- подсказки по своей инициативе (сверху страницы; считает программа, ничего не меняет) ---------------------------------

    function renderHints() {
      var host = $("hintBody");
      clear(host);
      if (!hintRep) return;
      var res = hintList(hintRep, t, canAsk);
      setHidden($("hints"), !res.rows.length && !res.none);
      if (res.none) { host.appendChild(el("p", "", res.none)); return; }
      var list = el("ul", "hints__list");
      res.rows.forEach(function (row) {
        var item = el("li", "hints__item");
        item.appendChild(el("p", "hints__text", row.text));
        if (row.names) item.appendChild(el("p", "hints__names", row.names));
        var acts = el("div", "hints__actions");
        if (row.ask) {
          var b = button("btn--secondary", "assistant.hint_ask", function () {
            if (busy) return;
            $("question").value = t(row.ask);
            send();
          });
          b.disabled = busy;
          acts.appendChild(b);
        }
        var a = el("a", "btn btn--secondary", t("assistant.hint_open"));
        a.setAttribute("href", row.open);
        acts.appendChild(a);
        item.appendChild(acts);
        list.appendChild(item);
      });
      host.appendChild(list);
    }

    function loadHints() {
      return Promise.resolve().then(function () {
        return win.fetch(auth.apiBase() + "/api/v1/assistant/hints", { headers: auth.authHeaders() });
      }).then(function (res) {
        if (!res.ok) { hintRep = null; return; }                 // 403 — нет права смотреть каталог: подсказок нет
        return res.json().then(function (data) { hintRep = data; });
      }, function () { /* сеть — подсказок нет, остальное работает */ }).then(renderHints);
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
      if (res.ok) return res.json().then(function (data) { onOk(data || {}); save(); render(); loadHints(); });
      return errorOf(res).then(function (err) { card.state = "error"; card.error = msgText(err); save(); render(); });
    }

    function netError(card) { card.state = "error"; card.error = t("error.network"); save(); render(); }

    function watchTask(card, taskId, tries) {             // новый товар: фото проверяются в фоне — узнать, чем кончилось
      if (!taskId || tries <= 0) return;
      win.setTimeout(function () {
        win.fetch(auth.apiBase() + "/api/v1/catalog/products/tasks/" + encodeURIComponent(taskId), { headers: auth.authHeaders() })
          .then(function (res) { return res.ok ? res.json() : null; }).then(function (data) {
            if (!data || data.state === "working") { watchTask(card, taskId, tries - 1); return; }
            if (data.state === "done") { card.say = { key: "assistant.add_done", name: card.action.name || "" }; card.note = ""; }
            else { card.say = data.message ? null : { key: "error.generic" }; card.note = data.message || ""; }
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
            card.state = "done"; card.note = ""; card.say = { key: "assistant.add_queued" };
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
      var bulk = card.action.kind === "bulk", lines = describe(card.action, t);
      if (bulk) box.appendChild(el("p", "chat__card-head", lines.shift()));
      var list = el("ul", "chat__card-list" + (bulk ? " chat__card-list--long" : ""));
      lines.forEach(function (line) { list.appendChild(el("li", "", line)); });
      box.appendChild(list);
      if (bulk && (card.state === "ask" || card.state === "error")) {
        describeBulk(card.action, t).notes.forEach(function (n) { box.appendChild(el("p", "chat__seen", n)); });
      }
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
      var nothing = bulk && !(Array.isArray(card.action.rows) && card.action.rows.length);    // список, где менять нечего
      if ((card.state === "ask" || card.state === "error") && !nothing) {
        row.appendChild(button("btn--primary", "assistant.do", function () {
          if (files && (!files.files || !files.files.length)) { card.state = "error"; card.error = t("assistant.add_need_photo"); render(); return; }
          confirm(card, files && files.files);
        }));
        row.appendChild(button("btn--secondary", "assistant.cancel", function () { card.state = "cancelled"; card.error = ""; save(); render(); }));
      } else if (card.state === "done" && card.undo) {
        row.appendChild(button("btn--secondary", bulk ? "assistant.undo_all" : "assistant.undo", function () { undo(card); }));
      }
      var says = {
        working: "assistant.card_working", done: card.action.kind === "add" ? "" : "assistant.card_done",
        cancelled: "assistant.card_cancelled", undone: "assistant.card_undone"
      }[card.state];
      var text = card.state === "error" ? card.error : (sayText(card.say, card.note, t) || (says ? t(says) : ""));
      if (text) box.appendChild(el("p", "chat__card-state" + (card.state === "error" ? " chat__card-state--error" : ""), text));
      if (row.firstChild) box.appendChild(row);
      var fresh = card.state === "working" ? [] : newItems(card.action);
      if (fresh.length) {                       // нет в каталоге — по кнопке карточка нового товара с фото
        var adds = el("div", "chat__actions");
        fresh.forEach(function (item) {
          if (card.opened.indexOf(item.index) !== -1) return;
          var b = el("button", "btn btn--secondary", t("assistant.list_add", { name: item.name }));
          b.setAttribute("type", "button");
          b.addEventListener("click", function () { openAdd(card, item); });
          adds.appendChild(b);
        });
        if (adds.firstChild) box.appendChild(adds);
      }
      return box;
    }

    function openAdd(card, item) {
      if (busy || card.opened.indexOf(item.index) !== -1) return;
      card.opened.push(item.index);
      log.push({ role: "assistant", content: t("assistant.list_add_reply", { name: item.name }), tools: [],
                 say: { key: "assistant.list_add_reply", name: item.name },
                 card: cleanCard({ action: addAction(item), state: "ask" }) });
      save(); render();
    }

    // --- переписка -----------------------------------------------------------------------------------------------------------

    function render() {
      var host = $("log");
      clear(host);
      var mem = memory(log, limits);
      log.forEach(function (m, idx) {
        var item = el("div", "chat__msg chat__msg--" + (m.role === "user" ? "user" : "bot") + (idx < mem.forgotten ? " chat__msg--forgotten" : ""));
        item.appendChild(el("div", "chat__who", t(m.role === "user" ? "assistant.you" : "assistant.bot")));
        item.appendChild(el("div", "chat__text", sayText(m.say, m.content, t)));
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
      renderHints();
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
          body: JSON.stringify({ messages: toSend(log, t), lang: I18N.getLang() }) });
      }).then(function (res) {
        if (!res.ok) return errorOf(res).then(function (err) { status = err; });
        return res.json().then(function (data) {
          var card = isObj(data.proposal) ? cleanCard({ action: data.proposal, state: "ask" }) : null;
          var ui = cleanUi(data.ui);
          var reply = String(data.reply || "").trim(), say = null;
          if (!reply) {
            say = { key: card && card.action.kind === "bulk" && !(Array.isArray(card.action.rows) && card.action.rows.length)
                      ? "assistant.list_reply" : card ? "assistant.proposal_default" : ui.length ? "assistant.ui_default" :
                      data.proposal_error ? "assistant.proposal_not_made" : "assistant.empty_reply" };
            reply = t(say.key);
          }
          var msg = { role: "assistant", content: reply,
                      tools: (Array.isArray(data.tools) ? data.tools : []).map(function (x) { return x && x.name; }).filter(Boolean) };
          if (say) msg.say = say;
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
          var scans = Promise.all([showScans(), loadHints()]);   // считает программа: видно и там, где модели нет
          if (!data.available) { note("server.assistant_disabled"); return scans; }
          canAsk = true;
          limits = data.memory && typeof data.memory === "object" ? data.memory : null;
          customersBusy = !!data.customers_busy;
          setHidden($("chatArea"), false); load(); render();
          if (win.setInterval) pollTimer = win.setInterval(poll, BUSY_POLL_MS);
          return scans;
        });
      }, function () { note("error.network"); });
    }

    I18N.mountSwitcher($("langHost"));
    I18N.apply();
    relabel();
    I18N.onChange(function () { relabel(); render(); renderScans(); renderHints(); });
    $("scanDays").addEventListener("change", function () {
      var d = parseInt($("scanDays").value, 10);
      if (SCAN_DAYS.indexOf(d) === -1) return;
      scanDays = d;
      try { win.localStorage.setItem(SCAN_DAYS_KEY, String(d)); } catch (e) { /* без запоминания */ }
      loadScans();
    });
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

  return { start: start, scanSummary: scanSummary, hintList: hintList, cleanLog: cleanLog, toSend: toSend, memory: memory, describe: describe,
           describeBulk: describeBulk, fmtValue: fmtValue, newItems: newItems, addAction: addAction, cleanCard: cleanCard,
           sayText: sayText,
           QUESTION_RESERVE: QUESTION_RESERVE, STORE_KEY: STORE_KEY };
});
