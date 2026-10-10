"""owner-bot: бот сообщества VK для Макса (owner) и Леонида (admin).

Запуск: python -m services.owner_bot.main
Всё через кнопки: показать прайс, изменить прайс (скопировал → поправил → прислал → подтвердил).
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from services.broadcast.worker import SCHEMA as BROADCAST_SCHEMA
from services.dialog import lessons as lesson_rules
from services.dialog.worker import SCHEMA as DIALOG_SCHEMA
from services.owner_bot.broadcast_flow import BroadcastFlow
from shared import alerts, catalog, sales
from shared.config import env
from shared.db import connect, single_instance
from shared.llm import LLM
from shared.vk import VK, button, keyboard, payload_cmd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("owner_bot")

MENU = keyboard([
    [button("📋 Показать прайс", "price_show", "primary")],
    [button("✏️ Изменить прайс", "price_edit", "positive")],
    [button("📣 Рассылки", "bc_menu", "primary"), button("🎁 Подарки", "gift_menu", "primary")],
    [button("📦 Аккаунты", "stock_menu", "primary"), button("💳 Способ оплаты", "pay_menu", "primary")],
    [button("⛔ Стоп Агент", "agent_stop", "negative"), button("▶️ Старт Агент", "agent_start", "positive")],
    [button("🔔 Алерты вкл/выкл", "alerts"), button("❓ Помощь", "help")],
])
CANCEL = keyboard([[button("❌ Отмена", "cancel", "negative")]])
CONFIRM = keyboard([[button("✅ Сохранить", "price_save", "positive"), button("❌ Отмена", "cancel", "negative")]],
                   inline=True)

HELP = """Это пульт управления ботом GameNonStop.

📋 Показать прайс — пришлю цены, которые сейчас называет бот клиентам.
✏️ Изменить прайс — пришлю прайс и объясню, как его поправить.
📣 Рассылки — написать клиентам: описываете словами кому, присылаете текст, бот сам рассылает понемногу (до 40 в день).
🎁 Подарки — список игр в подарок (бот присылает клиенту на выбор) и игры для розыгрыша.
📦 Аккаунты — склад учёток: сколько свободно, добавить, списать. На консольную учётку — два клиента.
💳 Способ оплаты — микс (до 6 мес на ИП, от 8 мес на карты), только ИП или только карты. На ИП бот сам видит оплату по ссылке СБП и выдаёт учётку.
⛔ Стоп Агент — бот сразу перестаёт отвечать в Avito (рассылки и автопостинг работают). ▶️ Старт Агент — включить снова.
🔔 Алерты — включить/выключить уведомления «нужен человек» (оплата, вопросы, картинки).

Когда боту нужен человек (оплата, скидка, спорный вопрос), придёт уведомление с кнопками:
✍️ Ответить клиенту — напишите ответ здесь, я сразу отправлю его в Avito;
✅ Отправить вариант бота — если бот предложил хороший ответ;
▶️ Пусть бот ответит сам — бот ответит и продолжит диалог.
Можно просто ответить на уведомление (свайп влево) — это тоже уйдёт клиенту.
Ваши ответы бот запоминает и в похожих случаях дальше отвечает так же сам.

Кнопки внизу экрана. Если их не видно — нажмите на значок ⌨ рядом с полем ввода."""

EDIT_STEPS = """✏️ Меняем прайс. Следующим сообщением пришлю текущий прайс.

1️⃣ Нажмите на сообщение с прайсом и держите палец → «Копировать».
2️⃣ Нажмите на поле ввода внизу → «Вставить».
3️⃣ Поменяйте нужные цифры. Всё остальное оставьте как есть.
   • убрать срок — сотрите эту строку целиком;
   • добавить срок — допишите строку так же: 9 мес — 2600₽
4️⃣ Отправьте мне.

Я покажу, что поменяется, и спрошу подтверждение. Пока не нажмёте «✅ Сохранить» — ничего не изменится.
Передумали — нажмите «❌ Отмена»."""

NOT_A_PRICE = """🤔 Не нашёл в сообщении цен.

Нужно прислать прайс целиком: скопируйте сообщение с прайсом выше, поменяйте цифры и отправьте.
Строки с ценами должны выглядеть так: 6 мес — 2290₽

Или нажмите «❌ Отмена»."""


@dataclass
class Session:
    mode: str = "menu"             # menu | await_price | await_confirm | await_reply
    changes: list = field(default_factory=list)
    handoff_id: int | None = None
    bc_filter_text: str | None = None   # рассылка в процессе создания
    bc_filter: object = None
    bc_audience: object = None
    bc_message: str | None = None
    bc_topic: str | None = None
    bc_topics: list = field(default_factory=list)
    bc_kind: str | None = None          # None — обычная; renew / invite / unfinished — из утренней сводки
    tpl: str | None = None              # «🎁 Подарки»: какой текст правим (catalog.TEMPLATES) и новый вариант
    tpl_text: str | None = None
    stock_rows: list = field(default_factory=list)   # «📦 Аккаунты»: разобранные строки до «Сохранить»
    rule_id: int | None = None          # ✏️ правило: какое правим и вариант по мысли владельца
    rule_text: str | None = None


class OwnerBot:
    def __init__(self, db, vk: VK):
        self.db, self.vk = db, vk
        self.sessions: dict[int, Session] = {}
        self.bc = None  # BroadcastFlow, подключается в main
        self.llm = None

    async def to_bot(self, chat_id: str, account_id: int, answer_now: bool) -> None:
        await self.db.execute("UPDATE dialog.chats SET state='bot', handoff_reason=NULL, updated_at=now() "
                              "WHERE chat_id=$1", chat_id)
        # чат снова у бота — висящие случаи закрыты, иначе ночью придёт повтор и «ответим утром»
        await self.db.execute("UPDATE dialog.handoffs SET status='resumed' WHERE chat_id=$1 "
                              "AND status IN ('open','info')", chat_id)
        if answer_now:  # dialog увидит событие и ответит на последнее сообщение клиента
            await self.db.execute("INSERT INTO gateway.events (account_id, chat_id, kind, payload) "
                                  "VALUES ($1,$2,'resume','{}')", account_id, chat_id)

    async def answer(self, say, h, text: str, who: str) -> None:
        """Ответ Макса из VK → сразу в Avito; случай сохраняется как пример для бота."""
        await self.db.execute("INSERT INTO gateway.outbox (account_id, chat_id, text) VALUES ($1,$2,$3)",
                              h["account_id"], h["chat_id"], text)
        await self.db.execute("UPDATE dialog.handoffs SET owner_answer=$2, answered_by=$3, answered_at=now(), "
                              "status='answered' WHERE id=$1", h["id"], text, who)
        await self.to_bot(h["chat_id"], h["account_id"], answer_now=False)
        await say("✅ Отправлено клиенту в Avito. Бот продолжит диалог сам и запомнит, как Вы ответили.",
                  keyboard([[button("⏸ Оставить чат мне", f"keep:{h['id']}")]], inline=True))

    async def payment(self, say, h, ok: bool, who: str) -> None:
        """Макс проверил поступление: пришла — выдаём аккаунт, не пришла — просим клиента проверить реквизиты."""
        order = await sales.open_order(self.db, h["chat_id"])
        if not order or order["status"] != "checking":
            return await say("Этот заказ уже обработан 👌", MENU)

        async def send(text: str) -> None:
            await self.db.execute("INSERT INTO gateway.outbox (account_id, chat_id, text) VALUES ($1,$2,$3)",
                                  h["account_id"], h["chat_id"], text)

        if not ok:
            await sales.reject(self.db, order["id"])
            await send(sales.PAY_NOT_FOUND.format(req=sales.pay_text(order)))
            await self.to_bot(h["chat_id"], h["account_id"], answer_now=False)
            return await say("↩️ Попросил клиента проверить реквизиты и оплату. Чат снова у бота.", MENU)
        order, stock, lines = await sales.settle(self.db, order["id"], who)
        await self.db.execute("UPDATE dialog.handoffs SET status='answered', answered_by=$2, answered_at=now() "
                              "WHERE id=$1", h["id"], who)
        if lines is None:   # оплату на ИП в эту же секунду подтвердил шлюз — клиенту уже всё отправлено
            return await say("Этот заказ уже обработан 👌", MENU)
        if stock:   # учётка выдана — дальше ведёт бот, человек нужен только для входа по коду
            await self.to_bot(h["chat_id"], h["account_id"], answer_now=False)
            lines.append("Чат ведёт бот. Пришлёт клиент код или попросит код с почты — придёт уведомление.")
            kb = alerts.issued_keyboard(h["id"], order["id"] if order["product"] == "console" else None, order["slot"])
        else:
            lines.append("Чат за Вами; когда закончите — «Вернуть боту».")
            kb = alerts.resume_keyboard(h["id"])
        await say("\n".join(lines), kb)

    async def on_message(self, msg: dict, who: str) -> None:
        peer, uid = msg["peer_id"], msg["from_id"]
        s = self.sessions.setdefault(uid, Session())
        cmd, text = payload_cmd(msg), (msg.get("text") or "").strip()
        say = lambda t, kb=None: self.vk.send(peer, t, kb)  # noqa: E731

        if cmd in ("cancel", "start") or text.lower() in ("отмена", "начать", "меню", "start"):
            self.sessions[uid] = Session()
            return await say("Хорошо, ничего не меняю. Выберите действие кнопкой внизу 👇" if cmd == "cancel"
                             else HELP, MENU)
        # ---- ответы по спорным ситуациям ----
        if cmd and ":" in cmd and cmd.split(":")[0] in ("reply", "draft", "resume", "keep", "payok", "payno"):
            action, hid = cmd.split(":", 1)
            h = await self.db.fetchrow("SELECT * FROM dialog.handoffs WHERE id=$1", int(hid))
            if not h:
                return await say("Не нашёл этот случай 🤷", MENU)
            if action in ("payok", "payno"):
                return await self.payment(say, h, action == "payok", who)
            if action == "reply":
                s.mode, s.handoff_id = "await_reply", h["id"]
                return await say(f"✍️ Напишите ответ клиенту одним сообщением — я сразу отправлю его в Avito.\n\n"
                                 f"Клиент писал: «{(h['client_text'] or '')[:300]}»", CANCEL)
            if action == "draft":
                if not h["bot_draft"]:
                    return await say("У бота не было своего варианта.", MENU)
                return await self.answer(say, h, h["bot_draft"], who)
            if action == "resume":
                await self.to_bot(h["chat_id"], h["account_id"], answer_now=True)
                await self.db.execute("UPDATE dialog.handoffs SET status='resumed', answered_by=$2, answered_at=now() "
                                      "WHERE id=$1 AND status='open'", h["id"], who)
                return await say("▶️ Хорошо, бот сейчас сам ответит клиенту и продолжит диалог.", MENU)
            if action == "keep":
                await self.db.execute("UPDATE dialog.chats SET state='human', owner_at=now(), updated_at=now() "
                                      "WHERE chat_id=$1", h["chat_id"])
                return await say("⏸ Чат за Вами, бот молчит. Новые сообщения клиента пришлю сюда — отвечайте прямо "
                                 "из VK или в Avito. Напишете в Avito — через 10 мин после Вашего последнего сообщения "
                                 "бот снова ведёт чат. Не пишете, а клиент ждёт час — напишу ему «нужно ещё немного "
                                 "времени» и напомню Вам; через 8 ч чат вернётся к боту.", MENU)
        swipe = (msg.get("reply_message") or {}).get("conversation_message_id")
        if swipe and text and not cmd:
            hid = await self.db.fetchval("SELECT handoff_id FROM dialog.alert_msgs WHERE vk_peer=$1 AND conv_msg_id=$2",
                                         peer, swipe)
            if hid:
                h = await self.db.fetchrow("SELECT * FROM dialog.handoffs WHERE id=$1", hid)
                return await self.answer(say, h, text, who)
        if s.mode == "await_reply" and text and not cmd:
            h = await self.db.fetchrow("SELECT * FROM dialog.handoffs WHERE id=$1", s.handoff_id)
            self.sessions[uid] = Session()
            return await self.answer(say, h, text, who)
        # ---- правила, выведенные ночью из ответов владельца (services/dialog/lessons.py) ----
        if cmd and cmd.startswith("rule_del:"):
            rule = await self.db.fetchval("UPDATE dialog.rules SET active=false, removed_by=$2 WHERE id=$1 "
                                          "AND removed_by IS NULL RETURNING rule", int(cmd[9:]), who)
            return await say(f"🗑 Убрал правило, бот им не пользуется:\n«{rule}»" if rule else
                             "Это правило уже убрано 👌", MENU)
        if cmd and cmd.startswith("rule_ok:"):
            ids = [int(x) for x in cmd[8:].split(",") if x.isdigit()]
            rules = await self.db.fetch("UPDATE dialog.rules SET active=true, approved_by=$2 WHERE id = ANY($1::bigint[]) "
                                        "AND removed_by IS NULL AND NOT active RETURNING rule", ids, who)
            return await say("✅ Включил, бот уже пользуется:\n" + "\n".join(f"• {r['rule']}" for r in rules)
                             if rules else "Уже решено 👌", MENU)
        if cmd and cmd.startswith("rule_edit:"):
            rule = await self.db.fetchval("SELECT rule FROM dialog.rules WHERE id=$1 AND removed_by IS NULL",
                                          int(cmd[10:]))
            if not rule:
                return await say("Это правило уже убрано 👌", MENU)
            s.mode, s.rule_id, s.rule_text = "await_rule", int(cmd[10:]), None
            return await say(f"✏️ Бот понял так:\n«{rule}»\n\nНапишите своими словами, как надо на самом деле, — "
                             "переформулирую и покажу, что получилось.", CANCEL)
        if s.mode == "await_rule" and text and not cmd:
            old = await self.db.fetchval("SELECT rule FROM dialog.rules WHERE id=$1", s.rule_id)
            s.rule_text = await lesson_rules.rewrite(self.llm, old or "", text)
            return await say(f"Будет так:\n«{s.rule_text}»\n\nНе то — напишите ещё раз по-другому.", keyboard(
                [[button("✅ Сохранить", "rule_save", "positive"), button("❌ Отмена", "cancel", "negative")]],
                inline=True))
        if cmd == "rule_save":
            if s.mode != "await_rule" or not s.rule_text:
                return await say("Нечего сохранять 🤷", MENU)
            await self.db.execute("UPDATE dialog.rules SET rule=$2, active=true, approved_by=$3, removed_by=NULL "
                                  "WHERE id=$1", s.rule_id, s.rule_text, who)
            self.sessions[uid] = Session()
            return await say(f"✅ Сохранил и включил, бот уже пользуется:\n«{s.rule_text}»", MENU)
        if cmd == "alerts":
            on = await self.db.fetchval("UPDATE owner.subscribers SET alerts = NOT alerts WHERE vk_id=$1 "
                                        "RETURNING alerts", uid)
            return await say("🔔 Уведомления включены." if on else
                             "🔕 Уведомления выключены. Включить обратно — эта же кнопка.", MENU)
        if cmd == "help":
            return await say(HELP, MENU)
        if cmd == "price_show":
            return await say(f"📋 Текущий прайс (эти цены бот называет клиентам):\n\n"
                             f"{await catalog.price_text(self.db)}\n\nЧтобы поменять — «✏️ Изменить прайс».", MENU)
        if cmd == "price_edit":
            s.mode = "await_price"
            await say(EDIT_STEPS)
            return await say(await catalog.price_text(self.db), CANCEL)
        if cmd == "price_save":
            if s.mode != "await_confirm" or not s.changes:
                return await say("Нечего сохранять. Нажмите «✏️ Изменить прайс», чтобы начать заново.", MENU)
            await catalog.apply(self.db, s.changes, who)
            self.sessions[uid] = Session()
            return await say(f"✅ Сохранено! С этой минуты бот называет клиентам новые цены:\n\n"
                             f"{await catalog.price_text(self.db)}", MENU)

        # ---- аварийная остановка: бот не отвечает в Avito; рассылки (outbox.broadcast) и автопостинг идут ----
        if cmd == "agent_stop":
            await self.db.execute("UPDATE gateway.accounts SET started_at=NULL, stopped_at=now()")
            await self.db.execute("UPDATE gateway.events SET processed_at=now() WHERE processed_at IS NULL")
            await self.db.execute("UPDATE gateway.outbox SET status='cancelled', error='агент остановлен' "
                                  "WHERE status='pending' AND NOT broadcast")
            log.warning("агент остановлен: %s", who)
            await say("⛔ Готово.", MENU)
            return await alerts.send_alert(self.db, f"⛔ Агент остановлен ({who}): в Avito не отвечает, неотправленные "
                                                    "ответы отменены. Рассылки и автопостинг работают.\n"
                                                    "Включить — «▶️ Старт Агент».")
        if cmd == "agent_start":
            await self.db.execute("UPDATE gateway.accounts SET started_at=now()")
            log.warning("агент запущен: %s", who)
            await say("▶️ Готово.", MENU)
            return await alerts.send_alert(self.db, f"▶️ Агент запущен ({who}): отвечает на новые сообщения в Avito "
                                                    "(пришедшие во время остановки не трогает).")
        # ---- способ оплаты: микс / только ИП / только карты (по умолчанию карты); два шага — случайно не сменить ----
        if cmd in ("pay_menu", "ip_menu", "ip_on", "ip_off"):   # ip_* — кнопки прежней версии в старых сообщениях
            mode = await sales.pay_mode(self.db)
            return await say(sales.pay_mode_text(mode), keyboard(
                [[button(name, f"pay_set:{m}", "positive" if m == mode else "secondary")]
                 for m, (name, _) in sales.PAY_MODES.items()], inline=True))
        if cmd and cmd.startswith("pay_set:") and cmd[8:] in sales.PAY_MODES:
            mode = cmd[8:]
            if mode != "card" and not sales.ALFA_READY:
                return await say("⚠️ Не заданы логин и пароль Альфа-Банка (ALFA_USER / ALFA_PASSWORD) — "
                                 "оплата на ИП не работает, оставил как было.", MENU)
            await sales.set_pay_mode(self.db, mode, who)
            log.warning("способ оплаты: %s (%s)", mode, who)
            await say("Готово.", MENU)
            return await alerts.send_alert(self.db, f"{sales.pay_mode_text(mode)}\n\nИзменил: {who}")
        # ---- склад учёток ----
        if cmd and cmd.startswith("instr:"):   # владелец вошёл по коду клиента — инструкция 1-му или 2-му клиенту
            o = await self.db.fetchrow("SELECT * FROM sales.orders WHERE id=$1", int(cmd[6:]))
            if not o:
                return await say("Не нашёл заказ 🤷", MENU)
            await self.db.execute("INSERT INTO gateway.outbox (account_id, chat_id, text) VALUES ($1,$2,$3)",
                                  o["account_id"], o["chat_id"], sales.instruction(o["slot"], o["discount"] > 0))
            await self.db.execute("UPDATE sales.orders SET connected_at=coalesce(connected_at, now()) WHERE id=$1",
                                  o["id"])   # вошли по коду — клиент подключён (sales.issued)
            await self.to_bot(o["chat_id"], o["account_id"], answer_now=False)
            return await say(f"📨 Инструкция для {o['slot'] or 1}-го клиента отправлена, чат ведёт бот.", MENU)
        if cmd == "stock_menu":
            return await say(await sales.stock_text(self.db), keyboard(
                [[button("➕ Добавить", "stock_add", "positive"), button("🗑 Списать", "stock_off", "negative")]],
                inline=True))
        if cmd == "stock_add":
            s.mode, s.stock_rows = "await_stock_add", []
            return await say("➕ Пришлите учётки — по одной в строке:\n"
                             f"{sales.ACCOUNT_FORMAT}\n\nПример:\nmail@outlook.com Пароль123 reserve@mail.ru 6\n"
                             "mail2@outlook.com Пароль456 reserve2@mail.ru 4 01.10\n\n"
                             "Дата — когда активирована подписка (без даты — сегодня), с неё идёт срок. "
                             "Для ПК допишите в строку «пк». Покажу, что добавлю, и спрошу подтверждение.", CANCEL)
        if cmd == "stock_off":
            s.mode = "await_stock_off"
            return await say("🗑 Пришлите почты учёток, которые списать (по одной в строке). "
                             "Учётки с закончившейся подпиской списываются сами.", CANCEL)
        if s.mode == "await_stock_add" and text and not cmd:
            rows, errors = sales.parse_accounts(text, datetime.now(timezone.utc))
            warn = ("\n\n⚠ Не понял и пропущу:\n" + "\n".join(errors)) if errors else ""
            if not rows:
                return await say("Не нашёл учёток в сообщении." + warn, CANCEL)
            s.stock_rows = rows
            items = [f"• {sales.NAMES.get(r['product'], r['product'])} {r['months']} мес до "
                     f"{r['expires_at']:%d.%m.%Y}: {r['email']}" + (f", резервная {r['recovery_email']}"
                                                                    if r["recovery_email"] else ", без резервной")
                     for r in rows]
            return await say(f"Добавлю {len(rows)}:\n" + "\n".join(items) + warn, keyboard(
                [[button("✅ Сохранить", "stock_save", "positive"), button("❌ Отмена", "cancel", "negative")]],
                inline=True))
        if cmd == "stock_save":
            if s.mode != "await_stock_add" or not s.stock_rows:
                return await say("Нечего сохранять. Нажмите «📦 Аккаунты» → «➕ Добавить».", MENU)
            added, skipped = await sales.add_accounts(self.db, s.stock_rows, who)
            self.sessions[uid] = Session()
            note = f"\nУже есть на складе, пропустил: {', '.join(skipped)}" if skipped else ""
            return await say(f"✅ Добавлено учёток: {added}.{note}\n\n{await sales.stock_text(self.db)}", MENU)
        if s.mode == "await_stock_off" and text and not cmd:
            n = await sales.write_off(self.db, re.findall(r"[^@\s;,]+@[^@\s;,]+", text))
            self.sessions[uid] = Session()
            return await say(f"🗑 Списано учёток: {n}.\n\n{await sales.stock_text(self.db)}", MENU)

        # ---- подарки: список игр на выбор и игры для розыгрыша ----
        if cmd == "gift_menu":
            parts = [f"{title}:\n\n{await catalog.template(self.db, code) or '—'}"
                     for code, title in catalog.TEMPLATES.items()]
            return await say("\n\n— — —\n\n".join(parts), keyboard(
                [[button("✏️ " + t.split(" — ")[0][2:], f"tpl_edit:{c}")] for c, t in catalog.TEMPLATES.items()],
                inline=True))
        if cmd and cmd.startswith("tpl_edit:") and cmd[9:] in catalog.TEMPLATES:
            s.mode, s.tpl, s.tpl_text = "await_template", cmd[9:], None
            await say(f"✏️ {catalog.TEMPLATES[s.tpl]}.\n\nСкопируйте текст ниже, поправьте и пришлите целиком — "
                      "он заменит текущий. Пока не нажмёте «✅ Сохранить», ничего не изменится.")
            return await say(await catalog.template(self.db, s.tpl) or "—", CANCEL)
        if s.mode == "await_template" and text and not cmd:
            s.tpl_text = text
            return await say(f"Будет так:\n\n{text}", keyboard(
                [[button("✅ Сохранить", "tpl_save", "positive"), button("❌ Отмена", "cancel", "negative")]],
                inline=True))
        if cmd == "tpl_save":
            if s.mode != "await_template" or not s.tpl_text:
                return await say("Нечего сохранять. Нажмите «🎁 Подарки», чтобы начать заново.", MENU)
            await catalog.set_template(self.db, s.tpl, s.tpl_text, who)
            self.sessions[uid] = Session()
            return await say("✅ Сохранено — бот уже использует новый текст.", MENU)

        if self.bc and await self.bc.handle(s, cmd, text, say, who):
            return
        # обычный текст: ждём прайс — или прислали прайс без кнопки
        if s.mode in ("await_price", "await_confirm") or re.search(r"\d+\s*мес", text):
            if not re.search(r"\d+\s*мес", text):
                return await say(NOT_A_PRICE, CANCEL)
            changes, errors, products = await catalog.preview(self.db, text)
            warn = ("\n\n⚠ Эти строки я не понял и пропущу:\n" + "\n".join(errors) +
                    "\nЕсли они важны — исправьте и пришлите прайс ещё раз.") if errors else ""
            if not changes:
                s.mode = "await_price"
                return await say("Цены совпадают с текущими — менять нечего. "
                                 "Поменяйте цифры и пришлите ещё раз, или нажмите «❌ Отмена»." + warn, CANCEL)
            s.mode, s.changes = "await_confirm", changes
            return await say(f"Проверьте, что поменяется ({len(changes)}):\n\n"
                             f"{catalog.describe(changes, products)}{warn}\n\n"
                             "Всё верно? Нажмите «✅ Сохранить». Если нет — пришлите исправленный прайс "
                             "или нажмите «❌ Отмена».", CONFIRM)

        await say("Я понимаю только кнопки 🙂 Выберите действие внизу 👇", MENU)


async def main() -> None:
    db = await connect(catalog.SCHEMA + DIALOG_SCHEMA + alerts.SCHEMA + BROADCAST_SCHEMA + sales.SCHEMA)
    await alerts.sync_subscribers(db)
    await single_instance(db, "owner_bot")
    vk = VK(env("VK_GROUP_TOKEN"), int(env("VK_GROUP_ID")))
    vk.on_sent = alerts.remember(db)   # ответы бота тоже исчезают через сутки
    roles = await alerts.access(db)
    llm = LLM()
    bot = OwnerBot(db, vk)
    bot.bc, bot.llm = BroadcastFlow(db, llm, MENU), llm
    log.info("слушаю VK, доступ: %s", roles)

    async def sweeper() -> None:   # чат с ботом не копится: его сообщения старше суток удаляются
        while True:
            try:
                await alerts.sweep(db, vk)
            except Exception:
                log.exception("уборка старых сообщений")
            await asyncio.sleep(600)

    sweep_task = asyncio.create_task(sweeper())
    try:
        while True:  # любая неожиданная ошибка — переподключаемся, а не падаем
            try:
                async for msg in vk.listen():
                    uid = msg["from_id"]
                    if uid not in roles and uid not in (roles := await alerts.access(db)):  # список — в БД
                        continue
                    try:
                        await bot.on_message(msg, f"vk:{uid}")
                    except Exception as e:
                        log.exception("ошибка обработки")
                        try:
                            await vk.send(msg["peer_id"], f"😬 Что-то сломалось: {e}\nНичего не сохранено. "
                                                          "Напишите Леониду.", MENU)
                        except Exception:
                            log.exception("не смог отправить сообщение об ошибке")
            except Exception:
                log.exception("VK: цикл упал, перезапуск через 5 с")
                await asyncio.sleep(5)
    finally:
        sweep_task.cancel()
        await vk.close()
        db.terminate()  # не ждём: одно соединение держит блокировку «единственной копии»


if __name__ == "__main__":
    asyncio.run(main())
