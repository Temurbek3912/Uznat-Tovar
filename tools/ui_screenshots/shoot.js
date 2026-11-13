// Скриншоты страниц frontend/ на разных размерах экрана с ПОДСТАВНЫМ API (настоящий сервер и БД не нужны). Только для разработки.
//
//   node tools/ui_screenshots/shoot.js <папка-вывода> [имя-сценария ...]
//
// Нужны Node.js, пакет playwright и браузер Chromium. Пути можно задать переменными окружения:
//   PLAYWRIGHT_MODULE — путь к модулю playwright (по умолчанию "playwright");  CHROME_PATH — путь к chrome/chromium;
//   FRONT — папка с html (по умолчанию ../../frontend);  SCENARIOS — json со сценариями (по умолчанию scenarios.json рядом).
// Сценарий: {name, page, query?, auth?, role?, nostores?, dark?, full?, sizes?:[laptop|wide|tablet|phone]}.
// В конце печатается горизонтальный «перелом» страницы (должен быть 0px) и ошибки JS. Данные ответов API — в mock() ниже: они выдуманные.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const http = require("http"), fs = require("fs"), path = require("path");

const FRONT = path.resolve(process.env.FRONT || path.join(__dirname, "..", "..", "frontend"));
const OUT = process.argv[2] || path.join(__dirname, "out");
const ONLY = process.argv.slice(3);
fs.mkdirSync(OUT, { recursive: true });

const MIME = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
               ".png": "image/png", ".webmanifest": "application/manifest+json", ".ico": "image/x-icon" };
const server = http.createServer((req, res) => {
  let p = decodeURIComponent(req.url.split("?")[0]);
  if (!p.startsWith("/ui/")) { res.writeHead(404); return res.end("no"); }
  const file = path.join(FRONT, p.slice(4) || "index.html");
  if (!file.startsWith(FRONT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); return res.end("no"); }
  res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});

const SHELVES = [
  { shelf_id: "makro-voda", name: "Напитки, стеллаж 1", registered: true, planogram_slots: 18 },
  { shelf_id: "makro-soki", name: "Соки", registered: true, planogram_slots: 12 },
  { shelf_id: "makro-snacks", name: "Снеки", registered: true, planogram_slots: 0 },
];
const STORES = [
  { id: "s1", name: "Макро Чиланзар", address: "ул. Бунёдкор, 1", format: "supermarket", shelves: SHELVES },
  { id: "s2", name: "Макро Юнусабад", address: "", format: "hypermarket", shelves: [] },
];
const ME = (role) => ({ username: role === "admin" ? "admin" : "m.karimov", role, all_stores: role === "admin", store_ids: ["s1"],
  permissions: role === "admin" ? ["snapshot:upload", "report:read", "review:manage", "catalog:read", "catalog:write", "user:manage", "store:manage", "camera:manage", "camera:agent"] : role === "merchandiser" ? ["snapshot:upload", "report:read", "review:manage", "catalog:read"] : ["report:read", "catalog:read"] });
const SLOTS = [];
for (let r = 0; r < 3; r++) for (let c = 0; c < 6; c++) {
  const st = (r === 0 && c === 5) || (r === 2 && c >= 4) ? "empty" : (r === 1 && c === 3) || (r === 2 && c === 3) ? "misplaced" : (r === 1 && c === 5) ? "foreign" : "matched";
  SLOTS.push({ r, c, status: st, label: st === "empty" ? undefined : ["COLA-0.5L", "WATER-0.5L", "CHIPS-CLASSIC"][(r + c) % 3], expected: st === "matched" ? undefined : "JUICE-ORANGE-1L" });
}
const DASH = { has_data: true, snapshot_id: "snap1", generated_at: "2026-10-14T09:40:00Z", osa_score: 0.83, compliance_score: 0.67,
  osa_trend_7d: [0.91, 0.89, 0.85, 0.88, 0.80, 0.83, 0.83], slots: SLOTS };
const QUEUE = { items: [{ detection_id: "d1", snapshot_id: "snap1", shelf_id: "makro-voda", created_at: "2026-10-14T09:40:00", shelf_row: 1, slot_index: 3,
  detector_confidence: 0.91, match_score: 0.62, suggested_product_id: "p1", suggested_sku: "COLA-0.5L", suggested_name: "Кола 0,5 л", crop_url: "/api/v1/review/d1/crop.jpg" }],
  total: 7, limit: 50, offset: 0 };
const OVERVIEW = { period: { days: 7, from: "2026-10-07T00:00:00Z", to: "2026-10-14T00:00:00Z" },
  totals: { stores: 2, shelves: 3, products: 48, users: 4, cameras: { total: 3, online: 2, error: 1, disabled: 0, unknown: 0 } },
  snapshots: { total: 41, by_status: { done: 37, failed: 2, processing: 1, queued: 1 } },
  audit: { reports: 37, avg_osa: 0.86, avg_compliance: 0.74, note: "Средние считаются только по снимкам со статусом done и готовым отчётом" },
  agent: { online: true, last_seen_at: "2026-10-14T09:39:50Z", status: "online", message: "Агент на связи" },
  recent_events: [{ time: "2026-10-14T08:10:00Z", camera: "Камера Напитки", kind: "camera_error", message: "Нет связи с камерой" }] };

function mock(route) {
  const url = new URL(route.request().url());
  const p = url.pathname, method = route.request().method();
  const role = process.env.ROLE || "admin";
  const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  if (p === "/api/v1/auth/login") return json({ access_token: "t", token_type: "bearer", expires_in: 3600, username: "admin", role });
  if (p === "/api/v1/auth/me") return json(ME(role));
  if (p === "/api/v1/auth/logout") return json({ ok: true });
  if (p === "/api/v1/stores") return json(process.env.NOSTORES ? [] : STORES);
  if (/\/api\/v1\/stores\/[^/]+\/dashboard$/.test(p)) return json(DASH);
  if (p === "/api/v1/review/queue") return json(QUEUE);
  if (p.endsWith("/crop.jpg")) return route.fulfill({ status: 200, contentType: "image/png", body: fs.readFileSync(path.join(FRONT, "icons/icon-192.png")) });
  if (p === "/api/v1/admin/overview") return json(OVERVIEW);
  if (p === "/api/v1/admin/system") return json({ server_time_utc: "2026-10-14T09:40:00Z", timezone: "Asia/Tashkent", server_time_local: "2026-10-14 14:40:00", database: "ok", queue: "ok",
    worker: { online: true, nodes: ["celery@w1"] }, agent: OVERVIEW.agent, storage: { path: "data", usage: { files: 120, bytes: 52428800 }, disk: { free_bytes: 90e9, total_bytes: 250e9 } }, settings: {}, settings_note: "" });
  if (p === "/api/v1/cameras") return json({ cameras: [], counts: { online: 0, error: 0, disabled: 0, unknown: 0 }, agent_last_seen_at: null });
  if (p === "/api/v1/users") return json([]);
  if (p === "/api/v1/admin/products") return json({ total: 0, limit: 50, offset: 0, items: [] });
  if (p === "/api/v1/admin/daily-report") return json({ date: "2026-10-13", timezone: "Asia/Tashkent", has_data: false, totals: { total: 0, done: 0, failed: 0, pending: 0, reports: 0, avg_osa: null, avg_compliance: null }, stores: [], camera_issues: [] });
  if (p === "/api/v1/catalog/products") return json([]);
  return json({ detail: "mock: not implemented " + method + " " + p }, 404);
}

// Сценарии: имя -> {page, query, auth, steps(page)}
const SCENARIOS = JSON.parse(fs.readFileSync(process.env.SCENARIOS || path.join(__dirname, "scenarios.json"), "utf-8"));
const SIZES = { laptop: { width: 1366, height: 768 }, wide: { width: 1920, height: 1080 }, tablet: { width: 820, height: 1180 }, phone: { width: 390, height: 844 } };

(async () => {
  await new Promise((r) => server.listen(8765, "127.0.0.1", r));
  const browser = await chromium.launch({ ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}), args: ["--no-sandbox"] });
  const log = [];
  for (const sc of SCENARIOS) {
    if (ONLY.length && !ONLY.includes(sc.name)) continue;
    process.env.ROLE = sc.role || "admin"; if (sc.nostores) process.env.NOSTORES = "1"; else delete process.env.NOSTORES;
    for (const sizeName of sc.sizes || ["laptop", "phone"]) {
      const ctx = await browser.newContext({ colorScheme: sc.dark ? "dark" : "light", viewport: SIZES[sizeName], hasTouch: sizeName === "phone" || sizeName === "tablet", isMobile: sizeName === "phone", deviceScaleFactor: 1 });
      const page = await ctx.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
      page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource|ERR_/.test(m.text())) errors.push("console: " + m.text()); });
      await page.route("**/api/v1/**", mock);
      await page.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());     // внешних ресурсов нет: как в магазине без интернета
      if (sc.auth) await page.addInitScript(([t, r]) => { sessionStorage.setItem("shelf_audit_token", t); sessionStorage.setItem("shelf_audit_role", r); }, ["tok", sc.role || "admin"]);
      await page.goto(`http://127.0.0.1:8765/ui/${sc.page}${sc.query || ""}`, { waitUntil: "load" });
      await page.waitForTimeout(500);
      if (sc.steps) for (const step of sc.steps) { await page.evaluate(step); await page.waitForTimeout(250); }
      const file = path.join(OUT, `${sc.name}.${sizeName}.png`);
      await page.screenshot({ path: file, fullPage: !!sc.full });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      log.push(`${sc.name}.${sizeName}: горизонтальный перелом ${overflow}px${errors.length ? " | ОШИБКИ: " + errors.join(" ; ") : ""}`);
      await ctx.close();
    }
  }
  await browser.close(); server.close();
  console.log(log.join("\n"));
})();
