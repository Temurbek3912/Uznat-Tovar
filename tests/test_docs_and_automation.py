"""Документация и файлы автоматизации не должны расходиться с кодом: каждая описанная команда и флаг существуют."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
# HANDOFF — журнал сессий: в нём упоминаются и удалённые/переименованные вещи, он не инструкция к запуску
INSTRUCTION_DOCS = [p for p in DOCS if p.name != "HANDOFF.md"]


def test_every_documented_scripts_module_exists():
    """`python -m scripts.xxx` в инструкциях обязан указывать на реальный файл — опечатка в команде не должна дожить до пользователя."""
    missing = []
    for doc in INSTRUCTION_DOCS:
        for name in set(re.findall(r"python -m scripts\.([a-z_]+)", doc.read_text(encoding="utf-8"))):
            if not (ROOT / "scripts" / f"{name}.py").is_file():
                missing.append(f"{doc.name}: scripts.{name}")
    assert missing == []


def test_documented_script_paths_exist():
    missing = []
    for doc in INSTRUCTION_DOCS:
        for rel in set(re.findall(r"(?<![\w/.])scripts[\\/]([a-z_]+\.(?:py|bat))", doc.read_text(encoding="utf-8"))):
            if not (ROOT / "scripts" / rel).is_file():
                missing.append(f"{doc.name}: scripts/{rel}")
    assert missing == []


def test_documented_env_variables_exist_in_env_example():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for var in ("APP_TIMEZONE",):
        assert re.search(rf"^#?{var}=", example, re.M), var


def test_removed_audit_tools_are_not_mentioned_as_commands_in_the_env_example_or_compose():
    """Камеры и агент убраны: подсказок «как запустить агента камер» в настройках быть не должно."""
    for name in (".env.example", "docker-compose.yml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "camera_agent.py" not in text and "CAMERA_AGENT_PASSWORD" not in text, name


def test_down_dash_v_is_only_ever_mentioned_as_a_warning_never_as_a_fix():
    """Пользователь запретил удалять тома без его явного согласия: инструкции и сообщения скриптов не должны советовать `down -v`."""
    warnings = ("Не используйте", "УДАЛЯЕТ", "Удаляется", "Не выполняйте", "не выполняйте")
    offenders = []
    sources = [*INSTRUCTION_DOCS, ROOT / "docker-compose.yml", *sorted((ROOT / "scripts").glob("*.py"))]
    for path in sources:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "down -v" in line and not any(w in line for w in warnings) and "Удаляется `down -v`" not in line:
                offenders.append(f"{path.name}:{number}: {line.strip()[:100]}")
    assert offenders == []
