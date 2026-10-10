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
from datetime import datetime, timedelta, timezone

import asyncpg

from services.dialog import brain
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
MAX_RULES = 60          # больше — старые выключаются (промпт не раздувается)
CHAT_MESSAGES = 40      # реплик из чата: всё вмешательство и немного до него
CHUNK = 40000           # символов переписок на один запрос

SYSTEM = """Ты обучаешь бота-продавца магазина GameNonStop (подписка Xbox Game Pass Ultimate, Avito). Бот отвечает
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
    rows = await db.fetch("SELECT rule, removed_by FROM dialog.rules WHERE removed_by IS DISTINCT FROM 'limit' "
                          "ORDER BY id")
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
            await db.execute("INSERT INTO dialog.rules (rule, chat_id, active) VALUES ($1,$2,false)", rule,
                             num.get(str(r.get("chat", "")).strip()))
            known.append(rule)
            added += 1
    # лимит: старые правила выключаются, свежие важнее
    await db.execute("UPDATE dialog.rules SET active=false, removed_by='limit' WHERE id IN (SELECT id FROM dialog.rules "
                     "WHERE active ORDER BY id DESC OFFSET $1)", MAX_RULES)
    log.info("чатов с вмешательством: %d, новых правил: %d", len(chats), added)
    return added


def rules_block(rules: list[str]) -> str:
    if not rules:
        return ""
    return ("\n\nПРАВИЛА ИЗ ПРАКТИКИ ВЛАДЕЛЬЦА (выведены из чатов, где он поправлял бота; важнее базы знаний):\n"
            + "\n".join(f"- {r}" for r in rules))


async def announce(db: asyncpg.Pool, send) -> None:
    """Новые правила — в VK по 3: под каждым ✅ / ✏️ / 🗑, внизу «✅ Все верны» (≤10 кнопок). send(text, kb)."""
    # нерешённые — снова раз в сутки: прошлое сообщение с кнопками к этому времени удалено (alerts.KEEP)
    rows = await db.fetch("SELECT id, rule FROM dialog.rules WHERE NOT active AND removed_by IS NULL "
                          "AND (announced_at IS NULL OR announced_at < now() - interval '23 hours') ORDER BY id")
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


REWRITE = """Ты помогаешь владельцу магазина GameNonStop (подписка Xbox Game Pass Ultimate, Avito) учить бота-продавца.
Бот вывел из переписки правило, а владелец своими словами объяснил, как надо на самом деле. Сформулируй одно правило
для бота по мысли владельца: одна-две фразы, «Если …, то …» или «Не …», общее (не про конкретного клиента), без цен,
имён и контактов. Мысль владельца важнее исходного правила, ничего от себя не добавляй.
Ответ — JSON: {"rule": "текст правила"}"""


async def rewrite(llm: LLM, rule: str, thought: str) -> str:
    """✏️: правило по мысли владельца (его текст — как есть, если LLM не ответила)."""
    try:
        res = await llm.chat_json(REWRITE, f"Правило бота: {rule}\nВладелец: {thought}", max_tokens=400,
                                  temperature=0.2)
        return " ".join(str(res.get("rule") or "").split())[:400] or thought
    except Exception:
        log.exception("не переформулировал правило")
        return thought


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
