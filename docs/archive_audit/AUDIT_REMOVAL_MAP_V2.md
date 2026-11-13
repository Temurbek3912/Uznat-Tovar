# Карта уборки v2 (фаза 2, часть А — только измерение)

Составлена 2026-10-30 по проекту выпуска 2026-10-30 (озвучка уже удалена). **Файлы проекта этим документом не менялись.** Ничего не удаляем, пока не выполнены условия из раздела 7.
Заменяет `docs/AUDIT_REMOVAL_MAP.md` (карта от 2026-10-23, написанная под старую концепцию «аудит полок»); та карта остаётся как история.

## 1. Как измерено и чему можно верить

- Граф импортов проекта (`app`, `scripts`, `ml`, `tests`) разобран через `ast`; для каждого набора модулей считалось: какие оставшиеся файлы на них ссылаются, какие тесты затронуты, какие модули осиротеют.
- Поверх этого — поиск по тексту (тесты, Docker, конфиги, скрипты запуска, документы), потому что граф не видит файлы, которые читаются по пути, и упоминания в строках.
- Сам измеряющий скрипт (разбор `ast`, имитация удаления) лежит вне проекта, в рабочей папке чата; при уборке действующая проверка — `tools/import_graph_check.py` (ловит висящие импорты после каждой правки). Если скрипт понадобится снова, его надо написать заново по этому описанию.
- **Проверка метода:** тот же инструмент на правилах карты 2026-10-23 выдал те же 13 файлов на правку, что записаны в старой карте: 8 кодовых (`routes_admin`, `schemas`, `main`, `ml_runtime`, `admin_service`, `worker`, `evaluate_matching`, `setup_store`) и 5 тестовых (`test_admin_api`, `test_admin_service`, `test_auth_api`, `test_ml_runtime`, `test_stores_api`).
- **Чего это не ловит:** динамические импорты и имена из строк, поведение (то, что файл делает, а не то, что импортирует), тесты, которые «подменяют» модуль заглушкой и из-за этого не ссылаются на него. Окончательный судья — `pytest` у вас: у меня нет FastAPI, SQLAlchemy и pytest.

## 2. Что изменилось по сравнению с картой 2026-10-23

1. **D10 из `PLAN_V2.md`:** детектор (`app/pipeline/detector.py`), набор `ml/detector_kit.py`, матчер, эмбеддер, `ml_runtime` и очередь задач **остаются**. Поэтому `ml/evaluate_matching.py` больше не правится (в старой карте правился из-за `detector_kit`), `ml_runtime.py` правится только из-за OCR ценников.
2. **D6:** таблицы магазинов и полок остаются; `routes_stores`, `store_service`, `scripts/setup_store.py` и роли в правах (включая роль `camera_agent`) остаются до переделки прав (фазы 3–4). Многие упоминания слова `camera_agent` в коде — это **название роли**, а не скрипт.
3. **Маршрут `/system` в админке нельзя просто «оставить»**, как говорила старая карта: он сам использует `agent_last_seen`, `iso_utc`, `tz_for` из удаляемых модулей.
4. **`_load_or_create_index` в `worker.py` нельзя удалять**, хотя внутри файла её никто не вызывает: её лениво подтягивает `ml_runtime` (`from app.worker import _load_or_create_index`).
5. **Связи, которых нет в графе импортов:** `tests/test_worker_static.py` читает `worker.py` как текст и требует, чтобы все пять задач были на месте; `tests/test_status_codes_static.py` и `tests/test_imageio.py` читают файлы роутеров по пути; сервис `agent` в `docker-compose.yml`; меню `nav.js`, `index.html`, `admin.html`, `scripts/phone_url.py`, `scripts/start_demo.py` ссылаются на удаляемые страницы; `scripts/bootstrap.py` и `routes_stores.py` подсказывают открыть `setup.html`.
6. **Тестов на удаление 18, а не 21:** `test_heuristic_detector`, `test_detector_kit`, `test_privacy` остаются вместе с тем, что они проверяют.

## 3. Удалить (всё измерено; порядок — в разделе 6)

| Что | Состав | Размер |
|---|---|---|
| Роутеры (6) | `routes_agent`, `routes_cameras`, `routes_planograms`, `routes_reports`, `routes_review`, `routes_upload` | 977 строк |
| Сервисы (7) | `camera_service`, `daily_report`, `planogram_bulk`, `planogram_import`, `report_service`, `review_service`, `export_service` | 1 640 строк |
| Конвейер аудита (8) | `osa`, `planogram`, `price_ocr`, `recompute`, `rectify`, `review_crop`, `snapshot_store`, `pipeline` | 764 строки |
| Скрипты (2) + запуск | `scripts/camera_agent.py`, `scripts/daily_report.py`, `scripts/run_daily_report.bat` | 1 144 строки |
| **Итого в коде** | **23 модуля** | **4 525 строк** |
| Конфиги и Docker | `configs/agent.example.yaml`, `configs/cameras.example.yaml`, сервис `agent` (профиль `agent`) в `docker-compose.yml`, строки про агента в `.env.example` | — |
| Страницы (4) | `frontend/capture.html`, `dashboard.html`, `review.html`, `setup.html` (всё внутри файла, отдельных `.js` нет) | 2 154 строки |
| JS-проверки страниц | `tools/capture_smoke_test.js`, `dashboard_smoke_test.js`, `review_smoke_test.js`, `setup_smoke_test.js` | 139 из 414 проверок |
| Тесты на Python (18 файлов) | `test_camera_agent`, `test_camera_agent_managed`, `test_camera_network`, `test_camera_sources`, `test_planogram_import`, `test_price_ocr`, `test_rectify`, `test_review_crop`, `test_snapshot_store`, `test_review_api`, `test_camera_service`, `test_cameras_api`, `test_daily_report`, `test_export_api`, `test_export_service`, `test_pipeline`, `test_planogram_bulk`, `test_recompute` | 320 из 1 091 функций `test_` (около 29 %), 3 994 строки |

Оговорки к тестам: `test_review_crop` кроме удаляемого проверяет и `privacy` (остаётся — см. вопрос 1), `test_pipeline` заодно использует `matcher`: перед удалением убедиться, что проверки матчера есть в `test_match_decision` и `test_matcher_persistence`, иначе перенести нужные.
Документы на удаление или пометку «история»: `docs/CAMERAS.md`, `docs/AUTOMATION_SETUP.md`, `docs/pilot_plan.md`; `README.md` и `docs/RUNBOOK.md`, `docs/DOCKER.md` переписываются под новую концепцию (фаза 12; до тех пор в них остаются устаревшие разделы).

## 4. Править (удаляемое из них вынуть, остальное оставить)

| Файл | Что именно |
|---|---|
| `app/api/routes_admin.py` | удалить маршруты `/overview` (стр. 43–96) и `/daily-report` (162–179); `/system` (119–138) **править**: убрать `agent_last_seen`, `tz_for`, время через `iso_utc`; оставить `/products` |
| `app/services/admin_service.py` | `merge_recent_events` и `agent_status` берут `iso_utc` из `camera_service`: перенести `iso_utc` сюда; `agent_status` после удаления агента, скорее всего, не нужна (проверить, кто её вызывает) |
| `app/main.py` | убрать импорты и `include_router` для шести роутеров |
| `app/worker.py` | удалить задачи `process_snapshot_task` (270–341) и `reindex_correction_task` (345–400) и помощников, нужных только им: `_run_price_checks` (227–266), `_load_planogram` (163–174), `_mark_snapshot_failed` (113–124), их импорты. **Оставить:** `add_catalog_product_task`, `add_catalog_photos_task`, `identify_product_task` и помощников `_apply_runtime`, `_download_image`, `_init_ml_components`, `_link_barcodes`, `_media_root`, `_ml_unavailable`, `_note_indexed`, `_require_ml`, а также `_load_or_create_index`. Осторожно: комментарий «БАГФИКС» около строки 403 — декоратор `add_catalog_product_task` уже один раз терялся при вставке соседней задачи |
| `app/pipeline/ml_runtime.py` | в `default_factories` (123–164) убрать создание `HeuristicPriceTagLocalizer` и `PriceTagOCR`; детектор, эмбеддер, матчер оставить |
| `app/core/schemas.py` | класс `Detection` (38–48) содержит поле типа `PriceCheckResult`: убрать поле и импорт, сам класс оставить (его используют детектор и матчер) |
| `scripts/setup_store.py` | удалить команду `cmd_planogram` (75–106) и её подкоманду; создание магазина оставить |
| `scripts/bootstrap.py`, `scripts/phone_url.py`, `scripts/start_demo.py`, `app/api/routes_stores.py` | тексты и ссылки на `setup.html`, `capture.html`, `dashboard.html`, `review.html` |
| `frontend/nav.js`, `index.html`, `admin.html`, `tools/ui_screenshots/scenarios.json` | убрать ссылки и сценарии удаляемых страниц; `index.html` как главную заменит фаза 5 |
| Тесты (править, не удалять) | `test_admin_api`, `test_admin_service`, `test_auth_api`, `test_stores_api` (упоминают `routes_upload` и `process_snapshot_task`), `test_ml_runtime`, `test_worker_static` (требует пять задач, а останется три), `test_status_codes_static`, `test_imageio`, `test_compose_config`, `test_docs_and_automation`, `test_frontend_pages`, `test_pwa_assets`, `test_safety_tools`; JS: `nav_smoke_test`, `home_smoke_test`, `admin_smoke_test` |

Ещё упоминают удаляемое, но **не требуют правки сейчас**: `app/core/config.py` (раздел `osa`, комментарии про `routes_upload`) и `configs/config.yaml` — это настройки аудита, их место в этапе Б вместе с моделью настроек; `app/core/review.py` осиротеет после удаления `routes_review` и `review_service` (97 строк) — удалять в этапе Б вместе с `core/crypto.py` (его ещё использует `scripts/generate_secret_key.py`).

## 5. Оставить (и почему)

- `detector`, `ml/detector_kit.py`, `matcher`, `embedder`, `imageio`, `ml_runtime`, очередь задач и `app/worker.py` в урезанном виде — D10, нужны поиску по фото; решение по детектору — после замеров (фаза 7).
- `app/core/public_url.py` — **не сирота**: это новый модуль постоянного адреса, пока не подключён к приложению (фаза 1).
- `routes_stores`, `store_service`, `scripts/setup_store.py`, таблицы БД, роли и права (`camera_agent` и др.) — до переделки прав (фазы 3–4, D6).
- `pytesseract` и пакеты tesseract в `Dockerfile`: после удаления `price_ocr.py` в проекте ими не пользуется ничего, но чтение текста с коробки (задача `ocr` в `bench`) может их потребовать — решить по результатам замеров, не раньше.
- Каталог и «Узнать товар» **не зависят от магазинов**: в `routes_catalog`, `catalog_service`, `identify_service` слова «магазин» нет вообще. Значит, удаление страницы `setup.html` каталог не ломает; магазин при необходимости создаётся скриптом `setup_store`.

## 6. Порядок шагов (проверен измерением: на каждом шаге ни один оставшийся файл кода не ссылается на уже удалённое)

После **каждого** шага: `python tools/static_check.py app scripts ml tests tools`, `python tools/import_graph_check.py`, `python tools/run_js_tests.py`, затем у вас полный `pytest`. Падение — шаг откатывается из копии.

1. **Правка админки:** `routes_admin.py` и `admin_service.py` (раздел 4), тесты `test_admin_api`, `test_admin_service`.
2. **Роутеры:** убрать их из `main.py`, удалить 6 файлов и тесты `test_review_api`, `test_cameras_api`, `test_export_api`; поправить `test_auth_api`, `test_stores_api`, `test_status_codes_static`, `test_imageio`.
3. **Правка `worker.py`** (удалить две задачи и помощников); `test_worker_static` теперь ждёт три задачи.
4. **Сервисы и скрипты:** 7 сервисов, `scripts/daily_report.py`, `scripts/run_daily_report.bat`, `scripts/camera_agent.py` и их тесты (`test_camera_service`, `test_daily_report`, `test_export_service`, `test_planogram_bulk`, `test_planogram_import`, `test_camera_agent`, `test_camera_agent_managed`, `test_camera_network`, `test_camera_sources`); правка `scripts/setup_store.py`, `test_docs_and_automation`.
5. **Конвейер аудита:** 8 модулей, правки `ml_runtime.py` и `schemas.py`, тесты `test_price_ocr`, `test_rectify`, `test_snapshot_store`, `test_review_crop`, `test_pipeline`, `test_recompute`; правка `test_ml_runtime`.
6. **Docker и конфиги:** сервис `agent`, два примера конфигов, `.env.example`, `test_compose_config`.
7. **Страницы:** 4 страницы, их JS-проверки, ссылки в `nav.js`, `index.html`, `admin.html`, сценарии снимков, `test_frontend_pages`, `test_pwa_assets`.
8. **Документы** (раздел 3).

Ожидаемый эффект: код `app`/`scripts` меньше на 4 525 строк, тестов на Python меньше примерно на 320 функций, JS-проверок станет около 275 (414 минус 139, плюс-минус правки). Точные цифры `pytest` я назвать не могу: он считает ещё параметры.

## 7. Условия начала (технический барьер, не вежливость)

1. Резервная копия проекта и дамп базы (в `INSTRUCTION_RU.md`, шаг 1; у вас они есть от 2026-10-25/26 — после выпуска 2026-10-30 лучше свежие).
2. Повторный `pytest` после выпуска 2026-10-30 зелёный (шаг 3 инструкции). Без него мы не отличим старые поломки от новых.
3. Ваши ответы на вопросы ниже.

## 8. Вопросы к владельцу

1. **Маскировка лиц (`app/pipeline/privacy.py`, 97 строк).** Раньше она скрывала лица на снимках полок. Теперь клиенты будут присылать фото товаров с телефона, и на них иногда попадают люди. Предлагаю **оставить** модуль (и его тест) до фазы 6 и решить тогда. Удалить сейчас — ещё минус 97 строк, но без маскировки.
2. **Чтение ценников (`price_ocr.py`).** Это сверка ценника на полке с каталогом, она нужна только аудиту. Предлагаю удалить; если когда-нибудь захотите сверять цену по фото ценника — вернём из копии.
3. **Страницы «Съёмка», «Обзор», «Настройка магазина», «Управление магазином»** предлагаю удалить сейчас, а не ждать замены в фазе 5: у них нет аналога в новой концепции. Цена решения: до фазы 5 главная страница (`index.html`) и «Управление» останутся с урезанными ссылками.
