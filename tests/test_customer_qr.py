"""QR-код страницы покупателя для сотрудников (2026-11-16): какой адрес в QR, лист для печати, доступ только со входом."""
from __future__ import annotations

import io

import pytest

from app.services import customer_qr as C


def test_address_prefers_internet_then_shop_wifi():
    a = C.customer_address(public_url="https://shop.tail1.ts.net/ui/shop.html", lan_ip="192.168.1.5", port=8000, scheme="http")
    assert (a.url, a.source) == ("https://shop.tail1.ts.net/ui/shop.html", "internet")
    a = C.customer_address(public_url="", lan_ip="192.168.1.5", port=8000, scheme="http")
    assert (a.url, a.source) == ("http://192.168.1.5:8000/ui/shop.html", "wifi")
    a = C.customer_address(public_url="http://not-https.example/ui/shop.html", lan_ip=None, port=8000, scheme="https")
    assert (a.url, a.source) == ("", "none"), "в QR из интернета — только https"


def test_print_sheet_is_an_a4_png_with_a_readable_code():
    pytest.importorskip("qrcode")
    from PIL import Image

    url = "https://shop.tail1.ts.net/ui/shop.html"
    png = C.print_sheet_png(url)
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG" and image.size == (1240, 1754)
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    text, _points, _ = cv2.QRCodeDetector().detectAndDecode(np.array(image.convert("L")))
    assert text == url


def test_qr_svg_is_a_data_uri():
    pytest.importorskip("qrcode")
    assert C.qr_svg_data_uri("https://x.example/ui/shop.html").startswith("data:image/svg+xml;base64,")
    assert C.qr_svg_data_uri("") == ""


def test_routes_need_a_signed_in_staff_member_and_give_the_current_address(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("qrcode")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import routes_catalog
    from app.api.deps import get_current_user
    from app.core.error_handlers import install_error_handlers
    from app.core.security import CurrentUser, Role

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes_catalog.router)
    client = TestClient(app, base_url="http://localhost:8000")
    assert client.get("/api/v1/catalog/customer-qr").status_code == 401
    assert client.get("/api/v1/catalog/customer-qr/print.png").status_code == 401

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(id="u1", username="kassir", role=Role.MERCHANDISER)
    monkeypatch.setenv(C.ENV_PUBLIC_URL, "https://shop.tail1.ts.net/ui/shop.html")
    body = client.get("/api/v1/catalog/customer-qr").json()
    assert body["url"] == "https://shop.tail1.ts.net/ui/shop.html" and body["source"] == "internet"
    assert body["qr"].startswith("data:image/svg+xml;base64,")
    res = client.get("/api/v1/catalog/customer-qr/print.png")
    assert res.status_code == 200 and res.headers["content-type"] == "image/png" and res.content[:4] == b"\x89PNG"
    assert "qr-pokupatel.png" in res.headers["content-disposition"]

    monkeypatch.delenv(C.ENV_PUBLIC_URL)
    monkeypatch.setattr(C, "lan_ip", lambda: "192.168.1.5")
    body = client.get("/api/v1/catalog/customer-qr").json()
    assert (body["url"], body["source"]) == ("http://192.168.1.5:8000/ui/shop.html", "wifi")
    monkeypatch.setattr(C, "lan_ip", lambda: None)
    assert client.get("/api/v1/catalog/customer-qr/print.png").status_code == 409
