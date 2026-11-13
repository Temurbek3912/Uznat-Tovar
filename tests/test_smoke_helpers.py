"""scripts/smoke_test.py — чистые помощники самопроверки (сам прогон по HTTP возможен только в запущенной системе)."""
from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts import smoke_test as st



def _pixels(image):
    """Пиксели изображения: Pillow 12+ — get_flattened_data, раньше — getdata (будет удалён в Pillow 14)."""
    return getattr(image, "get_flattened_data", image.getdata)()

def test_synthetic_snapshot_is_a_decodable_jpeg_of_the_requested_size():
    data = st.make_test_jpeg(640, 480)
    image = Image.open(io.BytesIO(data))
    assert image.format == "JPEG" and image.size == (640, 480)
    assert len(data) < 25 * 1024 * 1024                         # проходит лимит загрузки (MAX_UPLOAD_MB по умолчанию 25)


def test_synthetic_snapshot_has_distinct_boxes_not_a_blank_frame():
    image = Image.open(io.BytesIO(st.make_test_jpeg())).convert("RGB")
    assert len(set(_pixels(image.resize((80, 60))))) > 5


def test_poll_until_returns_as_soon_as_status_is_done():
    statuses = iter(["queued", "processing", "done", "never-reached"])
    seen = []
    ok, last = st.poll_until(lambda: next(statuses), {"done"}, timeout=60, interval=1, sleep=lambda s: None,
                             clock=lambda: 0.0, on_tick=seen.append)
    assert (ok, last) == (True, "done") and seen == ["queued", "processing", "done"]


def test_poll_until_times_out_with_the_last_status():
    now = {"t": 0.0}
    ok, last = st.poll_until(lambda: "processing", {"done"}, timeout=10, interval=4,
                             sleep=lambda s: now.__setitem__("t", now["t"] + s), clock=lambda: now["t"])
    assert ok is False and last == "processing" and now["t"] >= 10


def test_poll_until_checks_once_even_with_zero_timeout():
    ok, last = st.poll_until(lambda: "done", {"done"}, timeout=0, sleep=lambda s: None, clock=lambda: 0.0)
    assert ok is True


def test_expect_passes_on_match_and_explains_mismatch_with_body_and_hint():
    ok = SimpleNamespace(status_code=200, text="")
    assert st.expect(ok, 200, "x") is ok
    bad = SimpleNamespace(status_code=503, text='{"status":"unavailable"}')
    with pytest.raises(st.StepFailed) as exc:
        st.expect(bad, 200, "/health/ready", "docker compose logs bootstrap")
    message = str(exc.value)
    assert "503" in message and "unavailable" in message and "docker compose logs bootstrap" in message


def test_temp_admin_names_are_random_and_valid_for_the_login_policy():
    from app.core.security import check_password_policy, validate_username

    a, b = st.Smoke("http://x", False, 1, False), st.Smoke("http://x", False, 1, False)
    assert a.username != b.username and a.password != b.password
    assert validate_username(a.username) == a.username           # smoke-xxxxxxxx проходит правила логина
    check_password_policy(a.password)                            # и правила пароля — иначе создание админа упало бы


def test_main_parses_options_and_passes_them_through():
    captured = {}

    class Fake:
        def __init__(self, base_url, with_worker, timeout, keep):
            captured.update(base_url=base_url, with_worker=with_worker, timeout=timeout, keep=keep)

        def run(self):
            return 7

    original = st.Smoke
    st.Smoke = Fake
    try:
        assert st.main(["--with-worker", "--timeout", "5", "--keep", "--base-url", "http://h:1"]) == 7
    finally:
        st.Smoke = original
    assert captured == {"base_url": "http://h:1", "with_worker": True, "timeout": 5.0, "keep": True}


def test_with_worker_is_honestly_refused_instead_of_faking_a_result():
    lines = []
    sm = st.Smoke("http://x", True, 1, False, out=lines.append)
    assert sm.run() == 2
    assert any("не поддерживается" in line for line in lines)


def test_smoke_no_longer_knows_the_audit_flows():
    import inspect
    source = inspect.getsource(st)
    for gone in ("check_store_flow", "check_planogram_endpoint", "check_camera_flow", "create_agent_user", "CameraORM", "ShelfSnapshot", "/api/v1/stores"):
        assert gone not in source.replace("GONE_ROUTES", ""), gone
    assert "/api/v1/snapshots/x" in st.GONE_ROUTES and "/api/v1/cameras" in st.GONE_ROUTES


class FakeServer:
    """Двойник API: отвечает по таблице (метод, путь) -> (код, тело)."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def _answer(self, method, path):
        import json as _json
        self.calls.append((method, path))
        status, body = self.table.get((method, path), (404, {}))
        return SimpleNamespace(status_code=status, text=_json.dumps(body, ensure_ascii=False), json=lambda: body)

    def get(self, path, **kwargs):
        return self._answer("GET", path)

    def post(self, path, **kwargs):
        return self._answer("POST", path)


GOOD = {
    ("GET", "/api/v1/catalog/products"): (200, []),
    ("GET", "/api/v1/admin/products"): (200, {"total": 0, "items": []}),
    ("GET", "/api/v1/admin/system"): (200, {"database": "ok", "queue": "ok", "worker": {}, "storage": {}, "settings": []}),
}


def test_catalog_and_system_checks_pass_against_a_correct_server():
    sm = st.Smoke("http://x", False, 1, False, out=lambda s: None)
    server = FakeServer(GOOD)
    sm.check_catalog(server)
    sm.check_system(server)
    sm.check_audit_is_gone(server)                                  # на «пустых» маршрутах двойник отвечает 404


def test_checks_detect_a_leftover_agent_section_and_a_surviving_audit_route():
    sm = st.Smoke("http://x", False, 1, False, out=lambda s: None)
    bad_system = {**GOOD, ("GET", "/api/v1/admin/system"): (200, {"database": "ok", "queue": "ok", "worker": {}, "storage": {}, "settings": [], "agent": {}})}
    with pytest.raises(st.StepFailed):
        sm.check_system(FakeServer(bad_system))
    with pytest.raises(st.StepFailed):
        sm.check_audit_is_gone(FakeServer({("GET", "/api/v1/cameras"): (200, {})}))
    broken_db = {**GOOD, ("GET", "/api/v1/admin/system"): (200, {"database": "fail: OperationalError", "queue": "ok", "worker": {}, "storage": {}, "settings": []})}
    with pytest.raises(st.StepFailed):
        sm.check_system(FakeServer(broken_db))


def test_admin_product_list_with_audit_fields_is_rejected():
    sm = st.Smoke("http://x", False, 1, False, out=lambda s: None)
    old_shape = {**GOOD, ("GET", "/api/v1/admin/products"): (200, {"total": 1, "items": [{"sku": "A", "planogram_slots": 2}]})}
    with pytest.raises(st.StepFailed):
        sm.check_catalog(FakeServer(old_shape))
