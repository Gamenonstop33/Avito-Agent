"""Правила из практики Макса: чаты, где он вмешался (писал сам, удалял ответы бота), → короткие правила для бота.

Ночью (шаг nightly) LLM читает такие чаты за прошедшие сутки и выписывает правила → dialog.rules (выключены).
Утром новые правила приходят в VK: ✅ (rule_ok:<id>) — включить, ✏️ (rule_edit:<id>) — Макс пишет мысль своими
словами, LLM переформулирует (rewrite), 🗑 (rule_del:<id>) — убрать. В промпт идут только включённые (09.10: из 10
правил, действовавших сразу, Макс убрал 6 — неверно понятые и повторы). Убранные LLM видит и не предлагает снова.
Запуск: python -m services.dialog.lessons
"""
import asyncio
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import asyncpg

from services.dialog import brain
from shared.config import STORE
from shared.db import connect
from shared.llm import LLM
from shared.textmask import mask
from shared.vk import button, keyboard

log = logging.getLogger("lessons")

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS dialog;
CREATE TABLE IF NOT EXISTS dialog.rules (
    id           bigserial PRIMARY KEY,
    rule         text NOT NULL,
    chat_id      text,                      -- из какого чата выведено
    active       boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    announced_at timestamptz,               -- список новых правил ушёл в VK
    removed_by   text                       -- кто выключил (vk:<id>) или limit — вытеснено более свежими
);
CREATE TABLE IF NOT EXISTS dialog.lesson_runs (ran_at timestamptz PRIMARY KEY);
ALTER TABLE dialog.rules ADD COLUMN IF NOT EXISTS approved_by text;   -- кто включил (vk:<id>); NULL — ждёт решения
"""
MAX_NEW = 5             # правил за ночь — только самые полезные


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 3}


def similar(rule: str, known: list[str], share: float = 0.7) -> bool:
    """Перефразированный повтор: ≥70% значимых слов правила уже есть в одном из известных."""
    w = _words(rule)
    return bool(w) and any(len(w & _words(k)) >= share * min(len(w), len(_words(k)) or 1) for k in known)
MAX_RULES = 120         # больше — старые выключаются (промпт не раздувается); 30.10 два списка слиты в один
CHAT_MESSAGES = 40      # реплик из чата: всё вмешательство и немного до него
CHUNK = 40000           # символов переписок на один запрос

SYSTEM = """Ты обучаешь бота-продавца магазина """ + STORE + """ (подписка Xbox Game Pass Ultimate, Avito). Бот отвечает
клиентам вместо владельца (Макса). Ниже — чаты за сутки, где Макс вмешался: писал сам или удалял ответы бота.
К — клиент, Б — бот, П — Макс, «Б (удалено Максом)» — ответ бота, который Макс удалил как ошибочный или лишний.
Выпиши короткие правила для бота: как Макс решает такие ситуации и чего бот делать не должен.
- Правило — одна-две фразы, общее (не про конкретного клиента), в виде «Если …, то …» или «Не …».
- Только то, что видно из переписки, не выдумывай. Учись у того, ЧТО Макс ответил по сути, и на удалённых
  ответах бота.
- Правило «не отвечай сам — передай Максу» — только если видно, что у бота нет нужных сведений (их нет в прайсе
  и базе знаний). Если бот мог ответить по прайсу или базе знаний, а Макс просто ответил сам, — такого правила не пиши.
- Не больше 5 правил — только однозначные и полезные; сомневаешься, что понял ситуацию, — не пиши.
- Без цен (прайс ведётся отдельно), имён, почт, паролей, кодов, номеров телефонов.
- Не повторяй уже действующие правила (список ниже) и общие вещи («будь вежлив», «отвечай коротко»).
- Нечему учиться — пустой список.
Ответ — JSON: {"rules": [{"rule": "текст правила", "chat": "номер чата из заголовка"}]}

ДЕЙСТВУЮЩИЕ ПРАВИЛА:
"""
WHO = {"client": "К", "bot": "Б", "owner": "П"}


def render_chat(n: int, rows: list) -> str:
    lines = [f"### Чат {n}"]
    for r in rows:
        who = WHO.get(r["origin"])
        if not who:
            continue
        text = mask(" ".join((r["text"] or f"<{r['type']}>").split()))[:500]
        lines.append(f"{who}{' (удалено Максом)' if r['deleted_at'] and r['origin'] == 'bot' else ''}: {text}")
    return "\n".join(lines)


async def active_rules(db: asyncpg.Pool) -> list[str]:
    return [r["rule"] for r in await db.fetch("SELECT rule FROM dialog.rules WHERE active ORDER BY id")]


async def extract(db: asyncpg.Pool, llm: LLM, since: datetime) -> int:
    chats = [r["chat_id"] for r in await db.fetch(
        "SELECT DISTINCT m.chat_id FROM gateway.messages m JOIN gateway.chats c ON c.id=m.chat_id "
        "WHERE m.created_at > $1 AND (m.origin='owner' OR (m.origin='bot' AND m.deleted_at IS NOT NULL)) "
        "AND NOT EXISTS (SELECT 1 FROM gateway.ignored g WHERE g.client_id = c.client_id) "
        # только чаты, где был бот: вмешательство, а не обычная переписка Макса
        "AND EXISTS (SELECT 1 FROM gateway.messages b WHERE b.chat_id=m.chat_id AND b.origin='bot')", since)]
    if not chats:
        return 0
    texts = []
    for i, chat_id in enumerate(chats, 1):
        rows = await db.fetch(
            "SELECT * FROM (SELECT origin, text, type, deleted_at, created_at FROM gateway.messages WHERE chat_id=$1 "
            "AND origin <> 'system' AND created_at > $2::timestamptz - interval '6 hours' ORDER BY created_at DESC "
            "LIMIT $3) t "
            "ORDER BY created_at", chat_id, since, CHAT_MESSAGES)
        texts.append((chat_id, render_chat(i, rows)))
    rows = await db.fetch("SELECT id, rule, removed_by FROM dialog.rules WHERE removed_by IS DISTINCT FROM 'limit' "
                          "ORDER BY id")
    pool = [r for r in rows if not r["removed_by"] or r["removed_by"].startswith("vk:")]   # для проверки по смыслу
    known = [r["rule"] for r in rows if not r["removed_by"]]           # включённые и ждущие решения
    rejected = [r["rule"] for r in rows if r["removed_by"]][-30:]      # убранные Максом
    system = (SYSTEM + ("\n".join(f"- {r}" for r in known) or "(пока нет)")
              + ("\n\nОТКЛОНЕНЫ ВЛАДЕЛЬЦЕМ (неверно поняты — такие и похожие не предлагай):\n"
                 + "\n".join(f"- {r}" for r in rejected) if rejected else "")
              # 09.10: без них LLM предлагала «не предлагай продать игру» — прямо против правила бота
              + "\n\nОСНОВНЫЕ ПРАВИЛА БОТА (правило из практики не должно им противоречить; что в них уже есть — "
                "не повторяй; часть ошибок бота в чатах уже исправлена этими правилами):\n" + brain.RULES)
    known += rejected
    batches, cur = [], []
    for t in texts:
        if cur and sum(len(x[1]) for x in cur) + len(t[1]) > CHUNK:
            batches.append(cur)
            cur = []
        cur.append(t)
    batches.append(cur)
    added, num = 0, {str(i): chat_id for i, (chat_id, _) in enumerate(texts, 1)}
    for b in batches:
        res = await llm.chat_json(system, "\n\n".join(t for _, t in b), max_tokens=3000, temperature=0.2)
        for r in res.get("rules") or []:
            rule = " ".join(str(r.get("rule") or "").split())[:400]
            if len(rule) < 15 or similar(rule, known) or added >= MAX_NEW:
                continue
            if (await check(llm, rule, pool))[0]:   # то же по смыслу уже есть или владелец убрал
                log.info("правило — повтор по смыслу, не предлагаю: %s", rule)
                continue
            rid = await db.fetchval("INSERT INTO dialog.rules (rule, chat_id, active) VALUES ($1,$2,false) RETURNING id",
                                    rule, num.get(str(r.get("chat", "")).strip()))
            known.append(rule)
            pool.append({"id": rid, "rule": rule})
            added += 1
    # лимит: старые правила выключаются, свежие важнее
    await db.execute("UPDATE dialog.rules SET active=false, removed_by='limit' WHERE id IN (SELECT id FROM dialog.rules "
                     "WHERE active ORDER BY id DESC OFFSET $1)", MAX_RULES)
    log.info("чатов с вмешательством: %d, новых правил: %d", len(chats), added)
    return added


# готовый текст владельца в правиле (VERBATIM, 03.11): стенд — LLM пересказывала шаги своими словами и склеивала
# строки, поэтому в промпте метка {текст_N}, а клиенту код вставляет текст как есть (как {прайс}, {галочка})
TEXT_MARK = re.compile(r"(?i)[ \t]*[—–-]?[ \t]*отправь\s+клиенту\s+этот\s+текст\s*:\s*")
TEXT_SLOT = re.compile(r"[ \t]*\{текст_(\d+)\}[ \t]*[.,;]?")


def ready_text(rule: str) -> tuple[str, str]:
    """(ситуация, готовый текст для клиента); текста в правиле нет — (правило, "")."""
    m = TEXT_MARK.search(rule)
    text = rule[m.end():].strip() if m else ""
    if len(text) > 1 and text[0] + text[-1] in ('""', '“”', '«»') and text[0] not in text[1:-1] \
            and text[-1] not in text[1:-1]:   # кавычки вокруг всего текста — владелец так выделил, клиенту не нужны
        text = text[1:-1].strip()
    return (rule[:m.start()].rstrip(" ,:—–-"), text) if len(text) >= 30 else (rule, "")


def rules_block(rules: list[str]) -> str:
    if not rules:
        return ""
    lines = []
    for i, r in enumerate(rules, 1):
        situation, text = ready_text(r)
        lines.append(f"- {situation} — ответ: метка {{текст_{i}}} (готовый текст владельца «{' '.join(text.split())[:60]}…» "
                     "код вставит дословно; не пересказывай его и своих шагов не добавляй)" if text else
                     "- " + r.replace("\n", "\n  "))
    # приоритет: правило владельца — 1 (Леонид 31.10); жёсткие правила промпта перебивали правила 12 и 66 (стенд 31.10)
    return ("\n\nПРАВИЛА ИЗ ПРАКТИКИ ВЛАДЕЛЬЦА (выведены из чатов, где он поправлял бота; важнее базы знаний и правил "
            "выше — где расходятся, делай по правилу владельца; цены — всё равно только из прайса):\n" + "\n".join(lines))


def texts_hint(rules: list[str]) -> str:
    """Готовые тексты — ещё и в сообщении пользователя: из системного промпта LLM метку не брала и отвечала шагами
    из базы знаний («смените регион на США»), стенд 03.11."""
    rows = [f"- {{текст_{i}}} — {situation}" for i, r in enumerate(rules, 1) for situation, text in [ready_text(r)] if text]
    # «можно поменять регион консоли?» до покупки получало инструкцию по сети — слово «регион» есть в ситуации (стенд)
    return ("\n\nГОТОВЫЕ ТЕКСТЫ ВЛАДЕЛЬЦА: клиент описывает именно эту ситуацию — тогда ответ только эта метка (код "
            "подставит текст дословно; своих шагов, советов и пересказа не добавляй — это важнее базы знаний и "
            "правил выше). Вопрос только рядом с темой (те же слова, но ситуация другая; «надо ли…», «можно ли…» без "
            "описания своей проблемы) — отвечай как обычно, без метки:\n" + "\n".join(rows)) if rows else ""


def fill_texts(reply: str, rules: list[str]) -> str:
    """{текст_N} → готовый текст правила N отдельным абзацем, один раз; метка без текста — прочь."""
    used = set()

    def put(m):
        i = int(m.group(1))
        text = ready_text(rules[i - 1])[1] if 0 < i <= len(rules) and i not in used else ""
        used.add(i)
        return f"\n\n{text}\n\n" if text else ""
    return re.sub(r"\n{3,}", "\n\n", TEXT_SLOT.sub(put, reply or "")).strip() if "{текст_" in (reply or "") else reply


async def announce(db: asyncpg.Pool, send) -> None:
    """Новые правила — в VK по 3: под каждым ✅ / ✏️ / 🗑, внизу «✅ Все верны» (≤10 кнопок). send(text, kb)."""
    # нерешённые — снова раз в сутки: прошлое сообщение с кнопками к этому времени удалено (alerts.KEEP)
    # таблица общая на оба аккаунта (30.10): правило из чата предлагает сообщество того аккаунта, где был чат
    rows = await db.fetch("SELECT id, rule FROM dialog.rules WHERE NOT active AND removed_by IS NULL "
                          "AND (announced_at IS NULL OR announced_at < now() - interval '23 hours') "
                          "AND (chat_id IS NULL OR chat_id IN (SELECT id FROM gateway.chats)) ORDER BY id")
    for part in (rows[i:i + 3] for i in range(0, len(rows), 3)):
        lines = ["ℹ️ К СВЕДЕНИЮ · 🧠 Бот предлагает правила по Вашим ответам. Заработают после ✅. "
                 "Понял не так — ✏️ и напишите своими словами, как надо; неверное — 🗑", ""]
        lines += [f"{i}. {r['rule']}" for i, r in enumerate(part, 1)]
        rows_kb = [[button(f"✅ {i}", f"rule_ok:{r['id']}", "positive"), button(f"✏️ {i}", f"rule_edit:{r['id']}"),
                    button(f"🗑 {i}", f"rule_del:{r['id']}", "negative")] for i, r in enumerate(part, 1)]
        if len(part) > 1:
            rows_kb.append([button("✅ Все верны", "rule_ok:" + ",".join(str(r["id"]) for r in part), "positive")])
        await send("\n".join(lines), keyboard(rows_kb, inline=True))
        await db.execute("UPDATE dialog.rules SET announced_at=now() WHERE id = ANY($1::bigint[])",
                         [r["id"] for r in part])


# 03.11: владелец прислал в ✏️ инструкцию по шагам (регион, MAC, DNS) — правило вышло одной фразой «отправь
# стандартную инструкцию», самих шагов не было ни в правиле, ни в базе знаний
# Леонид 03.11: «орфографию то поправлять автоматом нужно, так же, как и пунктуацию» («оснвоной» ушло бы клиенту)
VERBATIM = """Прислал владелец готовый текст для клиента (инструкцию, шаги, ответ — в кавычках, списком или после
«отправь») — перенеси его в правило целиком, с переносами строк: ситуация одной фразой, дальше «отправь клиенту этот
текст:» и сам текст. В тексте исправь орфографию, опечатки и пунктуацию (в т.ч. незакрытые кавычки и скобки); слова,
порядок, шаги, числа, адреса, эмодзи и стрелки не меняй, ничего не добавляй и не убирай. Иначе правило — одна-две
фразы."""

REWRITE = """Ты помогаешь владельцу магазина """ + STORE + """ (подписка Xbox Game Pass Ultimate, Avito) учить бота-продавца.
Бот вывел из переписки правило, а владелец своими словами объяснил, как надо на самом деле. Сформулируй одно правило
для бота по мысли владельца: «Если …, то …» или «Не …», общее (не про конкретного клиента), без цен, имён и
контактов. Мысль владельца важнее исходного правила, ничего от себя не добавляй.
""" + VERBATIM + """
Ответ — JSON: {"rule": "текст правила"}"""


FORMULATE = """Ты помогаешь владельцу магазина """ + STORE + """ (подписка Xbox Game Pass Ultimate, Avito) учить бота-продавца.
Владелец своими словами написал, как бот должен поступать. Сформулируй из этого одно правило для бота: «Если …,
то …» или «Не …», общее (не про конкретного клиента), без цен, имён и контактов. Только мысль владельца, ничего от
себя не добавляй.
""" + VERBATIM + """
Ответ — JSON: {"rule": "текст правила"}"""

RULE_MAX = 2000   # правило владельца с готовым текстом для клиента; выведенные ночью — по-прежнему ≤400


CHECK = """Ты помогаешь владельцу магазина """ + STORE + """ (подписка Xbox Game Pass Ultimate, Avito) вести правила
бота-продавца. Ниже правила (номер. текст) и НОВОЕ правило. Найди среди правил:
- same — повтор: та же ситуация и то же действие, что в новом; старое можно удалить, и ничего не потеряется;
- conflict — противоречие: ровно та же ситуация, а действие другое — в этой ситуации нельзя выполнить оба правила.
НЕ повтор и НЕ противоречие (таких не называй): ситуации похожи, но разные (другой вопрос клиента, другой товар,
другой этап — до оплаты / после); одно правило уже или шире другого; одно добавляет к другому, не споря с ним; в
старом есть что-то, чего нет в новом. Сомневаешься — не называй. Обычно таких правил нет совсем.
Ответ — JSON: {"same": [{"id": номер, "why": "коротко"}], "conflict": [{"id": номер, "why": "коротко"}]}"""


async def check(llm: LLM, rule: str, rows: list) -> tuple[list, list]:
    """Повторы по смыслу и противоречия нового правила среди rows (id, rule) — 31.10: слов (similar) мало, 88 и 89
    вышли одинаковыми. Сбой нейросети — как будто ничего нет: правило владельца не блокируем."""
    if not rows:
        return [], []
    try:
        res = await llm.chat_json(CHECK, "ПРАВИЛА:\n" + "\n".join(f"{r['id']}. {r['rule']}" for r in rows)
                                  + f"\n\nНОВОЕ: {rule}", max_tokens=300, temperature=0)
    except Exception:
        log.exception("не проверил правило на повторы")
        return [], []
    by = {r["id"]: r for r in rows}

    def pick(key):
        out = []
        for x in res.get(key) or []:
            i = x.get("id") if isinstance(x, dict) else x
            if str(i).strip().isdigit() and int(i) in by and by[int(i)] not in out:
                out.append(by[int(i)])
                log.info("правило «%s»: %s №%s — %s", rule[:60], key, i, x.get("why") if isinstance(x, dict) else "")
        return out
    same = pick("same")
    return same, [r for r in pick("conflict") if r not in same]


async def active_rows(db: asyncpg.Pool, skip: int | None = None) -> list:
    return await db.fetch("SELECT id, rule FROM dialog.rules WHERE active AND id IS DISTINCT FROM $1 ORDER BY id", skip)


async def replace(db: asyncpg.Pool, new_id: int, old: list) -> None:
    """Владелец выбрал «выключить старое» (03.11; с 31.10 по 03.11 — сразу, проверка путала разные ситуации:
    «ошибка сети» и «без интернета»): правила old выключаются, их правки базы откатит kb_rules."""
    await db.execute("UPDATE dialog.rules SET active=false, removed_by=$2 WHERE id = ANY($1::bigint[]) AND active",
                     [r["id"] for r in old], f"replaced:{new_id}")


async def rewrite(llm: LLM, rule: str, thought: str) -> str:
    """✏️: правило по мысли владельца; rule пустое — новое правило с нуля («✍️ Добавить правило»).
    Текст владельца — как есть, если LLM не ответила."""
    try:
        for temperature in (0.2, 0):
            res = await (llm.chat_json(REWRITE, f"Правило бота: {rule}\nВладелец: {thought}", max_tokens=1500,
                                       temperature=temperature) if rule else
                         llm.chat_json(FORMULATE, thought, max_tokens=1500, temperature=temperature))
            out = tidy(str(res.get("rule") or ""))[:RULE_MAX]
            # правка орфографии не трогает числа: DNS, сроки, шаги — только те, что были у владельца или в правиле
            if not Counter(re.findall(r"\d+", out)) - Counter(re.findall(r"\d+", f"{rule} {thought}")):
                return out or tidy(thought)
            log.warning("переформулировка изменила числа: %s", out[:200])
        return tidy(thought)
    except Exception:
        log.exception("не переформулировал правило")
        return tidy(thought)


def tidy(text: str) -> str:
    """Лишние пробелы — прочь, переносы строк остаются: шаги инструкции владельца — как он прислал (03.11)."""
    return re.sub(r"\n{3,}", "\n\n", "\n".join(" ".join(s.split()) for s in text.split("\n"))).strip()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db = await connect(SCHEMA)
    llm = LLM()
    try:
        last = await db.fetchval("SELECT max(ran_at) FROM dialog.lesson_runs")
        now = datetime.now(timezone.utc)
        await extract(db, llm, last or now - timedelta(days=1))
        await db.execute("INSERT INTO dialog.lesson_runs VALUES ($1)", now)
    finally:
        await llm.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
