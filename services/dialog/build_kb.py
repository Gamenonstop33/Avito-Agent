"""База знаний из выгруженных диалогов обоих аккаунтов → knowledge/kb_<класс>.md, одна на оба бота (30.10)

Этап 1 (map): LLM читает диалоги пачками и выписывает факты/FAQ/возражения → <data>/kb/batches.jsonl (кэш; у каждого
аккаунта свой каталог: data/ — GameNonStop, data2/ — PREMIUM MARKET).
Этап 2 (reduce): сводки по партиям → сводка аккаунта (при расхождении важнее свежие партии).
Этап 3 (приоритет, решение 30.10; 31.10 — правила владельца вписываются в базу): база = правила владельца + переписка
PREMIUM MARKET + переписка GameNonStop, в споре — правило > PREMIUM > GameNonStop (PREMIUM владелец вёл сам,
GameNonStop куплен недавно); правила бота — только справка для сверки. LLM сводит раздел и
выписывает противоречия → data/kb/conflicts_<класс>.json (таблица владельцу — scripts/kb_conflicts_xlsx.py).
Собирает основная копия (nightly); копия 2 читает тот же knowledge/.
Запуск: python -m services.dialog.build_kb [--source all] [--kb-class core|games|all] [--rebuild]
Результат: knowledge/kb_<класс>.md (core — всегда в промпте, games — по теме вопроса). Steam не продаём (30.10):
чаты про Steam в базу не идут, всё о Steam из разделов убирается.
"""
import argparse
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from services.avito_gateway.export import quick_reply_candidates
from services.avito_gateway.logic import DELETED_TEXT
from services.dialog import brain, kb_rules, lessons
from services.dialog.merge_rules import OUT as RULE_CONFLICTS
from shared import catalog
from shared.kb_classes import CLASSES, KB, STEAM_ITEM as STEAM, classify_dialog
from shared.config import DATA_DIR, NS, ROOT
from shared.db import connect
from shared.llm import LLM
from shared.textmask import MASKS, mask  # noqa: F401  (общая: база знаний, уроки, промпт)

WORK, OUT = DATA_DIR / "kb", ROOT / "knowledge"
# Порядок — приоритет. Выгрузка копии 2 — data2/ (в контейнере основной копии смонтирована как /app/data2)
SOURCES = (("PREMIUM MARKET", "2"), ("GameNonStop", ""))
STORES = re.compile(r"(?i)premium\s*market|game\s*non\s*stop")   # база общая — название магазина не из неё


@dataclass
class Source:
    name: str
    ns: str
    by_class: dict[str, list[dict]] = field(default_factory=dict)
    results: dict[str, list[dict]] = field(default_factory=dict)   # выписки map по классам

    @property
    def export(self) -> Path:
        return ROOT / f"data{self.ns}" / "export"

    @property
    def work(self) -> Path:
        return ROOT / f"data{self.ns}" / "kb"

def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def load_templates(export: Path) -> dict[str, str]:
    """Повторяющиеся тексты продавца → короткая метка, чтобы не гонять их в LLM целиком."""
    qr = json.loads((export / "quick_replies.json").read_text(encoding="utf-8"))
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


MAP_SYSTEM = """Ты аналитик магазина <<S>> (продажи через Avito). Тема этих переписок: <<P>>.
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
REDUCE_SYSTEM = ("Ты составляешь базу знаний для бота-продавца магазина подписок Xbox (Avito), раздел: <<P>>. "
                 "Пиши по-русски, в Markdown, без вступлений. Не выдумывай. Не включай персональные данные. "
                 "Никаких конкретных цен (вместо них «{прайс}»), акций, подарков и розыгрышей — "
                 "это ведётся отдельно. Единственная постоянная скидка — 100 ₽ новым клиентам за отзыв.")
PRODUCT = CLASSES["core"]  # тема текущей сборки (меняется в main по классу)
CHUNK = 80000  # символов сырых выписок на один запрос сведения

# Шаблоны с прайсом или акциями в базу знаний не берём
PROMO = re.compile(r"(?i)розыгрыш|подар|акци|(?:\d+\s*мес.*?₽.*?){2}", re.S)
# Реквизиты и доступы в базу знаний не берём никогда: их даёт клиенту только Макс
SECRETS = re.compile(r"(?i)оплата💳|сбп|сбер|реквизит|<карта>|<телефон>|<скрыто>|vpn\s+\S+\s+<почта>")


async def map_stage(llm: LLM, src: Source, dialogs: list[dict], batch: int) -> list[dict]:
    src.work.mkdir(parents=True, exist_ok=True)
    cache = src.work / "batches.jsonl"
    # ключ кэша — набор чатов пачки, чтобы не подхватить выписки от другой выгрузки
    done = {} if not cache.exists() else {
        r["key"]: r for r in map(json.loads, filter(None, cache.read_text(encoding="utf-8").split("\n"))) if "key" in r}
    templates = load_templates(src.export)
    chunks = [dialogs[i:i + batch] for i in range(0, len(dialogs), batch)]
    sem = asyncio.Semaphore(8)
    system = MAP_SYSTEM.replace("<<S>>", src.name)

    async def extract(chunk: list[dict]) -> dict:
        text = "\n\n".join(render_dialog(d, templates) for d in chunk)
        try:
            return await llm.chat_json(system.replace("<<P>>", PRODUCT), text[:60000], max_tokens=8000)
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
            print(f"  [{src.name}] пачка {i + 1}/{len(chunks)}")
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


def cached(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def digest(*parts: str) -> str:
    return hashlib.md5("\x00".join(parts).encode()).hexdigest()[:12]


async def source_bodies(llm: LLM, cls: str, src: Source, batch: int, group: int) -> dict[str, str] | None:
    """Сводка раздела по переписке одного аккаунта: map → партии → слияние партий."""
    # по первому сообщению: новые чаты встают в конец, старые пачки и партии не сдвигаются — кэш LLM переиспользуется
    dialogs = sorted(src.by_class.get(cls, []),
                     key=lambda d: (min((m["created"] or 0 for m in d["messages"]), default=0), d["chat_id"]))
    if not dialogs:
        return None
    groups = [dialogs[i:i + group] for i in range(0, len(dialogs), group)]
    print(f"[{cls}] {src.name}: диалогов {len(dialogs)}, партий по {group}: {len(groups)}", flush=True)
    results = src.results[cls] = await map_stage(llm, src, dialogs, batch)
    per = -(-group // batch)  # пачек map в одной партии
    summaries, keys = [], []
    for gi in range(len(groups)):
        part = results[gi * per:(gi + 1) * per]
        key = hashlib.md5((cls + ",".join(r.get("key", "") for r in part)).encode()).hexdigest()[:12]
        keys.append(key)
        if (s := cached(src.work / f"group_{key}.json")) is None:
            print(f"[{cls}] {src.name}: сводка партии {gi + 1}/{len(groups)}…", flush=True)
            s = await summarize(llm, part)
            save(src.work / f"group_{key}.json", s)
        summaries.append(s)
    path = src.work / f"merged_{digest(cls, *keys)}.json"
    if (bodies := cached(path)) is None:
        print(f"[{cls}] {src.name}: сливаю партии…", flush=True)
        bodies = await merge(llm, summaries)
        save(path, bodies)
    return bodies


# База знаний = правила владельца + переписка обоих аккаунтов (Леонид 31.10: «база знаний это набор правил и истории
# сообщений с двух аккаунтов… приоритет правило — 1, премиум — 2, геймнонстоп — 3»). До 31.10 правила в разделы не
# вписывались («они и так есть у бота») и только вычищали несогласное — вычищали не всё, база спорила с правилами.
PRIORITY = (" База знаний состоит из трёх источников, по убыванию важности: 1) ПРАВИЛА ВЛАДЕЛЬЦА — верны всегда; "
            "2) раздел по переписке PREMIUM MARKET — магазин, который владелец вёл сам; 3) раздел по переписке "
            "GameNonStop — магазин куплен недавно. Сведи их в один раздел. Каждое правило владельца, которое "
            "относится к теме раздела, впиши в раздел как ответ или действие для его ситуации — с теми же условиями "
            "и действием (правило «передай Максу» — «Сами не отвечаем — передать Максу»). Где источники про одну "
            "ситуацию расходятся — оставь вариант более важного: правило важнее переписки, PREMIUM MARKET важнее "
            "GameNonStop; вариант менее важного убери целиком, рядом не ставь. Что есть только в одном источнике и не "
            "спорит с более важными — оставь. Порядок действий продавца в готовых ответах — пометкой «(продавцу: …)», "
            "ответ клиенту — отдельно. ПРАВИЛА БОТА — справка: что им противоречит, убери, сами их в раздел не "
            "переписывай. Источники не упоминай, названия магазинов не пиши. Steam мы не продаём — всё о Steam "
            "(пополнение, игры и ключи, аккаунты, смена региона Steam) убери.")
CONFLICTS = """Ты проверяешь базу знаний бота-продавца подписки Xbox Game Pass (Avito), раздел «<<P>>».
Источники по убыванию важности: ПРАВИЛА (бота и владельца), PREMIUM MARKET (магазин, который владелец вёл сам),
GameNonStop (магазин куплен недавно). Найди противоречия: один и тот же вопрос или ситуация, а источники говорят
разное — факты, условия, что ответить клиенту или что сделать. Не противоречие: разные слова об одном и том же;
то, о чём говорит только один источник; одно уточняет другое. Правила между собой не сравнивай.
Ответ — JSON: {"conflicts": [{"topic": "вопрос или ситуация коротко",
 "rules": "что говорят правила, или пусто", "premium": "что в PREMIUM MARKET, или пусто",
 "gamenonstop": "что в GameNonStop, или пусто",
 "chosen": "что остаётся в базе — вариант самого важного источника, где об этом есть"}]}.
Не противоречие и «передать человеку» в обоих источниках. Steam не продаём — споры о Steam не выписывай. Источник молчит — пустая строка (не «пусто»).
Самые важные для ответов клиентам — первыми. Каждое поле — одна короткая фраза своими словами, без цен.
Нет противоречий — {"conflicts": []}."""
SIDES = ("rules", "premium", "gamenonstop")
BOT_REPLY = re.compile(r'"reply"\s*:')   # ответ клиенту в формате бота, а не раздел базы
THIN = 0.5   # сведённый раздел короче половины самого полного источника — потеряно


def salvage(text: str) -> list:
    """Список противоречий из ответа LLM; оборвался на лимите токенов (30.10, раздел core) — целые записи из начала."""
    try:
        return (json.loads(text) or {}).get("conflicts") or []
    except (json.JSONDecodeError, AttributeError):
        pass
    dec, i, out = json.JSONDecoder(), text.find("[") + 1, []
    while i > 0:
        while i < len(text) and text[i] in " \n\r\t,":
            i += 1
        try:
            obj, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        out.append(obj)
    return out


NOTHING = re.compile(r"(?i)^(пусто|нет данных|нет информации|ничего|[—-]|не (упоминается|указано|сказано|говорится|"
                     r"описано|освещено|рассматривается)\b.*)?\.?$")


def clean_conflicts(items, section: str) -> list[dict]:
    """Только настоящие споры: хотя бы два источника сказали что-то своё."""
    out = []
    for c in items if isinstance(items, list) else []:
        if not isinstance(c, dict):
            continue
        row = {k: " ".join(str(c.get(k) or "").split()) for k in ("topic", *SIDES, "chosen")}
        row |= {k: "" for k in SIDES if NOTHING.match(row[k])}   # LLM пишет «Пусто» / «не упоминается»
        if sum(bool(row[k]) for k in SIDES) >= 2:
            out.append({"section": section, **row})
    return out


async def prioritize(llm: LLM, cls: str, rules: str, parts: list[tuple[str, dict[str, str]]]
                     ) -> tuple[dict[str, str], list[dict]]:
    """Один раздел на оба аккаунта: правила > PREMIUM > GameNonStop; кэш — пока не изменились источники, правила
    и сама инструкция сведения."""
    path = WORK / f"final_{digest(cls, rules, PRIORITY, json.dumps(parts, ensure_ascii=False))}.json"
    if (done := cached(path)) is not None:
        return done["bodies"], done["conflicts"]
    print(f"[{cls}] свожу по приоритету: правила > {' > '.join(n for n, _ in parts)}…", flush=True)

    async def merged(key: str, task: str, data: str) -> str:
        """Сведённый раздел. 30.10 LLM вместо раздела отвечала клиенту JSON-ом или теряла половину фактов
        (игры: 25 тыс. знаков на входе → 5 тыс.) — ещё попытка; не вышло — самый важный источник как есть."""
        srcs = [b.get(key) or "" for _, b in parts]
        best = ""
        for _ in range(2):
            text = await ask(llm, task + PRIORITY, data)
            if not BOT_REPLY.search(text) and len(text) > len(best):
                best = text
            if len(best) >= THIN * max(map(len, srcs), default=0):
                return best
        print(f"[{cls}] {REDUCE[key][0]}: сведение не удалось — "
              f"{'самое полное из попыток' if best else 'раздел самого важного источника'}", flush=True)
        return best or next((s for s in srcs if s), "")

    async def section(key: str) -> tuple[str, list[dict]]:
        title, task = REDUCE[key]
        data = f"ПРАВИЛА:\n{rules}\n\n" + "\n\n".join(f"### {name}\n{b[key]}" for name, b in parts)
        body = merged(key, task, data)
        if key == "lost":   # аналитика, в промпт бота не идёт
            return await body, []
        found = llm.chat([{"role": "system", "content": CONFLICTS.replace("<<P>>", f"{CLASSES[cls]} · {title}")},
                          {"role": "user", "content": data}], json_mode=True, max_tokens=8000, temperature=0)
        body, found = await asyncio.gather(body, found)
        return body, clean_conflicts(salvage(found), f"{CLASSES[cls]} · {title}")

    done = dict(zip(REDUCE, await asyncio.gather(*(section(k) for k in REDUCE))))
    bodies, conflicts = {k: v[0] for k, v in done.items()}, [c for v in done.values() for c in v[1]]
    save(path, {"bodies": bodies, "conflicts": conflicts})
    return bodies, conflicts


def templates_by_priority(srcs: list[Source], cls: str, n: int = 25) -> list[tuple[str, int]]:
    """Шаблоны продавца: сначала PREMIUM, потом GameNonStop; без прайсов, акций и реквизитов."""
    out, seen = [], set()
    for src in srcs:
        for t, k in quick_reply_candidates(src.by_class.get(cls, [])):
            if t not in seen and not PROMO.search(t) and not SECRETS.search(mask(t)) and not STEAM.search(t):
                seen.add(t)
                out.append((t, k))
    return out[:n]


# Steam не продаём (30.10): LLM убирает Steam из разделов не всегда (оставалось «пополнить баланс Steam — передать
# человеку») — дочищаем кодом: из перечислений вырезаем пункты про Steam, строку, где Steam остался, — целиком.
# Steam Deck — ПК, его не трогаем (STEAM — общий с kb_classes)
_STEAM_ITEM = r"[^,;:.()\n]*(?:steam(?!\s*deck)|(?<![а-яё])стим(?!\s*дек))[^,;:.()\n]*"


def drop_steam(text: str) -> str:
    out = []
    for line in text.split("\n"):
        if STEAM.search(line):
            line = re.sub(r"(?i)" + _STEAM_ITEM + r"(?:[,;]\s*|(?=\)))", "", line)   # пункт, за ним запятая или «)»
            line = re.sub(r"(?i)[,;]\s*" + _STEAM_ITEM, "", line)                    # последний пункт перечисления
            line = re.sub(r",(?=[^\s\d])", ", ", re.sub(r"\s+([.,;)])", r"\1", re.sub(r"\(\s*\)", "", line)))
            if STEAM.search(line) or not re.search(r"(?i)[а-яёa-z]{3}", re.sub(r"^[\s>*#\-\d.]+", "", line)):
                continue
        out.append(line)
    return "\n".join(out)


def stats(cls: str, srcs: list[Source]) -> tuple[list[str], int, list[tuple[str, int]]]:
    """Для шапки и шаблонов: чатов по аккаунтам, сколько кончились покупкой, шаблоны продавца."""
    counts, sold_all = [], 0
    for src in srcs:
        dialogs, results = src.by_class.get(cls, []), src.results.get(cls, [])
        sold, total = sum(r.get("sold", 0) or 0 for r in results), sum(r.get("total", 0) or 0 for r in results)
        sold_all += round(sold / max(total, 1) * len(dialogs))
        counts.append(f"{src.name} — {len(dialogs)}")
    return counts, sold_all, templates_by_priority(srcs, cls)


def assemble(cls: str, bodies: dict[str, str], counts: list[str], sold_all: int, templates: list[tuple[str, int]],
             price: str) -> str:
    sections = []
    for key, (title, _) in REDUCE.items():
        body = re.sub(r"(?m)^(#{1,4}) ", lambda m: "#" * min(len(m[1]) + 2, 5) + " ", bodies[key].strip())
        sections.append(f"## {title}\n\n{body}\n")
    tpl = "\n\n".join(f"**Ш{i}** (использован в {n} чатах)\n\n> {mask(t)}" for i, (t, n) in enumerate(templates, 1))
    head = (f"# База знаний — {CLASSES[cls]}\n\nОдна на оба аккаунта. Собрано автоматически из правил владельца и диалогов Avito "
            f"({', '.join(counts)}; покупкой закончились ≈{sold_all}). Приоритет: правила > PREMIUM MARKET > "
            "GameNonStop. Проверить и поправить — Макс.\n\n"
            "Цены здесь не хранятся. Акции, подарки и розыгрыши — только рассылками.\n")
    price_block = [f"## Прайс (из БД на момент сборки)\n\n{price}\n\nСкидка: до 100 ₽ за отзыв, "
                   "только новым клиентам (кто ещё не покупал).\n"] if cls == "core" else []
    body = STORES.sub("наш магазин", drop_steam(
        "\n".join([*sections, f"## Шаблоны продавца (топ-25, без прайсов и акций)\n\n{tpl}\n"])))
    return "\n".join([head, *price_block, body])


async def build_class(llm: LLM, cls: str, srcs: list[Source], rules: str, batch: int, group: int,
                      dest: Path) -> None:
    parts = [(src.name, b) for src in srcs if (b := await source_bodies(llm, cls, src, batch, group))]
    if not parts:
        return
    bodies, conflicts = await prioritize(llm, cls, rules, parts)
    save(WORK / f"conflicts_{cls}.json", conflicts)
    db = await connect(catalog.SCHEMA)
    price = await catalog.price_text(db)
    await db.close()
    (dest / f"kb_{cls}.md").write_text(assemble(cls, bodies, *stats(cls, srcs), price), encoding="utf-8")
    print(f"[{cls}] разделы сведены, противоречий источников: {len(conflicts)} | токены всего: {llm.usage}",
          flush=True)


FIND = """Ты проверяешь базу знаний бота-продавца (подписка Xbox Game Pass, Avito): база = правила владельца +
переписка магазинов, правило важнее переписки. Дано правило и куски базы. Найди фразы, по которым бот в ТОЙ ЖЕ ситуации,
что в правиле, ответит или поступит иначе, чем требует правило. Не в счёт: другая ситуация, уточнение, дополнение,
похожая тема, сама запись правила. Ответ — JSON: {"conflicts": [{"id": номер куска, "quote": "дословная фраза"}]}
Нет — {"conflicts": []}."""
SURE = """Правило владельца магазина и кусок базы знаний бота-продавца (подписка Xbox Game Pass, Avito). Правило важнее
базы. Это НАСТОЯЩЕЕ расхождение? «Да» — только если есть конкретный вопрос клиента, к которому относятся и правило, и
фраза, и бот, следуя фразе, нарушит правило. «Нет» — фраза про другую ситуацию, другого клиента (новый/покупавший),
другой товар, дополняет или уточняет правило, или расхождение надуманное. Ниже и все правила владельца: если фраза по
смыслу и есть другое правило из списка — "other": его номер (это спор правил, а не ошибка базы), иначе null.
Ответ — JSON: {"real": true|false, "other": номер или null}"""
FIX = """Ты редактор базы знаний бота-продавца (подписка Xbox Game Pass Ultimate, Avito). База = правила владельца +
переписка магазинов; правило владельца важнее переписки. Дан кусок базы ("text"), правила владельца ("rules") и фразы
куска, которые с ними расходятся ("phrases"). Замени эти фразы ответом по правилу (с теми же условиями и действием),
вариант переписки рядом не оставляй. Остальные правила владельца ("all_rules") не нарушай: где рядом ситуация другого
правила (общее и частное), не подменяй одно другим — разведи условиями. Больше ничего не меняй и не дописывай:
остальной текст — дословно, разметку (подзаголовки, списки) сохрани. «Передай Максу» — «Сами не отвечаем — передать Максу»; порядок действий продавца —
пометкой «(продавцу: …)».
Ответ — JSON: {"text": "кусок целиком"}"""
GROW = 400   # замена фразы на ответ по правилу почти не удлиняет кусок; больше — LLM дописала лишнее
ROUNDS = 2
LABEL = re.compile(r"(?m)^#+\s*(?:core|games)\s*›.*\n?")   # путь куска, если LLM вернула его заголовком


async def enforce(llm: LLM, owner: list[str], dest: Path) -> tuple[int, list[tuple[str, str]]]:
    """Правило важнее переписки (31.10). Сведение разделов по 20–40 тыс. знаков правила вписывает, но старые ответы
    из переписки рядом оставляет («подключим, потом оплатите» при правиле «так оплатить не получится»). Поэтому по
    каждому правилу: куски про его ситуацию (как у kb_rules) → расходящиеся фразы → две проверки «та же ситуация» →
    правка только этих фраз; до ROUNDS кругов, пока находится. Фраза, которая сама записывает другое правило, —
    спор правил владельца: не правим, возвращаем парой (решает владелец)."""
    sem, total, clash = asyncio.Semaphore(6), 0, set()
    listed = "\n".join(f"{i}. {r}" for i, r in enumerate(owner, 1))

    async def ask_json(system: str, user: str, tokens: int) -> dict:
        async with sem:
            try:
                return await llm.chat_json(system, user, max_tokens=tokens, temperature=0)
            except ValueError:   # битый JSON — как «ничего не нашли»
                return {}

    for rnd in range(1, ROUNDS + 1):
        texts = {cls: (dest / f"kb_{cls}.md").read_text(encoding="utf-8") for cls in KB}
        items = [b for cls in KB for b in kb_rules.blocks(cls, texts[cls])]
        for n, b in enumerate(items, 1):   # общая нумерация по обоим файлам
            b["id"] = n
        menu = kb_rules.toc(items)

        async def found(rule: str) -> list[tuple[dict, str, str]]:
            res = await ask_json(kb_rules.PICK, f"ПРАВИЛО: {rule}\n\nОГЛАВЛЕНИЕ:\n{menu}", 500)
            ids = [i for i in map(kb_rules.to_int, res.get("ids") or []) if i]
            chosen = kb_rules.pick(items, ids, kb_rules.to_int(res.get("home")),
                                   [str(w) for w in res.get("words") or []])
            by_id = {b["id"]: b for b in chosen}
            shown = "\n\n".join(f"=== кусок {b['id']}: {b['part']}\n{b['body'].strip()}" for b in chosen)
            res = await ask_json(FIND, f"ПРАВИЛО: {rule}\n\n{shown}", 1500)
            out = []
            for c in res.get("conflicts") or []:
                b = by_id.get(kb_rules.to_int(c.get("id"))) if isinstance(c, dict) else None
                q = str(c.get("quote") or "").strip() if b else ""
                if q and q[:60] in b["body"]:   # не дословно — править нечего
                    out.append((b, rule, q))
            return out

        async def sure(b: dict, rule: str, q: str) -> bool:
            data = (f"ПРАВИЛО: {rule}\n\nКУСОК БАЗЫ ({b['part']}):\n{b['body']}\n\nФРАЗА: {q}\n\n"
                    f"ВСЕ ПРАВИЛА ВЛАДЕЛЬЦА:\n{listed}")
            votes = await asyncio.gather(*(ask_json(SURE, data, 60) for _ in range(2)))
            if not all(v.get("real") is True for v in votes):
                return False
            other = {kb_rules.to_int(v.get("other")) for v in votes} - {None}
            if other and (n := min(other)) <= len(owner) and owner[n - 1] != rule:
                clash.add(tuple(sorted((rule, owner[n - 1]))))
                return False
            return True

        cand = [c for cs in await asyncio.gather(*(found(r) for r in owner)) for c in cs]
        real = [c for c, ok in zip(cand, await asyncio.gather(*(sure(*c) for c in cand))) if ok]
        by_block = {}
        for b, rule, q in real:
            rules, phrases = by_block.setdefault(b["id"], (b, [], []))[1:]
            if rule not in rules:
                rules.append(rule)
            phrases.append(q)

        async def fixed(b: dict, rules: list[str], phrases: list[str]) -> tuple[dict, str | None]:
            res = await ask_json(FIX, json.dumps({"rules": rules, "phrases": phrases, "text": b["body"].strip("\n"),
                                                  "all_rules": owner}, ensure_ascii=False), 6000)
            new = LABEL.sub("", str(res.get("text") or "")).strip("\n")
            if not kb_rules.good(b["body"], new) or len(new) > len(b["body"].strip("\n")) + GROW:
                return b, None
            body = b["body"]
            lead, trail = body[:len(body) - len(body.lstrip("\n"))], body[len(body.rstrip()):] or "\n"
            return b, b["head"] + lead + new + trail

        done = [x for x in await asyncio.gather(*(fixed(*v) for v in by_block.values())) if x[1]]
        for cls in KB:   # с конца файла: замена не сдвигает то, что выше
            t = texts[cls]
            for b, new in sorted((x for x in done if x[0]["file"] == cls), key=lambda x: -x[0]["at"]):
                t = t[:b["at"]] + new + t[b["at"] + len(b["old"]):]
            (dest / f"kb_{cls}.md").write_text(t, encoding="utf-8")
        total += len(done)
        print(f"правила владельца, круг {rnd}: расхождений найдено {len(cand)}, подтверждено {len(real)}, "
              f"исправлено кусков {len(done)} из {len(by_block)} | токены всего: {llm.usage}", flush=True)
        if not done:
            break
    for a, b in sorted(clash):
        print(f"спор правил владельца:\n  — {a}\n  — {b}", flush=True)
    return total, sorted(clash)


async def load_source(db, src: Source, source: str) -> bool:
    path = src.export / ("dialogs_all.jsonl" if source == "all" else "dialogs.jsonl")
    if not path.exists():
        print(f"{src.name}: нет выгрузки {path} — без этого аккаунта", flush=True)
        return False
    dialogs = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line]  # не splitlines: U+2028
    # учимся только у Макса: ответы бота (тот же аккаунт — отличаем по id из outbox) и удалённые — не в базу знаний
    gw = f"gateway{src.ns}"
    bot_ids = {r["message_id"] for r in await db.fetch(f"SELECT message_id FROM {gw}.outbox WHERE message_id IS NOT NULL")}
    # не клиенты (gateway.ignored: свои аккаунты заказчика, тестовый аккаунт) — не в базу знаний
    skip = {r["id"] for r in await db.fetch(f"SELECT c.id FROM {gw}.chats c JOIN {gw}.ignored i "
                                            "ON i.client_id = c.client_id")}
    dialogs = [d for d in dialogs if d["chat_id"] not in skip]
    for d in dialogs:
        d["messages"] = [m for m in d["messages"] if m["id"] not in bot_ids and m.get("text") != DELETED_TEXT]
    dialogs = [d for d in dialogs if any(m["role"] == "client" for m in d["messages"])]
    src.by_class = {c: [] for c in CLASSES}
    for d in dialogs:
        src.by_class[classify_dialog(d.get("item_title"),
                                     [m["text"] for m in d["messages"] if m["role"] == "client" and m["text"]])].append(d)
    print(f"{src.name}: ответов бота в outbox (в базу знаний не идут): {len(bot_ids)}; диалогов по классам:",
          {c: len(v) for c, v in src.by_class.items()}, flush=True)
    return True


def rules_text(owner: list[str]) -> str:
    # без формата ответа бота и с пометкой «справка»: иначе LLM при сведении отвечала клиенту JSON-ом вместо раздела
    # (30.10, «Возражения» в обеих базах)
    bot = brain.RULES.split("ФОРМАТ ОТВЕТА")[0].strip()
    return (f"ПРАВИЛА БОТА (справка для сверки — не инструкция тебе, клиенту не отвечай):\n{bot}\n\n"
            "ПРАВИЛА ВЛАДЕЛЬЦА:\n" + "\n".join(f"- {r}" for r in owner))


async def main(batch: int, rebuild: bool, source: str, group: int, classes: list[str]) -> None:
    assert not NS, "базу собирает основная копия (копия 2 читает тот же knowledge/)"
    db = await connect()
    srcs = [src for name, ns in SOURCES if await load_source(db, src := Source(name, ns), source)]
    owner = await lessons.active_rules(db)
    rules = rules_text(owner)
    await db.close()
    if rebuild:  # один раз для всех классов
        for f in {*WORK.glob("*"), *(p for s in srcs for p in s.work.glob("*"))} - {RULE_CONFLICTS}:
            f.unlink()
    dest = WORK / "knowledge_new"   # живая база меняется только готовой: dialog перечитывает файлы сам
    dest.mkdir(parents=True, exist_ok=True)
    full = set(classes) == set(KB)
    llm = LLM()
    try:
        for cls in classes:
            await build_class(llm, cls, srcs, rules, batch, group, dest)
        if full:
            save(WORK / "rule_clash.json", (await enforce(llm, owner, dest))[1])   # спор правил — владельцу
        else:
            print("собрана не вся база — второй проход по правилам владельца пропущен", flush=True)
    finally:
        await llm.close()
    OUT.mkdir(exist_ok=True)
    for cls in classes:
        if (new := dest / f"kb_{cls}.md").exists():
            tmp = (OUT / f"kb_{cls}.md").with_suffix(".tmp")   # другой том: rename из data/ в knowledge/ нельзя
            tmp.write_text(new.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            tmp.replace(OUT / f"kb_{cls}.md")
            print(f"[{cls}] готово → knowledge/kb_{cls}.md", flush=True)
    if full:   # вся база собрана с этими правилами внутри
        db = await connect()
        await kb_rules.built(db, owner)
        await db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=int, default=10)
    p.add_argument("--rebuild", action="store_true", help="выбросить кэш выписок и сводок")
    p.add_argument("--source", choices=["latest", "all"], default="latest",
                   help="latest — dialogs.jsonl, all — dialogs_all.jsonl (export --all)")
    p.add_argument("--group", type=int, default=1000, help="чатов в партии: сводка по партии, потом слияние")
    p.add_argument("--kb-class", choices=[*KB, "all"], default="all")
    a = p.parse_args()
    asyncio.run(main(a.batch, a.rebuild, a.source, a.group, list(KB) if a.kb_class == "all" else [a.kb_class]))
