"""nightly: раз в день после конца рабочего дня (OWNER_DAY_END, МСК) обновляет историю чатов и базу знаний.

Цепочка: export --all (новые/изменённые чаты) → import_history (чаты, сообщения, клиенты в БД) →
build_kb --source all (LLM перечитывает только новые пачки, остальное из кэша). dialog подхватывает файлы сразу.
Запуск: python -m services.nightly.worker [--now]
"""
import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, time, timedelta

from services.dialog import night
from shared import alerts
from shared.config import DATA_DIR
from shared.db import connect, single_instance

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("nightly")
STEPS = [["services.avito_gateway.export", "--all"],
         ["services.dialog.import_history"],
         ["services.dialog.build_kb", "--source", "all"],
         ["services.dialog.game_requests"],   # какие игры спрашивают клиенты (новые сообщения)
         ["services.dialog.gamepass"],   # каталог игр: Microsoft + newxboxone.ru, популярные у клиентов
         ["services.broadcast.purchases"],   # покупки, сроки, учётки из сообщений Макса → sales.issued
         ["services.dialog.lessons"]]    # правила из чатов, где вмешивался Макс (последним: сбой не мешает базе)
LAST = DATA_DIR / "nightly_last.txt"   # дата последнего успешного прогона — после рестарта не повторяем


def next_run(now: datetime) -> datetime:
    at = datetime.combine(now.astimezone(night.TZ).date(), time(night.DAY_END), night.TZ)
    done = LAST.read_text().strip() if LAST.exists() else ""
    return at + timedelta(days=1) if now >= at and done == at.date().isoformat() else max(at, now)


async def run_step(args: list[str]) -> None:
    """Отдельным процессом: память освобождается после шага (на VPS 2 ГБ)."""
    log.info("шаг: %s", " ".join(args))
    p = await asyncio.create_subprocess_exec(sys.executable, "-m", *args, env=os.environ | {"PYTHONIOENCODING": "utf-8"},
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    tail = out.decode("utf-8", "replace").strip().splitlines()[-5:]
    for line in tail:
        log.info("  %s", line)
    if p.returncode:
        raise RuntimeError(f"{args[0]} завершился с кодом {p.returncode}: " + " | ".join(tail[-2:]))


async def run_all() -> None:
    started = datetime.now(night.TZ)
    try:
        for args in STEPS:
            await run_step(args)
    except Exception as e:
        log.exception("ночное обновление не прошло")
        await alerts.notify_admins(f"⚠️ Ночное обновление базы знаний не прошло: {e}"[:1000])
        return
    LAST.write_text(started.date().isoformat())
    log.info("готово за %s", datetime.now(night.TZ) - started)


async def main(now_flag: bool) -> None:
    db = await connect()
    await single_instance(db, "nightly")
    try:
        if now_flag:
            await run_all()
        while True:
            at = next_run(datetime.now(night.TZ))
            log.info("следующее обновление: %s", at.strftime("%d.%m %H:%M"))
            await asyncio.sleep(max(0.0, (at - datetime.now(night.TZ)).total_seconds()))
            await run_all()
            LAST.write_text(at.date().isoformat())  # даже при ошибке — не зацикливаемся, следующая попытка завтра
    finally:
        db.terminate()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--now", action="store_true", help="сразу прогнать обновление, потом по расписанию")
    asyncio.run(main(p.parse_args().now))
