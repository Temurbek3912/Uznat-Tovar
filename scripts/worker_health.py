"""
Healthcheck воркера: готов ли он к РАСПОЗНАВАНИЮ, а не только жив ли процесс.

`celery inspect ping` отвечает «pong», даже если при старте не поднялись обязательные компоненты (раньше так и было: ошибка
FaceBlurrer, Celery пишет «Signal handler raised» и «ready», а все задачи потом падают). Воркер после инициализации пишет файл
статуса (app/pipeline/ml_runtime.py, ML_STATUS_FILE, по умолчанию /tmp/shelf_audit_ml_status.json); этот скрипт его читает.

    python -m scripts.worker_health       # код 0 — готов; 1 — нет файла статуса или не готов обязательный компонент (причины — в выводе)
"""
from __future__ import annotations

import sys

from app.pipeline.ml_runtime import read_status, status_path


def main(out=print) -> int:
    status = read_status()
    if status is None:
        out(f"нет файла статуса {status_path()}: воркер ещё стартует (первый запуск скачивает DINOv2) либо упал при старте — см. docker compose logs worker")
        return 1
    if not status.get("ready"):
        out("воркер НЕ готов к распознаванию:")
        for problem in status.get("problems") or ["причина не записана"]:
            out(f"  - {problem}")
        return 1
    note = " (есть заменённые компоненты — результаты менее точны)" if status.get("degraded") else ""
    out("воркер готов" + note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
