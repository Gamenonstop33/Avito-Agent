#!/bin/sh
# Таблица учёток для владельца (NocoDB + Caddy в docker-compose.prod.yml), на РФ-сервере:
#   sh scripts/sheet_setup.sh init   — до первого deploy с nocodb и caddy: пароли в .env (если их ещё нет), роль sheet в БД
#   sh scripts/sheet_setup.sh config — после регистрации администратора и NC_TOKEN в .env: подключение к базе, русские
#                                      названия, скрытые колонки, регистрация только по приглашению (повтор безопасен)
set -e
cd "$(dirname "$0")/.."
dc="docker compose -f docker-compose.prod.yml"
val() { sed -n "s/^$1=//p" .env | tr -d "\r'\""; }
put() {   # KEY='VALUE' в .env, если значения ещё нет
    [ -n "$(val "$1")" ] && return
    sed -i "/^$1=/d" .env
    printf "%s='%s'\n" "$1" "$2" >> .env
}
case "$1" in
init)
    [ -n "$(val SHEET_DOMAIN)" ] || { echo "задайте SHEET_DOMAIN в .env"; exit 1; }
    put NC_JWT_SECRET "$(openssl rand -hex 32)"
    put SHEET_DB_PASSWORD "$(openssl rand -hex 24)"
    put SHEET_BASIC_USER owner
    if [ -z "$(val SHEET_BASIC_PASS)" ]; then
        put SHEET_BASIC_PASS "$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-20)"
        sed -i "/^SHEET_BASIC_HASH=/d" .env
    fi
    $dc pull -q caddy nocodb
    put SHEET_BASIC_HASH "$($dc run --rm --no-deps -T caddy caddy hash-password --plaintext "$(val SHEET_BASIC_PASS)"                            | tail -n 1)"
    $dc exec -T db psql -U agent -q -v ON_ERROR_STOP=1 -v pw="$(val SHEET_DB_PASSWORD)" <<'SQL'
SELECT 'CREATE ROLE sheet' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sheet') \gexec
SELECT format('ALTER ROLE sheet LOGIN PASSWORD %L', :'pw') \gexec
SQL
    echo "готово: роль sheet в БД; вход на сайт — SHEET_BASIC_USER / SHEET_BASIC_PASS из .env";;
config)
    $dc run --rm --no-deps -T -v "$PWD/scripts:/app/scripts:ro" owner-bot python scripts/sheet_setup.py
    # чего NocoDB для внешней базы через API не даёт: названия таблиц и ключевой колонки, «только чтение»,
    # текст в одну строку (а не «длинный текст» — его нельзя сделать главной колонкой); регистрация —
    # только по приглашению (настройка приложения, по токену API её не поменять)
    $dc exec -T nocodb node - <<'JS'
const db = new (require("/usr/src/app/node_modules/sqlite3").Database)("/usr/app/data/noco.db");
const ok = (e) => { if (e) { console.error(e.message); process.exit(1); } };
const models = `SELECT m.id FROM nc_models_v2 m JOIN nc_bases_v2 b ON b.id = m.base_id
                WHERE b.title = 'Учётки' AND coalesce(b.deleted, 0) = 0 AND m.table_name = ?`;
db.serialize(() => {
  db.run(`UPDATE nc_models_v2 SET title = 'Склад учёток' WHERE id IN (${models})`, ["accounts"], ok);
  db.run(`UPDATE nc_columns_v2 SET title = '№' WHERE pk = 1 AND column_name = 'id' AND fk_model_id IN (${models})`,
         ["accounts"], ok);
  db.run(`UPDATE nc_columns_v2 SET readonly = 1 WHERE column_name IN ('expires_at', 'free_slots', 'slot1_until',
          'slot2_until', 'slot3_until') AND fk_model_id IN (${models})`, ["accounts"], ok);   // расчётные (триггер)
  // связь «orders» NocoDB находит по внешнему ключу sales.orders.stock_id, а заказы роли sheet не видны — с ней удаление
  // строк падает («reading 'mm'»); убираем (выданную учётку база удалить всё равно не даст)
  const links = `SELECT id FROM nc_columns_v2 WHERE uidt = 'LinkToAnotherRecord' AND fk_model_id IN (${models})`;
  db.run(`DELETE FROM nc_col_relations_v2 WHERE fk_column_id IN (${links})`, ["accounts"], ok);
  db.run(`DELETE FROM nc_grid_view_columns_v2 WHERE fk_column_id IN (${links})`, ["accounts"], ok);
  db.run(`DELETE FROM nc_columns_v2 WHERE id IN (${links})`, ["accounts"], ok);
  db.run(`UPDATE nc_columns_v2 SET rqd = 0 WHERE coalesce(pk, 0) = 0 AND fk_model_id IN (${models})`, ["accounts"],
         ok);   // обязательных полей нет: пустые заполнит триггер (20.10 — форма требовала скрытое months)
  db.run(`UPDATE nc_columns_v2 SET uidt = 'URL' WHERE column_name IN ('slot1_chat', 'slot2_chat')
          AND fk_model_id IN (${models})`, ["accounts"], ok);   // чат Avito — ссылкой
  db.run(`UPDATE nc_columns_v2 SET uidt = 'SingleLineText' WHERE uidt = 'LongText' AND column_name <> 'note'
          AND fk_model_id IN (${models})`, ["accounts"], ok);
  db.run(`UPDATE nc_columns_v2 SET pv = CASE WHEN column_name = 'email' THEN 1 END WHERE fk_model_id IN (${models})`,
         ["accounts"], ok);
  db.run(`DELETE FROM nc_store WHERE key = 'nc-app-settings'`, ok);
  db.run(`INSERT INTO nc_store (db_alias, key, value, created_at, updated_at) VALUES ('db', 'nc-app-settings', ?,
          datetime('now'), datetime('now'))`, [JSON.stringify({invite_only_signup: true, restrict_workspace_creation: true,
          allow_email_signin_with_sso: false})], (e) => { ok(e); db.close(); });
});
JS
    $dc restart nocodb   # кэш метаданных NocoDB — в памяти
    # после перезапуска часть настроек вида (скрытые колонки, порядок) первого прогона не сохраняется — второй прогон
    until $dc exec -T nocodb node -e "fetch('http://localhost:8080/api/v1/health').then(r => process.exit(r.ok ? 0 : 1),
            () => process.exit(1))"; do sleep 3; done
    $dc run --rm --no-deps -T -v "$PWD/scripts:/app/scripts:ro" owner-bot python scripts/sheet_setup.py
    echo "готово: таблица «Склад учёток», регистрация — только по приглашению";;
*)
    echo "использование: sh scripts/sheet_setup.sh init | config"; exit 1;;
esac
