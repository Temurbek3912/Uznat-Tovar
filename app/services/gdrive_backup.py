"""
Резервная копия магазина в Google Диск напрямую (облегчённый режим, 2026-11-16): без программы «Google Диск для компьютера».

Подключение (в «Настройках» магазина, кнопка «Подключить Google Диск»): вход по коду — так Google называет «вход на
устройствах с ограниченным вводом». Программа получает у Google короткий код и показывает его вместе с адресом
google.com/device; администратор открывает этот адрес на любом устройстве (компьютер магазина или свой телефон), вводит код
и входит в Google-аккаунт магазина. Код работает и когда «Настройки» открыты с телефона через интернет — адрес возврата
не нужен.

Право — только drive.file: программа видит лишь файлы, которые создала сама, чужие файлы на Диске ей недоступны.
Ключ доступа Google (refresh token) хранится в `<media>/backup_settings.json` зашифрованным ключом AUTH_SECRET_KEY
(app/core/auth_secrets.py) и никуда не отдаётся; в ответах API и журналах его нет.

Регистрация программы у Google делается ОДИН раз продавцом (владельцем программы), не магазином: клиент OAuth типа
«TVs and Limited Input devices» в Google Cloud (docs/GOOGLE_DRIVE_SETUP.md). Его данные — файл `google_client.json`
в папке программы (скачанный из Google Cloud как есть) или строки GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET в .env.
Google считает секрет такого клиента не секретным (он неизбежно лежит в программе у пользователей), поэтому файл едет
в архиве программы (scripts/make_package.py), но в git не хранится (.gitignore).

Устройство на Диске (как и в папке на компьютере, app/services/lite_backup.py):

    Узнать товар — копии/
      photos/<sha256>            ← каждое фото один раз на все копии: в облако каждый день уходят только новые фото и база
      kopiya-2026-11-16_030000/  ← manifest.json, shop.db.gz, photos.list, keys.env, index/…

Загрузка идёт после готовой копии на компьютере, отдельно, в фоне: сначала новые фото, потом папка копии под именем
«….part», после проверки (MD5, который возвращает Google) — переименование. Обрыв связи: каждый запрос повторяется до
5 раз с паузой, большой файл догружается с места обрыва; не вышло — следующая попытка через час и продолжает с того, что
уже загружено. Хранится KEEP_CLOUD копий; фото, на которые не ссылается ни одна оставшаяся копия, удаляются.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SCOPES = "https://www.googleapis.com/auth/drive.file openid email"
FOLDER_MIME = "application/vnd.google-apps.folder"
CLIENT_FILE = "google_client.json"
STATE_FILE = "gdrive_state.json"
SMALL_UPLOAD = 5 * 1024 * 1024          # меньше — одним запросом, больше — по частям с догрузкой
CHUNK = 8 * 1024 * 1024                 # часть большой загрузки (кратна 256 КБ, как требует Google)
ATTEMPTS = 5
TOKEN_LABEL = b"uznat:backup-google:v1"


def _bases() -> dict:
    """Адреса Google. GOOGLE_API_BASE — только для проверок на копии программы (подставной сервер), в магазине пусто."""
    base = (os.environ.get("GOOGLE_API_BASE") or "").rstrip("/")
    if base:
        return {"device": base + "/device/code", "token": base + "/token", "revoke": base + "/revoke",
                "api": base + "/drive/v3", "upload": base + "/upload/drive/v3"}
    return {"device": "https://oauth2.googleapis.com/device/code", "token": "https://oauth2.googleapis.com/token",
            "revoke": "https://oauth2.googleapis.com/revoke", "api": "https://www.googleapis.com/drive/v3",
            "upload": "https://www.googleapis.com/upload/drive/v3"}


class CloudError(RuntimeError):
    """Облако недоступно или ответило ошибкой. Текст — для администратора магазина (без ключей)."""


class AccessLost(CloudError):
    """Google больше не принимает ключ доступа (отключили доступ, сменили пароль, прошёл срок) — подключить заново."""


# ----------------------------------------------------------------------------------------------------------- клиент программы

def load_client(root: Path, settings=None) -> tuple[str, str] | None:
    """(client_id, client_secret) программы у Google: .env (GOOGLE_CLIENT_ID/SECRET) или файл google_client.json."""
    cid = (getattr(settings, "google_client_id", "") or "").strip()
    secret = (getattr(settings, "google_client_secret", "") or "").strip()
    if cid and secret:
        return cid, secret
    path = root / CLIENT_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for part in [data, *(v for v in data.values() if isinstance(v, dict))]:      # {"installed": {...}} как скачан
        if part.get("client_id") and part.get("client_secret"):
            return str(part["client_id"]), str(part["client_secret"])
    return None


def _encrypt(text: str, key: str) -> str:
    from app.core.auth_secrets import encrypt_text
    return encrypt_text(text, key, TOKEN_LABEL)


def _decrypt(token: str, key: str) -> str:
    from app.core.auth_secrets import decrypt_text
    return decrypt_text(token, key, TOKEN_LABEL)


def rekey_token(google: dict, old_key: str, new_key: str) -> dict:
    """Перешифровать ключ Google новым AUTH_SECRET_KEY (после восстановления копии ключи в .env берутся из копии)."""
    if not google.get("token") or old_key == new_key:
        return google
    try:
        plain = _decrypt(google["token"], old_key)
    except Exception:  # noqa: BLE001 — не расшифровался: оставить как есть, в «Настройках» попросят подключить заново
        return google
    return dict(google, token=_encrypt(plain, new_key))


def _email_from_id_token(id_token: str) -> str:
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload.encode("ascii"))).get("email") or "")
    except (IndexError, ValueError, UnicodeError):
        return ""


def _q(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _md5(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 — сверка с MD5, который считает Google, не защита
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------------------------------------- вход по коду

def device_start(http, client: tuple[str, str]) -> dict:
    """Код для входа: {device_code, user_code, verification_url, expires_in, interval}."""
    try:
        r = http.post(_bases()["device"], data={"client_id": client[0], "scope": SCOPES})
    except Exception as exc:  # noqa: BLE001
        raise CloudError("Нет связи с Google. Проверьте интернет и повторите.") from exc
    data = _json(r)
    if r.status_code != 200 or not data.get("device_code"):
        raise CloudError(_google_error(data, "Google не выдал код для входа"))
    return data


def device_poll(http, client: tuple[str, str], device_code: str) -> dict:
    """Ответ Google на опрос: {"refresh_token", "id_token", …} — вошли; {"error": "authorization_pending"|…} — ещё нет."""
    r = http.post(_bases()["token"], data={"client_id": client[0], "client_secret": client[1], "device_code": device_code,
                                           "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    return _json(r)


def _json(r) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _google_error(data: dict, prefix: str) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err.get("status")
    return f"{prefix}: {str(err or 'неизвестная ошибка')[:160]}"


def console_sign_in(http, client: tuple[str, str], *, out=print, sleep=time.sleep) -> dict:
    """Вход по коду в окне восстановления (scripts/lite_restore.py, новый компьютер). -> ответ Google с refresh_token."""
    data = device_start(http, client)
    url = data.get("verification_url") or data.get("verification_uri") or "https://www.google.com/device"
    out(f"\nОткройте на любом устройстве (компьютер или телефон) адрес:  {url}\nи введите код:  {data['user_code']}\n"
        "Войдите в Google-аккаунт магазина и разрешите доступ. Жду…")
    interval = max(int(data.get("interval") or 5), 1)
    deadline = time.monotonic() + int(data.get("expires_in") or 1800)
    while time.monotonic() < deadline:
        sleep(interval)
        try:
            answer = device_poll(http, client, data["device_code"])
        except Exception:  # noqa: BLE001
            continue
        err = answer.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err or not answer.get("refresh_token"):
            raise CloudError("Вход в Google не состоялся" + (f" ({err})" if err else "") + ".")
        return answer
    raise CloudError("Код устарел — запустите восстановление ещё раз.")


# ----------------------------------------------------------------------------------------------------------- Google Диск

class Drive:
    """Нужные программе запросы Google Drive API v3 с повтором при обрыве и обновлением ключа доступа."""

    def __init__(self, http, client: tuple[str, str], refresh_token: str, *, sleep=time.sleep, attempts: int = ATTEMPTS):
        self.http = http
        self.client = client
        self.refresh_token = refresh_token
        self.sleep = sleep
        self.attempts = attempts
        self._access = ""
        self._expires = 0.0
        b = _bases()
        self.api, self.upload = b["api"], b["upload"]

    # ----- ключ доступа
    def _token(self, force: bool = False) -> str:
        if self._access and not force and time.monotonic() < self._expires - 60:
            return self._access
        for attempt in range(self.attempts):
            try:
                r = self.http.post(_bases()["token"], data={"client_id": self.client[0], "client_secret": self.client[1],
                                                            "refresh_token": self.refresh_token,
                                                            "grant_type": "refresh_token"})
            except Exception:  # noqa: BLE001 — нет связи: подождать и повторить
                self._pause(attempt)
                continue
            data = _json(r)
            if r.status_code == 200 and data.get("access_token"):
                self._access = str(data["access_token"])
                self._expires = time.monotonic() + float(data.get("expires_in") or 3600)
                return self._access
            if data.get("error") in ("invalid_grant", "unauthorized_client", "invalid_client"):
                raise AccessLost("Google больше не принимает доступ программы — подключите Google Диск заново в «Настройках».")
            self._pause(attempt)
        raise CloudError("Нет связи с Google — копия в облако отправится при следующей попытке.")

    def _pause(self, attempt: int) -> None:
        if attempt < self.attempts - 1:
            self.sleep(min(2 ** attempt, 30))

    def call(self, method: str, url: str, *, ok=(200,), **kw):
        """Запрос с повтором (обрыв, 429, 5xx) и одним обновлением ключа на 401."""
        refreshed = False
        last = "нет связи"
        for attempt in range(self.attempts):
            headers = dict(kw.get("headers") or {})
            headers["Authorization"] = "Bearer " + self._token()
            args = {k: v for k, v in kw.items() if k != "headers"}
            try:
                r = self.http.request(method, url, headers=headers, **args)
            except Exception as exc:  # noqa: BLE001
                last = type(exc).__name__
                self._pause(attempt)
                continue
            if r.status_code in ok:
                return r
            if r.status_code == 401 and not refreshed:
                refreshed = True
                self._token(force=True)
                continue
            if r.status_code in (429, 500, 502, 503, 504) or (r.status_code == 403 and "rate" in r.text.lower()):
                last = f"Google ответил {r.status_code}"
                self._pause(attempt)
                continue
            if r.status_code == 403 and "storageQuotaExceeded" in r.text:
                raise CloudError("На Google Диске закончилось место — освободите место или купите больше.")
            raise CloudError(_google_error(_json(r), f"Google ответил {r.status_code}"))
        raise CloudError(f"Google Диск недоступен ({last}) — повторю позже.")

    # ----- файлы
    def list(self, q: str, fields: str = "id,name,size,mimeType,md5Checksum") -> list[dict]:
        out, page = [], None
        while True:
            params = {"q": q, "fields": f"nextPageToken,files({fields})", "pageSize": 1000, "spaces": "drive"}
            if page:
                params["pageToken"] = page
            data = _json(self.call("GET", self.api + "/files", params=params))
            out.extend(data.get("files") or [])
            page = data.get("nextPageToken")
            if not page:
                return out

    def children(self, parent: str, *, folders: bool | None = None, name: str | None = None) -> list[dict]:
        q = f"'{_q(parent)}' in parents and trashed=false"
        if folders is True:
            q += f" and mimeType='{FOLDER_MIME}'"
        elif folders is False:
            q += f" and mimeType!='{FOLDER_MIME}'"
        if name is not None:
            q += f" and name='{_q(name)}'"
        return self.list(q)

    def folder(self, name: str, parent: str = "root", *, create: bool = True) -> str | None:
        found = self.children(parent, folders=True, name=name)
        if found:
            return found[0]["id"]
        if not create:
            return None
        return _json(self.call("POST", self.api + "/files", params={"fields": "id"},
                               json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]}))["id"]

    def rename(self, file_id: str, name: str) -> None:
        self.call("PATCH", f"{self.api}/files/{file_id}", params={"fields": "id"}, json={"name": name})

    def delete(self, file_id: str) -> None:
        self.call("DELETE", f"{self.api}/files/{file_id}", ok=(200, 204, 404))

    def put_file(self, path: Path, name: str, parent: str) -> dict:
        """Загрузить файл и сверить MD5 с тем, что посчитал Google. -> {id, name, size, md5Checksum}."""
        size = path.stat().st_size
        meta = {"name": name, "parents": [parent]}
        fields = "id,name,size,md5Checksum"
        if size <= SMALL_UPLOAD:
            boundary = "uznat" + uuid.uuid4().hex
            body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                    f"{json.dumps(meta, ensure_ascii=False)}\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n"
                    ).encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("ascii")
            info = _json(self.call("POST", self.upload + "/files", params={"uploadType": "multipart", "fields": fields},
                                   content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"}))
        else:
            info = self._resumable(path, size, meta, fields)
        if info.get("md5Checksum") and info["md5Checksum"] != _md5(path):
            self.delete(info.get("id", ""))
            raise CloudError(f"Файл {name} дошёл до Google Диска с ошибкой — повторю позже.")
        return info

    def _resumable(self, path: Path, size: int, meta: dict, fields: str) -> dict:
        r = self.call("POST", self.upload + "/files", params={"uploadType": "resumable", "fields": fields}, json=meta,
                      headers={"X-Upload-Content-Type": "application/octet-stream", "X-Upload-Content-Length": str(size)})
        session = r.headers.get("Location") or r.headers.get("location")
        if not session:
            raise CloudError("Google не начал загрузку большого файла — повторю позже.")
        offset, failures = 0, 0
        with open(path, "rb") as f:
            while True:
                if failures >= self.attempts:
                    raise CloudError("Связь с Google Диском всё время рвётся — догружу при следующей попытке.")
                f.seek(offset)
                chunk = f.read(CHUNK)
                end = offset + len(chunk) - 1
                try:
                    r = self.http.request("PUT", session, content=chunk,
                                          headers={"Content-Range": f"bytes {offset}-{end}/{size}",
                                                   "Authorization": "Bearer " + self._token()})
                except Exception:  # noqa: BLE001 — обрыв: спросить у Google, сколько дошло, и продолжить оттуда
                    failures += 1
                    self._pause(failures - 1)
                    offset = self._received(session, size, offset)
                    continue
                if r.status_code in (200, 201):
                    return _json(r)
                if r.status_code == 308:
                    offset = _range_end(r.headers.get("Range") or r.headers.get("range"))
                    failures = 0
                    continue
                if r.status_code in (404, 410):
                    raise CloudError("Google прервал загрузку большого файла — начну заново при следующей попытке.")
                failures += 1
                self._pause(failures - 1)
                offset = self._received(session, size, offset)

    def _received(self, session: str, size: int, fallback: int) -> int:
        try:
            r = self.http.request("PUT", session, content=b"", headers={"Content-Range": f"bytes */{size}",
                                                                         "Authorization": "Bearer " + self._token()})
        except Exception:  # noqa: BLE001
            return fallback
        if r.status_code == 308:
            return _range_end(r.headers.get("Range") or r.headers.get("range"))
        return fallback

    def get_file(self, file_id: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        r = self.call("GET", f"{self.api}/files/{file_id}", params={"alt": "media"})
        tmp = dst.with_name(dst.name + ".dl")
        tmp.write_bytes(r.content)
        os.replace(tmp, dst)

    def get_text(self, file_id: str) -> str:
        return self.call("GET", f"{self.api}/files/{file_id}", params={"alt": "media"}).content.decode("utf-8")

    def revoke(self) -> None:
        try:
            self.http.post(_bases()["revoke"], data={"token": self.refresh_token})
        except Exception:  # noqa: BLE001 — отключение и без отзыва: ключ всё равно стирается у нас
            pass


def _range_end(value: str | None) -> int:
    """«bytes=0-1048575» -> 1048576 (сколько байт Google уже принял)."""
    if not value or "-" not in value:
        return 0
    try:
        return int(value.rsplit("-", 1)[1]) + 1
    except ValueError:
        return 0


# ----------------------------------------------------------------------------------------------------------- копии в облаке

def _local_photo(store: Path, sha: str) -> Path:
    return store / sha[:2] / sha


def upload_copy(drive: Drive, local_root: Path, name: str, *, folder_name: str, keep: int, progress=None) -> dict:
    """Отправить готовую копию name из папки local_root (папка «Узнать товар — копии» на компьютере) в Google Диск."""
    from app.services import lite_backup as B

    copy_dir = local_root / name
    photos = B.read_photos_list(copy_dir / B.PHOTOS_LIST)
    root_id = drive.folder(folder_name)
    photos_id = drive.folder(B.PHOTOS_DIR, root_id)
    have = {f["name"]: int(f.get("size") or -1) for f in drive.children(photos_id, folders=False)}
    need = []
    seen = set()
    for p in photos:
        if p.sha256 in seen or have.get(p.sha256) == p.size:
            continue
        seen.add(p.sha256)
        need.append(p)
    copies = {f["name"]: f["id"] for f in drive.children(root_id, folders=True)}
    files = [p for p in sorted(copy_dir.rglob("*")) if p.is_file()]
    total = len(need) + (0 if name in copies else len(files))
    done = 0
    if progress:
        progress(done, total)
    store = local_root / B.PHOTOS_DIR
    for p in need:
        src = _local_photo(store, p.sha256)
        drive.put_file(src, p.sha256, photos_id)
        done += 1
        if progress:
            progress(done, total)
    if name not in copies:
        part_name = name + B.PART_SUFFIX
        if part_name in copies:                       # прошлая попытка оборвалась на середине — начать папку заново
            drive.delete(copies[part_name])
        part_id = drive.folder(part_name, root_id)
        sub_ids: dict[str, str] = {}
        for f in files:
            rel = f.relative_to(copy_dir)
            parent = part_id
            if len(rel.parts) > 1:
                sub = rel.parts[0]
                if sub not in sub_ids:
                    sub_ids[sub] = drive.folder(sub, part_id)
                parent = sub_ids[sub]
            drive.put_file(f, rel.name, parent)
            done += 1
            if progress:
                progress(done, total)
        drive.rename(part_id, name)
    removed = prune_cloud(drive, root_id, photos_id, keep=keep, current=name)
    return {"copy": name, "photos_uploaded": len(need), **removed}


def _copy_folders(drive: Drive, root_id: str) -> list[dict]:
    from app.services import lite_backup as B
    return sorted((f for f in drive.children(root_id, folders=True) if B._COPY_RE.match(f["name"])),
                  key=lambda f: f["name"], reverse=True)


def prune_cloud(drive: Drive, root_id: str, photos_id: str, *, keep: int, current: str = "") -> dict:
    """Оставить keep новых копий; удалить брошенные «.part» и фото, на которые не ссылается ни одна оставшаяся копия."""
    from app.services import lite_backup as B

    folders = drive.children(root_id, folders=True)
    removed_copies = 0
    for f in folders:
        n = f["name"]
        if n.endswith(B.PART_SUFFIX) and B._COPY_RE.match(n[:-len(B.PART_SUFFIX)]) and n != current + B.PART_SUFFIX:
            drive.delete(f["id"])
    copies = _copy_folders(drive, root_id)
    for old in copies[keep:]:
        drive.delete(old["id"])
        removed_copies += 1
    referenced: set[str] = set()
    for c in copies[:keep]:
        lst = drive.children(c["id"], folders=False, name=B.PHOTOS_LIST)
        if not lst:
            return {"removed_copies": removed_copies, "removed_photos": 0}      # список не нашёлся — фото не трогаем
        try:
            text = drive.get_text(lst[0]["id"])
        except CloudError:
            return {"removed_copies": removed_copies, "removed_photos": 0}
        referenced.update(line.split("\t", 1)[0] for line in text.splitlines() if line.strip())
    removed_photos = 0
    for f in drive.children(photos_id, folders=False):
        if B._SHA_RE.match(f["name"]) and f["name"] not in referenced:
            drive.delete(f["id"])
            removed_photos += 1
    return {"removed_copies": removed_copies, "removed_photos": removed_photos}


def list_cloud_copies(drive: Drive, folder_name: str) -> list[dict]:
    """Копии в облаке, новые первыми: [{name, id, created_at, counts}]."""
    root_id = drive.folder(folder_name, create=False)
    if not root_id:
        return []
    out = []
    for c in _copy_folders(drive, root_id):
        m = drive.children(c["id"], folders=False, name="manifest.json")
        info = {"name": c["name"], "id": c["id"], "created_at": "", "counts": {}}
        if m:
            try:
                data = json.loads(drive.get_text(m[0]["id"]))
                info["created_at"] = str(data.get("created_at") or "")
                info["counts"] = dict(data.get("counts") or {})
            except (CloudError, ValueError):
                pass
        out.append(info)
    return out


def download_copy(drive: Drive, name: str, local_root: Path, *, folder_name: str, progress=None) -> Path:
    """Скачать копию name из облака в local_root (папка копий на компьютере): файлы копии и недостающие фото.
    Фото, которые уже есть на компьютере (то же SHA-256), не скачиваются. -> папка копии на компьютере."""
    from app.services import lite_backup as B

    root_id = drive.folder(folder_name, create=False)
    found = [c for c in (_copy_folders(drive, root_id) if root_id else []) if c["name"] == name]
    if not found:
        raise CloudError(f"В Google Диске нет копии {name}.")
    final = local_root / name
    if final.is_dir() and not B.verify_copy(final, full=True):
        return final
    part = local_root / (name + ".dl-part")
    if part.exists():
        import shutil
        shutil.rmtree(part)

    def fetch(folder_id: str, into: Path) -> None:
        for f in drive.children(folder_id):
            if f.get("mimeType") == FOLDER_MIME:
                fetch(f["id"], into / f["name"])
            else:
                drive.get_file(f["id"], into / f["name"])

    fetch(found[0]["id"], part)
    photos = B.read_photos_list(part / B.PHOTOS_LIST)
    store = local_root / B.PHOTOS_DIR
    photos_id = drive.folder(B.PHOTOS_DIR, root_id, create=False)
    remote = {f["name"]: f["id"] for f in drive.children(photos_id, folders=False)} if photos_id else {}
    missing = []
    for p in {p.sha256: p for p in photos}.values():
        target = _local_photo(store, p.sha256)
        if not (target.is_file() and target.stat().st_size == p.size and B.sha256_file(target) == p.sha256):
            missing.append(p)
    for i, p in enumerate(missing, 1):
        if p.sha256 not in remote:
            raise CloudError(f"В облаке нет фото {p.rel} — выберите другую копию.")
        target = _local_photo(store, p.sha256)
        drive.get_file(remote[p.sha256], target)
        if B.sha256_file(target) != p.sha256:
            target.unlink(missing_ok=True)
            raise CloudError(f"Фото {p.rel} скачалось с ошибкой — повторите.")
        if progress:
            progress(i, len(missing))
    if final.exists():
        import shutil
        shutil.rmtree(final)
    os.replace(part, final)
    return final


# ----------------------------------------------------------------------------------------------------------- служба

class GoogleBackup:
    """Подключение Google Диска магазина и отправка копий. Одна на процесс (app/services/lite_backup.py)."""

    def __init__(self, *, media_root: Path, client: tuple[str, str] | None, secret_key: str, http_factory=None,
                 sleep=time.sleep, clock=None):
        self.media_root = media_root
        self.client = client
        self.secret_key = secret_key
        self._http_factory = http_factory or _default_http
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._pending: dict | None = None          # идёт вход по коду: {user_code, verification_url, expires_at}
        self._connect_error = ""
        self._progress: dict | None = None
        self._cancel = threading.Event()
        self.on_connected = None                    # служба копий: сразу отправить последнюю копию (lite_backup)

    @property
    def available(self) -> bool:
        return self.client is not None

    @property
    def running(self) -> bool:
        return self._sync_lock.locked()

    # ----- хранение
    def _settings(self) -> dict:
        from app.services import lite_backup as B
        return dict(B.read_settings(self.media_root).get("google") or {})

    def _save(self, google: dict | None) -> None:
        from app.services import lite_backup as B

        def change(data: dict) -> dict:
            if google is None:
                data.pop("google", None)
            else:
                data["google"] = google
            return data
        B.update_settings(self.media_root, change)

    def refresh_token(self) -> str:
        g = self._settings()
        if not g.get("token") or g.get("lost"):
            return ""
        try:
            return _decrypt(g["token"], self.secret_key)
        except Exception:  # noqa: BLE001 — сменился AUTH_SECRET_KEY: подключить заново
            return ""

    @property
    def connected(self) -> bool:
        return self.available and bool(self.refresh_token())

    def state(self) -> dict:
        from app.services import lite_backup as B
        return B.read_json(self.media_root / STATE_FILE)

    def _write_state(self, state: dict) -> None:
        from app.services import lite_backup as B
        B.write_json(self.media_root / STATE_FILE, state)

    # ----- подключение
    def start_connect(self) -> dict:
        """Начать вход по коду. -> {user_code, verification_url, expires_at}. Ожидание входа — в фоне."""
        if not self.available:
            raise CloudError("Подключение Google Диска не настроено в этой версии программы — обратитесь к продавцу.")
        from app.core.auth_secrets import is_configured
        if not is_configured(self.secret_key):
            raise CloudError("Нет ключа AUTH_SECRET_KEY в .env — без него программа не может надёжно хранить доступ к Google.")
        with self._lock:
            http = self._http_factory()
            data = device_start(http, self.client)
            expires = self._clock() + timedelta(seconds=int(data.get("expires_in") or 1800))
            self._pending = {"user_code": str(data["user_code"]),
                             "verification_url": str(data.get("verification_url") or data.get("verification_uri") or
                                                     "https://www.google.com/device"),
                             "expires_at": expires.isoformat(timespec="seconds")}
            self._connect_error = ""
            pending = self._pending
        threading.Thread(target=self._wait_for_sign_in, args=(http, data, pending), name="gdrive-connect",
                         daemon=True).start()
        return dict(pending)

    def _wait_for_sign_in(self, http, data: dict, pending: dict) -> None:
        interval = max(int(data.get("interval") or 5), 1)
        deadline = time.monotonic() + int(data.get("expires_in") or 1800)
        try:
            while time.monotonic() < deadline and self._pending is pending:
                self._sleep(interval)
                try:
                    answer = device_poll(http, self.client, data["device_code"])
                except Exception:  # noqa: BLE001 — нет связи: попробовать ещё раз
                    continue
                err = answer.get("error")
                if err == "authorization_pending":
                    continue
                if err == "slow_down":
                    interval += 5
                    continue
                if err or not answer.get("refresh_token"):
                    with self._lock:
                        if self._pending is pending:
                            self._pending = None
                            # код для «Настроек» (bkp.g_err_*): отменён вход, код устарел, иначе отказ
                            self._connect_error = ("denied" if err == "access_denied"
                                                   else "expired" if err == "expired_token" else "failed")
                            logger.warning("Подключение Google Диска не удалось: %s", _google_error(answer, "Google"))
                    return
                self.finish_connect(answer)
                with self._lock:
                    if self._pending is pending:
                        self._pending = None
                if self.on_connected is not None:
                    try:
                        self.on_connected()
                    except Exception:  # noqa: BLE001
                        logger.exception("Не удалось начать отправку копии после подключения Google Диска")
                return
            with self._lock:
                if self._pending is pending:
                    self._pending = None
                    self._connect_error = "expired"
        finally:
            try:
                http.close()
            except Exception:  # noqa: BLE001
                pass

    def finish_connect(self, answer: dict, secret_key: str | None = None) -> None:
        """Сохранить ключ доступа из ответа Google (зашифрованным)."""
        self._save({"token": _encrypt(str(answer["refresh_token"]), secret_key or self.secret_key),
                    "email": _email_from_id_token(str(answer.get("id_token") or "")),
                    "connected_at": self._clock().isoformat(timespec="seconds")})
        self._write_state({})                       # новый аккаунт — прежние итоги не к нему

    def cancel_connect(self) -> None:
        with self._lock:
            self._pending = None

    def disconnect(self) -> None:
        token = self.refresh_token()
        self.cancel_connect()
        self._cancel.set()
        if token and self.available:
            http = self._http_factory()
            try:
                Drive(http, self.client, token, sleep=self._sleep).revoke()
            finally:
                http.close()
        self._save(None)
        self._write_state({})
        self._cancel.clear()

    def _mark_lost(self) -> None:
        g = self._settings()
        if g:
            g["lost"] = True
            self._save(g)

    # ----- отправка
    def drive(self, http) -> Drive:
        token = self.refresh_token()
        if not token:
            raise AccessLost("Google Диск не подключён.")
        return Drive(http, self.client, token, sleep=self._sleep)

    def due(self, newest: str) -> bool:
        """Отправить ли копию newest: ещё не в облаке, и после неудачной попытки прошёл час."""
        if not newest or not self.connected:
            return False
        st = self.state()
        if st.get("copy") == newest and not st.get("last_error"):
            return False
        last = st.get("last_attempt")
        try:
            if last and st.get("last_error") and self._clock() - datetime.fromisoformat(last) < timedelta(hours=1):
                return False
        except ValueError:
            pass
        return True

    def sync(self, local_root: Path, name: str, *, keep: int) -> dict:
        """Отправить копию name (из папки копий на компьютере). Одновременно — одна отправка."""
        if not self._sync_lock.acquire(blocking=False):
            raise CloudError("Копия уже отправляется в Google Диск.")
        st = self.state()
        st["last_attempt"] = self._clock().isoformat(timespec="seconds")
        self._progress = {"done": 0, "total": 0, "copy": name}
        http = self._http_factory()
        try:
            self._write_state(st)
            from app.services import lite_backup as B
            result = upload_copy(self.drive(http), local_root, name, folder_name=B.FOLDER_NAME, keep=keep,
                                 progress=self._on_progress)
            st.update(copy=name, last_ok=self._clock().isoformat(timespec="seconds"), last_error="",
                      photos_uploaded=result["photos_uploaded"])
            return result
        except AccessLost as exc:
            self._mark_lost()
            st["last_error"] = str(exc)
            raise
        except CloudError as exc:
            st["last_error"] = str(exc)[:300]
            raise
        except Exception as exc:  # noqa: BLE001 — неожиданное (диск, файл копии): записать и не ронять программу
            from app.services import lite_backup as B
            st["last_error"] = B._safe_error(exc)
            raise CloudError(st["last_error"]) from exc
        finally:
            try:
                http.close()
            except Exception:  # noqa: BLE001
                pass
            self._progress = None
            try:
                self._write_state(st)
            finally:
                self._sync_lock.release()

    def _on_progress(self, done: int, total: int) -> None:
        if self._progress is not None:
            self._progress.update(done=done, total=total)

    def status(self) -> dict:
        g = self._settings()
        st = self.state()
        pending = self._pending
        if not self.available:
            state = "unavailable"
        elif pending:
            state = "waiting"
        elif g.get("token") and (g.get("lost") or not self.refresh_token()):
            state = "lost"
        elif g.get("token"):
            state = "connected"
        else:
            state = "off"
        out = {"state": state, "email": g.get("email") or "", "connected_at": g.get("connected_at") or "",
               "running": self.running, "progress": dict(self._progress) if self._progress else None,
               "last_ok": st.get("last_ok") or "", "last_attempt": st.get("last_attempt") or "",
               "last_error": st.get("last_error") or "", "copy": st.get("copy") or "",
               "connect_error": self._connect_error}
        if pending:
            out.update(user_code=pending["user_code"], verification_url=pending["verification_url"],
                       expires_at=pending["expires_at"])
        return out


def _default_http():
    import httpx
    return httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)
