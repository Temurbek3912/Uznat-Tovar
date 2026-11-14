"""
Кадр «Узнать товар» — только в оперативной памяти (решение владельца D-10а; документ 04, §4.2; D-28 в документе 14).

Правила D-10а, которые здесь обеспечены:
  * кадр не пишется на диск: ни файлом, ни в журнал Redis — Redis для кадров должен работать без сохранения (без RDB и AOF);
    RedisFrameStore проверяет это сам и отказывается работать, если сохранение включено или проверить нельзя;
  * кадр живёт не дольше FRAME_TTL_SECONDS (2 минуты) — срок ставит сам Redis (или таймер в памяти процесса);
  * кадр удаляется сразу, как только его забрали на распознавание (take — «прочитать и удалить» одной командой GETDEL),
    и при отмене (discard);
  * кадр не входит в резервные копии и не восстанавливается после перезапуска (памяти больше нет).

Зачем отдельный Redis, а не Redis очереди. Redis очереди работает с --appendonly yes (очередь переживает перезапуск), то есть пишет
всё на диск. Поэтому в очередь уходит только номер кадра («frame:<id>»), а сам кадр — в отдельный Redis без сохранения
(служба `frames` в docker-compose.yml). Без него (один процесс, тесты) — MemoryFrameStore в памяти процесса.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

FRAME_TTL_SECONDS = 120              # D-10а: не дольше 2 минут
FRAME_REF_PREFIX = "frame:"
_KEY_PREFIX = "uznat:frame:"
MAX_FRAMES_IN_MEMORY = 64            # в памяти процесса: при переполнении вытесняется самый старый кадр
MAX_BYTES_IN_MEMORY = 128 * 1024 * 1024


class FrameStoreError(Exception):
    """Хранилище кадров недоступно или небезопасно (сохраняет на диск). Текст безопасен для журнала, кадра в нём нет."""


def new_frame_id() -> str:
    return secrets.token_urlsafe(18)


def is_frame_ref(ref: object) -> bool:
    return isinstance(ref, str) and ref.startswith(FRAME_REF_PREFIX) and 8 <= len(ref) - len(FRAME_REF_PREFIX) <= 64


def frame_ref(frame_id: str) -> str:
    return FRAME_REF_PREFIX + frame_id


def frame_id_of(ref: str) -> str:
    if not is_frame_ref(ref):
        raise ValueError("не ссылка на кадр")
    return ref[len(FRAME_REF_PREFIX):]


class MemoryFrameStore:
    """Кадры в памяти ЭТОГО процесса: для запуска одним процессом (Desktop, разработка) и тестов."""

    def __init__(self, *, ttl_seconds: float = FRAME_TTL_SECONDS, max_frames: int = MAX_FRAMES_IN_MEMORY,
                 max_bytes: int = MAX_BYTES_IN_MEMORY, clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = ttl_seconds
        self.max_frames = max_frames
        self.max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._frames: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._bytes = 0

    def _drop(self, frame_id: str) -> None:
        item = self._frames.pop(frame_id, None)
        if item is not None:
            self._bytes -= len(item[1])

    def _expire(self, now: float) -> None:
        for frame_id in [k for k, (deadline, _) in self._frames.items() if deadline <= now]:
            self._drop(frame_id)

    def put(self, data: bytes) -> str:
        frame_id = new_frame_id()
        with self._lock:
            now = self._clock()
            self._expire(now)
            self._frames[frame_id] = (now + self.ttl_seconds, bytes(data))
            self._bytes += len(data)
            while self._frames and (len(self._frames) > self.max_frames or self._bytes > self.max_bytes):
                self._drop(next(iter(self._frames)))
        return frame_id

    def take(self, frame_id: str) -> bytes | None:
        with self._lock:
            self._expire(self._clock())
            item = self._frames.get(frame_id)
            self._drop(frame_id)
        return item[1] if item is not None else None

    def discard(self, frame_id: str) -> None:
        with self._lock:
            self._drop(frame_id)

    def count(self) -> int:
        with self._lock:
            self._expire(self._clock())
            return len(self._frames)

    def check_safe(self) -> None:
        return None


class RedisFrameStore:
    """Кадры в отдельном Redis без сохранения на диск. Доступен и API, и воркеру (разные процессы и контейнеры)."""

    def __init__(self, client, *, ttl_seconds: float = FRAME_TTL_SECONDS):
        self.client = client
        self.ttl_seconds = int(ttl_seconds)
        self._verified = False
        self._lock = threading.Lock()

    def check_safe(self) -> None:
        """Отказ, если этот Redis сохраняет данные на диск (RDB «save» или AOF) или это нельзя проверить. Проверяется один раз."""
        if self._verified:
            return
        with self._lock:
            if self._verified:
                return
            try:
                save = self.client.config_get("save").get("save", None)
                aof = self.client.config_get("appendonly").get("appendonly", None)
            except Exception as exc:  # noqa: BLE001
                raise FrameStoreError(f"хранилище кадров недоступно или не даёт проверить настройки ({type(exc).__name__})") from exc
            if isinstance(save, bytes):
                save = save.decode()
            if isinstance(aof, bytes):
                aof = aof.decode()
            if save is None or aof is None:
                raise FrameStoreError("не удалось проверить, что хранилище кадров не пишет на диск")
            if str(save).strip() != "" or str(aof).strip().lower() != "no":
                raise FrameStoreError("хранилище кадров сохраняет данные на диск (save/appendonly) — по D-10а это запрещено")
            self._verified = True

    def put(self, data: bytes) -> str:
        self.check_safe()
        frame_id = new_frame_id()
        try:
            self.client.set(_KEY_PREFIX + frame_id, bytes(data), ex=self.ttl_seconds, nx=True)
        except Exception as exc:  # noqa: BLE001
            raise FrameStoreError(f"кадр не сохранён в памяти ({type(exc).__name__})") from exc
        return frame_id

    def take(self, frame_id: str) -> bytes | None:
        try:
            data = self.client.getdel(_KEY_PREFIX + frame_id)
        except Exception as exc:  # noqa: BLE001
            raise FrameStoreError(f"кадр не прочитан ({type(exc).__name__})") from exc
        return bytes(data) if data is not None else None

    def discard(self, frame_id: str) -> None:
        try:
            self.client.delete(_KEY_PREFIX + frame_id)
        except Exception:  # noqa: BLE001 — срок жизни всё равно удалит кадр через 2 минуты
            pass


_store = None
_store_lock = threading.Lock()


def build_frame_store(settings, *, redis_factory=None):
    url = (getattr(settings, "frames_redis_url", "") or "").strip()
    if not url:
        return MemoryFrameStore()
    if redis_factory is None:
        import redis  # noqa: PLC0415
        redis_factory = lambda u: redis.Redis.from_url(u, socket_connect_timeout=2, socket_timeout=5)  # noqa: E731
    return RedisFrameStore(redis_factory(url))


def get_frame_store():
    """Одно хранилище на процесс (по настройке FRAMES_REDIS_URL)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                from app.core.config import get_settings  # noqa: PLC0415
                _store = build_frame_store(get_settings())
    return _store


def set_frame_store(store) -> None:
    """Для тестов и запуска одним процессом."""
    global _store
    _store = store
