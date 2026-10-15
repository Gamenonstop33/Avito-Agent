"""Каталог игр Game Pass Ultimate — максимальный перечень из двух источников:
официальный каталог Microsoft (списки xbox.com/xbox-game-pass/games, платформы) и newxboxone.ru (обновляется ежедневно,
сборники по отдельным играм, пометка о русских субтитрах, новинки с датой). Игра «в подписке», если есть хоть в одном.
Раз в сутки обновляется в БД (шаг nightly) вместе со списком популярных у клиентов игр (game_requests).
Обновить вручную: python -m services.dialog.gamepass
"""
import asyncio
import html
import json
import logging
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from functools import lru_cache

import httpx

from shared.db import connect

SIGL = "https://catalog.gamepass.com/sigls/v2"
PRODUCTS = "https://displaycatalog.mp.microsoft.com/v7.0/products"
MARKET = "US"   # игры EA работают только с регионом США (база знаний)
LISTS = {       # списки xbox.com; EA Play входит в Ultimate отдельным списком
    "console": "f6f1f99f-9b49-4ccd-b3bf-4d9767a77f5e",
    "pc": "fdd9e2a7-0fee-49f6-ad69-4354098401ff",
    "ea_console": "b8900d09-a491-44cc-916e-32b5acae621b",
    "ea_pc": "1d33fbb9-b895-4732-a8ca-a55c8b99fa2c",
}
SITE = "https://newxboxone.ru/game-pass-ultimate"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MIN_GAMES = 300   # меньше — источник сломан, его прежний список не трогаем
POPULAR = 12      # сколько популярных игр держать для бота
NEW_DAYS = 45     # новинки — добавленные за столько дней

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS catalog;
CREATE TABLE IF NOT EXISTS catalog.gamepass (
    id         text PRIMARY KEY,           -- id продукта Microsoft Store | nx:<название> (сайт)
    title_ru   text NOT NULL,
    title_en   text NOT NULL,
    console    boolean,                    -- NULL — неизвестно (игра только на сайте)
    pc         boolean,
    ea_play    boolean,
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE catalog.gamepass ALTER COLUMN console DROP NOT NULL;
ALTER TABLE catalog.gamepass ALTER COLUMN pc DROP NOT NULL;
ALTER TABLE catalog.gamepass ALTER COLUMN ea_play DROP NOT NULL;
ALTER TABLE catalog.gamepass ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'ms';   -- ms | site
ALTER TABLE catalog.gamepass ADD COLUMN IF NOT EXISTS ru boolean;      -- русские субтитры (сайт)
ALTER TABLE catalog.gamepass ADD COLUMN IF NOT EXISTS added date;      -- добавлена в подписку (сайт, новинки)
-- игры, о которых спрашивали клиенты (services/dialog/game_requests.py)
CREATE TABLE IF NOT EXISTS catalog.game_mentions (
    chat_id  text NOT NULL,
    title    text NOT NULL,                -- официальное английское название (как его назвала LLM)
    first_at timestamptz NOT NULL,
    PRIMARY KEY (chat_id, title)
);
CREATE TABLE IF NOT EXISTS catalog.settings (key text PRIMARY KEY, value text);
"""
COLUMNS = ["id", "source", "title_ru", "title_en", "console", "pc", "ea_play", "ru", "added"]
log = logging.getLogger("gamepass")


async def fetch_ms(http: httpx.AsyncClient) -> list[dict]:
    where: dict[str, set[str]] = {}
    for name, sid in LISTS.items():
        r = await http.get(SIGL, params={"id": sid, "language": "en-us", "market": MARKET})
        r.raise_for_status()
        for x in r.json()[1:]:   # [0] — заголовок списка
            where.setdefault(x["id"], set()).add(name)
    ids, titles = list(where), {"ru-ru": {}, "en-us": {}}
    for lang, out in titles.items():
        for i in range(0, len(ids), 100):
            r = await http.get(PRODUCTS, params={"bigIds": ",".join(ids[i:i + 100]), "market": MARKET,
                                                 "languages": lang})
            r.raise_for_status()
            out.update({p["ProductId"]: p["LocalizedProperties"][0]["ProductTitle"] for p in r.json()["Products"]})
    return [{"id": i, "source": "ms", "title_ru": titles["ru-ru"].get(i) or titles["en-us"][i],
             "title_en": titles["en-us"][i], "console": bool(w & {"console", "ea_console"}),
             "pc": bool(w & {"pc", "ea_pc"}), "ea_play": bool(w & {"ea_console", "ea_pc"}), "ru": None, "added": None}
            for i, w in where.items() if i in titles["en-us"]]


def parse_site(page: str) -> list[dict]:
    """Полный список со страницы newxboxone.ru; новинки (слайдер выше списка) дают дату добавления."""
    i = page.find('id="game-list-results"')   # само слово встречается раньше — в стилях страницы
    if i < 0:
        raise RuntimeError("не нашёл список игр — поменялась разметка сайта")
    new = {html.unescape(t).strip(): datetime.strptime(d, "%d.%m.%Y").date() for t, d in re.findall(
        r'game-slide-title">([^<]+)</div>\s*<div class="game-slide-date">Добавлена:\s*([\d.]+)', page[:i])}
    games: dict[str, dict] = {}
    for li in re.findall(r"<li[^>]*>((?:(?!</li>).)*?game-title(?:(?!</li>).)*)</li>", page[i:], flags=re.S):
        title = html.unescape(re.search(r'class="game-title">([^<]+)<', li)[1]).strip()
        games[title] = {"id": f"nx:{title}", "source": "site", "title_ru": title, "title_en": title, "console": None,
                        "pc": None, "ea_play": None, "ru": "ru-subtitles" in li, "added": new.get(title)}
    return list(games.values())


async def fetch_site(http: httpx.AsyncClient) -> list[dict]:
    r = await http.get(SITE, headers={"User-Agent": UA}, follow_redirects=True)
    r.raise_for_status()
    return parse_site(r.text)


async def refresh(db) -> dict[str, int]:
    """Каждый источник обновляется независимо: сломался один — второй всё равно обновим, ошибку поднимем в конце."""
    got, errors = {}, []
    async with httpx.AsyncClient(timeout=30) as http:
        for source, fetcher in (("ms", fetch_ms), ("site", fetch_site)):
            try:
                games = await fetcher(http)
                if len(games) < MIN_GAMES:
                    raise RuntimeError(f"всего {len(games)} игр — похоже на сбой")
                got[source] = games
            except Exception as e:
                errors.append(f"{source}: {e}")
    if "site" in got:   # платформы для игр с сайта — из каталога Microsoft
        ms = got.get("ms") or [g for g in await load(db) if g["source"] == "ms"]
        for g in got["site"]:
            if hits := find(ms, g["title_en"], limit=20, fuzzy=False):
                g.update(console=any(h["console"] for h in hits), pc=any(h["pc"] for h in hits),
                         ea_play=any(h["ea_play"] for h in hits))
    async with db.acquire() as con, con.transaction():
        for source, games in got.items():
            await con.execute("DELETE FROM catalog.gamepass WHERE source=$1", source)
            await con.copy_records_to_table("gamepass", schema_name="catalog", columns=COLUMNS,
                                            records=[tuple(g[c] for c in COLUMNS) for g in games])
    pop = popular(await mentions(db), await load(db))
    await db.execute("INSERT INTO catalog.settings (key, value) VALUES ('popular', $1) "
                     "ON CONFLICT (key) DO UPDATE SET value=$1", json.dumps(pop, ensure_ascii=False))
    if errors:
        raise RuntimeError("каталог игр: " + "; ".join(errors) + (" (второй источник обновлён)" if got else ""))
    return {s: len(g) for s, g in got.items()}


async def load(db) -> list[dict]:
    return [dict(r) for r in await db.fetch("SELECT * FROM catalog.gamepass")]


async def mentions(db) -> list[dict]:
    return [dict(r) for r in await db.fetch("SELECT title, count(*) AS chats FROM catalog.game_mentions GROUP BY title")]


async def stats(db) -> dict | None:
    """Для блока КАТАЛОГ: размер, дата, популярные у клиентов и новинки."""
    r = await db.fetchrow("SELECT count(*) FILTER (WHERE source='site') AS site, "
                          "count(*) FILTER (WHERE source='ms' AND console) AS console, "
                          "count(*) FILTER (WHERE source='ms' AND pc) AS pc, max(updated_at) AS updated "
                          "FROM catalog.gamepass")
    if not r["updated"]:
        return None
    new = await db.fetch("SELECT title_en, added FROM catalog.gamepass WHERE source='site' AND added >= $1 "
                         "ORDER BY added DESC LIMIT 5", date.today() - timedelta(days=NEW_DAYS))
    pop = await db.fetchval("SELECT value FROM catalog.settings WHERE key='popular'")
    return {**dict(r), "popular": json.loads(pop) if pop else [], "new": [(x["title_en"], x["added"]) for x in new]}


# ---------- поиск (без сети и БД, покрыто тестами) ----------
ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}  # «x» нет: «Series X|S»
NOISE = {"the", "of", "a", "edition", "standard", "digital", "game", "издание", "стандартное", "цифровое", "игра"}


@lru_cache(maxsize=20000)
def tokens(s: str) -> tuple[str, ...]:
    return tuple(ROMAN.get(w, w) for w in re.findall(r"[a-zа-я0-9]+", s.lower().replace("ё", "е").replace("’", "'")))


def _has(q: str, words: tuple[str, ...], fuzzy: bool) -> bool:
    if q in words:
        return True
    return fuzzy and not q.isdigit() and len(q) >= 4 and any(  # опечатки, «assassins» ↔ «assassin s»; номер — точно
        not w.isdigit() and SequenceMatcher(None, q, w).ratio() >= 0.85 for w in words)


def find(games: list[dict], query: str, limit: int = 6, fuzzy: bool = True) -> list[dict]:
    """Игры, в названии которых (рус. или англ.) есть все слова запроса. Короткие названия — первыми."""
    q = [w for w in tokens(query) if w not in NOISE] or list(tokens(query))
    if not q:
        return []
    hits = [g for g in games if any(all(_has(w, tokens(g[t]), fuzzy) for w in q) for t in ("title_en", "title_ru"))]
    return sorted(hits, key=lambda g: len(g["title_en"]))[:limit]


def popular(rows: list[dict], games: list[dict], limit: int = POPULAR) -> list[str]:
    """Игры, о которых чаще всего спрашивали клиенты (по числу чатов) и которые сейчас есть в подписке."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:   # «Call of Duty: Black Ops 6» и «Call of Duty Black Ops 6» — одна игра
        groups.setdefault(tokens(r["title"]), []).append(r)
    out: list[str] = []
    for g in sorted(groups.values(), key=lambda g: -sum(r["chats"] for r in g)):
        name = max(g, key=lambda r: r["chats"])["title"]
        t = set(tokens(name))   # «Forza» рядом с «Forza Horizon 5» — повтор, пропускаем
        if any(t <= set(tokens(x)) or set(tokens(x)) <= t for x in out):
            continue
        if find(games, name, limit=1, fuzzy=False):
            out.append(name)
            if len(out) >= limit:
                break
    return out


def where(g: dict) -> str:
    return ", ".join([*(["консоль"] if g["console"] else []), *(["ПК"] if g["pc"] else []),
                      *(["EA Play"] if g["ea_play"] else []), *(["рус. субтитры"] if g["ru"] else [])])


def block(st: dict | None, looked: dict[str, list[dict]]) -> str:
    """Блок для промпта: размер каталога, популярное, новинки и найденное по запросам LLM."""
    if not st:
        return "\n\nКАТАЛОГ GAME PASS: не загружен — о наличии конкретных игр скажи, что уточнишь (handoff game)."
    total = max(st["site"], st["console"])
    lines = [f"\n\nКАТАЛОГ GAME PASS ULTIMATE (каталог Microsoft + newxboxone.ru, обновлён {st['updated']:%d.%m}): "
             f"клиенту — «больше {total // 50 * 50} игр»."]
    if st["popular"]:
        lines.append("ПОПУЛЯРНЫЕ У НАШИХ КЛИЕНТОВ (есть в подписке): " + ", ".join(st["popular"]))
    if st["new"]:
        lines.append("НОВИНКИ ПОДПИСКИ: " + ", ".join(f"{t} ({d:%d.%m})" for t, d in st["new"]))
    lines.append("Клиент в последнем сообщении упоминает какую-либо игру (например «фифа», «гта», «форза»)? Тогда "
                 "\"games\" ОБЯЗАТЕЛЬНО (фифа → \"EA Sports FC\"), о наличии — только по результатам поиска ниже.")
    lines += [f"«{q}»: " + ("; ".join(f"есть — {g['title_en']}" + (f" ({w})" if (w := where(g)) else "")
                                      for g in hits) if hits else "в подписке нет") for q, hits in looked.items()]
    return "\n".join(lines)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db = await connect(SCHEMA)
    log.info("каталог игр обновлён: %s", await refresh(db))
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
