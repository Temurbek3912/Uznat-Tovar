// Общее меню страниц сотрудников (frontend/shell.js, редизайн 2026-11-15) без браузера: пункты по правам, текущий пункт по странице
// и разделу, имя и роль, «Выйти» — на страницу входа, клавиши «/», «N», Esc; меню подключено на всех страницах управления и только там.
// Запуск из корня проекта:  node tools/shell_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { FakeDocument, makeStorage } = require("./fake_dom.js");
const FRONT = path.join(__dirname, "..", "frontend");
const I18N = require(path.join(FRONT, "i18n.js")), Icons = require(path.join(FRONT, "icons.js")), Auth = require(path.join(FRONT, "auth.js"));
const Shell = require(path.join(FRONT, "shell.js"));
const SRC = fs.readFileSync(path.join(FRONT, "shell.js"), "utf-8");
const DICTS = { ru: require(path.join(FRONT, "i18n.ru.js")), uz: require(path.join(FRONT, "i18n.uz.js")),
                "uz-Cyrl": require(path.join(FRONT, "i18n.uz-cyrl.js")), en: require(path.join(FRONT, "i18n.en.js")) };

let passed = 0;
async function test(name, fn) { try { await fn(); passed++; console.log("ok  ", name); } catch (err) { console.error("FAIL:", name); throw err; } }
const flush = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0)); };
const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) });
const ALL = ["catalog:read", "catalog:write", "store:manage"];

function boot(opts) {
  opts = opts || {};
  const doc = new FakeDocument();
  const nav = doc.createElement("nav"); nav.setAttribute("id", "rail"); nav.setAttribute("data-shell", ""); doc.body.appendChild(nav);
  (opts.extra || []).forEach((n) => doc.body.appendChild(n(doc)));
  doc.addEventListener = (t, fn) => { (doc._ev = doc._ev || {})[t] = fn; };
  const i18n = I18N.create({ localStorage: makeStorage(), navigator: { languages: ["ru"] }, document: doc });
  for (const [c, d] of Object.entries(DICTS)) i18n.add(c, d);
  const replaced = [], calls = [], ev = {};
  const session = makeStorage(Object.assign(opts.token === null ? {} : { shelf_audit_token: "tok" }, opts.session || {}));
  const win = {
    document: doc, sessionStorage: session,
    location: { protocol: "http:", origin: "http://localhost:8000", pathname: opts.path || "/ui/products.html", hash: opts.hash || "", search: "",
                replace: (u) => replaced.push(u) },
    addEventListener: (t, fn) => { ev[t] = fn; },
    fetch: (url) => {
      calls.push(String(url));
      if (String(url).endsWith("/auth/me")) return Promise.resolve(opts.me === undefined ? json(200, { username: "admin", role: "admin", permissions: ALL }) : opts.me);
      return Promise.resolve(json(200, {}));
    },
  };
  const app = Shell.start(win, { I18N: i18n, Icons, Auth });
  return { doc, win, nav, app, i18n, replaced, calls, ev, session, ready: async () => { if (app) await app.ready; await flush(); },
           links: () => nav.byTag("a").filter((a) => a.className.includes("rail__item")),
           labels: () => nav.byClass("rail__label").map((n) => n.textContent) };
}

(async () => {
  await test("пункты по правам: администратор — все семь; продавец — «Проверить по фото» и «Настройки»; без прав — только «Настройки»", () => {
    assert.deepStrictEqual(Shell.itemsFor(ALL).map((i) => i.id), ["products", "add", "card", "users", "identify", "system", "settings"]);
    assert.deepStrictEqual(Shell.itemsFor(["catalog:read"]).map((i) => i.id), ["identify", "settings"]);
    assert.deepStrictEqual(Shell.itemsFor(null).map((i) => i.id), ["settings"]);
    for (const i of Shell.ITEMS) for (const [c, d] of Object.entries(DICTS)) assert.ok(d[i.label] !== undefined, `${c}: нет ${i.label}`);
    for (const i of Shell.ITEMS) assert.ok(Icons.has(i.icon), i.icon);
    assert.ok(Shell.ITEMS.filter((i) => i.mobile).length <= 5, "на телефоне в нижней панели — не больше пяти пунктов");
  });

  await test("текущий пункт: по странице и разделу управления", () => {
    assert.strictEqual(Shell.activeId("/ui/products.html", ""), "products");
    assert.strictEqual(Shell.activeId("/ui/products.html", "#add"), "add");
    assert.strictEqual(Shell.activeId("/ui/admin.html", "#/users"), "users");
    assert.strictEqual(Shell.activeId("/ui/admin.html", ""), "system");
    assert.strictEqual(Shell.activeId("/ui/admin.html", "#/card"), "card");
    assert.strictEqual(Shell.activeId("/ui/identify.html", ""), "identify");
    assert.strictEqual(Shell.activeId("/ui/shop.html", ""), null);
  });

  await test("меню рисуется после «кто я»: подписи на языке приложения, текущий пункт отмечен, имя и роль внизу; права запоминаются", async () => {
    const s = boot({ path: "/ui/admin.html", hash: "#/users" });
    await s.ready();
    assert.deepStrictEqual(s.labels().slice(0, 7), ["Товары", "Добавить товар", "Карточка покупателя", "Сотрудники", "Проверить по фото", "Состояние программы", "Настройки"]);
    const current = s.links().filter((a) => a.getAttribute("aria-current") === "page");
    assert.strictEqual(current.length, 1); assert.strictEqual(current[0].getAttribute("href"), "admin.html#/users");
    assert.ok(s.nav.byClass("rail__who")[0].textContent.includes("admin"));
    assert.strictEqual(s.session.getItem("shelf_audit_perms"), JSON.stringify(ALL));
    s.i18n.setLang("en"); await flush();
    assert.strictEqual(s.labels()[0], DICTS.en["nav.products"]);
    s.win.location.hash = "#/card"; s.ev.hashchange(); await flush();
    assert.strictEqual(s.links().find((a) => a.getAttribute("aria-current") === "page").getAttribute("href"), "admin.html#/card");
    assert.ok(!SRC.includes("innerHTML") && !SRC.includes("insertAdjacentHTML"));
  });

  await test("без входа меню пустое и сервер не спрашивается; «Выйти» — выход и страница входа", async () => {
    const a = boot({ token: null }); await a.ready();
    assert.strictEqual(a.nav.children.length, 0); assert.deepStrictEqual(a.calls, []);
    const s = boot(); await s.ready();
    s.doc.getElementById("shellLogout").click(); await flush();
    assert.ok(s.calls.some((u) => u.endsWith("/auth/logout")));
    assert.strictEqual(s.session.getItem("shelf_audit_token"), null); assert.deepStrictEqual(s.replaced, ["login.html"]);
  });

  await test("клавиши: «/» — к поиску, «N» (и «Т» в русской раскладке) — новый товар, Esc — закрыть; при наборе текста не мешают", async () => {
    let added = 0, closed = 0;
    const s = boot({ extra: [
      (doc) => { const i = doc.createElement("input"); i.setAttribute("data-hotkey", "/"); i.setAttribute("id", "q"); return i; },
      (doc) => { const b = doc.createElement("button"); b.setAttribute("data-hotkey", "n"); b.click = () => { added++; }; return b; },
      (doc) => { const b = doc.createElement("button"); b.setAttribute("data-hotkey", "esc"); b.click = () => { closed++; }; return b; },
    ] });
    await s.ready();
    const key = (k, target) => { let stopped = false; s.doc._ev.keydown({ key: k, target: target || s.doc.body, preventDefault() { stopped = true; } }); return stopped; };
    assert.ok(key("/")); assert.strictEqual(s.doc.activeElement, s.doc.getElementById("q"));
    key("n"); key("т"); assert.strictEqual(added, 2);
    key("n", s.doc.getElementById("q")); assert.strictEqual(added, 2, "в поле ввода буква — это текст");
    key("Escape", s.doc.getElementById("q")); assert.strictEqual(closed, 1);
    s.doc._ev.keydown({ key: "n", ctrlKey: true, target: s.doc.body, preventDefault() {} }); assert.strictEqual(added, 2, "Ctrl+N — браузеру");
  });

  await test("подключение: меню на страницах управления (и только там); страницы покупателя и вход — без меню", () => {
    for (const page of ["products.html", "identify.html", "settings.html", "admin.html"]) {
      const html = fs.readFileSync(path.join(FRONT, page), "utf-8");
      assert.ok(/<nav class="rail" id="rail" data-shell/.test(html) && html.includes('<script src="shell.js"></script>') && html.includes('data-start="Shell"'), page);
      assert.ok(html.indexOf('src="auth.js"') < html.indexOf('src="shell.js"'), page);
    }
    for (const page of ["shop.html", "shop-list.html", "login.html"]) {
      const html = fs.readFileSync(path.join(FRONT, page), "utf-8");
      assert.ok(!html.includes("shell.js"), page);
    }
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
