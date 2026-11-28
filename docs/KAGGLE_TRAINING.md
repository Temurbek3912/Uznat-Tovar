# Дообучение поиска по виду на Kaggle (бесплатная видеокарта)

«Узнать товар» уже ищет товар по векторам: модель SigLIP 2 превращает фото в вектор, а индекс FAISS находит ближайшие
векторы фото каталога. Дообучение — научить саму модель лучше отличать **ваши** товары друг от друга. Замер, что это даёт, —
`docs/benchmark/finetune_report_2026-11-28.md`.

**Итог 2026-11-28: включено.** Обучали на RTX 5060 ноутбука: ключа Kaggle на компьютере нет. Честная проверка по 5 частям:
«верный первым» 73,6 % вместо 70,1 %. С правилом двойника «найдено» 424 вместо 356 из 1290, из них неверно 0 вместо 2,
«не узнан» 187 вместо 280. В программе: `models/embedder/siglip2-base-shop-ft` + проекция
`models/projection/siglip2-base-shop-ft.npz`, `match_threshold` 0.84. Копия до включения —
`D:\uznat_tovar\backups-finetune-2026-11-28`.

Kaggle нужен только если на компьютере нет видеокарты NVIDIA: на ноутбуке с RTX 5060 то же обучение идёт ~35 минут
(команда в конце). Пароли и ключи программа и Claude за вас не вводят — шаги 1–3 делаете вы.

## Шаги владельца (один раз, ~10 минут)

1. Откройте **kaggle.com** → Register (можно через Google). Подтвердите **номер телефона**: Settings → Phone verification —
   без этого Kaggle не даёт видеокарту и интернет в ноутбуке.
2. Settings → раздел **API** → **Create New Token**. Скачается файл `kaggle.json` (это ключ — никому не пересылайте).
3. Положите его в папку `C:\Users\user\.kaggle\` (создайте папку, если её нет). Скажите Claude «ключ положил».

## Что дальше делает Claude (или вы — по командам)

```powershell
.venv\Scripts\python.exe -m pip install kaggle
```

```powershell
.venv\Scripts\python.exe -m ml.kaggle_push upload
```

Загрузка частного датасета: ~25 МБ, при 80 КБ/с около 5 минут. Видите его только вы.

```powershell
.venv\Scripts\python.exe -m ml.kaggle_push run
```

Ноутбук `ml/kaggle/finetune.ipynb` на видеокарте T4: 5 честных проверок + финальное обучение, ~30–40 минут. Модель SigLIP 2
(~375 МБ) Kaggle скачивает сам — ваш интернет не тратится. Готово ли — `... -m ml.kaggle_push status`.

```powershell
.venv\Scripts\python.exe -m ml.kaggle_push download
```

Результат ~22 МБ (около 5 минут) → `D:\uznat_tovar\kaggle-embedder\kaggle-out`: `lora_final.pt` (поправки модели, ~5 МБ),
`cv_*.npy` и `base.npy` (векторы для замера).

## Замер и включение

```powershell
.venv\Scripts\python.exe -m ml.finetune_check --dataset D:/uznat_tovar/kaggle-embedder/dataset --results D:/uznat_tovar/kaggle-embedder/kaggle-out
```

Таблица «было / стало» на тех же 1741 фото: снимок — фото товара, которого модель при обучении не видела. Включать новую
модель — только если «Верный первым» выше, а неверных «найдено» не больше, чем сейчас:

1. Копия: `scripts\lite_copy.py` (или полная копия) и папка `data\lite\index`.
2. `.venv\Scripts\python.exe -m ml.finetune_check --install D:/uznat_tovar/kaggle-embedder/kaggle-out/lora_final.pt`
   → `models\embedder\siglip2-base-shop-ft` (~375 МБ на диске D, в git не идёт).
3. В `configs/config.yaml`: `models.embedder.weights_path: "models/embedder/siglip2-base-shop-ft"`, `projection_path: ""`
   (проекция старой модели к новой не подходит).
4. `python -m scripts.rebuild_index --apply --compute-missing --yes` — векторы всех фото считаются новой моделью, ~2 минуты.
5. `python -m ml.train_projection --skip-check --out models/projection/siglip2-base-shop-ft.npz`. Обычная проверка там
   нечестная: финальная модель видела все товары. Честная проверка — `ml.finetune_check` (строка «Дообученная + проекция»,
   её пороги). Затем `projection_path: "models/projection/siglip2-base-shop-ft.npz"`, `match_threshold` / `margin_threshold`
   из этой строки — и ещё раз шаг 4.
6. Правило двойника (`twin_threshold`) проверялось вместе с моделью — отчёт, §3. После нового обучения проверить его
   так же.

Команды шагов 4–5 — с переменными облегчённого режима (`DATABASE_URL`, `MEDIA_ROOT`, `CATALOG_INDEX_PATH` на `data/lite`,
как в `lite_environment` из `scripts/start_demo.py`).

Вернуть как было: `weights_path: ""`, `projection_path: "models/projection/siglip2-base.npz"`, `match_threshold: 0.83`, затем та же пересборка индекса (векторы старой
модели остаются в кэше базы — фото не пересчитываются).

## Без Kaggle, на своей видеокарте

```powershell
.venv\Scripts\python.exe -m ml.shop_dataset --out D:/uznat_tovar/kaggle-embedder/dataset
```

```powershell
.venv\Scripts\python.exe -I ml\finetune_embedder.py --data D:/uznat_tovar/kaggle-embedder/dataset --out D:/uznat_tovar/kaggle-embedder/run1 --cv --final
```

(`ml.shop_dataset` запускается с переменными облегчённого режима — см. описание в начале файла.)
