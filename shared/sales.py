"""Продажи: заказы, реквизиты по кругу банков, склад аккаунтов для выдачи."""
import re
from datetime import datetime, timedelta, timezone

import asyncpg

from shared import catalog, crm
from shared.config import NS, env, env_list

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS sales;
-- Банки для переводов по СБП: выдаются по кругу (sort), номер и получатель — PAY_PHONE / PAY_RECIPIENT в .env
CREATE TABLE IF NOT EXISTS sales.banks (
    name   text PRIMARY KEY,
    sort   int NOT NULL,
    active boolean NOT NULL DEFAULT true
);
-- с 07.10 по кругу только эти четыре (Ozon и Райффайзен на сервере выключены: active=false)
INSERT INTO sales.banks (name, sort) VALUES ('Альфа-Банк',1), ('Т-Банк',2), ('ОТП Банк',3), ('Газпромбанк',4)
    ON CONFLICT DO NOTHING;
"""

# Склад учёток общий для всех аккаунтов Avito (28.10): создаёт и меняет его только основная копия бота (без DB_NS)
STOCK_SCHEMA = """
-- Склад учёток с подпиской (реестр ведёт владелец в VK «📦 Аккаунты»). Консольная учётка — на двух клиентов:
-- первый делает её домашней, второй входит в неё при каждом включении. Кто на учётке — sales.orders.stock_id/slot.
CREATE TABLE IF NOT EXISTS sales.accounts (
    id         bigserial PRIMARY KEY,
    product    text NOT NULL,                  -- catalog.products.code: console | pc | personal
    months     int NOT NULL,                   -- срок подписки на учётке (2, 4, 6 мес)
    email      text,
    login      text,
    password   text,
    note       text,
    status     text NOT NULL DEFAULT 'free',   -- free (есть место) | full | off (списана: подписка кончилась)
    order_id   bigint,
    issued_at  timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE sales.accounts ADD COLUMN IF NOT EXISTS recovery_email text;
ALTER TABLE sales.accounts ADD COLUMN IF NOT EXISTS activated_at timestamptz;   -- с внесения в реестр идёт срок
ALTER TABLE sales.accounts ADD COLUMN IF NOT EXISTS expires_at timestamptz;     -- активация + срок; дальше — списание
-- Учётки владелец ведёт и в таблице (NocoDB, роль sheet — права ниже) — как свою Google-таблицу (15.10): строка —
-- учётка, места «Основа» (slot1: 1-я инструкция, делает домашней), «Доп» (slot2: 2-я) и «ПК» (slot3: ещё клиент на ПК,
-- бот туда не сажает): имя, с какого числа, на сколько месяцев, до (считается), чат Avito (заполняет бот при выдаче).
-- С 20.10 ничего не обязательно и не проверяется: пробелы в почте и пароле убираются, пустой срок — 2 мес, без даты
-- активации — сегодня, у места без «с» — сегодня (или дата из текста «Иван 20.10 6 мес»), срок места не указан — из
-- текста; конец срока учётки и мест считается всегда. Платформа и срок для таблицы — platform и term (enum), бот
-- работает с product и months — триггер держит их одинаковыми. Галочка written_off («Списана») и status='off' — одно и
-- то же; иначе status free, пока боту есть куда посадить клиента (консоль — пустая основа или доп, ПК-учётка — основа);
-- free_slots — какие места пустые. sheet_row — строка в Google-таблице владельца (scripts/import_accounts_sheet.py)
ALTER TABLE sales.accounts ADD COLUMN IF NOT EXISTS written_off boolean DEFAULT false;
DO $do$ BEGIN
    CREATE TYPE sales.platform AS ENUM ('консоль', 'пк');
EXCEPTION WHEN duplicate_object THEN NULL;
END $do$;
DO $do$ BEGIN
    CREATE TYPE sales.term AS ENUM ('1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12');
EXCEPTION WHEN duplicate_object THEN NULL;
END $do$;
ALTER TABLE sales.accounts ADD COLUMN IF NOT EXISTS platform sales.platform, ADD COLUMN IF NOT EXISTS term sales.term,
    ADD COLUMN IF NOT EXISTS slot1_client text, ADD COLUMN IF NOT EXISTS slot1_from date,
    ADD COLUMN IF NOT EXISTS slot1_months int, ADD COLUMN IF NOT EXISTS slot1_until date,
    ADD COLUMN IF NOT EXISTS slot1_chat text,
    ADD COLUMN IF NOT EXISTS slot2_client text, ADD COLUMN IF NOT EXISTS slot2_from date,
    ADD COLUMN IF NOT EXISTS slot2_months int, ADD COLUMN IF NOT EXISTS slot2_until date,
    ADD COLUMN IF NOT EXISTS slot2_chat text,
    ADD COLUMN IF NOT EXISTS slot3_client text, ADD COLUMN IF NOT EXISTS slot3_from date,
    ADD COLUMN IF NOT EXISTS slot3_months int, ADD COLUMN IF NOT EXISTS slot3_until date,
    ADD COLUMN IF NOT EXISTS free_slots text, ADD COLUMN IF NOT EXISTS sheet_row int;
-- NOT NULL NocoDB считает обязательным полем — значения подставляет триггер
ALTER TABLE sales.accounts ALTER COLUMN product SET DEFAULT 'console', ALTER COLUMN months SET DEFAULT 2,
    ALTER COLUMN activated_at SET DEFAULT now(),
    ALTER COLUMN product DROP NOT NULL, ALTER COLUMN months DROP NOT NULL, ALTER COLUMN status DROP NOT NULL,
    ALTER COLUMN written_off DROP NOT NULL, ALTER COLUMN created_at DROP NOT NULL;
ALTER TABLE sales.accounts DROP CONSTRAINT IF EXISTS accounts_values;   -- 15–20.10 проверки мешали заполнять таблицу
DROP INDEX IF EXISTS sales.accounts_email;
DROP TABLE IF EXISTS sales.stock_in;   -- «Новые учётки» (20.10) убраны: учётки заводят прямо в склад
DROP FUNCTION IF EXISTS sales.stock_in_sync();
DROP FUNCTION IF EXISTS sales.stock_in_delete();
CREATE OR REPLACE FUNCTION sales.slot_day(txt text) RETURNS date LANGUAGE plpgsql STABLE AS $f$
DECLARE
    m text[];
    d date;
BEGIN
    m := regexp_match(txt, '(?:^|[^0-9.])([0-9]{1,2})\\s*\\.\\s*\\.?\\s*([0-9]{1,2})(?:\\.([0-9]{2,4}))?');
    IF m IS NULL THEN
        RETURN NULL;
    END IF;
    BEGIN
        d := make_date(CASE WHEN m[3] IS NULL THEN extract(year FROM now() AT TIME ZONE 'Europe/Moscow')::int
                            WHEN length(m[3]) = 2 THEN 2000 + m[3]::int ELSE m[3]::int END, m[2]::int, m[1]::int);
    EXCEPTION WHEN others THEN
        RETURN NULL;
    END;
    IF m[3] IS NULL AND d > (now() AT TIME ZONE 'Europe/Moscow')::date + 3 THEN
        d := (d - interval '1 year')::date;
    END IF;
    RETURN d;
END $f$;
CREATE OR REPLACE FUNCTION sales.slot_months(txt text) RETURNS int LANGUAGE sql IMMUTABLE AS $f$
    SELECT (regexp_match(txt, '(?:^|[^0-9.,])([0-9]{1,2})\\s*(?:мес|мекс|m(?![a-z])|м(?![а-я]))'))[1]::int
$f$;
CREATE OR REPLACE FUNCTION sales.accounts_sheet() RETURNS trigger LANGUAGE plpgsql
    SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $f$
BEGIN
    -- скопировано с пробелом («почта@outlook.com ») — проверка формата почты не пропускала (20.10)
    NEW.email := nullif(regexp_replace(NEW.email, '\\s', '', 'g'), '');
    NEW.recovery_email := nullif(regexp_replace(NEW.recovery_email, '\\s', '', 'g'), '');
    NEW.password := nullif(btrim(NEW.password), '');
    NEW.product := coalesce(NEW.product, 'console');
    IF TG_OP = 'INSERT' AND NEW.platform IS NOT NULL OR TG_OP = 'UPDATE' AND NEW.platform IS DISTINCT FROM OLD.platform THEN
        NEW.product := CASE NEW.platform WHEN 'пк' THEN 'pc' ELSE 'console' END;
    END IF;
    NEW.platform := CASE NEW.product WHEN 'pc' THEN 'пк' ELSE 'консоль' END::sales.platform;
    IF TG_OP = 'INSERT' AND NEW.term IS NOT NULL OR TG_OP = 'UPDATE' AND NEW.term IS DISTINCT FROM OLD.term THEN
        NEW.months := NEW.term::text::int;
    END IF;
    NEW.months := coalesce(NEW.months, 2);
    NEW.term := CASE WHEN NEW.months BETWEEN 1 AND 12 THEN NEW.months::text::sales.term END;
    NEW.activated_at := coalesce(NEW.activated_at, now());
    NEW.expires_at := NEW.activated_at + make_interval(months => NEW.months);
    NEW.slot1_client := nullif(btrim(NEW.slot1_client), '');
    NEW.slot2_client := nullif(btrim(NEW.slot2_client), '');
    NEW.slot3_client := nullif(btrim(NEW.slot3_client), '');
    NEW.slot1_chat := nullif(btrim(NEW.slot1_chat), '');
    NEW.slot2_chat := nullif(btrim(NEW.slot2_chat), '');
    IF NEW.slot1_client IS NULL THEN   -- место освободили
        NEW.slot1_from := NULL; NEW.slot1_months := NULL; NEW.slot1_chat := NULL;
    ELSE
        NEW.slot1_from := coalesce(NEW.slot1_from, sales.slot_day(NEW.slot1_client),
                                    (now() AT TIME ZONE 'Europe/Moscow')::date);
        NEW.slot1_months := coalesce(NEW.slot1_months, sales.slot_months(NEW.slot1_client));
    END IF;
    NEW.slot1_until := (NEW.slot1_from + make_interval(months => NEW.slot1_months))::date;
    IF NEW.slot2_client IS NULL THEN   -- место освободили
        NEW.slot2_from := NULL; NEW.slot2_months := NULL; NEW.slot2_chat := NULL;
    ELSE
        NEW.slot2_from := coalesce(NEW.slot2_from, sales.slot_day(NEW.slot2_client),
                                    (now() AT TIME ZONE 'Europe/Moscow')::date);
        NEW.slot2_months := coalesce(NEW.slot2_months, sales.slot_months(NEW.slot2_client));
    END IF;
    NEW.slot2_until := (NEW.slot2_from + make_interval(months => NEW.slot2_months))::date;
    IF NEW.slot3_client IS NULL THEN   -- место освободили
        NEW.slot3_from := NULL; NEW.slot3_months := NULL;
    ELSE
        NEW.slot3_from := coalesce(NEW.slot3_from, sales.slot_day(NEW.slot3_client),
                                    (now() AT TIME ZONE 'Europe/Moscow')::date);
        NEW.slot3_months := coalesce(NEW.slot3_months, sales.slot_months(NEW.slot3_client));
    END IF;
    NEW.slot3_until := (NEW.slot3_from + make_interval(months => NEW.slot3_months))::date;
    NEW.written_off := coalesce(NEW.written_off, false);
    IF NEW.status = 'off' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'off') THEN
        NEW.written_off := true;   -- списал бот: срок учётки кончился (WRITE_OFF) или VK «🗑 списать»
    END IF;
    NEW.status := CASE WHEN NEW.written_off THEN 'off'
        WHEN NEW.slot1_client IS NULL OR NEW.product = 'console' AND NEW.slot2_client IS NULL THEN 'free' ELSE 'full' END;
    NEW.free_slots := CASE WHEN NEW.written_off THEN '—' ELSE coalesce(nullif(concat_ws(', ',
        CASE WHEN NEW.slot1_client IS NULL THEN 'основа' END,
        CASE WHEN NEW.product = 'console' AND NEW.slot2_client IS NULL THEN 'доп' END,
        CASE WHEN NEW.product = 'console' AND NEW.slot3_client IS NULL THEN 'пк' END), ''), '—') END;
    NEW.created_at := coalesce(NEW.created_at, now());
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER accounts_sheet BEFORE INSERT OR UPDATE ON sales.accounts
    FOR EACH ROW EXECUTE FUNCTION sales.accounts_sheet();
UPDATE sales.accounts SET platform = CASE product WHEN 'pc' THEN 'пк' ELSE 'консоль' END::sales.platform
    WHERE platform IS NULL OR free_slots IS NULL OR term IS NULL AND months <= 12;   -- новые колонки старых строк
                                                                                     -- считает триггер
"""
SCHEMA += "" if NS else STOCK_SCHEMA
SCHEMA += """
-- Заказы: что и на какой срок купил клиент, какие реквизиты выданы, какой аккаунт выдан
CREATE TABLE IF NOT EXISTS sales.orders (
    id           bigserial PRIMARY KEY,
    chat_id      text NOT NULL,
    account_id   bigint NOT NULL,              -- аккаунт Avito
    client_id    bigint,                       -- avito user id клиента
    product      text NOT NULL,
    months       int NOT NULL,
    amount       int NOT NULL,
    bank         text NOT NULL,
    status       text NOT NULL DEFAULT 'awaiting',   -- awaiting | checking | paid | cancelled
    created_at   timestamptz NOT NULL DEFAULT now(),
    claimed_at   timestamptz,                  -- клиент сообщил об оплате
    paid_at      timestamptz,                  -- владелец подтвердил
    confirmed_by text,
    stock_id     bigint REFERENCES sales.accounts,
    expires_at   timestamptz                   -- конец подписки (от даты оплаты)
);
CREATE INDEX IF NOT EXISTS orders_chat ON sales.orders (chat_id, status);
ALTER TABLE sales.orders ADD COLUMN IF NOT EXISTS slot int;            -- место на учётке: 1 — делает домашней, 2 — второй
ALTER TABLE sales.orders ADD COLUMN IF NOT EXISTS issued_months int;   -- срок выданной учётки; меньше заказанного —
                                                                       -- нужного срока на складе не было
ALTER TABLE sales.orders ADD COLUMN IF NOT EXISTS discount int NOT NULL DEFAULT 0;   -- скидка за отзыв в сумме, ₽
ALTER TABLE sales.orders ADD COLUMN IF NOT EXISTS connected_at timestamptz;   -- консоль: инструкция после входа по коду;
                                                                             -- ПК: клиенту ушли данные учётки
-- card — перевод по номеру (банки по кругу, поступление проверяет владелец); ip — СБП на ИП через шлюз Альфа-Банка
-- (оплату видит бот и выдаёт сам, хоть ночью); pay_id/pay_url — заказ в шлюзе и ссылка СБП
ALTER TABLE sales.orders ADD COLUMN IF NOT EXISTS method text NOT NULL DEFAULT 'card';
ALTER TABLE sales.orders ADD COLUMN IF NOT EXISTS pay_id text;
ALTER TABLE sales.orders ADD COLUMN IF NOT EXISTS pay_url text;
ALTER TABLE sales.orders ADD COLUMN IF NOT EXISTS test boolean NOT NULL DEFAULT false;   -- ALFA_TEST_CLIENTS: склад
                                                                                        -- и CRM не трогаем
-- заказы не висят вечно (решение 21.10): ждёт оплату сутки — cancelled; «на проверке» сутки — одно напоминание
-- владельцу (reminded_at), три дня — unconfirmed (оплату так и не подтвердили)
ALTER TABLE sales.orders ADD COLUMN IF NOT EXISTS reminded_at timestamptz;
-- Настройки продаж из VK: pay_mode — способ оплаты mix | ip | card (кнопка «💳 Способ оплаты»; нет записи — card),
-- auto_issue — автовыдача creds | self | off (кнопка «🤖 Автовыдача», 22–23.10; нет записи — SALES_AUTO_ISSUE)
CREATE TABLE IF NOT EXISTS sales.settings (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""
SCHEMA += "" if NS else """
-- 20.10 перенесённые места склеивали в одну ячейку («Ольга 01.12 12 мес»), в тот же день вернули раздельные колонки —
-- разово отрезаем дату и срок обратно (метка в sales.settings, чтобы позже не трогать то, что пишет владелец)
DO $do$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM sales.settings WHERE key = 'slots_split_2010') THEN
        UPDATE sales.accounts SET slot1_client = left(slot1_client, length(slot1_client) - length(' ' || to_char(slot1_from, 'DD.MM') || coalesce(' ' || slot1_months || ' мес', '')))
            WHERE sheet_row IS NOT NULL AND slot1_from IS NOT NULL AND slot1_client LIKE '%' || (' ' || to_char(slot1_from, 'DD.MM') || coalesce(' ' || slot1_months || ' мес', ''));
        UPDATE sales.accounts SET slot2_client = left(slot2_client, length(slot2_client) - length(' ' || to_char(slot2_from, 'DD.MM') || coalesce(' ' || slot2_months || ' мес', '')))
            WHERE sheet_row IS NOT NULL AND slot2_from IS NOT NULL AND slot2_client LIKE '%' || (' ' || to_char(slot2_from, 'DD.MM') || coalesce(' ' || slot2_months || ' мес', ''));
        UPDATE sales.accounts SET slot3_client = left(slot3_client, length(slot3_client) - length(' ' || to_char(slot3_from, 'DD.MM') || coalesce(' ' || slot3_months || ' мес', '')))
            WHERE sheet_row IS NOT NULL AND slot3_from IS NOT NULL AND slot3_client LIKE '%' || (' ' || to_char(slot3_from, 'DD.MM') || coalesce(' ' || slot3_months || ' мес', ''));
        INSERT INTO sales.settings (key, value, updated_by) VALUES ('slots_split_2010', 'done', 'schema');
    END IF;
END $do$;
"""

# Кому какая учётка выдана — один реестр (порядок полей от заказчика, 08.10: имя, последний чат, логин, пароль,
# резервная почта, срок, когда подключили, остальное). source: склад — выдал бот со склада; вручную — заказ бота,
# учётку дал Макс (разбор его сообщения); история — покупки по шаблонам Макса (до бота и мимо бота).
# Консоль из истории — часто без логина: клиенту давали код, а не пароль. Зависит от gateway, crm.purchases
# (их создают другие сервисы) — создаётся, когда они уже есть; добавлен и в SCHEMA разбора покупок.
ISSUED_VIEW = """
DO $do$ BEGIN
IF to_regclass('gateway.messages') IS NOT NULL AND to_regclass('crm.customers') IS NOT NULL AND EXISTS (
   SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('crm.purchases') AND attname = 'recovery_email'
   AND NOT attisdropped) AND to_regclass('sales.orders') IS NOT NULL
   AND to_regclass('gateway.ignored') IS NOT NULL THEN
DROP VIEW IF EXISTS sales.issued;
CREATE VIEW sales.issued AS
WITH x AS (
    SELECT o.client_id, o.chat_id, coalesce(a.login, a.email) AS login, a.password, a.recovery_email, o.expires_at,
           coalesce(o.connected_at, o.paid_at) AS connected_at, 'склад' AS source, o.product, o.months,
           o.issued_months, o.slot, o.amount, o.discount, o.bank, o.id AS order_id, NULL::bigint AS purchase_id,
           a.id AS stock_id, a.status AS account_status, a.expires_at AS account_expires_at
    FROM sales.orders o JOIN sales.accounts a ON a.id = o.stock_id WHERE o.status = 'paid'
    UNION ALL
    SELECT p.client_id, p.chat_id, p.login, p.password, p.recovery_email, p.expires_at, p.paid_at,
           CASE p.source WHEN 'order' THEN 'вручную' ELSE 'история' END, p.product, p.months, NULL, NULL,
           o.amount, o.discount, o.bank, o.id, p.id, NULL, NULL, NULL
    FROM crm.purchases p
    LEFT JOIN sales.orders o ON p.source = 'order' AND o.chat_id = p.chat_id AND o.status = 'paid'
                            AND o.paid_at = p.paid_at
    WHERE coalesce(p.product, '?') NOT IN ('none', 'other') AND o.stock_id IS NULL   -- со склада — выше
      AND NOT EXISTS (SELECT 1 FROM gateway.ignored i WHERE i.client_id = p.client_id)
), last_chat AS (
    SELECT DISTINCT ON (g.client_id) g.client_id, g.id
    FROM gateway.chats g JOIN LATERAL (SELECT max(created_at) AS at FROM gateway.messages m WHERE m.chat_id = g.id) l
         ON true
    WHERE g.client_id IS NOT NULL ORDER BY g.client_id, l.at DESC NULLS LAST
)
SELECT coalesce(cu.name, ch.client_name) AS client_name,
       'https://www.avito.ru/profile/messenger/channel/' || coalesce(lc.id, x.chat_id) AS last_chat_url,
       x.login, x.password, x.recovery_email, x.expires_at, x.connected_at,
       x.source, x.product, x.months, x.issued_months, x.slot, x.amount, x.discount, x.bank, x.order_id,
       x.purchase_id, x.stock_id, x.account_status, x.account_expires_at, x.client_id, x.chat_id AS purchase_chat_id
FROM x
LEFT JOIN crm.customers cu ON cu.avito_user_id = x.client_id
LEFT JOIN gateway.chats ch ON ch.id = x.chat_id
LEFT JOIN last_chat lc ON lc.client_id = x.client_id;
END IF;
END $do$;
"""
SCHEMA += ISSUED_VIEW

# Таблица для владельца (NocoDB, только на сервере): роль sheet создаёт scripts/sheet_setup.sh. Видит и правит только
# склад учёток; реестр sales.issued из таблицы убран (15.10: вид, править нечего).
SHEET_GRANTS = """
DO $do$ BEGIN
IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sheet') THEN
    GRANT USAGE ON SCHEMA sales TO sheet;
    -- с 20.10 без ограничений по колонкам: расчётные (срок, «Свободно», status) триггер всё равно перезапишет
    GRANT SELECT, INSERT, UPDATE, DELETE ON sales.accounts TO sheet;
    GRANT USAGE ON SEQUENCE sales.accounts_id_seq TO sheet;
    IF to_regclass('sales.issued') IS NOT NULL THEN
        REVOKE SELECT ON sales.issued FROM sheet;
    END IF;
END IF;
END $do$;
"""
SCHEMA += "" if NS else SHEET_GRANTS

ROUNDS = int(env("PAY_ROUNDS", "4"))   # кругов по банкам в сутки; дальше — предупреждение в VK
# Автовыдача аккаунта со склада после оплаты; выключена — после оплаты владелец подключает клиента сам.
# Переключается в VK «🤖 Автовыдача» (sales.settings auto_issue), пока не нажимали — SALES_AUTO_ISSUE.
AUTO_ISSUE = env("SALES_AUTO_ISSUE") == "1"
# Реквизиты не заданы (PAY_PHONE / PAY_RECIPIENT) — заказ не оформляем, оплату ведёт владелец (алерт в VK)
PAY_READY = bool(env("PAY_PHONE") and env("PAY_RECIPIENT"))
OPEN = ("awaiting", "checking")

# Оплата на ИП — СБП через шлюз Альфа-Банка (shared/alfa.py). Способ оплаты выбирает владелец в VK
# (sales.settings pay_mode): mix — по правилу pay_method, ip — всё на ИП, card — всё на карты (по умолчанию).
# Тестовые клиенты/чаты — всегда ИП на ALFA_TEST_AMOUNT ₽
ALFA_READY = bool(env("ALFA_USER") and env("ALFA_PASSWORD"))
ALFA_TEST_CLIENTS = {int(x) for x in env_list("ALFA_TEST_CLIENTS") if x.isdigit()}
ALFA_TEST_CHATS = set(env_list("ALFA_TEST_CHATS"))   # то же по чату Avito (u2i-…)
ALFA_TEST_AMOUNT = int(env("ALFA_TEST_AMOUNT") or 10)
IP_MONTHS = 6                    # до 6 мес — на ИП, от 8 мес — на карты (решение 09.10; 7 мес в прайсе нет)
IP_BANK = "СБП (ИП)"             # в sales.orders.bank для заказов на ИП
IP_TTL = timedelta(hours=24)     # жизнь заказа и ссылки в шлюзе


PAY_MODES = {
    "mix": ("🔀 Микс", f"сроки до {IP_MONTHS} мес — по ссылке СБП на ИП (бот сам видит оплату и выдаёт учётку, "
                      f"хоть ночью), от 8 мес — на карты по кругу банков; после {ROUNDS} кругов по картам за день — "
                      "тоже на ИП"),
    "ip": ("🏦 Только ИП", "все сроки — по ссылке СБП на ИП, бот сам видит оплату и выдаёт учётку, хоть ночью"),
    "card": ("💳 Только карты", "все оплаты — переводом на карту по кругу банков, поступление подтверждает "
                               "владелец кнопкой «✅ Оплата пришла»"),
}


def ip_mode(client_id: int | None, chat_id: str, mode: str) -> str | None:
    """Как принимать оплату на ИП: live — по правилу (микс), only — всё на ИП, test — тестовый клиент/чат
    (всегда ИП, тестовая сумма), None — только карты (или не задан логин Альфы)."""
    if not ALFA_READY:
        return None
    if client_id in ALFA_TEST_CLIENTS or chat_id in ALFA_TEST_CHATS:
        return "test"
    return {"mix": "live", "ip": "only"}.get(mode)


async def pay_mode(db) -> str:
    mode = await db.fetchval("SELECT value FROM sales.settings WHERE key='pay_mode'")
    return mode if mode in PAY_MODES else "card"


async def set_pay_mode(db, mode: str, who: str) -> None:
    await db.execute("INSERT INTO sales.settings (key, value, updated_by) VALUES ('pay_mode', $1, $2) "
                     "ON CONFLICT (key) DO UPDATE SET value=$1, updated_by=$2, updated_at=now()", mode, who)


ISSUE_MODES = {   # 23.10: creds — консоли тоже логин и пароль, self — вход по коду делает владелец, off — выдаёт он
    "creds": ("✅ ВКЛ — логин и пароль", "бот сам выдаёт учётку из таблицы и отправляет клиенту почту, пароль, "
                                       "резервную почту, шаги входа и инструкцию его места; Вам ничего не приходит "
                                       "(сколько выдано — в утренней сводке), только если что-то не так"),
    "self": ("✅ ВКЛ — подключаете сами", "бот сам выдаёт учётку из таблицы, клиенту — шаги для кода; когда он "
                                         "пришлёт код, Вы входите этой учёткой и жмёте «📨 Отправить инструкцию»"),
    "off": ("⛔ ВЫКЛ", "бот пишет клиенту «оплату получили», учётку выдаёте Вы (или «🔑 Выдать учётку»)"),
}


async def auto_issue(db) -> str:
    """Режим автовыдачи: creds | self | off (ISSUE_MODES); «1»/«0» — записи до 23.10."""
    mode = await db.fetchval("SELECT value FROM sales.settings WHERE key='auto_issue'")
    mode = {"1": "self", "0": "off"}.get(mode, mode)
    return mode if mode in ISSUE_MODES else "self" if AUTO_ISSUE else "off"


async def set_auto_issue(db, mode: str, who: str) -> None:
    await db.execute("INSERT INTO sales.settings (key, value, updated_by) VALUES ('auto_issue', $1, $2) "
                     "ON CONFLICT (key) DO UPDATE SET value=$1, updated_by=$2, updated_at=now()", mode, who)


def auto_issue_text(mode: str) -> str:
    lines = [f"🤖 Автовыдача сейчас: {ISSUE_MODES[mode][0]}", "", "После оплаты (кнопка «✅ Оплата пришла» или оплата "
             "по ссылке СБП на ИП):"]
    lines += [f"{'▶️' if m == mode else '▫️'} {name} — {about}." for m, (name, about) in ISSUE_MODES.items()]
    lines.append("\nНа ПК клиент всегда получает почту и пароль. «🔑 Выдать учётку» в режиме «логин и пароль» тоже "
                 "отправляет консольному клиенту почту и пароль, в остальных — шаги для кода и инструкцию места.")
    return "\n".join(lines)


def pay_mode_text(mode: str) -> str:
    lines = [f"💳 Способ оплаты сейчас: {PAY_MODES[mode][0]}", ""]
    lines += [f"{'✅' if m == mode else '▫️'} {name} — {about}." for m, (name, about) in PAY_MODES.items()]
    if not ALFA_READY:
        lines.append("\n⚠️ Логин и пароль Альфа-Банка не заданы — оплата на ИП не работает, только карты.")
    lines.append("\nУже выданные ссылки СБП бот проверяет при любом режиме.")
    return "\n".join(lines)


def pay_method(months: int, cards_today: int, limit: int, ip: str | None) -> str:
    """live (микс): ip — сроки до 6 мес; от 8 мес — card, пока за сутки не прошли ROUNDS кругов по банкам, дальше
    тоже ip. only/test — всегда ip, None — всегда card."""
    if ip in ("only", "test") or (ip == "live" and (months <= IP_MONTHS or cards_today >= limit)):
        return "ip"
    return "card"


def next_bank(banks: list[str], last: str | None) -> str:
    """Следующий банк после последнего выданного; после конца списка — снова с начала."""
    if last not in banks:
        return banks[0]
    return banks[(banks.index(last) + 1) % len(banks)]


DISCOUNT = 100   # скидка за отзыв: новым клиентам, доп. аккаунт и ПК (не личный аккаунт)


def valid_amount(price: int | None, amount: int, new: bool) -> bool:
    """Цена из прайса; новому клиенту — минус до 100 ₽ за отзыв."""
    return price is not None and (amount == price or (new and price - DISCOUNT <= amount < price))


def order_amount(price: int, product: str, new: bool) -> int:
    """Сумма счёта: новому клиенту скидка за отзыв сразу (решение 07.10), не только по просьбе."""
    return price - DISCOUNT if new and product not in NO_DISCOUNT else price


def requisites(bank: str, amount: int, discounted: bool = False) -> str:
    return (f"Номер: {env('PAY_PHONE')}\nПолучатель: {env('PAY_RECIPIENT')}\n"
            f"Сумма: {amount} ₽{' (со скидкой за отзыв)' if discounted else ''}\nБанк: {bank}")


def pay_text(order) -> str:
    """Как оплатить заказ: на ИП — ссылка СБП (текстом, для копирования), на карту — реквизиты."""
    if order["method"] != "ip":
        return requisites(order["bank"], order["amount"], order["discount"] > 0)
    return (f"Оплата по СБП — откройте ссылку на телефоне и выберите свой банк:\n{order['pay_url']}\n"
            f"Сумма: {order['amount']} ₽{' (со скидкой за отзыв)' if order['discount'] > 0 else ''}")


PAY_AFTER = {"card": "Как оплатите — пришлите, пожалуйста, скрин 🙏",
             "ip": "Оплата проверится автоматически — доступ пришлю сразу после неё 🙏"}
PAY_CHECKING = "Спасибо! Проверяю поступление, минуту 🙏"
PAY_RECHECK = "Проверяю ещё раз — как только оплата поступит, сразу напишу 🙏"   # «я же оплатил!» — не тишина


# Просьба об отзыве — в конце инструкции после подключения; за скидку напоминаем, размер не называем (07.10)
REVIEW_HOW = "Отзыв можно оставить в нашем профиле → «Отзывы» → «Оставить отзыв»."
REVIEW_ASK = f"Если всё получилось — будем очень благодарны за отзыв 🙏 {REVIEW_HOW}"
REVIEW_ASK_DISCOUNT = f"Если всё получилось — оставьте, пожалуйста, отзыв 🙏 Скидку мы сделали как раз за него. {REVIEW_HOW}"


def review_ask(discounted: bool) -> str:
    return REVIEW_ASK_DISCOUNT if discounted else REVIEW_ASK


PAY_NOT_FOUND = ("Пока не вижу Вашего перевода 🙏 Проверьте, пожалуйста, реквизиты и прошла ли оплата:\n\n{req}\n\n"
                 "Если всё верно — пришлите, пожалуйста, скрин или чек, ещё раз проверю.")
PAID = "Оплату получили, спасибо! 🙏"
# Шаблон Макса после оплаты (консоль): клиент присылает код, Макс входит выданным аккаунтом
CONSOLE_STEPS = ("1) Нажмите кнопку Xbox на джойстике и ведите вправо до конца\n"
                 "2) Нажмите: Добавить или сменить → Добавить новое\n"
                 "3) Нажмите «Использовать другое устройство» — там будет код, напишите его в чат и ожидайте")
SLOTS = {"console": 2}   # клиентов на одну учётку (остальные типы — по одному); «ПК» на консольной — вне счёта
TZ = timezone(timedelta(hours=int(env("OWNER_UTC_OFFSET", "3"))))   # дата посадки на место — по Москве
CHAT_URL = "https://www.avito.ru/profile/messenger/channel/"
# Инструкции заказчика после входа владельца по коду: первому клиенту на учётке (делает её домашней) и второму
INSTR_FIRST = """далее
пишите любой тег и жмете далее
нет спасибо
далее
далее
Без ограничений
пропустить
сделать домашней
далее
далее
у меня нет
4. Выходим из моего аккаунта
 • Нажмите кнопку Xbox → Профиль и система → Выйти.
 • ❗ Не удаляйте аккаунт.

5. Входим в свой аккаунт
 • Подписка и онлайн уже активны.
 • Проверьте наличие игр в полной библиотеке!"""
INSTR_SECOND = """далее
спасибо не надо
без ограничений
пропустить
не изменять
у меня нет
3️⃣ Переключаемся на свой аккаунт
 • Сразу после входа нажмите кнопку Xbox → Профиль и система → выберите свой аккаунт.
 • Проверьте наличие игр в Полной библиотеке 🎮

4️⃣ Использование
 • При каждом включении консоли сначала заходите в мой аккаунт.
 • Сразу переключайтесь на свой аккаунт и играйте."""


def instruction(slot: int | None, discounted: bool = False) -> str:
    return (INSTR_SECOND if slot and slot > 1 else INSTR_FIRST) + "\n\n" + review_ask(discounted)


NAMES = {"console": "консоль", "pc": "ПК", "personal": "личный аккаунт"}
NO_STOCK = {"personal"}   # подписку на аккаунт клиента подключает владелец сам, склад не нужен
NO_DISCOUNT = {"personal"}   # скидка за отзыв — только доп. аккаунт и ПК, на личный аккаунт её нет
PERSONAL_PAID = f"{PAID} Сейчас подключим подписку на Ваш аккаунт — напишем Вам здесь."


def issue_text(product: str, stock: dict | None, discounted: bool = False, creds: bool = False,
               slot: int | None = None) -> str:
    """Сообщение клиенту после подтверждения оплаты. ПК — данные и инструкция сразу, в конце просьба об отзыве.
    creds — консоли тоже данные учётки: шаги входа по паролю и сразу инструкция его места (23.10)."""
    if product in NO_STOCK:
        return PERSONAL_PAID
    if not stock:
        return f"{PAID} Сейчас подготовлю доступ, пару минут."
    if product == "console" and not creds:
        return f"{PAID}\n\n{CONSOLE_STEPS}"
    data = [f"Почта: {stock['email']}" if stock["email"] else None,
            f"Логин: {stock['login']}" if stock["login"] and stock["login"] != stock["email"] else None,
            f"Пароль: {stock['password']}",
            f"Резервная почта: {stock['recovery_email']}" if stock.get("recovery_email") else None]
    data = f"{PAID}\n\n" + "\n".join(x for x in data if x) + "\n\n"
    if product == "console":
        return data + CONSOLE_PASS_STEPS + "\n\n" + instruction(slot, discounted)
    return data + PC_STEPS + "\n\n" + review_ask(discounted)


# Консоль, режим «логин и пароль» (23.10): клиент входит сам, дальше — инструкция его места (INSTR_FIRST/SECOND)
CONSOLE_PASS_STEPS = """Как войти:
1) Нажмите кнопку Xbox на джойстике и ведите вправо до конца
2) Нажмите: Добавить или сменить → Добавить новое
3) Введите почту, затем нажмите «Использовать пароль» и введите пароль
4) Если попросит подтвердить почту — введите резервную почту (она выше)
5) Если придёт запрос кода безопасности — напишите сюда, пришлём код

После входа:"""


# Аренда учётки на ПК — как войти (текст согласован с заказчиком 07.10)
PC_STEPS = """Как войти:
1) Сначала войдите в Microsoft Store с этой почтой.
2) После ввода почты Microsoft предложит подтвердить вход по почте — ниже будет кнопка «Использовать пароль». Нажмите её и введите пароль.
3) Если попросит подтвердить почту — введите резервную почту (она выше).
4) Если придёт запрос кода безопасности — напишите сюда, пришлём код.
5) Затем войдите в приложение Xbox тем же аккаунтом."""


async def last_paid(db, chat_id: str, days: int = 7):
    """Свежий оплаченный заказ чата с выданной учёткой — чтобы бот понимал, что клиент уже на этапе входа."""
    return await db.fetchrow(
        "SELECT o.*, a.email, a.password, a.recovery_email, a.months AS acc_months FROM sales.orders o "
        "LEFT JOIN sales.accounts a ON a.id = o.stock_id WHERE o.chat_id=$1 AND o.status='paid' "
        "AND o.paid_at > now() - make_interval(days => $2) ORDER BY o.id DESC LIMIT 1", chat_id, days)


def add_months(at: datetime, months: int) -> datetime:
    m = at.month - 1 + months
    y, m = at.year + m // 12, m % 12 + 1
    for day in range(at.day, 27, -1):  # 31 января + 1 мес → 28/29 февраля
        try:
            return at.replace(year=y, month=m, day=day)
        except ValueError:
            continue
    return at.replace(year=y, month=m)   # день ≤ 27 есть в любом месяце


def day_start(now: datetime, tz) -> datetime:
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


async def gifts_for(db, product: str, months: int) -> int:
    """Сколько игр в подарок к этому сроку (🎁 в прайсе)."""
    return await db.fetchval("SELECT gifts FROM catalog.prices WHERE product=$1 AND months=$2", product, months) or 0


async def open_order(db, chat_id: str):
    return await db.fetchrow("SELECT * FROM sales.orders WHERE chat_id=$1 AND status = ANY($2::text[]) "
                             "ORDER BY id DESC LIMIT 1", chat_id, list(OPEN))


async def paid_by_hand(db, order) -> bool:
    """Макс принял оплату и подключил клиента сам в Avito, не нажав «✅ Оплата пришла»: после заказа в чате есть
    его шаблоны подключения — заказ закрываем как оплаченный вручную (09.10: картинка клиента через 3 ч после
    подключения ушла Максу как «скрин оплаты»). Покупку в crm.purchases запишет nightly по тем же шаблонам."""
    rows = await db.fetch("SELECT text, created_at FROM gateway.messages WHERE chat_id=$1 AND origin='owner' "
                          "AND created_at > $2 ORDER BY created_at", order["chat_id"], order["created_at"])
    at = next((r["created_at"] for r in rows if crm.is_purchase_msg(r["text"])), None)
    if not at:
        return False
    await db.execute("UPDATE sales.orders SET status='paid', paid_at=$2, confirmed_by='avito', expires_at=$3 "
                     "WHERE id=$1 AND status = ANY($4::text[])", order["id"], at, add_months(at, order["months"]),
                     list(OPEN))
    return True


async def create_order(db: asyncpg.Pool, chat_id: str, account_id: int, client_id: int | None,
                       product: str, months: int, amount: int, tz, discount: int = 0, ip: str | None = None
                       ) -> tuple[asyncpg.Record, int, int]:
    """Новый заказ. Куда платить — pay_method (ip — режим ip_mode, None — только карты). На карту банк — тот же,
    что за сутки уже давали в этом чате без оплаты (клиент передумал со сроком, отказался от скидки — реквизиты
    не меняются), иначе следующий по кругу. Возвращает (заказ, номер оплаты на карты за сутки, лимит в сутки)."""
    async with db.acquire() as con, con.transaction():
        await con.execute("SELECT pg_advisory_xact_lock(hashtext('sales.bank'))")   # круг банков без гонок
        same = await con.fetchval("SELECT bank FROM sales.orders WHERE chat_id=$1 AND status IN ('awaiting','cancelled') "
                                  "AND method='card' AND created_at > now() - interval '1 day' ORDER BY id DESC LIMIT 1",
                                  chat_id)
        await con.execute("UPDATE sales.orders SET status='cancelled' WHERE chat_id=$1 AND status='awaiting'",
                          chat_id)
        banks = [r["name"] for r in await con.fetch("SELECT name FROM sales.banks WHERE active ORDER BY sort")]
        last = await con.fetchval("SELECT bank FROM sales.orders WHERE method='card' ORDER BY id DESC LIMIT 1")
        cards = await con.fetchval("SELECT count(*) FROM sales.orders WHERE method='card' AND created_at >= $1",
                                   day_start(datetime.now(timezone.utc), tz))
        limit = ROUNDS * len(banks)
        method = pay_method(months, cards, limit, ip)
        order = await con.fetchrow(
            "INSERT INTO sales.orders (chat_id, account_id, client_id, product, months, amount, bank, discount, "
            "method, test) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *",
            chat_id, account_id, client_id, product, months, amount,
            IP_BANK if method == "ip" else same if same in banks else next_bank(banks, last), discount, method,
            ip == "test")
    return order, cards + (method == "card"), limit


async def set_pay(db, order_id: int, pay_id: str, url: str) -> asyncpg.Record:
    return await db.fetchrow("UPDATE sales.orders SET pay_id=$2, pay_url=$3 WHERE id=$1 RETURNING *",
                             order_id, pay_id, url)


async def claim(db, order_id: int) -> None:
    await db.execute("UPDATE sales.orders SET status='checking', claimed_at=now() WHERE id=$1", order_id)


async def reject(db, order_id: int) -> None:
    await db.execute("UPDATE sales.orders SET status='awaiting' WHERE id=$1", order_id)


# Учётка под заказ: срок — заказанный или ближайший меньший (нет 6 — 4, нет 4 — 2); место ($3) — как в прошлый раз
# (клиент привык к своей инструкции), нет такого — любое; новым — случайное свободное (09.10);
# $4 — только пустая (клиент спрашивал «каждый раз входить?», ему ответили «нет»); строку из таблицы владельца, где
# ещё нет почты или пароля (заполняет по ячейке), не выдаём. Занятость мест — колонки slot1/slot2 (status считает
# триггер), место «ПК» (slot3) бот не трогает
PICK = """SELECT * FROM sales.accounts a
WHERE a.product = $1 AND a.status = 'free' AND a.months <= $2 AND (a.expires_at IS NULL OR a.expires_at > now())
  AND coalesce(a.email, '') <> '' AND coalesce(a.password, '') <> ''
  AND (NOT $4 OR a.slot1_client IS NULL AND a.slot2_client IS NULL)
ORDER BY a.months DESC, CASE $3::int WHEN 1 THEN a.slot1_client IS NULL WHEN 2 THEN a.slot2_client IS NULL
                                     ELSE true END DESC, random() LIMIT 1"""
WRITE_OFF = "UPDATE sales.accounts SET status='off' WHERE status <> 'off' AND expires_at < now()"


async def wants_empty(con, chat_id: str) -> bool:
    """Клиент спрашивал, нужно ли каждый раз входить, — бот ответил «нет» (dialog.chats.want_empty)."""
    return bool(await con.fetchval("SELECT want_empty FROM dialog.chats WHERE chat_id=$1", chat_id))


async def prev_slot(con, client_id: int | None) -> int | None:
    """Каким клиент был на учётке в прошлый раз: по заказам бота, иначе по инструкции в переписке
    («сделать домашней» — первый, «не изменять» — второй). Не подключался — None."""
    if not client_id:
        return None
    return await con.fetchval(
        "SELECT slot FROM sales.orders WHERE client_id=$1 AND status='paid' AND slot IS NOT NULL "
        "ORDER BY paid_at DESC LIMIT 1", client_id) or await con.fetchval(
        "SELECT CASE WHEN m.text ILIKE '%сделать домашней%' THEN 1 ELSE 2 END FROM gateway.messages m "
        "JOIN gateway.chats g ON g.id = m.chat_id WHERE g.client_id=$1 AND m.origin IN ('owner','bot') "
        "AND (m.text ILIKE '%сделать домашней%' OR m.text ILIKE '%не изменять%') ORDER BY m.created_at DESC LIMIT 1",
        client_id)


def free_slot(stock, want: int | None) -> int:
    """Куда сажать клиента на выбранной учётке: желаемое место ($3 в PICK), если пустое, иначе первое пустое."""
    free = [k for k in range(1, SLOTS.get(stock["product"], 1) + 1) if not stock[f"slot{k}_client"]]
    return want if want in free else free[0]


async def confirm(db: asyncpg.Pool, order_id: int, who: str, issue: bool = False
                  ) -> tuple[asyncpg.Record, asyncpg.Record | None, bool]:
    """Оплата подтверждена: фиксируем срок, выдаём место на учётке. Возвращает (заказ, учётка|None, впервые ли);
    у заказа slot — какой по счёту клиент на учётке, issued_months — срок выданной учётки.
    Тестовый заказ — без склада и CRM. issue — выдавать ли учётку (автовыдача включена или VK «🔑 Выдать учётку»)."""
    now = datetime.now(timezone.utc)
    async with db.acquire() as con, con.transaction():
        order = await con.fetchrow("SELECT * FROM sales.orders WHERE id=$1 FOR UPDATE", order_id)
        if order["status"] == "paid":   # повторное нажатие кнопки / шлюз и владелец одновременно
            stock = await con.fetchrow("SELECT * FROM sales.accounts WHERE id=$1", order["stock_id"])
            return order, stock, False
        stock, slot = None, None
        if issue and not order["test"] and order["product"] not in NO_STOCK:
            await con.execute("SELECT pg_advisory_xact_lock(hashtext('sales.stock'))")   # два подтверждения сразу
            await con.execute(WRITE_OFF)
            solo = order["product"] == "console" and await wants_empty(con, order["chat_id"])
            want = 1 if solo else await prev_slot(con, order["client_id"]) if order["product"] == "console" else None
            stock = await con.fetchrow(PICK, order["product"], order["months"], want, solo)
            if stock:
                slot = free_slot(stock, want)
                name = await con.fetchval(
                    "SELECT coalesce(cu.name, ch.client_name) FROM gateway.chats ch "
                    "LEFT JOIN crm.customers cu ON cu.avito_user_id = ch.client_id WHERE ch.id=$1", order["chat_id"])
                stock = await con.fetchrow(   # клиент — на место в таблице владельца (status пересчитает триггер)
                    f"UPDATE sales.accounts SET slot{slot}_client=$2, slot{slot}_from=$3, slot{slot}_months=$4, "
                    f"slot{slot}_chat=$5, issued_at=now() WHERE id=$1 RETURNING *", stock["id"],
                    name or f"клиент {order['client_id'] or order['chat_id']}", now.astimezone(TZ).date(),
                    order["months"], CHAT_URL + order["chat_id"])
        order = await con.fetchrow(
            "UPDATE sales.orders SET status='paid', paid_at=$2, confirmed_by=$3, stock_id=$4, expires_at=$5, "
            "slot=$6, issued_months=$7 WHERE id=$1 RETURNING *", order_id, now, who, stock and stock["id"],
            add_months(now, order["months"]), slot, stock and stock["months"])
        if order["client_id"] and not order["test"]:   # покупка для напоминания о продлении (broadcast/purchases.py)
            await con.execute(
                "INSERT INTO crm.purchases (client_id, chat_id, paid_at, product, months, expires_at, source, "
                "checked_at) VALUES ($1,$2,$3,$4,$5,$6,'order',$3) ON CONFLICT DO NOTHING",
                order["client_id"], order["chat_id"], now, order["product"], order["months"], order["expires_at"])
    if order["client_id"] and not order["test"]:
        await crm.mark_purchased(db, order["client_id"], now, "order")
    return order, stock, True


async def settle(db: asyncpg.Pool, order_id: int, who: str, wait_text: str | None = None, manual: bool = False
                 ) -> tuple[asyncpg.Record, asyncpg.Record | None, list[str] | None]:
    """Оплата пришла (владелец нажал «✅ Оплата пришла» или шлюз ИП увидел оплату): выдача учётки, сообщения клиенту,
    отчёт владельцу строками. wait_text — клиенту, когда учётку выдаёт человек (по умолчанию «пару минут»).
    manual — VK «🔑 Выдать учётку»: выдаём и при выключенной автовыдаче, консольному клиенту сразу и инструкцию его
    места (владелец подключает сам, кнопки «📨 Отправить инструкцию» не будет — решение 20.10).
    Уже подтверждён раньше — (заказ, учётка, None): ничего не отправляем повторно."""
    before = await db.fetchrow("SELECT * FROM sales.orders WHERE id=$1", order_id)
    gift = (not before["test"] and await crm.is_new(db, before["client_id"]) and   # подарок — только первая покупка
            await gifts_for(db, before["product"], before["months"]))
    mode = await auto_issue(db)
    auto, creds = manual or mode != "off", mode == "creds"
    order, stock, fresh = await confirm(db, order_id, who, issue=auto)
    if not fresh:
        return order, stock, None

    async def send(text: str) -> None:
        await db.execute("INSERT INTO gateway.outbox (account_id, chat_id, text) VALUES ($1,$2,$3)",
                         order["account_id"], order["chat_id"], text)

    wait = wait_text or "Сейчас подготовлю доступ, пару минут."
    issued = issue_text(order["product"], stock and dict(stock), order["discount"] > 0, creds, order["slot"])
    await send(f"{PAID} {wait}" if not stock and order["product"] not in NO_STOCK else issued)
    if stock and manual and order["product"] == "console" and not creds:
        await send(instruction(order["slot"], order["discount"] > 0))
    if stock and (manual or creds or order["product"] != "console"):   # данные ушли клиенту; консоль — по коду
        order = await db.fetchrow("UPDATE sales.orders SET connected_at=now() WHERE id=$1 RETURNING *", order["id"])
    if gift:   # список игр на выбор; что выбрал клиент — владелец увидит в чате и поправит
        gifts = await catalog.template(db, "gift_list")
        head = (gifts or "").strip().split("\n")[0]
        # список уже был в переписке за 2 ч (клиент спрашивал до оплаты) — второй раз не шлём (стенд 23.10)
        shown = head and await db.fetchval(
            "SELECT 1 FROM gateway.outbox WHERE chat_id=$1 AND status IN ('sent','pending') "
            "AND created_at > now() - interval '2 hours' AND position($2 in text) > 0 LIMIT 1", order["chat_id"], head)
        if gifts and not shown:
            await send(gifts)
    lines = [f"✅ Оплата по заказу №{order['id']} подтверждена, подписка до {order['expires_at']:%d.%m.%Y}."]
    if order["test"]:
        lines.append("🧪 Тестовый заказ: склад и база клиентов не тронуты.")
    if order["product"] in NO_STOCK:
        lines.append("👤 Подключите подписку на личный аккаунт клиента — данные аккаунта запросите в чате. "
                     "Клиенту написал «сейчас подключим, напишем здесь».")
    elif not auto or order["test"]:
        lines.append(f"👤 Подключите клиента сами (автовыдача со склада выключена). Клиенту написал «{wait}».")
    elif not stock:
        solo = order["product"] == "console" and await wants_empty(db, order["chat_id"])
        lines.append(f"⚠️ Нет {'пустой' if solo else 'свободной'} учётки: "
                     f"{NAMES.get(order['product'], order['product'])} {order['months']} мес и меньше. "
                     f"Клиенту написал «{wait}» — выдайте вручную."
                     + ("\nКлиент спрашивал, нужно ли каждый раз входить, — ответили «нет»: дайте учётку, "
                        "где он будет первым." if solo else ""))
    else:
        slots = SLOTS.get(order["product"], 1)
        lines.append(f"Выдана учётка №{stock['id']}, {stock['months']} мес"
                     + (f" (до {stock['expires_at']:%d.%m.%Y})" if stock["expires_at"] else "")
                     + (f", место {order['slot']} из {slots}" if slots > 1 else "") + ":\n"
                     + "\n".join(x for x in (stock["email"], stock["password"],
                                             stock["recovery_email"] and f"резервная: {stock['recovery_email']}") if x))
        if order["issued_months"] and order["issued_months"] < order["months"]:
            lines.append(f"⚠️ Учёток на {order['months']} мес нет — выдана на {order['issued_months']} мес "
                         "(отмечено в заказе), остаток срока — за Вами.")
        place = f"{order['slot'] or 1}-го клиента ({'сделать домашней' if (order['slot'] or 1) == 1 else 'не изменять'})"
        lines.append("Клиенту отправлены данные для входа." if order["product"] != "console" else
                     f"Клиенту отправлены почта, пароль, шаги входа и инструкция {place}." if creds else
                     f"Клиенту отправлены шаги для кода и инструкция {place}. Пришлёт код — войдите этой учёткой."
                     if manual else
                     "Клиенту отправлены шаги для кода. Когда пришлёт код — войдите этой учёткой и нажмите "
                     "«📨 Отправить инструкцию».")
    return order, stock, lines


async def manual_issue(db: asyncpg.Pool, chat_id: str, product: str, months: int, who: str
                       ) -> tuple[str | None, list[str]]:
    """VK «🔑 Выдать учётку» (20.10): владелец подключает клиента сам — чат, консоль/ПК, срок. Всё как автовыдача после
    оплаты (учётка по тем же правилам, клиент — в таблицу, клиенту — «оплату получили», шаги и инструкция места,
    владельцу — данные учётки); открытый заказ чата закрывается оплаченным, нет заказа — заводится по прайсу.
    Нет свободной учётки — ничего не меняем и клиенту не пишем. Возвращает (что не так | None, строки отчёта)."""
    chat = await db.fetchrow("SELECT account_id, client_id FROM gateway.chats WHERE id=$1", chat_id)
    if not chat:
        return "Не нашёл такой чат 🤷 Проверьте ссылку.", []
    async with db.acquire() as con:   # есть ли что выдать — до любых изменений
        await con.execute(WRITE_OFF)
        solo = product == "console" and await wants_empty(con, chat_id)
        want = 1 if solo else await prev_slot(con, chat["client_id"]) if product == "console" else None
        if not await con.fetchrow(PICK, product, months, want, solo):
            return (f"⚠️ Нет {'пустой' if solo else 'свободной'} учётки: {NAMES.get(product, product)} {months} мес "
                    "и меньше. Добавьте учётку в таблицу и пришлите запрос ещё раз." +
                    ("\nКлиент спрашивал, нужно ли каждый раз входить, — нужна учётка, где он будет первым."
                     if solo else "")), []
    order = await open_order(db, chat_id)
    price = await db.fetchval("SELECT price_rub FROM catalog.prices WHERE product=$1 AND months=$2", product, months)
    if order:   # срок выдали другой — и сумма по прайсу этого срока (со скидкой, если она была в заказе)
        await db.execute("UPDATE sales.orders SET product=$2, months=$3, amount=coalesce($4 - discount, amount) "
                         "WHERE id=$1", order["id"], product, months, price)
    else:
        order = await db.fetchrow(
            "INSERT INTO sales.orders (chat_id, account_id, client_id, product, months, amount, bank) "
            "VALUES ($1,$2,$3,$4,$5,$6,'вручную') RETURNING *", chat_id, chat["account_id"], chat["client_id"],
            product, months, price or 0)
    order, stock, lines = await settle(db, order["id"], who, manual=True)
    if lines is None:
        return "Этот заказ уже подтвердили — учётка выдана раньше.", []
    return None, [f"🔑 {CHAT_URL}{chat_id}"] + lines


# ---------- реестр учёток (VK «📦 Аккаунты») ----------
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ACCOUNT_FORMAT = "почта пароль резервная_почта срок [дата активации]"


def parse_accounts(text: str, today: datetime) -> tuple[list[dict], list[str]]:
    """Строки «почта пароль резервная_почта 6 [01.10]»; «пк» в строке — учётка для ПК. Без даты — активирована сегодня."""
    rows, errors = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        t = [x for x in re.split(r"[\s;|]+", re.sub(r"(?i)\s*мес\w*", " ", line)) if x]
        product = "pc" if any(x.lower() in ("пк", "pc") for x in t) else "console"
        t = [x for x in t if x.lower() not in ("пк", "pc", "консоль")]
        try:
            email, password, *rest = t
            if not EMAIL.match(email) or EMAIL.match(password):
                raise ValueError
            recovery = rest.pop(0) if rest and EMAIL.match(rest[0]) else None
            months = int(rest.pop(0))
            if not 1 <= months <= 24:
                raise ValueError
            activated = today
            if rest:
                d, m, *y = (int(x) for x in rest.pop(0).split("."))
                activated = today.replace(year=(y[0] if y else today.year), month=m, day=d)
        except (ValueError, IndexError):
            errors.append(f"«{line}» — не разобрал, нужно: {ACCOUNT_FORMAT}")
            continue
        rows.append({"product": product, "email": email, "password": password, "recovery_email": recovery,
                     "months": months, "activated_at": activated, "expires_at": add_months(activated, months)})
    return rows, errors


async def add_accounts(db, rows: list[dict], who: str) -> tuple[int, list[str]]:
    """Добавляет учётки; уже действующие (та же почта) пропускает. Возвращает (добавлено, пропущенные почты)."""
    added, skipped = 0, []
    for r in rows:
        if await db.fetchval("SELECT 1 FROM sales.accounts WHERE lower(email)=lower($1) AND status <> 'off'",
                             r["email"]):
            skipped.append(r["email"])
            continue
        await db.execute("INSERT INTO sales.accounts (product, months, email, login, password, recovery_email, "
                         "activated_at, expires_at, note) VALUES ($1,$2,$3,$3,$4,$5,$6,$7,$8)",
                         r["product"], r["months"], r["email"], r["password"], r["recovery_email"],
                         r["activated_at"], r["expires_at"], f"добавил {who}")
        added += 1
    return added, skipped


async def write_off(db, emails: list[str]) -> int:
    rows = await db.fetch("UPDATE sales.accounts SET status='off' WHERE lower(email) = ANY($1::text[]) "
                          "AND status <> 'off' RETURNING id", [e.lower() for e in emails])
    return len(rows)


async def stock_text(db) -> str:
    """Остатки по срокам и список действующих учёток (кто на них — по заказам)."""
    await db.execute(WRITE_OFF)
    rows = await db.fetch("SELECT a.*, (a.slot1_client IS NOT NULL)::int + (a.product = 'console' AND "
                          "a.slot2_client IS NOT NULL)::int AS used FROM sales.accounts a WHERE a.status <> 'off' "
                          "ORDER BY a.product, a.months, a.expires_at NULLS LAST, a.id")
    if not rows:
        return "📦 Действующих учёток нет. Нажмите «➕ Добавить»."
    by: dict[tuple, list] = {}
    for r in rows:
        by.setdefault((r["product"], r["months"]), []).append(r)
    head = [f"• {NAMES.get(p, p)}, {m} мес: учёток {len(v)}, свободных мест "
            f"{sum(max(SLOTS.get(p, 1) - r['used'], 0) for r in v if r['status'] == 'free')}"
            for (p, m), v in by.items()]
    items = [f"№{r['id']} {r['email']} — {r['months']} мес"
             + (f" до {r['expires_at']:%d.%m.%Y}" if r["expires_at"] else "")
             + f", занято {r['used']}/{SLOTS.get(r['product'], 1)}" for r in rows[:40]]
    more = f"\n…и ещё {len(rows) - 40}" if len(rows) > 40 else ""
    return "📦 Склад учёток\n\n" + "\n".join(head) + "\n\n" + "\n".join(items) + more
