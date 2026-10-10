"""База знаний из выгруженных диалогов: data/export/*.json → knowledge/kb.md

Этап 1 (map): LLM читает диалоги пачками и выписывает факты/FAQ/возражения → data/kb/batches.jsonl (кэш).
Этап 2 (reduce): сведение по разделам в один документ для проверки Максом.
Запуск: python -m services.dialog.build_kb [--source all] [--kb-class core|games|steam|all] [--rebuild]
Результат: knowledge/kb_<класс>.md (core — всегда в промпте, остальные — по теме вопроса)
"""
import argparse
import asyncio
import hashlib
import json
import re

from services.avito_gateway.export import quick_reply_candidates
from services.avito_gateway.logic import DELETED_TEXT
from shared import catalog
from shared.kb_classes import CLASSES, classify_dialog
from shared.config import DATA_DIR, ROOT
from shared.db import connect
from shared.llm import LLM
from shared.textmask import MASKS, mask  # noqa: F401  (общая: база знаний, уроки, промпт)

EXPORT, WORK, OUT = DATA_DIR / "export", DATA_DIR / "kb", ROOT / "knowledge"

def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def load_templates() -> dict[str, str]:
    """Повторяющиеся тексты продавца → короткая метка, чтобы не гонять их в LLM целиком."""
    qr = json.loads((EXPORT / "quick_replies.json").read_text(encoding="utf-8"))
    return {q["text"]: f"[Ш{i}: {mask(q['text'])[:70]}…]" for i, q in enumerate(qr, 1)}


def render_dialog(d: dict, templates: dict[str, str]) -> str:
    lines = [f"### Чат (объявление: {d.get('item_title')}, {d.get('item_price')})"]
    for m in d["messages"]:
        if m["role"] == "system":
            continue
        text = norm(m["text"]) if m["text"] else f"<{m['type']}>"
        text = templates.get(text) or mask(text)[:600]
        lines.append(f"{'П' if m['role'] == 'seller' else 'К'}: {text}")
    return "\n".join(lines)


MAP_SYSTEM = """Ты аналитик магазина GameNonStop (продажи через Avito). Тема этих переписок: <<P>>.
Тебе дают переписки: К — клиент, П — продавец (владелец). [ШN: …] — готовый шаблон продавца.
Извлеки знания, которые помогут боту отвечать как этот продавец. Верни JSON:
{
 "facts": ["факт о товаре, сроках, гарантии, оплате, процессе подключения, требованиях к консоли/аккаунту"],
 "faq": [{"q": "вопрос клиента обобщённо", "a": "как отвечает продавец (его словами, кратко)"}],
 "objections": [{"objection": "сомнение/возражение клиента", "answer": "как продавец его снимает"}],
 "handoff": ["ситуации, где нужен человек: нестандартные запросы, проблемы, спорные моменты"],
 "stages": ["этап диалога продажи по порядку, если виден"],
 "lost": ["почему клиент не купил, если видно"],
 "sold": <сколько чатов закончились покупкой>, "total": <сколько чатов>
}
Не выписывай конкретные цены (прайс ведётся отдельно) и акции/подарки/розыгрыши (они идут рассылками) —
в ответах вместо цены пиши «{прайс}». Не включай имена, почты, телефоны, логины, пароли, коды.
Не выдумывай — только то, что есть в переписках."""

REDUCE = {
    "facts": ("Товар и условия",
              "Сведи факты в структурированный справочник (подразделы: варианты услуги, подключение, гарантия, "
              "оплата, требования). Убери дубли. Где факты противоречат друг другу — укажи оба варианта "
              "и пометь «⚠ уточнить у Макса»."),
    "stages": ("Этапы продажи", "Опиши типичный сценарий диалога от приветствия до отзыва, по шагам, "
               "с указанием, какой шаблон используется на шаге, если это видно."),
    "faq": ("Частые вопросы", "Объедини похожие вопросы, отсортируй по частоте (самые частые сверху). "
            "Для каждого — лучший ответ в стиле продавца: на «Вы», коротко, минимум эмодзи."),
    "objections": ("Возражения", "Объедини похожие, отсортируй по частоте, дай лучший ответ в стиле продавца."),
    "handoff": ("Когда звать Макса", "Объедини в список ситуаций, где бот должен передать диалог человеку."),
    "lost": ("Почему не купили", "Объедини причины, отсортируй по частоте, для каждой — идея, что можно улучшить."),
}
REDUCE_SYSTEM = ("Ты составляешь базу знаний для бота-продавца магазина GameNonStop (Avito), раздел: <<P>>. "
                 "Пиши по-русски, в Markdown, без вступлений. Не выдумывай. Не включай персональные данные. "
                 "Никаких конкретных цен (вместо них «{прайс}»), акций, подарков и розыгрышей — "
                 "это ведётся отдельно. Единственная постоянная скидка — 100 ₽ новым клиентам за отзыв.")
PRODUCT = CLASSES["core"]  # тема текущей сборки (меняется в main по классу)
CHUNK = 80000  # символов сырых выписок на один запрос сведения

# Шаблоны с прайсом или акциями в базу знаний не берём
PROMO = re.compile(r"(?i)розыгрыш|подар|акци|(?:\d+\s*мес.*?₽.*?){2}", re.S)
# Реквизиты и доступы в базу знаний не берём никогда: их даёт клиенту только Макс
SECRETS = re.compile(r"(?i)оплата💳|сбп|сбер|реквизит|<карта>|<телефон>|<скрыто>|vpn\s+\S+\s+<почта>")


async def map_stage(llm: LLM, dialogs: list[dict], batch: int, rebuild: bool) -> list[dict]:
    WORK.mkdir(parents=True, exist_ok=True)
    cache = WORK / "batches.jsonl"
    if rebuild and cache.exists():
        cache.unlink()
    # ключ кэша — набор чатов пачки, чтобы не подхватить выписки от другой выгрузки
    done = {} if not cache.exists() else {
        r["key"]: r for r in map(json.loads, filter(None, cache.read_text(encoding="utf-8").split("\n"))) if "key" in r}
    templates = load_templates()
    chunks = [dialogs[i:i + batch] for i in range(0, len(dialogs), batch)]
    sem = asyncio.Semaphore(8)

    async def extract(chunk: list[dict]) -> dict:
        text = "\n\n".join(render_dialog(d, templates) for d in chunk)
        try:
            return await llm.chat_json(MAP_SYSTEM.replace("<<P>>", PRODUCT), text[:60000], max_tokens=8000)
        except json.JSONDecodeError:  # ответ обрезался — делим пачку пополам
            if len(chunk) == 1:
                print("  пропущен диалог: LLM вернула не JSON")
                return {}
            a, b = await asyncio.gather(extract(chunk[:len(chunk) // 2]), extract(chunk[len(chunk) // 2:]))
            return {k: (a.get(k) or 0) + (b.get(k) or 0) if k in ("sold", "total") else
                    (a.get(k) or []) + (b.get(k) or []) for k in {*a, *b}}

    async def run(i: int, chunk: list[dict]) -> dict:
        key = ",".join(f"{d['chat_id']}:{len(d['messages'])}" for d in chunk)   # чат дополнился — пачку перечитать
        if key in done:
            return done[key]
        async with sem:
            r = await extract(chunk)
            r["key"] = key
            with cache.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"  пачка {i + 1}/{len(chunks)}")
            return r

    return await asyncio.gather(*(run(i, c) for i, c in enumerate(chunks)))


async def ask(llm: LLM, task: str, data: str) -> str:
    return await llm.chat([{"role": "system", "content": REDUCE_SYSTEM.replace("<<P>>", PRODUCT)},
                           {"role": "user", "content": f"{task}\nНе пиши заголовок раздела.\n\n{data}"}],
                          max_tokens=8000)


async def summarize(llm: LLM, results: list[dict]) -> dict[str, str]:
    """Сводка по разделам из выписок одной партии."""
    async def section(key: str) -> str:
        task = REDUCE[key][1]
        items = [json.dumps(x, ensure_ascii=False) for r in results for x in r.get(key, [])]
        chunks, cur = [], []
        for it in items:
            if cur and sum(map(len, cur)) + len(it) > CHUNK:
                chunks.append(cur)
                cur = []
            cur.append(it)
        chunks.append(cur)
        if len(chunks) == 1:
            return await ask(llm, task, f"Сырые выписки ({len(items)} шт.):\n" + "\n".join(items))
        parts = await asyncio.gather(*(ask(llm, task + " Для каждого пункта укажи частоту (×N).",
                                           "Сырые выписки:\n" + "\n".join(c)) for c in chunks))
        return await ask(llm, task + " Это частичные сводки — объедини их, частоты суммируй, пометки (×N) убери.",
                         "\n\n---\n\n".join(parts))

    bodies = await asyncio.gather(*(section(k) for k in REDUCE))
    return dict(zip(REDUCE, bodies))


async def merge(llm: LLM, groups: list[dict[str, str]]) -> dict[str, str]:
    """Сливает сводки нескольких партий (первая — самые свежие диалоги, важнее при противоречиях)."""
    if len(groups) == 1:
        return groups[0]

    async def section(key: str) -> str:
        parts = [f"### Партия {i} ({'самые свежие' if i == 1 else 'старее'})\n{g[key]}" for i, g in enumerate(groups, 1)]
        return await ask(llm, REDUCE[key][1] + " Это сводки по партиям диалогов разных периодов — объедини в одну. "
                         "При противоречиях приоритет у более свежей партии; устаревшее убери.", "\n\n".join(parts))

    bodies = await asyncio.gather(*(section(k) for k in REDUCE))
    return dict(zip(REDUCE, bodies))


def assemble(cls: str, bodies: dict[str, str], results: list[dict], dialogs: list[dict], price: str) -> str:
    sections = []
    for key, (title, _) in REDUCE.items():
        body = re.sub(r"(?m)^(#{1,4}) ", lambda m: "#" * min(len(m[1]) + 2, 5) + " ", bodies[key].strip())
        sections.append(f"## {title}\n\n{body}\n")
    sold, total = sum(r.get("sold", 0) or 0 for r in results), sum(r.get("total", 0) or 0 for r in results)
    sold = round(sold / max(total, 1) * len(dialogs))
    qr = [(t, n) for t, n in quick_reply_candidates(dialogs) if not PROMO.search(t) and not SECRETS.search(mask(t))][:25]
    tpl = "\n\n".join(f"**Ш{i}** (использован в {n} чатах)\n\n> {mask(t)}" for i, (t, n) in enumerate(qr, 1))
    head = (f"# База знаний GameNonStop — {CLASSES[cls]}\n\nСобрано автоматически из {len(dialogs)} диалогов Avito "
            f"(покупкой закончились ≈{sold}). Проверить и поправить — Макс.\n\n"
            "Цены здесь не хранятся. Акции, подарки и розыгрыши — только рассылками.\n")
    price_block = [f"## Прайс (из БД на момент сборки)\n\n{price}\n\nСкидка: до 100 ₽ за отзыв, "
                   "только новым клиентам (кто ещё не покупал).\n"] if cls == "core" else []
    return "\n".join([head, *price_block, *sections, f"## Шаблоны продавца (топ-25, без прайсов и акций)\n\n{tpl}\n"])


async def build_class(llm: LLM, cls: str, dialogs: list[dict], batch: int, group: int, rebuild: bool) -> None:
    global PRODUCT
    PRODUCT = CLASSES[cls]
    # по первому сообщению: новые чаты встают в конец, старые пачки и партии не сдвигаются — кэш LLM переиспользуется
    dialogs = sorted(dialogs, key=lambda d: (min((m["created"] or 0 for m in d["messages"]), default=0), d["chat_id"]))
    groups = [dialogs[i:i + group] for i in range(0, len(dialogs), group)]
    print(f"[{cls}] диалогов: {len(dialogs)}, партий по {group}: {len(groups)}", flush=True)
    results = await map_stage(llm, dialogs, batch, rebuild)
    per = -(-group // batch)  # пачек map в одной партии
    summaries = []
    for gi in range(len(groups)):
        part = results[gi * per:(gi + 1) * per]
        key = hashlib.md5((cls + ",".join(r.get("key", "") for r in part)).encode()).hexdigest()[:12]
        cache = WORK / f"group_{key}.json"
        if cache.exists():
            summaries.append(json.loads(cache.read_text(encoding="utf-8")))
        else:
            print(f"[{cls}] сводка партии {gi + 1}/{len(groups)}…", flush=True)
            s = await summarize(llm, part)
            cache.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
            summaries.append(s)
    print(f"[{cls}] сливаю партии…", flush=True)
    bodies = await merge(llm, summaries)
    db = await connect(catalog.SCHEMA)
    price = await catalog.price_text(db)
    await db.close()
    OUT.mkdir(exist_ok=True)
    (OUT / f"kb_{cls}.md").write_text(assemble(cls, bodies, results, dialogs, price), encoding="utf-8")
    print(f"[{cls}] готово → knowledge/kb_{cls}.md | токены всего: {llm.usage}", flush=True)


async def main(batch: int, rebuild: bool, source: str, group: int, classes: list[str]) -> None:
    path = EXPORT / ("dialogs_all.jsonl" if source == "all" else "dialogs.jsonl")
    dialogs = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line]  # не splitlines: U+2028
    # учимся только у Макса: ответы бота (тот же аккаунт — отличаем по id из outbox) и удалённые — не в базу знаний
    db = await connect()
    bot_ids = {r["message_id"] for r in await db.fetch("SELECT message_id FROM gateway.outbox WHERE message_id IS NOT NULL")}
    # не клиенты (gateway.ignored: свои аккаунты заказчика, тестовый аккаунт) — не в базу знаний
    skip = {r["id"] for r in await db.fetch("SELECT c.id FROM gateway.chats c JOIN gateway.ignored i "
                                            "ON i.client_id = c.client_id")}
    await db.close()
    dialogs = [d for d in dialogs if d["chat_id"] not in skip]
    for d in dialogs:
        d["messages"] = [m for m in d["messages"] if m["id"] not in bot_ids and m.get("text") != DELETED_TEXT]
    print(f"Ответов бота в outbox (в базу знаний не идут): {len(bot_ids)}", flush=True)
    dialogs = [d for d in dialogs if any(m["role"] == "client" for m in d["messages"])]
    by_class: dict[str, list[dict]] = {c: [] for c in CLASSES}
    for d in dialogs:
        by_class[classify_dialog(d.get("item_title"),
                                 [m["text"] for m in d["messages"] if m["role"] == "client" and m["text"]])].append(d)
    print("Диалогов по классам:", {c: len(v) for c, v in by_class.items()}, flush=True)
    if rebuild:  # один раз для всех классов
        for f in WORK.glob("*"):
            f.unlink()
    llm = LLM()
    try:
        for cls in classes:
            if by_class[cls]:
                await build_class(llm, cls, by_class[cls], batch, group, rebuild=False)
    finally:
        await llm.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=int, default=10)
    p.add_argument("--rebuild", action="store_true", help="выбросить кэш выписок и сводок")
    p.add_argument("--source", choices=["latest", "all"], default="latest",
                   help="latest — dialogs.jsonl, all — dialogs_all.jsonl (export --all)")
    p.add_argument("--group", type=int, default=1000, help="чатов в партии: сводка по партии, потом слияние")
    p.add_argument("--kb-class", choices=[*CLASSES, "all"], default="all")
    a = p.parse_args()
    asyncio.run(main(a.batch, a.rebuild, a.source, a.group, list(CLASSES) if a.kb_class == "all" else [a.kb_class]))
