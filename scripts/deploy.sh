#!/bin/sh
# Обновление на VPS: sh scripts/deploy.sh
set -e
cd "$(dirname "$0")/.."
git fetch -q origin main
git reset -q --hard origin/main  # main на GitHub каждый раз перезаписывается
# DEPLOY_SERVICES в .env — какие сервисы поднимать на этом сервере (через пробел); пусто = все
services=$(sed -n 's/^DEPLOY_SERVICES=//p' .env | tr -d '\r"')
# логи контейнеров пропадают при пересоздании — сохраняем перед выкладкой, храним 30 последних (аудит 20.10)
mkdir -p logs
docker compose -f docker-compose.prod.yml logs --no-color --timestamps > "logs/$(date +%Y%m%d-%H%M%S).log" 2>/dev/null || true
ls -1t logs/*.log 2>/dev/null | tail -n +31 | xargs -r rm -f
docker compose -f docker-compose.prod.yml up -d --build $services
docker compose -f docker-compose.prod.yml ps
