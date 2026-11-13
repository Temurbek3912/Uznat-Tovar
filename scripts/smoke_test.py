"""
Самопроверка работающей системы одной командой — после `docker compose up -d`:

    docker compose exec api python -m scripts.smoke_test                  # API, БД, очередь, вход, каталог, админ-раздел (~5 с)

Проходит тот же путь, что и пользователь: health -> вход -> токен -> защищённые эндпоинты -> каталог товаров -> раздел
«Система» администратора -> убедиться, что маршрутов прежнего аудита полок больше нет. Реальный пароль админа не нужен:
скрипт создаёт временного администратора `smoke-xxxxxxxx` со случайным паролем и в конце УДАЛЯЕТ его вместе с токенами —
ваши данные не затрагиваются. `--keep` — не удалять (для разбора неудачи).

`--with-worker` (круг «фото -> очередь -> воркер -> распознавание») после уборки аудита пока НЕ поддерживается: прежний круг
проверял загрузку снимка полки и отчёт, которых больше нет. Новый круг (добавить товар с фото -> показать его на странице
«Узнать товар») будет добавлен вместе с этапом распознавания «детектор -> вырезка -> признаки -> поиск». Ключ не подделывает
результат: при запросе скрипт сообщает об этом и завершается с кодом 2.

Код выхода: 0 — всё прошло; 1 — какой-то шаг не прошёл (причина и подсказка выводятся); 2 — запрошена неподдерживаемая проверка.
"""
from __future__ import annotations

import argparse
import io
import os
import secrets
import sys
import time
from typing import Callable

DEFAULT_BASE_URL = "http://127.0.0.1:8000"     # внутри контейнера api uvicorn слушает 8000, какой бы порт ни был снаружи
GONE_ROUTES = ("/api/v1/cameras", "/api/v1/planograms/x", "/api/v1/review/queue", "/api/v1/agent/cameras", "/api/v1/snapshots/x")


class StepFailed(Exception):
    """Шаг самопроверки не прошёл; текст — для человека, с подсказкой, что смотреть."""


# ------------------------------------------------------------------ чистые помощники (тестируются офлайн)


def make_test_jpeg(width: int = 800, height: int = 600) -> bytes:
    """Синтетическая картинка с цветными коробками на светлом фоне. Пригодится для проверки круга с воркером (когда он появится)."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), (235, 235, 230))
    draw = ImageDraw.Draw(image)
    colors = [(200, 50, 50), (50, 120, 200), (60, 160, 80), (220, 170, 40), (130, 70, 160)]
    for row in range(3):
        top = 40 + row * 190
        draw.rectangle([20, top + 150, width - 20, top + 156], fill=(90, 90, 90))
        for col in range(6):
            left = 40 + col * 125
            draw.rectangle([left, top, left + 100, top + 148], fill=colors[(row + col) % len(colors)], outline=(30, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def poll_until(fetch: Callable[[], str], done: set[str], timeout: float, interval: float = 3.0,
               sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
               on_tick: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """Опрашивает fetch() до статуса из `done` или таймаута. -> (достигнут ли, последний статус)."""
    deadline = clock() + timeout
    status = "unknown"
    while True:
        status = fetch()
        if on_tick:
            on_tick(status)
        if status in done:
            return True, status
        if clock() >= deadline:
            return False, status
        sleep(interval)


def expect(response, status: int, what: str, hint: str = "") -> object:
    """Проверяет код ответа; при несовпадении — StepFailed с куском тела ответа."""
    if response.status_code != status:
        body = (response.text or "")[:300].replace("\n", " ")
        raise StepFailed(f"{what}: ожидался HTTP {status}, получен {response.status_code}. Ответ: {body}" + (f"\n    Подсказка: {hint}" if hint else ""))
    return response


# ------------------------------------------------------------------ сценарий


class Smoke:
    def __init__(self, base_url: str, with_worker: bool, timeout: float, keep: bool, out: Callable[[str], None] = print):
        self.base_url, self.with_worker, self.timeout, self.keep, self.out = base_url.rstrip("/"), with_worker, timeout, keep, out
        self.user_id: str | None = None
        self.username = f"smoke-{secrets.token_hex(4)}"
        self.password = secrets.token_urlsafe(18)
        self.headers: dict[str, str] = {}

    def step(self, title: str) -> None:
        self.out(f"  • {title}")

    # -- шаги ------------------------------------------------------------------------------------------------------

    def check_health(self, client) -> None:
        self.step("GET /health — процесс жив")
        expect(client.get("/health"), 200, "/health")
        self.step("GET /health/ready — БД, таблицы, очередь")
        resp = expect(client.get("/health/ready"), 200, "/health/ready",
                      "docker compose logs bootstrap  (схема создаётся им); docker compose ps  (db и redis должны быть healthy)")
        self.out(f"      {resp.json()['checks']}")

    def create_temp_admin(self) -> None:
        self.step(f"создаю временного администратора {self.username} (будет удалён)")
        from app.core.security import Role
        from app.db.session import SessionLocal
        from app.services.auth_service import AuthService

        with SessionLocal() as db:
            self.user_id = AuthService(db).create_user(self.username, self.password, Role.ADMIN).id

    def check_auth(self, client) -> None:
        self.step("защищённый эндпоинт без токена -> 401")
        expect(client.get("/api/v1/catalog/products"), 401, "GET /api/v1/catalog/products без токена", "защита API отключена?!")
        self.step("неверный пароль -> 401")
        expect(client.post("/api/v1/auth/login", data={"username": self.username, "password": "wrong-password-xyz"}), 401, "вход с неверным паролем")
        self.step("вход -> токен")
        resp = expect(client.post("/api/v1/auth/login", data={"username": self.username, "password": self.password}), 200, "вход")
        self.headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        self.step("GET /api/v1/auth/me — роль и права")
        me = expect(client.get("/api/v1/auth/me", headers=self.headers), 200, "/auth/me").json()
        if me["role"] != "admin" or "catalog:write" not in me["permissions"]:
            raise StepFailed(f"/auth/me: у администратора нет права catalog:write: {me}")

    def check_catalog(self, client) -> None:
        self.step("GET /api/v1/catalog/products — список товаров открывается")
        listing = expect(client.get("/api/v1/catalog/products", headers=self.headers, params={"limit": 5}), 200, "список товаров").json()
        if not isinstance(listing, list):
            raise StepFailed(f"список товаров должен быть массивом, получено: {type(listing).__name__}")
        self.step("GET /api/v1/admin/products — таблица «Товары» в админ-панели")
        admin = expect(client.get("/api/v1/admin/products", headers=self.headers), 200, "админ-список товаров").json()
        if not {"total", "items"} <= set(admin) or any("planogram_slots" in item for item in admin["items"]):
            raise StepFailed(f"ответ админ-списка товаров неожиданной формы: {str(admin)[:200]}")

    def check_system(self, client) -> None:
        self.step("GET /api/v1/admin/system — состояние БД, очереди, воркера, диска")
        system = expect(client.get("/api/v1/admin/system", headers=self.headers), 200, "состояние системы").json()
        if system.get("database") != "ok":
            raise StepFailed(f"база данных в разделе «Система» не «ok»: {system.get('database')}")
        for key in ("queue", "worker", "storage", "settings"):
            if key not in system:
                raise StepFailed(f"в ответе «Система» нет раздела {key}")
        if "agent" in system:
            raise StepFailed("в ответе «Система» остался раздел agent (агент камер убран вместе с аудитом полок)")

    def check_audit_is_gone(self, client) -> None:
        self.step("маршрутов прежнего аудита полок больше нет (404)")
        for path in GONE_ROUTES:
            code = client.get(path, headers=self.headers).status_code
            if code != 404:
                raise StepFailed(f"{path}: ожидался 404 (маршрут удалён), получен {code}")

    def cleanup(self) -> None:
        if self.keep:
            self.out(f"  (--keep) оставлено: пользователь {self.username}")
            return
        from app.db.models import ApiToken, User, UserStoreAccess
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            if self.user_id:
                db.query(ApiToken).filter(ApiToken.user_id == self.user_id).delete()
                db.query(UserStoreAccess).filter(UserStoreAccess.user_id == self.user_id).delete()
                user = db.get(User, self.user_id)
                if user is not None:
                    db.delete(user)
            db.commit()
        self.out("  тестовые данные удалены")

    def run(self) -> int:
        if self.with_worker:
            self.out("--with-worker пока не поддерживается: прежний круг проверял снимок полки и отчёт, которых больше нет.")
            self.out("Круг «добавить товар с фото -> узнать его» появится вместе с этапом распознавания. Запустите без этого ключа.")
            return 2
        import httpx

        self.out(f"== Самопроверка «Узнать товар» ({self.base_url}) ==")
        failure: str | None = None
        try:
            with httpx.Client(base_url=self.base_url, timeout=30.0) as client:
                self.check_health(client)
                self.create_temp_admin()
                self.check_auth(client)
                self.check_catalog(client)
                self.check_system(client)
                self.check_audit_is_gone(client)
        except StepFailed as exc:
            failure = str(exc)
        except Exception as exc:  # noqa: BLE001 — любая неожиданная ошибка тоже должна дать понятный итог и уборку
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                self.cleanup()
            except Exception as exc:  # noqa: BLE001
                self.out(f"  ВНИМАНИЕ: уборка не удалась ({type(exc).__name__}: {exc}); удалите вручную пользователя {self.username}")

        if failure:
            self.out(f"\nНЕ ПРОЙДЕНО: {failure}")
            return 1
        self.out("\nВсё прошло. Круг с воркером (распознавание) этим скриптом пока не проверяется.")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Самопроверка работающей системы")
    parser.add_argument("--base-url", default=os.environ.get("SMOKE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--with-worker", action="store_true", help="пока не поддерживается (см. описание вверху файла)")
    parser.add_argument("--timeout", type=float, default=420.0, help="зарезервировано для круга с воркером")
    parser.add_argument("--keep", action="store_true", help="не удалять созданные тестовые данные")
    args = parser.parse_args(argv)
    return Smoke(args.base_url, args.with_worker, args.timeout, args.keep).run()


if __name__ == "__main__":
    sys.exit(main())
