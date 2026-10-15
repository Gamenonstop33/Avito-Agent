"""Таблица учёток в NocoDB: подключение к базе под ролью sheet, русские названия, скрытые служебные колонки. Повторный запуск безопасен (поменялись колонки sales.accounts — запустить снова).
Нужны NC_TOKEN (NocoDB → Team & Settings → Tokens) и SHEET_DB_PASSWORD в .env; запуск — sh scripts/sheet_setup.sh config.
Остальное (названия таблиц, ключевая и главная колонки, тип текстовых колонок, регистрация по приглашению) через API
не меняется (или меняется через ALTER, или не с токеном) — это sheet_setup.sh правит прямо в метаданных NocoDB."""
import os
import time

import httpx

BASE = "Учётки"
# таблица (вид) схемы sales → колонки по порядку с заголовками; остальные скрыты
COLUMNS = {
    "accounts": {"email": "Почта", "password": "Пароль", "recovery_email": "Резервная почта", "term": "Срок, мес",
                 "activated_at": "Активирована", "expires_at": "Действует до", "platform": "Платформа",
                 "free_slots": "Свободно",
                 "slot1_client": "Основа", "slot1_from": "Основа: с", "slot1_months": "Основа: мес",
                 "slot1_until": "Основа: до", "slot1_chat": "Основа: чат",
                 "slot2_client": "Доп", "slot2_from": "Доп: с", "slot2_months": "Доп: мес", "slot2_until": "Доп: до",
                 "slot2_chat": "Доп: чат",
                 "slot3_client": "ПК", "slot3_from": "ПК: с", "slot3_months": "ПК: мес", "slot3_until": "ПК: до",
                 "written_off": "Списана", "note": "Заметка", "id": "№"},
}
# во всех видах — по № от новых к старым; виды таблицы учёток кроме «Все»: название → фильтры (колонка, условие, значение); status free — у учётки пустая
# основа или доп (считает триггер), колонка скрыта, но фильтр по ней работает
VIEWS = {"Действующие": [("written_off", "notchecked", None)], "Есть место": [("status", "eq", "free")]}

nc = httpx.Client(base_url=os.environ.get("NC_URL", "http://nocodb:8080"), timeout=60,
                  headers={"xc-token": os.environ["NC_TOKEN"]})


def call(method: str, path: str, body: dict | None = None):
    r = nc.request(method, path, json=body)
    if r.is_error:
        raise SystemExit(f"{method} {path}: {r.status_code} {r.text[:300]}")
    return r.json()


def tables(base_id: str) -> dict[str, dict]:
    return {t["table_name"]: t for t in call("GET", f"/api/v2/meta/bases/{base_id}/tables")["list"]}


def source(base_id: str) -> dict | None:
    return next((s for s in call("GET", f"/api/v2/meta/bases/{base_id}/sources")["list"] if s["type"] == "pg"), None)


def missing(base_id: str) -> set[str]:
    """Каких таблиц и колонок из COLUMNS NocoDB не видит (и какие лишние таблицы видит — права у sheet отозвали)."""
    found = tables(base_id)
    miss = set(COLUMNS) ^ set(found)
    for name in set(COLUMNS) & set(found):
        cols = {c["column_name"] for c in call("GET", f"/api/v2/meta/tables/{found[name]['id']}")["columns"]}
        miss |= {f"{name}.{c}" for c in COLUMNS[name] if c not in cols}
    return miss


def wait(ready, what: str) -> None:
    for _ in range(60):   # подключение и отключение базы NocoDB делает фоновой задачей
        if ready():
            return
        time.sleep(2)
    raise SystemExit(what)


def main() -> None:
    base = next((b for b in call("GET", "/api/v2/meta/bases")["list"] if b["title"] == BASE), None)         or call("POST", "/api/v2/meta/bases", {"title": BASE})
    src = source(base["id"])
    if src and missing(base["id"]):
        # новые колонки или убранные таблицы: «синхронизировать метаданные» по токену API нельзя — подключение создаётся заново
        # (это только метаданные NocoDB, данные в Postgres не трогаются); заголовки и виды ниже ставятся снова
        call("DELETE", f"/api/v2/meta/bases/{base['id']}/sources/{src['id']}")
        wait(lambda: not source(base["id"]), "NocoDB не отключил старое подключение к базе")
        src = None
    if not src:
        call("POST", f"/api/v2/meta/bases/{base['id']}/sources", {
            "alias": "sales", "type": "pg", "inflection_column": "none", "inflection_table": "none",
            "config": {"client": "pg", "searchPath": ["sales"], "connection": {
                "host": "db", "port": 5432, "user": "sheet", "password": os.environ["SHEET_DB_PASSWORD"],
                "database": "agent"}}})
        wait(lambda: source(base["id"]) and not missing(base["id"]),   # таблицы и колонки он читает не сразу
             "NocoDB не видит нужных таблиц или колонок: роль sheet — sheet_setup.sh init, права ей выдаются "
             "при старте сервисов")
        src = source(base["id"])
    found = tables(base["id"])
    # заголовки — только в NocoDB: при «только чтение схемы» он их не меняет, а ALTER у роли sheet и так не пройдёт
    call("PATCH", f"/api/v2/meta/bases/{base['id']}/sources/{src['id']}", {"is_schema_readonly": False})
    for name, titles in COLUMNS.items():
        table = call("GET", f"/api/v2/meta/tables/{found[name]['id']}")
        if name == "accounts" and set(VIEWS) - {v["title"] for v in table["views"]}:
            cols = {c["column_name"]: c["id"] for c in table["columns"]}
            for title in set(VIEWS) - {v["title"] for v in table["views"]}:
                view = call("POST", f"/api/v2/meta/tables/{table['id']}/grids", {"title": title})
                for col, op, value in VIEWS[title]:
                    call("POST", f"/api/v2/meta/views/{view['id']}/filters",
                         {"fk_column_id": cols[col], "comparison_op": op, "value": value})
            table = call("GET", f"/api/v2/meta/tables/{table['id']}")
        cols = {c["id"]: c for c in table["columns"]}
        for c in cols.values():
            if c["column_name"] in titles and c["title"] != titles[c["column_name"]] and not c.get("pk"):
                call("PATCH", f"/api/v2/meta/columns/{c['id']}", {"title": titles[c["column_name"]]})
        order = list(titles)
        if table["views"] and table["views"][0]["title"] != "Все":
            call("PATCH", f"/api/v2/meta/views/{table['views'][0]['id']}", {"title": "Все"})
        key = next(c["id"] for c in cols.values() if c.get("pk"))
        for view in table["views"]:
            for vc in call("GET", f"/api/v2/meta/views/{view['id']}/columns")["list"]:
                col = cols.get(vc["fk_column_id"], {}).get("column_name")
                show = col in titles
                call("PATCH", f"/api/v2/meta/views/{view['id']}/columns/{vc['id']}",
                     {"show": show, "order": order.index(col) + 1 if show else len(order) + 1})
            if not any(x["fk_column_id"] == key for x in call("GET", f"/api/v2/meta/views/{view['id']}/sorts")["list"]):
                call("POST", f"/api/v2/meta/views/{view['id']}/sorts", {"fk_column_id": key, "direction": "desc"})
    call("PATCH", f"/api/v2/meta/bases/{base['id']}/sources/{src['id']}", {"is_schema_readonly": True})
    print(f"NocoDB: база «{BASE}», таблицы {', '.join(COLUMNS)}")


if __name__ == "__main__":
    main()
