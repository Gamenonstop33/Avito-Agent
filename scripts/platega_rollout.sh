#!/bin/sh
# Выкладка оплаты через Platega на сервер Макса одной командой с ноутбука (27.11), после `sh scripts/publish.sh`:
# ключи Platega → .env сервера (с копией .env), deploy.sh, по желанию — режим «⚡ Только Platega» в обоих магазинах.
# Команды на сервере читают < /dev/null — иначе съели бы остаток скрипта из входного потока ssh.
# Ключи спрашивает сам и передаёт по ssh во входном потоке — в истории команд и в списке процессов их нет.
#   sh scripts/platega_rollout.sh
set -e
cd "$(dirname "$0")/.."
JUMP=root@149.33.47.226
HOST=root@194.87.76.198

printf "Platega Merchant ID: "; read -r MID
printf "Platega API-ключ (не отображается): "; stty -echo 2>/dev/null || true; read -r KEY; stty echo 2>/dev/null || true; echo
[ -n "$MID" ] && [ -n "$KEY" ] || { echo "пусто — ничего не меняю"; exit 1; }
printf "Сразу включить «⚡ Только Platega» в GameNonStop и PREMIUM? [Y/n] "; read -r ON
case "$ON" in n|N|н|Н) ON=0 ;; *) ON=1 ;; esac

ssh -J "$JUMP" "$HOST" sh -s <<EOF
set -e
cd /root/avito-agent
cp .env .env.bak-platega-\$(date +%d%m-%H%M)
sed -i '/^PLATEGA_MERCHANT_ID=/d; /^PLATEGA_SECRET=/d' .env
printf 'PLATEGA_MERCHANT_ID=%s\nPLATEGA_SECRET=%s\n' '$MID' '$KEY' >> .env
echo "ключи Platega в .env (копия — .env.bak-platega-*)"
sh scripts/deploy.sh < /dev/null
if [ "$ON" = 1 ]; then
  docker compose -f docker-compose.prod.yml exec -T db psql -U agent -q -v ON_ERROR_STOP=1 -c "
    INSERT INTO sales.settings (key, value, updated_by) VALUES ('pay_mode', 'platega', 'rollout')
      ON CONFLICT (key) DO UPDATE SET value='platega', updated_by='rollout', updated_at=now();
    INSERT INTO sales2.settings (key, value, updated_by) VALUES ('pay_mode', 'platega', 'rollout')
      ON CONFLICT (key) DO UPDATE SET value='platega', updated_by='rollout', updated_at=now();" < /dev/null
  echo "режим «⚡ Только Platega» включён в обоих магазинах (вернуть — VK «💳 Способ оплаты»)"
fi
docker compose -f docker-compose.prod.yml exec -T dialog python -c \
  "from shared import sales; print('Platega в dialog:', 'да' if sales.PLATEGA_READY else 'НЕТ')" < /dev/null
EOF

git branch -f max HEAD && sh scripts/own_repo.sh || echo "свой репозиторий не обновлён — запустите sh scripts/own_repo.sh"
echo "готово"
