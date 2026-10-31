#!/bin/sh
# ПК → GitHub: в main только текущая версия одним коммитом (история не публикуется, docs/ в git нет)
set -e
cd "$(dirname "$0")/.."
sha=$(git commit-tree HEAD^{tree} -m "Версия от $(date '+%F %H:%M')")
git push -f origin "$sha:refs/heads/main"
