"""
Печатает новый ключ шифрования. Ничего не записывает на диск и не меняет.

    docker compose exec api python -m scripts.generate_secret_key           # CAMERA_SECRET_KEY (секреты камер, наследие)
    docker compose exec api python -m scripts.generate_secret_key --auth    # AUTH_SECRET_KEY (второй фактор входа, документ 18)

Вставьте строку CAMERA_SECRET_KEY=<ключ> в файл .env и выполните `docker compose up -d`. Сохраните ключ отдельно
от базы: без него зашифрованные адреса камер не расшифровать. Не публикуйте ключ и не добавляйте .env в git.
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from app.core.crypto import generate_key
    name = "AUTH_SECRET_KEY" if "--auth" in (sys.argv[1:] if argv is None else argv) else "CAMERA_SECRET_KEY"
    print(f"{name}={generate_key()}")
    print("\nДобавьте эту строку в файл .env, затем: docker compose up -d", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
