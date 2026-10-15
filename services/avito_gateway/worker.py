"""Цикл gateway: отправить готовые ответы из outbox → забрать новые сообщения → события в gateway.events.

Отправка и приём идут последовательно в одном цикле: так свежеотправленное сообщение бота
не примется за сообщение Макса.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import asyncpg

from services.avito_gateway.logic import (DELETED_TEXT, MAX_TEXT, chat_allowed, event_kind, origin_of,
                                          split_text, ts)
from shared import alerts
from shared.avito import AvitoClient, AvitoError

log = logging.getLogger("gateway")
SEND_TRIES = 3          # сбой сети / Avito 5xx — столько попыток отправки
STALL_ALERT = 600       # цикл падает дольше 10 мин — алерт админу (бот не принимает и не отправляет сообщения)


class Gateway:
    def __init__(self, pool: asyncpg.Pool, avito: AvitoClient, user_id: int):
        self.db, self.av, self.me = pool, avito, user_id

    # ---------- отправка ----------
    async def send_due(self) -> None:
        rows = await self.db.fetch(
            "SELECT id, chat_id, text, broadcast, attempts, created_at FROM gateway.outbox WHERE status='pending' "
            "AND account_id=$1 AND send_after <= now() ORDER BY send_after LIMIT 20", self.me)
        acc = await self.db.fetchrow("SELECT all_chats, whitelist, started_at FROM gateway.accounts WHERE user_id=$1",
                                     self.me)
        for r in rows:
            if not r["broadcast"] and acc["started_at"] is None:   # «⛔ Стоп Агент»: уходят только рассылки
                await self.db.execute("UPDATE gateway.outbox SET status='cancelled', error='агент остановлен' "
                                      "WHERE id=$1", r["id"])
                continue
            if not r["broadcast"] and not chat_allowed(r["chat_id"], acc["all_chats"], acc["whitelist"]):
                await self.db.execute("UPDATE gateway.outbox SET status='failed', error='чат не в белом списке' "
                                      "WHERE id=$1", r["id"])
                log.warning("не отправляю в %s: чат не в белом списке", r["chat_id"])
                continue
            if len(r["text"]) > MAX_TEXT:   # Avito не примет — остаток отдельными сообщениями следом, по порядку
                first, *rest = split_text(r["text"])
                await self.db.execute("UPDATE gateway.outbox SET text=$2 WHERE id=$1", r["id"], first)
                for i, part in enumerate(rest, 1):
                    await self.db.execute(
                        "INSERT INTO gateway.outbox (account_id, chat_id, text, send_after, broadcast) "
                        "VALUES ($1, $2, $3, now() + make_interval(secs => $4), $5)",
                        self.me, r["chat_id"], part, 2 * i, r["broadcast"])
                r = {**r, "text": first}
            try:
                # прошлая попытка могла дойти (таймаут уже после отправки) — сначала ищем её в чате, без дубля
                m = await self._already_sent(r) if r["attempts"] else None
                m = m or await self.av.send_text(self.me, r["chat_id"], r["text"])
                await self.db.execute(
                    "UPDATE gateway.outbox SET status='sent', message_id=$2, sent_at=now() WHERE id=$1",
                    r["id"], m.get("id"))
                log.info("отправлено в %s", r["chat_id"])
            except Exception as e:
                await self._send_failed(r, e)

    async def _already_sent(self, r) -> dict | None:
        for m in await self.av.messages(self.me, r["chat_id"], limit=10):
            if (m.get("direction") == "out" and (m.get("content") or {}).get("text") == r["text"]
                    and ts(m["created"]) >= r["created_at"] - timedelta(seconds=5)):
                return m
        return None

    async def _send_failed(self, r, e: Exception) -> None:
        """Сбой сети / Avito 5xx — ещё попытки через 1 и 2 мин; 4xx или попытки кончились — failed и алерт админу;
        рассылка — получатель снова «не отправлено» (иначе считался бы напомненным, как 06.10)."""
        err = f"{type(e).__name__}: {e}"[:500]
        n = r["attempts"] + 1
        if n < SEND_TRIES and not (isinstance(e, AvitoError) and e.status and 400 <= e.status < 500):
            await self.db.execute("UPDATE gateway.outbox SET attempts=$2, error=$3, send_after=now() + $4::interval "
                                  "WHERE id=$1", r["id"], n, err, timedelta(minutes=n))
            log.warning("не отправлено в %s (попытка %d, повторю): %s", r["chat_id"], n, err)
            return
        await self.db.execute("UPDATE gateway.outbox SET status='failed', attempts=$2, error=$3 WHERE id=$1",
                              r["id"], n, err)
        log.error("не отправлено в %s: %s", r["chat_id"], err)
        if r["broadcast"]:
            try:
                await self.db.execute("UPDATE broadcast.recipients SET status='failed' WHERE chat_id=$1 AND "
                                      "status='sent' AND sent_at >= $2::timestamptz - interval '1 minute'",
                                      r["chat_id"], r["created_at"])
            except asyncpg.UndefinedTableError:
                pass
        await alerts.notify_admins(f"⚠️ Avito не принял сообщение{' рассылки' if r['broadcast'] else ''} "
                                   f"(попыток: {n}): {err[:300]}\nЧат: {alerts.chat_url(r['chat_id'])}")

    # ---------- приём ----------
    async def poll(self) -> None:
        acc = await self.db.fetchrow("SELECT * FROM gateway.accounts WHERE user_id=$1", self.me)
        if acc["all_chats"]:
            chats = await self.av.chats(self.me, limit=50)
            known = {r["id"] for r in await self.db.fetch(
                "SELECT id FROM gateway.messages WHERE id = ANY($1::text[])",
                [c["last_message"]["id"] for c in chats if c.get("last_message")])}
            todo = [c for c in chats if c.get("last_message") and c["last_message"]["id"] not in known]
        else:
            todo = [{"id": cid} for cid in acc["whitelist"]]

        ignored = {r["client_id"] for r in await self.db.fetch("SELECT client_id FROM gateway.ignored")}
        for c in todo:
            if chat_allowed(c["id"], acc["all_chats"], acc["whitelist"]):
                await self._sync_chat(c, acc["started_at"], ignored, acc["stopped_at"])

    async def _sync_chat(self, chat: dict, started_at: datetime | None, ignored: set[int],
                         stopped_at: datetime | None = None) -> None:
        msgs = await self.av.messages(self.me, chat["id"], limit=30)
        if "users" not in chat and not await self.db.fetchval(
                "SELECT client_id FROM gateway.chats WHERE id=$1", chat["id"]):
            chat = await self.av.chat(self.me, chat["id"])  # режим белого списка: узнаём клиента и объявление
        item = (chat.get("context") or {}).get("value") or {}
        client = next((u for u in chat.get("users", []) if u.get("id") != self.me), {})
        await self.db.execute(
            "INSERT INTO gateway.chats (id, account_id, item_title, item_url, client_name, client_id) "
            "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (id) DO UPDATE SET updated_at=now(), "
            "item_title=COALESCE(EXCLUDED.item_title, gateway.chats.item_title), "
            "item_url=COALESCE(EXCLUDED.item_url, gateway.chats.item_url), "
            "client_name=COALESCE(EXCLUDED.client_name, gateway.chats.client_name), "
            "client_id=COALESCE(EXCLUDED.client_id, gateway.chats.client_id)",
            chat["id"], self.me, item.get("title"), item.get("url"), client.get("name"), client.get("id"))
        if await self.db.fetchval("SELECT client_id FROM gateway.chats WHERE id=$1", chat["id"]) in ignored:
            started_at = stopped_at = None   # не клиент (gateway.ignored): сообщения храним, событий для бота нет

        ids = [m["id"] for m in msgs]
        bot_ids = {r["message_id"] for r in await self.db.fetch(
            "SELECT message_id FROM gateway.outbox WHERE message_id = ANY($1::text[])", ids)}

        for m in reversed(msgs):  # от старых к новым
            origin = origin_of(m, bot_ids)
            content = m.get("content") or {}
            created = ts(m["created"])
            deleted = content.get("text") == DELETED_TEXT
            inserted = await self.db.fetchval(
                "INSERT INTO gateway.messages (id, chat_id, direction, origin, type, text, content, created_at, "
                "deleted_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING RETURNING id",
                m["id"], chat["id"], m.get("direction", "out"), origin, m.get("type", "text"),
                content.get("text"), json.dumps(content, ensure_ascii=False), created,
                datetime.now(timezone.utc) if deleted else None)
            if deleted and not inserted:   # удалили уже после приёма: исходный текст оставляем, только отметка
                await self.db.execute("UPDATE gateway.messages SET deleted_at=now() WHERE id=$1 AND deleted_at IS NULL",
                                      m["id"])
            kind = event_kind(origin, created, started_at, content.get("text"), stopped_at)
            if inserted and kind:
                payload = {"type": m.get("type"), "text": content.get("text"), "content": content,
                           "created": m["created"]}
                await self.db.execute(
                    "INSERT INTO gateway.events (account_id, chat_id, kind, message_id, payload) "
                    "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (message_id) DO NOTHING",
                    self.me, chat["id"], kind, m["id"], json.dumps(payload, ensure_ascii=False))
                log.info("событие %s в %s", kind, chat["id"])

    async def run(self, interval: float) -> None:
        failing_since, alerted = None, False
        while True:
            try:
                await self.send_due()
                await self.poll()
                if alerted:
                    await alerts.notify_admins("✅ gateway снова работает: сообщения Avito принимаются и отправляются.")
                failing_since, alerted = None, False
            except Exception as e:
                log.exception("ошибка цикла")
                failing_since = failing_since or time.monotonic()
                if not alerted and time.monotonic() - failing_since > STALL_ALERT:
                    alerted = True   # одиночные 500 от Avito — норма, алерт только при долгом сбое
                    await alerts.notify_admins(f"⚠️ gateway: ошибки уже {STALL_ALERT // 60}+ мин — бот не принимает и не "
                                               f"отправляет сообщения Avito.\n{type(e).__name__}: {str(e)[:300]}")
            await asyncio.sleep(interval)


async def ensure_account(pool: asyncpg.Pool, avito: AvitoClient, whitelist: list[str]) -> int:
    me = await avito.me()
    await pool.execute(
        "INSERT INTO gateway.accounts (user_id, name, whitelist) VALUES ($1,$2,$3) ON CONFLICT (user_id) DO NOTHING",
        me["id"], me.get("name"), whitelist)
    return me["id"]

