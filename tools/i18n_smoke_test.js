// Проверка выбора языка (frontend/i18n.js + словари) без браузера: поддельные localStorage, navigator и DOM.
// Проверяется: определение языка, запоминание выбора, переводы и запасные языки, склонения, подстановка в страницу, переключатель,
// одинаковость ключей во всех словарях. Не проверяется: настоящий браузер и качество самого перевода (узбекский — на проверку носителю языка).
// Запуск из корня проекта:  node tools/i18n_smoke_test.js
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const FRONT = path.join(__dirname, "..", "frontend");
const I18N = require(path.join(FRONT, "i18n.js"));
const RU = require(path.join(FRONT, "i18n.ru.js"));
const UZ = require(path.join(FRONT, "i18n.uz.js"));
const EN = require(path.join(FRONT, "i18n.en.js"));
const CYRL = require(path.join(FRONT, "i18n.uz-cyrl.js"));

let passed = 0;
async function test(name, fn) {
  try { await fn(); passed++; } catch (err) { console.error("FAIL:", name); throw err; }
}

function makeStorage(initial) {
  const m = new Map(Object.entries(initial || {}));
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { m.set(k, String(v)); }, _m: m };
}
function makeEl(tag) {
  return {
    tag, attrs: {}, children: [], listeners: {}, className: "", _text: "",
    setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(c) { this.children.push(c); return c; }, addEventListener(type, fn) { this.listeners[type] = fn; },
    set textContent(v) { this._text = String(v); }, get textContent() { return this._text; }
  };
}
function makeDoc() {
  return {
    documentElement: makeEl("html"), i18nEls: [], attrEls: [], createElement: makeEl,
    querySelectorAll(sel) { return sel === "[data-i18n]" ? this.i18nEls : sel === "[data-i18n-attr]" ? this.attrEls : []; }
  };
}
function make(opts) {
  opts = opts || {};
  const env = {
    localStorage: opts.storage === undefined ? makeStorage() : opts.storage,
    navigator: opts.navigator || { languages: [] },
    document: opts.document
  };
  const i = I18N.create(env);
  i.add("ru", RU); i.add("uz", UZ); i.add("uz-Cyrl", CYRL); i.add("en", EN);
  return i;
}

(async () => {
  await test("без выбора и с неподдерживаемым языком браузера — русский", async () => {
    assert.strictEqual(make({ navigator: { languages: ["de-DE", "fr"] } }).getLang(), "ru");
    assert.strictEqual(make({ navigator: {} }).getLang(), "ru");
  });

  await test("язык браузера: берётся первый поддерживаемый по порядку (uz-UZ, en-GB, de -> en)", async () => {
    assert.strictEqual(make({ navigator: { languages: ["uz-UZ", "ru"] } }).getLang(), "uz");
    assert.strictEqual(make({ navigator: { languages: ["en-GB"] } }).getLang(), "en");
    assert.strictEqual(make({ navigator: { languages: ["de", "en-US"] } }).getLang(), "en");
    assert.strictEqual(make({ navigator: { language: "uz-Latn-UZ" } }).getLang(), "uz");
  });

  await test("сохранённый выбор сильнее языка браузера; мусор в хранилище игнорируется", async () => {
    assert.strictEqual(make({ storage: makeStorage({ app_lang: "en" }), navigator: { languages: ["uz"] } }).getLang(), "en");
    assert.strictEqual(make({ storage: makeStorage({ app_lang: "klingon" }), navigator: { languages: ["uz"] } }).getLang(), "uz");
  });

  await test("автоопределённый язык на диск не пишется — только выбор человека", async () => {
    const storage = makeStorage();
    make({ storage, navigator: { languages: ["uz"] } }).getLang();
    assert.strictEqual(storage._m.size, 0);
  });

  await test("выбор запоминается: после «перезапуска» (новый экземпляр) язык тот же", async () => {
    const storage = makeStorage();
    const first = make({ storage, navigator: { languages: ["ru"] } });
    assert.strictEqual(first.setLang("uz"), true);
    assert.strictEqual(storage._m.get("app_lang"), "uz");
    const second = make({ storage, navigator: { languages: ["ru"] } });
    assert.strictEqual(second.getLang(), "uz");
    assert.strictEqual(second.t("nav.products"), UZ["nav.products"]);
  });

  await test("явный выбор запоминается, даже если совпал с автоопределённым", async () => {
    const storage = makeStorage();
    make({ storage, navigator: { languages: ["en"] } }).setLang("en");
    assert.strictEqual(storage._m.get("app_lang"), "en");
  });

  await test("несуществующий язык отвергается: ничего не меняется и не пишется", async () => {
    const storage = makeStorage();
    const i = make({ storage });
    assert.strictEqual(i.setLang("de"), false);
    assert.strictEqual(i.setLang(null), false);
    assert.strictEqual(i.getLang(), "ru");
    assert.strictEqual(storage._m.size, 0);
  });

  await test("варианты записи: uz-UZ, EN, ru_RU принимаются", async () => {
    const i = make();
    assert.ok(i.setLang("uz-UZ")); assert.strictEqual(i.getLang(), "uz");
    assert.ok(i.setLang("EN")); assert.strictEqual(i.getLang(), "en");
    assert.ok(i.setLang("ru_RU")); assert.strictEqual(i.getLang(), "ru");
  });

  await test("слушатели: вызываются при смене, не вызываются при том же языке, отписка работает, сломанный не мешает остальным", async () => {
    const i = make();
    const seen = [];
    i.onChange(() => { throw new Error("сломанный слушатель"); });
    const off = i.onChange((lang) => seen.push(lang));
    i.setLang("uz"); i.setLang("uz"); i.setLang("en");
    assert.deepStrictEqual(seen, ["uz", "en"]);
    off(); i.setLang("ru");
    assert.deepStrictEqual(seen, ["uz", "en"]);
  });

  await test("переводы на всех трёх языках; подстановка {имя}; неизвестное имя остаётся как есть", async () => {
    const i = make();
    assert.strictEqual(i.t("identify.show_to_camera", null, "ru"), "Покажите товар камере");
    assert.strictEqual(i.t("identify.show_to_camera", null, "uz"), UZ["identify.show_to_camera"]);
    assert.strictEqual(i.t("identify.show_to_camera", null, "en"), "Show the product to the camera");
    assert.strictEqual(i.t("barcode.code", { code: "4780\u02BB12" }, "uz"), UZ["barcode.code"].replace("{code}", "4780\u02BB12"));
    assert.ok(i.t("barcode.code", {}, "ru").includes("{code}"));
  });

  await test("запасные языки: uz -> ru -> en, en -> ru, нигде нет — возвращается ключ", async () => {
    const i = make();
    i.add("ru", { "x.ru": "только по-русски" });
    i.add("en", { "x.en": "english only" });
    assert.strictEqual(i.t("x.ru", null, "uz"), "только по-русски");
    assert.strictEqual(i.t("x.en", null, "uz"), "english only");
    assert.strictEqual(i.t("x.ru", null, "en"), "только по-русски");
    assert.strictEqual(i.t("x.nowhere", null, "uz"), "x.nowhere");
  });

  await test("склонения: русский (1 / 2 / 5 / 11 / 21 / 22 / 25), английский (0 / 1 / 2), узбекский всегда одна форма", async () => {
    const i = make();
    const ru = (n) => i.t("items.count", { n }, "ru");
    assert.deepStrictEqual([1, 2, 5, 11, 21, 22, 25].map(ru),
      ["1 товар", "2 товара", "5 товаров", "11 товаров", "21 товар", "22 товара", "25 товаров"]);
    const en = (n) => i.t("items.count", { n }, "en");
    assert.deepStrictEqual([0, 1, 2].map(en), ["0 products", "1 product", "2 products"]);
    assert.deepStrictEqual([1, 7].map((n) => i.t("items.count", { n }, "uz")), [1, 7].map((n) => UZ["items.count"].other.replace("{n}", n)));
  });

  await test("apply: <html lang>, [data-i18n] и [data-i18n-attr] переводятся; смена языка перерисовывает страницу", async () => {
    const doc = makeDoc();
    const title = makeEl("h1"); title.setAttribute("data-i18n", "identify.show_to_camera");
    const input = makeEl("input"); input.setAttribute("data-i18n-attr", "placeholder:common.search;aria-label:nav.products");
    doc.i18nEls.push(title); doc.attrEls.push(input);
    const i = make({ document: doc });
    i.apply();
    assert.strictEqual(doc.documentElement.getAttribute("lang"), "ru");
    assert.strictEqual(title.textContent, "Покажите товар камере");
    assert.strictEqual(input.getAttribute("placeholder"), "Поиск");
    i.setLang("uz");
    assert.strictEqual(doc.documentElement.getAttribute("lang"), "uz");
    assert.strictEqual(title.textContent, UZ["identify.show_to_camera"]);
    assert.strictEqual(input.getAttribute("placeholder"), UZ["common.search"]);
    assert.strictEqual(input.getAttribute("aria-label"), UZ["nav.products"]);
  });

  await test("хранилище недоступно (бросает ошибки) — язык всё равно работает в памяти", async () => {
    const broken = { getItem() { throw new Error("SecurityError"); }, setItem() { throw new Error("QuotaExceeded"); } };
    const i = make({ storage: broken });
    assert.strictEqual(i.getLang(), "ru");
    assert.ok(i.setLang("en"));
    assert.strictEqual(i.getLang(), "en");
    const env = { navigator: { languages: ["uz"] } };
    Object.defineProperty(env, "localStorage", { get() { throw new Error("SecurityError"); } });
    assert.strictEqual(I18N.create(env).getLang(), "uz");
  });

  await test("переключатель: четыре кнопки с названиями на своих языках, aria-pressed, клик меняет и запоминает язык", async () => {
    const doc = makeDoc(); const storage = makeStorage();
    const i = make({ document: doc, storage });
    i.add("ru", { "lang.title": "Язык" });
    const box = makeEl("div");
    const group = i.mountSwitcher(box);
    assert.strictEqual(box.children[0], group);
    assert.deepStrictEqual(group.children.map((b) => b.textContent), ["Русский", "O\u02BBzbekcha", "Ўзбекча", "English"]);
    assert.deepStrictEqual(group.children.map((b) => b.getAttribute("aria-pressed")), ["true", "false", "false", "false"]);
    assert.strictEqual(group.getAttribute("role"), "group");
    assert.deepStrictEqual(group.children.map((b) => b.getAttribute("data-short")), ["RU", "UZ", "ЎЗ", "EN"]);
    assert.deepStrictEqual(group.children.map((b) => b.getAttribute("lang")), ["ru", "uz", "uz-Cyrl", "en"]);
    group.children[2].listeners.click();
    assert.strictEqual(i.getLang(), "uz-Cyrl");
    assert.strictEqual(storage._m.get("app_lang"), "uz-Cyrl");
    group.children[1].listeners.click();
    assert.strictEqual(i.getLang(), "uz");
    assert.strictEqual(storage._m.get("app_lang"), "uz");
    assert.deepStrictEqual(group.children.map((b) => b.getAttribute("aria-pressed")), ["false", "true", "false", "false"]);
    assert.strictEqual(group.getAttribute("aria-label"), UZ["lang.title"]);
  });

  await test("словари: одинаковые ключи, нет пустых значений, одинаковые {подстановки}, формы склонений полные", async () => {
    const keys = (d) => Object.keys(d).sort();
    assert.deepStrictEqual(keys(UZ), keys(RU));
    assert.deepStrictEqual(keys(CYRL), keys(RU));
    assert.deepStrictEqual(keys(EN), keys(RU));
    const slots = (v) => (typeof v === "string" ? [v] : Object.values(v)).join(" ").match(/\{\w+\}/g) || [];
    const uniq = (v) => [...new Set(slots(v))].sort();
    for (const k of keys(RU)) {
      for (const [name, d] of [["ru", RU], ["uz", UZ], ["uz-Cyrl", CYRL], ["en", EN]]) {
        const v = d[k];
        const parts = typeof v === "string" ? [v] : Object.values(v);
        assert.ok(parts.length > 0 && parts.every((p) => typeof p === "string" && p.trim() !== ""), `${name}:${k} пусто`);
      }
      assert.deepStrictEqual(uniq(UZ[k]), uniq(RU[k]), `подстановки ${k}: uz != ru`);
      assert.deepStrictEqual(uniq(EN[k]), uniq(RU[k]), `подстановки ${k}: en != ru`);
      assert.deepStrictEqual(uniq(CYRL[k]), uniq(RU[k]), `подстановки ${k}: uz-Cyrl != ru`);
    }
    for (const c of ["one", "few", "many", "other"]) assert.ok(typeof RU["items.count"][c] === "string", `ru: нет формы ${c}`);
    for (const c of ["one", "other"]) assert.ok(typeof EN["items.count"][c] === "string", `en: нет формы ${c}`);
    assert.ok(typeof UZ["items.count"].other === "string");
    assert.ok(typeof CYRL["items.count"].other === "string");
  });

  await test("сообщения сервера: has() по цепочке языков, вложенная причина {key, params} переводится тем же языком", async () => {
    const i = make({});
    i.setLang("en");
    assert.strictEqual(i.has("server.photo_numbered"), true);
    assert.strictEqual(i.has("server.no_such_code"), false);
    const params = { n: 2, reason: { key: "server.photo_too_small", params: { width: 40, height: 30 } } };
    assert.strictEqual(i.t("server.photo_numbered", params), "Photo 2: The photo is too small (40×30). Take it closer.");
    i.setLang("ru");
    assert.strictEqual(i.t("server.photo_numbered", params), "Фото 2: Фото слишком маленькое (40×30). Снимите крупнее.");
    i.setLang("uz-Cyrl");
    assert.ok(/[\u0400-\u04FF]/.test(i.t("server.photo_numbered", params)) && !i.t("server.photo_numbered", params).includes("{"));
    assert.strictEqual(i.t("server.field_required", { field: { key: "server.field_brand" } }, "en"), "Enter the brand.");
    const loop = { key: "server.photo_numbered", params: {} }; loop.params.reason = loop;          // зацикленная ссылка не вешает страницу
    assert.ok(typeof i.t("server.photo_numbered", { n: 1, reason: loop }) === "string");
  });

  await test("сообщения сервера: список вложенных причин (запрет удаления) переводится поэлементно и соединяется через «; »; строки — как есть; глубина ограничена", async () => {
    const i = make({});
    const reasons = [{ key: "server.blocker_store_shelves", params: { n: 3 } }, { key: "server.blocker_store_cameras", params: { n: 2 } }, "старая причина"];
    assert.strictEqual(i.t("server.delete_blocked", { reasons }, "en"), "Cannot delete: the store has shelves (3) — delete them first; the store has cameras (2); старая причина");
    assert.strictEqual(i.t("server.delete_blocked", { reasons }, "ru"), "Удалить нельзя: в магазине есть полки (3) — удалите их сначала; в магазине есть камеры (2); старая причина");
    assert.ok(i.t("server.delete_blocked", { reasons }, "uz-Cyrl").startsWith("Ўчириб бўлмайди: дўконда жавонлар бор (3)"));
    assert.strictEqual(i.t("server.delete_blocked", { reasons: "строкой, как раньше" }, "ru"), "Удалить нельзя: строкой, как раньше");
    const deep = []; deep.push(deep);                                                                    // зацикленный список
    assert.ok(typeof i.t("server.delete_blocked", { reasons: deep }, "ru") === "string");
    assert.strictEqual(i.t("server.delete_blocked", { reasons: [null, "", "x"] }, "ru"), "Удалить нельзя: x");
  });

  await test("узбекская кириллица: код uz-Cyrl, язык браузера uz-Cyrl-UZ, сохранённый «uz» остаётся латиницей", async () => {
    assert.strictEqual(make({ navigator: { languages: ["uz-Cyrl-UZ", "ru"] } }).getLang(), "uz-Cyrl");
    assert.strictEqual(make({ navigator: { languages: ["uz_cyrl"] } }).getLang(), "uz-Cyrl");
    assert.strictEqual(make({ storage: makeStorage({ app_lang: "uz" }), navigator: { languages: ["uz-Cyrl"] } }).getLang(), "uz");
    assert.strictEqual(make({ navigator: { languages: ["uz-Latn-UZ"] } }).getLang(), "uz");
    assert.strictEqual(I18N.normalize("UZ-CYRL"), "uz-Cyrl");
    assert.strictEqual(I18N.normalize("uz-Arab"), "uz");      // нет такого варианта — основной (латиница)
  });

  await test("узбекская кириллица: перевод, запасной путь кириллица -> латиница -> русский, <html lang>", async () => {
    const doc = makeDoc();
    const i = make({ document: doc });
    i.add("uz", { "x.latin_only": "faqat lotin" });
    i.add("ru", { "x.ru_only": "только по-русски" });
    assert.ok(i.setLang("uz-Cyrl"));
    assert.strictEqual(i.t("nav.products"), CYRL["nav.products"]);
    assert.strictEqual(i.t("x.latin_only"), "faqat lotin");
    assert.strictEqual(i.t("x.ru_only"), "только по-русски");
    assert.strictEqual(doc.documentElement.getAttribute("lang"), "uz-Cyrl");
    assert.deepStrictEqual([1, 7].map((n) => i.t("items.count", { n })), [1, 7].map((n) => CYRL["items.count"].other.replace("{n}", n)));
  });

  await test("узбекская кириллица: в словаре нет латиницы, кроме подстановок и технических слов (коды, адреса, команды)", async () => {
    const allowed = /^(?:\{\w+\}|https?|HTTPS?|Wi-Fi|Windows|IPv4-?|Address|KOMPYUTER-IP|IP|QR-?|LOGIN|API|VPN|FA|ui\/admin\.html|Redis|worker|AUTH_SECRET_KEY|Google|Drive|aistudio.google.com|Get|key|.bat|Microsoft|Authenticator|docker|compose|up|-d|configs\/config\.yaml|localhost|python|Zoom|iPhone|JPG|PNG|WEBP|HEIC|catalog|product_images|workerni|EAN-\d+|UPC-A|M200|ALLOW_DEBUG_EMBEDDER|docs\/[\w./]+|scripts\.\w+|\.env|env|-m|--dir|dir)$/;
    for (const [k, v] of Object.entries(CYRL)) {
      const text = typeof v === "string" ? v : Object.values(v).join(" ");
      const stripped = text.replace(/\{\w+\}/g, " ").replace(/\b[A-Z]:\\/g, " ");   // буква диска: D:\Нусхалар
      for (const token of stripped.match(/[.\-]*[A-Za-z][\w./\-]*/g) || []) {
        assert.ok(allowed.test(token.replace(/[.,;:)]+$/, "")), `${k}: латиница «${token}» в кириллическом словаре`);
      }
      assert.ok(!/[A-Za-z][\u0400-\u04FF]|[\u0400-\u04FF][A-Za-z]/.test(text), `${k}: смесь кириллицы и латиницы в слове`);
    }
  });

  await test("узбекский словарь: апостроф только U+02BB (ʻ), без обычного ' и без кириллицы внутри слов латиницы", async () => {
    for (const [k, v] of Object.entries(UZ)) {
      const text = typeof v === "string" ? v : Object.values(v).join(" ");
      assert.ok(!text.includes("'"), `${k}: обычный апостроф`);
      assert.ok(!/[A-Za-z][\u0400-\u04FF]|[\u0400-\u04FF][A-Za-z]/.test(text), `${k}: смесь кириллицы и латиницы в слове`);
    }
  });

  await test("в браузере: файл подключается как глобальный I18N, словари дописываются, выбор берётся из localStorage окна", async () => {
    const ctx = { navigator: { languages: ["ru"] }, localStorage: makeStorage({ app_lang: "uz" }), document: undefined };
    ctx.window = ctx;
    vm.createContext(ctx);
    for (const f of ["i18n.js", "i18n.ru.js", "i18n.uz.js", "i18n.uz-cyrl.js", "i18n.en.js"]) vm.runInContext(fs.readFileSync(path.join(FRONT, f), "utf-8"), ctx, { filename: f });
    assert.strictEqual(typeof ctx.I18N.t, "function");
    assert.strictEqual(typeof ctx.I18N.create, "function");
    assert.strictEqual(ctx.I18N.getLang(), "uz");
    assert.strictEqual(ctx.I18N.t("nav.products"), UZ["nav.products"]);
    ctx.I18N.setLang("uz-Cyrl");
    assert.strictEqual(ctx.I18N.t("nav.products"), CYRL["nav.products"]);
    ctx.I18N.setLang("en");
    assert.strictEqual(ctx.localStorage.getItem("app_lang"), "en");
    assert.strictEqual(ctx.I18N.t("nav.products"), "Products");
  });

  await test("в i18n.js нет innerHTML/outerHTML/insertAdjacentHTML/eval — страница меняется только через textContent и setAttribute", async () => {
    const src = fs.readFileSync(path.join(FRONT, "i18n.js"), "utf-8");
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) {
      assert.ok(!src.includes(bad), `в i18n.js найдено ${bad}`);
    }
  });

  console.log(`\n${passed} passed`);
})().catch((err) => { console.error("FAIL:", err && err.stack || err); process.exit(1); });
