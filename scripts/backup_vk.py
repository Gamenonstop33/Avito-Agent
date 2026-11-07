"""Бэкап базы владельцу и админу в VK (29.10): архив приходит на stdin, имя файла — аргументом.
Сообщение не убирается через сутки (в owner.vk_sent не пишем) — копия должна жить вне сервера.
Запуск — scripts/backup_vk.sh (cron раз в неделю): ... | python scripts/backup_vk.py agent_2026-11-02.7z [--to VK_ID]
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import alerts  # noqa: E402
from shared.config import env  # noqa: E402
from shared.db import connect  # noqa: E402
from shared.vk import NO_RETRY, VK, VKError  # noqa: E402

log = logging.getLogger("backup_vk")


async def main(name: str, to: int | None) -> int:
    data = sys.stdin.buffer.read()
    if not data:
        log.error("пустой архив — не отправляю")
        return 1
    db = await connect()
    vk = VK(env("VK_GROUP_TOKEN"), int(env("VK_GROUP_ID")))
    sent = 0
    try:
        uids = [to] if to else await alerts.recipients(db)
        text = (f"{alerts.TAG_INFO} · 💾 Еженедельная копия базы бота: {name} ({len(data) / 1048576:.1f} МБ). "
                "Архив 7z с паролем, пароль — у админа. Сохраните файл: на сервере копии хранятся только 14 дней.")
        for uid in uids:
            try:
                await vk.send(uid, text, None, await vk.upload_doc(uid, data, name))
                sent += 1
            except VKError as e:
                log.error("бэкап не отправлен %s: %s%s", uid, e, " (сообщения сообществу запрещены)"
                          if e.code in NO_RETRY else "")
    finally:
        await vk.close()
        await db.close()
    log.info("бэкап %s отправлен %d из %d", name, sent, len(uids))
    return 0 if sent else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("name")
    p.add_argument("--to", type=int, help="только этому VK id (проверка)")
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.name, a.to)))
