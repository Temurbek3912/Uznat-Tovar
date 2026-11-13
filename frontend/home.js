/* Главная страница (frontend/index.html): логика вынесена из встроенного <script> (CSP script-src 'self'). Проверяется tools/home_smoke_test.js.
   Тексты — на языке приложения (I18N, ключи home.*; переключатель языков на самой странице), при смене языка перерисовываются. */
"use strict";

// Перевод по ключу. Словари не загрузились (не должно случаться: те же файлы с того же сервера) — виден сам ключ, а не пустота.
function tr(key, params) {
  const I = typeof window !== "undefined" ? window.I18N : null;
  return I && typeof I.t === "function" ? I.t(key, params) : key;
}

// ---- Чистая логика (тестируется в tools/home_smoke_test.js) ----
function canSee(perms, needs) { return !needs || (Array.isArray(perms) && perms.indexOf(needs) !== -1); }

function greeting(username) {
  const name = String(username || "").trim();
  return name ? tr("home.hello_name", {name}) : tr("home.hello");
}

// Шаги первой настройки. Состояние считается по данным системы, а не по памяти браузера: что сделано — отмечено само.
// Данных нет или они не число (не удалось узнать) — шаг не показываем, чтобы не говорить человеку неправду.
// catalogCount — сколько товаров вернул запрос с лимитом 3 (0, 1, 2 или 3), поэтому «хотя бы три» можно проверить.
function onboardingSteps(data) {
  const count = data && typeof data.catalogCount === "number" ? data.catalogCount : null;
  const steps = [
    {id: "catalog", done: count === null ? null : count > 0, title: tr("home.step_catalog_title"), href: "products.html",
     cta: tr("home.step_cta"), note: tr("home.step_catalog_note")},
    {id: "more", done: count === null ? null : count >= 3, title: tr("home.step_more_title"), href: "products.html",
     cta: tr("home.step_cta"), note: tr("home.step_more_note")},
  ];
  return steps.filter((st) => st.done !== null);
}
function nextStep(steps) { return steps.find((st) => !st.done) || null; }

function phoneExample(protocol, port) {
  return `http://${tr("home.ip_word")}:${port || "8000"}`;
}

const TOKEN_KEY = "shelf_audit_token", ROLE_KEY = "shelf_audit_role", PERMS_KEY = "shelf_audit_perms";
let memoryToken = null;
function getToken() { try { return sessionStorage.getItem(TOKEN_KEY) || memoryToken; } catch (e) { return memoryToken; } }
function setToken(t) {
  memoryToken = t || null;
  try { if (t) sessionStorage.setItem(TOKEN_KEY, t); else sessionStorage.removeItem(TOKEN_KEY); } catch (e) { /* только в памяти */ }
}
function setSession(role, perms) {
  try {
    if (role) sessionStorage.setItem(ROLE_KEY, role); else sessionStorage.removeItem(ROLE_KEY);
    if (perms) sessionStorage.setItem(PERMS_KEY, JSON.stringify(perms)); else sessionStorage.removeItem(PERMS_KEY);
  } catch (e) { /* не критично */ }
}
function apiBaseUrl() {
  const fromQuery = new URLSearchParams(window.location.search).get("api_base");
  if (fromQuery) return fromQuery;
  return /^https?:$/.test(window.location.protocol) ? window.location.origin : "http://localhost:8000";
}

const $ = (id) => document.getElementById(id);
function show(id, on) { $(id).hidden = !on; }

function mk(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== null && text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

// Строка перевода с вставками-элементами: {cmd} → <code>ipconfig</code> и т. п. Порядок слов у языков разный, поэтому элементы
// встают туда, где их место в переводе, а не склеиваются из кусков. Всё — через textContent и appendChild.
const SLOT = "\u0001";
function fillWithNodes(target, key, nodes, params) {
  const marks = {};
  for (const name of Object.keys(nodes)) marks[name] = SLOT + name + SLOT;
  const parts = tr(key, Object.assign({}, params || {}, marks)).split(SLOT);
  target.textContent = "";
  parts.forEach((part, i) => {
    if (i % 2 === 1 && nodes[part]) target.appendChild(nodes[part]);
    else if (part) target.appendChild(mk("span", part));
  });
}

let lastCount = null, lastUser = null, loginMsg = null;

function renderStatic() {
  fillWithNodes($("phoneStep2"), "home.phone_2", {cmd: mk("code", "ipconfig"), ip: mk("code", "192.168.1.50")});
  const url = $("phoneUrl");
  url.textContent = phoneExample(window.location.protocol, window.location.port);
  fillWithNodes($("phoneStep3"), "home.phone_3", {url}, {ip_word: tr("home.ip_word")});
}

function paintLoginMsg() {
  const m = loginMsg;
  $("loginError").textContent = !m ? "" : (typeof m === "string" ? m : tr(m.key, m.params));
}
function setLoginMsg(m) { loginMsg = m || null; paintLoginMsg(); }

function renderChecklist(steps) {
  const list = $("checklist");
  list.textContent = "";
  const next = nextStep(steps);
  steps.forEach((step, i) => {
    const li = mk("li", null, "step" + (step.done ? " done" : "") + (step === next ? " next" : ""));
    li.appendChild(mk("span", step.done ? "✓" : String(i + 1), "mark"));
    const body = mk("div", null, "body");
    body.appendChild(mk("b", step.title));
    if (step === next) {
      if (step.href) { const a = mk("a", step.cta, "btn auto"); a.href = step.href; body.appendChild(a); }
      else body.appendChild(mk("div", step.note, "hint"));
    }
    li.appendChild(body);
    list.appendChild(li);
  });
  show("startCard", !!next);          // всё сделано — карточка больше не нужна
}

async function getJson(path, token) {
  try {
    const res = await fetch(`${apiBaseUrl()}${path}`, {headers: {Authorization: `Bearer ${token}`}});
    return res.ok ? await res.json() : null;
  } catch (e) { return null; }
}

function showLogin(message) {
  show("homeView", false); show("loginView", true);
  setLoginMsg(message);
}

// Вход — общий модуль auth.js (тот же, что у «Узнать товар», «Товаров» и «Настроек»): пароль и, если включён, второй фактор.
let authClient = null;
function auth() {
  if (!authClient) authClient = window.Auth.create(window);
  return authClient;
}
function codeField(on) {
  const f = $("loginCodeField"), c = $("loginCode");
  f.hidden = !on;
  if (!on) c.value = ""; else if (c.focus) c.focus();
}

async function doLogin(evt) {
  evt.preventDefault();
  const btn = $("loginBtn");
  setLoginMsg(null); btn.disabled = true;
  try {
    const r = await auth().login($("loginUser").value, $("loginPass").value, $("loginCode").value || "");
    codeField(!!r.needCode);
    if (r.needEnroll || r.needPasswordChange) { window.location.assign("settings.html"); return; }   // и временный пароль: смена — тоже в «Настройках»      // администратор без 2FA: настройка — в «Настройках»
    if (!r.ok) {
      if (r.key === "login.throttled") setLoginMsg(r.retryAfter ? {key: "home.throttled_min", params: {min: Math.ceil(r.retryAfter / 60)}} : {key: "home.throttled"});
      else setLoginMsg({key: r.key, params: r.params});
      return;
    }
    setToken(auth().getToken());
    $("loginPass").value = "";
    await init();
  } catch (e) { setLoginMsg({key: "login.failed", params: {reason: e.message}}); }
  finally { btn.disabled = false; }
}

async function init() {
  const token = getToken();
  if (!token) { showLogin(null); return; }
  let me;
  try {
    const res = await fetch(`${apiBaseUrl()}/api/v1/auth/me`, {headers: {Authorization: `Bearer ${token}`}});
    if (res.status === 401) { setToken(null); setSession(null, null); showLogin({key: "login.expired"}); return; }
    if (!res.ok) { showLogin({key: "home.me_failed", params: {status: res.status}}); return; }
    me = await res.json();
  } catch (e) { showLogin({key: "home.offline", params: {reason: e.message}}); return; }

  setSession(me.role, me.permissions);
  lastUser = me.username;
  $("hello").textContent = greeting(me.username);
  for (const id of ["tileIdentify", "tileProducts", "tileAdmin"]) {
    const tile = $(id);
    tile.hidden = !canSee(me.permissions, tile.getAttribute("data-needs"));
  }
  renderStatic();
  show("loginView", false); show("homeView", true);

  // «С чего начать» — только тому, кто добавляет товары. Если список товаров получить не удалось — карточку не показываем.
  show("startCard", false);
  if (canSee(me.permissions, "catalog:write")) {
    const catalog = await getJson("/api/v1/catalog/products?limit=3", token);
    lastCount = Array.isArray(catalog) ? catalog.length : null;
    renderChecklist(onboardingSteps({catalogCount: lastCount}));
  }
}

// Смена языка: статичные надписи переводит I18N.apply (атрибуты data-i18n), остальное перерисовывается здесь.
function onLanguageChange() {
  renderStatic();
  paintLoginMsg();
  if (lastUser !== null) $("hello").textContent = greeting(lastUser);
  if (lastCount !== null && !$("startCard").hidden) renderChecklist(onboardingSteps({catalogCount: lastCount}));
}

function startI18n() {
  const I = typeof window !== "undefined" ? window.I18N : null;
  if (!I) return;
  if (typeof I.apply === "function") I.apply();
  if (typeof I.mountSwitcher === "function") I.mountSwitcher($("langSwitch"));
  if (typeof I.onChange === "function") I.onChange(onLanguageChange);
}

startI18n();
renderStatic();
$("loginForm").addEventListener("submit", doLogin);
init();
