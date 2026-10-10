"""Показывает последний пост открытой стены VK (проверка сервисного ключа).

Запуск: python scripts/vk_last_post.py <короткое имя или ссылка на паблик>
Секреты не выводит.
"""
import json
import ssl
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import certifi

SSL = ssl.create_default_context(cafile=certifi.where())


def load_env() -> dict:
    env = {}
    for line in (Path(__file__).parent.parent / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    key = load_env().get("VK_SERVICE_KEY")
    if not key:
        sys.exit("В .env нет VK_SERVICE_KEY")
    name = sys.argv[1].rstrip("/").rsplit("/", 1)[-1]
    # public123 / club123 → числовой owner_id, иначе короткое имя
    num = next((name[len(p):] for p in ("public", "club") if name.startswith(p) and name[len(p):].isdigit()), None)
    target = {"owner_id": f"-{num}"} if num else {"domain": name}
    body = urllib.parse.urlencode({**target, "count": 2, "v": "5.199", "access_token": key}).encode()
    with urllib.request.urlopen("https://api.vk.com/method/wall.get", data=body, context=SSL, timeout=30) as r:
        d = json.load(r)
    if "error" in d:
        sys.exit(f"Ошибка VK {d['error'].get('error_code')}: {d['error'].get('error_msg')}")
    items = d["response"]["items"]
    posts = [p for p in items if not p.get("is_pinned")] or items  # закреплённый — не последний
    if not posts:
        sys.exit("Стена пустая")
    p = posts[0]
    kinds = ", ".join(a["type"] for a in p.get("attachments", [])) or "нет"
    print(f"https://vk.com/wall{p['owner_id']}_{p['id']}")
    print(f"Дата: {datetime.fromtimestamp(p['date']):%d.%m.%Y %H:%M}   Вложения: {kinds}")
    print(f"Лайки: {p.get('likes', {}).get('count', 0)}   Просмотры: {p.get('views', {}).get('count', 0)}")
    print("-" * 40)
    print(p.get("text") or "(без текста)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
