"""
HTTPS в сети магазина без покупки домена и без сторонних служб (решение владельца 2026-11-08 21:53, документ 18, §14).

Создаёт в папке secrets/tls/ (не входит в git и в резервную копию):
  * ca.crt — «удостоверяющий центр магазина»: его один раз устанавливают на телефоны сотрудников (инструкция —
    docs/REMOTE_ACCESS_GUIDE.md, п. 7);
  * ca.key — его закрытый ключ (права только владельцу файла); нужен лишь для выпуска нового сертификата сервера;
  * server.crt, server.key — сертификат и ключ сервера для адресов, которые вы укажете (адрес компьютера в Wi-Fi, его имя).

Безопасность:
  * ЦЕНТР ОГРАНИЧЕН ИМЕНАМИ (X.509 Name Constraints, критичное расширение): он может подписывать только частные адреса
    (10/8, 172.16/12, 192.168/16, 100.64/10, 127/8, ::1) и перечисленные имена. Даже если ca.key украдут, им нельзя выдать
    сертификат для чужих сайтов (банк, почта): браузеры с поддержкой ограничений его не примут;
  * сертификат сервера — 397 дней (меньше предела Apple 398), ключи ECDSA P-256; продление — та же команда с --renew;
  * ничего не отправляется в сеть.

Запуск (в папке проекта):
    docker compose exec api python -m scripts.make_local_tls --ip 192.168.1.10 --name shop-pc
    docker compose exec api python -m scripts.make_local_tls --ip 192.168.1.10 --renew     # новый сертификат сервера, тот же центр
Затем: docker compose -f docker-compose.yml -f docker-compose.https.yml up -d
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PRIVATE_NETWORKS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8", "::1/128", "fc00::/7")
SERVER_DAYS = 397
CA_DAYS = 3650


def _x509():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    return x509, hashes, serialization, ec, ExtendedKeyUsageOID, NameOID


def parse_targets(ips: list[str], names: list[str]) -> tuple[list, list[str]]:
    """Проверяет адреса и имена. Адрес вне частных сетей — ошибка: такой сертификат центр магазина выдавать не должен."""
    out_ips = []
    for raw in ips:
        ip = ipaddress.ip_address(raw.strip())
        if not any(ip in ipaddress.ip_network(n) for n in PRIVATE_NETWORKS):
            raise ValueError(f"адрес {ip} не из частной сети — для него нужен настоящий сертификат, не центр магазина")
        out_ips.append(ip)
    out_names = []
    for raw in names:
        name = raw.strip().lower().rstrip(".")
        if not name or any(c in name for c in " /\\:*"):
            raise ValueError(f"имя «{raw}» не подходит")
        out_names.append(name)
    if "localhost" not in out_names:
        out_names.append("localhost")
    if not out_ips:
        raise ValueError("укажите хотя бы один адрес компьютера в сети магазина: --ip 192.168.1.10")
    return out_ips, out_names


def build_ca(names: list[str], now: datetime):
    x509, hashes, _ser, ec, _eku, NameOID = _x509()
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Uznat Tovar — store CA"),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Uznat Tovar (local)")])
    permitted = [x509.IPAddress(ipaddress.ip_network(n)) for n in PRIVATE_NETWORKS] + [x509.DNSName(n) for n in names]
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def build_server(ca_key, ca_cert, ips, names: list[str], now: datetime):
    x509, hashes, _ser, ec, ExtendedKeyUsageOID, NameOID = _x509()
    key = ec.generate_private_key(ec.SECP256R1())
    san = [x509.IPAddress(ip) for ip in ips] + [x509.DNSName(n) for n in names]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(ips[0]))]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=SERVER_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def _pem_key(key) -> bytes:
    _x, _h, serialization, *_ = _x509()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _pem_cert(cert) -> bytes:
    _x, _h, serialization, *_ = _x509()
    return cert.public_bytes(serialization.Encoding.PEM)


def _restrict_to_owner_windows(path: Path) -> None:
    """Windows: права 0600 там не действуют — файл наследует права папки (на диске D: часто «Прошедшие проверку: изменение»,
    то есть ключ читает любой пользователь компьютера). Убираем наследование и оставляем полный доступ только текущей учётной
    записи. Если ключ будет читать служба под другой учётной записью (будущий Desktop, документ 03) — выдать ей доступ отдельно.
    Не получилось — ошибка, а не молчание: закрытый ключ с открытыми правами оставлять нельзя."""
    import subprocess  # noqa: PLC0415
    user = os.environ.get("USERNAME") or os.getlogin()
    domain = os.environ.get("USERDOMAIN")
    account = f"{domain}\\{user}" if domain else user
    result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise OSError(f"не удалось ограничить доступ к {path.name} (icacls, код {result.returncode})")


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    if os.name == "nt":
        _restrict_to_owner_windows(path)
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def make(out_dir: Path, ips: list[str], names: list[str], *, renew: bool = False, now: datetime | None = None) -> dict:
    """Создаёт (или продлевает) файлы. -> {"ca": путь, "server": путь, "ips": [...], "names": [...], "not_after": дата}."""
    x509 = _x509()[0]
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    now = now or datetime.now(timezone.utc)
    ip_list, name_list = parse_targets(ips, names)
    out_dir.mkdir(parents=True, exist_ok=True)
    ca_crt, ca_key_path = out_dir / "ca.crt", out_dir / "ca.key"
    if renew:
        if not (ca_crt.exists() and ca_key_path.exists()):
            raise ValueError("для --renew нужны ca.crt и ca.key в этой папке; без них создайте всё заново (без --renew)")
        ca_cert = x509.load_pem_x509_certificate(ca_crt.read_bytes())
        ca_key = load_pem_private_key(ca_key_path.read_bytes(), password=None)
    else:
        if ca_crt.exists():
            raise ValueError("центр магазина уже создан: продлите сертификат сервера (--renew) или удалите папку и установите "
                             "новый ca.crt на все телефоны заново")
        ca_key, ca_cert = build_ca(name_list, now)
        ca_crt.write_bytes(_pem_cert(ca_cert))
        _write_private(ca_key_path, _pem_key(ca_key))
    key, cert = build_server(ca_key, ca_cert, ip_list, name_list, now)
    (out_dir / "server.crt").write_bytes(_pem_cert(cert) + _pem_cert(ca_cert))       # цепочка: сервер + центр
    _write_private(out_dir / "server.key", _pem_key(key))
    return {"ca": str(ca_crt), "server": str(out_dir / "server.crt"), "ips": [str(i) for i in ip_list], "names": name_list,
            "not_after": cert.not_valid_after_utc.strftime("%Y-%m-%d")}


def main(argv: list[str] | None = None, out=print) -> int:
    parser = argparse.ArgumentParser(description="HTTPS в сети магазина: свой центр сертификатов (только для частных адресов)")
    parser.add_argument("--ip", action="append", default=[], help="адрес компьютера в сети магазина (можно несколько раз)")
    parser.add_argument("--name", action="append", default=[], help="имя компьютера в сети (можно несколько раз)")
    parser.add_argument("--out", default=None, help="папка (по умолчанию secrets/tls в папке проекта)")
    parser.add_argument("--renew", action="store_true", help="выпустить новый сертификат сервера тем же центром")
    args = parser.parse_args(argv)
    out_dir = Path(args.out) if args.out else Path(__file__).resolve().parents[1] / "secrets" / "tls"
    try:
        info = make(out_dir, args.ip, args.name, renew=args.renew)
    except ValueError as exc:
        out(f"Ошибка: {exc}")
        return 1
    out(f"Готово: сертификат сервера для {', '.join(info['ips'] + info['names'])} — до {info['not_after']}.")
    out(f"Центр магазина: {info['ca']} — установите его на телефоны сотрудников (docs/REMOTE_ACCESS_GUIDE.md, п. 7).")
    out("Запуск с HTTPS: docker compose -f docker-compose.yml -f docker-compose.https.yml up -d")
    out(f"Адрес для сотрудников: https://{info['ips'][0]}:8000  (впишите его в .env: PUBLIC_HTTPS_URL=https://{info['ips'][0]}:8000)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
