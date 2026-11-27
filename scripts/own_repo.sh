#!/bin/sh
# Свой приватный репозиторий (25.11): вся история — main, что выложено у Макса и у Димана — ветки max и diman,
# снимки выкладок — теги release-*. Публичный origin (Gamenonstop33) — только снимок для deploy.sh, его не трогаем.
#   sh scripts/own_repo.sh https://github.com/<логин>/avito-agent.git   # первый раз: remote own и заливка
#   sh scripts/own_repo.sh                                               # потом: обновить
set -e
cd "$(dirname "$0")/.."
if [ -n "$1" ]; then
  git remote remove own 2>/dev/null || true
  git remote add own "$1"
fi
git push own HEAD:refs/heads/main
git push -f own max:refs/heads/max diman:refs/heads/diman
git push own "refs/heads/release-*:refs/tags/release-*"
echo "готово: $(git remote get-url own)"
