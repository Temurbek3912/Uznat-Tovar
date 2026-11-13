"""FaceBlurrer (app/pipeline/privacy.py): загрузка каскада, понятные ошибки, размытие найденных лиц. Размытие лиц обязательно."""
from __future__ import annotations

import types

import numpy as np
import pytest

from app.pipeline.privacy import FACE_CASCADE_FILE, FaceBlurrer, FaceBlurUnavailable, default_cascade_path


def _noisy(h=240, w=320, seed=0):
    return np.random.default_rng(seed).integers(0, 255, (h, w, 3), dtype=np.uint8)


def _cv2_with_fake_cascade(tmp_path, boxes=()):
    """Настоящие функции изображений cv2 (cvtColor, GaussianBlur) + поддельный каскад с заданными рамками лиц.

    Логика размытия (запас вокруг рамки, обрезка по краю, неизменность входа, нечётное ядро) — код приложения, а не данные
    OpenCV; проверяется без файла каскада. Что каскад из окружения действительно загружается — отдельные тесты окружения
    (test_real_blurrer_… здесь и test_the_real_cv2_… в test_opencv_setup.py)."""
    import cv2
    cascade = tmp_path / FACE_CASCADE_FILE
    cascade.write_text("<opencv_storage/>", encoding="utf-8")

    class FakeCascade:
        def __init__(self, path):
            self.path = path

        def empty(self):
            return False

        def detectMultiScale(self, *args, **kwargs):
            return np.array(list(boxes), dtype=int).reshape(-1, 4)

    module = types.SimpleNamespace(__version__="fake", CascadeClassifier=FakeCascade, cvtColor=cv2.cvtColor,
                                   COLOR_RGB2GRAY=cv2.COLOR_RGB2GRAY, GaussianBlur=cv2.GaussianBlur)
    return module, str(cascade)


@pytest.mark.environment
def test_real_blurrer_creates_with_a_loaded_cascade_and_runs_on_a_frame_without_faces():
    import cv2
    blurrer = FaceBlurrer()
    assert blurrer.cascade_path.endswith(FACE_CASCADE_FILE) and not blurrer._detector.empty()
    image = np.full((200, 300, 3), 120, np.uint8)
    out, faces = blurrer.blur_faces(image)
    assert faces == 0 and out.shape == image.shape and (out == image).all() and out is not image
    gray, faces = blurrer.blur_faces(image[:, :, 0])                           # серое изображение тоже принимается
    assert faces == 0 and gray.shape == (200, 300)
    assert cv2.__version__.startswith("4.")


def test_importing_the_module_does_not_touch_cv2_data():
    # раньше путь к каскаду вычислялся при ИМПОРТЕ (cv2.data.haarcascades) и мог уронить импорт на «сломанном» cv2
    import app.pipeline.privacy as privacy
    assert not hasattr(privacy, "_FACE_CASCADE_PATH")


def test_a_cv2_without_cascadeclassifier_gives_a_clear_actionable_error_not_an_attribute_error():
    broken = types.SimpleNamespace(__version__="5.0.0")
    with pytest.raises(FaceBlurUnavailable) as err:
        FaceBlurrer(cv2_module=broken)
    text = str(err.value)
    assert "5.0.0" in text and "CascadeClassifier" in text and "scripts.check_opencv" in text and "<5" in text
    assert isinstance(err.value, RuntimeError)                                  # прежний код ловил RuntimeError — совместимость


def test_missing_data_dir_missing_file_and_an_empty_cascade_are_all_refused(tmp_path):
    class Cascade:
        def __init__(self, path):
            pass

        def empty(self):
            return True
    no_data = types.SimpleNamespace(__version__="4.10.0", CascadeClassifier=Cascade)
    with pytest.raises(FaceBlurUnavailable) as err:
        FaceBlurrer(cv2_module=no_data)
    assert "cv2.data.haarcascades" in str(err.value)
    with pytest.raises(FaceBlurUnavailable) as err:
        FaceBlurrer(cascade_path=str(tmp_path / "nope.xml"), cv2_module=no_data)
    assert "не найден" in str(err.value)
    (tmp_path / "empty.xml").write_text("<x/>")
    with pytest.raises(FaceBlurUnavailable) as err:
        FaceBlurrer(cascade_path=str(tmp_path / "empty.xml"), cv2_module=no_data)
    assert "Не удалось загрузить каскад" in str(err.value)
    assert default_cascade_path(types.SimpleNamespace(data=types.SimpleNamespace(haarcascades=str(tmp_path)))).endswith(FACE_CASCADE_FILE)


def test_found_faces_are_blurred_with_a_margin_inside_the_frame_and_the_input_is_not_mutated(tmp_path):
    module, path = _cv2_with_fake_cascade(tmp_path, boxes=[[100, 80, 60, 60], [0, 0, 40, 40]])
    blurrer = FaceBlurrer(cascade_path=path, cv2_module=module)
    image = _noisy()
    original = image.copy()
    out, faces = blurrer.blur_faces(image)
    assert faces == 2 and (image == original).all()                            # входной массив не изменён
    assert not (out[80:140, 100:160] == original[80:140, 100:160]).all()       # лицо размыто
    assert not (out[75:80, 100:160] == original[75:80, 100:160]).all()         # запас 15% вокруг рамки захвачен
    assert (out[200:, 200:] == original[200:, 200:]).all()                     # остальной кадр не тронут
    assert out[0:30, 0:30].std() < original[0:30, 0:30].std()                  # лицо в углу: рамка обрезана по границе кадра без ошибки


def test_blur_kernel_is_forced_odd(tmp_path):
    module, path = _cv2_with_fake_cascade(tmp_path)
    make = lambda k: FaceBlurrer(cascade_path=path, cv2_module=module, blur_kernel=k)  # noqa: E731
    assert make(50).blur_kernel == 51 and make(51).blur_kernel == 51
    out, faces = make(50).blur_faces(_noisy())                                   # чётное ядро не роняет GaussianBlur
    assert faces == 0 and out.shape == (240, 320, 3)
