"""Какие игры спрашивают клиенты: LLM находит названия игр в сообщениях клиентов → catalog.game_mentions
(по ним gamepass.popular выбирает популярные игры из подписки — бот называет их, чтобы заинтересовать клиента).
Первый запуск — вся история, дальше только новые сообщения (шаг nightly). Запуск: python -m services.dialog.game_requests
"""
import asyncio
import logging
from datetime import datetime, timezone

from services.dialog.gamepass import SCHEMA
from shared.db import connect
from shared.llm import LLM

SYSTEM = """Тебе дают пронумерованные сообщения клиентов магазина подписок Xbox Game Pass.
Найди сообщения, где клиент называет конкретную видеоигру — в т.ч. сленгом и с ошибками (фифа, фк, колда, гта, форза,
майн, мортал, рдр, ведьмак, ластуха). Для каждой игры верни официальное английское название: номер части — если назван
(фифа 26 / фк 26 → "EA Sports FC 26"; фифа без номера → "EA Sports FC"; FIFA 23 → "FIFA 23"; гта 5 → "Grand Theft Auto V";
колда → "Call of Duty"). Не считай играми подписки и сервисы (Game Pass, EA Play, Ubisoft+, Xbox Live), консоли, жанры.
Ответ — JSON: {"m": [{"i": номер сообщения, "games": ["название", ...]}]}, только сообщения с играми; нет таких — {"m": []}."""
BATCH = 200
PARALLEL = 6
log = logging.getLogger("game_requests")


def batch_text(rows: list) -> str:
    return "\n".join(f"{i}. {' '.join(r['text'].split())[:300]}" for i, r in enumerate(rows))


def parse(d: dict, rows: list) -> list[tuple[str, str, datetime]]:
    """(чат, игра, когда) из ответа LLM; номера вне пачки и мусор отбрасываем."""
    out = []
    for x in d.get("m") or []:
        i = x.get("i")
        if not isinstance(i, int) or not 0 <= i < len(rows):
            continue
        for g in x.get("games") or []:
            if isinstance(g, str) and 2 <= len(g.strip()) <= 80:
                out.append((rows[i]["chat_id"], g.strip(), rows[i]["created_at"]))
    return out


async def run(db, llm: LLM) -> int:
    until = await db.fetchval("SELECT value FROM catalog.settings WHERE key='game_requests_until'")
    since = datetime.fromisoformat(until) if until else datetime(2000, 1, 1, tzinfo=timezone.utc)
    rows = await db.fetch("SELECT chat_id, text, created_at FROM gateway.messages WHERE origin='client' AND type='text' "
                          "AND length(text) BETWEEN 4 AND 400 AND created_at > $1 ORDER BY created_at", since)
    if not rows:
        return 0
    batches = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
    sem, found, failed = asyncio.Semaphore(PARALLEL), [], 0

    async def one(b: list) -> None:
        nonlocal failed
        async with sem:
            try:
                found.extend(parse(await llm.chat_json(SYSTEM, batch_text(b), temperature=0, max_tokens=2000), b))
            except Exception:
                failed += 1
                log.exception("пачка из %d сообщений не разобрана", len(b))

    await asyncio.gather(*(one(b) for b in batches))
    await db.executemany("INSERT INTO catalog.game_mentions (chat_id, title, first_at) VALUES ($1,$2,$3) "
                         "ON CONFLICT (chat_id, title) DO UPDATE SET first_at = least(game_mentions.first_at, $3)",
                         found)
    if failed:   # точку отсчёта не двигаем — в следующий раз пачки пройдут заново (повторы отсекает ключ)
        raise RuntimeError(f"game_requests: не разобрано пачек {failed} из {len(batches)}")
    await db.execute("INSERT INTO catalog.settings (key, value) VALUES ('game_requests_until', $1) "
                     "ON CONFLICT (key) DO UPDATE SET value=$1", rows[-1]["created_at"].isoformat())
    log.info("сообщений %d, упоминаний игр %d, LLM: %s", len(rows), len(found), llm.usage)
    return len(found)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db, llm = await connect(SCHEMA), LLM()
    try:
        await run(db, llm)
    finally:
        await llm.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
