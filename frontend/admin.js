/* Страница управления (frontend/admin.html): логика вынесена из встроенного <script> (CSP script-src 'self'). Проверяется tools/admin_smoke_test.js.
   Тексты — на языке приложения (I18N, ключи admin.*; переключатель языков в верхней строке). Сообщения сервера с известным кодом
   переводятся по ключу server.<код>; без кода — прежний текст сервера. Смена языка перерисовывает открытый раздел. */
"use strict";

// Перевод по ключу. Словари не загрузились (не должно случаться: те же файлы с того же сервера) — виден сам ключ.
function i18n() { return typeof window !== "undefined" && window.I18N ? window.I18N : null; }
function tr(key, params) { const I = i18n(); return I && typeof I.t === "function" ? I.t(key, params) : key; }
function trHas(key) { const I = i18n(); return !!(I && typeof I.has === "function" && I.has(key)); }

// ============================ Чистая логика (тестируется в tools/admin_smoke_test.js) ============================

const PAGES = ["products", "card", "users", "system"];
const PAGE_TITLE_KEYS = {products: "nav.products", card: "nav.card", users: "nav.staff", system: "nav.system"};
// Поля карточки покупателя (app/services/shop_card.py: CARD_FIELDS). Срока годности здесь нет и не будет — он у каждой партии свой.
const CARD_FIELDS = ["price", "net_content", "manufacturer", "country", "in_stock", "ingredients", "description"];
/** Отмеченные поля -> тело PUT /api/v1/admin/shop-card: только известные коды, в порядке карточки. */
function buildCardFields(checked) {
  const set = new Set(checked || []);
  return {fields: CARD_FIELDS.filter((code) => set.has(code))};
}
const ROLES = ["admin", "analyst", "merchandiser", "regional_manager", "camera_agent"];
function roleLabel(role) { return ROLES.includes(role) ? tr("role." + role) : String(role || ""); }

function pageFromHash(hash) {
  const m = /^#\/([a-z]+)/.exec(hash || "");
  return m && PAGES.includes(m[1]) ? m[1] : "system";       // без раздела — «Состояние программы» (редизайн 2026-11-15)
}
function fmtBytes(n) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  const units = tr("admin.units").split(",");
  let v = Number(n), i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}
function describeError(status, detail, body) {
  // Известный код сервера — сообщение на языке человека (единая система ошибок: {detail, code, params}).
  const code = body && typeof body.code === "string" && /^[a-z_]+$/.test(body.code) ? body.code : null;
  if (code && trHas("server." + code)) return tr("server." + code, body.params && typeof body.params === "object" ? body.params : {});
  if (detail && typeof detail === "object" && !Array.isArray(detail) && Array.isArray(detail.errors)) {
    const shown = detail.errors.slice(0, 15).map((e) => `• ${e}`);
    const rest = detail.errors.length - shown.length;
    return [detail.message || tr("admin.err_generic"), ...shown, ...(rest > 0 ? [tr("admin.err_more", {n: rest})] : [])].join("\n");
  }
  const text = typeof detail === "string" ? detail : (detail && typeof detail === "object" && typeof detail.message === "string" ? detail.message : "");
  if (status === 401) return tr("login.expired");
  if (status === 403) return tr("admin.err_forbidden");
  if (status === 404) return text || tr("admin.err_not_found");
  if (status === 409 || status === 422 || status === 503) return text || tr("admin.err_check_input");
  if (status === 429) return tr("admin.err_too_many");
  if (status >= 500) return tr("admin.err_server");
  return text ? tr("admin.err_text", {text}) : tr("admin.err_http", {status});
}
// Пользователь: проверка на клиенте повторяет серверную (сервер всё равно проверяет заново) — чтобы ошибки были видны сразу.
// Пароль сотруднику задаёт не администратор, а сервер: временный, показывается один раз, при первом входе сотрудник задаёт свой
// (документ 05, §12.1–§12.2). Поэтому в запросе создания пароля нет.
// «Доступ до» (документ 05, §12.1): человек выбирает день; доступ действует ВЕСЬ этот день по часам компьютера, то есть до
// полуночи следующего дня. Сервер хранит момент в UTC. "" — без срока.
function accessUntilFromDate(day) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ""));
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]) + 1).toISOString();
}
function dateFromAccessUntil(value) {
  if (!value) return "";
  const at = new Date(/Z$|[+-]\d\d:\d\d$/.test(value) ? value : value + "Z");
  if (isNaN(at.getTime())) return "";
  const last = new Date(at.getTime() - 1);                  // последний момент доступа -> его день
  const pad = (n) => String(n).padStart(2, "0");
  return `${last.getFullYear()}-${pad(last.getMonth() + 1)}-${pad(last.getDate())}`;
}
function buildUserCreatePayload(f) {
  const username = String(f.username || "").trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._@-]{2,63}$/.test(username)) return {error: tr("admin.v_username")};
  if (!ROLES.includes(f.role)) return {error: tr("admin.v_role")};
  const payload = {username, role: f.role, store_ids: [...(f.store_ids || [])]};
  const until = accessUntilFromDate(f.access_until_date);
  if (until) payload.access_until = until;
  return {payload};
}
function buildUserUpdatePayload(f, original) {
  const payload = {};
  if (f.role !== original.role) payload.role = f.role;
  if (!!f.is_active !== !!original.is_active) payload.is_active = !!f.is_active;
  const a = [...(f.store_ids || [])].sort().join(","), b = [...(original.store_ids || [])].sort().join(",");
  if (a !== b) payload.store_ids = [...(f.store_ids || [])];
  if (f.access_until_date !== undefined && String(f.access_until_date || "") !== dateFromAccessUntil(original.access_until)) {
    payload.access_until = accessUntilFromDate(f.access_until_date);   // null — снять срок
  }
  return Object.keys(payload).length ? {payload} : {error: tr("admin.v_nothing")};
}
// Состояние учётной записи от сервера (app/core/account_status.py) -> значок. Прежний сервер без status — по is_active.
function userStatusBadge(u) {
  const st = u.status || (!u.is_active ? "blocked" : u.must_change_password ? "pending_first_login" : "active");
  if (st === "archived") return badge(tr("admin.status_archived"), "");
  if (st === "blocked") return badge(u.block_reason ? tr("admin.status_blocked_reason", {reason: u.block_reason}) : tr("admin.disabled"), "");
  if (st === "expired") return badge(tr("admin.status_expired"), "warn");
  if (st === "pending_first_login") return badge(tr("admin.awaiting_first_login"), "warn");
  return badge(u.access_until ? tr("admin.status_active_until", {date: dateFromAccessUntil(u.access_until)}) : tr("admin.active"), "ok");
}

// ============================ Состояние, API, вспомогательный UI ============================

const TOKEN_KEY = "shelf_audit_token";
let memoryToken = null;
const state = {me: null, stores: [], page: null};

function getToken() { try { return sessionStorage.getItem(TOKEN_KEY) || memoryToken; } catch (e) { return memoryToken; } }
function setToken(t) {
  memoryToken = t || null;
  try { if (t) sessionStorage.setItem(TOKEN_KEY, t); else sessionStorage.removeItem(TOKEN_KEY); } catch (e) { /* только в памяти */ }
}
function apiBase() {
  const q = new URLSearchParams(window.location.search).get("api_base");
  if (q) return q;
  return /^https?:$/.test(window.location.protocol) ? window.location.origin : "http://localhost:8000";
}
const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "value") node.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected") node[k] = true;
    else node.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  return node;
}
function clear(node) { node.replaceChildren(); }

async function api(path, options) {
  const headers = Object.assign({}, (options && options.headers) || {});
  const t = getToken();
  if (t) headers.Authorization = `Bearer ${t}`;
  const res = await fetch(`${apiBase()}${path}`, Object.assign({}, options, {headers}));
  if (res.status === 401) { setToken(null); showLogin({key: "login.expired"}); throw new Error("unauthorized"); }
  return res;
}
async function bodyOf(res) { try { const b = await res.json(); return b && typeof b === "object" ? b : {}; } catch (e) { return {}; } }
// JSON-запрос: вернёт данные или бросит Error с готовым текстом для пользователя
async function call(path, {method = "GET", body, form} = {}, retried = false) {
  const options = {method};
  if (body !== undefined) { options.headers = {"Content-Type": "application/json"}; options.body = JSON.stringify(body); }
  if (form) options.body = form;
  const res = await api(path, options);
  if (!res.ok) {
    const b = await bodyOf(res);
    // Важная операция (решение владельца 2026-11-08): сервер просит подтвердить пароль и код — спросить и повторить один раз.
    if (res.status === 403 && b.code === "reauth_required" && !retried && await reauthDialog()) return call(path, {method, body, form}, true);
    const e = new Error(describeError(res.status, b.detail, b)); e.status = res.status; throw e;
  }
  return res.status === 204 ? null : res.json();
}

async function reauthDialog() {
  // На этом компьютере, если у человека есть ПИН, — подтверждение ПИН-кодом (решение владельца 2026-11-15); иначе пароль и код.
  let local = null;
  try { local = typeof auth().localInfo === "function" ? await auth().localInfo() : null; } catch (e) { local = null; }
  const byPin = !!(local && local.this_computer && state.me && (local.users || []).some((u) => u.username === state.me.username));
  if (byPin) return reauthPinDialog();
  return reauthPasswordDialog();
}
function reauthPinDialog() {
  return new Promise((resolve) => {
    const pin = h("input", {id: "reauthPin", class: "pin-input", type: "password", inputmode: "numeric", maxlength: "6", autocomplete: "off"});
    const err = h("div", {class: "err", id: "reauthError"});
    openModal(tr("admin.reauth_title"), h("div", {}, h("div", {class: "hint"}, tr("pin.reauth_hint")), field(tr("signin.enter_pin"), pin), err), [
      {label: tr("admin.cancel"), kind: "sec", onClick: (close) => { close(); resolve(false); }},
      {label: tr("admin.reauth_ok"), id: "reauthOk", onClick: async (close) => {
        const r = await auth().reauthPin(pin.value);
        pin.value = "";
        if (r.ok) { close(); resolve(true); return; }
        err.textContent = tr(r.key, r.params);
        if (r.key === "login.expired") { close(); resolve(false); showLogin({key: "login.expired"}); }
      }},
    ]);
    if (pin.focus) pin.focus();
  });
}
function reauthPasswordDialog() {
  return new Promise((resolve) => {
    const pass = h("input", {id: "reauthPass", type: "password", autocomplete: "current-password"});
    const code = h("input", {id: "reauthCode", type: "text", autocomplete: "one-time-code", maxlength: "9"});
    const err = h("div", {class: "err", id: "reauthError"});
    openModal(tr("admin.reauth_title"), h("div", {}, h("div", {class: "hint"}, tr("admin.reauth_hint")),
      field(tr("login.pass"), pass), field(tr("login.code"), code), err), [
      {label: tr("admin.cancel"), kind: "sec", onClick: (close) => { close(); resolve(false); }},
      {label: tr("admin.reauth_ok"), id: "reauthOk", onClick: async (close) => {
        const r = await auth().reauth(pass.value, code.value);
        pass.value = ""; code.value = "";
        if (r.ok) { close(); resolve(true); return; }
        err.textContent = tr(r.key, r.params);
        if (r.key === "login.expired") { close(); resolve(false); showLogin({key: "login.expired"}); }
      }},
    ]);
    if (pass.focus) pass.focus();
  });
}

function toast(text, kind) {
  const node = h("div", {class: `toast ${kind || ""}`}, text);
  $("toasts").appendChild(node);
  setTimeout(() => node.remove(), kind === "error" ? 9000 : 5000);
}
function fail(e) { if (e && e.message !== "unauthorized") toast(e.message || String(e), "error"); }

function openModal(title, body, actions, {wide} = {}) {
  const root = $("modalRoot");
  const close = () => { overlay.remove(); };
  const foot = h("div", {class: "foot"}, (actions || []).map((a) => h("button", {class: `btn ${a.kind || ""}`, type: "button", id: a.id, onclick: () => a.onClick(close)}, a.label)));
  const overlay = h("div", {class: "overlay"}, h("div", {class: `modal ${wide ? "wide" : ""}`}, h("h3", {}, title), body, foot));
  root.appendChild(overlay);
  return {close, overlay};
}
function confirmDialog(text, okLabel) {
  return new Promise((resolve) => {
    openModal(tr("admin.confirm"), h("div", {class: "pre-line"}, text), [
      {label: tr("admin.cancel"), kind: "sec", onClick: (close) => { close(); resolve(false); }},
      {label: okLabel || tr("admin.confirm_ok"), kind: "danger", id: "confirmOk", onClick: (close) => { close(); resolve(true); }},
    ]);
  });
}
function field(label, input, hint) { return h("label", {class: "f"}, label, input, hint ? h("div", {class: "hint"}, hint) : null); }
function loading(node) { clear(node); node.appendChild(h("div", {class: "empty"}, tr("admin.loading"))); }
function showError(node, message, retry) {
  clear(node);
  node.appendChild(h("div", {class: "card"}, h("div", {class: "err"}, message), retry ? h("button", {class: "btn sec sm mt-8", type: "button", onclick: retry}, tr("admin.retry")) : null));
}
function badge(text, cls) { return h("span", {class: `badge ${cls || ""}`}, text); }
function table(headers, rows, emptyText) {
  if (!rows.length) return h("div", {class: "empty"}, emptyText || tr("admin.no_data"));
  return h("div", {class: "tablewrap"}, h("table", {}, h("thead", {}, h("tr", {}, headers.map((x) => h("th", {}, x)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", c && c.actions ? {class: "actions"} : {}, c && c.actions ? c.actions : c)))))));
}
function storeName(id) { const s = state.stores.find((x) => x.id === id); return s ? s.name : id; }
async function loadStores() { state.stores = await call("/api/v1/stores"); return state.stores; }

// ============================ Вход / оболочка ============================

// Вход — на единой странице login.html (редизайн 2026-11-15): без сеанса или после его окончания — туда, с возвратом сюда.
let authClient = null;
function auth() { if (!authClient) authClient = window.Auth.create(window); return authClient; }
function showLogin(message) {
  const key = message && message.key;
  auth().goLogin(key === "login.expired" || key === "server.session_max_age" ? "expired" : "");
}
async function boot() {
  if (!getToken()) { showLogin(null); return; }
  let me;
  try {
    const res = await api("/api/v1/auth/me");
    if (!res.ok) { showLogin({key: "admin.me_failed", params: {status: res.status}}); return; }
    me = await res.json();
  } catch (e) { if (e.message !== "unauthorized") showLogin({key: "home.offline", params: {reason: e.message}}); return; }
  state.me = me;
  startRefresh();
  // Роль — из ответа сервера; права в permissions — действующие здесь (вне административного входа прав управления в списке нет).
  const isAdmin = me.role === "admin";
  if (isAdmin && me.admin_listener === false) {
    // Решение В-1: это адрес для сотрудников (порт 8000) — права управления здесь не действуют; управление — на 8001 через VPN.
    clear($("main"));
    $("main").appendChild(h("div", {class: "card"}, h("h2", {}, tr("admin.not_admin_listener_title")),
      h("div", {class: "mute pre-line"}, tr("admin.not_admin_listener"))));
    return;
  }
  if (!isAdmin) {
    clear($("main"));
    $("main").appendChild(h("div", {class: "card"}, h("h2", {}, tr("admin.need_admin")),
      h("div", {class: "mute"}, tr("admin.need_admin_text")),
      h("a", {class: "btn mt-10", href: "identify.html"}, tr("admin.go_identify"))));
    return;
  }
  await route();
}
async function route() {
  const page = pageFromHash(window.location.hash);
  state.page = page;
  $("pageTitle").textContent = tr(PAGE_TITLE_KEYS[page]);
  document.title = `${tr(PAGE_TITLE_KEYS[page])} — ${tr("app.name")}`;
  const main = $("main");
  loading(main);
  try {
    await PAGE_RENDERERS[page](main);
  } catch (e) { if (e.message !== "unauthorized") showError(main, e.message, route); }
}


function statCard(n, label, extra) { return h("div", {class: "stat"}, h("div", {class: "n"}, String(n)), h("div", {class: "l"}, label), extra || null); }



// ============================ Товары ============================

async function renderProducts(main) {
  let offset = 0;
  const LIMIT = 50;
  const q = h("input", {id: "prQ", type: "search", placeholder: tr("admin.search")});
  const body = h("div", {});
  const draw = async () => {
    loading(body);
    try {
      const d = await call(`/api/v1/admin/products?${new URLSearchParams({q: q.value, limit: LIMIT, offset})}`);
      clear(body);
      body.appendChild(table([tr("admin.col_sku"), tr("admin.col_name"), tr("admin.col_brand"), tr("admin.col_category"), tr("admin.col_price")],
        d.items.map((p) => [h("code", {}, p.sku), p.name, p.brand, p.category, p.expected_price === null ? "—" : String(p.expected_price)]),
        q.value ? tr("admin.nothing_found") : tr("admin.catalog_empty")));
      const pages = Math.max(1, Math.ceil(d.total / LIMIT)), page = Math.floor(offset / LIMIT) + 1;
      body.appendChild(h("div", {class: "toolbar mt-10"}, h("span", {class: "mute"}, tr("admin.total_pages", {total: d.total, page, pages})),
        h("button", {class: "btn sec sm", type: "button", id: "prPrev", disabled: offset === 0, onclick: () => { offset = Math.max(0, offset - LIMIT); draw(); }}, "←"),
        h("button", {class: "btn sec sm", type: "button", id: "prNext", disabled: offset + LIMIT >= d.total, onclick: () => { offset += LIMIT; draw(); }}, "→")));
    } catch (e) { fail(e); showError(body, e.message, draw); }
  };
  q.addEventListener("input", () => { offset = 0; clearTimeout(renderProducts.t); renderProducts.t = setTimeout(draw, 300); });
  clear(main);
  main.appendChild(h("div", {class: "card"}, h("div", {class: "toolbar"}, q),
    withNodes(h("div", {class: "hint"}), "admin.products_hint", {link: h("a", {href: "products.html"}, tr("admin.products_link"))}), body));
  await draw();
}



// ============================ Пользователи ============================

async function renderUsers(main) {
  await loadStores();
  const users = await call("/api/v1/users");
  clear(main);
  main.appendChild(h("div", {class: "toolbar"}, h("button", {class: "btn", type: "button", id: "addUser", onclick: () => userModal(null)}, tr("admin.add_user")),
    h("div", {class: "grow"}), h("span", {class: "mute"}, tr("admin.users_server_note"))));
  main.appendChild(h("div", {class: "card"}, table([tr("login.user"), tr("admin.col_role"), tr("admin.col_status"), tr("admin.col_stores"), ""],
    users.map((u) => [h("b", {}, u.username), roleLabel(u.role), userStatusBadge(u),
    ["admin", "analyst"].includes(u.role) ? h("span", {class: "mute"}, tr("admin.whole_network"))
      : (u.store_ids.length ? u.store_ids.map(storeName).join(", ") : h("span", {class: "mute"}, tr("admin.no_access"))),
    u.status === "archived" ? {actions: [h("span", {class: "mute"}, u.block_reason || "")]} :
    {actions: [h("button", {class: "btn sec sm", type: "button", onclick: () => userModal(u)}, tr("admin.edit")),
      h("button", {class: "btn sec sm", type: "button", onclick: () => toggleUser(u)}, u.is_active ? tr("admin.disable") : tr("admin.enable")),
      u.username !== (state.me && state.me.username) ? h("button", {class: "btn sec sm", type: "button", "data-temppass": u.id, onclick: () => issueTempPassword(u)}, tr("admin.temp_password_issue")) : null,
      u.username !== (state.me && state.me.username) ? h("button", {class: "btn sec sm", type: "button", "data-reset2fa": u.id, onclick: () => resetUser2fa(u)}, tr("admin.reset_2fa")) : null,
      h("button", {class: "btn sec sm", type: "button", "data-history": u.id, onclick: () => showUserHistory(u)}, tr("admin.history")),
      u.username !== (state.me && state.me.username) ? h("button", {class: "btn danger sm", type: "button", "data-archive": u.id, onclick: () => archiveUser(u)}, tr("admin.archive")) : null]}]),
    tr("admin.no_users"))));
}
function storeChecks(selected) {
  const boxes = state.stores.map((s) => ({id: s.id, input: h("input", {type: "checkbox", checked: selected.includes(s.id)})}));
  const node = h("div", {id: "usStores", class: "scroll-160"}, boxes.length ? boxes.map((b, i) => h("label", {class: "chk"}, b.input, state.stores[i].name)) : h("div", {class: "mute"}, tr("admin.no_stores")));
  return {node, value: () => boxes.filter((b) => b.input.checked).map((b) => b.id)};
}
function userModal(user) {
  const editing = !!user;
  const username = h("input", {id: "usName", type: "text", autocomplete: "off", value: user ? user.username : "", disabled: editing});
  const role = h("select", {id: "usRole"}, ROLES.filter((v) => v !== "camera_agent" || (user && user.role === "camera_agent")).map((v) => h("option", {value: v}, roleLabel(v))));
  role.value = user ? user.role : "merchandiser";
  const active = h("input", {id: "usActive", type: "checkbox", checked: user ? user.is_active : true});
  const stores = storeChecks(user ? user.store_ids : []);
  const until = h("input", {id: "usUntil", type: "date", value: user ? dateFromAccessUntil(user.access_until) : ""});
  openModal(editing ? tr("admin.user_title", {name: user.username}) : tr("admin.new_user"), h("div", {}, field(tr("admin.f_login"), username),
    editing ? null : h("div", {class: "hint"}, tr("admin.new_user_temp_hint")), field(tr("admin.f_role"), role),
    editing ? h("label", {class: "chk"}, active, tr("admin.f_active")) : null,
    field(tr("admin.f_access_until"), until, tr("admin.access_until_hint")),
    h("div", {class: "hint"}, tr("admin.stores_hint")), stores.node),
    [{label: tr("admin.cancel"), kind: "sec", onClick: (close) => close()}, {label: tr("admin.save"), id: "usSave", onClick: async (close) => {
      try {
        if (editing) {
          const built = buildUserUpdatePayload({role: role.value, is_active: active.checked, store_ids: stores.value(), access_until_date: until.value}, user);
          if (built.error) { toast(built.error, "error"); return; }
          await call(`/api/v1/users/${encodeURIComponent(user.id)}`, {method: "PATCH", body: built.payload});
        } else {
          const built = buildUserCreatePayload({username: username.value, role: role.value, store_ids: stores.value(), access_until_date: until.value});
          if (built.error) { toast(built.error, "error"); return; }
          const created = await call("/api/v1/users", {method: "POST", body: built.payload});
          close(); await route();
          if (created && created.temporary_password) showTempPassword(created.username, created.temporary_password, created.temp_password_expires_at);
          return;
        }
        close(); toast(tr("admin.saved"), "ok"); await route();
      } catch (e) { fail(e); }
    }}]);
}
// Временный пароль показывается ОДИН раз (документ 05, §12.2): в окне, не в уведомлении и не в журнале; закрыл — снова не увидеть.
function showTempPassword(name, value, expiresAt) {
  const until = expiresAt ? tr("admin.temp_password_until", {time: new Date(expiresAt.endsWith("Z") || /[+-]\d\d:\d\d$/.test(expiresAt) ? expiresAt : expiresAt + "Z").toLocaleString()})
    : tr("admin.temp_password_no_deadline");
  openModal(tr("admin.temp_password_title", {name}), h("div", {},
    h("p", {class: "secret", id: "tempPassValue"}, value),
    h("p", {class: "mute pre-line"}, tr("admin.temp_password_text")), h("p", {class: "mute"}, until)),
    [{label: tr("admin.close"), id: "tempPassClose", onClick: (close) => close()}]);
}
async function issueTempPassword(user) {
  if (!(await confirmDialog(tr("admin.temp_password_confirm", {name: user.username}), tr("admin.temp_password_issue")))) return;
  try {
    const r = await call(`/api/v1/users/${encodeURIComponent(user.id)}/temporary-password`, {method: "POST", body: {}});
    await route();
    showTempPassword(user.username, r.temporary_password, r.expires_at);
  } catch (e) { fail(e); }
}
// Подтверждение с необязательной причиной (блокировка, отзыв доступа — документ 05, §12.5, §12.7). -> {ok, reason}.
function reasonDialog(text, okLabel) {
  return new Promise((resolve) => {
    const reason = h("input", {id: "reasonInput", type: "text", maxlength: "200", autocomplete: "off"});
    openModal(tr("admin.confirm"), h("div", {}, h("div", {class: "pre-line"}, text), field(tr("admin.reason_optional"), reason)), [
      {label: tr("admin.cancel"), kind: "sec", onClick: (close) => { close(); resolve({ok: false}); }},
      {label: okLabel, kind: "danger", id: "confirmOk", onClick: (close) => { const value = reason.value.trim(); close(); resolve({ok: true, reason: value}); }},
    ]);
  });
}
async function toggleUser(user) {
  let body = {is_active: !user.is_active};
  if (user.is_active) {
    const answer = await reasonDialog(tr("admin.disable_confirm", {name: user.username}), tr("admin.disable"));
    if (!answer.ok) return;
    if (answer.reason) body.block_reason = answer.reason;
  }
  try { await call(`/api/v1/users/${encodeURIComponent(user.id)}`, {method: "PATCH", body}); toast(tr("admin.saved"), "ok"); await route(); } catch (e) { fail(e); }
}
// Журнал действий по сотруднику (документ 05, §16): что с ним делали и его входы. Только чтение; секретов в журнале нет.
function auditLabel(action) { return trHas("audit." + action) ? tr("audit." + action) : action; }
function auditWho(row) { return row.actor || (row.via === "console" ? tr("audit.by_console") : tr("audit.by_system")); }
async function showUserHistory(user) {
  try {
    const rows = await call(`/api/v1/audit?target_id=${encodeURIComponent(user.id)}&limit=100`);
    const body = rows.length
      ? table([tr("audit.col_time"), tr("audit.col_action"), tr("audit.col_who"), tr("audit.col_reason")],
          rows.map((r) => [new Date(/Z$|[+-]\d\d:\d\d$/.test(r.at) ? r.at : r.at + "Z").toLocaleString(), auditLabel(r.action), auditWho(r), r.reason || ""]))
      : h("div", {class: "empty"}, tr("audit.empty"));
    openModal(tr("audit.title", {name: user.username}), h("div", {class: "scroll-400"}, body),
      [{label: tr("admin.close"), id: "historyClose", onClick: (close) => close()}]);
  } catch (e) { fail(e); }
}
async function archiveUser(user) {
  const answer = await reasonDialog(tr("admin.archive_confirm", {name: user.username}), tr("admin.archive"));
  if (!answer.ok) return;
  try {
    await call(`/api/v1/users/${encodeURIComponent(user.id)}/archive`, {method: "POST", body: answer.reason ? {reason: answer.reason} : {}});
    toast(tr("admin.saved"), "ok"); await route();
  } catch (e) { fail(e); }
}

// ============================ Система ============================

async function renderSystem(main) {
  const d = await call("/api/v1/admin/system");
  const st = (ok, text) => badge(text, ok ? "ok" : "error");
  clear(main);
  // Сначала — одна понятная строка: всё ли в порядке (подробности — в карточках ниже, технические параметры — под «Для специалиста»).
  const allOk = d.database === "ok" && d.queue === "ok" && !!(d.worker && d.worker.online);
  main.appendChild(h("div", {class: allOk ? "banner ok" : "banner bad", id: "systemSummary", role: "status"}, tr(allOk ? "system.all_ok" : "system.problem")));
  main.appendChild(h("div", {class: "grid"},
    statCard(d.database === "ok" ? tr("admin.works") : tr("admin.failed"), tr("admin.database"), h("div", {class: "hint"}, d.database)),
    statCard(d.queue === "ok" ? tr("admin.works") : tr("admin.failed"), tr("admin.queue"), h("div", {class: "hint"}, d.queue)),
    statCard(d.worker.online ? tr("admin.responds") : tr("admin.not_responding"), tr("admin.worker"),
      h("div", {class: "hint"}, d.worker.online ? d.worker.nodes.join(", ") : tr("admin.worker_wait"))),
    statCard(d.server_time_local, tr("admin.server_time", {tz: d.timezone}))));
  const u = d.storage.usage, disk = d.storage.disk;
  main.appendChild(h("div", {class: "card"}, h("h3", {class: "mt-0"}, tr("admin.storage_title")),
    h("div", {}, tr("admin.storage_folder") + " ", h("code", {}, d.storage.path),
      "  ·  " + tr("admin.storage_stats", {files: `${u.files}${u.truncated ? "+" : ""}`, size: fmtBytes(u.bytes)}) + (u.truncated ? " " + tr("admin.count_partial") : "")),
    disk ? h("div", {class: "mute"}, tr("admin.disk", {free: fmtBytes(disk.free_bytes), total: fmtBytes(disk.total_bytes)})) : null,
    h("div", {class: "hint"}, tr("admin.no_retention"))));
  // Подписи параметров переводятся по их ключу; неизвестный ключ — подпись сервера (по-русски). Источник — технический (файл: имя).
  const settingLabel = (s) => (trHas("admin.setting." + s.key) ? tr("admin.setting." + s.key) : s.label);
  main.appendChild(h("details", {class: "card tech-details"}, h("summary", {}, `${tr("identify.tech")}: ${tr("admin.params_title")}`),
    table([tr("admin.col_param"), tr("admin.col_value"), tr("admin.col_where")], d.settings.map((s) => [settingLabel(s), h("b", {}, String(s.value)), h("span", {class: "mute"}, s.source)])),
    h("div", {class: "hint mt-8"}, trHas("admin.settings_note") ? tr("admin.settings_note") : d.settings_note)));
}

// ============================ Карточка для покупателя ============================

async function renderCard(main) {
  let d = await call("/api/v1/admin/shop-card");
  const boxes = {};
  const list = h("div", {class: "mt-8"});
  const fill = () => { for (const code of CARD_FIELDS) boxes[code].checked = d.fields.includes(code); };
  for (const code of CARD_FIELDS) {
    boxes[code] = h("input", {type: "checkbox", id: `cf_${code}`});
    list.appendChild(h("label", {class: "chk"}, boxes[code], tr("card." + code)));
  }
  fill();
  const save = async (fields) => {
    try {
      d = await call("/api/v1/admin/shop-card", {method: "PUT", body: buildCardFields(fields)});
      fill();
      toast(tr("admin.card_saved"), "ok");
    } catch (e) { fail(e); }
  };
  const saveBtn = h("button", {class: "btn", type: "button", id: "cardSave",
    onclick: () => save(CARD_FIELDS.filter((code) => boxes[code].checked))}, tr("common.save"));
  const defBtn = h("button", {class: "btn sec", type: "button", id: "cardDefault", onclick: () => save(d.default)}, tr("admin.card_default"));
  clear(main);
  main.appendChild(h("div", {class: "card"},
    h("div", {class: "hint"}, tr("admin.card_hint")),
    h("div", {class: "mute mt-8"}, tr("admin.card_always")),
    list,
    h("div", {class: "hint mt-8"}, h("b", {}, tr("admin.card_expiry"))),
    h("div", {class: "toolbar mt-10"}, saveBtn, defBtn, h("a", {href: "shop-list.html", target: "_blank", rel: "noopener"}, tr("admin.card_open_list")))));
}

const PAGE_RENDERERS = {products: renderProducts, card: renderCard, users: renderUsers, system: renderSystem};

async function resetUser2fa(user) {
  if (!(await confirmDialog(tr("admin.reset_2fa_confirm", {name: user.username}), tr("admin.reset_2fa")))) return;
  try { await call(`/api/v1/users/${encodeURIComponent(user.id)}/2fa/reset`, {method: "POST"}); toast(tr("admin.reset_2fa_done"), "ok"); await route(); } catch (e) { fail(e); }
}

// Продление сеанса обменом токена (решение В-4: предел 8 часов от входа не сдвигается): раз в 20 минут, пока страница открыта.
let refreshTimer = null;
function startRefresh() {
  if (refreshTimer || typeof setInterval !== "function") return;
  refreshTimer = setInterval(async () => {
    if (!getToken()) return;
    const r = await auth().refresh();
    if (r.ok) setToken(auth().getToken());
    else if (r.key === "login.expired" || r.key === "server.session_max_age") { setToken(null); showLogin({key: r.key}); }
  }, 20 * 60 * 1000);
  if (refreshTimer && typeof refreshTimer.unref === "function") refreshTimer.unref();     // не держать процесс в проверках Node
}


// Строка перевода с вставками-элементами ({link} → ссылка): элемент встаёт туда, где его место в переводе.
function withNodes(target, key, nodes) {
  const SLOT = "\u0001", marks = {};
  for (const name of Object.keys(nodes)) marks[name] = SLOT + name + SLOT;
  tr(key, marks).split(SLOT).forEach((part, i) => {
    if (i % 2 === 1 && nodes[part]) target.appendChild(nodes[part]);
    else if (part) target.appendChild(document.createTextNode(part));
  });
  return target;
}

// Смена языка: статичные надписи (data-i18n) переводит I18N.apply, сообщение входа и открытый раздел перерисовываются здесь.
function onLanguageChange() {
  paintTheme();
  if (!state.me) return;
  if (Array.isArray(state.me.permissions) && state.me.permissions.includes("store:manage")) route(); else boot();
}
function startI18n() {
  const I = i18n();
  if (!I) return;
  if (typeof I.apply === "function") I.apply();
  if (typeof I.mountSwitcher === "function") I.mountSwitcher($("langSwitch"));
  if (typeof I.onChange === "function") I.onChange(onLanguageChange);
}

// Тема: та же кнопка, что на остальных страницах (theme.js).
function paintTheme() {
  const btn = $("themeBtn"), T = window.Theme, I = window.Icons;
  if (!btn || !T || !I) return;
  const mode = T.get();
  btn.replaceChildren(I.make(document, mode === "dark" ? "moon" : mode === "light" ? "sun" : "monitor", {size: 20}));
  btn.setAttribute("aria-label", tr("theme.label", {mode: tr("theme." + mode)}));
}
// Esc закрывает открытое окно (кнопка «Отмена» — первая в окне).
if (typeof document.addEventListener === "function") document.addEventListener("keydown", (evt) => {
  if (evt.key !== "Escape") return;
  const cancel = document.querySelector ? document.querySelector("#modalRoot .overlay .foot .btn.sec") : null;
  if (cancel && cancel.click) cancel.click();
});

startI18n();
paintTheme();
if ($("themeBtn") && window.Theme) $("themeBtn").addEventListener("click", () => { window.Theme.next(); paintTheme(); });
window.addEventListener("hashchange", () => { if (state.me) route(); });
boot();
