# Карта уборки аудита полок (этап 3, часть А)

Составлена 2026-10-23 по архиву `retail-shelf-audit-docker-ready (2).zip`. Файлы проекта этим документом **не менялись**.
Списки получены измерением (граф импортов по `ast` + имитация удаления на копии), а не «на глаз».

## Принцип: два этапа, ядро не трогаем

Аудит вплетён в общие части (воркер, админка, права, модели БД), поэтому:

- **Этап А (этот документ).** Убираем *поведение* аудита: роутеры, задачи воркера, конвейер, страницы, тесты.
  Общее ядро **не меняем**: `app/db/models.py`, `app/core/security.py`, `app/core/schemas.py` (кроме одной строки),
  `app/core/crypto.py`, `app/core/review.py`. Таблицы в БД не трогаем вообще.
- **Этап Б (позже, вместе с новой гибкой карточкой товара).** Чистим модели и схемы, удаляем `crypto`/`review`,
  решаем судьбу Celery/Redis по результатам замеров. Удаление таблиц из БД — только с резервной копией и по вашему слову.

Почему так: модели импортируют `crypto` и `review`, а новая структура данных всё равно перепишет модели.
Двойная переделка одних и тех же файлов — лишний риск.

## Перед началом (обязательно)

1. **Копия проекта** (папка или коммит git) и резервная копия БД скриптом `backup_db` из папки scripts.
2. **Базовый прогон тестов у вас**: `docker compose run --rm api python -m pytest -q` (или как вы обычно запускаете).
   Пришлите итоговые строки. Причина: в тестах прямо написано, что часть из них (например `test_review_api`) писалась в среде
   без FastAPI/SQLAlchemy и **ни разу не выполнялась**. Без базового прогона мы не отличим старые поломки от новых.
3. **Что проверялось без сети (песочница 2026-10-23, исходный проект, до любых правок):**
   `python tools/static_check.py app scripts ml tests tools` → 0 замечаний;
   `python tools/run_js_tests.py` → 304 проверки в 9 наборах, все прошли;
   `python tools/import_graph_check.py` → висящих импортов нет (148 файлов).
   `pytest` там запустить было нельзя (нет pytest, FastAPI, SQLAlchemy и сети). Если в новой среде сеть есть —
   поставьте зависимости (`pip install -r requirements.txt`) и запустите `pytest` сами; если нет — полные тесты запускаете вы.

## Удалить

**Роутеры (`app/api/`):** `routes_agent`, `routes_cameras`, `routes_planograms`, `routes_reports`, `routes_review`, `routes_upload`.

**Сервисы (`app/services/`):** `camera_service`, `daily_report`, `planogram_bulk`, `planogram_import`, `report_service`,
`review_service`, `export_service`.

**Конвейер (`app/pipeline/`):** `detector`, `osa`, `planogram`, `price_ocr`, `privacy`, `recompute`, `rectify`,
`review_crop`, `snapshot_store`, `pipeline`.

**Прочее код:** `ml/detector_kit.py`; в папке scripts — `camera_agent`, `daily_report` и `run_daily_report.bat`.

**Страницы:** `frontend/capture.html`, `dashboard.html`, `review.html`, `setup.html`.
**Конфиги:** `configs/agent.example.yaml`, `configs/cameras.example.yaml`. **Docker:** сервис `agent` в `docker-compose.yml`.

**Тесты целиком (21):** `test_camera_agent`, `test_camera_agent_managed`, `test_camera_network`, `test_camera_sources`,
`test_heuristic_detector`, `test_planogram_import`, `test_price_ocr`, `test_privacy`, `test_rectify`, `test_review_crop`,
`test_snapshot_store`, `test_review_api`, `test_camera_service`, `test_cameras_api`, `test_daily_report`, `test_detector_kit`,
`test_export_api`, `test_export_service`, `test_pipeline`, `test_planogram_bulk`, `test_recompute`.
(Останутся `test_review` и `test_camera_secrets`: они проверяют `core.review`/`core.crypto`, которые живут до этапа Б.)
Оговорка: `test_pipeline` импортирует ещё и `matcher`, а `test_recompute` — часть логики, общую с аудитом. Перед удалением
просмотреть их и убедиться, что проверки матчера есть и в `test_match_decision`/`test_matcher_persistence`; если нет, перенести нужные в них.

## Править (13 файлов; список измерен)

| Файл | Что поменять |
|---|---|
| `app/main.py` | убрать импорты и `include_router` для agent, cameras, planograms, reports, review, upload |
| `app/worker.py` | удалить задачи `process_snapshot_task` и `reindex_correction_task` и их помощников; **оставить** `add_catalog_product_task`, `add_catalog_photos_task`, `identify_product_task`. Проверить `_media_root` и `_link_barcodes` (нужны ли оставшимся задачам) |
| `app/pipeline/ml_runtime.py` | убрать детектор, OCR ценников, размытие лиц; оставить эмбеддер и матчер |
| `app/api/routes_admin.py` | убрать `/overview`, `/daily-report` и импорты cameras/daily_report/export; оставить `/system`, `/products` |
| `app/services/admin_service.py` | функция `iso_utc` импортируется из `camera_service`, перенести её в admin_service |
| `app/core/schemas.py` | убрать импорт `PriceCheckResult` и связанное поле (строки ~13 и ~96) |
| `ml/evaluate_matching.py` | использует из `detector_kit` три помощника (`IMAGE_EXTS`, `parse_label_text`, `yolo_to_xyxy`): перенести их внутрь или убрать этот режим оценки |
| `setup_store` (папка scripts) | убрать импорт планограмм; создание магазина оставить |
| `tests/test_admin_api.py`, `test_admin_service.py`, `test_auth_api.py`, `test_ml_runtime.py`, `test_stores_api.py` | убрать проверки удалённого, остальное оставить |

## Уже известные «подводные камни» (найдены чтением)

- `tests/test_docs_and_automation.py` читает все `docs/*.md` (кроме HANDOFF) и проверяет, что упомянутые в них скрипты существуют.
  После удаления камер и отчётов из него надо убрать три теста: про флаги `camera_agent`, про `.bat` планировщика и про `AUTOMATION_SETUP`;
  тест про переменные `CAMERA_*` пересмотреть. Устаревшие документы (`CAMERAS.md`, `AUTOMATION_SETUP.md`, `detector_quickstart.md`,
  `training_guide.md`, `pilot_plan.md`) просмотреть и удалить или переписать. Новые документы не должны упоминать несуществующие скрипты.
- `tests/test_imageio.py` ссылается на `app/api/routes_review.py` — убрать из списка.
- `tools/run_js_tests.py` и `tests/test_run_js_tests.py` берут все `tools/*_smoke_test.js`: вместе со страницами удалить
  `capture`, `dashboard`, `review`, `setup` из набора и поправить ожидаемый список (сейчас 9 наборов, останется 5).
- `tests/test_static_check.py` («ворота»): весь код проекта, включая `tools/`, обязан проходить `tools/static_check.py` без замечаний.
  Это касается и добавленного `tools/import_graph_check.py` (проверено: проходит) и всех новых файлов.
- `app/db/models.py` импортирует `app.core.crypto` и `app.core.review`: до этапа Б их удалять нельзя.

## Проверить на строковые связи (возможны правки)

Эти тесты и файлы не импортируют удаляемое, но могут проверять *тексты* (страницы, compose, документацию):
`test_compose_config`, `test_docs_and_automation`, `test_frontend_pages`, `test_pwa_assets`, `test_run_js_tests`,
`test_smoke_cleanup_static`, `test_status_codes_static`, `test_worker_static`, `test_readiness`, `test_store_service`,
`test_bootstrap`, `test_preflight`, `test_safety_tools`, `test_security`, `test_smoke_helpers`.
Фронтенд: `index.html` (ссылки на 4 страницы), `nav.js`, `admin.html` (вкладки полок, камер, отчётов).
Скрипты: `bootstrap.py`, `diagnose.py`, `seed_catalog.py`, `smoke_test.py`, `create_user.py` (упоминают модели аудита).
Инструменты: `tools/capture_smoke_test.js`, `home_smoke_test.js`, `e2e_demo.py`, `ui_screenshots/*`.
Документы: `README.md`, `docs/*` (описывают аудит).

## Остаётся на этом этапе без изменений

`stores`/`routes_stores` (нужны для «разных магазинов»; полки в них пока остаются), `users`/`auth`/права, все таблицы БД
(`shelves`, `shelf_snapshots`, `detections`, `cameras` и др. остаются пустыми и безвредными), `crypto`, `review`,
Celery/Redis (решим по замерам), каталог, штрих-коды, `identify`.

## Проверка после правок

1. `python tools/import_graph_check.py` — должно быть `OK: висящих импортов нет`.
2. `python tools/static_check.py app scripts ml tests tools` — должно быть `Найдено замечаний: 0`.
3. `python tools/run_js_tests.py` — все наборы `ok` (после уборки останется 5 наборов вместо 9).
4. `python -m pytest -q` у вас (или в среде с сетью); присылаете итог. Ожидаемо: несколько красных тестов на тексты документации/страниц — это обычная
   цена такой чистки, они чинятся быстро.
5. Ручная проверка у вас: открыть «Товары» и «Узнать товар», добавить товар, узнать товар по штрих-коду.
6. Только после этого — этап Б.

## Риски

- Тесты не запускались в моей среде; безопасность даёт только статическая проверка и ваш прогон.
- Основная опасность — `app/worker.py` (общий узел) и `ml_runtime.py`: ошибка там ломает и «Товары», и «Узнать товар».
- Если базовый прогон тестов покажет много старых красных — сначала чиним их, потом убираем аудит.
