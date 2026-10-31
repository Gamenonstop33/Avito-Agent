"""Разово (30.10): правила владельца двух аккаунтов → один общий список dialog.rules (shared/db.SHARED).

Правила PREMIUM MARKET (dialog2.rules), которых нет в dialog.rules дословно, переносятся со всеми полями. Затем LLM
ищет среди включённых повторы (остаётся одно, при равных — правило PREMIUM) и противоречия: правило PREMIUM важнее
правила GameNonStop, в одном аккаунте — более свежее; проигравшее выключается (removed_by='merge'), а спор пишется
в data/kb/rule_conflicts.json — владелец решает по таблице (scripts/kb_conflicts_xlsx.py).
dialog2.rules остаётся как было (архив: копия 2 его больше не читает). Повторный запуск ничего не задваивает.
Откат: DELETE FROM dialog.rules WHERE id > <печатается при запуске>; UPDATE … SET active=true, removed_by=NULL
WHERE removed_by='merge'.
Запуск — основной копией: python -m services.dialog.merge_rules; повторы и противоречия в общем списке —
`--dedupe` (список), `--dedupe --apply` (выключить).
"""
import asyncio
import json

from services.dialog import lessons
from shared.config import DATA_DIR, NS
from shared.db import connect
from shared.llm import LLM

OUT = DATA_DIR / "kb" / "rule_conflicts.json"

SYSTEM = """Перед тобой правила бота-продавца (подписка Xbox Game Pass, Avito), которые владелец ввёл в двух своих
магазинах; у каждого правила номер и метка: [P] — PREMIUM MARKET, [G] — GameNonStop, [PG] — в обоих.
Найди:
1) повторы — правила об одном и том же с одинаковым смыслом (разные слова не важны);
2) противоречия — одна и та же ситуация, а действовать велят по-разному.
Не повтор и не противоречие: правила о разных ситуациях; одно правило уточняет другое, не споря с ним.
Ответ — JSON: {"duplicates": [{"keep": номер самого точного и полного, "drop": [номера остальных]}],
"conflicts": [{"ids": [номера], "topic": "ситуация коротко"}]}. Пусто — пустые списки."""


def norm(rule: str) -> str:
    return " ".join(rule.lower().split())


def winner(ids: list[int], tags: dict[int, str], prefer: int | None = None) -> int:
    """Правило PREMIUM (P/PG) важнее GameNonStop; при равных — prefer (выбор LLM для повторов), иначе более свежее."""
    return max(ids, key=lambda i: ("P" in tags[i], i == prefer, i))


def ids_of(value, known) -> list[int]:
    return [int(i) for i in value if str(i).strip().isdigit() and int(i) in known]


async def copy_premium(db) -> dict[int, str]:
    """Переносит правила PREMIUM; возвращает метки включённых правил общего списка: id → P | G | PG."""
    ours = {norm(r["rule"]): r["id"] for r in await db.fetch("SELECT id, rule FROM dialog.rules")}
    print("откат: id новых правил больше", await db.fetchval("SELECT coalesce(max(id), 0) FROM dialog.rules"))
    theirs = await db.fetch("SELECT * FROM dialog2.rules ORDER BY id")
    tags, moved = {}, 0
    for r in theirs:
        if (same := ours.get(norm(r["rule"]))) is not None:
            tags[same] = "PG"
            # в PREMIUM включено, в GameNonStop выключено — PREMIUM важнее
            if r["active"] and await db.fetchval("UPDATE dialog.rules SET active=true, removed_by=NULL "
                                                 "WHERE id=$1 AND NOT active RETURNING id", same):
                print("включено снова (было выключено только в GameNonStop):", same)
            continue
        moved += 1
        ours[norm(r["rule"])] = new = await db.fetchval(
            "INSERT INTO dialog.rules (rule, chat_id, active, created_at, announced_at, removed_by, approved_by) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id", r["rule"], r["chat_id"], r["active"], r["created_at"],
            r["announced_at"], r["removed_by"], r["approved_by"])
        tags[new] = "P"
    print(f"PREMIUM: правил {len(theirs)}, перенесено {moved}, уже были {len(theirs) - moved}")
    return {r["id"]: tags.get(r["id"], "G") for r in await db.fetch("SELECT id FROM dialog.rules WHERE active")}


async def analyze(llm: LLM, text: dict[int, str], tags: dict[int, str]) -> tuple[set[int], list[dict]]:
    """Повторы и противоречия среди включённых правил → (что выключить, споры для таблицы владельца)."""
    res = await llm.chat_json(SYSTEM, "\n".join(f"{i}. [{tags[i]}] {text[i]}" for i in sorted(text)),
                              max_tokens=4000, temperature=0)
    drop: set[int] = set()
    for d in res.get("duplicates") or []:
        ids = ids_of([d.get("keep"), *(d.get("drop") or [])], text)
        if len(ids) > 1:
            keep = winner(ids, tags, prefer=ids[0])
            drop |= set(ids) - {keep}
            print(f"повтор: остаётся {keep}, выключены {sorted(set(ids) - {keep})}")
    conflicts = []
    for c in res.get("conflicts") or []:
        ids = [i for i in ids_of(c.get("ids") or [], text) if i not in drop]
        if len(ids) < 2:
            continue
        keep = winner(ids, tags)
        drop |= set(ids) - {keep}
        conflicts.append({"section": "Правила владельца", "topic": c.get("topic") or "", "rules": "",
                          "premium": "\n".join(text[i] for i in ids if "P" in tags[i]),
                          "gamenonstop": "\n".join(text[i] for i in ids if tags[i] == "G"),
                          "chosen": text[keep], "ids": ids, "kept": keep,
                          "off": [text[i] for i in ids if i != keep]})
        print(f"противоречие «{c.get('topic')}»: остаётся {keep}, выключены {sorted(set(ids) - {keep})}")
    return drop, conflicts


async def dedupe(apply: bool) -> None:
    """31.10: повторы и противоречия среди включённых правил одного списка (88 и 89 вышли одинаковыми) — той же
    проверкой, что и каждое новое правило (lessons.check; одним запросом на весь список выходило по-разному): от
    свежего к старому, свежее важнее. Без --apply только список; с --apply проигравшие выключаются
    (removed_by='merge'), их правки базы откатит kb_rules."""
    db, llm = await connect(lessons.SCHEMA), LLM()
    try:
        rows = await lessons.active_rows(db)
        text, drop = {r["id"]: r["rule"] for r in rows}, set()
        for r in reversed(rows):
            if r["id"] in drop:
                continue
            same, off = await lessons.check(llm, r["rule"], [o for o in rows if o["id"] < r["id"] and o["id"] not in drop])
            for o, kind in [(o, "повтор") for o in same] + [(o, "противоречие") for o in off]:
                drop.add(o["id"])
                print(f"{kind}: {r['id']} «{r['rule']}»\n   выключится {o['id']} «{o['rule']}»")
        if apply and drop:
            await db.execute("UPDATE dialog.rules SET active=false, removed_by='merge' WHERE id = ANY($1)", list(drop))
        print(f"включено правил: {len(text)} → {len(text) - len(drop)}{'' if apply else ' (только список)'}")
    finally:
        await llm.close()
        await db.close()


async def main() -> None:
    assert not NS, "запускать основной копией (без DB_NS)"
    db, llm = await connect(lessons.SCHEMA), LLM()
    try:
        tags = await copy_premium(db)
        text = {r["id"]: r["rule"] for r in await db.fetch("SELECT id, rule FROM dialog.rules WHERE id = ANY($1)",
                                                           list(tags))}
        drop, conflicts = await analyze(llm, text, tags)
        if drop:
            await db.execute("UPDATE dialog.rules SET active=false, removed_by='merge' WHERE id = ANY($1)", list(drop))
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(conflicts, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"включено правил: было {len(tags)}, стало {len(tags) - len(drop)}; противоречий: {len(conflicts)} → {OUT}")
    finally:
        await llm.close()
        await db.close()


if __name__ == "__main__":
    import sys
    if "--dedupe" in sys.argv:   # python -m services.dialog.merge_rules --dedupe [--apply]
        asyncio.run(dedupe("--apply" in sys.argv))
    else:
        asyncio.run(main())
