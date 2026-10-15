"""Аудитория рассылки: текст Макса → фильтр (LLM) → понятное описание (код) → список получателей (SQL)."""
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import asyncpg

from services.dialog import night
from shared.config import env
from shared.kb_classes import CLASSES, classify_dialog
from shared.llm import LLM

DAILY_LIMIT = 40                    # сообщений рассылок в рабочий день, суммарно по всем рассылкам
CLIENT_COOLDOWN = 7                 # дней: одному клиенту — не чаще одной рассылки
ACTIVE_SKIP = timedelta(hours=24)   # любая переписка (клиент, Макс, бот) в любом чате клиента — не мешаем

INTERESTS = {"core": "подписка Game Pass", "games": "игры и ключи Xbox", "steam": "Steam"}

HINT = """📣 Новая рассылка. Напишите своими словами, КОМУ отправить.

Примеры:
— Кто писал про подписку за последние 5 дней, но не купил
— Кто покупал Game Pass больше 5 месяцев назад
— Все, кто интересовался Steam, не больше 100 человек
— Клиенту «Недвижимость МО» (конкретному человеку — по имени в Avito)

Можно указать: купили или нет; интерес (подписка / игры Xbox / Steam); когда писали; когда покупали;
слово из названия объявления; сколько человек максимум.

Текст рассылки пришлёте следующим шагом."""

PARSE_SYSTEM = """Ты переводишь описание аудитории рассылки (магазин подписок Xbox Game Pass, игр и ключей Xbox, Steam)
в JSON-фильтр. Поля (если в описании чего-то нет — null, не выдумывай):
{"bought": "yes" | "no" | null,                 // покупали ли у нас хоть что-то
 "interest": ["core","games","steam"] | null,   // core — подписка Game Pass, games — игры/ключи Xbox, steam — Steam
 "wrote_within_days": int | null,               // писали нам за последние N дней
 "silent_for_days": int | null,                 // не писали нам N дней и больше
 "bought_within_days": int | null,              // (только если покупали) последняя покупка не раньше N дней назад
 "bought_before_days": int | null,              // (только если покупали) последняя покупка N дней назад и раньше
 "item_contains": string | null,                // слово из НАЗВАНИЯ ОБЪЯВЛЕНИЯ
 "client_name": string | null,                  // имя/ник конкретного клиента в Avito («клиенту Иван», «нику X»)
 "max_recipients": int | null,
 "not_understood": string | null}               // часть описания про АУДИТОРИЮ, которую нельзя выразить полями
Правила: месяц = 30 дней. «Не купил», «не довёл покупку/оплату до конца», «спрашивал, но не взял» за N дней =
bought "no" + wrote_within_days N (НЕ bought_within_days). «Пользователю/клиенту/нику/человеку X», «ник X» —
это client_name X, а не объявление. Пожелания к тексту рассылки («пригласи в сообщество»)
игнорируй — это не аудитория. Верни только JSON."""


@dataclass
class Filter:
    bought: str | None = None
    interest: list[str] = field(default_factory=list)
    wrote_within_days: int | None = None
    silent_for_days: int | None = None
    bought_within_days: int | None = None
    bought_before_days: int | None = None
    item_contains: str | None = None
    client_name: str | None = None
    max_recipients: int | None = None
    not_understood: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Filter":
        def days(v):
            return max(0, min(int(v), 3650)) if isinstance(v, (int, float)) else None

        def text(v):
            return (str(v).strip()[:50] or None) if v else None
        bought = d.get("bought") if d.get("bought") in ("yes", "no") else None
        f = cls(
            bought=bought,
            interest=[i for i in (d.get("interest") or []) if i in CLASSES],
            wrote_within_days=days(d.get("wrote_within_days")), silent_for_days=days(d.get("silent_for_days")),
            bought_within_days=days(d.get("bought_within_days")),
            bought_before_days=days(d.get("bought_before_days")),
            item_contains=text(d.get("item_contains")), client_name=text(d.get("client_name")),
            max_recipients=max(1, int(d["max_recipients"])) if isinstance(d.get("max_recipients"), (int, float))
            else None,
            not_understood=d.get("not_understood") or None)
        if f.bought == "no":  # «не покупал» + «когда покупал» — противоречие, дата покупки не имеет смысла
            f.bought_within_days = f.bought_before_days = None
        return f

    def is_empty(self) -> bool:
        return not any([self.bought, self.interest, self.wrote_within_days, self.silent_for_days,
                        self.bought_within_days, self.bought_before_days, self.item_contains, self.client_name,
                        self.max_recipients])

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


async def parse(llm: LLM, text: str) -> Filter:
    return Filter.from_dict(await llm.chat_json(PARSE_SYSTEM, text, temperature=0, max_tokens=400))


def describe(f: Filter) -> str:
    """Пересказ фильтра словами — делает код, не LLM, чтобы Макс видел ровно то, что будет выполнено."""
    lines = []
    if f.client_name:
        lines.append(f"• клиент, у которого в имени есть «{f.client_name}»")
    if f.bought == "yes":
        lines.append("• уже покупали у нас")
    elif f.bought == "no":
        lines.append("• ещё ничего не покупали")
    if f.interest:
        lines.append("• интересовались: " + ", ".join(INTERESTS[i] for i in f.interest))
    if f.wrote_within_days is not None:
        lines.append(f"• писали нам за последние {f.wrote_within_days} дн.")
    if f.silent_for_days is not None:
        lines.append(f"• не писали нам {f.silent_for_days} дн. и больше")
    if f.bought_within_days is not None:
        lines.append(f"• покупали не раньше чем {f.bought_within_days} дн. назад")
    if f.bought_before_days is not None:
        lines.append(f"• покупали {f.bought_before_days} дн. назад и раньше")
    if f.item_contains:
        lines.append(f"• писали по объявлению, где есть «{f.item_contains}»")
    if f.max_recipients:
        lines.append(f"• не больше {f.max_recipients} человек")
    return "\n".join(lines) or "• все клиенты"


@dataclass
class Audience:
    rows: list[tuple[int, str]]          # (client_id, chat_id) в случайном порядке
    matched: int                         # сколько клиентов подходит под фильтр (до правила круга)
    round_no: int                        # номер круга по этой теме
    done_in_round: int                   # сколько подходящих уже получили тему в этом круге


def round_pick(candidates: list[int], got: dict[int, int]) -> tuple[list[int], int, int]:
    """Круг по теме: берём тех, кто получал эту тему меньше всех. Пока круг не пройден — повторов нет."""
    if not candidates:
        return [], 1, 0
    least = min(got.get(c, 0) for c in candidates)
    picked = [c for c in candidates if got.get(c, 0) == least]
    return picked, least + 1, len(candidates) - len(picked)


async def select(db: asyncpg.Pool, f: Filter, topic: str | None = None, now: datetime | None = None) -> Audience:
    """topic=None — просто посчитать подходящих (правило круга ещё не применяем)."""
    now = now or datetime.now(timezone.utc)
    rows = await db.fetch("""
        SELECT ch.client_id, ch.id AS chat_id, ch.item_title, ch.client_name,
               max(m.created_at) FILTER (WHERE m.origin = 'client') AS last_client, max(m.created_at) AS last_any
        FROM gateway.chats ch JOIN gateway.messages m ON m.chat_id = ch.id
        WHERE ch.client_id IS NOT NULL AND ch.client_id NOT IN (SELECT client_id FROM gateway.ignored) GROUP BY ch.client_id, ch.id, ch.item_title, ch.client_name
        HAVING count(*) FILTER (WHERE m.origin = 'client') > 0""")
    customers = {r["avito_user_id"]: (r["purchased_at"], r["name"])
                 for r in await db.fetch("SELECT avito_user_id, purchased_at, name FROM crm.customers")}
    # последняя покупка и её чат: даты «когда покупал» считаем от неё, рассылку шлём в этот чат
    paid = {r["client_id"]: (r["paid_at"], r["chat_id"]) for r in await db.fetch(
        "SELECT DISTINCT ON (client_id) client_id, paid_at, chat_id FROM crm.purchases "
        "WHERE coalesce(product, '?') <> 'none' ORDER BY client_id, paid_at DESC")}   # none — поддержка, не покупка
    got = {} if topic is None else {r["client_id"]: r["n"] for r in await db.fetch(
        "SELECT r.client_id, count(*) AS n FROM broadcast.recipients r JOIN broadcast.campaigns c ON c.id=r.campaign_id "
        "WHERE lower(c.topic)=lower($1) AND r.status IN ('sent','queued') GROUP BY r.client_id", topic)}

    clients: dict[int, dict] = {}
    for r in rows:
        c = clients.setdefault(r["client_id"], {"chats": [], "classes": set(), "titles": [], "names": set(),
                                                "last_any": r["last_any"]})
        c["chats"].append((r["last_client"], r["chat_id"]))
        c["last_any"] = max(c["last_any"], r["last_any"])
        c["classes"].add(classify_dialog(r["item_title"], []))
        c["titles"].append((r["item_title"] or "").lower())
        c["names"].add((r["client_name"] or "").lower())

    def ok(cid: int, c: dict) -> bool:
        last = max(c["chats"])[0]
        first, name = customers.get(cid, (None, None))
        bought = paid[cid][0] if cid in paid else first   # последняя покупка (если не разобрана — первая)
        if f.client_name:  # конкретный человек: Макс явно хочет написать ему — «живой диалог» не проверяем
            if not any(f.client_name.lower() in n for n in c["names"] | {(name or "").lower()}):
                return False
        elif now - c["last_any"] < ACTIVE_SKIP:
            return False
        if f.bought == "yes" and not bought or f.bought == "no" and bought:
            return False
        if f.interest and not c["classes"] & set(f.interest):
            return False
        if f.wrote_within_days is not None and now - last > timedelta(days=f.wrote_within_days):
            return False
        if f.silent_for_days is not None and now - last < timedelta(days=f.silent_for_days):
            return False
        if f.bought_within_days is not None and (not bought or now - bought > timedelta(days=f.bought_within_days)):
            return False
        if f.bought_before_days is not None and (not bought or now - bought < timedelta(days=f.bought_before_days)):
            return False
        if f.item_contains and not any(f.item_contains.lower() in t for t in c["titles"]):
            return False
        return True

    def chat_of(cid: int) -> str:
        """Чат, где была последняя покупка; не покупал — последний чат, где писал."""
        chats = [ch for _, ch in clients[cid]["chats"]]
        return paid[cid][1] if cid in paid and paid[cid][1] in chats else max(clients[cid]["chats"])[1]

    matched = [cid for cid, c in clients.items() if ok(cid, c)]
    picked, round_no, done = round_pick(matched, got)
    random.shuffle(picked)
    if f.max_recipients:
        picked = picked[:f.max_recipients]
    return Audience([(cid, chat_of(cid)) for cid in picked], len(matched), round_no, done)


def eta_days(n: int) -> int:
    return -(-n // DAILY_LIMIT)


# ---------- продление ----------
RENEW_BEFORE = timedelta(days=7)    # заканчивается в ближайшую неделю — предлагаем продлить
RENEW_AFTER = timedelta(days=30)    # закончилась больше месяца назад — уже не продление, не пишем
RENEW_TOPIC = "Продление подписки"
RENEW_WHO = "Подписка закончилась за последние 30 дней или заканчивается в ближайшие 7"
RENEW_ENDED = ("Здравствуйте! Ваша подписка Game Pass на {months} мес. закончилась {date}. Продлить? "
               "Подскажу актуальные цены.")
RENEW_SOON = "Здравствуйте! Ваша подписка Game Pass заканчивается {date}. Продлить заранее, чтобы доступ не прерывался?"
# последняя подписка клиента (игры/ключи/Steam и поддержка не в счёт; не разобранная — срок неизвестен, не пишем)
SUB = "coalesce(product, '?') NOT IN ('other', 'none')"
LAST_SUB = f"SELECT DISTINCT ON (client_id) * FROM crm.purchases WHERE {SUB} ORDER BY client_id, paid_at DESC"


def renew_text(expires_at: datetime | None, months: int | None, now: datetime) -> str | None:
    """Текст на момент отправки: закончилась / заканчивается; None — срок неизвестен или вне окна."""
    if not expires_at or not months or not now - RENEW_AFTER <= expires_at <= now + RENEW_BEFORE:
        return None
    date = expires_at.astimezone(night.TZ).strftime("%d.%m")
    return (RENEW_ENDED if expires_at <= now else RENEW_SOON).format(months=months, date=date)


async def renewals(db: asyncpg.Pool, now: datetime | None = None) -> list[asyncpg.Record]:
    """Чья последняя подписка в окне продления и о ней ещё не напоминали (одно напоминание на покупку)."""
    now = now or datetime.now(timezone.utc)
    return await db.fetch(f"""
        SELECT s.* FROM ({LAST_SUB}) s
        WHERE s.expires_at BETWEEN $1 AND $2 AND s.client_id NOT IN (SELECT client_id FROM gateway.ignored)
          AND NOT EXISTS (
            SELECT 1 FROM broadcast.recipients r WHERE r.purchase_id = s.id AND r.status IN ('queued', 'sent'))
        ORDER BY s.expires_at""", now - RENEW_AFTER, now + RENEW_BEFORE)


async def renew_message(db: asyncpg.Pool, purchase_id: int, now: datetime) -> str | None:
    """None — с момента запуска продлил (есть покупка новее) или срок ушёл из окна."""
    p = await db.fetchrow("SELECT * FROM crm.purchases WHERE id=$1", purchase_id)
    if not p or await db.fetchval("SELECT EXISTS (SELECT 1 FROM crm.purchases WHERE client_id=$1 AND paid_at > $2 "
                                  f"AND {SUB})", p["client_id"], p["paid_at"]):
        return None
    return renew_text(p["expires_at"], p["months"], now)


# ---------- утренняя сводка: продление + чем занять остаток дневного лимита ----------
COMMUNITY_URL = env("VK_COMMUNITY_URL", "vk.ru/club241851955")
PRESETS = {   # kind: (тема, кому словами, фильтр, текст)
    "invite": ("Приглашение в VK", "Все, кто покупал у нас", Filter(bought="yes"),
               "Здравствуйте! Приглашаем Вас в наше сообщество VK — там новости и помощь по подключению: "
               f"{COMMUNITY_URL}. Подпишетесь?"),
    "unfinished": ("Не завершили покупку", "Писали за последние 30 дней, но ни в одном чате не купили",
                   Filter(bought="no", wrote_within_days=30),
                   "Здравствуйте! Вы обращались к нам, но до покупки так и не дошло. Если вопрос ещё актуален — "
                   "помогу подобрать вариант и всё подключить. Для новых клиентов — скидка 100 ₽ за отзыв. "
                   "Подсказать актуальные цены?"),
}


async def spare(db: asyncpg.Pool, renew: int = 0) -> int:
    """Сколько ещё влезет в ближайший день: лимит минус продления минус уже стоящие в очереди."""
    queued = await db.fetchval("SELECT count(*) FROM broadcast.recipients r JOIN broadcast.campaigns c "
                               "ON c.id = r.campaign_id WHERE c.status = 'active' AND r.status = 'queued'")
    return max(DAILY_LIMIT - renew - queued, 0)


async def daily_offer(db: asyncpg.Pool, now: datetime | None = None) -> tuple[str, list[tuple[str, str]]]:
    """Текст сводки и кнопки (подпись, команда)."""
    now = now or datetime.now(timezone.utc)
    rows = await renewals(db, now)
    ended = sum(r["expires_at"] <= now for r in rows)
    unknown = await db.fetchval(f"SELECT count(*) FROM ({LAST_SUB}) s WHERE s.months IS NULL AND s.paid_at > $1",
                                now - timedelta(days=60))
    lines = [f"📅 Подписки на {now.astimezone(night.TZ):%d.%m} (о них ещё не напоминали):",
             f"• закончились за последние 30 дней: {ended}",
             f"• заканчиваются в ближайшие 7 дней: {len(rows) - ended}"]
    queued = await db.fetchval("SELECT count(*) FROM broadcast.recipients r JOIN broadcast.campaigns c "
                               "ON c.id = r.campaign_id WHERE c.kind = 'renew' AND c.status IN ('active', 'paused') "
                               "AND r.status = 'queued'")
    if queued:
        lines.append(f"• уже в очереди рассылки о продлении: {queued}")
    if unknown:
        lines.append(f"• куплены за 2 месяца, но срок в переписке не нашёл (им не пишу): {unknown}")
    buttons = []
    if rows:
        lines.append("\nКаждый получит свой текст: «закончилась ДД.ММ» или «заканчивается ДД.ММ».")
        buttons.append((f"🔁 Продление ({len(rows)})", "bc_auto:renew"))
    free = await spare(db, len(rows))
    if free:
        lines.append(f"\nВ день уходит до {DAILY_LIMIT} сообщений — свободно ещё {free}. Можно добавить:")
        for kind, label in (("invite", "👋 Приглашение в VK"), ("unfinished", "🛒 Не завершили покупку")):
            topic, who, f, _ = PRESETS[kind]
            n = len((await select(db, f, topic, now)).rows)
            lines.append(f"{label} — {who.lower()}: подходит {n}")
            if n:
                buttons.append((label, f"bc_auto:{kind}"))
    return "\n".join(lines), buttons
