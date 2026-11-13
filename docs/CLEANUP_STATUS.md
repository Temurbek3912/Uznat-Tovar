# Уборка прежнего аудита полок — состояние на 2026-11-06

Проект переходит от Retail Shelf Audit (аудит торговых полок) к «Узнать товар» (распознавание отдельного товара по фото).
Уборка выполнена по карте `docs/archive_audit/AUDIT_REMOVAL_MAP_V2.md` (шаги 1–8) и сверена с контрольной точкой 2026-11-02.

**Удалён только код. Данные не трогались:** таблицы PostgreSQL (магазины, полки, снимки, планограммы, камеры и др.), Docker volumes,
фото, модели и веса, файлы каталога и индекса остались как были. Приложение старые таблицы больше не читает. Миграций схемы не было.
Если в `.env` есть `CAMERA_SECRET_KEY`, его не удалять: им зашифрованы адреса камер в базе.

## 1. Что удалено (56 файлов)

| Группа | Файлы |
|---|---|
| API аудита (6) | `app/api/routes_agent.py`, `routes_cameras.py`, `routes_planograms.py`, `routes_reports.py`, `routes_review.py`, `routes_upload.py` |
| Конвейер аудита (8) | `app/pipeline/osa.py`, `pipeline.py`, `planogram.py`, `price_ocr.py` (OCR ценников), `recompute.py`, `rectify.py`, `review_crop.py`, `snapshot_store.py` |
| Сервисы аудита (7) | `app/services/camera_service.py`, `daily_report.py`, `export_service.py`, `planogram_bulk.py`, `planogram_import.py`, `report_service.py`, `review_service.py` |
| Скрипты и конфиги (5) | в `scripts/`: `camera_agent.py`, `daily_report.py`, `run_daily_report.bat`; в `configs/`: `agent.example.yaml`, `cameras.example.yaml` |
| Страницы (4) и их JS-проверки (4) | `frontend/capture.html`, `dashboard.html`, `review.html`, `setup.html`; `tools/capture_smoke_test.js`, `dashboard_`, `review_`, `setup_smoke_test.js` |
| Тесты удалённого кода (18) | `tests/test_camera_*` (5), `test_cameras_api`, `test_daily_report`, `test_export_api`, `test_export_service`, `test_pipeline`, `test_planogram_bulk`, `test_planogram_import`, `test_price_ocr`, `test_recompute`, `test_rectify`, `test_review_api`, `test_review_crop`, `test_snapshot_store` |
| Документы (4) | перенесены в `docs/archive_audit/` (не удалены): `CAMERAS.md`, `AUTOMATION_SETUP.md`, `AUDIT_REMOVAL_MAP.md`, `AUDIT_REMOVAL_MAP_V2.md` |

Тесты, которые проверяли ядро распознавания внутри удалённых файлов, перенесены, а не потеряны: `tests/test_matcher_basic.py` (matched / uncertain / unknown).

## 2. Что изменено (50 файлов, главное)

- `app/main.py` — подключены только auth, admin, stores, catalog и служебные маршруты.
- `app/worker.py` — три задачи: `add_catalog_product_task`, `add_catalog_photos_task`, `identify_product_task`.
- `app/pipeline/ml_runtime.py` — компоненты face_blurrer, detector, embedder, matcher (без OCR ценников).
- `app/core/schemas.py` — `DetectionStatus`, `BoundingBox`, `ProductIn`.
- `app/api/routes_admin.py`, `app/services/admin_service.py` — `GET /api/v1/admin/system` и `GET /api/v1/admin/products`.
- `docker-compose.yml`, `.env.example` — без сервиса агента камер и переменных камер (сервисы: db, redis, bootstrap, api, worker).
- Интерфейс: `index.html`, `nav.js`, `admin.html` (товары, пользователи, система), `manifest.webmanifest` — под «Узнать товар».
- Скрипты: `smoke_test.py` (без снимков полок; `--with-worker` отдаёт код 2), `bootstrap.py`, `diagnose.py`, `phone_url.py`, `start_demo.py`, `make_shortcut.py`, `seed_catalog.py`, `setup_store.py`.
- Документы: `README.md`, `docs/RUNBOOK.md`, `docs/DOCKER.md`, `docs/detector_quickstart.md`, `docs/pilot_plan.md`.
- Тесты обновлены под оставшийся код (список — в выводе `apply_update.py`). `tests/test_smoke_cleanup_static.py` теперь проверяет новую
  уборку smoke-теста: токены и привязки удаляются до пользователя, каждое групповое удаление ограничено этим пользователем (массового DELETE нет).

## 3. Что сохранено (не трогалось)

Каталог и поиск, фото товаров (несколько на SKU), штрихкоды EAN-13/EAN-8/UPC-A, YOLO и веса `models/detector/sku110k_yolo11n.pt`,
DINOv2, FAISS/Numpy-индекс, решения matched / uncertain / unknown / empty_catalog, Celery-задачи каталога, авторизация и роли,
интерфейс RU / UZ / EN, маскировка лиц (`app/pipeline/privacy.py`, решение: оставить до фазы 6).

**Этап Б (ещё не сделан):** магазины, `routes_stores.py`, `store_service.py`, модели `Store`/`ShelfORM`, привязки пользователей к магазинам
(`user_store_access`) и роли «по магазинам» пока на месте — на них опираются авторизация и админка. Их уборка — отдельный выпуск;
изменения схемы базы — только на копии дампа и по «да» владельца.

## 4. Проверки

| Проверка | Где | Результат |
|---|---|---|
| Статическая проверка (`tools/static_check.py`) | у меня | 0 замечаний |
| Висящие импорты (`tools/import_graph_check.py`) | у меня | нет (115 файлов) |
| JS-проверки (`tools/run_js_tests.py`) | у меня | 251 в 16 наборах, все прошли |
| `pytest tests` (без fastapi/sqlalchemy/torch) | у меня | 703 прошли, 4 упали, 4 пропущены. 4 падения — OpenCV моей среды не загружает каскад лиц; те же 4 падают и на контрольной точке до уборки. **Новых падений после уборки нет.** |
| Полный `pytest` в Docker | **у пользователя** | **не запускался** |
| Запуск через Docker Compose, страницы в браузере, RU/UZ/EN | **у пользователя** | **не проверялось** |
| Распознавание на реальных фото | — | **не проверялось** |

Число тестов у пользователя после уборки уменьшится (было 1144 passed): вместе с кодом ушли тесты удалённых функций.

## 5. Что осталось

1. Применить уборку к `D:\retail-shelf-audit` через `apply_update.py` (сначала проверка без `--apply`) и прогнать полный `pytest`.
2. `docker compose up -d`; если Docker предупредит о «orphan containers» (контейнер удалённого сервиса агента) — это не ошибка, данные не затронуты.
3. Этап Б — магазины и привязки (см. выше).
4. Интеграция YOLO → crop → DINOv2 → FAISS — после результатов `ml/probe_detector.py` на фото пользователя.
