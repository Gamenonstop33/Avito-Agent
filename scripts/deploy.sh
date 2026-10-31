#!/bin/sh
# Обновление на VPS: sh scripts/deploy.sh
set -e
cd "$(dirname "$0")/.."
git fetch -q origin main
# база знаний в git — только копия (30.10): на сервере её правят правила владельца (31.10, kb_rules), reset затёр бы
# правки — после него возвращаем серверную
mkdir -p backups
if [ -d knowledge ]; then rm -rf backups/knowledge-prev; cp -r knowledge backups/knowledge-prev; fi
git reset -q --hard origin/main  # main на GitHub каждый раз перезаписывается
if [ -d backups/knowledge-prev ] && ! diff -rq backups/knowledge-prev knowledge >/dev/null 2>&1; then
  rm -rf knowledge; cp -r backups/knowledge-prev knowledge
  echo "ℹ база знаний на сервере новее GitHub (правки по правилам) — оставлена серверная"
fi
# DEPLOY_SERVICES в .env — какие сервисы поднимать на этом сервере (через пробел); пусто = все
services=$(sed -n 's/^DEPLOY_SERVICES=//p' .env | tr -d '\r"')
# логи контейнеров пропадают при пересоздании — сохраняем перед выкладкой, храним 30 последних (аудит 20.10)
mkdir -p logs
docker compose -f docker-compose.prod.yml logs --no-color --timestamps > "logs/$(date +%Y%m%d-%H%M%S).log" 2>/dev/null || true
ls -1t logs/*.log 2>/dev/null | tail -n +31 | xargs -r rm -f
docker compose -f docker-compose.prod.yml up -d --build $services
docker compose -f docker-compose.prod.yml ps
