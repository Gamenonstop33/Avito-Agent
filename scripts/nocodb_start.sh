#!/bin/sh
# Запуск NocoDB (таблица учёток): интерфейс по умолчанию на русском (31.10 — владелец не мог переключить язык).
# NocoDB берёт язык из localStorage (nocodb-gui-v2 → lang) и сам всегда ставит en, русский браузер не помогает.
# В страницы вписываем скрипт: до загрузки приложения один раз на браузер ставит ru (метка nc-lang-ru),
# язык, выбранный потом в меню профиля, остаётся. Страницы правятся при каждом старте контейнера (новая версия NocoDB — тоже)
JS='<script>try{var k="nocodb-gui-v2",s=JSON.parse(localStorage.getItem(k)||"{}");if(!localStorage.getItem("nc-lang-ru")){s.lang="ru";localStorage.setItem(k,JSON.stringify(s));localStorage.setItem("nc-lang-ru","1")}}catch(e){}</script>'
find /usr/src/app/docker/nc-gui -name '*.html' | while read -r f; do
  grep -q nc-lang-ru "$f" || sed -i "s#<head>#<head>$JS#" "$f"
done
exec /usr/src/appEntry/start.sh
