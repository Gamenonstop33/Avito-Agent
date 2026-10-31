"""Разбор Google-таблицы учёток владельца (выгрузка .xlsx, 15.10) для переноса в sales.accounts.

Колонки: «Аккаунты» (почта, пароль, резервная почта; в новых строках первая строка — метка срока «2m»),
«Срок подписки», «Пользователи» (по строке на клиента: «Имя ДД.ММ. N мес [пометки]»; первый — основа, второй — доп,
с «ПК» — место «ПК»), «Оключить» и случайные ячейки правее — в заметку. Год в датах почти всегда не указан:
строки идут по порядку, год восстанавливается по соседним строкам (infer_dates)."""
import re
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

from shared.sales import add_months

EMAIL = re.compile(r"[^@\s|]+@[^@\s|]+\.[A-Za-z]{2,}")
TERM = re.compile(r"(?i)^\s*(?:ult\s*)?(\d{1,2})\s*(?:m|м|мес)(?![a-zа-я])")   # «2m», «4 м», «Ult 2m»
DATE = re.compile(r"(?<![\d.])(\d{1,2})\s*\.\s*\.?\s*(\d{1,2})(?:\.(\d{2,4}))?\.?(?![\d])")
MONTHS = re.compile(r"(?i)(?<![\d.,])(\d{1,2})\s*(?:мес\w*|мекс|m(?![a-z]))")
PC = re.compile(r"(?i)(?<!\+)(?<!\+ )(?<![\w])(?:пк|pc)(?!\w)")
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def read_xlsx(path: str) -> list[tuple[int, dict[str, str]]]:
    """Первый лист: [(номер строки, {буква колонки: текст})], только непустые ячейки."""
    z = zipfile.ZipFile(path)
    shared = [] if "xl/sharedStrings.xml" not in z.namelist() else [
        "".join(t.text or "" for t in si.iter(NS + "t"))
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(NS + "si")]
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    first = ET.fromstring(z.read("xl/workbook.xml")).find(NS + "sheets")[0]
    sheet = ET.fromstring(z.read("xl/" + rels[first.get(REL + "id")].replace("/xl/", "").lstrip("/")))
    rows = []
    for row in sheet.iter(NS + "row"):
        cells = {}
        for c in row.findall(NS + "c"):
            v = c.find(NS + "v")
            val = (shared[int(v.text)] if c.get("t") == "s" else v.text) if v is not None else \
                "".join(t.text or "" for t in c.iter(NS + "t"))
            if val and val.strip():
                cells[re.match(r"[A-Z]+", c.get("r")).group(0)] = val
        if cells:
            rows.append((int(row.get("r")), cells))
    return rows


def clean(text: str) -> str:
    text = re.sub(r"(?:^|\s)[-–—]+(?=\s|$)", " ", text.replace("|", " "))   # «Иван 08.12. - 14 мес» → без тире
    return re.sub(r"\s+", " ", text).strip(" -–—,;")


def parse_account(cell: str) -> dict:
    """Ячейка «Аккаунты»: метка срока, почта, пароль, резервная почта; остальное — leftover."""
    lines = [x.strip() for x in cell.split("\n") if x.strip()]
    term, extra = None, []
    if lines and not EMAIL.search(lines[0]) and (m := TERM.match(lines[0]) or re.fullmatch(r"(\d{1,2})", lines[0])):
        term = int(m.group(1))
        extra.append(lines.pop(0)[m.end():])
    email = password = recovery = None
    tokens = []
    for t in " ".join(lines).split():   # «почта:пароль» одной строкой
        head, sep, tail = t.partition(":")
        tokens += [head, tail] if sep and tail and EMAIL.fullmatch(head) else [t]
    for t in tokens:
        if EMAIL.fullmatch(t.strip(",;")):
            t = t.strip(",;")
            if not email:
                email = t
            elif not recovery and password:
                recovery = t
            else:
                extra.append(t)
        elif email and not password:
            password = t
        else:
            extra.append(t)
    return {"term": term, "email": email, "password": password, "recovery": recovery, "extra": clean(" ".join(extra))}


def parse_users(cell: str) -> list[dict]:
    """Ячейка «Пользователи» → записи {text, d, m, y, months, pc}; несколько клиентов в одной строке делятся по датам."""
    out: list[dict] = []
    for line in (x.strip() for x in cell.split("\n")):
        if not clean(line):
            continue
        dates = list(DATE.finditer(line))
        if not dates:
            out.append({"raw": line, "name": line, "date": None})
            continue
        name = line[:dates[0].start()]
        for i, m in enumerate(dates):
            after = line[m.end():dates[i + 1].start() if i + 1 < len(dates) else len(line)]
            if i + 1 < len(dates):   # между двумя датами: срок первого клиента, дальше — имя следующего
                mm = MONTHS.match(after.lstrip(" -–"))
                cut = len(after) - len(after.lstrip(" -–")) + mm.end() if mm else 0
                tail, nxt = after[:cut], after[cut:]
            else:
                tail, nxt = after, ""
            out.append({"raw": name + " " + tail, "name": name, "date": m})
            name = nxt
    entries: list[dict] = []
    for e in out:
        raw, m = e["raw"], e["date"]
        if m and not clean(e["name"]) and entries and entries[-1]["date"] is None:
            raw = entries.pop()["text"] + " " + raw   # имя строкой выше, дата — отдельной строкой
        mm = MONTHS.search(raw)
        pc = bool(PC.search(raw))
        if pc:   # место «ПК» — пометку из имени убираем
            raw = PC.sub(" ", raw)
            mm = MONTHS.search(raw)
        y = int(m.group(3)) if m and m.group(3) else None
        y = (y + 2000 if y < 100 else y) if y else None
        entries.append({
            "text": clean(MONTHS.sub(" ", raw, count=1) if mm else raw),
            "date": m and (int(m.group(1)), int(m.group(2)), y if y and 2024 <= y <= 2026 else None),
            "months": int(mm.group(1)) if mm else None,
            "pc": pc,
        })
    return [e for e in entries if e["text"] or e["date"]]


def pick_date(d: int, mo: int, near: date, today: date, after: date | None = None) -> date | None:
    """День.месяц без года → дата: ближайшая к near (не позже сегодня); after — не раньше этой даты, если можно."""
    cands = []
    for y in (near.year - 1, near.year, near.year + 1):
        try:
            c = date(y, mo, d)
        except ValueError:
            continue
        if c <= today + timedelta(days=3):
            cands.append(c)
    if not cands:
        return None
    if after:
        later = [c for c in cands if c >= after]
        if later:
            return min(later)
    return min(cands, key=lambda c: abs((c - near).days))


def infer_dates(rows: list[list[dict]], today: date) -> None:
    """Проставляет записям поле day (date): год — по соседним строкам (таблица ведётся сверху вниз по времени)."""
    cur = next((date(e["date"][2], e["date"][1], e["date"][0]) for r in rows for e in r
                if e["date"] and e["date"][2]), today)
    for entries in rows:
        base = None
        for e in entries:
            if not e["date"]:
                e["day"] = None
                continue
            d, mo, y = e["date"]
            try:
                day = date(y, mo, d) if y else None
            except ValueError:
                day = None
            if not day:   # первый клиент — не раньше соседних строк, остальные — не раньше первого (дописаны позже)
                day = pick_date(d, mo, base or cur, today, after=base - timedelta(days=45) if base else
                                cur - timedelta(days=60))
            e["day"] = day
            if day and not base:
                base = day
        # опечатка в месяце («04.03» среди сентябрьских строк) опорную дату назад не утаскивает
        if base and cur - timedelta(days=30) <= base <= cur + timedelta(days=200):
            cur = base


def parse_sheet(rows: list[tuple[int, dict[str, str]]], today: date) -> list[dict]:
    """Строки листа (без заголовка) → учётки для вставки в sales.accounts (колонки как в таблице, slotN_*)."""
    users = [parse_users(c.get("C", "")) for _, c in rows]
    infer_dates(users, today)
    out = []
    for (num, c), entries in zip(rows, users):
        acc = parse_account(c.get("A", ""))
        note = [f"из «Аккаунты»: {acc['extra']}"] if acc["extra"] else []
        b = re.search(r"\d+", c.get("B", ""))
        months = int(b.group(0)) if b else acc["term"]
        if not months or not 1 <= months <= 24:
            note.append(f"срок учётки не разобран («{clean(c.get('B', ''))}») — поставлен 2 мес")
            months = 2
        main = [e for e in entries if not e["pc"]]
        pcs = [e for e in entries if e["pc"]]
        rec = {"sheet_row": num, "email": acc["email"], "password": acc["password"], "recovery_email": acc["recovery"],
               "months": months}
        for k, e in ((1, main[0] if main else None), (2, main[1] if len(main) > 1 else None),
                     (3, pcs[0] if pcs else None)):
            rec[f"slot{k}_client"] = e and (e["text"] or "без имени")
            rec[f"slot{k}_from"] = e and e["day"]
            rec[f"slot{k}_months"] = e and e["months"]
        rest = main[2:] + pcs[1:]
        if rest:
            note.append("ещё в «Пользователи»: " + "; ".join(
                e["text"] + (f" {e['day']:%d.%m.%y}" if e["day"] else "") + (f" {e['months']} мес" if e["months"] else "")
                for e in rest))
        if any(not e["day"] for e in entries) or len(entries) > 3 or pcs:   # Максу — сверить
            note.append("было: " + clean(c.get("C", "")))
        for col in sorted(set(c) - {"A", "B", "C"}):
            note.append(clean(c[col]))
        starts = [e["day"] for e in entries if e["day"]]
        rec["activated_at"] = starts[0] if starts else None
        end = rec["activated_at"] and add_months(rec["activated_at"], months)
        rec["written_off"] = bool(end and end < today)
        rec["note"] = "; ".join(x for x in note if x) or None
        out.append(rec)
    seen: dict[str, dict] = {}   # одна почта в нескольких строках: действующей остаётся последняя
    for rec in out:
        key = (rec["email"] or "").lower()
        if key and not rec["written_off"]:
            if key in seen:
                old = seen[key]
                old["written_off"] = True
                old["note"] = "; ".join(x for x in (old["note"], f"повтор почты, действующая — строка {rec['sheet_row']}")
                                        if x)
            seen[key] = rec
    return out

