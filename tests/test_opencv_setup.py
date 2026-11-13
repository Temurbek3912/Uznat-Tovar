"""OpenCV: требования, Dockerfile и проверка фактических возможностей (app/core/opencv_check.py, scripts/check_opencv.py).

Регресс, который закрывается: в образе оказались OpenCV 5.0.0.93 в двух сборках, cv2.CascadeClassifier отсутствовал, а
`pip check` молчал. Тесты закрепляют (1) пин <5 и обязательный «замок», (2) шаги Dockerfile, (3) логику проверки на ПОДДЕЛЬНЫХ
модулях cv2 (воспроизведение сломанного окружения) и (4) на настоящем cv2 этой среды."""
from __future__ import annotations

import pytest

import re
import types
from pathlib import Path

from app.core import opencv_check as oc
from scripts import check_opencv as cli

ROOT = Path(__file__).resolve().parent.parent


def _fake_cv2(version="4.10.0", cascade=True, data_dir=None, empty=False, boom=False):
    mod = types.SimpleNamespace(__version__=version, __file__="/fake/cv2/__init__.py")
    if cascade:
        class Cascade:
            def __init__(self, path):
                if boom:
                    raise RuntimeError("bad xml")
                self.path = path

            def empty(self):
                return empty
        mod.CascadeClassifier = Cascade
    if data_dir is not None:
        mod.data = types.SimpleNamespace(haarcascades=str(data_dir) + "/")
    return mod


# ------------------------------------------------------------------ файлы проекта

def test_requirements_pin_opencv_below_5_and_ultralytics_for_yolo11():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if l.startswith("opencv-python-headless"))
    assert re.search(r">=4\.9\s*,\s*<5", line), line
    ultra = next(l for l in text.splitlines() if l.startswith("ultralytics"))
    assert re.search(r">=8\.3", ultra), ultra                                  # веса YOLO11 (sku110k_yolo11n.pt) — с 8.3.0


def test_mandatory_opencv_constraints_file_blocks_every_5x_build():
    text = (ROOT / "constraints-opencv.txt").read_text(encoding="utf-8")
    pins = {l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")}
    assert pins == {"opencv-python<5", "opencv-python-headless<5", "opencv-contrib-python<5", "opencv-contrib-python-headless<5"}


def test_dockerfile_installs_with_the_lock_leaves_one_opencv_and_verifies_it_before_the_import_audit():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    install = text.index("pip install -r requirements.txt -c constraints-opencv.txt")
    uninstall = text.index("pip uninstall -y opencv-python opencv-contrib-python opencv-python-headless")
    reinstall = text.index('pip install --no-deps -c constraints-opencv.txt "opencv-python-headless>=4.9,<5"')
    check = text.index("python -m scripts.check_opencv")
    imports = text.index("python -m scripts.check_imports --runtime")
    assert install < uninstall < reinstall < check < imports
    assert text.count("COPY . .") == 1 and text.index("COPY . .") < check               # скрипт проверки копируется до запуска
    assert "constraints-opencv.txt" in text[text.index("COPY requirements.txt"):install]  # замок копируется до pip install


# ------------------------------------------------------------------ проверка сборок pip

def test_several_opencv_builds_side_by_side_are_reported_with_the_fix():
    problems = oc.distribution_problems({"opencv-python": "5.0.0.93", "opencv-python-headless": "5.0.0.93"})
    assert len(problems) == 1 and "opencv-python==5.0.0.93" in problems[0] and "pip uninstall -y opencv-python" in problems[0]
    assert "opencv-python-headless>=4.9,<5" in problems[0]


def test_single_headless_build_is_fine_and_no_build_is_a_problem():
    assert oc.distribution_problems({"opencv-python-headless": "4.10.0.84"}) == []
    assert "не найдено ни одной сборки" in oc.distribution_problems({})[0]


# ------------------------------------------------------------------ проверка возможностей (подделки cv2)

def test_opencv_5_without_cascadeclassifier_is_diagnosed_like_in_the_container():
    problems = oc.capability_problems(_fake_cv2(version="5.0.0", cascade=False))
    assert any("5.x не поддерживается" in p and "xobjdetect" in p for p in problems) and any("нет атрибута CascadeClassifier" in p for p in problems)


def test_missing_data_directory_missing_file_empty_cascade_and_constructor_error_are_each_reported(tmp_path):
    assert any("cv2.data.haarcascades" in p for p in oc.capability_problems(_fake_cv2()))
    assert any("файл каскада лиц не найден" in p for p in oc.capability_problems(_fake_cv2(data_dir=tmp_path)))
    (tmp_path / oc.FACE_CASCADE).write_text("<xml/>")
    assert any("empty() == True" in p for p in oc.capability_problems(_fake_cv2(data_dir=tmp_path, empty=True)))
    assert any("RuntimeError: bad xml" in p for p in oc.capability_problems(_fake_cv2(data_dir=tmp_path, boom=True)))
    assert oc.capability_problems(_fake_cv2(data_dir=tmp_path)) == []


def test_the_cli_returns_one_on_any_problem_and_zero_when_everything_is_fine(tmp_path):
    (tmp_path / oc.FACE_CASCADE).write_text("<xml/>")
    lines = []
    assert cli.run(_fake_cv2(data_dir=tmp_path), {"opencv-python-headless": "4.10.0"}, out=lines.append) == 0
    assert "OpenCV в порядке" in "\n".join(lines)
    lines.clear()
    assert cli.run(_fake_cv2(version="5.0.0", cascade=False), {"opencv-python": "5.0.0", "opencv-python-headless": "5.0.0"}, out=lines.append) == 1
    text = "\n".join(lines)
    assert "ПРОБЛЕМЫ" in text and "CascadeClassifier: НЕТ" in text


@pytest.mark.environment
def test_the_real_cv2_of_this_environment_loads_the_face_cascade():
    try:
        import cv2
    except ImportError:
        return                                                                  # окружение без OpenCV — проверять нечего
    if int(cv2.__version__.split(".")[0]) >= 5:
        raise AssertionError(f"в этом окружении cv2 {cv2.__version__}: нужен 4.x (см. requirements.txt и constraints-opencv.txt)")
    assert oc.capability_problems(cv2) == []
