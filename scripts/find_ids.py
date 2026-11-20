"""Показывает ID чатов Avito и ID пользователей VK, написавших сообществу.

Запуск: python scripts/find_ids.py
Секреты не выводит.
"""
import json
import ssl
import urllib.parse
import urllib.request
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


def http(url: str, data: dict | None = None, token: str | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20, context=SSL) as r:
        return json.loads(r.read())


def avito(env: dict) -> None:
    print("=== Avito ===")
    token = http("https://api.avito.ru/token", {
        "grant_type": "client_credentials",
        "client_id": env["AVITO_CLIENT_ID"],
        "client_secret": env["AVITO_CLIENT_SECRET"],
    })["access_token"]
    me = http("https://api.avito.ru/core/v1/accounts/self", token=token)
    print(f"Аккаунт: {me.get('name')} (user_id={me['id']})")
    chats = http(f"https://api.avito.ru/messenger/v2/accounts/{me['id']}/chats?limit=15", token=token)
    for c in chats.get("chats", []):
        users = ", ".join(u.get("name", "?") for u in c.get("users", []) if u.get("id") != me["id"])
        item = (c.get("context", {}).get("value") or {}).get("title", "")
        text = ((c.get("last_message") or {}).get("content") or {}).get("text", "")[:50]
        print(f"- chat_id={c['id']} | {users} | {item} | «{text}»")


def vk(env: dict) -> None:
    print("\n=== VK (кто писал сообществу) ===")
    params = urllib.parse.urlencode({
        "access_token": env["VK_GROUP_TOKEN"], "v": "5.199", "count": 20, "extended": 1,
    })
    r = http(f"https://api.vk.com/method/messages.getConversations?{params}")
    if "error" in r:
        print("Ошибка VK:", r["error"].get("error_msg"))
        return
    names = {p["id"]: f"{p['first_name']} {p['last_name']}" for p in r["response"].get("profiles", [])}
    for it in r["response"]["items"]:
        pid = it["conversation"]["peer"]["id"]
        print(f"- vk_id={pid} | {names.get(pid, '?')}")


if __name__ == "__main__":
    env = load_env()
    for fn in (avito, vk):
        try:
            fn(env)
        except Exception as e:  # скрипт разовый, просто показываем, что упало
            print(f"{fn.__name__}: ошибка — {e}")
