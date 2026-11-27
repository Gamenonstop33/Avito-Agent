"""Разовый перенос последних постов открытой стены VK в наше сообщество (без согласования).

Постоянный автопостинг с согласованием — сервис services/content. Пересказ и загрузка фото — общие с ним.
Запуск: python scripts/vk_copy_posts.py <паблик> [--count 10] [--limit N] [--dry] [--fresh]
  --count  сколько последних постов источника взять (закреплённый не считается)
  --limit  опубликовать не больше N (идём от старых к новым)
  --dry    только показать тексты, ничего не публиковать (тексты запоминаются — публикация возьмёт их же)
  --fresh  пересказать заново, не брать запомненные тексты
Перенесённые посты, которые ещё есть на нашей стене, пропускаются (data/vk_posted.json). Секреты не выводит.
"""
import argparse
import asyncio
import json
import ssl
import sys
from pathlib import Path

import certifi
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.content import vk_wall  # noqa: E402
from services.content.logic import skip_reason  # noqa: E402
from services.content.vk_token import NeedLogin, ensure_token  # noqa: E402
from shared.config import DATA_DIR, env  # noqa: E402
from shared.llm import LLM  # noqa: E402
from shared.vk import VK, VKError  # noqa: E402

POSTED = DATA_DIR / "vk_posted.json"
TEXTS = DATA_DIR / "vk_rewrites.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def save(path: Path, d: dict) -> None:
    path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()

    try:
        user_token = "" if a.dry else await ensure_token()  # токен живёт 24 ч — обновляется сам
    except NeedLogin as e:
        sys.exit(str(e))
    reader, poster, user = VK(env("VK_SERVICE_KEY"), 0), VK(env("VK_POST_TOKEN"), 0), VK(user_token, 0)
    http = httpx.AsyncClient(timeout=60, verify=ssl.create_default_context(cafile=certifi.where()))
    llm = LLM()
    try:
        group_id = (await poster.call("groups.getById"))["groups"][0]["id"]
        domain = a.source.rstrip("/").rsplit("/", 1)[-1]
        items = (await reader.call("wall.get", domain=domain, count=a.count + 1))["items"]
        posts = [p for p in items if not p.get("is_pinned")][:a.count][::-1]  # от старых к новым
        alive = {p["id"] for p in (await reader.call("wall.get", owner_id=-group_id, count=100))["items"]}
        posted, texts = load(POSTED), load(TEXTS)
        done = 0
        for p in posts:
            key, title = f"{p['owner_id']}_{p['id']}", p["text"].split("\n")[0][:70]
            if posted.get(key) in alive and not (a.dry and a.fresh):
                continue
            if a.limit and done >= a.limit:
                break
            if skip_reason(p):
                print(f"\n=== {key} ПРОПУЩЕН: {skip_reason(p)} | {title}")
                continue
            if a.fresh or not isinstance(texts.get(key), dict):
                texts[key] = await vk_wall.rewrite(llm, p)
                save(TEXTS, texts)
            if "skip" in texts[key]:
                print(f"\n=== {key} ПРОПУЩЕН: {texts[key]['skip']} | {title}")
                continue
            urls = vk_wall.photo_urls(p)
            print(f"\n=== {key} | фото: {len(urls)} | исходный заголовок: {title}")
            if texts[key].get("ad"):
                print(f"⚠ Возможна реклама: {texts[key]['ad']}")
            print(texts[key]["text"])
            done += 1
            if a.dry:
                continue
            posted[key] = await vk_wall.publish(user if env("VK_USER_POSTS") == "1" else poster, user, http,
                                                group_id, texts[key]["text"], urls)
            save(POSTED, posted)
            print(f"→ https://vk.com/wall-{group_id}_{posted[key]}")
            await asyncio.sleep(3)
        print(f"\nГотово: {done}" + (" (без публикации)" if a.dry else ""))
    except (VKError, RuntimeError) as e:
        sys.exit(f"Ошибка VK: {e}")
    finally:
        for c in (reader, poster, user, llm):
            await c.close()
        await http.aclose()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
