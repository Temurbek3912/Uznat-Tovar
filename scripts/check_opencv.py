"""
Проверка OpenCV в окружении по ФАКТИЧЕСКИМ возможностям, а не по метаданным pip.

    python -m scripts.check_opencv            # печатает отчёт; код 0 — всё в порядке, 1 — есть проблемы

Dockerfile запускает этот скрипт после установки зависимостей: образ с неисправным OpenCV (несколько сборок рядом, версия 5.x,
нет CascadeClassifier, каскад лиц не загружается) не соберётся. Логика — в app/core/opencv_check.py.
"""
from __future__ import annotations

import sys

from app.core.opencv_check import capability_problems, distribution_problems, installed_distributions, report_lines


def run(cv2_module=None, dists: dict[str, str] | None = None, out=print) -> int:
    if cv2_module is None:
        try:
            import cv2 as cv2_module  # noqa: PLC0415
        except ImportError as exc:
            out(f"ОШИБКА: cv2 не импортируется: {exc}")
            return 1
    dists = installed_distributions() if dists is None else dists
    problems = distribution_problems(dists) + capability_problems(cv2_module)
    for line in report_lines(cv2_module, dists):
        out(line)
    if problems:
        out("\nПРОБЛЕМЫ:")
        for item in problems:
            out(f"  - {item}")
        return 1
    out("OpenCV в порядке: одна сборка, версия <5, каскад лиц загружается.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
