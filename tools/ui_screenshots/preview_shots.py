"""Снимки витрины интерфейса (frontend/preview.html) настоящим Chromium через Playwright — для самопроверки дизайна без показа человеку.
Нужно: pip install playwright && playwright install chromium (сеть нужна один раз). Запуск из корня проекта:
  python tools/ui_screenshots/preview_shots.py --out /tmp/shots                      # набор по умолчанию
  python tools/ui_screenshots/preview_shots.py --out /tmp/shots laptop:ru:light:matched:identify phone:uz:dark:unknown:identify
Каждый вариант: устройство:язык:тема:состояние:раздел[:селектор] (устройства: laptop 1280x800, phone 390x844, tablet 820x1180);
необязательный селектор — на этот элемент нажать перед снимком (например .plist__toggle открывает фильтры на телефоне).
Печатает ошибки страницы и консоли (это важнее самих картинок) и сохраняет PNG. Шрифты здесь — какие есть в системе, а не Segoe UI пользователя."""
import argparse
import pathlib
import sys

from playwright.sync_api import sync_playwright

DEVICES = {"laptop": (1280, 800), "phone": (390, 844), "tablet": (820, 1180)}
DEFAULT = ["laptop:ru:light:matched:identify", "phone:ru:light:matched:identify"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/shots")
    ap.add_argument("--full", action="store_true", help="снимать всю страницу, а не один экран")
    ap.add_argument("--extra", default="", help="дополнительные параметры адреса, например group=category&sort=name")
    ap.add_argument("variants", nargs="*", default=DEFAULT)
    args = ap.parse_args()
    page_url = (pathlib.Path(__file__).resolve().parents[2] / "frontend" / "preview.html").as_uri()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    problems = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for variant in args.variants:
            device, lang, theme, state, view, *rest = variant.split(":")
            click = ":".join(rest)
            w, h = DEVICES[device]
            ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1, has_touch=device == "phone", reduced_motion="reduce")
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
            page.on("requestfailed", lambda r: errors.append(f"request failed: {r.url}"))
            extra = f"&{args.extra}" if args.extra else ""
            page.goto(f"{page_url}?lang={lang}&theme={theme}&state={state}&view={view}{extra}")
            page.wait_for_timeout(350)
            if click:
                page.click(click)
                page.wait_for_timeout(300)
            name = f"{device}_{lang}_{theme}_{state}_{view}{'_click' if click else ''}.png"
            page.screenshot(path=str(out / name), full_page=args.full)
            print(("ОШИБКИ " if errors else "ok     ") + name)
            for e in errors:
                print("   ", e)
            problems += len(errors)
            ctx.close()
        browser.close()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
