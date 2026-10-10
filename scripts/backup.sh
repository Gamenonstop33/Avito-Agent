#!/bin/sh
# Ежедневный бэкап БД (cron): хранит 14 последних
set -e
cd "$(dirname "$0")/.."
mkdir -p backups
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U agent -Fc agent > "backups/agent_$(date +%F).dump"
ls -1t backups/agent_*.dump | tail -n +15 | xargs -r rm --
