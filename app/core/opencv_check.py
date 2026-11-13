"""
Проверка OpenCV по ФАКТИЧЕСКИМ возможностям, а не по метаданным pip (только stdlib + сам cv2; общий код для воркера,
API, scripts/check_opencv.py и тестов).

Зачем. В образе оказались OpenCV 5.0.0.93 в ДВУХ сборках сразу (opencv-python и opencv-python-headless): `pip check` ответил
«No broken requirements found», а cv2.CascadeClassifier отсутствовал, и воркер не мог создать FaceBlurrer. Метаданных мало —
проверяем то, что приложению реально нужно.
"""
from __future__ import annotations

from importlib import metadata
from pathlib import Path

OPENCV_DISTS = ("opencv-python", "opencv-python-headless", "opencv-contrib-python", "opencv-contrib-python-headless")
EXPECTED_DIST = "opencv-python-headless"
FACE_CASCADE = "haarcascade_frontalface_default.xml"
MAX_MAJOR = 4          # OpenCV 5.x не содержит CascadeClassifier (проверено на 5.0.0.93)


def installed_distributions() -> dict[str, str]:
    """{имя пакета pip: версия} для всех установленных сборок OpenCV."""
    found: dict[str, str] = {}
    for name in OPENCV_DISTS:
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return found


def distribution_problems(dists: dict[str, str]) -> list[str]:
    problems: list[str] = []
    if len(dists) > 1:
        names = ", ".join(f"{n}=={v}" for n, v in sorted(dists.items()))
        problems.append(
            f"установлено несколько сборок OpenCV ({names}): они пишут в одну папку cv2/ и перезаписывают друг друга. "
            f"Оставьте одну — {EXPECTED_DIST} (pip uninstall -y {' '.join(n for n in dists if n != EXPECTED_DIST)} && "
            f"pip install --force-reinstall --no-deps \"{EXPECTED_DIST}>=4.9,<5\")."
        )
    elif not dists:
        problems.append(f"не найдено ни одной сборки OpenCV в метаданных pip (ожидается {EXPECTED_DIST}).")
    return problems


def capability_problems(cv2_module) -> list[str]:
    """Что приложению нужно от cv2: версия <5, CascadeClassifier, файл каскада лиц, который реально загружается."""
    problems: list[str] = []
    version = str(getattr(cv2_module, "__version__", "?"))
    try:
        major = int(version.split(".")[0])
    except ValueError:
        major = 0
    if major > MAX_MAJOR:
        problems.append(f"cv2 {version}: версия 5.x не поддерживается — в колёсах 5.x нет файлов каскадов (cv2/data пуст), а "
                        "cv2.CascadeClassifier есть только в сборке contrib (модуль xobjdetect). Нужна >=4.9,<5.")
    if not hasattr(cv2_module, "CascadeClassifier"):
        problems.append(f"в cv2 {version} нет атрибута CascadeClassifier — размытие лиц (app/pipeline/privacy.py) работать не может.")
        return problems
    data = getattr(cv2_module, "data", None)
    cascades_dir = getattr(data, "haarcascades", None)
    if not cascades_dir:
        problems.append(f"в cv2 {version} нет cv2.data.haarcascades — не найти папку с каскадами.")
        return problems
    path = Path(cascades_dir) / FACE_CASCADE
    if not path.is_file():
        problems.append(f"файл каскада лиц не найден: {path}")
        return problems
    try:
        empty = cv2_module.CascadeClassifier(str(path)).empty()
    except Exception as exc:  # noqa: BLE001 — любая ошибка конструктора — это проблема, которую нужно показать, а не скрыть
        problems.append(f"cv2.CascadeClassifier({path}) завершился ошибкой: {type(exc).__name__}: {exc}")
        return problems
    if empty:
        problems.append(f"каскад лиц не загрузился (CascadeClassifier.empty() == True): {path}")
    return problems


def report_lines(cv2_module, dists: dict[str, str]) -> list[str]:
    return [
        f"cv2 {getattr(cv2_module, '__version__', '?')} ({getattr(cv2_module, '__file__', '?')})",
        "сборки pip: " + (", ".join(f"{n}=={v}" for n, v in sorted(dists.items())) or "нет"),
        f"CascadeClassifier: {'есть' if hasattr(cv2_module, 'CascadeClassifier') else 'НЕТ'}; "
        f"cv2.barcode: {'есть' if hasattr(cv2_module, 'barcode') else 'нет (запасной читатель штрих-кодов недоступен)'}",
    ]
