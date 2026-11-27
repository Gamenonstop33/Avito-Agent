"""Прогон по ответам бота, которые владелец удалил и написал свой (25.11): бот отвечает на те же сообщения заново
текущим кодом, судья сравнивает с ответом владельца. Ничего не пишет в БД и не отправляет клиентам.

    PYTHONPATH=. python scripts/bench_deleted.py run /tmp/bench.json [--days 7]   # ответы бота
    PYTHONPATH=. python scripts/bench_deleted.py judge /tmp/bench.json            # судья + все случаи для проверки

Запускать внутри контейнера dialog нужного магазина (dialog — GameNonStop, dialog2 — PREMIUM). Судья — та же
нейросеть и ошибается примерно в 6 случаях из 53 в обе стороны: итог проверять глазами по распечатке.
Отличия от worker.handle: подсказок из handle нет (кроме подключения и действующей подписки), ответ не уходит,
заказ не создаётся — вместо ссылки СБП пометка."""
import argparse
import asyncio
import json
import logging
from datetime import timedelta

from services.dialog import brain
from services.dialog import lessons as lesson_rules
from services.dialog.worker import HISTORY, Dialog
from shared import catalog, sales
from shared.db import connect
from shared.kb_classes import claude_item
from shared.llm import LLM

OWNER_SPAN = timedelta(minutes=3)   # ответ владельца — его сообщения за 3 мин от первого, и через реплики клиента
# (20.11: «если на личный — присылаете данные…», клиент «4190», «если через доп — нужно быть у консоли»)


async def cases(db, days: int) -> list[dict]:
    rows = await db.fetch(
        "SELECT m.chat_id, m.created_at, m.text, c.item_title, c.client_id FROM gateway.messages m "
        "JOIN gateway.chats c ON c.id=m.chat_id WHERE m.origin='bot' AND m.deleted_at IS NOT NULL "
        "AND m.created_at > now() - make_interval(days => $1) ORDER BY m.created_at", days)
    out = []
    for r in rows:
        if claude_item(r["item_title"]):   # объявления Claude бот не ведёт (20.11)
            continue
        prev = await db.fetch(
            "SELECT origin, text, type, created_at FROM gateway.messages WHERE chat_id=$1 AND origin <> 'system' "
            "AND deleted_at IS NULL AND created_at < $2 ORDER BY created_at DESC LIMIT $3",
            r["chat_id"], r["created_at"], HISTORY)
        if not prev or prev[0]["origin"] != "client":
            continue
        nxt = await db.fetch(
            "SELECT text, created_at FROM gateway.messages WHERE chat_id=$1 AND origin='owner' AND text IS NOT NULL "
            "AND deleted_at IS NULL AND created_at > $2 AND created_at < $2 + interval '40 minutes' ORDER BY created_at",
            r["chat_id"], r["created_at"])
        if not nxt:
            continue
        owner = [n["text"] for n in nxt if n["created_at"] <= nxt[0]["created_at"] + OWNER_SPAN]
        h = [brain.Msg("client" if p["origin"] == "client" else "seller", p["text"] or f"<{p['type']}>",
                       p["created_at"], p["origin"]) for p in reversed(prev)]
        out.append({"chat": r["chat_id"], "at": r["created_at"], "deleted": r["text"], "owner": " / ".join(owner)[:900],
                    "title": r["item_title"], "client_id": r["client_id"], "h": h})
    return out


async def order_then(db, chat: str, at):
    """Заказ чата на момент ответа: неоплаченный (order) или оплаченный за последние 2 дня (paid)."""
    o = await db.fetchrow("SELECT * FROM sales.orders WHERE chat_id=$1 AND created_at < $2 AND status <> 'cancelled' "
                          "ORDER BY created_at DESC LIMIT 1", chat, at)
    if not o:
        return None, None
    o = dict(o)
    if o["paid_at"] and o["paid_at"] < at:
        return None, ({**o, "status": "paid"} if at - o["paid_at"] < timedelta(days=2) else None)
    return {**o, "status": "awaiting"}, None


async def one(d: Dialog, db, c: dict, sem) -> dict:
    async with sem:
        at, cid, h = c["at"], c["client_id"], c["h"]
        bought = await db.fetch(
            "SELECT product, months, paid_at, expires_at FROM crm.purchases WHERE client_id=$1 AND months IS NOT NULL "
            "AND paid_at < $2 ORDER BY paid_at DESC LIMIT 5", cid, at) if cid else []
        order, paid = await order_then(db, c["chat"], at)
        new = not bought and not paid
        gift_due = not new and await d.gift_due(cid, paid)
        price, personal = await catalog.price_parts(db, gifts=new or gift_due)
        said = " ".join(t for w, t in h[-8:] if w == "client")
        latest, first = h[-1][1], brain.is_first(h)
        tail = []
        for w, t in reversed(h):
            if w != "client":
                break
            tail.insert(0, t)
        client_text = " ".join(tail)
        issue = await sales.auto_issue(db)
        hint = brain.issue_hint(issue)
        act = next((b for b in bought if b["expires_at"] and b["expires_at"] > at), None)
        if act:
            hint += brain.ACTIVE_HINT.format(name=sales.NAMES.get(act["product"], act["product"]), months=act["months"],
                                             paid=act["paid_at"].astimezone(sales.TZ),
                                             end=act["expires_at"].astimezone(sales.TZ))
        facts = brain.facts_block(await catalog.promo(db), new=new, gift_due=gift_due,
                                  purchases=[dict(b) for b in bought], issue=issue, pay=await sales.pay_mode(db))
        o = order or paid
        u, err = None, None
        try:
            u = await d.understand(h, price, personal, new, o, facts)

            def precheck(dd):
                if not dd.reply or dd.handoff:
                    return ""
                why = []
                out = brain.guard_reply(dd.reply, new, price, said, dd.found, first=first, latest=latest,
                                        checking=True, why=why)
                return brain.cut_note(dd.reply, out, why)
            dec, _ = await d.think(h, price, personal, False, new, o, hint, facts=facts, precheck=precheck,
                                   understood=u)
            own = bool(brain.personal_said([t for w, t in h[-8:] if w == "client"])
                       or (o and o["product"] == "personal"))
            dec.reply, dec.handoff = brain.goods_guard(dec.reply, dec.handoff, dec.games, client_text, own, said,
                                                       bool(paid), u)
            dec.reply, dec.handoff = brain.price_drop_guard(dec.reply, dec.handoff, u)
            dec.reply = brain.variant_guard(dec.reply, u, client_text, bool(paid), brain.personal_offered(h))
            reply = brain.guard_reply(dec.reply, new, price, said, dec.found, first=first, latest=latest,
                                      checking=True, gift_due=gift_due)
            reply = brain.render_reply(reply, price, first, new, personal, "") if reply or first else ""
            if dec.order:   # в работе здесь создаётся заказ и уходит ссылка СБП
                reply += f"\n\n[ссылка на оплату СБП: {dec.order}]"
        except Exception as e:   # noqa: BLE001 — один сбойный случай не останавливает прогон
            dec, reply, err = None, "", repr(e)[:200]
        return {"chat": c["chat"], "at": at.isoformat(), "title": c["title"], "new": new,
                "hist": [(getattr(m, "origin", None) or m[0], (m[1] or "")[:300]) for m in h[-10:]],
                "client": client_text[-500:], "deleted": c["deleted"], "owner": c["owner"], "reply": reply,
                "handoff": dec.handoff if dec else None, "err": err,
                "understood": u and {k: sorted(v) if isinstance(v, set) else v for k, v in u.items()}}


JUDGE = (
    "Ты проверяешь ответы бота магазина подписок Xbox Game Pass в Avito. Владелец удалил прежний ответ бота и ответил "
    "сам. Оцени НОВЫЙ ответ бота: он по смыслу совпадает с ответом владельца или тоже был бы приемлем владельцу "
    "(верные факты, нет выдумок, нет лишних обещаний, отвечает на вопрос клиента)? Передача владельцу («Уточню и "
    "напишу Вам») — хорошо, если владелец сам решал нестандартный вопрос. Пустой ответ — хорошо, только если отвечать "
    "было не на что.\n"
    "Правила магазина (решения владельца), учитывай их:\n"
    "- Прайс ПОДПИСКИ, сроки, скидку 100 ₽ за отзыв, подарки и розыгрыш бот называет сам; ссылку на оплату подписки "
    "выставляет сам.\n"
    "- Цены и наличие ИГР (покупка игры отдельно от подписки) бот не называет никогда; о самой игре (пойдёт ли на "
    "консоли, вышла ли, когда выйдет, издания) не сообщает. Если владелец ответил ценой игры или фактами об игре, "
    "хороший ответ бота — «Уточню и напишу Вам» с передачей владельцу. Рассказать, как продаём игры (ключом на "
    "аккаунт клиента, после оплаты почта и пароль, игра навсегда), — можно.\n"
    "- «Подешевеет ли / когда скидки» — бот отвечает «Такой информации у нас нет, и мы её не предоставляем», без "
    "передачи — это хорошо.\n"
    "- Клиент не выбрал между дополнительным аккаунтом и личным — бот объясняет оба варианта.\n"
    "- Бот не видит, из какого объявления пишет клиент: оценивай по переписке.\n"
    'Верни JSON {"good": true|false, "why": "коротко"}.')


async def judge(path: str) -> None:
    res = json.load(open(path, encoding="utf-8"))
    db = await connect(max_size=2)
    rules = await lesson_rules.active_rules(db)   # метки {текст_N} — готовые тексты владельца, как в worker
    llm, sem = LLM(backup=False), asyncio.Semaphore(4)

    async def verdict(r):
        reply = lesson_rules.fill_texts(r["reply"], rules) if r["reply"] else "(пусто)"
        hist = "\n".join(f"{w}: {t}" for w, t in r["hist"][-8:])
        u = (f"Переписка до ответа:\n{hist}\n\nКлиент (последнее): {r['client']}\nУдалённый ответ бота: {r['deleted']}\n"
             f"Ответ владельца: {r['owner']}\nНОВЫЙ ответ бота: {reply}"
             + (f" [передал владельцу: {r['handoff']['reason']}]" if r.get("handoff") else ""))
        async with sem:
            try:
                return await llm.chat_json(JUDGE, u, temperature=0)
            except Exception as e:   # noqa: BLE001
                return {"good": None, "why": repr(e)[:100]}
    js = await asyncio.gather(*(verdict(r) for r in res))
    print("случаев", len(res), "| хороших по судье", sum(1 for j in js if j.get("good") is True),
          "| сбоев", sum(1 for r in res if r["err"]))
    for i, (r, j) in enumerate(zip(res, js)):
        print(f"\n===== #{i} {'OK' if j.get('good') else 'ПЛОХО'} | {r['at'][:16]} | новый клиент: {r['new']}")
        for w, t in r["hist"][-8:]:
            print(f"   {w:7} {t[:260]!r}")
        print("   УДАЛЁН  :", r["deleted"][:300])
        print("   ВЛАДЕЛЕЦ:", r["owner"][:400])
        print("   БОТ     :", (r["reply"] or "(пусто)")[:400], "| передача:", r["handoff"] and r["handoff"].get("reason"))
        if r.get("understood"):
            u = r["understood"]
            print("   РАЗБОР  :", u.get("question"), u.get("situations"), u.get("rules"))
        print("   СУДЬЯ   :", j.get("why"), r["err"] or "")
    await db.close()


async def run(path: str, days: int) -> None:
    logging.basicConfig(level=logging.ERROR)
    db = await connect(max_size=6)
    d = Dialog(db, LLM(backup=False))
    cs = await cases(db, days)
    sem = asyncio.Semaphore(4)
    res = await asyncio.gather(*(one(d, db, c, sem) for c in cs))
    json.dump(res, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("случаев:", len(res), "ошибок:", sum(1 for r in res if r["err"]), "токены:", d.llm.usage)
    await db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("run", "judge"))
    ap.add_argument("path")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    asyncio.run(run(a.path, a.days) if a.mode == "run" else judge(a.path))
