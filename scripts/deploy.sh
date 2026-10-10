#!/bin/sh
# Обновление на VPS: sh scripts/deploy.sh
set -e
cd "$(dirname "$0")/.."
git fetch -q origin main
git reset -q --hard origin/main  # main на GitHub каждый раз перезаписывается
# DEPLOY_SERVICES в .env — какие сервисы поднимать на этом сервере (через пробел); пусто = все
services=$(sed -n 's/^DEPLOY_SERVICES=//p' .env | tr -d '\r"')
docker compose -f docker-compose.prod.yml up -d --build $services
docker compose -f docker-compose.prod.yml ps
