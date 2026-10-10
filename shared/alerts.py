"""Алерты владельцам/админам в VK."""
import asyncio
import logging

import asyncpg

from shared.config import env, env_list
from shared.vk import VK, button, keyboard

log = logging.getLogger("alerts")

# Метка в начале уведомления — что от владельца нужно, видно сразу (09.10: вопросы, оплаты и коды сливались)
TAG_Q = "🟥 ВОПРОС"
TAG_PAY = "💰 ОПЛАТА"
TAG_CODE = "🔑 КОД"
TAG_INFO = "ℹ️ К СВЕДЕНИЮ"

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS owner;
CREATE TABLE IF NOT EXISTS owner.subscribers (
    vk_id  bigint PRIMARY KEY,
    role   text NOT NULL,
    alerts boolean NOT NULL DEFAULT true
);
-- Сообщения бота в VK: через KEEP удаляются у всех, чтобы чат не копился (09.10)
CREATE TABLE IF NOT EXISTS owner.vk_sent (
    peer    bigint NOT NULL,
    cmid    bigint NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (peer, cmid)
);
"""
KEEP = "23 hours"   # VK удаляет у всех только в течение суток после отправки — убираем чуть раньше


def remember(db: asyncpg.Pool):
    """Обработчик VK.on_sent: запоминает отправленное сообщение, чтобы убрать его через KEEP."""
    async def on_sent(peer: int, cmid: int) -> None:
        await db.execute("INSERT INTO owner.vk_sent (peer, cmid) VALUES ($1,$2) ON CONFLICT DO NOTHING", peer, cmid)
    return on_sent


async def sweep(db: asyncpg.Pool, vk: VK) -> None:
    """Удаляет у всех сообщения бота старше KEEP (сообщения самого человека бот удалить не может)."""
    due: dict[int, list[str]] = {}
    for r in await db.fetch(f"DELETE FROM owner.vk_sent WHERE sent_at < now() - interval '{KEEP}' "
                            "RETURNING peer, cmid"):
        due.setdefault(r["peer"], []).append(str(r["cmid"]))
    for peer, cmids in due.items():
        for i in range(0, len(cmids), 100):
            try:
                await vk.call("messages.delete", peer_id=peer, cmids=",".join(cmids[i:i + 100]), delete_for_all=1)
            except Exception as e:
                log.warning("старые сообщения у %s не удалились: %s", peer, e)


def roles() -> dict[int, str]:
    return {int(i): "owner" for i in env_list("VK_OWNER_IDS")} | {int(i): "admin" for i in env_list("VK_ADMIN_IDS")}


async def sync_subscribers(db: asyncpg.Pool) -> None:
    """Добавляет владельцев/админов из .env в БД (удалять — только в БД)."""
    for vk_id, role in roles().items():
        await db.execute("INSERT INTO owner.subscribers (vk_id, role) VALUES ($1,$2) "
                         "ON CONFLICT (vk_id) DO UPDATE SET role=$2", vk_id, role)


async def notify_admins(text: str) -> None:
    """Технические сбои — только админам (не владельцу). Сбой отправки не роняет вызывающего."""
    vk = VK(env("VK_GROUP_TOKEN"), int(env("VK_GROUP_ID")))
    try:
        for uid, role in roles().items():
            if role == "admin":
                await vk.send(uid, text[:3500])
    except Exception:
        log.exception("не отправил админам: %s", text[:200])
    finally:
        await vk.close()


async def recipients(db: asyncpg.Pool) -> list[int]:
    return [r["vk_id"] for r in await db.fetch("SELECT vk_id FROM owner.subscribers WHERE alerts")]


async def access(db: asyncpg.Pool) -> dict[int, str]:
    """Кто управляет ботом: owner.subscribers (из .env попадают при старте, остальных добавляем в БД)."""
    return {r["vk_id"]: r["role"] for r in await db.fetch("SELECT vk_id, role FROM owner.subscribers")}


def chat_url(chat_id: str) -> str:
    return f"https://www.avito.ru/profile/messenger/channel/{chat_id}"


async def send_alert(db: asyncpg.Pool, text: str, kb: dict | None = None, handoff_id: int | None = None,
                     images: list[str] | None = None) -> None:
    """images — ссылки на картинки клиента (Avito CDN): прикладываем к алерту, чтобы не ходить в чат."""
    vk = VK(env("VK_GROUP_TOKEN"), int(env("VK_GROUP_ID")))
    vk.on_sent = remember(db)
    blobs: list[tuple[str, bytes | None]] = []
    for url in (images or [])[:10]:
        try:
            r = await vk._http.get(url)
            r.raise_for_status()
            blobs.append((url, r.content))
        except Exception:
            log.warning("не скачал картинку клиента: %s", url[:80])
            blobs.append((url, None))

    async def one(uid: int) -> None:
        attach, lost = [], [u for u in (images or [])[10:]]
        for url, b in blobs:
            try:
                if b is None:
                    raise ValueError("картинка не скачана")
                attach.append(await vk.upload_photo(uid, b))
            except Exception:
                log.exception("не загрузил картинку в VK для %s", uid)
                lost.append(url)
        # не приложилась — даём прямую ссылку на картинку в Avito, чтобы не искать её в чате
        note = ("\n\n⚠ Не все картинки удалось приложить, открыть по ссылке:\n" + "\n".join(lost)) if lost else ""
        conv_id = await vk.send(uid, text + note, kb, ",".join(attach) or None)
        if handoff_id and conv_id:
            await db.execute("INSERT INTO dialog.alert_msgs (handoff_id, vk_peer, conv_msg_id) VALUES ($1,$2,$3) "
                             "ON CONFLICT DO NOTHING", handoff_id, uid, conv_id)

    try:   # получателям — параллельно: загрузка фото в VK бывает долгой (до ~10 с на штуку)
        uids = await recipients(db)
        for uid, res in zip(uids, await asyncio.gather(*(one(u) for u in uids), return_exceptions=True)):
            if isinstance(res, Exception):
                log.error("алерт не отправлен %s: %r", uid, res)
    finally:
        await vk.close()


def handoff_keyboard(handoff_id: int, has_draft: bool) -> dict:
    rows = [[button("✍️ Ответить клиенту", f"reply:{handoff_id}", "primary")]]
    if has_draft:
        rows.append([button("✅ Отправить вариант бота", f"draft:{handoff_id}", "positive")])
    rows.append([button("▶️ Пусть бот ответит сам", f"resume:{handoff_id}")])
    return keyboard(rows, inline=True)


def fyi_keyboard(handoff_id: int) -> dict:
    """Бот сам ведёт чат; человек может ответить или забрать чат себе."""
    return keyboard([[button("✍️ Ответить клиенту", f"reply:{handoff_id}", "primary")],
                     [button("⏸ Забрать чат", f"keep:{handoff_id}")]], inline=True)


def code_keyboard(handoff_id: int, order_id: int | None) -> dict:
    """Вход по коду после оплаты: войти учёткой → «📨 Отправить инструкцию» (консоль), или ответить клиенту (код с почты)."""
    rows = [[button("📨 Вошёл — отправить инструкцию", f"instr:{order_id}", "positive")]] if order_id else []
    rows += [[button("✍️ Ответить клиенту", f"reply:{handoff_id}", "primary")],
             [button("▶️ Пусть бот ответит сам", f"resume:{handoff_id}")]]
    return keyboard(rows, inline=True)


def payment_keyboard(handoff_id: int) -> dict:
    return keyboard([[button("✅ Оплата пришла", f"payok:{handoff_id}", "positive"),
                      button("❌ Не пришла", f"payno:{handoff_id}", "negative")],
                     [button("✍️ Ответить клиенту", f"reply:{handoff_id}", "primary")]], inline=True)


def issued_keyboard(handoff_id: int, order_id: int | None, slot: int | None) -> dict:
    """Оплачено, учётка выдана, чат у бота: консоль — инструкция после входа по коду; чат можно забрать."""
    rows = [[button(f"📨 Отправить инструкцию ({slot}-й клиент)", f"instr:{order_id}", "positive")]] if order_id else []
    return keyboard(rows + [[button("⏸ Забрать чат", f"keep:{handoff_id}")]], inline=True)


def resume_keyboard(handoff_id: int) -> dict:
    return keyboard([[button("▶️ Вернуть боту", f"resume:{handoff_id}")]], inline=True)
