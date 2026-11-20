"""Настройки из .env / переменных окружения."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_list(name: str) -> list[str]:
    return [x.strip() for x in env(name).split(",") if x.strip()]


DATA_DIR = ROOT / "data"
# Второй аккаунт Avito (28.10) — отдельная копия бота: DB_NS=2 → свои схемы в базе (shared/db.py), на сервере свои
# data2/ и knowledge2/ (docker-compose.prod.yml). Пусто — основной аккаунт
NS = env("DB_NS")
# Название магазина (профиль Avito) — в приветствии и промптах
STORE = env("STORE_NAME", "GameNonStop")
# Правила владельца и база знаний приходят с другого сервера (19.11, XBOXGARANT берёт у Макса раз в 15 мин —
# scripts/rules_sync.sh): здесь их не предлагаем, не правим и базу по ним не переписываем. Значение — чьи правила
RULES_FROM = env("RULES_FROM")
