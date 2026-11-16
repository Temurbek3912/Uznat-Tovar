// Проверка страницы «Настройки» (frontend/settings.html + settings.js) без браузера: настоящая разметка разбирается в подделку страницы, настоящий settings.js работает на ней;
// хранилище и сервер — подделки, которыми управляет тест. Проверяется ЛОГИКА: тема, язык, переключатель страницы «Узнать товар» (тот же ключ и те же правила разбора, что у самой страницы),
// пункты меню по правам без обязательного входа, а также то, что озвучки на странице больше нет.
// Не проверяется: настоящий сервер, внешний вид (его смотрят по снимкам Chromium: tools/ui_screenshots/settings_shots.py).
// Запуск из корня проекта:  node tools/settings_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { FakeDocument, parseHtml, makeStorage } = require("./fake_dom.js");
const FRONT = path.join(__dirname, "..", "frontend");
const load = (f) => require(path.join(FRONT, f));
const I18N = load("i18n.js"), Theme = load("theme.js"), Icons = load("icons.js"), Settings = load("settings.js"), Identify = load("identify.js"), Auth = load("auth.js");
const HTML = fs.readFileSync(path.join(FRONT, "settings.html"), "utf-8");
const SCRIPT = fs.readFileSync(path.join(FRONT, "settings.js"), "utf-8");
const CSS = fs.readFileSync(path.join(FRONT, "ui.css"), "utf-8");
const DICTS = { ru: load("i18n.ru.js"), uz: load("i18n.uz.js"), en: load("i18n.en.js") };

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; } }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const flush = async () => { for (let i = 0; i < 8; i++) await sleep(0); };
const json = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });

function boot(opts) {
  opts = opts || {};
  const doc = new FakeDocument(); parseHtml(doc, HTML);
  const local = opts.local || makeStorage(opts.stored ? { identify_settings: opts.stored } : {});
  if (opts.lang) local.setItem("app_lang", opts.lang);
  const session = makeStorage(Object.assign(opts.token ? { shelf_audit_token: opts.token } : {}, opts.session || {}));
  const i18n = I18N.create({ localStorage: local, navigator: { languages: ["ru"] }, document: doc });
  for (const c of ["ru", "uz", "en"]) i18n.add(c, DICTS[c]);
  const theme = Theme.create({ localStorage: local, document: doc, matchMedia: () => ({ matches: false }) });
  const calls = [], errors = [];
  const fetch = async (url, o) => {
    // «Можно ли отправлять пароль отсюда» (документ 18, §14): по умолчанию — защищённый канал; в список вызовов не попадает.
    if (String(url).endsWith("/api/v1/auth/transport")) return json(200, opts.transport || { secure: true, required: true, https_url: null });
    calls.push({ url, opts: o || {} }); if (opts.fetch) return opts.fetch(url, o); return json(200, opts.me || { permissions: [] });
  };
  const win = { document: doc, I18N: i18n, Theme: theme, Auth, Icons, fetch, localStorage: local, sessionStorage: session, location: { protocol: "http:", origin: "http://app", pathname: "/ui/settings.html", hash: "", search: "",
                                                                                                                     replace(u) { win.redirects.push(u); } }, redirects: [] };
  // озвучки в программе больше нет: любое обращение страницы к синтезатору речи или к модулю Voice — ошибка (попадает в s.errors)
  const forbidden = (name) => ({ get() { errors.push(new Error("страница обратилась к win." + name + ": озвучки больше нет")); return undefined; }, enumerable: true });
  Object.defineProperties(win, { Voice: forbidden("Voice"), speechSynthesis: forbidden("speechSynthesis") });
  const $ = (id) => doc.getElementById(id);
  const app = Settings.start(win, { onError: (e) => errors.push(e) });
  return {
    doc, win, i18n, theme, local, session, calls, errors, $, app,
    ready: async () => { await app.ready; await flush(); },
    shown: (id) => !$(id).hasAttribute("hidden"),
    click: async (id) => { $(id).click(); await sleep(5); await flush(); },
    submit: async (user, pass) => { const r = await Auth.create(win).login(user, pass, ""); await app.reload(); await flush(); return r; },
    stored: () => JSON.parse(local.getItem("identify_settings") || "null")
  };
}

(async () => {
  await test("разметка: id, которые трогает settings.js, есть на странице и не повторяются; меню: «Настройки» — текущая", async () => {
    const markup = HTML.replace(/<script[\s\S]*?<\/script>/gi, "");
    const ids = [...markup.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
    assert.strictEqual(new Set(ids).size, ids.length);
    for (const id of new Set([...SCRIPT.matchAll(/\$\("([A-Za-z][\w-]*)"\)/g)].map((m) => m[1]))) assert.ok(ids.includes(id), `в разметке нет #${id}`);
    const s = boot(); await s.ready();
    assert.strictEqual(s.$("rail").getAttribute("data-shell"), "", "меню — общее (shell.js)");
    assert.ok(!HTML.includes('id="loginUser"') && !HTML.includes('id="loginPass"'), "своей формы входа нет — кнопка «Войти» ведёт на login.html");
  });

  await test("скрипты и стили: язык раньше значков и страницы, озвучки нет, без внешних адресов, в странице только запуск модуля; все ключи текстов есть в трёх словарях; у каждого класса есть правило в ui.css; нет innerHTML", async () => {
    const scripts = [...HTML.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]), at = (n) => scripts.indexOf(n);
    assert.ok(at("i18n.js") < at("i18n.ru.js") && at("icons.js") < at("settings.js") && at("auth.js") < at("settings.js") && scripts.indexOf("settings.js") < scripts.indexOf("boot.js") && scripts[scripts.length - 1] === "boot.js");
    assert.strictEqual(at("voice.js"), -1, "озвучки больше нет: модуль голоса не подключается");
    assert.ok(!/(?:src|href)="(?:https?:)?\/\//.test(HTML));
    assert.deepStrictEqual([...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)], []);
    assert.ok(/<script src="theme\.js"><\/script>\s*<script src="theme-apply\.js"><\/script>/.test(HTML) && /<script src="boot\.js" data-start="Settings"><\/script>\s*<script src="boot\.js" data-start="Shell"><\/script>\s*<\/body>/.test(HTML));
    const prefixes = new Set(Object.keys(DICTS.ru).map((k) => k.split(".")[0]));
    const keys = new Set([...SCRIPT.matchAll(/"([a-z_]+\.[a-z_]+)"/g)].map((m) => m[1]).filter((k) => prefixes.has(k.split(".")[0]) && !/\.(html|js|css|json)$/.test(k)));
    for (const m of HTML.matchAll(/data-i18n="([^"]+)"/g)) keys.add(m[1]);
    for (const k of keys) for (const l of ["ru", "uz", "en"]) assert.ok(Object.prototype.hasOwnProperty.call(DICTS[l], k), `нет ключа «${k}» в ${l}`);
    const classes = new Set(); for (const m of HTML.matchAll(/\bclass="([^"]+)"/g)) m[1].split(/\s+/).forEach((c) => classes.add(c)); for (const m of SCRIPT.matchAll(/className = "([^"]+)"/g)) m[1].split(/\s+/).forEach((c) => classes.add(c));
    const missing = [...classes].filter((c) => c && !new RegExp("\\." + c.replace(/[-_]/g, "[-_]") + "(?![\\w-])").test(CSS)); assert.deepStrictEqual(missing, []);
    for (const m of HTML.matchAll(/data-icon="([^"]+)"/g)) assert.ok(Icons.has(m[1]), m[1]);
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"]) assert.ok(!SCRIPT.includes(bad), bad);
  });

  await test("тема: две кнопки (светлая, тёмная) с подписями из словаря, выбранная помечена; клик меняет и запоминает; верхняя кнопка переключает", async () => {
    const s = boot(); await s.ready();
    const btns = () => s.$("themeHost").byTag("button");
    assert.deepStrictEqual(btns().map((b) => b.textContent), ["светлая", "тёмная"]);
    assert.deepStrictEqual(btns().map((b) => b.getAttribute("aria-pressed")), ["true", "false"]);
    btns()[1].click(); assert.strictEqual(s.local.getItem("app_theme"), "dark"); assert.deepStrictEqual(btns().map((b) => b.getAttribute("aria-pressed")), ["false", "true"]);
    assert.ok(s.$("themeBtn").getAttribute("aria-label").includes("тёмная"));
    await s.click("themeBtn"); assert.strictEqual(s.theme.get(), "light"); assert.strictEqual(btns()[0].getAttribute("aria-pressed"), "true");
    s.i18n.setLang("en"); await flush(); assert.deepStrictEqual(btns().map((b) => b.textContent), [DICTS.en["theme.light"], DICTS.en["theme.dark"]]);
  });

  await test("язык: два переключателя (вверху и в группе) меняют язык приложения, всё на странице переводится и запоминается", async () => {
    const s = boot(); await s.ready();
    assert.strictEqual(s.$("langHost").byClass("lang-btn").length, 4); assert.strictEqual(s.$("langHost2").byClass("lang-btn").length, 4);
    s.$("langHost2").byClass("lang-btn")[3].click(); await flush();
    assert.strictEqual(s.i18n.getLang(), "en"); assert.strictEqual(s.local.getItem("app_lang"), "en"); assert.strictEqual(s.doc.title, "Settings — Identify Product");
    assert.ok(s.$("themeBtn").getAttribute("aria-label").includes(DICTS.en["theme.light"])); assert.strictEqual(s.$("langHost").byClass("lang-btn")[3].getAttribute("aria-pressed"), "true");
    s.$("langHost").byClass("lang-btn")[1].click(); await flush(); assert.strictEqual(s.local.getItem("app_lang"), "uz"); assert.strictEqual(s.$("rail").getAttribute("aria-label"), DICTS.uz["nav.menu"]);
  });

  await test("озвучки нет: ни выбора голоса, ни «Проверить звук», ни переключателей «Говорить вслух» и «Называть и цену»; страница не трогает Voice и speechSynthesis; в словарях нет текстов про голос", async () => {
    const s = boot(); await s.ready();
    for (const id of ["voiceSelect", "voiceTest", "voiceInfo", "speakToggle", "speakPrice"]) assert.ok(s.$(id) === null, `#${id} должен исчезнуть`);
    for (const lang of ["uz", "en", "ru"]) { s.i18n.setLang(lang); await flush(); }
    assert.deepStrictEqual(s.errors, [], "ни одного обращения к озвучке: " + s.errors);
    assert.ok(!/Voice|speechSynthesis/.test(SCRIPT), "в settings.js не осталось следов озвучки");
    for (const l of ["ru", "uz", "en"]) assert.deepStrictEqual(Object.keys(DICTS[l]).filter((k) => /^(voice|speech)\./.test(k) || /^identify\.speak/.test(k) || k === "settings.voice_hint"), [], `в словаре ${l} остались тексты озвучки`);
  });

  await test("«Узнавать сама»: по умолчанию включено, сохранённое значение читается; изменение пишется в тот же ключ; поля старых записей (speak, price) отбрасываются; мусор в памяти браузера не ломает страницу", async () => {
    const d = boot(); await d.ready();
    assert.ok(d.$("autoToggle").checked, "по умолчанию включено");
    d.$("autoToggle").checked = false; d.$("autoToggle").dispatch("change");
    assert.deepStrictEqual(d.stored(), { auto: false });
    const r = boot({ stored: JSON.stringify({ speak: true, auto: false, price: false }) }); await r.ready(); assert.ok(!r.$("autoToggle").checked);
    r.$("autoToggle").checked = true; r.$("autoToggle").dispatch("change"); assert.deepStrictEqual(r.stored(), { auto: true }, "старые поля при сохранении уходят");
    const j = boot({ stored: "{не json" }); await j.ready(); assert.ok(j.$("autoToggle").checked);
  });

  await test("правила разбора настроек совпадают с самой страницей «Узнать товар» (иначе переключатели в двух местах разойдутся)", async () => {
    for (const raw of [null, undefined, "мусор", 5, [], {}, { speak: true }, { speak: "да", auto: 1, price: null }, { speak: true, auto: false, price: false, lang: "uz" }, { auto: false }])
      assert.deepStrictEqual(Settings.cleanSettings(raw), Identify.cleanSettings(raw), JSON.stringify(raw));
    assert.strictEqual(Settings.SETTINGS_KEY, Identify.SETTINGS_KEY); assert.strictEqual(Settings.SETTINGS_KEY, "identify_settings");
  });

  await test("без входа страница работает без сервера; с токеном — «кто я»; сбой или чужой токен — ошибок нет; «Войти» ведёт на общую страницу входа", async () => {
    const a = boot(); await a.ready(); assert.strictEqual(a.calls.length, 0, "без токена сервер не спрашиваем");
    a.$("loginPanel").dispatch("submit", { preventDefault() {} }); await flush();
    assert.deepStrictEqual(a.win.redirects, ["login.html?next=settings.html"]);
    const b = boot({ token: "tok", me: { permissions: ["catalog:read", "catalog:write", "store:manage"] } }); await b.ready();
    assert.strictEqual(b.calls[0].url, "http://app/api/v1/auth/me"); assert.strictEqual(b.calls[0].opts.headers.Authorization, "Bearer tok");
    const d = boot({ token: "tok", fetch: async () => json(401, {}) }); await d.ready(); assert.deepStrictEqual(d.errors, []);
    const e = boot({ token: "tok", fetch: async () => { throw new Error("offline"); } }); await e.ready(); assert.deepStrictEqual(e.errors, []);
  });

  // ============================================================ вход владельца и администратора
  const ADMIN = { username: "admin1", role: "admin", permissions: ["catalog:read", "catalog:write", "store:manage"] };
  const route = (handlers) => async (url, o) => {
    for (const [suffix, fn] of Object.entries(handlers)) if (url.endsWith(suffix)) return fn(url, o);
    throw new Error("нет ответа для " + url);
  };

  await test("вход: без токена видна форма, «Вы вошли» скрыто, сервер не спрашиваем; пока сервер не ответил, ни форма, ни имя не мигают", async () => {
    const a = boot(); await a.ready();
    assert.ok(a.shown("loginPanel") && !a.shown("accountInfo") && !a.shown("loginError")); assert.strictEqual(a.calls.length, 0);
    let release; const gate = new Promise((r) => { release = r; });
    const b = boot({ token: "tok", fetch: async () => { await gate; return json(200, ADMIN); } }); await flush();
    assert.ok(!b.shown("loginPanel") && !b.shown("accountInfo"), "пока ждём сервер — ни формы, ни имени");
    release(); await b.ready();
    assert.ok(b.shown("accountInfo") && !b.shown("loginPanel")); assert.strictEqual(b.$("accountName").textContent, "Вы вошли как admin1.");
  });

  await test("вход: удачный вход — запрос в форме, токен в sessionStorage, пароль очищен и нигде не хранится, появились имя и пункты меню", async () => {
    const s = boot({ fetch: route({ "/auth/login": () => json(200, { access_token: "tok-9", token_type: "bearer" }), "/auth/me": () => json(200, ADMIN), "/catalog/customer-qr": () => json(404, {}), "/admin/backup": () => json(404, {}) }) }); await s.ready();
    await s.submit("admin1", "se&cret 1");
    assert.strictEqual(s.session.getItem("shelf_audit_token"), "tok-9");
    const login = s.calls[0]; assert.strictEqual(login.url, "http://app/api/v1/auth/login"); assert.strictEqual(login.opts.method, "POST");
    assert.strictEqual(new URLSearchParams(login.opts.body).get("password"), "se&cret 1");
    assert.strictEqual(s.calls[1].url, "http://app/api/v1/auth/me"); assert.strictEqual(s.calls[1].opts.headers.Authorization, "Bearer tok-9");
    assert.ok(s.shown("accountInfo") && !s.shown("loginPanel") && !s.shown("loginError")); assert.strictEqual(s.$("accountName").textContent, "Вы вошли как admin1.");
    for (const store of [s.local, s.session]) assert.ok(![...store._m.values()].some((v) => v.includes("se&cret")), "пароль не сохраняется");
    assert.deepStrictEqual(s.errors, []);
  });

  await test("вход: прежняя сессия истекла — форма и «Сессия истекла», токен стёрт; сервер недоступен — форма и сообщение о связи, ошибок страницы нет", async () => {
    const a = boot({ token: "old", fetch: async () => json(401, {}) }); await a.ready();
    assert.ok(a.shown("loginPanel") && a.$("loginError").textContent.includes("Сессия истекла")); assert.strictEqual(a.session.getItem("shelf_audit_token"), null);
    const b = boot({ token: "tok", fetch: async () => { throw new Error("offline"); } }); await b.ready();
    assert.ok(b.shown("loginPanel") && b.shown("loginError") && b.$("loginError").textContent === DICTS.ru["error.network"]); assert.deepStrictEqual(b.errors, []);
    assert.strictEqual(b.session.getItem("shelf_audit_token"), "tok", "при сбое связи токен не стирается");
  });

  await test("выход: токен стирается сразу (до ответа сервера), серверу уходит прежний токен, затем — на страницу входа", async () => {
    let release; const gate = new Promise((r) => { release = r; });
    const s = boot({ token: "tok-11", fetch: route({ "/auth/me": () => json(200, ADMIN), "/catalog/customer-qr": () => json(404, {}), "/admin/backup": () => json(404, {}), "/auth/logout": async () => { await gate; return json(200, {}); } }) }); await s.ready();
    assert.ok(s.shown("accountInfo"));
    s.$("logoutBtn").click(); await flush();
    assert.strictEqual(s.session.getItem("shelf_audit_token"), null, "стёрт сразу, не дожидаясь сервера");
    assert.ok(s.shown("loginPanel") && !s.shown("accountInfo"));
    const out = s.calls.find((c) => c.url.endsWith("/auth/logout")); assert.ok(out && out.opts.method === "POST" && out.opts.headers.Authorization === "Bearer tok-11");
    release(); await flush(); assert.deepStrictEqual(s.errors, []);
    assert.deepStrictEqual(s.win.redirects, ["login.html"]);
  });

  await test("вход: тексты на странице следуют за языком — «Вы вошли как», кнопки и строка ошибки меняются на лету, имя из базы не переводится", async () => {
    const s = boot({ token: "tok", fetch: async () => json(200, ADMIN) }); await s.ready();
    s.i18n.setLang("en"); await flush();
    assert.strictEqual(s.$("accountName").textContent, DICTS.en["settings.signed_in"].replace("{name}", "admin1")); assert.strictEqual(s.$("logoutBtn").textContent, DICTS.en["settings.sign_out"]);
    s.i18n.setLang("uz"); await flush(); assert.strictEqual(s.$("accountName").textContent, DICTS.uz["settings.signed_in"].replace("{name}", "admin1"));
  });

  // ================================================================ безопасность входа (документ 18)
  const SESSIONS = [
    { id: "s-cur", created_at: "2026-11-08T05:00:00", expires_at: "2026-11-08T17:00:00", last_used_at: "2026-11-08T06:10:00", device_label: null,
      device_kind: "computer", device_os: "Windows", device_browser: "Chrome", source: "local", mfa: false, current: true },
    { id: "s-phone", created_at: "2026-11-07T10:00:00", expires_at: "2026-11-07T22:00:00", last_used_at: null, device_label: "Телефон <b>дома</b>",
      device_kind: "phone", device_os: "Android", device_browser: "Chrome", source: "remote", mfa: true, current: false }
  ];
  function secServer(state) {
    return async (url, o) => {
      const p = url.replace(/^.*\/api\/v1/, ""), method = (o && o.method) || "GET", body = o && o.body ? JSON.parse(o.body) : null;
      state.log.push(method + " " + p + (body ? " " + JSON.stringify(body) : ""));
      if (p === "/auth/me") return json(200, ADMIN);
      if (p === "/auth/2fa") return json(200, state.mfa);
      if (p === "/auth/sessions" && method === "GET") return json(200, state.sessions);
      if (p === "/auth/2fa/setup") return body.password === "right" ? json(200, { secret: "JBSWY3DPEHPK3PXP", secret_grouped: "JBSW Y3DP EHPK 3PXP", otpauth_uri: "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP" })
                                                               : json(422, { detail: "Пароль неверный", code: "password_wrong", params: {} });
      if (p === "/auth/2fa/confirm") { if (body.code !== "123456") return json(422, { code: "mfa_code_wrong", params: {} }); state.mfa = { enabled: true, enabled_at: "2026-11-08T06:00:00", available: true, pending: false }; return json(200, { status: "enabled", other_sessions_ended: 1 }); }
      if (p === "/auth/2fa/disable") { state.mfa = { enabled: false, available: true }; return json(200, { status: "disabled" }); }
      if (p.startsWith("/auth/sessions/") && method === "DELETE") { state.sessions = state.sessions.filter((s) => "/auth/sessions/" + s.id !== p); return json(200, { status: "ended" }); }
      if (p === "/auth/sessions/revoke-others") { state.sessions = state.sessions.filter((s) => s.current); return json(200, { status: "ended", count: 1 }); }
      if (p === "/auth/logout-all" || p === "/auth/logout") return json(200, {});
      return json(404, {});
    };
  }

  await test("безопасность: без входа группы нет; после входа — состояние второго фактора и список сеансов (устройство, откуда, этот сеанс, без разметки из данных)", async () => {
    const anon = boot(); await anon.ready(); assert.ok(!anon.shown("securityGroup"));
    const state = { log: [], mfa: { enabled: false, available: true, pending: false }, sessions: SESSIONS.slice() };
    const s = boot({ token: "tok", fetch: secServer(state) }); await s.ready(); await flush();
    assert.ok(s.shown("securityGroup") && s.shown("mfaEnableBtn") && !s.shown("mfaDisableBtn") && !s.shown("mfaUnavailable"));
    assert.strictEqual(s.$("mfaStatus").textContent, DICTS.ru["security.mfa_off"]);
    const items = s.$("sessionList").byTag("li");
    assert.strictEqual(items.length, 2);
    assert.ok(items[0].textContent.includes("Компьютер · Windows · Chrome") && items[0].textContent.includes("этот компьютер") && items[0].textContent.includes("этот сеанс"));
    assert.ok(items[1].textContent.includes("Телефон <b>дома</b>") && items[1].textContent.includes("интернет") && items[1].textContent.includes("со вторым фактором"));
    assert.ok(items[1].byTag("b").length === 1, "название устройства — текстом, не разметкой");
    for (const k of ["security.signed_in_at", "security.active_at"]) assert.ok(items[0].textContent.includes(DICTS.ru[k].split(" ")[0]));
    assert.ok(state.log.every((l) => !l.includes("password")), "без действий пароль не отправляется");
  });

  await test("безопасность: включение второго фактора — пароль, ключ и ссылка otpauth, код; неверный пароль и код — строки по кодам сервера; после включения ключ исчезает со страницы", async () => {
    const state = { log: [], mfa: { enabled: false, available: true, pending: false }, sessions: SESSIONS.slice() };
    const s = boot({ token: "tok", fetch: secServer(state) }); await s.ready(); await flush();
    await s.click("mfaEnableBtn"); assert.ok(s.shown("mfaSetupForm") && !s.shown("mfaEnableBtn"));
    s.$("mfaSetupPass").value = "wrong"; s.$("mfaSetupForm").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.strictEqual(s.$("securityError").textContent, DICTS.ru["server.password_wrong"]); assert.strictEqual(s.$("mfaSetupPass").value, "");
    s.$("mfaSetupPass").value = "right"; s.$("mfaSetupForm").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.ok(s.shown("mfaConfirmForm") && !s.shown("mfaSetupForm") && !s.shown("securityError"));
    assert.strictEqual(s.$("mfaSecret").textContent, "JBSW Y3DP EHPK 3PXP"); assert.ok(s.$("mfaLink").getAttribute("href").startsWith("otpauth://totp/"));
    s.$("mfaConfirmCode").value = "000000"; s.$("mfaConfirmForm").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.strictEqual(s.$("securityError").textContent, DICTS.ru["server.mfa_code_wrong"]);
    s.$("mfaConfirmCode").value = "123456"; s.$("mfaConfirmForm").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.ok(!s.shown("mfaConfirmForm") && s.shown("mfaDisableBtn") && !s.shown("mfaEnableBtn"));
    assert.strictEqual(s.$("mfaSecret").textContent, ""); assert.strictEqual(s.$("mfaLink").getAttribute("href"), "#");
    assert.strictEqual(s.$("securityDone").textContent, DICTS.ru["security.mfa_enabled_done"].replace("{n}", "1"));
    assert.ok(s.$("mfaStatus").textContent.startsWith("Второй фактор включён ("));
    assert.deepStrictEqual(s.errors, []);
  });

  await test("безопасность: выключение — пароль и код; завершить чужой сеанс, «все, кроме этого», «выйти везде» (токен стёрт, форма входа)", async () => {
    const state = { log: [], mfa: { enabled: true, enabled_at: "2026-11-08T06:00:00", available: true }, sessions: SESSIONS.slice() };
    const s = boot({ token: "tok", fetch: secServer(state) }); await s.ready(); await flush();
    await s.click("mfaDisableBtn"); s.$("mfaDisablePass").value = "right"; s.$("mfaDisableCode").value = "654321";
    s.$("mfaDisableForm").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.ok(state.log.includes('POST /auth/2fa/disable {"password":"right","code":"654321"}'));
    assert.strictEqual(s.$("mfaDisablePass").value, ""); assert.ok(s.shown("mfaEnableBtn"));
    s.$("sessionList").byTag("li")[1].byTag("button")[0].click(); await sleep(5); await flush();
    assert.ok(state.log.includes("DELETE /auth/sessions/s-phone")); assert.strictEqual(s.$("sessionList").byTag("li").length, 1);
    state.sessions = SESSIONS.slice(); await s.click("endOthersBtn");
    assert.ok(state.log.includes("POST /auth/sessions/revoke-others")); assert.strictEqual(s.$("securityDone").textContent, "Завершено сеансов: 1.");
    await s.click("logoutAllBtn");
    assert.ok(state.log.includes("POST /auth/logout-all")); assert.strictEqual(s.session.getItem("shelf_audit_token"), null);
    assert.ok(s.shown("loginPanel") && !s.shown("securityGroup"));
  });

  await test("безопасность: ключ сервера не задан — пояснение вместо кнопки; язык меняет тексты группы на лету", async () => {
    const state = { log: [], mfa: { enabled: false, available: false }, sessions: SESSIONS.slice() };
    const s = boot({ token: "tok", fetch: secServer(state) }); await s.ready(); await flush();
    assert.ok(s.shown("mfaUnavailable") && !s.shown("mfaEnableBtn"));
    s.i18n.setLang("en"); await flush();
    assert.strictEqual(s.$("mfaStatus").textContent, DICTS.en["security.mfa_off"]);
    assert.ok(s.$("sessionList").byTag("li")[0].textContent.includes("Computer · Windows · Chrome"));
  });

  // ================================================================ решения владельца В-1…В-4 (2026-11-08)
  await test("первый вход администратора без второго фактора: настройка по пропуску (без пароля), включение даёт сеанс и коды восстановления", async () => {
    const log = [];
    const s = boot({ fetch: async (url, o) => {
      const p = url.replace(/^.*\/api\/v1/, ""), method = (o && o.method) || "GET", auth = (o && o.headers && o.headers.Authorization) || "";
      log.push(method + " " + p + " " + auth);
      if (p === "/auth/login") return json(200, { mfa_enrollment_required: true, enroll_token: "enroll-1", expires_in: 600, username: "boss" });
      if (p === "/auth/2fa") return json(200, auth === "Bearer tok-full" ? { enabled: true, required: true, available: true, recovery_left: 10 }
                                                                      : { enabled: false, required: true, available: true, enrolling: true });
      if (p === "/auth/2fa/setup") return json(200, { secret: "JBSWY3DPEHPK3PXP", secret_grouped: "JBSW Y3DP EHPK 3PXP", otpauth_uri: "otpauth://totp/x" });
      if (p === "/auth/2fa/confirm") return json(200, { status: "enabled", other_sessions_ended: 0, recovery_codes: ["7KQ4M-P2XJR", "ABCDE-FGHJK"],
                                                       access_token: "tok-full", expires_in: 28800, username: "boss", role: "admin" });
      if (p === "/auth/me") return json(200, ADMIN);
      if (p === "/auth/sessions") return json(200, []);
      return json(404, {});
    } });
    await s.ready();
    await s.submit("boss", "secret-pass");
    assert.strictEqual(s.session.getItem("shelf_audit_token"), null); assert.strictEqual(s.session.getItem("shelf_audit_enroll"), "enroll-1");
    assert.ok(s.shown("securityGroup") && s.shown("enrollPanel") && !s.shown("sessionList") && !s.shown("mfaEnableBtn"));
    await s.click("enrollStartBtn");
    assert.ok(log.some((l) => l === "POST /auth/2fa/setup Bearer enroll-1"), "настройка — с пропуском, без пароля");
    assert.ok(s.shown("mfaConfirmForm") && !s.shown("enrollPanel"));
    s.$("mfaConfirmCode").value = "123456"; s.$("mfaConfirmForm").dispatch("submit", { preventDefault() {} }); await sleep(10); await flush();
    assert.strictEqual(s.session.getItem("shelf_audit_token"), "tok-full"); assert.strictEqual(s.session.getItem("shelf_audit_enroll"), null);
    assert.ok(s.shown("recoveryBox"));
    assert.deepStrictEqual(s.$("recoveryList").byTag("li").map((li) => li.textContent), ["7KQ4M-P2XJR", "ABCDE-FGHJK"]);
    assert.ok(s.shown("mfaRequired") && !s.shown("mfaDisableBtn"), "администратору выключить нельзя");
    assert.strictEqual(s.$("recoveryLeft").textContent, "Неиспользованных кодов восстановления: 10.");
    assert.ok(s.shown("accountInfo") && s.shown("sessionList"));
    await s.click("recoveryHideBtn");                       // «Я сохранил коды»: с экрана общего компьютера убраны, показать снова нельзя
    assert.ok(!s.shown("recoveryBox"));
    assert.deepStrictEqual(s.$("recoveryList").byTag("li").map((li) => li.textContent), []);
    assert.deepStrictEqual(s.errors, []);
  });

  await test("истёкший пропуск настройки — его нет, вход заново; вход по коду восстановления — предупреждение; новые коды — пароль и код", async () => {
    const expired = boot({ session: { shelf_audit_enroll: "enroll-old" }, fetch: async () => json(401, { code: "token_invalid" }) });
    await expired.ready();
    assert.strictEqual(expired.session.getItem("shelf_audit_enroll"), null);
    assert.strictEqual(expired.$("loginError").textContent, DICTS.ru["login.code_expired"]);
    const log = [];
    const s = boot({ token: "tok", session: { shelf_audit_recovery_left: "3" }, fetch: async (url, o) => {
      const p = url.replace(/^.*\/api\/v1/, ""), body = o && o.body ? o.body : "";
      log.push(p + " " + body);
      if (p === "/auth/me") return json(200, ADMIN);
      if (p === "/auth/2fa") return json(200, { enabled: true, required: true, available: true, recovery_left: 3 });
      if (p === "/auth/sessions") return json(200, []);
      if (p === "/auth/2fa/recovery-codes") return json(200, { recovery_codes: ["NEW23-CODES"] });
      return json(404, {});
    } });
    await s.ready();
    assert.ok(s.shown("recoveryWarning") && s.$("recoveryWarning").textContent.includes("(осталось кодов: 3)"));
    await s.click("recoveryNewBtn"); assert.ok(s.shown("recoveryForm"));
    s.$("recoveryPass").value = "pw"; s.$("recoveryCode").value = "123456";
    s.$("recoveryForm").dispatch("submit", { preventDefault() {} }); await sleep(10); await flush();
    assert.ok(log.includes('/auth/2fa/recovery-codes {"password":"pw","code":"123456"}'));
    assert.deepStrictEqual(s.$("recoveryList").byTag("li").map((li) => li.textContent), ["NEW23-CODES"]);
    assert.ok(!s.shown("recoveryWarning"), "после новых кодов предупреждение о входе по коду снято");
  });

  await test("смена своего пароля: новый пароль дважды, код при включённом втором факторе; ответ — новый токен; несовпадение и ошибки сервера — строкой; поля очищаются", async () => {
    const log = [];
    let reply = () => json(200, { access_token: "tok-new", token_type: "bearer", expires_in: 3600, username: "boss", role: "admin" });
    const s = boot({ token: "tok", fetch: async (url, o) => {
      const p = url.replace(/^.*\/api\/v1/, ""), body = o && o.body ? o.body : "";
      log.push(p + " " + body + " " + ((o && o.headers && o.headers.Authorization) || ""));
      if (p === "/auth/me") return json(200, ADMIN);
      if (p === "/auth/2fa") return json(200, { enabled: true, required: true, available: true, recovery_left: 10 });
      if (p === "/auth/sessions") return json(200, []);
      if (p === "/auth/password") return reply();
      return json(404, {});
    } });
    await s.ready(); await flush();
    assert.ok(s.shown("passwordButtons") && !s.shown("passwordForm"));
    await s.click("passwordChangeBtn"); assert.ok(s.shown("passwordForm") && s.shown("pwCodeField"));
    assert.strictEqual(s.$("pwNewLabel").textContent, "Новый пароль (не короче 8 символов)");
    s.$("pwCurrent").value = "old-pass-1"; s.$("pwNew").value = "new-pass-12"; s.$("pwRepeat").value = "new-pass-13";
    s.$("passwordForm").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.strictEqual(s.$("securityError").textContent, DICTS.ru["security.password_mismatch"]);
    assert.ok(!log.some((l) => l.startsWith("/auth/password")), "при несовпадении сервер не спрашиваем");
    s.$("pwRepeat").value = "new-pass-12"; s.$("pwCode").value = "123456";
    s.$("passwordForm").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.ok(log.includes('/auth/password {"current_password":"old-pass-1","new_password":"new-pass-12","code":"123456"} Bearer tok'));
    assert.strictEqual(s.session.getItem("shelf_audit_token"), "tok-new", "новый сеанс заменил прежний");
    for (const id of ["pwCurrent", "pwNew", "pwRepeat", "pwCode"]) assert.strictEqual(s.$(id).value, "", id);
    assert.strictEqual(s.$("securityDone").textContent, DICTS.ru["security.password_changed_done"]);
    assert.ok(!s.shown("passwordForm"));
    reply = () => json(422, { detail: "x", code: "password_same", params: {} });
    await s.click("passwordChangeBtn");
    s.$("pwCurrent").value = "a-pass-123"; s.$("pwNew").value = "a-pass-123"; s.$("pwRepeat").value = "a-pass-123";
    s.$("passwordForm").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.strictEqual(s.$("securityError").textContent, DICTS.ru["server.password_same"]);
    assert.strictEqual(s.session.getItem("shelf_audit_token"), "tok-new");
    assert.deepStrictEqual(s.errors, []);
  });

  await test("временный пароль: после входа — форма своего пароля (не сеанс); несовпадение и отказ сервера — строкой; без второго фактора — сразу сеанс", async () => {
    const log = [];
    let initial = () => json(422, { detail: "x", code: "password_is_login", params: {} });
    const s = boot({ fetch: async (url, o) => {
      const p = url.replace(/^.*\/api\/v1/, ""), method = (o && o.method) || "GET", auth = (o && o.headers && o.headers.Authorization) || "";
      log.push(method + " " + p + " " + auth + " " + ((o && o.body) || ""));
      if (p === "/auth/login") return json(200, { password_change_required: true, password_token: "pw-1", expires_in: 900, username: "m.karimov", code_required: false });
      if (p === "/auth/password/initial") return initial();
      if (p === "/auth/me") return json(200, { username: "m.karimov", role: "merchandiser", permissions: ["catalog:read"] });
      if (p === "/auth/2fa") return json(200, { enabled: false, required: false, available: true, recovery_left: 0 });
      if (p === "/auth/sessions") return json(200, []);
      return json(404, {});
    } });
    await s.ready();
    await s.submit("m.karimov", "7KQM-X4HP-9TDW");
    assert.ok(s.shown("initialPanel") && !s.shown("loginPanel") && !s.shown("initialCodeField"));
    assert.strictEqual(s.$("initialPrompt").textContent, DICTS.ru["login.password_change_prompt"]);
    assert.strictEqual(s.session.getItem("shelf_audit_token"), null);
    s.$("initialNew").value = "my-pass-123"; s.$("initialRepeat").value = "my-pass-124";
    s.$("initialPanel").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.strictEqual(s.$("initialError").textContent, DICTS.ru["security.password_mismatch"]);
    assert.ok(!log.some((l) => l.includes("/auth/password/initial")));
    s.$("initialRepeat").value = "my-pass-123";
    s.$("initialPanel").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.strictEqual(s.$("initialError").textContent, DICTS.ru["server.password_is_login"]);
    assert.ok(log.some((l) => l.startsWith("POST /auth/password/initial Bearer pw-1 {\"new_password\":\"my-pass-123\"}")));
    for (const id of ["initialNew", "initialRepeat", "initialCode"]) assert.strictEqual(s.$(id).value, "", id);
    initial = () => json(200, { access_token: "tok-own", token_type: "bearer", expires_in: 3600, username: "m.karimov", role: "merchandiser" });
    s.$("initialNew").value = "my-pass-125"; s.$("initialRepeat").value = "my-pass-125";
    s.$("initialPanel").dispatch("submit", { preventDefault() {} }); await sleep(10); await flush();
    assert.strictEqual(s.session.getItem("shelf_audit_token"), "tok-own");
    assert.strictEqual(s.session.getItem("shelf_audit_pwchange"), null);
    assert.ok(!s.shown("initialPanel") && s.shown("accountInfo"));
    assert.deepStrictEqual(s.errors, []);
  });

  await test("временный пароль администратора со вторым фактором: нужен код; после установки — вход заново с новым паролем", async () => {
    const s = boot({ fetch: async (url) => {
      const p = url.replace(/^.*\/api\/v1/, "");
      if (p === "/auth/login") return json(200, { password_change_required: true, password_token: "pw-2", expires_in: 900, username: "boss", code_required: true });
      if (p === "/auth/password/initial") return json(200, { status: "password_set", next: "login" });
      return json(404, {});
    } });
    await s.ready();
    await s.submit("boss", "TEMP-PASS-1234");
    assert.ok(s.shown("initialPanel") && s.shown("initialCodeField"));
    s.$("initialNew").value = "boss-own-pass"; s.$("initialRepeat").value = "boss-own-pass"; s.$("initialCode").value = "123456";
    s.$("initialPanel").dispatch("submit", { preventDefault() {} }); await sleep(10); await flush();
    assert.ok(!s.shown("initialPanel") && s.shown("loginPanel"));
    assert.strictEqual(s.$("loginError").textContent, DICTS.ru["login.password_set_sign_in"]);
    assert.strictEqual(s.session.getItem("shelf_audit_token"), null);
  });

  await test("ПИН-код (2026-11-15): только на этом компьютере; задать впервые — без текущего; сменить — с текущим; несовпадение — строкой", async () => {
    const log = []; let local = { this_computer: true, users: [], setup_needed: true };
    const s = boot({ token: "tok", fetch: async (url, o) => {
      const p = url.replace(/^.*\/api\/v1/, ""), method = (o && o.method) || "GET";
      log.push(method + " " + p + " " + ((o && o.body) || ""));
      if (p === "/auth/me") return json(200, ADMIN);
      if (p === "/auth/local") return json(200, local);
      if (p === "/auth/pin") return method === "PUT" ? json(200, { status: "pin_set" }) : json(404, {});
      if (p === "/auth/2fa") return json(200, { enabled: false, available: true });
      if (p === "/auth/sessions") return json(200, []);
      return json(404, {});
    } });
    await s.ready(); await flush();
    assert.ok(s.shown("pinGroup") && !s.shown("pinCurrentField")); assert.strictEqual(s.$("pinStatus").textContent, DICTS.ru["pin.status_off"]);
    s.$("pinNew").value = "482917"; s.$("pinRepeat").value = "482918"; s.$("pinForm").dispatch("submit", { preventDefault() {} }); await flush();
    assert.strictEqual(s.$("pinMsg").textContent, DICTS.ru["signin.mismatch"]); assert.ok(!log.some((l) => l.startsWith("PUT")));
    s.$("pinNew").value = "482917"; s.$("pinRepeat").value = "482917"; s.$("pinForm").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.ok(log.includes('PUT /auth/pin {"pin":"482917"}')); assert.strictEqual(s.$("pinMsg").textContent, DICTS.ru["pin.saved"]);
    assert.ok(s.shown("pinCurrentField"), "ПИН теперь есть — для смены нужен текущий");
    for (const id of ["pinNew", "pinRepeat", "pinCurrent"]) assert.strictEqual(s.$(id).value, "", id);
    local = { this_computer: false, users: [] };
    const far = boot({ token: "tok", fetch: async (url) => (url.endsWith("/auth/me") ? json(200, ADMIN) : url.endsWith("/auth/local") ? json(200, local) : json(200, [])) });
    await far.ready(); await flush();
    assert.ok(!far.shown("pinGroup"), "с другого устройства раздела ПИН нет");
  });

  await test("Пароль для других устройств по ПИН-коду (2026-11-15): только когда ПИН есть; несовпадение — строкой; верно — новый токен", async () => {
    const log = []; let reply = json(422, { code: "pin_wrong" });
    const s = boot({ token: "tok", fetch: async (url, o) => {
      const p = url.replace(/^.*\/api\/v1/, ""), method = (o && o.method) || "GET";
      log.push(method + " " + p + " " + ((o && o.body) || ""));
      if (p === "/auth/me") return json(200, ADMIN);
      if (p === "/auth/local") return json(200, { this_computer: true, users: [{ username: ADMIN.username }] });
      if (p === "/auth/password/by-pin") return reply;
      if (p === "/auth/2fa") return json(200, { enabled: false, available: true });
      if (p === "/auth/sessions") return json(200, []);
      return json(404, {});
    } });
    await s.ready(); await flush();
    assert.ok(s.shown("pinPwBox") && s.shown("pinPwButtons") && !s.shown("pinPwForm"));
    s.$("pinPwOpenBtn").dispatch("click"); await flush();
    assert.ok(s.shown("pinPwForm") && !s.shown("pinPwButtons"));
    const fill = (pin, a, b) => { s.$("pinPwPin").value = pin; s.$("pinPwNew").value = a; s.$("pinPwRepeat").value = b; };
    fill("482917", "my-new-password-1", "my-new-password-2"); s.$("pinPwForm").dispatch("submit", { preventDefault() {} }); await flush();
    assert.strictEqual(s.$("pinPwMsg").textContent, DICTS.ru["security.password_mismatch"]);
    assert.ok(!log.some((l) => l.includes("by-pin")), "несовпадение — без запроса");
    fill("482917", "my-new-password-1", "my-new-password-1"); s.$("pinPwForm").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.strictEqual(s.$("pinPwMsg").textContent, DICTS.ru["server.pin_wrong"]);
    reply = json(200, { access_token: "tok2", expires_in: 1800, username: ADMIN.username, role: "admin" });
    fill("482917", "my-new-password-1", "my-new-password-1"); s.$("pinPwForm").dispatch("submit", { preventDefault() {} }); await sleep(5); await flush();
    assert.ok(log.includes('POST /auth/password/by-pin {"pin":"482917","new_password":"my-new-password-1"}'));
    assert.strictEqual(s.$("pinPwMsg").textContent, DICTS.ru["pinpw.saved"]);
    assert.strictEqual(s.session.getItem("shelf_audit_token"), "tok2", "новый сеанс заменил прежний");
    assert.ok(!s.shown("pinPwForm"));
    for (const id of ["pinPwPin", "pinPwNew", "pinPwRepeat"]) assert.strictEqual(s.$(id).value, "", id);
  });

  await test("QR-код для покупателей (2026-11-16): после входа — адрес, картинка и «Скачать для печати»; без входа и при ошибке — блока нет", async () => {
    const qr = "data:image/svg+xml;base64,PHN2Zy8+";
    let reply = json(200, { url: "https://shop.example.ts.net/ui/shop.html", source: "internet", qr });
    const s = boot({ token: "tok", fetch: async (url) => {
      const p = url.replace(/^.*\/api\/v1/, "");
      if (p === "/auth/me") return json(200, { username: "kassir", role: "merchandiser", permissions: ["catalog:read"] });
      if (p === "/catalog/customer-qr") return reply;
      return json(404, {});
    } });
    await s.ready(); await flush();
    assert.ok(s.shown("customerQrGroup") && s.shown("cqrImg") && s.shown("cqrPrintBtn"));
    assert.strictEqual(s.$("cqrImg").getAttribute("src"), qr);
    assert.strictEqual(s.$("cqrUrl").textContent, "https://shop.example.ts.net/ui/shop.html");
    assert.strictEqual(s.$("cqrHint").textContent, DICTS.ru["cqr.internet"]);
    reply = json(200, { url: "http://192.168.1.5:8000/ui/shop.html", source: "wifi", qr: "javascript:alert(1)" });
    await s.app.reload(); await flush();
    assert.strictEqual(s.$("cqrHint").textContent, DICTS.ru["cqr.wifi"]); assert.ok(!s.shown("cqrImg"), "чужая картинка не показывается");
    reply = json(200, { url: "", source: "none", qr: "" });
    await s.app.reload(); await flush();
    assert.ok(s.shown("customerQrGroup") && !s.shown("cqrPrintBtn") && !s.shown("cqrUrl")); assert.strictEqual(s.$("cqrHint").textContent, DICTS.ru["cqr.none"]);
    reply = json(500, {});
    await s.app.reload(); await flush();
    assert.ok(!s.shown("customerQrGroup")); assert.deepStrictEqual(s.errors, []);
    const anon = boot(); await anon.ready();
    assert.ok(!anon.shown("customerQrGroup"));
  });

  await test("резервная копия (2026-11-16): блок только у администратора в облегчённом режиме; места, облако, ошибка, «Сделать копию сейчас»", async () => {
    let status = { available: true, running: false, last_ok: "2026-11-16T03:00:00+05:00", last_error: "", cloud: false,
                   destinations: [{ kind: "local", path: "D:\Узнать\backups\magazin\Узнать товар — копии", copies: 3 }] };
    const log = [];
    const s = boot({ token: "tok", fetch: async (url, o) => {
      const p = url.replace(/^.*\/api\/v1/, ""), m = (o && o.method) || "GET";
      if (p === "/admin/backup") log.push(m);
      if (p === "/auth/me") return json(200, { username: "admin1", role: "admin", permissions: ["catalog:read", "store:manage"] });
      if (p === "/admin/backup" && m === "GET") return json(200, status);
      if (p === "/admin/backup" && m === "POST") { status = Object.assign({}, status, { running: true }); return json(202, { started: true }); }
      return json(404, {});
    } });
    await s.ready(); await flush();
    assert.ok(s.shown("backupGroup") && s.shown("bkpNoCloud") && !s.shown("bkpError"));
    assert.ok(s.$("bkpLast").textContent.startsWith("Последняя копия:"));
    assert.strictEqual(s.$("bkpPlaces").children.length, 1);
    assert.ok(s.$("bkpPlaces").children[0].textContent.includes("(копий: 3)"));
    await s.click("bkpNowBtn");
    assert.deepStrictEqual(log, ["GET", "POST", "GET"]);
    assert.strictEqual(s.$("bkpLast").textContent, DICTS.ru["bkp.running"]); assert.ok(s.$("bkpNowBtn").disabled);
    status = Object.assign({}, status, { running: false, cloud: true, last_error: "Мало места",
                                          destinations: status.destinations.concat([{ kind: "cloud", path: "G:\Мой диск\Узнать товар — копии", copies: 1 }]) });
    await s.app.reload(); await flush();
    assert.ok(!s.shown("bkpNoCloud") && s.shown("bkpError") && !s.$("bkpNowBtn").disabled);
    assert.strictEqual(s.$("bkpError").textContent, "Последняя копия не получилась: Мало места");
    assert.ok(s.$("bkpPlaces").children[1].textContent.startsWith("В облаке:"));
    status = { available: false };
    await s.app.reload(); await flush();
    assert.ok(!s.shown("backupGroup"), "полный режим — блока нет");
    assert.deepStrictEqual(s.errors, []);
    const calls = [];
    const kassir = boot({ token: "tok", fetch: async (url) => { calls.push(url); return url.endsWith("/auth/me") ? json(200, { username: "k", role: "merchandiser", permissions: ["catalog:read"] }) : json(404, {}); } });
    await kassir.ready(); await flush();
    assert.ok(!kassir.shown("backupGroup") && !calls.some((u) => u.includes("/admin/backup")), "у продавца блока нет и запроса нет");
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
