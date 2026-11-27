#!/bin/sh
# ПК → GitHub: в main только текущая версия одним коммитом (история не публикуется, docs/ в git нет)
# SERVERS.md (адреса и входы на серверы) — только в своём приватном репозитории (scripts/own_repo.sh), сюда не идёт
set -e
cd "$(dirname "$0")/.."
idx="$(git rev-parse --git-dir)/publish.index"   # временный индекс: рабочий индекс и файлы не трогаем
trap 'rm -f "$idx"' EXIT
GIT_INDEX_FILE=$idx git read-tree HEAD
GIT_INDEX_FILE=$idx git rm -q --cached --ignore-unmatch SERVERS.md
tree=$(GIT_INDEX_FILE=$idx git write-tree)
sha=$(git commit-tree "$tree" -m "Версия от $(date '+%F %H:%M')")
git push -f origin "$sha:refs/heads/main"
