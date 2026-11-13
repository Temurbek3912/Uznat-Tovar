// Сквозная проверка страниц «Товары» и «Узнать товар» в настоящем Chromium с поддельной камерой. Только для разработки.
//
//   python tools/e2e_assets.py /tmp/e2e-assets                       # картинки и «видео с камеры»
//   node tools/ui_screenshots/e2e.js <папка-вывода> [/tmp/e2e-assets]
//
// Сам запускает tools/e2e_demo.py (настоящий код маршрутов + подставные база, очередь и эмбеддер «средний цвет»), открывает страницы
// в Chromium, проходит сценарии и печатает ok/FAIL по каждой проверке; в папку вывода кладёт скриншоты.
// Нужны Node.js, пакет playwright и Chromium: PLAYWRIGHT_MODULE, CHROME_PATH (необязательно) — как в shoot.js.
// НЕ проверяется: настоящая модель распознавания (DINOv2), настоящий шум веб-камеры.
"use strict";
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("child_process");
const fs = require("fs"), path = require("path"), assert = require("assert");

const ROOT = path.resolve(__dirname, "..", "..");
const OUT = path.resolve(process.argv[2] || path.join(__dirname, "out-e2e"));
const ASSETS = path.resolve(process.argv[3] || "/tmp/e2e-assets");
const PORT = 8777;
const BASE = `http://127.0.0.1:${PORT}`;
fs.mkdirSync(OUT, { recursive: true });

let passed = 0, failed = 0;
const recent = [];                                           // последние ответы API — печатаются при провале проверки
async function check(name, fn) {
  try { await fn(); passed++; console.log(`ok   ${name}`); } catch (e) {
    failed++;
    console.log(`FAIL ${name}\n     ${(e && e.message || e).split("\n").slice(0, 6).join("\n     ")}`);
    if (process.env.E2E_VERBOSE) console.log(recent.slice(-10).map((l) => `       · ${l}`).join("\n"));
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function startServer() {
  const work = fs.mkdtempSync("/tmp/e2e-work-");
  const child = spawn("python3", [path.join(ROOT, "tools", "e2e_demo.py"), "--port", String(PORT), "--workdir", work], { cwd: ROOT });
  let log = "";
  child.stderr.on("data", (d) => { log += d; });
  return new Promise((resolve, reject) => {
    child.stdout.on("data", (d) => { if (String(d).includes("READY")) resolve({ child, work }); });
    child.on("exit", (code) => reject(new Error(`сервер завершился (${code}): ${log}`)));
    setTimeout(() => reject(new Error("сервер не запустился за 30 с")), 30000);
  });
}

// Озвучки в программе больше нет: «сторож» записывает любое обращение страницы к синтезатору речи, проверка ниже требует, чтобы его не было.
const INIT = `
  sessionStorage.setItem("shelf_audit_token", "demo-token");
  Object.defineProperty(window, "speechSynthesis", { configurable: true, get() { window.__speechTouched = true; return undefined; } });
`;

async function text(page, sel) { return (await page.locator(sel).first().textContent() || "").trim(); }
async function visible(page, sel) { return page.locator(sel).first().isVisible(); }

(async () => {
  const { child, work } = await startServer();
  const launchOpts = { args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-video-capture=${path.join(ASSETS, "camera.y4m")}`, "--autoplay-policy=no-user-gesture-required"] };
  if (process.env.CHROME_PATH) launchOpts.executablePath = process.env.CHROME_PATH;
  const browser = await chromium.launch(launchOpts);
  const errors = [];
  const newPage = async (viewport) => {
    const context = await browser.newContext({ viewport, permissions: ["camera"], acceptDownloads: false });
    await context.addInitScript(INIT);
    const page = await context.newPage();
    page.on("response", async (r) => { if (/\/api\/v1\/catalog/.test(r.url())) { try { const json = (r.headers()["content-type"] || "").includes("json"); recent.push(`${r.status()} ${r.request().method()} ${r.url().replace(BASE, "")} ${json ? (await r.text()).slice(0, 110) : "[не JSON]"}`); } catch (e) { /* страница закрыта */ } } });
    page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
    page.on("console", (m) => { if (m.type() === "error" && !/favicon|Failed to load resource/.test(m.text())) errors.push(`console: ${m.text()}`); });
    return { page, context };
  };
  const laptop = { width: 1366, height: 800 }, phone = { width: 390, height: 844 };
  const photos = (name) => [1, 2, 3].map((i) => path.join(ASSETS, `${name}-${i}.jpg`));
  const originalSize = (name) => photos(name).reduce((s, f) => s + fs.statSync(f).size, 0);

  // ============================================================ страница «Товары»
  const { page, context } = await newPage(laptop);
  const uploadSizes = [];
  page.on("request", (r) => { if (/products\/upload$/.test(r.url())) uploadSizes.push((r.postDataBuffer() || Buffer.alloc(0)).length); });

  await page.goto(`${BASE}/ui/products.html`);
  await page.waitForSelector("#mainArea", { state: "visible", timeout: 10000 });

  await check("пустой каталог: подсказка «добавьте первый», кнопка «Добавить товар» выключена", async () => {
    await page.waitForSelector("#listEmpty", { state: "visible", timeout: 10000 });          // список грузится отдельным запросом
    assert.ok(await visible(page, "#listEmpty"));
    assert.strictEqual(await page.locator("#submitBtn").isDisabled(), true);
  });
  await page.screenshot({ path: path.join(OUT, "products-empty-laptop.png") });

  async function addProduct(name, files, extra = {}) {
    await page.fill("#nameInput", name);
    if (extra.price || extra.barcode) await page.evaluate(() => { document.querySelector("details.more").open = true; });   // открыть, а не переключить
    if (extra.price) await page.fill("#priceInput", extra.price);
    if (extra.barcode) await page.fill("#barcodeInput", extra.barcode);
    await page.setInputFiles("#photoInput", files);
    await page.waitForFunction((n) => document.querySelectorAll("#photoStrip .ph").length === n, files.length, { timeout: 15000 });
  }

  await addProduct("Кола 0,5 л", photos("cola"), { price: "9 900", barcode: "4 780001 234562" });
  await check("после выбора фото: 3 миниатюры, подсказка «хорошо», кнопка включилась", async () => {
    assert.strictEqual(await page.locator("#photoStrip .ph").count(), 3);
    assert.ok((await text(page, "#photosHint")).includes("3 фото — хорошо"));
    assert.strictEqual(await page.locator("#submitBtn").isDisabled(), false);
  });
  await page.screenshot({ path: path.join(OUT, "products-filled-laptop.png"), fullPage: true });
  await page.click("#submitBtn");
  await page.waitForSelector("#doneBlock", { state: "visible", timeout: 30000 });
  await check("товар добавлен: сообщение «Кола 0,5 л добавлен», в списке появился с 3 фото и ценой", async () => {
    assert.ok((await text(page, "#doneText")).includes("«Кола 0,5 л» добавлен"));
    await page.waitForFunction(() => document.querySelectorAll("#productList .prod").length === 1, null, { timeout: 10000 });
    const line = await text(page, "#productList .prod .sub");
    assert.ok(line.includes("3 фото") && line.includes("9 900 сум") && line.includes("штрих-код 4780001234562"), line);
  });
  await check("браузер уменьшил фото перед отправкой (3×~2,7 МБ -> заметно меньше), сервер принял и сохранил как JPEG ≤1600 px", async () => {
    assert.strictEqual(uploadSizes.length, 1);
    assert.ok(uploadSizes[0] < originalSize("cola") * 0.5, `отправлено ${uploadSizes[0]} Б при исходных ${originalSize("cola")} Б`);
    const stored = path.join(work, "media", "catalog-uploads", "KOLA-0.5-L");
    const files = fs.readdirSync(stored).filter((f) => /^\d{3}-[0-9a-f]{12}\.jpg$/.test(f));
    assert.strictEqual(files.length, 3, fs.readdirSync(stored).join(","));
  });
  await check("миниатюра товара реально загрузилась (картинка с размерами > 0)", async () => {
    await page.waitForFunction(() => { const i = document.querySelector("#productList .prod .thumb img"); return i && i.naturalWidth > 0; }, null, { timeout: 10000 });
  });
  await page.screenshot({ path: path.join(OUT, "products-done-laptop.png"), fullPage: true });

  await page.click("#againBtn");
  await addProduct("Лимонад лайм", photos("lime").slice(0, 2).concat([path.join(ASSETS, "lime-back.jpg")]));
  await page.click("#submitBtn");
  await page.waitForSelector("#doneBlock", { state: "visible", timeout: 30000 });
  await check("штрих-код на фото упаковки прочитан браузерно-серверной цепочкой сам и привязан (без ввода руками)", async () => {
    assert.ok((await text(page, "#warnList")).includes("Найден штрих-код 5901234123457 — привязан к товару."), await text(page, "#warnList"));
  });
  await page.click("#againBtn");
  await addProduct("Вода голубая", photos("sky"));
  await page.click("#submitBtn");
  await page.waitForSelector("#doneBlock", { state: "visible", timeout: 30000 });
  await page.click("#againBtn");
  await check("три товара в списке, новые сверху", async () => {
    await page.waitForFunction(() => document.querySelectorAll("#productList .prod").length === 3, null, { timeout: 10000 });
    const names = await page.locator("#productList .prod .nm").allTextContents();
    assert.deepStrictEqual(names, ["Вода голубая", "Лимонад лайм", "Кола 0,5 л"]);
  });

  await check("в списке у колы и лайма видны штрих-коды (введённый руками и найденный на фото)", async () => {
    const cola = await page.locator("#productList .prod", { hasText: "Кола 0,5 л" }).textContent();
    const lime = await page.locator("#productList .prod", { hasText: "Лимонад лайм" }).textContent();
    assert.ok(cola.includes("штрих-код 4780001234562") && lime.includes("штрих-код 5901234123457"), `${cola} | ${lime}`);
  });
  await check("штрих-код уже у другого товара: отказ с названием владельца; опечатка в коде: понятное объяснение; ввод не теряется", async () => {
    await addProduct("Кола Зеро", [photos("sky")[0]], { barcode: "4780001234562" });
    await page.click("#submitBtn");
    await page.waitForSelector("#dupNotice", { state: "visible", timeout: 10000 });
    assert.ok((await text(page, "#dupTitle")).includes("Штрих-код 4780001234562 уже привязан к товару «Кола 0,5 л»"));
    await page.click("#dupNo");
    await page.fill("#barcodeInput", "4780001234563");
    await page.click("#submitBtn");
    await page.waitForFunction(() => document.querySelector("#formError").textContent.includes("Контрольная цифра"), null, { timeout: 10000 });
    assert.strictEqual(await page.inputValue("#nameInput"), "Кола Зеро");
    await page.fill("#barcodeInput", "");
    await page.fill("#nameInput", "");
    await page.locator("#photoStrip .ph button").first().click();
    await page.evaluate(() => { document.querySelector("#formError").textContent = ""; });
  });
  await check("такое название уже есть: предложено добавить фото к существующему, и оно добавляется (а не создаётся второй товар)", async () => {
    await addProduct("кола 0.5 Л", photos("cola").slice(0, 2));
    await page.click("#submitBtn");
    await page.waitForSelector("#dupNotice", { state: "visible", timeout: 10000 });
    assert.ok((await text(page, "#dupTitle")).includes("«Кола 0,5 л» уже есть"));
    await page.click("#dupYes");
    await page.waitForSelector("#doneBlock", { state: "visible", timeout: 30000 });
    assert.ok((await text(page, "#doneText")).includes("к «Кола 0,5 л» добавлено фото: 2"));
    await page.click("#againBtn");
    await page.waitForFunction(() => document.querySelectorAll("#productList .prod").length === 3, null, { timeout: 10000 });
  });
  await check("плохой файл: понятная причина с номером фото, форма не потеряла ввод", async () => {
    await page.fill("#nameInput", "Тест плохого файла");
    await page.setInputFiles("#photoInput", [path.join(ASSETS, "not-a-photo.txt")]);
    await page.waitForFunction(() => document.querySelectorAll("#photoStrip .ph").length === 1, null, { timeout: 10000 });
    await page.click("#submitBtn");
    await page.waitForFunction(() => document.querySelector("#formError").textContent.length > 0, null, { timeout: 10000 });
    const msg = await text(page, "#formError");
    assert.ok(msg.startsWith("Фото 1:") && msg.includes("Не удалось открыть"), msg);
    assert.strictEqual(await page.inputValue("#nameInput"), "Тест плохого файла");
    assert.ok(await visible(page, "#addCard"));
    await page.locator("#photoStrip .ph button").first().click();            // человек убирает плохое фото крестиком
    assert.strictEqual(await page.locator("#photoStrip .ph").count(), 0);
  });
  await check("сбой при обработке: причина простыми словами, «Для специалиста» свёрнуто, можно вернуться", async () => {
    await page.fill("#nameInput", "Ошибка тест");
    await page.setInputFiles("#photoInput", [photos("sky")[0]]);
    await page.waitForFunction(() => document.querySelectorAll("#photoStrip .ph").length === 1, null, { timeout: 10000 });
    await page.click("#submitBtn");
    await page.waitForSelector("#failBlock", { state: "visible", timeout: 30000 });
    assert.ok((await text(page, "#failText")).includes("Фото повреждено"));
    await page.click("#failBack");
    assert.ok(await visible(page, "#addCard"));
    await page.fill("#nameInput", "");
    await page.evaluate(() => document.querySelectorAll("#photoStrip .ph button").forEach((b) => b.click()));
  });
  await check("правка: цена и название меняются, список обновляется", async () => {
    await page.locator("#productList .prod", { hasText: "Вода голубая" }).getByRole("button", { name: "Изменить" }).click();
    await page.fill("#editName", "Вода голубая 1 л");
    await page.fill("#editPrice", "3 000");
    await page.click("#editSave");
    await page.waitForFunction(() => /Вода голубая 1 л/.test(document.querySelector("#productList").textContent), null, { timeout: 10000 });
    assert.ok((await page.locator("#productList .prod", { hasText: "Вода голубая 1 л" }).textContent()).includes("3 000 сум"));
  });
  await page.screenshot({ path: path.join(OUT, "products-list-laptop.png"), fullPage: true });

  // телефон
  const mobile = await newPage(phone);
  await mobile.page.goto(`${BASE}/ui/products.html`);
  await mobile.page.waitForSelector("#mainArea", { state: "visible" });
  await check("телефон: страница не уезжает вбок, нижнее меню прокручивается и содержит «Товары» и «Узнать товар»", async () => {
    const overflow = await mobile.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 0, `горизонтальный перелом ${overflow}px`);
    const labels = await mobile.page.locator(".shell-nav .shell-link").allTextContents();
    assert.ok(labels.some((l) => l.includes("Товары")) && labels.some((l) => l.includes("Узнать товар")), labels.join("|"));
    const scrolls = await mobile.page.evaluate(() => { const n = document.querySelector(".shell-nav"); return getComputedStyle(n).overflowX; });
    assert.strictEqual(scrolls, "auto");
  });
  await mobile.page.screenshot({ path: path.join(OUT, "products-list-phone.png"), fullPage: true });
  await mobile.context.close();

  // ============================================================ страница «Узнать товар»
  const idLaptop = await newPage(laptop);
  const ip = idLaptop.page;
  await ip.goto(`${BASE}/ui/identify.html`);
  await ip.waitForSelector("#mainArea", { state: "visible", timeout: 10000 });
  // Ждём именно ОТВЕТ сервера на этот снимок (иначе новый выбор файла попал бы в ещё идущий запрос — страница такие игнорирует),
  // затем — пока ответ нарисован на экране.
  const fromFile = async (file, waitFor) => {
    const answered = ip.waitForResponse((r) => /catalog\/identify$/.test(r.url()), { timeout: 25000 });
    await ip.setInputFiles("#fileInput", [file]);
    await answered;
    await ip.waitForFunction((w) => !w || document.querySelector("#resLabel").textContent.includes(w) || document.querySelector("#resName").textContent.includes(w), waitFor, { timeout: 10000 });
    await ip.waitForTimeout(250);
  };

  await check("озвучки нет: на странице нет переключателей и кнопок голоса; ответ на экране крупно, с ценой; браузерный синтезатор речи не трогается", async () => {
    for (const sel of ["#speakToggle", "#speakPrice", "#voiceOpts", "#soundBtn", "#voiceLang"]) assert.strictEqual(await ip.locator(sel).count(), 0, sel);
    await fromFile(photos("cola")[0], "Кола");
    assert.strictEqual(await text(ip, "#resPrice"), "9 900 сум");
  });
  await check("по очереди: кола с ценой, лимонад без цены — «Цена не указана», неизвестное — «Не знаю этот товар»", async () => {
    await fromFile(photos("cola")[1], "Кола");
    await fromFile(photos("lime")[0], "Лимонад");
    assert.strictEqual(await text(ip, "#resPrice"), "Цена не указана");
    await fromFile(path.join(ASSETS, "flat.jpg"), "Не знаю");
    assert.ok((await ip.locator("#resBox").getAttribute("class")).includes("none"));
    assert.strictEqual(await ip.evaluate(() => window.__speechTouched === true), false, "страница обратилась к синтезатору речи, а озвучки больше нет");
  });
  await ip.screenshot({ path: path.join(OUT, "identify-unknown-laptop.png"), fullPage: true });

  await check("ШТРИХ-КОД на фото: «По штрих-коду», название и цена крупно, номер кода виден", async () => {
    await fromFile(path.join(ASSETS, "barcode-cola.jpg"), "По штрих-коду");
    assert.strictEqual(await text(ip, "#resName"), "Кола 0,5 л");
    assert.strictEqual(await text(ip, "#resPrice"), "9 900 сум");
    assert.strictEqual(await text(ip, "#barcodeNote"), "Штрих-код 4780001234562");
  });
  await ip.screenshot({ path: path.join(OUT, "identify-barcode-laptop.png"), fullPage: true });
  await check("штрих-код лайма (найден на фото упаковки при добавлении) тоже узнаётся — без цены", async () => {
    await fromFile(path.join(ASSETS, "barcode-lime.jpg"), "По штрих-коду");
    assert.strictEqual(await text(ip, "#resName"), "Лимонад лайм");
    assert.strictEqual(await text(ip, "#resPrice"), "Цена не указана");
  });
  await check("прочитан штрих-код, которого нет в каталоге: «Не знаю», код показан, ссылка ведёт в «Товары» с уже вписанным кодом", async () => {
    await fromFile(path.join(ASSETS, "barcode-unknown.jpg"), "Штрих-код прочитан");
    assert.strictEqual(await text(ip, "#barcodeNote"), "Штрих-код 9780201379624 пока не привязан ни к одному товару.");
    assert.ok(await ip.locator("#addHint").isVisible());
    assert.strictEqual(await ip.getAttribute("#addLink", "href"), "products.html?barcode=9780201379624");
    const next = await ip.context().newPage();
    await next.addInitScript(INIT);
    await next.goto(`${BASE}/ui/products.html?barcode=9780201379624`);
    await next.waitForSelector("#mainArea", { state: "visible", timeout: 10000 });
    assert.strictEqual(await next.inputValue("#barcodeInput"), "9780201379624");
    assert.ok(await next.locator("details.more").evaluate((d) => d.open));
    await next.close();
  });
  await check("товар узнан по виду, а на упаковке прочитан непривязанный штрих-код: кнопка «Привязать» — и дальше он узнаётся по коду", async () => {
    await fromFile(path.join(ASSETS, "sky-with-code.jpg"), "Вода");
    assert.strictEqual(await text(ip, "#resLabel"), "Это");
    assert.ok(await ip.locator("#linkBtn").isVisible());
    assert.ok((await text(ip, "#linkBtn")).startsWith("Привязать этот штрих-код к «Вода голубая"));
    await ip.click("#linkBtn");
    await ip.waitForFunction(() => document.querySelector("#barcodeNote").textContent.startsWith("Готово"), null, { timeout: 10000 });
    await fromFile(path.join(ASSETS, "barcode-sky.jpg"), "По штрих-коду");
    assert.ok((await text(ip, "#resName")).startsWith("Вода голубая"));
  });
  await check("страница «Товары» показывает привязанный вот так код у воды", async () => {
    const api = await ip.evaluate(async () => { const r = await fetch("/api/v1/catalog/products?limit=50", {headers: {Authorization: "Bearer demo-token"}}); return r.json(); });
    const water = api.find((x) => x.name.startsWith("Вода голубая"));
    assert.deepStrictEqual(water.barcodes, ["4607000000014"]);
  });

  await check("фото не из файла-картинки — понятная ошибка, страница жива", async () => {
    await ip.setInputFiles("#fileInput", [path.join(ASSETS, "not-a-photo.txt")]);
    await ip.waitForFunction(() => document.querySelector("#resError").textContent.length > 0, null, { timeout: 10000 });
    assert.ok((await text(ip, "#resError")).includes("Не удалось открыть"));
  });

  // Чистая страница перед камерой: иначе оставшийся на экране ответ от предыдущей проверки выдал бы себя за ответ камеры.
  await ip.reload();
  await ip.waitForSelector("#mainArea", { state: "visible", timeout: 10000 });
  assert.strictEqual(await text(ip, "#resLabel"), "Пока ничего не показано");
  await ip.click("#camBtn");
  await check("камера включилась: видео идёт, кнопка «Узнать товар» доступна", async () => {
    await ip.waitForFunction(() => document.querySelector("#camBox").className.includes("live"), null, { timeout: 15000 });
    await ip.waitForFunction(() => { const v = document.querySelector("#video"); return v.videoWidth > 0 && v.readyState >= 2; }, null, { timeout: 15000 });
    assert.strictEqual(await ip.locator("#snapBtn").isDisabled(), false);
  });
  const seen = [], seenCodes = [];
  const WANT = ["Кола 0,5 л", "Лимонад лайм", "Вода голубая 1 л"];
  await check("камера по очереди видит карточки и штрих-коды: показывает Колу, Лимонад, Воду; читает штрих-код колы и сообщает о неизвестном", async () => {
    const deadline = Date.now() + 110000;
    let shot = false, shotCode = false;
    while (Date.now() < deadline) {
      const name = await text(ip, "#resName"), label = await text(ip, "#resLabel");
      if (label === "Это" && !seen.includes(name)) {
        seen.push(name);
        if (!shot) { await ip.screenshot({ path: path.join(OUT, "identify-matched-laptop.png"), fullPage: true }); shot = true; }
      }
      if (label === "По штрих-коду" && !seenCodes.includes("known")) { seenCodes.push("known"); if (!shotCode) { await ip.screenshot({ path: path.join(OUT, "identify-camera-barcode-laptop.png"), fullPage: true }); shotCode = true; } }
      if (label.includes("Штрих-код прочитан") && !seenCodes.includes("unknown")) seenCodes.push("unknown");
      if (WANT.every((n) => seen.includes(n)) && seenCodes.length === 2) break;
      await sleep(300);
    }
    assert.ok(WANT.every((n) => seen.includes(n)), `по виду увидели только: ${seen.join(", ")}`);
    assert.deepStrictEqual(seenCodes.slice().sort(), ["known", "unknown"], `по штрих-кодам с камеры: ${seenCodes.join(", ")}`);
  });
  await check("история: узнанные товары перечислены по порядку, без «не знаю»", async () => {
    const items = await ip.locator("#histList li").allTextContents();
    assert.ok(items.length >= 3 && items.every((t) => !t.includes("Не знаю")), items.join(" | "));
  });
  await check("кнопка «Узнать товар» работает и по ней всегда есть ответ; повторное нажатие выключает камеру", async () => {
    await ip.evaluate(() => { document.querySelector("#autoToggle").checked = false; });
    const answered = ip.waitForResponse((r) => /catalog\/identify$/.test(r.url()), { timeout: 15000 });
    await ip.click("#snapBtn");
    await answered;
    await ip.click("#camBtn");
    await ip.waitForFunction(() => !document.querySelector("#camBox").className.includes("live"), null, { timeout: 5000 });
    assert.strictEqual(await ip.locator("#snapBtn").isDisabled(), true);
  });
  await idLaptop.context.close();

  const idPhone = await newPage(phone);
  await idPhone.page.goto(`${BASE}/ui/identify.html`);
  await idPhone.page.waitForSelector("#mainArea", { state: "visible" });
  await idPhone.page.setInputFiles("#fileInput", [photos("sky")[1]]);
  await idPhone.page.waitForFunction(() => document.querySelector("#resName").textContent.includes("Вода"), null, { timeout: 15000 });
  await check("телефон: результат и камера помещаются, горизонтального перелома нет", async () => {
    const overflow = await idPhone.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 0, `перелом ${overflow}px`);
  });
  await idPhone.page.screenshot({ path: path.join(OUT, "identify-matched-phone.png"), fullPage: true });
  await idPhone.context.close();

  await check("в консоли браузера нет ошибок JS", async () => { assert.deepStrictEqual(errors, []); });

  await context.close();
  await browser.close();
  child.kill();
  console.log(`\n${passed} passed${failed ? `, ${failed} FAILED` : ""}`);
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("ПРОГОН ПРЕРВАН:", e && e.stack || e); process.exit(2); });
