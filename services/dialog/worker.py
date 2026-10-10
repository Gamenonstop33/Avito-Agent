"""dialog: забирает события из gateway.events, решает, что ответить, кладёт ответ в gateway.outbox.

Запуск: python -m services.dialog.worker
"""
import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.avito_gateway.logic import DELETED_TEXT
from services.dialog import brain, gamepass, night
from services.dialog import lessons as lesson_rules
from shared import alerts, alfa, catalog, crm, sales
from shared.config import env
from shared.db import connect, single_instance
from shared.kb_classes import EXTRA, route
from shared.llm import LLM

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
# Оплата на ИП: свежий заказ (или клиент сказал «оплатил») спрашиваем у шлюза раз в 10 с, остальные — раз в 2 мин
PAY_POLL_FAST, PAY_POLL_SLOW, PAY_FAST_FOR = 10, 120, timedelta(minutes=30)
PAY_ESCALATE = timedelta(minutes=10)    # «оплатил», а шлюз оплату не видит — к владельцу
# свои аккаунты заказчика (gateway.ignored) — ни пингов, ни «ответим утром», ни скидок
NOT_IGNORED = ("NOT EXISTS (SELECT 1 FROM gateway.chats gi JOIN gateway.ignored i ON i.client_id=gi.client_id "
               "WHERE gi.id=c.chat_id)")


class Dialog:
    def __init__(self, db, llm: LLM):
        self.db, self.llm = db, llm
        self.alfa = alfa.AlfaClient() if sales.ALFA_READY else None   # оплата на ИП (СБП, Альфа-Банк)
        self.pay_checked: dict[int, float] = {}                     # заказ → когда последний раз спросили шлюз
        self.system = brain.system_prompt(brain.load_kb("core"))
        self.extra = {c: brain.load_kb(c) for c in EXTRA}  # подключаются по теме вопроса

    async def tick(self) -> None:
        now = datetime.now(timezone.utc)
        chats = await self.db.fetch(
            "SELECT chat_id, account_id, max(created_at) AS last FROM gateway.events "
            "WHERE processed_at IS NULL GROUP BY chat_id, account_id")
        for c in chats:
            if now - c["last"] >= DEBOUNCE:
                await self.process_chat(c["chat_id"], c["account_id"])

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
        except Exception:
            log.exception("ошибка в чате %s", chat_id)
            await self.handoff(chat_id, account_id, "nonstandard", "бот не смог ответить (ошибка)", None, None, [])

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
            # Макс ведёт чат в Avito сам — он и так всё видит; иначе пересылаем, отвечать можно прямо из VK
            if client and chat["handoff_reason"] != "owner":
                texts = [self.text_of(e) for e in client]
                await self.handoff(chat_id, account_id, "nonstandard", "клиент пишет, пока чат у человека",
                                   " / ".join(texts), None, await self.history(chat_id), notify_only=True,
                                   images=[u for e in client if (u := self.image_url(e))])
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

        # Не отправленный ещё ответ устарел — пересоберём с учётом новых сообщений
        await self.db.execute("UPDATE gateway.outbox SET status='cancelled' WHERE chat_id=$1 AND status='pending'",
                              chat_id)
        first = brain.is_first(history)
        if client_id and any(who == "seller" and crm.is_purchase_msg(t) for who, t in history):
            await crm.mark_purchased(self.db, client_id, now, "chat")  # Макс уже выдал подписку в этом чате
        new = await crm.is_new(self.db, client_id)
        price, personal = await catalog.price_parts(self.db, gifts=new)   # подарок — только новым
        gift_list = await catalog.template(self.db, "gift_list") if new else ""
        order = await sales.open_order(self.db, chat_id)
        if order and await sales.paid_by_hand(self.db, order):   # Макс подключил сам, кнопку в VK не нажимал
            order = None
        paid = None if order else await sales.last_paid(self.db, chat_id)   # оплачен — клиент на этапе входа
        client_text = " / ".join(self.text_of(e) for e in client) if client else history[-1][1]
        draft = None
        if any(json.loads(e["payload"]).get("type") not in ("text", None) for e in client):
            decision = (brain.Decision("Спасибо! Проверяю поступление, минуту 🙏",
                                       {"reason": "payment", "summary": "прислал скрин оплаты"}, chat["stage"])
                        if order and (order["status"] == "awaiting" or order["method"] == "ip") else
                        brain.Decision("Секунду, сейчас посмотрю 🙏", {"reason": "image", "summary": "прислал вложение"},
                                       chat["stage"]))
        else:
            hint = ""   # новый клиент сомневается — один раз напоминаем о скидке (вдруг не заметил в прайсе)
            if (new and client and not order and not paid and not chat["discount_nudged_at"]
                    and brain.HESITATE.search(client_text)
                    and not any("со скидкой" in t for who, t in history if who == "seller")):
                hint = brain.DISCOUNT_HINT
                await self.db.execute("UPDATE dialog.chats SET discount_nudged_at=now() WHERE chat_id=$1", chat_id)
            if client and brain.SOLO_Q.search(client_text):   # «каждый раз входить?» — «нет», после оплаты пустая учётка
                hint += brain.SOLO_HINT
                await self.db.execute("UPDATE dialog.chats SET want_empty=true WHERE chat_id=$1", chat_id)
            if not new and client_id and not order and not paid:   # повторная покупка — предложить срок больше
                last = await self.db.fetchrow(
                    "SELECT product, months, paid_at FROM crm.purchases WHERE client_id=$1 AND months IS NOT NULL "
                    "AND product IN ('console','pc') ORDER BY paid_at DESC LIMIT 1", client_id)
                if last:
                    _, prices = await catalog.load(self.db)
                    up = brain.upsell_targets(last["product"], last["months"], prices.get(last["product"], {}))
                    hint += (f"\n\nРАНЬШЕ ПОКУПАЛ: {sales.NAMES.get(last['product'], last['product'])} "
                             f"{last['months']} мес ({last['paid_at']:%m.%Y})."
                             + (f" Срок ещё не выбран — предложи взять больше: {' или '.join(map(str, up))} мес, "
                                "выходит выгоднее за месяц (цену за месяц не пиши)." if up else ""))
            decision, draft = await self.think(history, title, price, personal, resumed, new,
                                               (order and dict(order)) or (paid and dict(paid)), hint)
            if client and not decision.handoff and brain.BOT_ASK.search(client_text):
                decision.handoff = {"reason": "bot_question", "summary": "спросил, не бот ли"}
                decision.reply = random.choice(brain.BOT_JOKES)   # её ответ мог быть «Нет, …» — то есть «я человек»
        said =" ".join(t for who, t in history[-8:] if who == "client")
        reply = brain.guard_reply(brain.drop_repeats(decision.reply, history), new, price, said, decision.found)
        if decision.handoff and decision.handoff["reason"] == "game":
            reply = brain.GAME_HANDOFF
        if (decision.handoff and decision.handoff["reason"] == "payment" and order and order["method"] == "ip"
                and order["status"] in sales.OPEN):
            # оплата на ИП: поступление видит шлюз (pay_watch, хоть ночью); владелец — если оплаты нет PAY_ESCALATE
            decision.handoff = None
            if order["status"] == "awaiting":
                await sales.claim(self.db, order["id"])
            self.pay_checked.pop(order["id"], None)   # проверить сразу
            reply = sales.PAY_CHECKING
        if client and not decision.order and not decision.handoff and not order and not paid:
            # клиент согласился, а LLM повторила цену / переспросила — срок и тип однозначны, оформляем сами
            _, prices = await catalog.load(self.db)
            if guess := brain.implied_order(history, prices):
                log.info("%s: клиент согласен, LLM заказ не оформила — оформляю %s", chat_id, guess)
                decision.order, reply = guess, brain.ORDER_LEAD
        if decision.order and not decision.handoff and not order and client and not chat["upsell_at"]:
            if up := await self.upsell(decision.order, new, said):   # один раз за переписку, реквизиты — после
                await self.db.execute("UPDATE dialog.chats SET upsell_at=now() WHERE chat_id=$1", chat_id)
                log.info("%s: выбрал %s мес — предлагаю срок больше", chat_id, decision.order["months"])
                decision.order, reply = None, up
        if decision.order and not decision.handoff:
            reply, order = await self.make_order(chat_id, account_id, client_id, decision, reply, said, new, order)
        pay = None
        if decision.handoff and decision.handoff["reason"] == "payment" and order and order["status"] == "awaiting":
            await sales.claim(self.db, order["id"])   # клиент говорит, что оплатил — владелец проверяет
            pay = order
        failed_order = bool(decision.order and not decision.handoff and not order)
        reason = decision.handoff["reason"] if decision.handoff else "payment" if failed_order else None
        defer = bool(reason) and reason != "bot_question" and night.is_night(now)
        if defer:   # ночью без Макса не решить — сразу «ответим утром», без ночных пингов (09.10)
            reply = night.NIGHT_PAY_REPLY if pay else night.DEFER_REPLY

        text = brain.render_reply(reply, price, first, new, personal, gift_list) if reply or first else ""
        if text and not first and brain.is_duplicate(text, await self.recent_out(chat_id)):
            log.info("%s: ответ почти дословно повторяет недавнее сообщение — не отправляю", chat_id)
            text = ""
        if text:
            base = client[-1]["created_at"] if client else now
            send_after = max(now, base + timedelta(seconds=random.uniform(*REPLY_DELAY)))
            await self.db.execute(
                "INSERT INTO gateway.outbox (account_id, chat_id, text, send_after) VALUES ($1,$2,$3,$4)",
                account_id, chat_id, text, send_after)
        await self.db.execute("UPDATE dialog.chats SET stage=$2, updated_at=now() WHERE chat_id=$1",
                              chat_id, decision.stage)
        log.info("%s: ответ готов%s", chat_id, f", передаю человеку ({decision.handoff['reason']})"
                 if decision.handoff else "")
        if decision.handoff:
            await self.handoff(chat_id, account_id, decision.handoff["reason"], decision.handoff.get("summary"),
                               client_text, draft, history, stage=decision.stage, title=title,
                               bot_said=brain.render_reply(reply, "[прайс]") if reply else None, order=pay,
                               images=[u for e in client if (u := self.image_url(e))],
                               fyi=decision.handoff["reason"] == "bot_question")
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
        if not targets:
            return None
        if not (new and o["product"] == "console"):
            rows = {m: {**r, "gifts": 0, "raffle": False} for m, r in rows.items()}
        cut = (sales.DISCOUNT if new and o["product"] not in sales.NO_DISCOUNT
               and not brain.DISCOUNT_DECLINE.search(said) else 0)
        return brain.upsell_text(o["months"], rows, targets, cut)

    async def make_order(self, chat_id, account_id, client_id, decision, reply: str, said: str, new: bool, order):
        """Клиент выбрал тип и срок — заказ и реквизиты в ответ. Цена проверяется по прайсу; банк — тот же, что уже
        давали в этом чате (09.10: второй счёт ушёл с другим банком), иначе следующий по кругу."""
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
            await alerts.send_alert(self.db, f"{alerts.TAG_PAY} · ⚠️ Сегодня уже {n}-я оплата: {sales.ROUNDS} круга "
                                             f"по банкам пройдены. Заказ №{order['id']}, банк {order['bank']}.\n"
                                             f"Чат: {alerts.chat_url(chat_id)}")
        # reply — уже после guard_reply (раньше брали сырой ответ LLM, и «реквизиты те же» уходило с новым банком);
        # «Оплачиваете?» перед реквизитами не к месту — реквизиты уже тут (прогон 10.10)
        reply = " ".join(s for s in brain._sentences(reply) if not brain.ASK_GO.search(s)) if reply else reply
        text = "\n\n".join(x for x in (reply, sales.pay_text(order), sales.PAY_AFTER[order["method"]]) if x)
        return text, order

    async def alfa_order(self, order):
        """Заказ на ИП → заказ в шлюзе Альфа-Банка и ссылка СБП. Номер в шлюзе уникален и после восстановления БД."""
        pay_id, _ = await self.alfa.register(f"a{order['id']}-{int(order['created_at'].timestamp())}",
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
        at_night = night.is_night(datetime.now(timezone.utc))
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
            kb = alerts.issued_keyboard(hid, order["id"] if order["product"] == "console" else None, order["slot"])
            tail = "Чат ведёт бот. Пришлёт клиент код или попросит код с почты — придёт уведомление."
        what = f"{sales.NAMES.get(order['product'], order['product'])} {order['months']} мес"
        head = (f"{alerts.TAG_PAY} · Оплата на ИП пришла — проверено банком автоматически"
                + (" · 🧪 ТЕСТ" if order["test"] else "") + f"\nСумма: {order['amount']} ₽, заказ №{order['id']}: {what}")
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
        if night.is_night(now):
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
        no_bot_since — без сообщений бота с этого момента («уточню и напишу», «нужно время» — не ответ человека)."""
        rows = await self.db.fetch(
            "SELECT origin, text, type FROM (SELECT * FROM gateway.messages WHERE chat_id=$1 AND origin <> 'system' "
            "AND deleted_at IS NULL AND text IS DISTINCT FROM $3 "
            "AND NOT (origin='bot' AND created_at >= coalesce($4::timestamptz, 'infinity')) "
            "ORDER BY created_at DESC LIMIT $2) t ORDER BY created_at", chat_id, HISTORY, DELETED_TEXT, no_bot_since)
        return [("client" if r["origin"] == "client" else "seller", r["text"] or f"<{r['type']}>") for r in rows]

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
        if chat["state"] != "human":
            log.info("%s: Макс пишет в чате сам — бот молчит", chat_id)

    async def think(self, history, title, price, personal, resumed: bool, new: bool,
                    order: dict | None = None, hint: str = "") -> tuple[brain.Decision, str | None]:
        """Возвращает решение и черновик бота (если ответ LLM заблокирован — его покажем Максу)."""
        lessons = await self.db.fetch(
            "SELECT reason, summary, client_text, owner_answer FROM dialog.handoffs WHERE owner_answer IS NOT NULL "
            "ORDER BY answered_at DESC LIMIT $1", LESSONS)
        rules = lesson_rules.rules_block(await lesson_rules.active_rules(self.db))   # из вмешательств Макса
        # Доп. разделы базы знаний: сразу по ключевым словам клиента, иначе — если LLM сама попросит
        recent = " ".join(t for who, t in history[-3:] if who == "client")
        classes = [c for c in route(recent) if self.extra.get(c)]
        looked: dict[str, list[dict]] = {}   # каталог Game Pass: запрос LLM → найденные игры
        gp_stats, games = await gamepass.stats(self.db), None

        def build(cls: list[str]) -> list[dict]:
            system = (self.system + "".join(brain.extra_block(c, self.extra[c]) for c in cls) +
                      brain.lessons_block([dict(r) for r in lessons]) + rules)
            return [{"role": "system", "content": system},
                    {"role": "user", "content": brain.user_prompt(price, title, history, new, order, personal,
                                                                  gamepass.block(gp_stats, looked)) +
                     (brain.RESUMED_NOTE if resumed else "") + hint}]

        messages = build(classes)
        _, prices = await catalog.load(self.db)
        allowed = {p for rows in prices.values() for p in rows.values()}
        discountable = {p for code, rows in prices.items() if code not in sales.NO_DISCOUNT for p in rows.values()}
        for attempt in range(2):
            raw = await self.llm.chat(messages, json_mode=True, temperature=0.3, max_tokens=1500)
            decision = brain.parse_decision(raw)
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
                    log.info("каталог Game Pass: %s", {g: len(looked[g]) for g in want})
                messages = build(classes)
                raw = await self.llm.chat(messages, json_mode=True, temperature=0.3, max_tokens=1500)
                decision = brain.parse_decision(raw)
            bad = brain.foreign_prices(decision.reply, allowed, new, discountable)
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
        if bad:
            return brain.Decision("Сейчас уточню и вернусь к Вам 🙏",
                                  {"reason": "nonstandard", "summary": f"бот хотел назвать цену не из прайса: {bad}"},
                                  decision.stage), brain.render_reply(decision.reply, price)
        decision.found = looked
        return decision, None

    async def open_question(self, history: list[tuple[str, str]]) -> bool:
        """Ждёт ли клиент ответа (иначе «нужно время» / «ответим утром» и пинги не нужны)."""
        quick = brain.needs_answer(history)
        if quick is not None:
            return quick
        text = "\n".join(f"{'Клиент' if who == 'client' else 'Продавец'}: {t}" for who, t in history[-12:])
        try:
            raw = await self.llm.chat([{"role": "system", "content": brain.OPEN_Q}, {"role": "user", "content": text}],
                                      json_mode=True, temperature=0, max_tokens=20)
            return bool(json.loads(raw).get("open", True))
        except Exception:
            log.exception("не понял, ждёт ли клиент ответа — считаю, что ждёт")
            return True

    async def wait_watch(self, now: datetime) -> None:
        """Чаты у человека: Макс писал сам и 10 мин молчит — снова у бота (клиент ждёт — бот отвечает); остальные —
        8 ч без Макса; клиент ждёт — пинги, «нужно время», ночью «ответим утром»."""
        for c in await self.db.fetch(
                "UPDATE dialog.chats c SET state='bot', handoff_reason=NULL, updated_at=now() WHERE state='human' "
                "AND handoff_reason='owner' AND owner_at < now() - $1::interval AND NOT EXISTS (SELECT 1 FROM "
                "gateway.messages m WHERE m.chat_id=c.chat_id AND m.origin='owner' AND m.created_at > now() - $1::interval) "
                "AND EXISTS (SELECT 1 FROM gateway.accounts a WHERE a.user_id=c.account_id AND a.started_at IS NOT NULL) "
                f"AND {NOT_IGNORED} RETURNING chat_id, account_id", night.OWNER_IDLE):
            await self.db.execute("UPDATE dialog.handoffs SET status='resumed' WHERE chat_id=$1 "
                                  "AND status IN ('open','info')", c["chat_id"])
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
            log.info("%s: 8 часов без ответа человека — чат снова у бота", c["chat_id"])

        for c in await self.db.fetch(
                "SELECT c.*, (SELECT max(created_at) FROM gateway.messages m WHERE m.chat_id=c.chat_id "
                "AND m.origin='owner') AS owner_msg FROM dialog.chats c "
                "JOIN gateway.accounts a ON a.user_id=c.account_id AND a.started_at IS NOT NULL "   # стоп — молчим
                f"WHERE c.state='human' AND {NOT_IGNORED}"):
            answered = max((x for x in (c["owner_at"], c["owner_msg"]) if x), default=None)
            first = await self.db.fetchval(
                "SELECT min(created_at) FROM gateway.messages WHERE chat_id=$1 AND origin='client' "
                "AND deleted_at IS NULL AND ($2::timestamptz IS NULL OR created_at > $2)", c["chat_id"], answered)
            if not first:
                continue   # Макс ответил на всё — ждать нечего
            since = max(x for x in (first, c["handoff_at"]) if x)
            kind = "owner" if c["handoff_reason"] == "owner" else "handoff"
            action = night.wait_action(kind, since, now, c["wait_pinged_at"], c["wait_nudged_at"],
                                       c["wait_deferred_at"])
            if not action:
                continue
            history = await self.history(c["chat_id"], no_bot_since=since)
            if not await self.open_question(history):   # поблагодарил, всё решили — ни клиенту, ни Максу
                await self.db.execute("UPDATE dialog.chats SET wait_pinged_at=$2, wait_nudged_at=$2, "
                                      "wait_deferred_at=$2 WHERE chat_id=$1", c["chat_id"], now)
                continue
            mins = int((now - since).total_seconds() // 60)
            waited = f"{mins // 60} ч {mins % 60} мин" if mins >= 60 else f"{mins} мин"
            if action == "ping":
                await self.db.execute("UPDATE dialog.chats SET wait_pinged_at=$2 WHERE chat_id=$1", c["chat_id"], now)
                tail = ("Через 10 минут попрошу клиента подождать до утра." if night.is_night(now) and kind != "owner"
                        else "Напомню через час.")
            else:
                reply = night.NEED_TIME_REPLY if action == "nudge" else night.DEFER_REPLY
                col = "wait_nudged_at" if action == "nudge" else "wait_deferred_at"
                await self.db.execute(f"UPDATE dialog.chats SET {col}=$2, wait_pinged_at=$2 WHERE chat_id=$1",
                                      c["chat_id"], now)
                await self.db.execute("INSERT INTO gateway.outbox (account_id, chat_id, text) VALUES ($1,$2,$3)",
                                      c["account_id"], c["chat_id"], reply)
                if action == "defer":
                    await self.db.execute("UPDATE dialog.handoffs SET status='deferred', deferred_at=now() "
                                          "WHERE chat_id=$1 AND status='open'", c["chat_id"])
                tail = f"Клиенту написал: «{reply}»" + ("" if action == "defer" else " Напомню через час.")
                log.info("%s: клиент ждёт %s — клиенту «%s»", c["chat_id"], waited, action)
            client_text = " / ".join(t for who, t in history[-5:] if who == "client")
            await self.handoff(c["chat_id"], c["account_id"], c["handoff_reason"] or "nonstandard", None, client_text,
                               None, history, notify_only=True, head=f"⏰ Клиент ждёт ответа {waited}\n{tail}")

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
                "AND m.created_at BETWEEN now() - interval '30 minutes' AND now() - $1::interval", DISCOUNT_AFTER):
            price, _ = await catalog.price_parts(self.db, gifts=True)
            if (not brain.shows_price(c["text"], price) or not await crm.is_new(self.db, c["client_id"])
                    or await sales.open_order(self.db, c["chat_id"])):
                continue
            await self.db.execute("UPDATE dialog.chats SET discount_nudged_at=now() WHERE chat_id=$1", c["chat_id"])
            await self.db.execute("INSERT INTO gateway.outbox (account_id, chat_id, text) VALUES ($1,$2,$3)",
                                  c["account_id"], c["chat_id"], brain.DISCOUNT_NUDGE)
            log.info("%s: молчит после прайса — напомнил о скидке новым", c["chat_id"])

    async def night_watch(self) -> None:
        """Ожидание человека (wait_watch), скидка молчащим после прайса, утренняя сводка за ночь."""
        now = datetime.now(timezone.utc)
        await self.wait_watch(now)
        await self.discount_watch()
        if not night.is_night(now):   # правила, выученные ночью, — утром списком в VK
            await lesson_rules.announce(self.db, lambda text, kb: alerts.send_alert(self.db, text, kb))
        local = now.astimezone(night.TZ)
        if night.is_night(now) or local.hour >= 12 or await self.db.fetchval(
                "SELECT 1 FROM dialog.digests WHERE day=$1", local.date()):
            return
        start, end = night.last_night(now)
        writers = await self.db.fetchval("SELECT count(DISTINCT chat_id) FROM gateway.messages "
                                         "WHERE origin='client' AND created_at BETWEEN $1 AND $2", start, end)
        cases = await self.db.fetch("SELECT * FROM dialog.handoffs WHERE status <> 'info' "
                                    "AND created_at BETWEEN $1 AND $2 ORDER BY id", start, end)
        await self.db.execute("INSERT INTO dialog.digests (day) VALUES ($1) ON CONFLICT DO NOTHING", local.date())
        if not writers:
            return
        waiting = [c for c in cases if c["status"] in ("open", "deferred")]
        lines = [f"{alerts.TAG_INFO} · ☀️ Сводка за ночь ({night.DAY_END}:00–{night.DAY_START}:00)",
                 f"Писали клиентов: {writers}. Бот справился сам: {max(writers - len(cases), 0)}.", ""]
        if waiting:
            lines.append(f"⏳ Ждут ответа ({len(waiting)}):")
            for i, c in enumerate(waiting, 1):
                lines += [f"{i}. {brain.REASONS.get(c['reason'], c['reason'])} — {c['summary'] or '—'}",
                          f"   Клиент: «{(brain.readable(c['client_text']) or '—')[:200]}»",
                          f"   {alerts.chat_url(c['chat_id'])}"]
        solved = len(cases) - len(waiting)
        if solved:
            lines += ["", f"✅ Уже решено ночью: {solved}"]
        if not waiting:
            lines += ["", "Все вопросы закрыты 👍"]
        await alerts.send_alert(self.db, "\n".join(lines))

    async def set_state(self, chat_id: str, state: str, reason: str | None) -> None:
        await self.db.execute("UPDATE dialog.chats SET state=$2, handoff_reason=$3, handoff_at=now(), updated_at=now() "
                              "WHERE chat_id=$1", chat_id, state, reason)

    async def handoff(self, chat_id, account_id, reason, summary, client_text, draft, history, *,
                      stage=None, title=None, bot_said=None, notify_only=False, order=None,
                      images: list[str] | None = None, fyi: bool = False, head: str | None = None) -> None:
        """Записывает спорный случай и шлёт алерт в VK с кнопками ответа.
        fyi — только уведомление («вы бот?»): бот отшутился и ведёт чат дальше, человек может забрать чат.
        head — свой заголовок уведомления (notify_only: клиент ждёт ответа)."""
        if not notify_only and not fyi:  # в чате один открытый случай: новый заменяет старые
            await self.db.execute("UPDATE dialog.handoffs SET status='superseded' WHERE chat_id=$1 "
                                  "AND status IN ('open','info')", chat_id)
        hid = await self.db.fetchval(
            "INSERT INTO dialog.handoffs (chat_id, account_id, reason, summary, client_text, context, bot_draft, status) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id", chat_id, account_id, reason, summary, client_text,
            json.dumps(history[-10:], ensure_ascii=False), draft, "info" if notify_only or fyi else "open")
        paid = await sales.last_paid(self.db, chat_id) if reason == "code" else None
        if fyi:
            head = f"{alerts.TAG_INFO} · Клиент спрашивает, не бот ли — бот отшутился и ведёт чат дальше"
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
        lines = [head, f"Объявление: {title}" if title else None, f"Этап: {stage}" if stage else None,
                 f"Клиент: «{shown[:400]}»" if (shown := brain.readable(client_text, bool(images))) else None,
                 f"Суть: {summary}" if summary else None,
                 f"Бот уже ответил: «{bot_said[:300]}»" if bot_said else None,
                 f"Бот предлагал ответить: «{draft[:600]}»" if draft else None,
                 "", "Ответ — кнопкой, свайпом на это сообщение или прямо в Avito (бот молчит и вернётся через "
                     "10 мин после Вашего последнего сообщения).",
                 f"Чат: {alerts.chat_url(chat_id)}"]
        if paid and paid["stock_id"]:   # вход по коду: данные учётки под рукой, инструкция — кнопкой
            lines.insert(-2, f"Учётка №{paid['stock_id']}" + (f", место {paid['slot']}" if paid["slot"] else "") + ":\n"
                         + "\n".join(x for x in (paid["email"], paid["password"], paid["recovery_email"]
                                                 and f"резервная: {paid['recovery_email']}") if x))
        kb = (alerts.payment_keyboard(hid) if order else alerts.fyi_keyboard(hid) if fyi else
              alerts.code_keyboard(hid, paid["id"] if paid and paid["stock_id"] and paid["product"] == "console"
                                   else None) if paid else alerts.handoff_keyboard(hid, bool(draft)))
        await alerts.send_alert(self.db, "\n".join(x for x in lines if x is not None), kb, hid, images)


async def main() -> None:
    db = await connect(catalog.SCHEMA + gamepass.SCHEMA + SCHEMA + alerts.SCHEMA + crm.SCHEMA + sales.SCHEMA +
                       lesson_rules.SCHEMA)
    await alerts.sync_subscribers(db)
    await single_instance(db, "dialog")
    if not await gamepass.stats(db):   # первый запуск: не ждём ночного обновления каталога игр
        try:
            log.info("каталог Game Pass загружен: %d игр", await gamepass.refresh(db))
        except Exception:
            log.exception("каталог Game Pass не загрузился — о наличии игр бот будет передавать человеку")
    llm = LLM()
    d = Dialog(db, llm)
    log.info("dialog запущен: core %d символов, доп. разделы %s", len(d.system),
             {c: len(t) for c, t in d.extra.items()})
    last_watch = last_pay = 0.0
    try:
        while True:
            try:
                await d.tick()
                if d.alfa and (t := asyncio.get_running_loop().time()) - last_pay >= 5:
                    last_pay = t
                    await d.pay_watch()
                if (t := asyncio.get_running_loop().time()) - last_watch > 30:
                    last_watch = t
                    await d.night_watch()
            except Exception:
                log.exception("ошибка цикла")
            await asyncio.sleep(1)
    finally:
        await llm.close()
        if d.alfa:
            await d.alfa.close()
        db.terminate()  # одно соединение держит блокировку «единственной копии»


if __name__ == "__main__":
    asyncio.run(main())
