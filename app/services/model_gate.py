"""
Очерёдность общей модели «зрение + текст»: покупатели и «Узнать товар» — первыми, ИИ-помощник — потом (документ 16, §10.1).

Помощник и распознавание работают на одной модели (qwen3-vl-4b, свой движок или LM Studio), а движок отвечает по одному запросу
за раз. Поэтому помощник перед каждым обращением к модели ждёт, пока идущие распознавания закончатся, и сам обращается к модели
не больше одного раза одновременно (`assistant_slot`). Распознавание помощника не ждёт никогда.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

_lock = threading.Lock()
_identify_running = 0
_assistant = threading.Semaphore(1)


@contextmanager
def identify_turn():
    """Обёртка вокруг одного «Узнать товар» (сотрудник или покупатель)."""
    global _identify_running
    with _lock:
        _identify_running += 1
    try:
        yield
    finally:
        with _lock:
            _identify_running -= 1


def identify_busy() -> bool:
    with _lock:
        return _identify_running > 0


def wait_for_identify(max_seconds: float, sleep=time.sleep, clock=time.monotonic) -> bool:
    """Ждать, пока распознавания закончатся. True — модель свободна; False — так и не освободилась за max_seconds."""
    end = clock() + max_seconds
    while identify_busy():
        if clock() >= end:
            return False
        sleep(0.1)
    return True


@contextmanager
def assistant_slot(timeout: float):
    """Один запрос помощника к модели за раз. Не дождались — LookupError («помощник занят»)."""
    if not _assistant.acquire(timeout=timeout):
        raise LookupError("assistant busy")
    try:
        yield
    finally:
        _assistant.release()
