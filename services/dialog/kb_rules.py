"""База знаний меняется по правилам владельца (31.10): ночью из чатов — предложения правил (lessons), Максим в VK
✅/✏️/🗑 → утверждённое правило сразу правит базу (knowledge/kb_*.md): места про ту же ситуацию, где написано иначе,
переписываются по правилу, чего нет — дописывается в подходящий раздел, остальное не трогается. Без отчёта.
Правило убрали (🗑, слияние) — прежний текст возвращается; изменили (✏️) — откат и правка заново по новому тексту.
Вытесненное лимитом (`removed_by='limit'`) из базы не убирается: выходит только из промпта.

Ход: база режется на куски (раздел; большой — по абзацам) → LLM по оглавлению и словам выбирает куски про правило →
LLM переписывает нужные → кусок заменяется, если его текст в файле один; в dialog.kb_edits — было/стало (откат).
Откат правила снимает и более поздние правки тех же разделов (сверху вниз), их правила потом правят базу заново.
Работает в основной копии dialog (цикл, раз в 15 с); dialog обеих копий перечитывает файлы сам (brain.kb_stamp).
Пересборка базы (build_kb) вписывает правила владельца сама (31.10) — `built`: прежние правки забываются, правила, с
которыми собрана база, считаются вписанными. Такое правило убрали (🗑, замена, слияние) или изменили (✏️) — отката нет,
и его текст сразу вычищается из базы (`apply(gone)`, решение Леонида 31.10: «из базы удалять, если правило удалено,
сразу»). `--reset` — для базы без правил: все правила правят её заново.
Запуск: python -m services.dialog.kb_rules [--reset]   (один проход по всем ждущим правилам)
"""
import argparse
import asyncio
import logging
import re
import time

import asyncpg

from services.dialog import brain
from shared import alerts
from shared.config import STORE
from shared.db import connect
from shared.llm import LLM

log = logging.getLogger("kb_rules")

SCHEMA = """
ALTER TABLE dialog.rules ADD COLUMN IF NOT EXISTS kb_text text;   -- текст правила, по которому поправлена база
CREATE TABLE IF NOT EXISTS dialog.kb_edits (
    id         bigserial PRIMARY KEY,
    rule_id    bigint NOT NULL,
    file       text NOT NULL,             -- core | games (knowledge/kb_<file>.md)
    part       text NOT NULL,             -- раздел (путь заголовков): откат снимает поздние правки тех же разделов
    old        text NOT NULL,             -- кусок до правки (вместе со строкой заголовка)
    new        text NOT NULL,
    nth        int NOT NULL DEFAULT 0,    -- какое по счёту вхождение new в файле (одинаковые абзацы бывают)
    created_at timestamptz NOT NULL DEFAULT now(),
    undone_at  timestamptz
);
"""
FILES = ("core", "games")
SKIP = ("Прайс",)        # снимок прайса: цены бот берёт из БД
BIG, PIECE = 2500, 2000  # раздел больше BIG — куски по абзацам до PIECE символов
MAX_PICK, MAX_CHARS = 14, 30000
EVERY = 15               # с, как часто dialog смотрит новые/убранные правила
RETRY = 1800             # не вышло — следующая попытка через 30 мин (правило и так в промпте)

PICK = """Ты редактор базы знаний бота-продавца магазина """ + STORE + """ (подписка Xbox Game Pass Ultimate, Avito).
Владелец утвердил правило для бота. Базу надо поправить под правило: найти места про ту же ситуацию — где написано
иначе, чем в правиле, или где по смыслу должно быть сказано то, что в правиле.
Ниже оглавление: номер куска — файл › раздел — начало текста. Выбери до 12 кусков, которые нужно прочитать (про тот же
вопрос клиента, ситуацию или действие продавца), и один кусок «home» — куда дописать правило, если в базе его нет.
Ещё дай 3–8 слов для поиска по тексту базы: основы слов по-русски, как пишут клиенты и продавец («удал», «подар»).
Ответ — JSON: {"ids": [номера], "home": номер, "words": ["…"]}"""

EDIT = """Ты редактор базы знаний бота-продавца магазина """ + STORE + """ (подписка Xbox Game Pass Ultimate, Avito).
Владелец утвердил правило для бота — оно важнее всего, что написано в базе. Ниже куски базы. Поправь базу под правило:
- кусок, где про ту же ситуацию написано иначе (другой ответ клиенту, другое действие продавца), — замени именно это
  место на то, что говорит правило: старый ответ убери, двух разных ответов рядом не оставляй; остальной текст куска
  оставь слово в слово;
- если нигде в кусках нет того, что говорит правило, — допиши в кусок «home» коротко и в стиле базы; если правило уже
  есть в каком-то куске (или ты его туда вписал) — больше нигде не повторяй;
- кусок, который правилу не противоречит, не трогай и не возвращай; правило не расширяй на соседние вопросы —
  кусок про другую ситуацию (другой вопрос клиента, другой товар) оставь как есть;
- это справка о магазине: без JSON и форматов ответа, без цен (цены — из прайса), без имён клиентов и контактов;
  правило велит передать вопрос Максу — пиши как действие продавца («Сами не отвечаем — передать Максу»), не
  обращайся к клиенту с просьбой что-то передать; в готовом ответе клиенту (частые вопросы, возражения) — сам ответ,
  а порядок действий продавца («сначала пришлю прайс…») — пометкой «(продавцу: …)», бот может отправить ответ как есть.
Ответ — JSON: {"blocks": [{"id": номер, "text": "новый текст куска целиком, без строки заголовка"}]};
менять нечего — {"blocks": []}"""

DROP = """Ты редактор базы знаний бота-продавца магазина """ + STORE + """ (подписка Xbox Game Pass Ultimate, Avito).
Владелец ОТМЕНИЛ правило для бота — в базе его больше быть не должно. Ниже куски базы. Убери из них то, что записано
по этому правилу: ответ клиенту или действие продавца, которого правило требовало в своей ситуации.
- для той же ситуации в куске есть и другой ответ — его оставь; ответ был только по правилу — убери это место целиком,
  взамен ничего не придумывай;
- то, что говорят ДЕЙСТВУЮЩИЕ правила владельца (список ниже), не трогай;
- остальной текст куска оставь слово в слово; кусок, где отменённого правила нет, не трогай и не возвращай.
Ответ — JSON: {"blocks": [{"id": номер, "text": "новый текст куска целиком, без строки заголовка"}]};
убирать нечего — {"blocks": []}"""


def path(cls: str):
    return brain.ROOT / "knowledge" / f"kb_{cls}.md"


def blocks(cls: str, text: str) -> list[dict]:
    """Куски файла по порядку: head — строка заголовка (у продолжения большого раздела пусто), body, part — путь
    заголовков, at — где кусок в файле (в базе игр есть дословные повторы абзацев — по одному тексту не найти)."""
    out, stack, pos = [], [], 0
    for sec in re.split(r"(?m)^(?=#{1,6} )", text):
        start, pos = pos, pos + len(sec)
        if not sec:
            continue
        head, body = (sec.split("\n", 1) + [""])[:2] if sec.startswith("#") else ("", sec)
        if head:
            level = len(head) - len(head.lstrip("#"))
            stack = [s for s in stack if s[0] < level] + [(level, head.lstrip("#").strip())]
            head += "\n"
        if any(t.startswith(SKIP) for _, t in stack):
            continue
        pieces = [body]
        if len(body) > BIG:   # «Когда звать Макса» — 26 тыс. знаков одним разделом
            pieces, cur = [], ""
            for line in body.splitlines(keepends=True):   # режем перед пустой строкой или пунктом списка
                if len(cur) + len(line) > PIECE and cur.strip() and (
                        not line.strip() or line.lstrip().startswith(("-", "*")) or len(cur) > PIECE * 1.5):
                    pieces.append(cur)
                    cur = ""
                cur += line
            pieces.append(cur)
        at = start
        for i, piece in enumerate(pieces):
            h = head if i == 0 else ""
            if piece.strip():   # пустой раздел (только заголовок) править нечего
                out.append({"file": cls, "part": " › ".join(t for lv, t in stack if lv > 1), "head": h,
                            "body": piece, "old": h + piece, "at": at, "whole": len(pieces) == 1})
            at += len(h) + len(piece)
    for n, b in enumerate(out, 1):
        b["id"] = n
    return out


def occ(text: str, s: str, at: int) -> int:
    """Сколько вхождений s начинается раньше позиции at (номер нашего вхождения среди одинаковых)."""
    n, i = 0, text.find(s)
    while 0 <= i < at:
        n, i = n + 1, text.find(s, i + 1)
    return n


def nth_find(text: str, s: str, n: int) -> int:
    i = text.find(s)
    for _ in range(n):
        if i < 0:
            break
        i = text.find(s, i + 1)
    return i


def toc(items: list[dict]) -> str:
    return "\n".join(f"{b['id']}. {b['file']} › {b['part']} — {' '.join(b['body'].split())[:70]}" for b in items)


def pick(items: list[dict], ids: list, home, words: list) -> list[dict]:
    """Куски, выбранные LLM, + найденные по словам (чем больше разных слов в куске, тем выше), до лимита."""
    by_id = {b["id"]: b for b in items}
    chosen = [by_id[i] for i in dict.fromkeys(ids + [home]) if i in by_id]
    words = [w.lower() for w in words if len(w) >= 3]
    hits = sorted(((sum(w in b["body"].lower() for w in words), b["id"]) for b in items), key=lambda x: -x[0])
    for n, i in hits:
        if n >= min(2, len(words)) and by_id[i] not in chosen:
            chosen.append(by_id[i])
    out, size = [], 0
    for b in chosen[:MAX_PICK]:
        if size + len(b["body"]) <= MAX_CHARS or not out:
            out.append(b)
            size += len(b["body"])
    return out


def good(old: str, new: str) -> bool:
    """Защита от порчи: пусто, обрезано, JSON-ответ клиенту вместо справки (как ночная сборка 30.10)."""
    return (bool(new.strip()) and new.strip() != old.strip() and '"reply"' not in new and "```" not in new
            and (len(old) < 300 or len(new) >= 0.6 * len(old)) and len(new) <= len(old) + 1500)


def cut(old: str, new: str) -> bool:
    """Вычистка правила: только убирает (короче, без JSON) и не больше абзаца-другого из большого куска."""
    return (new.strip() != old.strip() and len(new) < len(old) and '"reply"' not in new and "```" not in new
            and len(old) - len(new) <= max(800, len(old) // 2))


def to_int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def read(cls: str) -> str:
    p = path(cls)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def write(cls: str, text: str) -> None:
    p = path(cls)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(p)   # целиком: dialog не прочитает полфайла


async def propose(llm: LLM, rule: str, task: str = EDIT, keep: str = "") -> list[tuple[dict, str]]:
    """Что поменять: [(кусок, новый текст куска с заголовком)]. task=DROP — вычистить правило (keep — действующие)."""
    items = [b for cls in FILES for b in blocks(cls, read(cls))]
    for n, b in enumerate(items, 1):   # общая нумерация по обоим файлам
        b["id"] = n
    res = await llm.chat_json(PICK, f"ПРАВИЛО: {rule}\n\nОГЛАВЛЕНИЕ:\n{toc(items)}", max_tokens=500, temperature=0.1)
    ids = [i for i in map(to_int, res.get("ids") or []) if i]
    home = to_int(res.get("home"))
    chosen = pick(items, ids, home, [str(w) for w in res.get("words") or []])
    shown = "\n\n".join(f"=== кусок {b['id']}{' (home)' if b['id'] == home else ''}: {b['file']} › {b['part']}\n"
                        f"{b['body'].strip()}" for b in chosen)
    res = await llm.chat_json(task, f"ПРАВИЛО: {rule}\n\n{keep}{shown}", max_tokens=6000, temperature=0.1)
    by_id, out = {b["id"]: b for b in chosen}, []
    for r in res.get("blocks") or []:
        b = by_id.get(to_int(r.get("id")) if isinstance(r, dict) else None)
        new = str(r.get("text") or "").strip("\n") if b else ""
        if not b or not (cut(b["body"], new) if task is DROP else good(b["body"], new)):
            if b:
                log.warning("правку куска %s (%s) не берём", b["id"], b["part"])
            continue
        body = b["body"]
        if task is DROP and not new.strip() and b["whole"] and b["head"]:
            out.append((b, ""))   # вопрос, ответ на который был только по отменённому правилу, — без заголовка-сироты
            continue
        lead, trail = body[:len(body) - len(body.lstrip("\n"))], body[len(body.rstrip()):] or "\n"
        out.append((b, b["head"] + lead + new + trail))
    return out


async def undo(db: asyncpg.Pool, rule_id: int) -> None:
    """Снимаем правки правила и всех более поздних правок тех же разделов (сверху вниз); их правила — заново."""
    edits = await db.fetch("SELECT * FROM dialog.kb_edits WHERE undone_at IS NULL ORDER BY id")
    rules = {rule_id}
    while True:   # замыкание: поздняя правка того же раздела стоит поверх — снимать её первой
        mine = [e for e in edits if e["rule_id"] in rules]
        if not mine:
            break
        keys, low = {(e["file"], e["part"]) for e in mine}, min(e["id"] for e in mine)
        more = {e["rule_id"] for e in edits if e["id"] > low and (e["file"], e["part"]) in keys} - rules
        if not more:
            break
        rules |= more
    async with db.acquire() as con, con.transaction():
        await con.execute("SELECT pg_advisory_xact_lock(hashtext('kb_rules'))")
        texts = {cls: read(cls) for cls in FILES}
        for e in sorted((e for e in edits if e["rule_id"] in rules), key=lambda e: -e["id"]):
            t = texts[e["file"]]
            if (i := nth_find(t, e["new"], e["nth"])) < 0 and t.count(e["new"]) == 1:
                i = t.find(e["new"])
            if i >= 0:
                texts[e["file"]] = t[:i] + e["old"] + t[i + len(e["new"]):]
            else:
                log.warning("правка %s (правило %s, %s) в базе не найдена — откат пропущен", e["id"], e["rule_id"],
                            e["part"])
            await con.execute("UPDATE dialog.kb_edits SET undone_at=now() WHERE id=$1", e["id"])
        await con.execute("UPDATE dialog.rules SET kb_text=NULL WHERE id = ANY($1::bigint[])", list(rules))
        for cls in FILES:
            if texts[cls] != read(cls):
                write(cls, texts[cls])
    log.info("правило %s: правки базы сняты%s", rule_id,
             f" (и правил {sorted(rules - {rule_id})} — поправят заново)" if len(rules) > 1 else "")


async def apply(db: asyncpg.Pool, llm: LLM, rule_id: int, rule: str, gone: bool = False) -> None:
    """gone — правило вписано сборкой, а его убрали или изменили: вычищаем его текст; «было/стало» не пишем — откат
    вернул бы отменённое."""
    keep = ""
    if gone:
        rows = await db.fetch("SELECT rule FROM dialog.rules WHERE active AND id<>$1 ORDER BY id", rule_id)
        keep = "ДЕЙСТВУЮЩИЕ ПРАВИЛА ВЛАДЕЛЬЦА:\n" + "\n".join(f"- {r['rule']}" for r in rows) + "\n\n"
    changes = await propose(llm, rule, DROP if gone else EDIT, keep)
    async with db.acquire() as con, con.transaction():
        await con.execute("SELECT pg_advisory_xact_lock(hashtext('kb_rules'))")
        texts, rows = {cls: read(cls) for cls in FILES}, []
        for cls in FILES:   # по порядку в файле: правка сдвигает только то, что ниже
            shift = 0
            for b, new in sorted((c for c in changes if c[0]["file"] == cls), key=lambda c: c[0]["at"]):
                at, t = b["at"] + shift, texts[cls]
                if t[at:at + len(b["old"])] != b["old"]:   # файл поменялся, пока думали
                    log.warning("правило %s: кусок «%s» в базе изменился — пропуск", rule_id, b["part"])
                    continue
                texts[cls] = t[:at] + new + t[at + len(b["old"]):]
                shift += len(new) - len(b["old"])
                rows.append((b, new, at))
        for b, new, at in [] if gone else rows:
            await con.execute("INSERT INTO dialog.kb_edits (rule_id, file, part, old, new, nth) "
                              "VALUES ($1,$2,$3,$4,$5,$6)", rule_id, b["file"], b["part"], b["old"], new,
                              occ(texts[b["file"]], new, at))
        await con.execute("UPDATE dialog.rules SET kb_text=$2 WHERE id=$1", rule_id, None if gone else rule)
        for cls in FILES:
            if texts[cls] != read(cls):
                write(cls, texts[cls])
    log.info("правило %s: в базе %s кусков — %d", rule_id, "вычищено" if gone else "поправлено", len(rows))


_failed: dict[int, float] = {}


async def sync(db: asyncpg.Pool, llm: LLM) -> int:
    """Один проход: сначала откаты (убранные и изменённые правила), потом правка базы по новым правилам."""
    n = 0
    try:
        for r in await db.fetch(
                "SELECT r.id FROM dialog.rules r WHERE EXISTS (SELECT 1 FROM dialog.kb_edits e WHERE e.rule_id=r.id "
                "AND e.undone_at IS NULL) AND (r.active AND r.rule IS DISTINCT FROM r.kb_text "
                "OR NOT r.active AND r.removed_by IS DISTINCT FROM 'limit') ORDER BY r.id DESC"):
            await undo(db, r["id"])
        # вписано сборкой (правок нет) и убрано или изменено — вычистить; изменённое потом правит базу заново
        gone = await db.fetch(
            "SELECT r.id, r.kb_text AS rule, true AS gone FROM dialog.rules r WHERE r.kb_text IS NOT NULL AND NOT "
            "EXISTS (SELECT 1 FROM dialog.kb_edits e WHERE e.rule_id=r.id AND e.undone_at IS NULL) AND (r.active AND "
            "r.rule IS DISTINCT FROM r.kb_text OR NOT r.active AND r.removed_by IS DISTINCT FROM 'limit') ORDER BY r.id")
        for r in [*gone, *await db.fetch("SELECT id, rule, false AS gone FROM dialog.rules WHERE active "
                                         "AND rule IS DISTINCT FROM kb_text ORDER BY id")]:
            if time.monotonic() < _failed.get(r["id"], 0):
                continue
            try:
                await apply(db, llm, r["id"], r["rule"], r["gone"])
                n += 1
            except Exception as e:
                log.exception("правило %s: базу не поправил", r["id"])
                if r["id"] not in _failed:   # раз на правило: правило и так действует через промпт
                    await alerts.notify_admins(f"⚠️ База знаний не поправлена по правилу №{r['id']} "
                                               f"({type(e).__name__}); правило действует, повтор через 30 мин.")
                _failed[r["id"]] = time.monotonic() + RETRY
    except Exception:
        log.exception("правка базы по правилам")
    return n


async def built(db: asyncpg.Pool, rules: list[str]) -> None:
    """База собрана заново с правилами владельца внутри (build_kb): прежние «было/стало» к ней не относятся,
    правила, с которыми её собрали, в ней уже есть — повторно не правим."""
    async with db.acquire() as con, con.transaction():
        await con.execute("SELECT pg_advisory_xact_lock(hashtext('kb_rules'))")
        await con.execute("UPDATE dialog.kb_edits SET undone_at=now() WHERE undone_at IS NULL")
        await con.execute("UPDATE dialog.rules SET kb_text = CASE WHEN active AND rule = ANY($1::text[]) THEN rule END",
                          rules)


async def main(reset: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db = await connect(SCHEMA)
    llm = LLM()
    try:
        if reset:   # база пересобрана вручную: старые «было/стало» к ней не относятся
            await db.execute("UPDATE dialog.kb_edits SET undone_at=now() WHERE undone_at IS NULL")
            await db.execute("UPDATE dialog.rules SET kb_text=NULL")
        print("поправлено по правилам:", await sync(db, llm))
    finally:
        await llm.close()
        await db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="после ручной пересборки базы: все правила — заново")
    asyncio.run(main(ap.parse_args().reset))
