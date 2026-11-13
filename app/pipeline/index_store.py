"""
Надёжное хранение файлов индекса каталога на диске (2026-11-07). Используется app/pipeline/matcher.py.

Индекс — единственная копия эталонных векторов. Его пишут воркер (добавление товара и фото) и скрипты импорта
(scripts/import_catalog.py, scripts/reindex_uploads.py), часто в разных контейнерах с общей папкой models/. Здесь собраны
правила, без которых возможны потеря эталонов или ответ «чужим» товаром:

1. Атомарная замена: временный файл в той же папке -> fsync -> os.replace. Обрыв посреди записи оставляет прежний файл.
2. Блокировка <индекс>.lock на время чтения и записи (fcntl.flock в Linux, msvcrt.locking в Windows): проверка «файл не
   менялся» и замена выполняются без вмешательства другого процесса. Если файловая система блокировки не поддерживает,
   работа продолжается без неё (с предупреждением в журнале), и остаются только проверки ниже.
3. Поколение: при каждом сохранении записывается новый случайный идентификатор. Процесс помнит поколение, которое загрузил
   или записал сам, и не перезаписывает файл другого поколения (точное сравнение, а не время изменения файла).
4. Замена на Windows: антивирус или резервное копирование могут держать файл открытым — os.replace повторяется несколько раз.
"""
from __future__ import annotations

import errno
import hashlib
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

REBUILD_HINT = ("Файл не перезаписан. Восстановите индекс из резервной копии или пересоберите его "
                "(порядок полной пересборки — в начале scripts/reindex_uploads.py).")
LOCK_TIMEOUT_SECONDS = 60.0
REPLACE_ATTEMPTS = 6
_UNSUPPORTED_LOCK_ERRNOS = {errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL,
                            getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)}


class IndexIntegrityError(RuntimeError):
    """Файл индекса есть, но пользоваться им нельзя (повреждён, не сходится с id, другая размерность)."""


class IndexChangedError(IndexIntegrityError):
    """Файл индекса записан другим процессом после того, как этот процесс его загрузил: перезапись стёрла бы чужие изменения."""


class IndexBusyError(IndexIntegrityError):
    """Индекс слишком долго занят другим процессом (блокировка не освободилась)."""


def new_generation() -> str:
    return uuid.uuid4().hex


def tmp_name(final: Path) -> Path:
    """Временный файл рядом с итоговым. Уникален и между процессами в разных контейнерах (pid там может совпадать)."""
    return final.with_name(f"{final.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fsync_file(path: Path) -> None:
    with open(path, "rb+") as fh:
        os.fsync(fh.fileno())


def fsync_dir(directory: Path) -> None:
    """После os.replace — сброс записи о переименовании (POSIX). В Windows папку так открыть нельзя: пропускаем."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def replace_with_retry(src: Path, dst: Path, attempts: int = REPLACE_ATTEMPTS, first_delay: float = 0.1,
                       sleep=time.sleep) -> None:
    """os.replace с повторами при PermissionError: в Windows файл, открытый антивирусом или программой копирования, нельзя
    заменить, пока его не отпустят (обычно доли секунды). Другие ошибки — сразу наверх."""
    delay = first_delay
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            logger.warning("Файл %s занят (попытка %d из %d), повтор через %.1f с", dst, attempt, attempts, delay)
            sleep(delay)
            delay *= 2


def file_probe(path: Path):
    """Дешёвый признак «файл, возможно, изменился»: (размер, время изменения, inode) или None, если файла нет.
    Только для решения «стоит ли перечитать поколение»; решение о перезаписи принимается по поколению."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _try_lock(fh) -> None:
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl  # noqa: PLC0415
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh) -> None:
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # noqa: PLC0415
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _is_busy(exc: OSError) -> bool:
    return isinstance(exc, BlockingIOError) or exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK)


class IndexLock:
    """Исключительная блокировка индекса между процессами на время чтения или записи. `locked` — удалось ли её взять:
    False означает, что файловая система блокировки не поддерживает (тогда работаем без неё)."""

    _warned_unsupported = False

    def __init__(self, target: Path, timeout: float | None = None):
        self.path = Path(target).with_name(Path(target).name + ".lock")
        self.timeout = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
        self.locked = False
        self._fh = None

    def __enter__(self) -> "IndexLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                _try_lock(self._fh)
                self.locked = True
                return self
            except OSError as exc:
                if not _is_busy(exc) and exc.errno in _UNSUPPORTED_LOCK_ERRNOS:
                    if not IndexLock._warned_unsupported:
                        IndexLock._warned_unsupported = True
                        logger.warning("Файловая система не поддерживает блокировку %s (%s): индекс защищён только проверкой "
                                       "поколения", self.path, exc)
                    return self
                if not _is_busy(exc):
                    self._close()
                    raise
                if time.monotonic() >= deadline:
                    self._close()
                    raise IndexBusyError(f"Индекс занят другим процессом дольше {self.timeout:.0f} с ({self.path}). "
                                         f"Повторите позже; если не помогает — перезапустите воркер.") from exc
                time.sleep(0.05)

    def __exit__(self, *exc_info) -> None:
        if self.locked:
            try:
                _unlock(self._fh)
            except OSError:
                pass
        self._close()

    def _close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
