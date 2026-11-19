"""Восстановление индекса по расписанию (документ ENGINEERING_STATUS §3, запись «2026-11-19. `recover_index` по расписанию»).

Раньше задача восстановления индекса (app/worker.py, recover_index_task) ставилась только при запуске воркера: товар, потерявший
векторы при работающем воркере, ждал перезапуска. Теперь тот же процесс, что запустил воркер (или API в облегчённом режиме),
держит фоновый поток, который раз в `interval` секунд ставит задачу снова.

Без дублей: каждая поставленная задача живёт в очереди не дольше одного промежутка (`expires`) — если воркер занят и не взял её
до следующего тика, старая отбрасывается Celery, и в очереди остаётся одна. В облегчённом режиме задача выполняется сразу в потоке
таймера (task_always_eager), поэтому их и так не бывает две. Пустая очередь и «нечего восстанавливать» безвредны: задача только
сверяет базу, фото и индекс. Ошибка постановки (Redis недоступен) — в журнал, следующий тик попробует снова.

Без Celery beat: отдельный процесс beat — ещё одна служба, которую надо запускать и в Docker, и в start_demo, и в облегчённом
режиме (где его нет вовсе); фоновый поток работает одинаково везде.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 30


class RecoveryTicker:
    """Фоновый поток: раз в `interval` секунд вызывает `enqueue(expires_seconds)`. Первый вызов — через один промежуток
    (сразу после запуска задачу уже поставил обработчик запуска воркера). `start` повторно ничего не делает."""

    def __init__(self, interval: float, enqueue: Callable[[float], object], *, name: str = "recover-index-ticker") -> None:
        self.interval = float(interval)
        self._enqueue = enqueue
        self._name = name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Запустить поток. False — не запущен: расписание выключено (промежуток 0) или поток уже работает."""
        if self.interval <= 0:
            return False
        with self._lock:
            if self.running:
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def tick(self) -> bool:
        """Один тик: поставить задачу. Исключения не выпускает (иначе поток умер бы и расписание тихо прекратилось)."""
        try:
            self._enqueue(self.interval)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Восстановление индекса по расписанию не поставлено (%s) — повторится через %.0f мин",
                           exc, self.interval / 60)
            return False

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.tick()


def interval_seconds(minutes: object) -> float:
    """Промежуток из настройки (минуты) в секундах; неверное или отрицательное значение — расписание выключено (0)."""
    try:
        value = float(minutes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return value * 60 if value > 0 else 0.0
