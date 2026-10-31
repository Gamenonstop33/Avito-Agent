"""dialog: забирает события из gateway.events, решает, что ответить, кладёт ответ в gateway.outbox.

Запуск: python -m services.dialog.worker
"""
import asyncio
import json
import logging
import random
import re
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from services.avito_gateway.logic import DELETED_TEXT
from services.dialog import brain, gamepass, kb_rules, night
from services.dialog import lessons as lesson_rules
from shared import alerts, alfa, catalog, chatshot, crm, sales
from shared.config import NS, env
from shared.db import connect, single_instance
from shared.kb_classes import EXTRA, route, steam_item
from shared.llm import LLM
from shared.llm import alarm as llm_alarm
from shared.llm import describe as llm_describe
from shared.llm import on_reserve as llm_on_reserve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("dialog")

SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
INSTANT = env("REPLY_INSTANT") == "1"  # режим тестов: отвечаем сразу, без паузы «как человек»
DEBOUNCE = timedelta(seconds=1 if INSTANT else 3)   # клиент часто пишет несколькими сообщениями подряд — ждём паузу
REPLY_DELAY = (0, 0) if INSTANT else (7, 12)        # секунд от сообщения клиента: «как человек», но в пределах 15 с
STALE = timedelta(hours=1)           # на совсем старые события не отвечаем
HISTORY = 30
LESSONS = 40                         # сколько последних решений Макса показывать LLM
DISCOUNT_AFTER = timedelta(minutes=5)   # новый клиент молчит после прайса — напоминаем о скидке
# 15.10 в 04:51 нейросеть думала 22 мин, а чаты шли по одному — стояли все. Теперь: запрос ≤45 с и 1 повтор,
# ответ целиком ≤90 с (первое сообщение — приветствие с прайсом и без неё), до 8 чатов параллельно (26.10: 40 разом — 95% ответов за 19 с, при 4 — 30 с)
LLM_TIMEOUT, LLM_TRIES = 45, 2
REPLY_DEADLINE = 90
PARALLEL = 8
OWNER_LEADS = timedelta(hours=24)   # Макс писал в чате за это время — переписку ведёт он, допродажи срока нет
ORDER_WAIT = timedelta(hours=24)        # заказ без оплаты — отмена; «на проверке» — напоминание владельцу
ORDER_CHECK_CLOSE = timedelta(days=3)   # «на проверке» столько — закрыт как неподтверждённый (решение 21.10)
BALANCE_LOW = {"USD": 3, "CNY": 20}     # остаток на счёте нейросети — меньше: предупреждение (решение 21.10)
STOP_WAIT = 8                           # с при остановке (выкладке): дать дописаться ответам — docker ждёт 10 с
# Оплата на ИП: свежий заказ (или клиент сказал «оплатил») спрашиваем у шлюза раз в 10 с, остальные — раз в 2 мин
PAY_POLL_FAST, PAY_POLL_SLOW, PAY_FAST_FOR = 10, 120, timedelta(minutes=30)
PAY_ESCALATE = timedelta(minutes=10)    # «оплатил», а шлюз оплату не видит — к владельцу
# свои аккаунты заказчика (gateway.ignored) — ни пингов, ни «ответим утром», ни скидок
NOT_IGNORED = ("NOT EXISTS (SELECT 1 FROM gateway.chats gi JOIN gateway.ignored i ON i.client_id=gi.client_id "
               "WHERE gi.id=c.chat_id)")
# Сводка за ночь: чаты, где клиент писал ночью, и кто ответил на его последнее сообщение (ответ бота может ещё ждать
# паузу в outbox, а событие — быть в работе)
NIGHT_CHATS = f"""
SELECT c.chat_id, c.client_text,
       EXISTS (SELECT 1 FROM gateway.messages m WHERE m.chat_id=c.chat_id AND m.origin='owner'
               AND m.created_at > c.last_client) AS by_owner,
       EXISTS (SELECT 1 FROM gateway.messages m WHERE m.chat_id=c.chat_id AND m.origin='bot'
               AND m.created_at > c.last_client)
       OR EXISTS (SELECT 1 FROM gateway.outbox o WHERE o.chat_id=c.chat_id AND NOT o.broadcast
                  AND o.status IN ('pending','sent') AND o.created_at > c.last_client)
       OR EXISTS (SELECT 1 FROM gateway.events e WHERE e.chat_id=c.chat_id AND e.processed_at IS NULL) AS by_bot
FROM (SELECT chat_id, max(created_at) AS last_client, (array_agg(text ORDER BY created_at DESC))[1] AS client_text
      FROM gateway.messages WHERE origin='client' AND deleted_at IS NULL AND created_at BETWEEN $1 AND $2
      GROUP BY chat_id) c
WHERE {NOT_IGNORED} ORDER BY c.last_client"""


class Dialog:
    def __init__(self, db, llm: LLM):
        self.db, self.llm = db, llm
        self.alfa = alfa.AlfaClient() if sales.ALFA_READY else None   # оплата на ИП (СБП, Альфа-Банк)
        self.pay_checked: dict[int, float] = {}                     # заказ → когда последний раз спросили шлюз
        self.kb_at = None
        self.load_kb()
        self.busy: set[str] = set()       # чаты в обработке — второй раз параллельно не берём
        self.tasks: set[asyncio.Task] = set()
        self.balance_at = -3600.0         # когда последний раз смотрели баланс нейросети (loop.time)

    async def tick(self) -> None:
        """Чаты с новыми событиями — параллельно (до PARALLEL), один чат — не больше одной обработки за раз."""
        now = datetime.now(timezone.utc)
        chats = await self.db.fetch(
            "SELECT chat_id, account_id, max(created_at) AS last FROM gateway.events "
            "WHERE processed_at IS NULL GROUP BY chat_id, account_id ORDER BY min(created_at)")
        for c in chats:
            if now - c["last"] >= DEBOUNCE and c["chat_id"] not in self.busy and len(self.busy) < PARALLEL:
                self.busy.add(c["chat_id"])
                task = asyncio.create_task(self.run_chat(c["chat_id"], c["account_id"]))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    async def owner_since(self, chat_id: str, at: datetime) -> bool:
        """Макс писал в чат после этого момента (сообщения пишет gateway, пока dialog думает)."""
        return bool(await self.db.fetchval("SELECT 1 FROM gateway.messages WHERE chat_id=$1 AND origin='owner' "
                                           "AND created_at > $2 LIMIT 1", chat_id, at))

    async def dark(self, now: datetime) -> bool:
        """Ночь бота (23–6): Макса не зовём, клиенту «ответим утром». Но если Макс на связи — за OWNER_AWAKE писал
        в Avito или нажимал кнопки в VK — уже день (16.10: в 07:47 Макс работал, а бот оплатившему — «проверим
        утром, после 8:00»)."""
        return night.is_night(now) and not await self.db.fetchval(
            "SELECT EXISTS (SELECT 1 FROM gateway.messages WHERE origin='owner' AND created_at > $1) "
            "OR EXISTS (SELECT 1 FROM owner.seen s JOIN owner.subscribers o ON o.vk_id=s.vk_id "
            "WHERE o.role='owner' AND s.at > $1)", now - night.OWNER_AWAKE)

    async def first_buy(self, client_id: int | None, paid) -> bool:
        """Только что оплаченная покупка — первая у клиента."""
        return bool(client_id and not await self.db.fetchval(
            "SELECT 1 FROM crm.purchases WHERE client_id=$1 AND paid_at < $2::timestamptz - interval '1 day'",
            client_id, paid["paid_at"]))

    async def gift_due(self, client_id: int | None, paid) -> bool:
        """Подарок положен за только что оплаченную покупку: она первая у клиента и на срок с подарком. Клиент уже
        «покупавший», но ждёт свою игру (17.10: бот ответил ему «подарок только при первой покупке»)."""
        return bool(paid and client_id and await self.db.fetchval(
            "SELECT 1 FROM catalog.prices p WHERE p.product=$1 AND p.months=$2 AND p.gifts > 0 AND NOT EXISTS "
            "(SELECT 1 FROM crm.purchases c WHERE c.client_id=$3 AND c.paid_at < $4::timestamptz - interval '1 day')",
            paid["product"], paid["months"], client_id, paid["paid_at"]))

    async def run_chat(self, chat_id: str, account_id: int) -> None:
        try:
            await self.process_chat(chat_id, account_id)
        except Exception:
            log.exception("ошибка обработки чата %s", chat_id)
        finally:
            self.busy.discard(chat_id)

    async def process_chat(self, chat_id: str, account_id: int) -> None:
        async with self.db.acquire() as con, con.transaction():
            events = await con.fetch(
                "SELECT id, kind, payload, created_at FROM gateway.events WHERE chat_id=$1 AND processed_at IS NULL "
                "ORDER BY id FOR UPDATE SKIP LOCKED", chat_id)
            if not events:
                return
            await con.execute("UPDATE gateway.events SET processed_at=now() WHERE id = ANY($1::bigint[])",
                              [e["id"] for e in events])
            await con.execute("INSERT INTO dialog.chats (chat_id, account_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                              chat_id, account_id)
        try:
            await self.handle(chat_id, account_id, events)
        except Exception as e:
            log.exception("ошибка в чате %s", chat_id)
            if isinstance(e, (TimeoutError, httpx.TimeoutException)):
                await llm_alarm(e)   # нейросеть не ответила вовремя — админу (раз в час)
            said = " / ".join(self.text_of(x) for x in events if x["kind"] == "client_message")
            await self.handoff(chat_id, account_id, "nonstandard", f"бот не смог ответить: {llm_describe(e)}",
                               said or None, None, await self.history(chat_id))

    async def handle(self, chat_id: str, account_id: int, events: list) -> None:
        now = datetime.now(timezone.utc)
        client = [e for e in events if e["kind"] == "client_message"]
        resumed = any(e["kind"] == "resume" for e in events)
        chat = await self.db.fetchrow("SELECT * FROM dialog.chats WHERE chat_id=$1", chat_id)
        meta = await self.db.fetchrow("SELECT item_title, client_id, client_name FROM gateway.chats WHERE id=$1",
                                      chat_id)
        title, client_id = (meta["item_title"], meta["client_id"]) if meta else (None, None)
        if client_id:
            await crm.touch(self.db, client_id, meta["client_name"])

        failed = [self.text_of(e) for e in events if e["kind"] == "delivery_failed"]
        if failed:  # Avito не пропустил наше сообщение — клиент его не увидел
            last_out = await self.db.fetchval(
                "SELECT text FROM gateway.messages WHERE chat_id=$1 AND origin IN ('bot','owner') "
                "ORDER BY created_at DESC LIMIT 1", chat_id)
            await self.handoff(chat_id, account_id, "nonstandard",
                               f"Avito не доставил сообщение «{(last_out or '')[:200]}». Причина: {failed[-1][:200]}",
                               None, None, await self.history(chat_id), title=title)
            return
        owner = [self.text_of(e) for e in events if e["kind"] == "owner_message"]
        if owner:
            await self.owner_wrote(chat_id, chat, " / ".join(owner))
            chat = await self.db.fetchrow("SELECT * FROM dialog.chats WHERE chat_id=$1", chat_id)
        if chat["state"] == "human":
            # чат у человека — бот молчит. Каждое сообщение клиента в VK не пересылаем (14.10: 26 таких за 2 дня —
            # «мусор»): Макс видит их в Avito, а если не ответил — одно напоминание (wait_watch)
            return
        if client and all(now - e["created_at"] > STALE for e in client):
            log.info("%s: старые сообщения, пропускаю", chat_id)
            return

        history = await self.history(chat_id)
        if not client and not (resumed and history and history[-1][0] == "client"):
            return  # нечего отвечать
        if (client and not brain.is_first(history) and brain.needs_answer(history) is False
                and all(json.loads(e["payload"]).get("type") in ("text", None) for e in client)):
            log.info("%s: клиент только подтвердил/поблагодарил — не отвечаем", chat_id)
            return

        # Не отправленный ещё ответ устарел — пересоберём с учётом новых сообщений. Только ответы бота (reply_to):
        # служебные — «Оплату получили» с шагами, «нужно время», хвост длинного сообщения — уходят как есть (стенд
        # 22.10: клиент написал через секунду после оплаты по ссылке, и шаги для кода отменились)
        cancelled = await self.db.fetch("UPDATE gateway.outbox SET status='cancelled' WHERE chat_id=$1 "
                                        "AND status='pending' AND reply_to IS NOT NULL RETURNING text", chat_id)
        if any(brain.UPSELL_MARK in (r["text"] or "") for r in cancelled):
            # предложение срока побольше клиенту не ушло — пусть бот предложит снова (14.10: «)» через 3 с после
            # «Оформить нужно» отменило предложение, и клиент сразу получил реквизиты)
            await self.db.execute("UPDATE dialog.chats SET upsell_at=NULL WHERE chat_id=$1", chat_id)
        if any(brain.PC_TERMS_HEAD in (r["text"] or "") for r in cancelled):   # условия ПК тоже не дошли
            await self.db.execute("UPDATE dialog.chats SET pc_terms_at=NULL WHERE chat_id=$1", chat_id)
        # отменённый ответ нёс реквизиты (клиент дописал, пока ответ ждал паузу) — в новом они должны быть снова
        # (стенд 22.10: «оставим 2» + «а можно на карту?» — заказ создан, а ссылку клиент так и не увидел)
        lost_pay = any("Сумма:" in (r["text"] or "") for r in cancelled)
        if cancelled:
            chat = await self.db.fetchrow("SELECT * FROM dialog.chats WHERE chat_id=$1", chat_id)
        # прошлый ответ мог уйти, пока мы его отменяли (gateway держит строку, пока отправляет), — история заново,
        # иначе он «не виден» и бот здоровается второй раз (18.10: двойное приветствие с прайсом)
        history = await self.history(chat_id)
        first = brain.is_first(history)
        if client_id and not steam_item(title) and any(who == "seller" and crm.is_purchase_msg(t) for who, t in history):
            await crm.mark_purchased(self.db, client_id, now, "chat")  # Макс уже выдал подписку в этом чате
        new = await crm.is_new(self.db, client_id)
        price, personal = await catalog.price_parts(self.db, gifts=new)   # подарок — только новым
        order = await sales.open_order(self.db, chat_id)
        if order and await sales.paid_by_hand(self.db, order):   # Макс подключил сам, кнопку в VK не нажимал
            order = None
        paid = None if order else await sales.last_paid(self.db, chat_id)   # оплачен — клиент на этапе входа
        said_now = " / ".join(self.text_of(e) for e in client)
        if (client and order and order["status"] == "awaiting" and not first and brain.pure_consent(said_now)
                and any(sales.pay_text(order) in t for who, t in history[-3:] if who == "seller")):
            # «давайте» / «хорошо» сразу после реквизитов — как «ок»: отвечать нечего (стенд 23.10: LLM выдумала
            # «доплата 100 ₽ — итого 1 090 ₽»)
            log.info("%s: согласие после реквизитов — не отвечаю", chat_id)
            return
        gift_due = not new and await self.gift_due(client_id, paid)   # уже купил, но подарок — за эту покупку
        # первая покупка на консоль, но срок без подарка — «подарок от 10 мес», не «только при первой» (правило 85)
        gift_short = (not new and not gift_due and bool(paid) and paid["product"] == "console"
                      and await self.first_buy(client_id, paid))
        gift_list = await catalog.template(self.db, "gift_list") if new or gift_due else ""
        # Макс сам ведёт эту переписку — «возьмите срок больше» не предлагаем (18.10: договорился с клиентом на 6 мес,
        # а бот на «Давайте оформим» вместо реквизитов предложил 12)
        owner_leads = bool(await self.db.fetchval(
            "SELECT 1 FROM gateway.messages WHERE chat_id=$1 AND origin='owner' AND created_at > now() - $2::interval "
            "LIMIT 1", chat_id, OWNER_LEADS))
        # срок подольше уже предлагали (допродажа кодом, словами LLM) или ведёт Макс — больше не предлагает никто
        already_offered = bool(chat["upsell_at"]) or owner_leads or brain.offered_longer(history)
        # Макс сам прислал клиенту реквизиты — свои, на другой банк, бот не выставляет (прод 23.10)
        owner_req = owner_leads and any(brain.OWNER_REQ.search(r["text"] or "") for r in await self.db.fetch(
            "SELECT text FROM gateway.messages WHERE chat_id=$1 AND origin='owner' AND created_at > now() - $2::interval",
            chat_id, OWNER_LEADS))
        client_text = " / ".join(self.text_of(e) for e in client) if client else history[-1][1]
        platform = brain.platform_of([t for who, t in history[-8:] if who == "client"])   # "both" — только личный
        draft, step_up, known_product = None, None, None
        attached = any(json.loads(e["payload"]).get("type") not in ("text", None) for e in client)
        if attached:
            decision = (brain.Decision("Спасибо! Проверяю поступление, минуту 🙏",
                                       {"reason": "payment", "summary": "прислал скрин оплаты"}, chat["stage"])
                        if order and (order["status"] == "awaiting" or order["method"] == "ip") else
                        brain.Decision("Секунду, сейчас посмотрю 🙏", {"reason": "image", "summary": "прислал вложение"},
                                       chat["stage"]))
        else:
            hint = ""   # новый клиент сомневается — один раз напоминаем о скидке (вдруг не заметил в прайсе)
            # только если прайс он уже видел (аудит 20.10: на первое «а дешевле можно?» — «Вы пропустили скидку»)
            if (new and client and not first and not order and not paid and not chat["discount_nudged_at"]
                    and brain.hesitates(client_text)
                    and any(brain.shows_price(t, price) for who, t in history if who == "seller")
                    and not any("со скидкой" in t for who, t in history if who == "seller")):
                hint = brain.DISCOUNT_HINT
                await self.db.execute("UPDATE dialog.chats SET discount_nudged_at=now() WHERE chat_id=$1", chat_id)
            if client and brain.doubts_again(history, client_text) and not (
                    personal and any(personal.split("\n")[0] in t for who, t in history if who == "seller")):
                hint += brain.DOUBT_AGAIN_HINT   # второе сомнение в схеме — личный аккаунт (если ещё не предлагали)
            if client and brain.SOLO_Q.search(client_text):   # «каждый раз входить?» — «нет», после оплаты пустая учётка
                hint += brain.SOLO_HINT
                await self.db.execute("UPDATE dialog.chats SET want_empty=true WHERE chat_id=$1", chat_id)
            if client and client_id and brain.END_Q.search(client_text) and (sub := await self.db.fetchrow(
                    "SELECT product, months, paid_at, expires_at FROM crm.purchases WHERE client_id=$1 "
                    "AND months IS NOT NULL AND expires_at IS NOT NULL ORDER BY paid_at DESC LIMIT 1", client_id)):
                hint += brain.END_HINT.format(name=sales.NAMES.get(sub["product"], sub["product"]),
                                              months=sub["months"], paid=sub["paid_at"].astimezone(sales.TZ),
                                              end=sub["expires_at"].astimezone(sales.TZ))
            if client and brain.ADVICE_Q.search(client_text) and not brain.PLATFORM_WORDS.search(client_text):
                hint += brain.ADVICE_HINT
            if not new and client_id and not order and not paid:   # повторная покупка — предложить срок больше
                last = await self.db.fetchrow(
                    "SELECT product, months, paid_at FROM crm.purchases WHERE client_id=$1 AND months IS NOT NULL "
                    "AND product IN ('console','pc') ORDER BY paid_at DESC LIMIT 1", client_id)
                if last:
                    known_product = last["product"]   # платформа известна по прошлой покупке — не переспрашиваем
                    _, prices = await catalog.load(self.db)
                    terms = prices.get(last["product"], {})
                    # только сроки, где месяц правда дешевле прошлого (аудит 20.10: ПК 10 → 20 «дешевле» — неправда)
                    up = (brain.cheaper_terms(last["months"], terms, brain.upsell_targets(last["product"],
                                                                                          last["months"], terms))
                          if not owner_leads else [])
                    if up and not already_offered and brain.PRICE_Q.search(client_text) \
                            and not brain.named_term(client_text):   # «сколько на 6» — срок назван (стенд 26.10)
                        step_up = brain.step_up_text(last["months"], up)   # допишем к ответу кодом
                    hint += (f"\n\nРАНЬШЕ ПОКУПАЛ: {sales.NAMES.get(last['product'], last['product'])} "
                             f"{last['months']} мес ({last['paid_at']:%m.%Y}) — платформу не переспрашивай."
                             + (f" Если он покупает или продлевает и срок ещё не выбран — предложи взять больше: "
                                f"{' или '.join(map(str, up))} мес, выходит выгоднее за месяц (цену за месяц не пиши). "
                                "Пишет о проблеме с подпиской — срок не предлагай."
                                if up and not already_offered else ""))
                    if client and brain.SAME_AS_BEFORE.search(client_text) and not brain.named_term(client_text):
                        hint += brain.SAME_HINT.format(months=last["months"])
            if client:   # «у меня всего 1000 ₽» — что по карману, считает код
                hint += brain.budget_hint(client_text, (await catalog.load(self.db))[1], new)
            if already_offered and not order and not paid:
                hint += brain.UPSOLD_HINT
            if owner_req and not paid:
                hint += brain.OWNER_REQ_HINT
            if gift_due:   # первая покупка с подарком оплачена: игра положена (прогон 21.10: «подарок не положен»)
                hint += brain.GIFT_DUE_HINT + gift_list
            if gift_short:
                hint += brain.GIFT_SHORT_HINT
            # ночью до оплаты — «подключим как можно оперативнее»; оплата — только реквизиты или (если включено) ИП
            dark_now = await self.dark(now)
            mode = await sales.pay_mode(self.db) if self.alfa and not owner_req else "card"   # реквизиты Макса
            ip_on, ip_only = mode in ("mix", "ip"), mode == "ip"
            if dark_now and not paid:
                hint += brain.NIGHT_HINT
            if client and not order and not paid:   # счёт за сутки не оплатили — отменён, ссылка не действует
                gone = await self.db.fetchrow(
                    "SELECT product, months, amount FROM sales.orders WHERE chat_id=$1 AND status='cancelled' "
                    "AND created_at > now() - interval '7 days' ORDER BY id DESC LIMIT 1", chat_id)
                if gone:
                    hint += brain.STALE_ORDER_HINT.format(
                        what=f"{sales.NAMES.get(gone['product'], gone['product'])} {gone['months']} мес, "
                             f"{gone['amount']} ₽")
            if client and platform == "both":   # и консоль, и ПК — только личный аккаунт
                hint += brain.BOTH_HINT
            if ip_only:   # «Только ИП»: всегда — иначе «ссылку не присылаем, оплата по реквизитам» (стенд 22.10)
                hint += brain.IP_ONLY_HINT
            elif ip_on and client and brain.PAY_TALK.search(client_text):
                hint += brain.IP_HINT
            try:
                decision, draft = await asyncio.wait_for(
                    self.think(history, title, price, personal, resumed, new,
                               (order and dict(order)) or (paid and dict(paid)), hint), REPLY_DEADLINE)
            except Exception as e:
                if not first:
                    raise
                # приветствие с прайсом собирает код — без нейросети тоже уйдёт (14.10: при сбое молчал и на «прайс?»)
                log.error("%s: нейросеть недоступна (%s) — отправляю приветствие с прайсом", chat_id, llm_describe(e))
                if isinstance(e, TimeoutError):
                    await llm_alarm(e)
                decision, draft = brain.Decision("", None, chat["stage"]), None
            if await self.owner_since(chat_id, client[-1]["created_at"] if client else now):
                # пока бот думал, ответил Макс — свой ответ не шлём и ничего не оформляем (15.10: приветствие
                # ушло через 22 мин, когда Макс давно вёл разговор)
                log.info("%s: пока бот думал, в чат написал Макс — ответ не отправляю", chat_id)
                return
            if client and not decision.handoff and brain.BOT_ASK.search(client_text):
                decision.handoff = {"reason": "bot_question", "summary": "спросил, не бот ли"}
                decision.reply = random.choice(brain.BOT_JOKES)   # её ответ мог быть «Нет, …» — то есть «я человек»
            if client:   # PS Plus, возврат, чужие товары — владельцу, без «да/нет»
                own = bool(brain.PERSONAL_WORDS.search(" ".join(t for w, t in history[-8:] if w == "client"))
                           or (order and order["product"] == "personal"))
                decision.reply, decision.handoff = brain.goods_guard(
                    decision.reply, decision.handoff, decision.games, client_text, own,
                    " ".join(t for w, t in history[-8:] if w == "client"), bool(paid))
                if (brain.CORE_HAVE.search(client_text) and not decision.handoff and not any(
                        brain.CORE_RULE.search(r) for r in await lesson_rules.active_rules(self.db))):
                    # своя Core у клиента — пока у владельца нет правила, уточняет он (решение 23.10)
                    decision.reply, decision.handoff = brain.GOODS_HANDOFF, {
                        "reason": "nonstandard", "summary": "у клиента уже есть подписка Game Pass Core — как подключать?"}
                if not (decision.handoff and decision.handoff["reason"] in ("payment", "code")):
                    decision.reply = brain.pay_guard(decision.reply, client_text, ip_on, ip_only)
                    if dark_now:   # и после оплаты: ночью подключает человек утром (стенд 22.10)
                        decision.reply = brain.night_guard(decision.reply)
        said =" ".join(t for who, t in history[-8:] if who == "client")
        upsold = any(who == "seller" and brain.UPSELL_MARK in t for who, t in history[-6:])   # тип уже выбран
        both = set.intersection(*brain.price_terms(price))
        if (decision.order and decision.order["product"] != "personal" and decision.order["months"] in both
                and not order and not paid and not upsold and not known_product
                and not brain.PLATFORM_WORDS.search(said)):
            # срок есть и у консоли, и у ПК, а клиент платформу не называл — LLM угадала тип; сначала спросим
            decision.order, decision.reply = None, "На консоль или на ПК?"
        reply = brain.guard_reply(brain.drop_repeats(decision.reply, history), new, price, said, decision.found,
                                  ordered=bool(order or paid), gift_due=gift_due, first=first,
                                  bank=order["bank"] if order and order["method"] == "card" else "",
                                  platform_known=bool(decision.order or order or paid or upsold or known_product),
                                  checking=not paid, latest=client_text, gift_short=gift_short)
        reply = brain.personal_label(reply, (await catalog.load(self.db))[1])
        if already_offered and reply and not re.search(r"(?i)выгодн|дольше|подольше|больше|дешевл", client_text):
            # срок подольше уже предлагали — повтор LLM («можно взять 4 или 6») убираем, остальной ответ оставляем
            reply = " ".join(s for s in brain._sentences(reply)
                             if not (brain.OWN_UPSELL.search(s) and re.search(r"\d+\s*мес", s))) or reply
        if paid and paid["discount"] and brain.DISCOUNT_DENIED.search(reply) and re.search(r"(?i)скидк", client_text):
            reply = brain.DISCOUNT_GIVEN.format(months=paid["months"], amount=brain.money(paid["amount"]),
                                                full=brain.money(paid["amount"] + paid["discount"]))
        if not reply and brain.leaks(decision.reply) and not decision.handoff and not decision.order:
            # ответ целиком из доступов (почта, пароль, код) — убран; не молчим, а передаём владельцу (аудит 20.10)
            decision.handoff = {"reason": "nonstandard",
                                "summary": "ответ бота с данными учётки, реквизитами или ссылкой убран"}
            reply = brain.GOODS_HANDOFF
        if not reply and client and decision.handoff and decision.handoff["reason"] == "code":
            reply = brain.CODE_ACK
        asked = next((t for who, t in reversed(history) if who == "seller"), "")
        if not reply and decision.reply and brain.UPSELL_MARK in asked and not decision.order:
            # после допродажи LLM спросила «консоль или ПК?» (тип уже ясен) — повторяем вопрос о сроке из допродажи
            reply = asked.strip().split("\n")[-1]
        if not first and re.search(r"(?i)вот\s+(\w+\s+)?(прайс|цены)", reply) and "{прайс" not in reply:
            reply += " {прайс}"   # «вот актуальный прайс» без самого прайса (прогон 20.10)
        if not paid and brain.CODE_EARLY.search(reply) and not decision.handoff:
            reply = brain.CODE_AFTER_PAY   # код с консоли — только после оплаты (прогон 21.10)
        if client and brain.DOUBT.search(client_text) and not brain.doubts_again(history, client_text):
            reply = brain.first_doubt_reply(reply)   # первое сомнение в схеме — только объяснение, без личного
        if (client and brain.ADVICE_Q.search(client_text) and not brain.PLATFORM_WORDS.search(client_text)
                and brain.PLATFORM_Q.search(reply) and not decision.order):
            reply = brain.advice_text(new)   # на «какой срок лучше?» — совет, а не «консоль или ПК?»
        if decision.handoff and decision.handoff["reason"] == "game" and not reply.startswith("Уточню"):
            reply = brain.GAME_HANDOFF
        if (decision.handoff and decision.handoff["reason"] == "payment" and order and order["status"] == "awaiting"
                and not owner_req and not await self.pay_shown(chat_id, order)):
            # картинка / «оплатил», а реквизитов этого заказа клиент не видел — это не оплата (прод 30.10: «Да давай»
            # и через 7 с фото со своими данными — фото отменило ответ с реквизитами, ушло «Проверяю поступление»):
            # реквизиты, владельцу — к сведению, чат у бота (решение 30.10)
            decision.order = None
            what = "прислал вложение" if attached else "пишет, что оплатил"
            decision.handoff = {"reason": "image" if attached else "nonstandard", "fyi": True,
                                "summary": f"{what} до реквизитов",
                                "head": f"Клиент {what} до реквизитов — бот прислал их и ведёт чат дальше"}
            reply = "\n\n".join((sales.pay_text(order), sales.PAY_AFTER[order["method"]]))
        if (decision.handoff and decision.handoff["reason"] == "payment" and order and order["method"] == "ip"
                and order["status"] in sales.OPEN):
            # оплата на ИП: поступление видит шлюз (pay_watch, хоть ночью); владелец — если оплаты нет PAY_ESCALATE.
            # Новый заказ тут не нужен (стенд 22.10: на «оплатил» LLM заполнила другой срок — ушла вторая ссылка)
            decision.handoff, decision.order = None, None
            if order["status"] == "awaiting":
                await sales.claim(self.db, order["id"])
                order = await sales.open_order(self.db, chat_id)   # уже «на проверке»: ссылку следом не повторяем
            self.pay_checked.pop(order["id"], None)   # проверить сразу
            reply = sales.PAY_CHECKING
        if client and not decision.order and not decision.handoff and not order and not paid:
            # клиент согласился, а LLM повторила цену / переспросила — срок и тип однозначны, оформляем сами
            _, prices = await catalog.load(self.db)
            if guess := brain.implied_order(history, prices):
                log.info("%s: клиент согласен, LLM заказ не оформила — оформляю %s", chat_id, guess)
                decision.order, reply = guess, brain.ORDER_LEAD
        if owner_req and decision.order and not order and not paid:
            log.info("%s: реквизиты клиенту уже прислал Макс — свой счёт не выставляю", chat_id)
            reply = " ".join(s for s in brain._sentences(reply or "") if not brain.ASK_GO.search(s))
            decision.order, reply = None, (reply if reply and reply != brain.ORDER_LEAD else brain.OWNER_REQ_REPLY)
        if (owner_req and not paid and client and not decision.handoff
                and (brain.PAY_TALK.search(client_text) or brain.REQ_AGAIN.search(client_text))):
            # спрашивает, куда / как платить, — к реквизитам Макса, без «пришлём ссылку» и своих сумм (стенд 23.10)
            keep = [s for s in brain._sentences(reply or "") if not re.search(r"(?i)ссылк|сбп|реквизит|сумм|₽|qr", s)]
            reply = " ".join(keep + [brain.OWNER_REQ_REPLY])
        if step_up and not decision.order and not decision.handoff and not brain.OWN_UPSELL.search(reply or ""):
            reply = "\n\n".join(x for x in (reply, step_up) if x)   # покупавшему — срок больше прошлого, один раз
            await self.db.execute("UPDATE dialog.chats SET upsell_at=now() WHERE chat_id=$1", chat_id)
        # «ой, мне на ПК» после реквизитов на консоль — условия аренды тоже (прогон 21.10: ушла одна цена ПК)
        if client and not decision.handoff and (not order or order["status"] == "awaiting") and not paid and \
                platform != "both" and brain.pc_terms_needed(decision.order, client_text, bool(chat["pc_terms_at"]) or any(
                    who == "seller" and (brain.PC_TERMS_HEAD in t or re.search(r"(?i)аренд", t)
                                         and re.search(r"(?i)сохранени", t)) for who, t in history[-8:])):
            # ПК (15.10): сначала условия аренды и прайс на личный, реквизиты — после ответа клиента; срок побольше
            # советуем в самих условиях — отдельной допродажи для ПК нет
            await self.db.execute("UPDATE dialog.chats SET pc_terms_at=now(), upsell_at=coalesce(upsell_at, now()) "
                                  "WHERE chat_id=$1", chat_id)
            log.info("%s: ПК — условия аренды и прайс на личный", chat_id)
            # срок клиент уже назвал — не «На какой срок оформляем?», а «Оформляем на N мес?» (22.10)
            def pc_ask(text: str) -> str:
                """Вопрос LLM — в конце, после условий; срок клиент уже назвал — «Оформляем на N мес?» (стенд 22.10)."""
                asks = [s for s in brain._sentences(text or "") if s.rstrip().endswith("?")
                        and not re.search(r"(?i)аккаунт|аренд", s)]
                n = brain.chosen_term(client_text)
                if n and n in pc_terms:
                    return f"Оформляем на {n} мес?"
                return brain.pc_missing(pc_terms, n) or (asks[-1] if asks else "")   # 2 мес на ПК нет — какие есть

            # первый ответ без общего прайса — цены аренды ПК тоже, иначе клиент их не видел (стенд 22.10)
            pc_prices = brain.pc_price_block(price) if first and "{прайс}" not in (reply or "") else ""
            pc_terms, months = brain.price_terms(price)[1], decision.order and decision.order["months"]
            reply = (f"{pc_prices}\n\n{brain.PC_TERMS}\n\n"
                     f"{brain.pc_missing(pc_terms, months) or f'Оформляем на {months} мес?'}".strip()
                     if decision.order else
                     "\n\n".join(x for x in (brain.pc_reply(reply), pc_prices, brain.PC_TERMS, pc_ask(reply)) if x))
            decision.order = None
        elif (client and platform == "pc" and not decision.order and not decision.handoff and not order and not paid
                and (missing := brain.pc_missing(brain.price_terms(price)[1], brain.chosen_term(client_text)))
                and missing not in (reply or "")):
            # «давайте 2» на ПК — сроки ПК, а не одно «Какой берём?» от LLM (решение владельца 31.10)
            keep = " ".join(s for s in brain._sentences(reply or "") if not s.rstrip().endswith("?"))
            t = sorted(brain.price_terms(price)[1])   # сроки ПК LLM уже перечислила — только вопрос
            if re.search(r"\s*,\s*".join(map(str, t[:-1])) + rf"\s*(и|или)\s*{t[-1]}(?!\d)", keep):
                missing = "На какой оформляем?"
            reply = "\n\n".join(x for x in (keep, missing) if x)
        if client and platform == "both" and not paid and not decision.handoff:
            # «для ПК, играю на одной консоли» — только личный аккаунт (решение владельца 31.10)
            if decision.order and decision.order["product"] != "personal":
                log.info("%s: и консоль, и ПК — заказ %s не оформляю, предлагаю личный", chat_id, decision.order)
                decision.order, reply = None, "" if reply == brain.ORDER_LEAD else reply
            if not decision.order and not order and personal and "{прайс_личный}" not in (reply or ""):
                if any(who == "seller" and personal.split("\n")[0] in t for who, t in history[-6:]):
                    reply = reply or brain.BOTH_AGAIN   # цены на личный уже были
                else:   # LLM про личный сказала, а цен нет — только цены
                    add = "{прайс_личный}" if re.search(r"(?i)личн", reply or "") else brain.BOTH_OFFER
                    reply = "\n\n".join(x for x in (reply, add) if x)
        if decision.order and not decision.handoff and not order and client and not chat["upsell_at"] \
                and not owner_leads and not brain.offered_longer(history):
            if up := await self.upsell(decision.order, new, said):   # один раз за переписку, реквизиты — после
                await self.db.execute("UPDATE dialog.chats SET upsell_at=now() WHERE chat_id=$1", chat_id)
                log.info("%s: выбрал %s мес — предлагаю срок больше", chat_id, decision.order["months"])
                # спросил о чём-то ещё — сначала ответ, потом предложение (прод 16.10: на «По qr?» — одна допродажа)
                answer = brain.answer_part(reply) if brain.asks_question(client_text) else ""
                decision.order, reply = None, "\n\n".join(x for x in (answer, up) if x)
        if (decision.order and not decision.handoff and not order and not paid
                and (again := brain.upsell_overreach(history, decision.order["months"]))):
            log.info("%s: LLM оформила %s мес из допродажи, клиент их не выбирал — спрашиваю снова", chat_id,
                     decision.order["months"])
            decision.order, reply = None, "\n\n".join(x for x in (brain.answer_part(reply), again) if x)
        if client and not decision.handoff and not order and not paid and (again := brain.upsell_unclear(history)):
            log.info("%s: «давайте» на допродажу с несколькими сроками — уточняю срок", chat_id)
            decision.order, reply = None, again
        if (client and paid and not order and not decision.order
                and (not decision.handoff or decision.handoff["reason"] in ("payment", "nonstandard"))
                and brain.MORE_TERM.search(client_text)
                # «ещё 2 сверху» — число без «мес»: после оплаты это срок (стенд 23.10)
                and (more := brain.named_term(client_text) or (
                    int(n[0]) if len(n := re.findall(r"(?<!\d)(\d{1,2})(?!\d)", client_text)) == 1 else None))):
            _, prices = await catalog.load(self.db)
            if more in prices.get(paid["product"], {}):   # ещё N мес после оплаты — счёт сразу, без «на какой срок?»
                # (стенд 22.10: LLM отдала «доплату» владельцу и писала «пришлю ссылку», ссылки не было)
                decision.order, decision.handoff = {"product": paid["product"], "months": more,
                                                    "price": prices[paid["product"]][more]}, None
                reply = brain.ORDER_LEAD
        if (client and not decision.order and not decision.handoff and reply and brain.ASK_TERM.search(reply)
                and (n := brain.named_term(client_text))):
            # срок клиент назвал — не переспрашиваем; без вопроса пусто — цена срока (стенд 26.10: «сколько на год?» →
            # «На какой срок — 12 мес?»)
            _, prices = await catalog.load(self.db)
            product = brain.term_product(n, said, prices, known_product, platform)
            kind = {"console": "консоль", "pc": "ПК"}.get(product)
            rest = " ".join(s for s in brain._sentences(reply) if not brain.ASK_TERM.search(s))
            if rest or kind or not product:
                reply = rest or (brain.term_line(price, n, new, kind) if kind else "") or "На консоль или на ПК?"
        offer, term_named = "", None
        if (client and not decision.order and not decision.handoff and not order and not paid
                and (term_named := brain.named_term(client_text))
                # выбор — срок без вопроса («нужно на 2 месяца», «2 месяца так же»); «а на 12?» — вопрос о цене
                and (first or brain.TERM_Q.search(reply or "") or not brain.asks_question(client_text))):
            # клиент сам назвал срок — «Вас на какой срок интересует?» не спрашиваем, вместо вопроса допродажа (22.10:
            # «gamepass для Xbox на 12 месяцев?» — ответ, прайс и в конце «на какой срок?»; «нужно два месяца» — тоже)
            _, prices = await catalog.load(self.db)
            product = brain.term_product(term_named, said, prices, known_product, platform)
            if product == "pc":   # у ПК своя подача — условия аренды и совет 10/20 мес (15.10), допродажи нет
                term_named = None
            elif product and not chat["upsell_at"] and not owner_leads and not brain.offered_longer(history):
                offer = await self.upsell({"product": product, "months": term_named,
                                           "price": prices[product][term_named]}, new, said) or ""
                if offer:
                    await self.db.execute("UPDATE dialog.chats SET upsell_at=now() WHERE chat_id=$1", chat_id)
                    log.info("%s: назвал %s мес — предлагаю срок больше", chat_id, term_named)
            asked_term = brain.TERM_Q.sub("", reply or "").strip()
            if offer:   # вопрос в сообщении один — допродажа; свои «Подключаем?», «На консоль?» LLM лишние,
                # как и её цены других сроков («4 мес — 1 690 ₽ (422 ₽/мес)»: допродажа кодом их и так покажет)
                asked_term = " ".join(k for s in brain._sentences(asked_term)
                                      if (k := brain.offer_keeps(s, decision.games or []))
                                      and not brain.OWN_UPSELL.search(s) and "₽/мес" not in s
                                      and {int(x) for x in re.findall(r"(\d{1,2})\s*мес", s)} <= {term_named})
                asked_term = brain.drop_term_price(asked_term, term_named, perk=bool(re.search(
                    r"подар|розыгрыш", offer.split("\n")[0])))
            elif product and term_named and "?" not in asked_term:
                offer = f"Оформляем на {term_named} мес?"   # дольше предложить нечего — подтверждаем его срок
            if asked_term or offer or first:
                reply = asked_term
            if offer and not first:   # в первом ответе — после прайса (render_reply)
                reply, offer = "\n\n".join(x for x in (reply, offer) if x), ""
        if (decision.order and not decision.handoff and decision.order["product"] == "pc"
                and (missing := brain.pc_missing(brain.price_terms(price)[1], decision.order["months"]))):
            # LLM оформила на ПК срок, которого там нет («да, на 2 мес»), — сроки ПК, не «Уточню» владельцу (прод 31.10)
            log.info("%s: на ПК нет %s мес — спрашиваю срок", chat_id, decision.order["months"])
            decision.order, reply = None, missing
        if decision.order and not decision.handoff:
            reply, order = await self.make_order(chat_id, account_id, client_id, decision, reply, said, new, order,
                                                 client_text)
        elif (client and not decision.handoff and order and order["status"] == "awaiting"
                and brain.REQ_AGAIN.search(client_text) and sales.pay_text(order) not in (reply or "")):
            # «скиньте реквизиты ещё раз» — те же реквизиты заказа, а не «реквизиты выше» или тишина (аудит 20.10)
            keep = " ".join(s for s in brain._sentences(reply or "") if not re.search(r"(?i)реквизит", s))
            reply = "\n\n".join(x for x in (keep, sales.pay_text(order), sales.PAY_AFTER[order["method"]]) if x)
        if (lost_pay and not decision.handoff and order and order["status"] == "awaiting"
                and sales.pay_text(order) not in (reply or "")):
            reply = "\n\n".join(x for x in (reply, sales.pay_text(order), sales.PAY_AFTER[order["method"]]) if x)
        pay = None
        if decision.handoff and decision.handoff["reason"] == "payment" and order and order["status"] == "awaiting":
            await sales.claim(self.db, order["id"])   # клиент говорит, что оплатил — владелец проверяет
            pay = order
        failed_order = bool(decision.order and not decision.handoff and not order)
        reason = decision.handoff["reason"] if decision.handoff else "payment" if failed_order else None
        # к сведению («вы бот?», вложение до реквизитов) — бот ведёт чат сам, и ночью тоже
        fyi = bool(decision.handoff) and (reason == "bot_question" or bool(decision.handoff.get("fyi")))
        defer = bool(reason) and not fyi and await self.dark(now)
        # ночью «оплатил» на карту (решение 21.10): алерта нет, клиенту «проверяю поступление», и только через
        # PAY_NIGHT_WAIT без владельца — «проверим утром» (wait_watch); остальное — сразу «ответим утром» (09.10)
        defer = defer and not (pay and pay["method"] != "ip")
        if defer:
            reply = night.NIGHT_PAY_REPLY if reason == "payment" else night.DEFER_REPLY

        if (client and gift_list and brain.GIFT_ASK.search(client_text) and "{подарки}" not in (reply or "")
                and not decision.order and not decision.handoff and reply):
            # спросил, что за подарок, — список сразу, а не «пришлю варианты» (стенд 22.10)
            # «…из списка — пришлю его при оформлении» → только сам хвост про «пришлю», остальное оставляем
            reply = re.sub(r"(?i)\s*[—–,-]?\s*(список\s+)?(пришлю|скину|отправлю)[^.!?]*", "", reply).strip()
            reply = (reply if re.search(r"\w", reply) else "") + " {подарки}"   # от «Пришлю список.» не остаётся «.»
        if not first and reply and re.search(r"\{прайс(_личный)?\}", reply) and not re.search(
                r"(?i)прайс|цен[ыуа]|расценк|ещ[её]\s+раз|скинь|пришл|повтор", client_text or ""):
            # тот же прайс был в последних сообщениях — второй раз целиком не шлём (LLM вставляла метку снова)
            recent_seller = [t for who, t in history[-4:] if who == "seller"]
            for mark, head, note in (("{прайс}", price.strip().split("\n")[0] if price else "", "Цены — в сообщении выше 👆"),
                                     ("{прайс_личный}", personal.split("\n")[0] if personal else "",
                                      "Цены на личный аккаунт — в сообщении выше 👆")):
                if mark in reply and head and any(head in t for t in recent_seller):
                    reply = re.sub(rf"[ \t]*{re.escape(mark)}[ \t]*[.,;]?", "", reply).strip()
                    reply = re.sub(r"\s*:$", ".", reply)   # «…минимальный срок 4 мес:» без цен (стенд 31.10)
                    if not re.search(r"\d", reply):   # «Цены зависят от срока и платформы.» — без указателя пусто
                        reply = note
        if (first and reply and "{прайс}" not in reply and not decision.order and not decision.handoff
                and not decision.games and platform != "both" and brain.PRICE_ASK.search(client_text)):
            reply += " {прайс}"   # спросил цену подписки, а LLM прайс не вставила — в первом ответе он нужен
        # ПК без такого срока и «ПК + консоль» — общий прайс с консолью в хвост не идёт (прод 31.10)
        text = (brain.render_reply(reply, price, first, new, personal, gift_list,
                                   bool(term_named) and platform not in ("pc", "both"), offer)
                if reply or first else "")
        # реквизиты уходят всегда: новый срок — новая сумма, текст почти тот же (аудит 20.10: клиент не видел сумму)
        must_send = bool(order) and order["status"] in sales.OPEN and sales.pay_text(order) in text
        recent = await self.recent_out(chat_id) if text and not first and not must_send else []
        # и с каждым абзацем: ответ мог повторить хвост первого сообщения после шапки (стенд 26.10: ночью «Подключим
        # как можно оперативнее. На консоль или на ПК?» дважды, «ответим утром» на «давайте»)
        tails = [p for t in recent for p in t.split("\n\n")[1:] if len(p) >= 25]
        if recent and brain.is_duplicate(text, recent + tails):
            whole = brain.is_duplicate(text, recent)
            if brain.PLATFORM_Q.search(text) and len(text) < 120 and not brain.is_duplicate(brain.PLATFORM_AGAIN,
                                                                                             recent + tails):
                text = brain.PLATFORM_AGAIN   # тот же вопрос — другими словами, а не молчание
            elif (sales.PAY_CHECKING in text or brain.CHECKING_SAID.search(text) and len(text) < 120) and \
                    not brain.is_duplicate(sales.PAY_RECHECK, recent + tails):   # «я же оплатил!» ещё раз — не тишина
                text = sales.PAY_RECHECK
            elif client and ("?" in client_text or brain.NUDGE.match(client_text)):
                # «алло», «ау» — тоже переспрос (стенд 22.10: на «алло» после «оплата проверяется» — тишина)
                log.info("%s: ответ повторяет недавний, но клиент переспросил — отправляю", chat_id)
            elif not whole and client and not (brain.is_ack(client_text) or brain.pure_consent(client_text)):
                # повтор хвоста прошлого сообщения, а клиент написал новое («у вас брал», «ну хоть почту») — лучше
                # повтор, чем тишина; на «давайте» после «ответим утром» — молчим (стенд 26.10)
                log.info("%s: ответ повторяет часть недавнего, клиент написал новое — отправляю", chat_id)
            else:
                log.info("%s: ответ почти дословно повторяет недавнее сообщение — не отправляю", chat_id)
                text = ""
        if text:
            base = client[-1]["created_at"] if client else now
            send_after = max(now, base + timedelta(seconds=random.uniform(*REPLY_DELAY)))
            # reply_to — время сообщения клиента по часам Avito: Макс написал позже — gateway ответ не отправит
            said_at = json.loads(client[-1]["payload"]).get("created") if client else None
            reply_to = datetime.fromtimestamp(said_at, timezone.utc) if said_at else base
            await self.db.execute(
                "INSERT INTO gateway.outbox (account_id, chat_id, text, send_after, reply_to) VALUES ($1,$2,$3,$4,$5)",
                account_id, chat_id, text, send_after, reply_to)
        await self.db.execute("UPDATE dialog.chats SET stage=coalesce($2, stage), updated_at=now() WHERE chat_id=$1",
                              chat_id, decision.stage)
        log.info("%s: ответ готов%s", chat_id, f", передаю человеку ({decision.handoff['reason']})"
                 if decision.handoff else "")
        if decision.handoff:
            await self.handoff(chat_id, account_id, decision.handoff["reason"], decision.handoff.get("summary"),
                               client_text, draft, history, stage=decision.stage, title=title,
                               bot_said=brain.render_reply(reply, "[прайс]") if reply else None, order=pay,
                               images=[u for e in client if (u := self.image_url(e))],
                               fyi=fyi, head=decision.handoff.get("head"))
        elif failed_order:  # заказ не прошёл проверку цены/срока
            await self.handoff(chat_id, account_id, "payment", f"бот не смог оформить заказ: {decision.order}",
                               client_text, None, history, stage=decision.stage, title=title,
                               bot_said=brain.render_reply(reply, "[прайс]") if reply else None)
        if defer:   # клиенту уже сказали «утром» — ночной повтор и «ответим утром» второй раз не нужны
            await self.db.execute("UPDATE dialog.chats SET wait_deferred_at=now(), wait_pinged_at=now() "
                                  "WHERE chat_id=$1", chat_id)
            await self.db.execute("UPDATE dialog.handoffs SET status='deferred', deferred_at=now() "
                                  "WHERE chat_id=$1 AND status='open'", chat_id)

    async def upsell(self, o: dict, new: bool, said: str) -> str | None:
        """Клиент назвал срок — предлагаем срок больше (brain.UPSELL), текст с ценами и ценой за месяц — кодом.
        Подарок и розыгрыш — только новым на консоль (первая покупка через доп. аккаунт)."""
        rows = {r["months"]: {"price": r["price_rub"], "gifts": r["gifts"], "raffle": r["raffle"]}
                for r in await self.db.fetch("SELECT months, price_rub, gifts, raffle FROM catalog.prices "
                                             "WHERE product=$1", o["product"])}
        targets = brain.upsell_targets(o["product"], o["months"], rows) if o["months"] in rows else []
        if not (new and o["product"] == "console"):
            rows = {m: {**r, "gifts": 0, "raffle": False} for m, r in rows.items()}
        cut = (sales.DISCOUNT if new and o["product"] not in sales.NO_DISCOUNT
               and not brain.DISCOUNT_DECLINE.search(said) else 0)
        # только сроки, где месяц дешевле или есть подарок / розыгрыш (аудит 20.10: 10 → 12 «выгоднее» при 349 = 349)
        targets = [t for t in targets if brain.worth(o["months"], rows, t, cut)]
        if not targets:
            return None
        return brain.upsell_text(o["months"], rows, targets, cut)

    async def make_order(self, chat_id, account_id, client_id, decision, reply: str, said: str, new: bool, order,
                         client_text: str = ""):
        """Клиент выбрал тип и срок — заказ и реквизиты в ответ. Цена проверяется по прайсу; банк — тот же, что уже
        давали в этом чате (09.10: второй счёт ушёл с другим банком), иначе следующий по кругу. Тот же заказ уже ждёт
        оплаты — новый не заводим, реквизиты повторяем, только если клиент их просит или без них ответ пуст."""
        o = decision.order
        if not sales.PAY_READY:
            log.warning("%s: реквизиты не заданы (PAY_PHONE/PAY_RECIPIENT) — заказ ведёт владелец", chat_id)
            return "Сейчас пришлю реквизиты для оплаты 🙏", None
        _, prices = await catalog.load(self.db)
        listed = prices.get(o["product"], {}).get(o["months"])
        if not sales.valid_amount(listed, o["price"], new and o["product"] not in sales.NO_DISCOUNT):
            log.warning("%s: заказ с неверной ценой/сроком %s", chat_id, o)
            return "Уточню и напишу Вам 🙏", None
        if order and order["status"] == "checking":
            return reply, order   # оплату уже проверяют — новый заказ не создаём
        # новому — сразу со скидкой за отзыв; отказался от неё («давайте без скидки») — полная цена
        amount = listed if brain.DISCOUNT_DECLINE.search(said) else sales.order_amount(listed, o["product"], new)
        ip = sales.ip_mode(client_id, chat_id, await sales.pay_mode(self.db)) if self.alfa else None
        if ip == "test":
            amount, listed = sales.ALFA_TEST_AMOUNT, sales.ALFA_TEST_AMOUNT
        # reply — уже после guard_reply (раньше брали сырой ответ LLM, и «реквизиты те же» уходило с новым банком);
        # «Оплачиваете?» перед реквизитами не к месту — реквизиты уже тут (прогон 10.10)
        lead = " ".join(s for s in brain._sentences(reply) if not brain.ASK_GO.search(s)) if reply else reply
        if (order and order["status"] == "awaiting"
                and (order["product"], order["months"], order["amount"]) == (o["product"], o["months"], amount)):
            # LLM снова заполнила тот же заказ, а клиент пишет о другом — без нового счёта и без повтора реквизитов
            rest = " ".join(s for s in brain._sentences(lead or "") if not re.search(r"(?i)реквизит", s))
            if rest and not brain.REQ_AGAIN.search(client_text):
                return rest, order
            return "\n\n".join(x for x in (rest, sales.pay_text(order), sales.PAY_AFTER[order["method"]]) if x), order
        order, n, limit = await sales.create_order(self.db, chat_id, account_id, client_id,
                                                   o["product"], o["months"], amount, night.TZ, listed - amount, ip)
        if order["method"] == "ip":
            try:
                order = await self.alfa_order(order)
            except alfa.AlfaError as e:   # шлюз недоступен — принимаем на карту, чтобы не терять клиента
                log.error("%s: заказ №%d в шлюз Альфы не ушёл: %s", chat_id, order["id"], e)
                await alerts.notify_admins(f"⚠️ Альфа-Банк: заказ №{order['id']} не создан ({e}) — клиенту "
                                           f"реквизиты на карту.\nЧат: {alerts.chat_url(chat_id)}")
                order, n, limit = await sales.create_order(self.db, chat_id, account_id, client_id, o["product"],
                                                           o["months"], amount, night.TZ, listed - amount)
        log.info("%s: заказ №%d, %s", chat_id, order["id"], order["bank"])
        if order["method"] == "card" and n > limit:
            # считает выданные реквизиты, а не оплаты (аудит 20.10: «N-я оплата» вводило в заблуждение)
            await alerts.send_alert(self.db, f"{alerts.TAG_PAY} · ⚠️ Сегодня уже {n}-й счёт на карту: "
                                             f"{sales.ROUNDS} круга по банкам пройдены. Заказ №{order['id']}, банк "
                                             f"{order['bank']}.\nЧат: {alerts.chat_url(chat_id)}")
        text = "\n\n".join(x for x in (lead, sales.pay_text(order), sales.PAY_AFTER[order["method"]]) if x)
        return text, order

    async def alfa_order(self, order):
        """Заказ на ИП → заказ в шлюзе Альфа-Банка и ссылка СБП. Номер в шлюзе уникален и после восстановления БД;
        у копии второго аккаунта — с меткой (a2-…), чтобы в кабинете банка было видно, чей заказ (29.10)."""
        pay_id, _ = await self.alfa.register(f"a{NS + '-' if NS else ''}{order['id']}-"
                                             f"{int(order['created_at'].timestamp())}",
                                             order["amount"], f"Оплата заказа №{order['id']}",
                                             ttl=int(sales.IP_TTL.total_seconds()))
        return await sales.set_pay(self.db, order["id"], pay_id, await self.alfa.sbp_link(pay_id))

    async def pay_watch(self) -> None:
        """Заказы на ИП: шлюз видит оплату — выдаём сами, хоть ночью. Отменённые (клиенту дали новую ссылку) тоже
        смотрим: мог оплатить старую. Клиент сообщил об оплате, а её PAY_ESCALATE нет — к владельцу."""
        now, t = datetime.now(timezone.utc), asyncio.get_running_loop().time()
        for o in await self.db.fetch(
                "SELECT * FROM sales.orders WHERE method='ip' AND pay_id IS NOT NULL "
                "AND status IN ('awaiting','checking','cancelled') AND created_at > now() - $1::interval", sales.IP_TTL):
            fast = o["status"] == "checking" or now - o["created_at"] < PAY_FAST_FOR
            if t - self.pay_checked.get(o["id"], 0) < (PAY_POLL_FAST if fast else PAY_POLL_SLOW):
                continue
            self.pay_checked[o["id"]] = t
            try:
                s = await self.alfa.status(o["pay_id"])
            except alfa.AlfaError as e:
                log.warning("заказ №%d: статус в шлюзе не получен: %s", o["id"], e)
                continue
            if s.get("orderStatus") == alfa.PAID:
                await self.ip_paid(o)
            elif o["status"] == "checking" and now - o["claimed_at"] > PAY_ESCALATE:
                await self.ip_escalate(o, now)

    async def ip_paid(self, o) -> None:
        """Оплата на ИП пришла: выдача как после «✅ Оплата пришла», владельцу — уведомление."""
        # клиенту могли выдать новую ссылку, а оплатил он старую — открытые заказы чата больше не нужны
        await self.db.execute("UPDATE sales.orders SET status='cancelled' WHERE chat_id=$1 AND id<>$2 "
                              "AND status = ANY($3::text[])", o["chat_id"], o["id"], list(sales.OPEN))
        at_night = await self.dark(datetime.now(timezone.utc))
        order, stock, lines = await sales.settle(self.db, o["id"], "alfa", night.MORNING_ACCESS if at_night else None)
        if lines is None:
            return
        log.info("%s: оплата на ИП по заказу №%d пришла — выдано автоматически", o["chat_id"], o["id"])
        # кто-то уже проверял эту оплату вручную — случай закрыт: деньги подтвердил банк
        await self.db.execute("UPDATE dialog.handoffs SET status='answered', answered_by='alfa', answered_at=now() "
                              "WHERE chat_id=$1 AND reason='payment' AND status IN ('open','deferred')", o["chat_id"])
        manual = not stock   # учётку выдаёт человек: чат за ним (как после «✅ Оплата пришла»)
        if not manual:   # оплату успели передать владельцу на проверку — чат снова у бота
            await self.db.execute("UPDATE dialog.chats SET state='bot', handoff_reason=NULL, updated_at=now() "
                                  "WHERE chat_id=$1 AND state='human' AND handoff_reason='payment'", o["chat_id"])
        hid = await self.db.fetchval(
            "INSERT INTO dialog.handoffs (chat_id, account_id, reason, summary, status) VALUES ($1,$2,'payment',$3,$4) "
            "RETURNING id", o["chat_id"], o["account_id"], "оплата на ИП пришла (шлюз Альфа-Банка)",
            "open" if manual else "info")
        if manual:
            await self.set_state(o["chat_id"], "human", "payment")
            if at_night:   # клиенту уже сказали «утром» — ночные пинги и второе «утром» не нужны
                await self.db.execute("UPDATE dialog.chats SET wait_deferred_at=now(), wait_pinged_at=now() "
                                      "WHERE chat_id=$1", o["chat_id"])
                await self.db.execute("UPDATE dialog.handoffs SET status='deferred', deferred_at=now() WHERE id=$1",
                                      hid)
            kb, tail = alerts.resume_keyboard(hid), "Чат за Вами; когда закончите — «Вернуть боту»."
        else:
            kb = alerts.issued_keyboard(hid, order["id"] if order["product"] == "console" and not order["connected_at"]
                                        else None, order["slot"])
            tail = "Чат ведёт бот. Пришлёт клиент код или попросит код с почты — придёт уведомление."
        what = f"{sales.NAMES.get(order['product'], order['product'])} {order['months']} мес"
        head = (f"{alerts.TAG_PAY} · Оплата на ИП пришла — проверено банком автоматически"
                + (" · 🧪 ТЕСТ" if order["test"] else "") + f"\nСумма: {order['amount']} ₽, заказ №{order['id']}: {what}")
        if at_night and not order["test"]:   # ночью Макса не будим: оплата и «кого подключить» — в утренней сводке
            return
        if stock and await sales.auto_issue(self.db) == "creds":   # всё выдано клиенту — без алерта, счёт в сводке
            return
        await alerts.send_alert(self.db, "\n".join([head, *lines, tail, f"Чат: {alerts.chat_url(o['chat_id'])}"]),
                                kb, hid)

    async def ip_escalate(self, o, now: datetime) -> None:
        """Клиент сообщил об оплате, шлюз её не видит — к владельцу (ночью клиенту «проверим утром»). Один раз."""
        if await self.db.fetchval("SELECT 1 FROM dialog.handoffs WHERE chat_id=$1 AND reason='payment' "
                                  "AND created_at > $2", o["chat_id"], o["claimed_at"]):
            return
        meta = await self.db.fetchrow("SELECT item_title FROM gateway.chats WHERE id=$1", o["chat_id"])
        await self.handoff(o["chat_id"], o["account_id"], "payment",
                           f"сообщил об оплате, а шлюз Альфа-Банка за {PAY_ESCALATE.seconds // 60} мин её не видит",
                           None, None, await self.history(o["chat_id"]), title=meta and meta["item_title"], order=o)
        if await self.dark(now):
            await self.db.execute("INSERT INTO gateway.outbox (account_id, chat_id, text) VALUES ($1,$2,$3)",
                                  o["account_id"], o["chat_id"], night.NIGHT_PAY_REPLY)
            await self.db.execute("UPDATE dialog.chats SET wait_deferred_at=now(), wait_pinged_at=now() "
                                  "WHERE chat_id=$1", o["chat_id"])
            await self.db.execute("UPDATE dialog.handoffs SET status='deferred', deferred_at=now() "
                                  "WHERE chat_id=$1 AND status='open'", o["chat_id"])

    @staticmethod
    def image_url(e) -> str | None:
        """Ссылка на самый крупный размер картинки из сообщения клиента (Avito CDN)."""
        sizes = ((json.loads(e["payload"]).get("content") or {}).get("image") or {}).get("sizes") or {}
        def area(k: str) -> int:
            w, _, h = k.partition("x")
            return int(w) * int(h) if w.isdigit() and h.isdigit() else 0
        return sizes[max(sizes, key=area)] if sizes else None

    @staticmethod
    def text_of(e) -> str:
        p = json.loads(e["payload"])
        return p.get("text") or f"<{p.get('type')}>"

    async def history(self, chat_id: str, no_bot_since: datetime | None = None) -> list[tuple[str, str]]:
        """Переписка без удалённых сообщений — клиент их не видел.
        no_bot_since — без сообщений бота с этого момента («уточню и напишу», «нужно время» — не ответ человека).
        Ответы, уже ушедшие клиенту, но ещё не подтянутые опросом Avito в gateway.messages, берём из outbox — иначе
        на второе сообщение клиента бот повторял свой же ответ (14.10, вопрос про EA и Ubisoft)."""
        rows = await self.db.fetch(
            "SELECT origin, text, type, created_at FROM gateway.messages WHERE chat_id=$1 AND origin <> 'system' "
            "AND deleted_at IS NULL AND text IS DISTINCT FROM $3 "
            "AND NOT (origin='bot' AND created_at >= coalesce($4::timestamptz, 'infinity')) "
            "ORDER BY created_at DESC LIMIT $2", chat_id, HISTORY, DELETED_TEXT, no_bot_since)
        # и служебные сообщения в очереди («Оплату получили» с шагами): уйдут как есть — бот должен их учитывать
        sent = await self.db.fetch(
            "SELECT text, coalesce(sent_at, created_at) AS created_at FROM gateway.outbox o WHERE chat_id=$1 "
            "AND (status='sent' AND sent_at > now() - interval '15 minutes' "
            "OR status='pending' AND reply_to IS NULL AND NOT broadcast) "
            "AND coalesce(sent_at, created_at) < coalesce($2::timestamptz, 'infinity') "
            "AND NOT EXISTS (SELECT 1 FROM gateway.messages m WHERE m.chat_id = o.chat_id AND (m.id = o.message_id "
            "OR (m.text = o.text AND m.created_at > o.sent_at - interval '2 minutes')))", chat_id, no_bot_since)
        # Avito даёт время с точностью до секунды: клиент и бот в одну секунду — клиент после бота, иначе его
        # сообщение выглядело отвеченным и бот молчал (аудит 20.10: 6 таких за 14 дней)
        items = sorted([(r["created_at"], "client" if r["origin"] == "client" else "seller",
                         r["text"] or f"<{r['type']}>") for r in rows] +
                       [(r["created_at"], "seller", r["text"]) for r in sent], key=lambda x: (x[0], x[1] == "client"))
        return [(who, text) for _, who, text in items[-HISTORY:]]

    async def pay_shown(self, chat_id: str, order) -> bool:
        """Реквизиты (ссылку СБП) заказа клиент видел: в переписке или ушли / уходят из очереди."""
        return bool(await self.db.fetchval(
            "SELECT 1 FROM gateway.messages WHERE chat_id=$1 AND origin IN ('bot','owner') AND strpos(text, $2) > 0 "
            "UNION ALL SELECT 1 FROM gateway.outbox WHERE chat_id=$1 AND status IN ('sent','pending') "
            "AND strpos(text, $2) > 0 LIMIT 1", chat_id, sales.pay_text(order)))

    async def recent_out(self, chat_id: str) -> list[str]:
        """Последние сообщения продавца, включая удалённые Максом (их тоже нельзя повторять), и очередь отправки:
        только что ушедший ответ попадает в чат через несколько секунд — без outbox второй прогон дублировал его."""
        return [r["text"] for r in await self.db.fetch(
            "(SELECT text FROM gateway.messages WHERE chat_id=$1 AND origin IN ('bot','owner') AND text IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 8) UNION ALL (SELECT text FROM gateway.outbox WHERE chat_id=$1 "
            "AND status IN ('sent','pending') AND created_at > now() - interval '30 minutes')", chat_id)]

    async def owner_wrote(self, chat_id: str, chat, text: str) -> None:
        """Макс написал в Avito сам: открытый случай закрыт (его ответ — пример для бота), чат за ним, бот молчит.
        Дальше — wait_watch: 10 мин без сообщений Макса — чат снова у бота."""
        await self.db.execute(
            "UPDATE dialog.handoffs SET owner_answer=$2, answered_by='avito', answered_at=now(), status='answered' "
            "WHERE id = (SELECT max(id) FROM dialog.handoffs WHERE chat_id=$1 AND status IN ('open','deferred'))",
            chat_id, text)
        await self.db.execute("UPDATE dialog.chats SET state='human', handoff_reason='owner', owner_at=now(), "
                              "handoff_at=CASE WHEN state='human' THEN handoff_at ELSE now() END, updated_at=now() "
                              "WHERE chat_id=$1", chat_id)
        # ответ бота, ещё не ушедший (пауза 7–12 с), — отменяем: Макс уже пишет сам
        await self.db.execute("UPDATE gateway.outbox SET status='cancelled', error='Макс пишет сам' "
                              "WHERE chat_id=$1 AND status='pending' AND NOT broadcast", chat_id)
        # Макс прислал шаблоны подключения — заказ оплачен вручную сразу, а не при следующем сообщении клиента
        # (18.10: четыре подключённых заказа висели «на проверке»)
        if order := await sales.open_order(self.db, chat_id):
            await sales.paid_by_hand(self.db, order)
        if chat["state"] != "human":
            log.info("%s: Макс пишет в чате сам — бот молчит", chat_id)

    def load_kb(self) -> None:
        """База знаний: при старте и после ночной пересборки (30.10: раньше — только при перезапуске)."""
        if (at := brain.kb_stamp()) == self.kb_at:
            return
        reload, self.kb_at = self.kb_at is not None, at
        self.system = brain.system_prompt(brain.load_kb("core"))
        self.extra = {c: brain.load_kb(c) for c in EXTRA}  # подключаются по теме вопроса
        if reload:
            log.info("база знаний перечитана: core %s символов, доп. разделы %s", len(self.system),
                     {c: len(t) for c, t in self.extra.items()})

    async def think(self, history, title, price, personal, resumed: bool, new: bool,
                    order: dict | None = None, hint: str = "") -> tuple[brain.Decision, str | None]:
        """Возвращает решение и черновик бота (если ответ LLM заблокирован — его покажем Максу)."""
        self.load_kb()
        lessons = await self.db.fetch(
            "SELECT reason, summary, client_text, owner_answer FROM dialog.handoffs WHERE owner_answer IS NOT NULL "
            "ORDER BY answered_at DESC LIMIT $1", LESSONS)
        rules = lesson_rules.rules_block(await lesson_rules.active_rules(self.db))   # из вмешательств Макса
        # Доп. разделы базы знаний: сразу по ключевым словам клиента, иначе — если LLM сама попросит
        recent = " ".join(t for who, t in history[-3:] if who == "client")
        classes = [c for c in route(recent) if self.extra.get(c)]
        looked: dict[str, list[dict]] = {}   # каталог Game Pass: запрос LLM → найденные игры
        alike: dict[str, list[dict]] = {}    # не нашлось — похожие по звучанию на слова клиента
        gp_stats, games = await gamepass.stats(self.db), None
        if gp_stats and history and history[-1][0] == "client" and re.search(r"[а-яё]{4}", history[-1][1].lower()):
            # игра русскими буквами без вопроса («стардью валли») — LLM в каталог не смотрела и отвечала прайсом
            # (стенд 22.10): явное совпадение по звучанию — сразу в блок каталога
            games = await gamepass.load(self.db)
            said_last = history[-1][1]
            if heard := await asyncio.to_thread(gamepass.sound_alike, games, said_last, 3, True):
                looked[said_last[:60]] = heard

        def build(cls: list[str]) -> list[dict]:
            system = (self.system + "".join(brain.extra_block(c, self.extra[c]) for c in cls) +
                      brain.lessons_block([dict(r) for r in lessons]) + rules)
            return [{"role": "system", "content": system},
                    {"role": "user", "content": brain.user_prompt(price, title, history, new, order, personal,
                                                                  gamepass.block(gp_stats, looked, alike)) +
                     (brain.RESUMED_NOTE if resumed else "") + hint}]

        messages = build(classes)
        _, prices = await catalog.load(self.db)
        allowed = {p for rows in prices.values() for p in rows.values()}
        discountable = {p for code, rows in prices.items() if code not in sales.NO_DISCOUNT for p in rows.values()}
        asked = brain.asks_question(recent)
        for attempt in range(2):
            raw, decision = await self.ask(messages, asked)
            for _ in range(2):   # LLM может попросить раздел базы знаний и/или поиск игр в каталоге
                more_kb = decision.kb if decision.kb not in classes and self.extra.get(decision.kb) else None
                want = [g for g in decision.games if g not in looked] if gp_stats else []
                if not more_kb and not want:
                    break
                if more_kb:
                    log.info("LLM попросила раздел базы знаний: %s", more_kb)
                    classes.append(more_kb)
                if want:
                    games = games or await gamepass.load(self.db)
                    looked.update({g: gamepass.find(games, g) for g in want})
                    alike.update({g: gamepass.sound_alike(games, recent) for g in want if not looked[g]})
                    log.info("каталог Game Pass: %s", {g: len(looked[g]) for g in want})
                messages = build(classes)
                raw, decision = await self.ask(messages, asked)
            bad = brain.foreign_prices(decision.reply, allowed, new, discountable)
            # «за 500 не получится», «на 2500 хватит на…» — число клиента в отказе или его бюджет, не цена
            if brain.REFUSAL.search(decision.reply) or brain.BUDGET.search(recent):
                bad = [x for x in bad if x not in brain.client_numbers(recent)]
            if not bad:
                break
            log.warning("LLM назвала цены не из прайса %s (попытка %d)", bad, attempt + 1)
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"Цены {bad} недопустимы: " + (
                             "новому клиенту можно только цену из прайса или минус до 100 ₽ (на личный аккаунт — "
                             "только цену из прайса, скидки нет)." if new else
                             "этот клиент уже покупал, скидки нет — только цены из прайса.") +
                          " Перепиши ответ в том же JSON."}]
        if bad and not new:
            return brain.Decision(brain.NO_DISCOUNT_REPLY, None, decision.stage), None
        if bad and brain.HAGGLE.search(recent):   # новый торгуется — вежливо отказываем сам (правило 2: не владельцу)
            return brain.Decision(brain.NO_DISCOUNT_REPLY_NEW, None, decision.stage), None
        if bad:
            return brain.Decision("Сейчас уточню и вернусь к Вам 🙏",
                                  {"reason": "nonstandard", "summary": f"бот хотел назвать цену не из прайса: {bad}"},
                                  decision.stage), brain.render_reply(decision.reply, price)
        decision.found = looked
        return decision, None

    async def ask(self, messages: list[dict], asked: bool) -> tuple[str, brain.Decision]:
        """Запрос к LLM и разбор. Не JSON или пустой ответ на вопрос клиента — ещё одна попытка (стенд 22.10: обрывок
        или пустота от нейросети — клиенту тишина, владельцу «ошибка программы»); вторая неудача — как раньше."""
        for attempt in range(2):
            raw = await self.llm.chat(messages, json_mode=True, temperature=0.3, max_tokens=1500)
            try:
                decision = brain.parse_decision(raw)
            except ValueError:
                if attempt:
                    raise
                log.warning("ответ нейросети не разобран — спрашиваю ещё раз: %r", (raw or "")[:200])
                continue
            if attempt or not asked or decision.reply or decision.order or decision.handoff:
                break
            log.warning("пустой ответ нейросети на вопрос клиента — спрашиваю ещё раз")
        return raw, decision

    async def open_question(self, history: list[tuple[str, str]]) -> bool:
        """Ждёт ли клиент ответа (иначе «нужно время» / «ответим утром» и пинги не нужны)."""
        quick = brain.needs_answer(history)
        if quick is not None:
            return quick
        text = "\n".join(f"{'Клиент' if who == 'client' else 'Продавец'}: {t}" for who, t in history[-12:])
        try:
            raw = await asyncio.wait_for(   # проверка в общем цикле — долго не ждём
                self.llm.chat([{"role": "system", "content": brain.OPEN_Q}, {"role": "user", "content": text}],
                              json_mode=True, temperature=0, max_tokens=20), 30)
            return bool(json.loads(raw).get("open", True))
        except Exception:
            log.exception("не понял, ждёт ли клиент ответа — считаю, что ждёт")
            return True

    async def wait_watch(self, now: datetime) -> None:
        """Чаты у человека: Макс писал сам и 10 мин молчит — снова у бота (клиент ждёт — бот отвечает); остальные —
        8 ч без Макса; клиент ждёт — одно напоминание Максу через 15 мин, через час клиенту «нужно время»,
        ночью «ответим утром»."""
        for c in await self.db.fetch(
                "UPDATE dialog.chats c SET state='bot', handoff_reason=NULL, updated_at=now() WHERE state='human' "
                "AND handoff_reason='owner' AND owner_at < now() - $1::interval AND NOT EXISTS (SELECT 1 FROM "
                "gateway.messages m WHERE m.chat_id=c.chat_id AND m.origin='owner' AND m.created_at > now() - $1::interval) "
                "AND EXISTS (SELECT 1 FROM gateway.accounts a WHERE a.user_id=c.account_id AND a.started_at IS NOT NULL) "
                f"AND {NOT_IGNORED} RETURNING chat_id, account_id", night.OWNER_IDLE):
            await self.db.execute("UPDATE dialog.handoffs SET status='resumed' WHERE chat_id=$1 "
                                  "AND status IN ('open','info','deferred')", c["chat_id"])
            history = await self.history(c["chat_id"])
            if history and history[-1][0] == "client" and await self.open_question(history):
                await self.db.execute("INSERT INTO gateway.events (account_id, chat_id, kind, payload) "
                                      "VALUES ($1,$2,'resume','{}')", c["account_id"], c["chat_id"])
                log.info("%s: Макс 10 мин не пишет, а клиент ждёт — бот продолжает", c["chat_id"])
            else:
                log.info("%s: Макс 10 мин не пишет — чат снова у бота", c["chat_id"])
        for c in await self.db.fetch(
                "UPDATE dialog.chats c SET state='bot', handoff_reason=NULL, updated_at=now() WHERE state='human' "
                "AND greatest(handoff_at, owner_at) < now() - $1::interval AND NOT EXISTS (SELECT 1 FROM "
                "gateway.messages m WHERE m.chat_id=c.chat_id AND m.origin='owner' AND m.created_at > now() - $1::interval) "
                "RETURNING chat_id", night.RETURN_AFTER):
            # случаи чата закрыты — иначе висели «open/deferred» неделями (аудит 20.10: 25 таких с 30.09)
            await self.db.execute("UPDATE dialog.handoffs SET status='resumed' WHERE chat_id=$1 "
                                  "AND status IN ('open','info','deferred')", c["chat_id"])
            log.info("%s: 8 часов без ответа человека — чат снова у бота", c["chat_id"])

        dark = await self.dark(now)
        for c in await self.db.fetch(
                "SELECT c.*, (SELECT max(created_at) FROM gateway.messages m WHERE m.chat_id=c.chat_id "
                "AND m.origin='owner') AS owner_msg FROM dialog.chats c "
                "JOIN gateway.accounts a ON a.user_id=c.account_id AND a.started_at IS NOT NULL "   # стоп — молчим
                f"WHERE c.state='human' AND {NOT_IGNORED}"):
            # ждёт с первого сообщения клиента после ответа Макса или после «клиент уже не ждёт» (wait_quiet_at)
            answered = max((x for x in (c["owner_at"], c["owner_msg"], c["wait_quiet_at"]) if x), default=None)
            first = await self.db.fetchval(
                "SELECT min(created_at) FROM gateway.messages WHERE chat_id=$1 AND origin='client' "
                "AND deleted_at IS NULL AND ($2::timestamptz IS NULL OR created_at > $2)", c["chat_id"], answered)
            if not first:
                continue   # Макс ответил на всё — ждать нечего
            since = max(x for x in (first, c["handoff_at"]) if x)
            kind = "owner" if c["handoff_reason"] == "owner" else "handoff"
            action = night.wait_action(kind, since, now, c["wait_pinged_at"], c["wait_nudged_at"],
                                       c["wait_deferred_at"], dark, c["handoff_reason"])
            if action == "defer" and c["handoff_reason"] == "payment" and not await self.db.fetchval(
                    "SELECT 1 FROM sales.orders WHERE chat_id=$1 AND status='checking'", c["chat_id"]):
                continue   # оплату уже подтвердили или отклонили — «проверим утром» не к месту
            if not action:
                continue
            history = await self.history(c["chat_id"], no_bot_since=since)
            # скрин оплаты ждёт проверки всегда (18.10: LLM сочла его «завершённой покупкой» — без «проверим утром»)
            if c["handoff_reason"] != "payment" and not await self.open_question(history):
                # поблагодарил, всё решили — ни клиенту, ни Максу; снова — только если клиент напишет ещё
                await self.db.execute("UPDATE dialog.chats SET wait_quiet_at=$2 WHERE chat_id=$1", c["chat_id"], now)
                continue
            mins = int((now - since).total_seconds() // 60)
            waited = f"{mins // 60} ч {mins % 60} мин" if mins >= 60 else f"{mins} мин"
            if action == "ping":   # единственное напоминание — второй и последний алерт по чату (14.10)
                await self.db.execute("UPDATE dialog.chats SET wait_pinged_at=$2 WHERE chat_id=$1", c["chat_id"], now)
                client_text = " / ".join(t for who, t in history[-5:] if who == "client")
                await self.handoff(c["chat_id"], c["account_id"], c["handoff_reason"] or "nonstandard", None,
                                   client_text, None, history, notify_only=True,
                                   head=f"⏰ Клиент ждёт ответа {waited} — напоминаю один раз")
                continue
            # клиенту — «нужно ещё немного времени» (днём) или «ответим утром» (ночью); Максу алерт не шлём
            reply = (night.NEED_TIME_REPLY if action == "nudge" else
                     night.NIGHT_PAY_REPLY if c["handoff_reason"] == "payment" else night.DEFER_REPLY)
            col = "wait_nudged_at" if action == "nudge" else "wait_deferred_at"
            await self.db.execute(f"UPDATE dialog.chats SET {col}=$2, wait_pinged_at=$2 WHERE chat_id=$1",
                                  c["chat_id"], now)
            await self.db.execute("INSERT INTO gateway.outbox (account_id, chat_id, text) VALUES ($1,$2,$3)",
                                  c["account_id"], c["chat_id"], reply)
            if action == "defer":
                await self.db.execute("UPDATE dialog.handoffs SET status='deferred', deferred_at=now() "
                                      "WHERE chat_id=$1 AND status='open'", c["chat_id"])
            log.info("%s: клиент ждёт %s — клиенту «%s»", c["chat_id"], waited, action)

    async def discount_watch(self) -> None:
        """Новый клиент 5 мин молчит после прайса — один раз напоминаем о скидке. Окно до 30 мин: старые чаты
        (например, после перезапуска) не трогаем."""
        for c in await self.db.fetch(
                "SELECT c.chat_id, c.account_id, g.client_id, m.text FROM dialog.chats c "
                "JOIN gateway.chats g ON g.id=c.chat_id "
                "JOIN gateway.accounts a ON a.user_id=c.account_id AND a.started_at IS NOT NULL "
                "JOIN LATERAL (SELECT origin, text, created_at FROM gateway.messages WHERE chat_id=c.chat_id "
                "AND origin <> 'system' AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1) m ON true "
                f"WHERE c.state='bot' AND c.discount_nudged_at IS NULL AND m.origin='bot' AND {NOT_IGNORED} "
                "AND NOT EXISTS (SELECT 1 FROM gateway.messages s WHERE s.chat_id=c.chat_id AND s.origin='bot' "
                "AND s.created_at > now() - interval '1 hour' AND s.text ILIKE '%со скидкой%') "
                "AND m.created_at BETWEEN now() - interval '30 minutes' AND now() - $1::interval", DISCOUNT_AFTER):
            price, _ = await catalog.price_parts(self.db, gifts=True)
            if (not brain.shows_price(c["text"], price) or not await crm.is_new(self.db, c["client_id"])
                    or await sales.open_order(self.db, c["chat_id"])):
                continue
            await self.db.execute("UPDATE dialog.chats SET discount_nudged_at=now() WHERE chat_id=$1", c["chat_id"])
            await self.db.execute("INSERT INTO gateway.outbox (account_id, chat_id, text) VALUES ($1,$2,$3)",
                                  c["account_id"], c["chat_id"], brain.DISCOUNT_NUDGE)
            log.info("%s: молчит после прайса — напомнил о скидке новым", c["chat_id"])

    async def stale_orders(self, now: datetime) -> None:
        """Заказы не висят вечно (решение 21.10; 20.10 открытыми висели 12, с 07–08.10): ждёт оплату сутки — отмена;
        «на проверке» сутки — одно напоминание владельцу с кнопками оплаты; три дня — закрыт как неподтверждённый."""
        await self.db.execute("UPDATE sales.orders SET status='cancelled' WHERE status='awaiting' "
                              "AND created_at < now() - $1::interval", ORDER_WAIT)
        await self.db.execute("UPDATE sales.orders SET status='unconfirmed' WHERE status='checking' "
                              "AND coalesce(claimed_at, created_at) < now() - $1::interval", ORDER_CHECK_CLOSE)
        if await self.dark(now):
            return
        for o in await self.db.fetch(
                "UPDATE sales.orders SET reminded_at=now() WHERE status='checking' AND reminded_at IS NULL "
                "AND coalesce(claimed_at, created_at) < now() - $1::interval RETURNING *", ORDER_WAIT):
            hid = await self.db.fetchval(
                "INSERT INTO dialog.handoffs (chat_id, account_id, reason, summary, status) "
                "VALUES ($1,$2,'payment',$3,'info') RETURNING id", o["chat_id"], o["account_id"],
                f"напоминание: заказ №{o['id']} больше суток на проверке")
            what = f"{sales.NAMES.get(o['product'], o['product'])} {o['months']} мес"
            await alerts.send_alert(
                self.db, f"{alerts.TAG_PAY} · ⏰ Заказ №{o['id']} больше суток «на проверке»: клиент сообщил об оплате, "
                         f"подтверждения нет.\nСумма: {o['amount']} ₽ → {o['bank']}, {what}\n"
                         f"Через {ORDER_CHECK_CLOSE.days} дня с оплаты заказ закроется как неподтверждённый.\n"
                         f"Чат: {alerts.chat_url(o['chat_id'])}", alerts.payment_keyboard(hid), hid)

    async def balance_watch(self, now: datetime) -> None:
        """Раз в час смотрим баланс нейросети; меньше порога — днём, раз в сутки, владельцу и админу (решение 21.10:
        14.10 баланс кончился, и бот ~15 ч не отвечал)."""
        t = asyncio.get_running_loop().time()
        if NS or t - self.balance_at < 3600 or not night.in_workday(now) or llm_on_reserve():
            return   # на резервном ключе не напоминаем (28.10): о переходе уже написали; ключ на оба аккаунта Avito
                     # общий — напоминает основная копия бота
        self.balance_at = t
        info = await self.llm.balance()
        if not info or info[0] >= BALANCE_LOW.get(info[1], BALANCE_LOW["USD"]):
            return
        day = now.astimezone(night.TZ).date().isoformat()
        if not await self.db.fetchval(
                "INSERT INTO sales.settings (key, value, updated_by) VALUES ('llm_balance_alert', $1, 'dialog') "
                "ON CONFLICT (key) DO UPDATE SET value=$1, updated_at=now() WHERE sales.settings.value <> $1 "
                "RETURNING 1", day):
            return   # сегодня уже предупредили
        await alerts.send_alert(self.db, f"{alerts.TAG_INFO} · 💸 На балансе нейросети (DeepSeek) осталось "
                                         f"{info[0]:.2f} {info[1]} — пополните. Кончится — бот перестанет отвечать "
                                         "клиентам (кроме приветствия с прайсом).")

    async def recover(self) -> None:
        """Перезапуск (выкладка) посреди ответа: события уже взяты, а ответа нет — снова в очередь (аудит 20.10:
        при каждой выкладке недописанные ответы пропадали). Не трогаем остановленного агента и то, на что ответ уже
        есть (сообщение в outbox, случай владельцу, обновлённый чат)."""
        rows = await self.db.fetch(
            "UPDATE gateway.events e SET processed_at=NULL WHERE e.processed_at > now() - interval '5 minutes' "
            "AND e.kind IN ('client_message','resume') AND e.created_at > now() - $1::interval "
            "AND EXISTS (SELECT 1 FROM gateway.accounts a WHERE a.user_id=e.account_id AND a.started_at IS NOT NULL "
            "AND a.started_at < e.processed_at) "
            "AND NOT EXISTS (SELECT 1 FROM gateway.outbox o WHERE o.chat_id=e.chat_id AND o.created_at >= e.processed_at) "
            "AND NOT EXISTS (SELECT 1 FROM dialog.handoffs h WHERE h.chat_id=e.chat_id AND h.created_at >= e.processed_at) "
            "AND NOT EXISTS (SELECT 1 FROM dialog.chats c WHERE c.chat_id=e.chat_id AND c.updated_at >= e.processed_at) "
            "RETURNING chat_id", STALE)
        if rows:
            log.warning("после перезапуска снова в очереди: %s", sorted({r["chat_id"] for r in rows}))
        # случаи, давно закрытые по сути (чат у бота больше суток), — не «open/deferred» (25 таких с 30.09)
        await self.db.execute(
            "UPDATE dialog.handoffs h SET status='resumed' WHERE status IN ('open','deferred') "
            "AND created_at < now() - interval '1 day' "
            "AND EXISTS (SELECT 1 FROM dialog.chats c WHERE c.chat_id=h.chat_id AND c.state='bot')")
        await self.db.execute("UPDATE dialog.chats SET handoff_reason=NULL WHERE state='bot' AND handoff_reason IS NOT NULL")

    async def night_watch(self) -> None:
        """Ожидание человека (wait_watch), скидка молчащим после прайса, зависшие заказы, баланс нейросети,
        утренняя сводка за ночь."""
        now = datetime.now(timezone.utc)
        await self.wait_watch(now)
        await self.discount_watch()
        await self.stale_orders(now)
        await self.balance_watch(now)
        local = now.astimezone(night.TZ)
        if night.DAY_START <= local.hour < night.NIGHT_FROM:   # правила, выученные ночью, — утром списком в VK
            await lesson_rules.announce(self.db, lambda text, kb: alerts.send_alert(self.db, text, kb))
        if not night.DAY_START <= local.hour < 12 or await self.db.fetchval(   # сводка — в 8, алерты — уже с 6
                "SELECT 1 FROM dialog.digests WHERE day=$1", local.date()):
            return
        start, end = night.last_night(now)
        chats = await self.db.fetch(NIGHT_CHATS, start, end)
        # по чату — последний случай (было: все случаи, и чат с двумя вычитался из «справился» дважды)
        cases = await self.db.fetch("SELECT DISTINCT ON (chat_id) * FROM dialog.handoffs WHERE status <> 'info' "
                                    "AND created_at BETWEEN $1 AND $2 ORDER BY chat_id, id DESC", start, end)
        handed = {c["chat_id"] for c in cases}
        quiet = set()   # никто не ответил, но и не нужно: «спасибо», «ок» после реквизитов
        for r in chats:
            if r["chat_id"] not in handed and not r["by_owner"] and not r["by_bot"] \
                    and brain.needs_answer(await self.history(r["chat_id"])) is False:
                quiet.add(r["chat_id"])
        await self.db.execute("INSERT INTO dialog.digests (day) VALUES ($1) ON CONFLICT DO NOTHING", local.date())
        # выдано ботом за сутки (в режиме «логин и пароль» алертов по ним нет — 23.10)
        issued = await self.db.fetchval("SELECT count(*) FROM sales.orders WHERE stock_id IS NOT NULL AND NOT test "
                                        "AND paid_at > now() - interval '24 hours'")
        if chats or cases or issued:
            await self.digest(chats, cases, quiet, issued)
        # Строка в сводке — мало (18.10: оплативший ночью ждал 19 ч): по каждому, кому ночью ответили «утром»
        # (и тем, кто ждёт с вечера, до 23:00), — свой алерт с кнопками, дальше как днём: одно напоминание через
        # 15 мин, через час клиенту «нужно ещё немного времени»
        for c in await self.db.fetch(
                "SELECT DISTINCT ON (chat_id) * FROM dialog.handoffs WHERE status='deferred' "
                "AND created_at > now() - interval '24 hours' ORDER BY chat_id, id DESC"):
            try:
                await self.morning_alert(c)
            except Exception:
                log.exception("%s: утренний алерт не ушёл", c["chat_id"])

    async def digest(self, chats: list, cases: list, quiet: set = frozenset(), issued: int = 0) -> None:
        """Сводка по чатам (29.10: было «чаты − случаи» — чаты, где никто не ответил, считались «бот справился»,
        свои аккаунты — «писали клиентов»). Без ответа — отдельным списком со ссылками."""
        handed = {c["chat_id"] for c in cases}
        rest = [r for r in chats if r["chat_id"] not in handed]
        human = [r for r in rest if r["by_owner"]]
        silent = [r for r in rest if not r["by_owner"] and not r["by_bot"] and r["chat_id"] not in quiet]
        waiting = sorted((c for c in cases if c["status"] in ("open", "deferred")), key=lambda c: c["id"])
        lines = [f"{alerts.TAG_INFO} · ☀️ Сводка за ночь ({night.NIGHT_FROM}:00–{night.DAY_START}:00)",
                 f"Писали клиентов: {len(chats)}. Бот ответил сам: {len(rest) - len(human) - len(silent)}. "
                 f"Ответили в Avito сами: {len(human)}. Передано человеку: {len(cases)}."]
        if issued:
            lines.append(f"🔑 Выдано учёток ботом за сутки: {issued}.")
        lines.append("")
        if waiting:
            lines.append(f"⏳ Ждут ответа ({len(waiting)}):")
            for i, c in enumerate(waiting, 1):
                lines += [f"{i}. {brain.REASONS.get(c['reason'], c['reason'])} — {c['summary'] or '—'}",
                          f"   Клиент: «{(brain.readable(c['client_text']) or '—')[:200]}»",
                          f"   {alerts.chat_url(c['chat_id'])}"]
        solved = len(cases) - len(waiting)
        if solved:
            lines += ["", f"✅ Уже решено ночью: {solved}"]
        if silent:
            lines += ["", f"⚠️ Остались без ответа ({len(silent)}):"]
            for i, r in enumerate(silent, 1):
                lines += [f"{i}. Клиент: «{(brain.readable(r['client_text']) or '—')[:200]}»",
                          f"   {alerts.chat_url(r['chat_id'])}"]
        if not waiting and not silent:
            lines += ["", "Все вопросы закрыты 👍"]
        await alerts.send_alert(self.db, "\n".join(lines))

    async def morning_alert(self, case) -> None:
        chat_id = case["chat_id"]
        await self.db.execute("UPDATE dialog.handoffs SET status='superseded' WHERE chat_id=$1 AND status='deferred'",
                              chat_id)
        order = await sales.open_order(self.db, chat_id) if case["reason"] == "payment" else None
        await self.handoff(chat_id, case["account_id"], case["reason"], case["summary"], case["client_text"],
                           case["bot_draft"], await self.history(chat_id),
                           order=order if order and order["status"] == "checking" else None, since_night=case)

    async def set_state(self, chat_id: str, state: str, reason: str | None) -> None:
        await self.db.execute("UPDATE dialog.chats SET state=$2, handoff_reason=$3, handoff_at=now(), updated_at=now() "
                              "WHERE chat_id=$1", chat_id, state, reason)

    async def handoff(self, chat_id, account_id, reason, summary, client_text, draft, history, *,
                      stage=None, title=None, bot_said=None, notify_only=False, order=None,
                      images: list[str] | None = None, fyi: bool = False, head: str | None = None,
                      since_night=None) -> None:
        """Записывает спорный случай и шлёт алерт в VK с кнопками ответа.
        fyi — только уведомление («вы бот?»): бот отшутился и ведёт чат дальше, человек может забрать чат.
        head — свой заголовок уведомления (notify_only: клиент ждёт ответа).
        since_night — ночной случай (клиенту ответили «утром»), утром повторяется отдельным алертом."""
        if not notify_only and not fyi:  # в чате один открытый случай: новый заменяет старые
            await self.db.execute("UPDATE dialog.handoffs SET status='superseded' WHERE chat_id=$1 "
                                  "AND status IN ('open','info')", chat_id)
        hid = await self.db.fetchval(
            "INSERT INTO dialog.handoffs (chat_id, account_id, reason, summary, client_text, context, bot_draft, status) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id", chat_id, account_id, reason, summary, client_text,
            json.dumps(history[-10:], ensure_ascii=False), draft, "info" if notify_only or fyi else "open")
        paid = await sales.last_paid(self.db, chat_id) if reason == "code" else None
        if fyi:
            head = f"{alerts.TAG_INFO} · " + (head or "Клиент спрашивает, не бот ли — бот отшутился и ведёт чат дальше")
        elif notify_only:
            head = f"{alerts.TAG_Q} · " + (head or "Клиент снова пишет (чат у человека, бот молчит)")
        elif order:
            await self.set_state(chat_id, "human", reason)
            what = f"{sales.NAMES.get(order['product'], order['product'])} {order['months']} мес"
            head = (f"{alerts.TAG_PAY} · Клиент сообщает об оплате — проверьте поступление\n"
                    f"Сумма: {order['amount']} ₽ → {order['bank']}\nЗаказ №{order['id']}: {what}")
        else:
            await self.set_state(chat_id, "human", reason)
            tag = {"code": alerts.TAG_CODE, "payment": alerts.TAG_PAY}.get(reason, alerts.TAG_Q)
            head = f"{tag} · {brain.REASONS.get(reason, reason)}"
        if await self.dark(datetime.now(timezone.utc)):   # 23–6 Макс не отвечает — случай ждёт утренней сводки
            log.info("%s: ночь — алерт не шлю, случай в утренней сводке", chat_id)
            return
        if not notify_only and not order and await self.db.fetchval(   # не больше двух алертов по чату (14.10)
                "SELECT 1 FROM dialog.handoffs h JOIN dialog.alert_msgs a ON a.handoff_id = h.id WHERE h.chat_id=$1 "
                "AND h.id <> $2 AND h.created_at > now() - interval '30 minutes'", chat_id, hid):
            log.info("%s: алерт по чату был меньше 30 мин назад — не повторяю", chat_id)
            return
        meta = await self.db.fetchrow(
            "SELECT coalesce(cu.name, g.client_name) AS name, g.item_title FROM gateway.chats g "
            "LEFT JOIN crm.customers cu ON cu.avito_user_id = g.client_id WHERE g.id=$1", chat_id)
        name, title = (meta and meta["name"]) or "Клиент", title or (meta and meta["item_title"])
        lines = [head, f"👤 {name}" + (f" · {title}" if title else ""),   # «Суть» убрана (14.10: излишняя)
                 f"🌙 Ждёт с ночи, с {since_night['created_at'].astimezone(night.TZ):%H:%M}: клиенту ответили «утром»"
                 + (f" — {since_night['summary']}" if since_night["summary"] else "") if since_night else None,
                 f"Клиент: «{shown[:200]}»" if (shown := brain.readable(client_text, bool(images))) else None,
                 f"Бот предлагал ответить: «{draft[:600]}»" if draft else None,
                 f"Чат: {alerts.chat_url(chat_id)}"]
        if paid and paid["stock_id"]:   # вход по коду: данные учётки под рукой, инструкция — кнопкой
            lines.insert(-1, f"Учётка №{paid['stock_id']}" + (f", место {paid['slot']}" if paid["slot"] else "") + ":\n"
                         + "\n".join(x for x in (paid["email"], paid["password"], paid["recovery_email"]
                                                 and f"резервная: {paid['recovery_email']}") if x))
        kb = (alerts.payment_keyboard(hid) if order else alerts.fyi_keyboard(hid) if fyi else
              alerts.code_keyboard(hid, paid["id"] if paid and paid["stock_id"] and paid["product"] == "console"
                                   else None) if paid else alerts.handoff_keyboard(hid, bool(draft)))
        shot = await self.chat_shot(chat_id, head.split("\n")[0], lines[1], bot_said)
        await alerts.send_alert(self.db, "\n".join(x for x in lines if x is not None), kb, hid, images,
                                [shot] if shot else None)

    async def chat_shot(self, chat_id: str, header: str, sub: str, pending: str | None = None) -> bytes | None:
        """Картинка последних сообщений чата для алерта (как скриншот); pending — ответ бота, ещё не ушедший."""
        rows = await self.db.fetch(
            "SELECT * FROM (SELECT origin, text, type, created_at FROM gateway.messages WHERE chat_id=$1 "
            "AND origin IN ('client','bot','owner') AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 8) t "
            "ORDER BY created_at", chat_id)
        items = [(r["origin"], brain.readable(r["text"] or f"<{r['type']}>"), r["created_at"]) for r in rows]
        if pending and not (items and items[-1][0] == "bot" and items[-1][1] == pending):
            items.append(("bot", pending, None))
        try:
            return chatshot.render(header, sub, items, night.TZ) if items else None
        except Exception:
            log.exception("%s: картинка переписки не нарисовалась", chat_id)
            return None


async def main() -> None:
    db = await connect(catalog.SCHEMA + gamepass.SCHEMA + SCHEMA + alerts.SCHEMA + crm.SCHEMA + sales.SCHEMA +
                       lesson_rules.SCHEMA + kb_rules.SCHEMA, max_size=12)   # параллельные чаты + блокировка «одной копии»
    await alerts.sync_subscribers(db)
    await single_instance(db, "dialog")
    if not await gamepass.stats(db):   # первый запуск: не ждём ночного обновления каталога игр
        try:
            log.info("каталог Game Pass загружен: %d игр", await gamepass.refresh(db))
        except Exception:
            log.exception("каталог Game Pass не загрузился — о наличии игр бот будет передавать человеку")
    llm = LLM(timeout=LLM_TIMEOUT, tries=LLM_TRIES)
    d = Dialog(db, llm)
    log.info("dialog запущен: core %d символов, доп. разделы %s", len(d.system),
             {c: len(t) for c, t in d.extra.items()})
    await d.recover()
    # docker stop (выкладка) шлёт SIGTERM: без обработчика процесс умирал сразу — недописанные ответы пропадали
    stop = asyncio.Event()
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    except (NotImplementedError, RuntimeError):   # Windows (локальный запуск)
        pass
    last_watch = last_pay = last_kb = 0.0
    kb_llm, kb_task = LLM(), None   # правка базы по правилам — долгие запросы, отдельно от ответов клиентам
    try:
        while not stop.is_set():
            try:
                await d.tick()
                # база знаний одна на оба аккаунта: по правилам её правит основная копия (31.10)
                if not NS and (kb_task is None or kb_task.done()) and                         (t := asyncio.get_running_loop().time()) - last_kb >= kb_rules.EVERY:
                    last_kb, kb_task = t, asyncio.create_task(kb_rules.sync(db, kb_llm))
                if d.alfa and (t := asyncio.get_running_loop().time()) - last_pay >= 5:
                    last_pay = t
                    await d.pay_watch()
                if (t := asyncio.get_running_loop().time()) - last_watch > 30:
                    last_watch = t
                    await d.night_watch()
            except Exception:
                log.exception("ошибка цикла")
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except TimeoutError:
                pass
        log.info("остановка: жду недописанные ответы до %d с", STOP_WAIT)
    finally:
        if d.tasks:
            await asyncio.wait(list(d.tasks), timeout=STOP_WAIT)
        for task in list(d.tasks):   # не успели — их события вернёт в очередь recover() при следующем запуске
            task.cancel()
        if kb_task:   # обычно оборвётся на запросе к LLM — в базе и файлах ничего не меняется
            kb_task.cancel()
        await kb_llm.close()
        await llm.close()
        if d.alfa:
            await d.alfa.close()
        db.terminate()  # одно соединение держит блокировку «единственной копии»


if __name__ == "__main__":
    asyncio.run(main())
