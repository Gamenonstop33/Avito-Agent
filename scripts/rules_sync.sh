#!/bin/sh
# Правила владельца и база знаний — с одного сервера на другой (19.11: XBOXGARANT берёт у Макса раз в 15 минут).
#   sh scripts/rules_sync.sh export   — на сервере-источнике: tar в stdout (knowledge/kb_*.md, dialog.rules без
#                                        ждущих решения предложений, dialog.kb_edits). Запускается ключом получателя:
#                                        в authorized_keys источника command="sh /root/avito-agent/scripts/rules_sync.sh
#                                        export",restrict,from="<IP получателя>" — этим ключом больше ничего нельзя.
#   sh scripts/rules_sync.sh pull     — на получателе (cron */15): забирает по RULES_SYNC_SSH из .env
#                                        (например root@194.87.76.198), если что-то изменилось — заменяет базу знаний
#                                        и обе таблицы одной транзакцией. У получателя в .env RULES_FROM=<чьи правила>:
#                                        свои правила там не предлагаются и не правятся (shared/config.RULES_FROM).
set -e
cd "$(dirname "$0")/.."
dc="docker compose -f docker-compose.prod.yml"
val() { sed -n "s/^$1=//p" .env | tr -d "\r'\""; }
case "$1" in
export)
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    mkdir "$tmp/knowledge"; cp knowledge/kb_*.md "$tmp/knowledge/"
    $dc exec -T db psql -U agent -q -v ON_ERROR_STOP=1 -c "COPY (SELECT * FROM dialog.rules WHERE active OR \
removed_by IS NOT NULL OR approved_by IS NOT NULL ORDER BY id) TO STDOUT (FORMAT csv, HEADER)" > "$tmp/rules.csv"
    $dc exec -T db psql -U agent -q -v ON_ERROR_STOP=1 -c "COPY (SELECT * FROM dialog.kb_edits ORDER BY id) \
TO STDOUT (FORMAT csv, HEADER)" > "$tmp/kb_edits.csv"
    tar -C "$tmp" -cf - knowledge rules.csv kb_edits.csv;;
pull)
    src=$(val RULES_SYNC_SSH); [ -n "$src" ] || { echo "задайте RULES_SYNC_SSH в .env"; exit 1; }
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    ssh -i /root/.ssh/rules_sync -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new "$src" \
        export > "$tmp/in.tar"
    tar -C "$tmp" -xf "$tmp/in.tar"
    [ -s "$tmp/knowledge/kb_core.md" ] && [ -s "$tmp/knowledge/kb_games.md" ] && [ -s "$tmp/rules.csv" ] || {
        echo "$(date '+%F %T') пустая выгрузка — ничего не меняю"; exit 1; }
    sum=$(cd "$tmp" && cat knowledge/kb_core.md knowledge/kb_games.md rules.csv kb_edits.csv | sha256sum | cut -c1-16)
    [ "$sum" = "$(cat backups/rules_sync.last 2>/dev/null)" ] && exit 0
    cols() { head -1 "$1" | tr -d '\r'; }
    $dc cp "$tmp/rules.csv" db:/tmp/rules.csv; $dc cp "$tmp/kb_edits.csv" db:/tmp/kb_edits.csv
    $dc exec -T db psql -U agent -q -1 -v ON_ERROR_STOP=1 <<SQL
LOCK TABLE dialog.rules, dialog.kb_edits IN EXCLUSIVE MODE;
DELETE FROM dialog.kb_edits;
DELETE FROM dialog.rules;
COPY dialog.rules ($(cols "$tmp/rules.csv")) FROM '/tmp/rules.csv' (FORMAT csv, HEADER);
COPY dialog.kb_edits ($(cols "$tmp/kb_edits.csv")) FROM '/tmp/kb_edits.csv' (FORMAT csv, HEADER);
SELECT setval('dialog.rules_id_seq', coalesce((SELECT max(id) FROM dialog.rules), 1));
SELECT setval(pg_get_serial_sequence('dialog.kb_edits', 'id'), coalesce((SELECT max(id) FROM dialog.kb_edits), 1));
SQL
    $dc exec -T db rm -f /tmp/rules.csv /tmp/kb_edits.csv
    for f in kb_core.md kb_games.md; do   # замена файла целиком: dialog перечитывает базу по дате файла
        cp "$tmp/knowledge/$f" "knowledge/.$f.new" && mv "knowledge/.$f.new" "knowledge/$f"
    done
    echo "$sum" > backups/rules_sync.last
    echo "$(date '+%F %T') обновлено: $($dc exec -T db psql -U agent -tAc "SELECT 'правил ' || count(*) FILTER \
(WHERE active) || ' действующих из ' || count(*) FROM dialog.rules")";;
*)
    echo "использование: sh scripts/rules_sync.sh export | pull"; exit 1;;
esac
