# Дообучение поиска по виду на Kaggle (бесплатная видеокарта)

«Узнать товар» уже ищет товар по векторам: модель SigLIP 2 превращает фото в вектор, а индекс FAISS находит ближайшие
векторы фото каталога. Дообучение — научить саму модель лучше отличать **ваши** товары друг от друга. Замер, что это даёт, —
`docs/benchmark/finetune_report_2026-11-27.md`.

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
3. В `configs/config.yaml`: `models.embedder.weights_path: "models/embedder/siglip2-base-shop-ft"`, пороги из отчёта; проекция
   старой модели к новой не подходит (программа её сама не применит) — переобучить `python -m ml.train_projection` после
   пересборки индекса.
4. `python -m scripts.rebuild_index --apply --compute-missing` (векторы всех фото считаются новой моделью, ~2 минуты).

Вернуть как было: `weights_path: ""`, прежние `projection_path` и пороги, затем та же пересборка индекса (векторы старой
модели остаются в кэше базы — фото не пересчитываются).

## Без Kaggle, на своей видеокарте

```powershell
.venv\Scripts\python.exe -m ml.shop_dataset --out D:/uznat_tovar/kaggle-embedder/dataset
```

```powershell
.venv\Scripts\python.exe -I ml\finetune_embedder.py --data D:/uznat_tovar/kaggle-embedder/dataset --out D:/uznat_tovar/kaggle-embedder/run1 --cv --final
```

(`ml.shop_dataset` запускается с переменными облегчённого режима — см. описание в начале файла.)
