"""
Жизненный цикл временных кадров «Узнать товар» (временная мера D-10а, 2026-11-07).

Сейчас API передаёт кадр воркеру через файл в `<media>/identify-tmp/` (см. routes_catalog.identify_product). Это НЕ выполнение
D-10а («кадр не записывается на диск»): окончательная схема — кадр только в памяти, этап 6 (ARCHITECTURE_DECISIONS_2026-11-07.md,
§5.4). Здесь — только ограничение срока жизни файла:

  * срок — не больше FRAME_TTL_SECONDS (ТК-2: две минуты);
  * уборкой владеет API — тот, кто пишет кадры: при запуске и затем каждые SWEEP_INTERVAL_SECONDS, независимо от воркера
    (раньше уборка шла только при следующем запросе и только для файлов старше 15 минут; при остановленном воркере кадры лежали);
  * воркер тоже убирает при запуске (кадры, оставшиеся от прошлого запуска);
  * нового правила «удалять сразу после ответа /identify» нет: оно помешало бы будущему выбору товара на кадре (ТК-3).

Время берётся из mtime: эти файлы создаёт только API, в резервные копии они не входят и из копий не восстанавливаются,
поэтому подмены времени при копировании/восстановлении здесь нет (в отличие от фото каталога, §6.4.6).
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.services.catalog_upload import identify_root, sweep_old_files

FRAME_TTL_SECONDS = 120          # ТК-2
SWEEP_INTERVAL_SECONDS = 10      # документ 04, §4.2: очистка не реже раза в 10 секунд

logger = logging.getLogger(__name__)


def sweep_frames(media_root: Path, *, now: float | None = None, ttl_seconds: float = FRAME_TTL_SECONDS) -> int:
    """Удаляет временные кадры старше ttl_seconds. Возвращает число удалённых файлов; ошибки отдельных файлов не прерывают уборку."""
    return sweep_old_files(identify_root(media_root), max_age_seconds=ttl_seconds, now=time.time() if now is None else now)


class PeriodicSweeper:
    """Фоновый поток: вызывает sweep() сразу при start() и затем каждые interval секунд, пока не вызван stop().

    Поток-демон: не мешает завершению процесса. Исключение одной уборки пишется в журнал и не останавливает следующие.
    """

    def __init__(self, sweep: Callable[[], int], *, interval: float = SWEEP_INTERVAL_SECONDS, name: str = "frame-sweeper"):
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self._sweep = sweep
        self._interval = interval
        self._name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.runs = 0
        self.failures = 0

    def _run_once(self) -> None:
        try:
            removed = self._sweep()
            if removed:
                logger.info("Удалено устаревших временных кадров: %d", removed)
        except Exception:  # noqa: BLE001 — уборка не должна падать насовсем из-за одного сбоя
            self.failures += 1
            logger.exception("Сбой уборки временных кадров — повторится через %s с", self._interval)
        finally:
            self.runs += 1

    def _loop(self) -> None:
        self._run_once()
        while not self._stop.wait(self._interval):
            self._run_once()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
