"""Собирает .env.new по структуре .env.example, подставляя значения из текущего .env.

Запуск: python scripts/env_sync.py
Старый .env не меняется. Значения не выводит — только имена переменных.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def parse(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main() -> None:
    old = parse(ROOT / ".env")
    lines, seen, empty = [], set(), []
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, default = (x.strip() for x in line.split("=", 1))
            seen.add(k)
            line = f"{k}={old.get(k) or default}"
            if not (old.get(k) or default):
                empty.append(k)
        if line.startswith("# Шаблон настроек"):
            line = "# Настройки проекта. Секреты — только здесь, файл не попадает в git."
        lines.append(line)
    extra = [k for k in old if k not in seen]
    if extra:
        lines += ["", "", "# ───────── Нет в шаблоне (проверить, нужны ли) ─────────", ""]
        lines += [f"{k}={old[k]}" for k in extra]
    (ROOT / ".env.new").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Записан .env.new: переменных {len(seen) + len(extra)}, перенесено значений {len(seen) - len(empty)}")
    print("Пустые:", ", ".join(empty) or "нет")
    print("Нет в шаблоне:", ", ".join(extra) or "нет")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
