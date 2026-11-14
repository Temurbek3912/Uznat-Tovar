"""
Показывает адрес для телефона и QR-код: наведите камеру телефона — страница съёмки откроется (по Wi-Fi, без кабеля).

    python -m scripts.phone_url              # порт 8000
    python -m scripts.phone_url --port 8080
    python -m scripts.phone_url --host 192.168.1.50      # если IP определился не тот (VPN, несколько адаптеров)

Определяет IP компьютера в локальной сети. Сам сервер не запускает: сначала
`uvicorn app.main:app --host 0.0.0.0 --port 8000` (именно 0.0.0.0, иначе телефон не достучится).
QR-код рисуется, если установлен необязательный пакет `qrcode` (pip install qrcode); без него печатается только адрес.
"""
from __future__ import annotations

import argparse
import io
import socket


def _outgoing_ip() -> str:
    """IP интерфейса, которым компьютер выходит в сеть. UDP-«соединение» ничего не отправляет — лишь выбирает интерфейс."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]


def lan_ip(outgoing=_outgoing_ip) -> str | None:
    try:
        ip = outgoing()
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def phone_urls(ip: str, port: int = 8000, scheme: str = "http") -> dict[str, str]:
    base = f"{scheme}://{ip}:{port}"
    return {"home": f"{base}/ui/index.html", "identify": f"{base}/ui/identify.html", "products": f"{base}/ui/products.html",
            "shop": f"{base}/ui/shop.html"}


def render_qr(url: str, *, invert: bool = True, qr_module=None) -> str | None:
    """ASCII-QR для терминала или None, если пакет qrcode не установлен."""
    if qr_module is None:
        try:
            import qrcode as qr_module
        except ImportError:
            return None
    qr = qr_module.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=invert)
    return buf.getvalue()


def main(argv: list[str] | None = None, *, lan=lan_ip, out=print, qr_module=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", help="указать IP вручную вместо автоопределения")
    ap.add_argument("--light", action="store_true", help="QR для светлой темы терминала (по умолчанию — для тёмной)")
    args = ap.parse_args(argv)

    ip = args.host or lan()
    if not ip:
        out("Не удалось определить IP в локальной сети (нет подключения?). Укажите вручную: --host 192.168.1.50 "
            "(адрес смотрите командой ipconfig, строка «IPv4-адрес»).")
        return 1
    urls = phone_urls(ip, args.port)
    out(f"Откройте на телефоне (телефон и компьютер — в ОДНОЙ Wi-Fi сети):\n\n    {urls['home']}\n")
    qr = render_qr(urls["home"], invert=not args.light, qr_module=qr_module)
    if qr is None:
        out("(QR-код не показан: pip install qrcode — и он появится здесь)")
    else:
        try:
            out(qr)
        except UnicodeEncodeError:           # старая кодировка консоли Windows не знает блочных символов
            out("(консоль не показывает QR-код — откройте адрес вручную)")
    out("Проверьте, что сервер запущен с --host 0.0.0.0, а порт разрешён в брандмауэре (RUNBOOK, п. 6.5).")
    out(f"Прямые ссылки: узнать товар {urls['identify']}   товары {urls['products']}")
    out("Иконка на экране: откройте адрес в браузере телефона и добавьте страницу на главный экран (подсказка внизу страницы).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
