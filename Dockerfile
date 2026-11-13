# Образ для API, воркера и bootstrap (одна сборка, три сервиса — см. docker-compose.yml). CPU-сборка: GPU в контейнере
# не используется. Для ML-инференса на GPU позже понадобится отдельный образ на базе nvidia/cuda (см. TODO в compose).
FROM python:3.14-slim

# PYTHONDONTWRITEBYTECODE: проект примонтирован в /srv/app с хоста — не засоряем его папками __pycache__.
# PIP_RETRIES/PIP_DEFAULT_TIMEOUT: torch и CUDA-библиотеки весят гигабайты; на нестабильной сети pip не должен сдаваться
# с первой оборванной загрузки. HF_HOME: кэш моделей HuggingFace лежит в именованном томе (hf_cache), а не в слое контейнера.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_RETRIES=8 \
    PIP_DEFAULT_TIMEOUT=120 \
    HF_HOME=/root/.cache/huggingface

WORKDIR /srv/app

RUN apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-uzb \
    tesseract-ocr-uzb-cyrl \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt обязателен; constraints-opencv.txt — обязательный «замок» OpenCV (<5, см. файл); constraints.txt — необязательный
# «замок» версий всего остального (docs/DOCKER.md, «Закрепить версии»): если он есть, сборка берёт ровно те версии, что были в
# проверенном образе, а не «самые свежие на сегодня».
COPY requirements.txt constraints-opencv.txt constraints*.txt ./
RUN if [ -f constraints.txt ]; then \
        pip install -r requirements.txt -c constraints-opencv.txt -c constraints.txt; \
    else \
        pip install -r requirements.txt -c constraints-opencv.txt; \
    fi

# ultralytics объявляет зависимость opencv-python (сборка с GUI) — она оказывается рядом с opencv-python-headless, и обе пишут
# в одну папку cv2/. Оставляем РОВНО одну сборку — headless (<5): сначала удаляем все, затем ставим её без зависимостей.
RUN pip uninstall -y opencv-python opencv-contrib-python opencv-python-headless opencv-contrib-python-headless \
    && pip install --no-deps -c constraints-opencv.txt "opencv-python-headless>=4.9,<5"

COPY . .

# Проверка ФАКТИЧЕСКИХ возможностей OpenCV (а не метаданных pip): одна сборка, версия <5, cv2.CascadeClassifier существует и
# загружает каскад лиц. Падает при любом нарушении — размытие лиц обязательно, образ без него собирать нельзя.
RUN python -m scripts.check_opencv

# Сборка падает, если какой-то импорт из кода не объявлен в requirements.txt или не установился, либо нет tesseract.
# Лучше узнать об этом здесь, чем когда контейнер уже «Up», а приложение падает на первом запросе.
RUN python -m scripts.check_imports --runtime

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
