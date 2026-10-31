#!/bin/sh
# Раз в неделю (cron, пн 07:00 UTC = 10:00 МСК, 29.10): последний ночной дамп → 7z с паролем BACKUP_PASSWORD из .env
# → владельцу и админу в VK. Проверка одному: sh scripts/backup_vk.sh --to <VK id>
set -e
cd "$(dirname "$0")/.."
f=$(ls -1t backups/agent_*.dump | head -1)
pass=$(grep '^BACKUP_PASSWORD=' .env | cut -d= -f2-)
[ -n "$pass" ] || { echo "нет BACKUP_PASSWORD в .env"; exit 1; }
name="$(basename "$f" .dump).7z"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# данные зашифрованы, имя файла внутри видно: архив со скрытыми именами VK не принимает («empty_archive», 29.10)
7z a -t7z -mhe=off -p"$pass" "$tmp/$name" "./$f" >/dev/null
docker compose -f docker-compose.prod.yml exec -T owner-bot python scripts/backup_vk.py "$name" "$@" < "$tmp/$name"
