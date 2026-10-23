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
