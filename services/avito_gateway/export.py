"""Выгрузка последних диалогов (для RAG), объявлений и кандидатов в быстрые ответы.

Запуск: python -m services.avito_gateway.export [--chats 300 | --all]
Результат в data/export/. Имена клиентов не сохраняем — только роль автора.
"""
import argparse
import asyncio
import json
import re
from collections import Counter
from pathlib import Path

from shared.avito import AvitoClient
from shared.config import DATA_DIR, env

PAUSE = 0.3  # между запросами, чтобы не упереться в лимиты


def clean_message(m: dict, me: int) -> dict:
    return {
        "id": m["id"],
        "created": m.get("created"),
        "role": "seller" if m.get("author_id") == me else ("system" if m.get("type") == "system" else "client"),
        "type": m.get("type"),
        "text": (m.get("content") or {}).get("text", ""),
    }


async def fetch_all_messages(av: AvitoClient, me: int, chat_id: str) -> list[dict]:
    out, offset = [], 0
    while True:
        batch = await av.messages(me, chat_id, limit=100, offset=offset)
        await asyncio.sleep(PAUSE)
        out += batch
        if len(batch) < 100:
            return list(reversed(out))  # от старых к новым
        offset += 100


def quick_reply_candidates(dialogs: list[dict], min_count: int = 3) -> list[tuple[str, int]]:
    """Одинаковые исходящие тексты, повторяющиеся в разных чатах, — скорее всего быстрые ответы."""
    cnt: Counter[str] = Counter()
    for d in dialogs:
        seen = {re.sub(r"\s+", " ", m["text"]).strip() for m in d["messages"] if m["role"] == "seller" and m["text"]}
        cnt.update(t for t in seen if len(t) > 20)
    return [(t, n) for t, n in cnt.most_common() if n >= min_count]


async def list_all_chats(av: AvitoClient, me: int) -> list[dict]:
    """Все чаты аккаунта. Общий список API отдаёт максимум ~1100, поэтому идём ещё по каждому объявлению
    (включая архивные и удалённые) и отдельно по личным перепискам u2u."""
    async def paged(**params) -> list[dict]:
        out = []
        for offset in range(0, 1001, 100):
            try:
                d = await av._req("GET", f"/messenger/v2/accounts/{me}/chats",
                                  params={"limit": 100, "offset": offset, **params})
            except Exception as e:
                print(f"  стоп на offset={offset} {params}: {str(e)[:80]}")
                break
            await asyncio.sleep(PAUSE)
            out += d.get("chats", [])
            if len(d.get("chats", [])) < 100:
                break
        return out

    item_ids = []
    for status in ("active", "old", "removed", "blocked", "rejected"):
        page = 1
        while True:
            res = (await av._req("GET", "/core/v1/items",
                                 params={"per_page": 100, "page": page, "status": status})).get("resources", [])
            item_ids += [i["id"] for i in res]
            if len(res) < 100:
                break
            page += 1
    chats: dict[str, dict] = {c["id"]: c for c in await paged(chat_types="u2i,u2u")}
    chats |= {c["id"]: c for c in await paged(chat_types="u2u")}
    for n, iid in enumerate(item_ids, 1):
        chats |= {c["id"]: c for c in await paged(item_ids=iid, chat_types="u2i")}
        if n % 25 == 0:
            print(f"  объявления {n}/{len(item_ids)}, чатов найдено {len(chats)}")
    return sorted(chats.values(), key=lambda c: c.get("updated", 0), reverse=True)


def save_jsonl(path: Path, rows) -> None:
    """Целиком и атомарно: обрыв не портит выгрузку, повторный запуск продолжит."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in rows), encoding="utf-8")
    tmp.replace(path)


async def main_all() -> None:
    """Выгрузка всех чатов в data/export/dialogs_all.jsonl; повторный запуск докачивает новые и изменившиеся."""
    av = AvitoClient(env("AVITO_CLIENT_ID"), env("AVITO_CLIENT_SECRET"))
    out = DATA_DIR / "export" / "dialogs_all.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        me = (await av.me())["id"]
        chats = await list_all_chats(av, me)
        # split("\n"), не splitlines(): тот режет и по U+2028 внутри текста клиента
        done = {d["chat_id"]: d for d in map(json.loads, filter(None, out.read_text(encoding="utf-8").split("\n")))} \
            if out.exists() else {}
        todo = [c for c in chats if c["id"] not in done or (c.get("updated") or 0) > (done[c["id"]].get("updated") or 0)]
        print(f"Всего чатов: {len(chats)}, уже выгружено: {len(done)}, качаю новые/изменённые: {len(todo)}", flush=True)
        for i, c in enumerate(todo, 1):
            item = (c.get("context") or {}).get("value") or {}
            msgs = [clean_message(m, me) for m in await fetch_all_messages(av, me, c["id"])]
            done[c["id"]] = {"chat_id": c["id"], "updated": c.get("updated"), "item_id": item.get("id"),
                             "item_title": item.get("title"), "item_price": item.get("price_string"), "messages": msgs}
            if i % 100 == 0:
                print(f"  {i}/{len(todo)}", flush=True)
            if i % 500 == 0:
                save_jsonl(out, done.values())
        save_jsonl(out, done.values())
        dialogs = list(done.values())
        qr = quick_reply_candidates(dialogs)
        (out.parent / "quick_replies.json").write_text(
            json.dumps([{"count": n, "text": t} for t, n in qr], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"Готово: {len(dialogs)} чатов, {sum(len(d['messages']) for d in dialogs)} сообщений, "
              f"шаблонов: {len(qr)}")
    finally:
        await av.close()


async def main(n_chats: int) -> None:
    av = AvitoClient(env("AVITO_CLIENT_ID"), env("AVITO_CLIENT_SECRET"))
    out_dir = DATA_DIR / "export"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        me = (await av.me())["id"]

        chats, offset = [], 0
        while len(chats) < n_chats:
            batch = await av.chats(me, limit=100, offset=offset)
            await asyncio.sleep(PAUSE)
            chats += batch
            if len(batch) < 100 or offset >= 900:
                break
            offset += 100
        chats = chats[:n_chats]
        print(f"Чатов: {len(chats)}")

        dialogs = []
        with (out_dir / "dialogs.jsonl").open("w", encoding="utf-8") as f:
            for i, c in enumerate(chats, 1):
                item = (c.get("context") or {}).get("value") or {}
                msgs = [clean_message(m, me) for m in await fetch_all_messages(av, me, c["id"])]
                d = {"chat_id": c["id"], "item_title": item.get("title"), "item_price": item.get("price_string"),
                     "messages": msgs}
                dialogs.append(d)
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
                if i % 25 == 0:
                    print(f"  {i}/{len(chats)}")
        total = sum(len(d["messages"]) for d in dialogs)
        print(f"Сообщений: {total} → data/export/dialogs.jsonl")

        items = []
        try:
            page = 1
            while batch := await av.items(page=page):
                items += batch
                page += 1
                await asyncio.sleep(PAUSE)
        except Exception as e:
            print(f"Объявления: ошибка — {e}")
        (out_dir / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nОбъявлений: {len(items)}")
        for it in items:
            print(f"  {it.get('price')} ₽ | {it.get('status')} | {it.get('title')}")

        qr = quick_reply_candidates(dialogs)
        (out_dir / "quick_replies.json").write_text(
            json.dumps([{"count": n, "text": t} for t, n in qr], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nКандидаты в быстрые ответы: {len(qr)} (топ-10)")
        for t, n in qr[:10]:
            print(f"  [{n}] {t[:160]}")
    finally:
        await av.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--chats", type=int, default=300)
    p.add_argument("--all", action="store_true", help="все чаты аккаунта (обход лимита API по объявлениям)")
    a = p.parse_args()
    asyncio.run(main_all() if a.all else main(a.chats))
