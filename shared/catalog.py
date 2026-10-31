"""Прайс базовых услуг: хранение, вывод текстом, разбор присланного Максом прайса."""
import re
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg

SCHEMA = (Path(__file__).parent / "catalog_schema.sql").read_text(encoding="utf-8")

# «6 мес — 2 290₽ ХИТ», «20мес - 6890», «8 мес — удалить»; хвост после цены игнорируем
PRICE_LINE = re.compile(
    r"^\D{0,3}?(\d+)\s*мес\w*\.?\s*[-—–:]*\s*(удалить|нет|\d{1,3}(?:\s\d{3})+|\d+)\s*(?:₽|руб\w*\.?)?(?:\s.*)?$",
    re.I)
EMOJI = re.compile(r"[^\w\s()₽.,:;!?+\-—–/«»\"']")


def fmt_rub(n: int) -> str:
    return f"{n:,}".replace(",", " ") + "₽"


def gift_mark(n: int, raffle: bool = False) -> str:
    """🎁 — игра в подарок; «+ розыгрыш» — ещё и участие в розыгрыше топовой игры (от 14 мес)."""
    return ("" if not n else " 🎁" if n == 1 else f" {n} 🎁") + (" + розыгрыш" if raffle else "")


def render(products: list[dict], prices: dict[str, dict[int, int]]) -> str:
    """Раздел: заголовок, «N мес — цена» (+ 🎁, если к сроку игра в подарок), строка-примечание раздела."""
    blocks = []
    for p in products:
        rows, gifts, raffle = prices.get(p["code"], {}), p.get("gifts") or {}, p.get("raffle") or set()
        lines = [f"{m} мес — {fmt_rub(rows[m])}{gift_mark(gifts.get(m, 0), m in raffle)}" for m in sorted(rows)]
        blocks.append("\n".join([p["title"], *lines, *([p["note"]] if p.get("note") else [])]) if lines
                      else p["title"] + "\n(нет позиций)")
    return "\n\n".join(blocks)


@dataclass
class Parsed:
    changes: dict[str, dict[int, int | None]] = field(default_factory=dict)  # None = удалить
    errors: list[str] = field(default_factory=list)


def match_product(line: str, products: list[dict]) -> str | None:
    """Раздел по самому длинному совпавшему ключу: «XBOX … на Ваш аккаунт» — личный, а не консоль (xbox)."""
    low = EMOJI.sub(" ", line.lower())
    best = max(((len(k), -i, p["code"]) for i, p in enumerate(products) for k in p["keywords"]
                if re.search(rf"(?<!\w){re.escape(k)}", low)), default=None)
    return best and best[2]


def parse(text: str, products: list[dict]) -> Parsed:
    """Разделы узнаём по заголовкам; строки «N мес — цена». Не упомянутые позиции не трогаем."""
    res, current = Parsed(), None
    for raw in text.splitlines():
        line = EMOJI.sub(" ", raw).strip()
        if not line:
            continue
        m = PRICE_LINE.search(line)
        if m:
            if current is None:
                res.errors.append(f"«{raw.strip()}» — непонятно, к какому разделу (нужен заголовок: консоль или ПК)")
                continue
            months, val = int(m[1]), m[2].strip().lower()
            price = None if val in ("удалить", "нет") else int(re.sub(r"\s", "", val))
            if price is not None and not 50 <= price <= 100_000:
                res.errors.append(f"«{raw.strip()}» — странная цена")
                continue
            res.changes.setdefault(current, {})[months] = price
        elif code := match_product(line, products):
            current = code
        elif "подар" in line.lower():
            continue   # «🎁 Игра в подарок от 10 мес» — примечание раздела, не цена
        elif re.search(r"\d+\s*мес", line):
            res.errors.append(f"«{raw.strip()}» — не разобрал строку")
    return res


Change = tuple[str, int, int | None, int | None]  # (раздел, месяцев, было, стало); None = нет позиции


def diff(old: dict[str, dict[int, int]], changes: dict[str, dict[int, int | None]]) -> list[Change]:
    """Присланный раздел заменяет старый целиком: стёртые строки удаляются. Не присланные разделы не трогаем."""
    out = []
    for code, rows in changes.items():
        for months in sorted({*old.get(code, {}), *rows}):
            prev, new = old.get(code, {}).get(months), rows.get(months)
            if prev != new:
                out.append((code, months, prev, new))
    return out


def describe(changes: list[Change], products: list[dict]) -> str:
    titles = {p["code"]: re.sub(r"[^\w\s]", "", p["title"].split("(")[0]).strip() for p in products}
    lines = []
    for code, months, old, new in changes:
        if new is None:
            what = f"❌ убрать (было {fmt_rub(old)})"
        elif old is None:
            what = f"➕ добавить {fmt_rub(new)}"
        else:
            what = f"{fmt_rub(old)} → {fmt_rub(new)}"
            if not 0.5 <= new / old <= 2:
                what += f"  ⚠ цена изменилась в {max(new / old, old / new):.0f} раз — нет ли опечатки?"
        lines.append(f"• {titles.get(code, code)}, {months} мес: {what}")
    return "\n".join(lines)


# ---------- БД ----------
async def load(db: asyncpg.Pool) -> tuple[list[dict], dict[str, dict[int, int]]]:
    products = [dict(r) for r in await db.fetch(
        "SELECT code, title, keywords, on_request, note FROM catalog.products ORDER BY sort")]
    prices: dict[str, dict[int, int]] = {}
    gifts: dict[str, dict[int, int]] = {}
    raffle: dict[str, set[int]] = {}
    for r in await db.fetch("SELECT product, months, price_rub, gifts, raffle FROM catalog.prices"):
        prices.setdefault(r["product"], {})[r["months"]] = r["price_rub"]
        if r["gifts"]:
            gifts.setdefault(r["product"], {})[r["months"]] = r["gifts"]
        if r["raffle"]:
            raffle.setdefault(r["product"], set()).add(r["months"])
    games = ", ".join(x.strip(" 💎•-") for x in (await template(db, "raffle_games")).splitlines() if x.strip())
    for p in products:
        p["gifts"], p["raffle"] = gifts.get(p["code"], {}), raffle.get(p["code"], set())
        if p["note"]:
            p["note"] = p["note"].replace("{розыгрыш}", games or "топовые игры")
    return products, prices


async def price_text(db: asyncpg.Pool) -> str:
    return render(*await load(db))


async def price_parts(db: asyncpg.Pool, gifts: bool = True) -> tuple[str, str]:
    """(прайс для клиента, прайс «по запросу» — личный аккаунт: бот называет, только если клиент сам спросил).
    gifts=False — без 🎁 и строки о подарке: покупавшим подарок не положен."""
    products, prices = await load(db)
    if not gifts:
        products = [{**p, "gifts": {}, "raffle": set(), "note": None} for p in products]   # розыгрыш — часть подарка
    return (render([p for p in products if not p["on_request"]], prices),
            render([p for p in products if p["on_request"]], prices))


async def preview(db: asyncpg.Pool, text: str) -> tuple[list[Change], list[str], list[dict]]:
    """Что поменяется, если принять присланный прайс. В БД ничего не пишет."""
    products, prices = await load(db)
    parsed = parse(text, products)
    return diff(prices, parsed.changes), parsed.errors, products


async def apply(db: asyncpg.Pool, changes: list[Change], who: str) -> None:
    async with db.acquire() as con, con.transaction():
        for code, months, old, new in changes:
            if new is None:
                await con.execute("DELETE FROM catalog.prices WHERE product=$1 AND months=$2", code, months)
            else:
                await con.execute(
                    "INSERT INTO catalog.prices (product, months, price_rub, updated_by) VALUES ($1,$2,$3,$4) "
                    "ON CONFLICT (product, months) DO UPDATE SET price_rub=$3, updated_by=$4, updated_at=now()",
                    code, months, new, who)
            await con.execute("INSERT INTO catalog.price_history (product, months, old_rub, new_rub, changed_by) "
                              "VALUES ($1,$2,$3,$4,$5)", code, months, old, new, who)


async def template(db: asyncpg.Pool, code: str) -> str:
    """Текст владельца по коду (catalog.templates), например gift_list — игры в подарок на выбор."""
    return await db.fetchval("SELECT text FROM catalog.templates WHERE code=$1", code) or ""


TEMPLATES = {   # что владелец ведёт в VK кнопкой «🎁 Подарки»
    "gift_list": "🎁 Список игр в подарок — бот присылает клиенту на выбор (от 10 мес, первая покупка)",
    "raffle_games": "🎰 Игры для розыгрыша — бот упоминает при покупке от 14 мес (по одной в строке)",
}


async def set_template(db: asyncpg.Pool, code: str, text: str, who: str) -> None:
    await db.execute("INSERT INTO catalog.templates (code, text, updated_by) VALUES ($1,$2,$3) ON CONFLICT (code) "
                     "DO UPDATE SET text=$2, updated_by=$3, updated_at=now()", code, text, who)
