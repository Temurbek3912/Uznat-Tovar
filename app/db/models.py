"""
ORM-модели персистентного слоя. Эмбеддинги каталога НЕ хранятся здесь как
BLOB в Postgres в проде — они живут в векторном индексе (FAISS/pgvector),
здесь хранится только путь/ссылка на них плюс метаданные для трассируемости.

СХЕМА МЕНЯЕТСЯ ТОЛЬКО МИГРАЦИЕЙ (с 2026-11-13): изменили модель — добавьте ревизию в migrations/versions/ (alembic revision -m …).
tests/test_migrations.py сверяет итог всех миграций с этими моделями. Пометки «create_all создаёт новую таблицу» ниже — история
до миграций: такие таблицы входят в базовую ревизию 0001.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String)
    address: Mapped[str] = mapped_column(String)
    format: Mapped[str] = mapped_column(String)  # hypermarket | supermarket | convenience
    # Миграция 0007: какие поля карточки видит покупатель (JSON-список кодов, app/services/shop_card.py); NULL — набор по умолчанию.
    # Срока годности в этом списке не бывает никогда (решение владельца 2026-11-14: он у каждой партии свой).
    card_fields: Mapped[str | None] = mapped_column(Text, nullable=True)


class ShelfORM(Base):
    """Реестр полок магазина — чтобы клиенты (телефон, камеры) могли выбрать полку ДО первого снимка.

    Раньше полка существовала лишь как строка shelf_id в planogram_slots и shelf_snapshots, без связи с
    магазином: список «магазин -> полки» нельзя было получить, пока по полке не пришёл снимок.
    shelf_id — внешний код полки, тот же, что в planogram_slots/shelf_snapshots; он глобально уникален
    (планограмма ключуется одним shelf_id, без store_id), поэтому коды вида «<магазин>-<полка>».
    Новая таблица — create_all создаёт её и в уже существующей БД (alembic в проекте нет).
    """

    __tablename__ = "shelves"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    store_id: Mapped[str] = mapped_column(String, ForeignKey("stores.id"), index=True)
    shelf_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)


PRODUCT_ACTIVE = "active"          # products.status (миграция 0006)
PRODUCT_ARCHIVED = "archived"      # в архиве: не распознаётся, не находится по штрихкоду, не виден покупателю; можно вернуть


class Product(Base):
    """Каталожный SKU. reference_image_urls хранит эталонные фото для построения эмбеддинга."""

    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    sku: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    brand: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    expected_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # для сверки с ценником (F11)
    embedding_index_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Миграция 0006 (документ 06, §4.1): товар в архиве не распознаётся и не виден покупателю, но не удаляется — его можно вернуть.
    status: Mapped[str] = mapped_column(String(16), default=PRODUCT_ACTIVE, server_default=PRODUCT_ACTIVE, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")   # номер правки: защита от тихой перезаписи
    # Миграция 0007: сведения для карточки покупателя (показ — по настройке магазина, Store.card_fields). Все необязательные.
    manufacturer: Mapped[str | None] = mapped_column(String, nullable=True)      # производитель
    country: Mapped[str | None] = mapped_column(String, nullable=True)           # страна производства
    net_content: Mapped[str | None] = mapped_column(String, nullable=True)       # вес или объём как на упаковке: «0,5 л», «200 г»
    ingredients: Mapped[str | None] = mapped_column(Text, nullable=True)         # состав
    description: Mapped[str | None] = mapped_column(Text, nullable=True)         # описание
    in_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)        # есть в наличии; NULL — не указано


class ProductBarcodeORM(Base):
    """Штрих-код товара (EAN-13/EAN-8; UPC-A хранится как EAN-13 с нулём — app/services/barcode.py). НОВАЯ таблица: create_all
    создаёт её и в уже существующей БД, не трогая прежние таблицы и данные (колонка в products потребовала бы миграции, которых нет).
    Код уникален во всей системе: один штрих-код не может вести на два товара; у товара кодов может быть несколько (штучный, упаковка)."""

    __tablename__ = "product_barcodes"

    barcode: Mapped[str] = mapped_column(String, primary_key=True)
    product_id: Mapped[str] = mapped_column(String, ForeignKey("products.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlanogramSlotORM(Base):
    __tablename__ = "planogram_slots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    shelf_id: Mapped[str] = mapped_column(String, index=True)
    row: Mapped[int] = mapped_column(Integer)
    slot_index: Mapped[int] = mapped_column(Integer)
    expected_product_id: Mapped[str] = mapped_column(String, ForeignKey("products.id"))
    expected_facings: Mapped[int] = mapped_column(Integer, default=1)


class ShelfSnapshot(Base):
    __tablename__ = "shelf_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    store_id: Mapped[str] = mapped_column(String, ForeignKey("stores.id"))
    shelf_id: Mapped[str] = mapped_column(String, index=True)
    image_path: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued")  # queued|processing|done|failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    detections: Mapped[list["DetectionORM"]] = relationship(back_populates="snapshot")
    report: Mapped["ComplianceReportORM"] = relationship(back_populates="snapshot", uselist=False)


class DetectionORM(Base):
    __tablename__ = "detections"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    snapshot_id: Mapped[str] = mapped_column(String, ForeignKey("shelf_snapshots.id"))
    x1: Mapped[float] = mapped_column(Float)
    y1: Mapped[float] = mapped_column(Float)
    x2: Mapped[float] = mapped_column(Float)
    y2: Mapped[float] = mapped_column(Float)
    detector_confidence: Mapped[float] = mapped_column(Float)
    matched_product_id: Mapped[str | None] = mapped_column(String, nullable=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String)
    shelf_row: Mapped[int | None] = mapped_column(Integer, nullable=True)     # для восстановления сетки полки
    slot_index: Mapped[int | None] = mapped_column(Integer, nullable=True)    # (дашборд, повторная сверка)
    price_status: Mapped[str | None] = mapped_column(String, nullable=True)      # F11: match|mismatch|ocr_failed
    detected_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_diff_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # F12 (human-in-the-loop): заполняются только когда status был "uncertain" и человек
    # его разобрал — см. app/core/review.py:decide_review. reviewed_at IS NULL — единственный
    # признак «ещё не тронуто» (см. ReviewService.list_queue); review_action хранит СЫРОЕ
    # решение (confirmed/corrected/marked_unknown) отдельно от итогового status — по одному
    # только status нельзя отличить «алгоритм сразу был уверен» от «человек подтвердил uncertain».
    reviewed_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_action: Mapped[str | None] = mapped_column(String, nullable=True)

    snapshot: Mapped[ShelfSnapshot] = relationship(back_populates="detections")


class ComplianceReportORM(Base):
    __tablename__ = "compliance_reports"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    snapshot_id: Mapped[str] = mapped_column(String, ForeignKey("shelf_snapshots.id"), unique=True)
    compliance_score: Mapped[float] = mapped_column(Float)
    osa_score: Mapped[float] = mapped_column(Float)
    violations_json: Mapped[str] = mapped_column(Text)   # сериализованный список ComplianceViolation
    empty_slots_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    snapshot: Mapped[ShelfSnapshot] = relationship(back_populates="report")


class User(Base):
    """Пользователь системы (F13). Роль — строка из app.core.security.Role;
    неизвестное значение в БД трактуется как «нет прав» (см. parse_role)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)  # в нижнем регистре
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Язык интерфейса, выбранный сотрудником (ru | uz | uz-Cyrl | en; NULL — не выбран). Миграция 0002.
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Блокировка, архив и «доступ до» (документ 05, §12.1, §12.5, §12.7; app/core/account_status.py). Миграция 0003.
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    block_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    access_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    store_access: Mapped[list["UserStoreAccess"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserStoreAccess(Base):
    """Какие магазины видит пользователь. Для ролей admin/analyst не используется
    (у них вся сеть), для merchandiser/regional_manager — единственный источник скоупа."""

    __tablename__ = "user_store_access"

    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    store_id: Mapped[str] = mapped_column(String, ForeignKey("stores.id"), primary_key=True)

    user: Mapped[User] = relationship(back_populates="store_access")


class ApiToken(Base):
    """Выданный токен доступа. Хранится ТОЛЬКО SHA-256 хеш — сам токен знает лишь клиент."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


# --------------------------------------------------------------------------- сеансы и второй фактор (2026-11-08, документ 18)
# Четыре НОВЫЕ таблицы рядом с users и api_tokens: create_all создаёт их в существующей базе, прежние таблицы и данные не меняются.
# По документу 05, §10 эти поля со временем станут колонками users и api_tokens (миграцией Alembic, этап 2Б) — пока Alembic нет,
# новые колонки в существующие таблицы не добавляются. Это НЕ вторая система входа: токен по-прежнему api_tokens, проверка —
# AuthService.resolve_token, зависимость — app/api/deps.get_current_user.


class UserSecurity(Base):
    """Безопасность учётной записи: поколение сеансов (документ 05, §12.11) и второй фактор TOTP.
    Строки может не быть — это «поколение 0, второго фактора нет»."""

    __tablename__ = "user_security"

    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    totp_secret_enc: Mapped[str | None] = mapped_column(String, nullable=True)     # действующий секрет, зашифрован (app/core/crypto.py)
    totp_pending_enc: Mapped[str | None] = mapped_column(String, nullable=True)    # секрет настройки, пока не подтверждён кодом
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # последний принятый шаг — повтор кода запрещён
    # Владелец системы (документ 05, §8; документ 18, §4.2): первый администратор, созданный bootstrap, или назначенный с консоли
    # сервера (scripts.user_admin owner). Через API признак не меняется никогда — ни маршрута, ни поля в запросе.
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Временный пароль (документ 05, §12.2): текущий пароль выдан администратором или взят из .env — годен только для установки
    # своего пароля (пропуск «смена пароля»), обычного сеанса с ним нет. Срок — temp_password_expires_at (NULL — без срока).
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    temp_password_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ПИН-код для входа на САМОМ компьютере программы (решение владельца 2026-11-15, app/core/local_pin.py; миграция 0008):
    # хеш scrypt, как у пароля. Через сеть ПИН не принимается. После PIN_MAX_FAILURES ошибок подряд ПИН отключается.
    pin_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    pin_set_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pin_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuthRecoveryCode(Base):
    """Код восстановления второго фактора: в базе только HMAC (app/core/recovery_codes.py), каждый код — один раз."""

    __tablename__ = "auth_recovery_codes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuthSessionMeta(Base):
    """Сведения о сеансе (один к одному с api_tokens): устройство, откуда вход, последняя активность, поколение, пройден ли 2FA.
    Удаляется вместе с токеном (ON DELETE CASCADE). Полного IP и полной строки браузера здесь нет (app/core/session_info.py)."""

    __tablename__ = "auth_session_meta"

    token_id: Mapped[str] = mapped_column(String, ForeignKey("api_tokens.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    device_label: Mapped[str | None] = mapped_column(String, nullable=True)
    device_kind: Mapped[str] = mapped_column(String, default="unknown")
    device_os: Mapped[str] = mapped_column(String, default="")
    device_browser: Mapped[str] = mapped_column(String, default="")
    source: Mapped[str] = mapped_column(String, default="unknown")                 # local | lan | remote | unknown
    ip_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    mfa: Mapped[bool] = mapped_column(Boolean, default=False)
    scope: Mapped[str] = mapped_column(String, default="full", nullable=False)       # full | enroll (пропуск настройки 2FA)
    listener: Mapped[str] = mapped_column(String, default="all", nullable=False)     # где выдан: public | admin | all
    auth_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)        # проверка пароля (и кода) — от неё предел 8 ч
    reauth_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)      # последнее подтверждение для важных операций
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    method: Mapped[str | None] = mapped_column(String, nullable=True)                # pin — вход по ПИН (простой → выход), иначе NULL


class AuthSetupLink(Base):
    """Одноразовая ссылка «Придумайте ПИН» (первый вход и «забыли ПИН», миграция 0008). Создаёт её только программа на этом
    компьютере (scripts/setup_link.py, запуск start_demo); открыть — только на этом компьютере. В базе — только хеш."""

    __tablename__ = "auth_setup_links"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuthMfaChallenge(Base):
    """Одноразовый пропуск второго шага входа: пароль уже проверен, ждём код из приложения. Живёт минуты, хранится только хеш."""

    __tablename__ = "auth_mfa_challenges"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    pass_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AuditEvent(Base):
    """Журнал действий (документ 05, §16; app/services/audit.py). Только добавление: изменение и удаление запрещены триггером
    (миграция 0004). Секретов нет: записи собираются из структурированных полей без паролей, токенов и кодов."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    store_id: Mapped[str | None] = mapped_column(String, nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)   # None — система или консоль сервера
    via: Mapped[str] = mapped_column(String(16))                                         # ui | api | import | assistant | system | console
    action: Mapped[str] = mapped_column(String(64), index=True)                          # user.blocked, login.succeeded, …
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String, nullable=True)


# --------------------------------------------------------------------------- фото товаров и кэш эмбеддингов (миграция 0005)


class ProductImage(Base):
    """Фото товара как сущность (документ 06, §4.1; документ 05, §13.3: sha256, UNIQUE (product_id, sha256)).

    role: reference — эталон для распознавания, display — витринное (в индекс не входит). is_active=False — фото выведено из
    эталонов; запись и файл остаются (фото пользователей не удаляются). source_ref — источник вектора в индексе
    («upload:<папка>/<файл>», «import:<sha256>», «file:<папка>/<файл>», http-ссылка); storage_path — путь файла внутри
    media_root или None (файл вне программы: папка импорта, ссылка). Связь с моделью товара (group_id, документ 06, §4.6) —
    позже, вместе с product_groups."""

    __tablename__ = "product_images"
    __table_args__ = (UniqueConstraint("product_id", "sha256", name="uq_product_images_product_sha256"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(String, ForeignKey("products.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="reference")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    storage_path: Mapped[str | None] = mapped_column(String, nullable=True)
    original_name: Mapped[str | None] = mapped_column(String, nullable=True)
    credit: Mapped[str | None] = mapped_column(String(200), nullable=True)   # чьё фото: «Open Food Facts, CC BY-SA» (0010)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EmbeddingModel(Base):
    """Паспорт модели эмбеддера (app/pipeline/embedder.model_signature) под коротким номером версии model_version: по нему
    индекс пересобирается из кэша без загрузки самой модели."""

    __tablename__ = "embedding_models"

    version: Mapped[str] = mapped_column(String(80), primary_key=True)
    signature_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ImageEmbedding(Base):
    """Кэш эмбеддингов (документ 07, §8): вектор фото, посчитанный моделью model_version. Ключ (image_id, model_version) —
    ключ идемпотентности: одно фото одной моделью считается один раз. vector — float32, little-endian, dim чисел."""

    __tablename__ = "image_embeddings"

    image_id: Mapped[str] = mapped_column(String, ForeignKey("product_images.id"), primary_key=True)
    model_version: Mapped[str] = mapped_column(String(80), ForeignKey("embedding_models.version"), primary_key=True)
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# --------------------------------------------------------------------------- камеры (только новые таблицы)
# Эти три таблицы НОВЫЕ: create_all создаёт их в уже существующей базе, не трогая прежние таблицы и данные.
# Поэтому связь «снимок <-> камера» хранится в camera_events (snapshot_id), а не колонкой в shelf_snapshots.


class CameraORM(Base):
    """Камера, управляемая из админ-панели. Адрес источника (с паролем) — ТОЛЬКО зашифрованным (app/core/crypto.py)."""

    __tablename__ = "cameras"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[str] = mapped_column(String)                 # usb | rtsp | http_stream | http_snapshot | video_file | image_file
    source_enc: Mapped[str] = mapped_column(Text)             # Fernet-токен; открытого адреса в БД нет
    source_masked: Mapped[str] = mapped_column(String)        # адрес без логина/пароля — для показа в интерфейсе
    store_id: Mapped[str] = mapped_column(String, ForeignKey("stores.id"), index=True)
    shelf_id: Mapped[str] = mapped_column(String, index=True)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=900)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Состояние ставит ТОЛЬКО агент своими отчётами (POST /api/v1/agent/cameras/{id}/report) — не конфигурация.
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)   # последний ОТПРАВЛЕННЫЙ снимок
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Команды из админ-панели: агент подхватывает при ближайшем опросе и сбрасывает отчётом.
    capture_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    test_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CameraEventORM(Base):
    """Журнал камеры: отправленные снимки, ошибки, проверки. Источник «истории снимков» и «последних событий»."""

    __tablename__ = "camera_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    camera_id: Mapped[str] = mapped_column(String, ForeignKey("cameras.id"), index=True)
    kind: Mapped[str] = mapped_column(String)                 # uploaded | bad_frame | camera_error | upload_error | test_ok | test_error
    message: Mapped[str] = mapped_column(Text, default="")
    snapshot_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class AgentStateORM(Base):
    """«Пульс» агента камер: когда он последний раз запрашивал список камер. Без него нельзя отличить «камера сломана» от «агент не запущен»."""

    __tablename__ = "agent_state"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    cameras_count: Mapped[int] = mapped_column(Integer, default=0)


class ScanEventORM(Base):
    """Один скан покупателя «Узнать товар» — для помощника владельца «почему не узнал» (решение владельца 2026-11-17, документ 16,
    §10.5). Только итог: когда, что ответила программа, какой товар или какое название вне базы. Кадра, адреса, устройства и
    чего-либо о покупателе нет (D-10, D-10а). Строки старше 90 дней удаляются сами (app/services/scan_log.py). Миграция 0009."""

    __tablename__ = "scan_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(16))                   # matched | uncertain | not_in_catalog | unknown
    via: Mapped[str | None] = mapped_column(String(8), nullable=True)  # barcode | look
    product_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    guess_name: Mapped[str | None] = mapped_column(String(120), nullable=True)   # not_in_catalog: как программа назвала товар
    guess_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)    # not_in_catalog: вид товара (drink, …)
