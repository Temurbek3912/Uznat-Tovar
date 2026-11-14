"""
API программы на двух входах в одном процессе (docs/PUBLIC_ACCESS.md):
  * обычный — --host/--port (как `uvicorn app.main:app`), страницы сотрудников и покупателя в сети магазина;
  * вход покупателя — 127.0.0.1:--customer-port, только страница покупателя и публичный API (app/core/customer_gate.py).
    К нему подключается туннель (Tailscale Funnel, Cloudflare Tunnel, свой прокси). Без HTTPS: шифрует сам туннель, а снаружи
    компьютера этот порт недоступен.

Один процесс — модели распознавания (облегчённый режим) загружаются один раз. Ctrl+C останавливает оба входа.

    python -m scripts.serve --port 8000 --customer-port 8100 [--ssl-certfile F --ssl-keyfile F]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys


def _servers(port: int, customer_port: int, host: str = "0.0.0.0", ssl_certfile: str | None = None,
             ssl_keyfile: str | None = None):
    import uvicorn

    os.environ["CUSTOMER_PORT"] = str(customer_port)              # тот же порт видит CustomerGateMiddleware
    from app.main import app

    class _Follower(uvicorn.Server):
        """Второй вход: сигналы ловит основной вход, этот останавливается вместе с ним."""

        @contextlib.contextmanager
        def capture_signals(self):
            yield

    main = uvicorn.Server(uvicorn.Config(app, host=host, port=port, ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile))
    # proxy_headers=False: адрес покупателя из заголовка туннеля читает сам вход покупателя (customer_gate), uvicorn его не трогает
    customer = _Follower(uvicorn.Config(app, host="127.0.0.1", port=customer_port, lifespan="off", proxy_headers=False))
    return main, customer


async def _serve_both(main, customer) -> None:
    follower = asyncio.create_task(customer.serve())
    try:
        await main.serve()
    finally:
        customer.should_exit = True
        await follower


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--customer-port", type=int, default=8100)
    ap.add_argument("--ssl-certfile")
    ap.add_argument("--ssl-keyfile")
    a = ap.parse_args(argv)
    if a.customer_port == a.port:
        print("Порт входа покупателя должен отличаться от основного (--customer-port).", file=sys.stderr)
        return 2
    main_srv, customer = _servers(a.port, a.customer_port, a.host, a.ssl_certfile, a.ssl_keyfile)
    asyncio.run(_serve_both(main_srv, customer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
