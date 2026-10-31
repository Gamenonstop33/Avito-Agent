"""Проверка логина API Альфа-Банка (платёжный шлюз) на тестовом и боевом адресах.

Запуск: python scripts/alfa_check.py
Берёт ALFA_USER / ALFA_PASSWORD из .env, запрашивает статус несуществующего заказа (ничего не создаёт).
Секреты не выводит. Ответ: errorCode 6 — пара рабочая на этом адресе; 5 — доступ запрещён / сменить пароль.
"""
import json
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.config import ROOT, env  # noqa: E402

# Сертификаты шлюза выданы УЦ Минцифры — его корня нет в стандартных хранилищах
SSL = ssl.create_default_context(cafile=str(ROOT / "shared" / "certs" / "russian_trusted_root_ca.pem"))
HOSTS = {"тест": "alfa.rbsuat.com", "бой": "pay.alfabank.ru"}


def main() -> None:
    user, password = env("ALFA_USER"), env("ALFA_PASSWORD")
    if not user or not password:
        sys.exit("В .env нет ALFA_USER / ALFA_PASSWORD")
    body = urllib.parse.urlencode({"userName": user, "password": password, "orderNumber": "check-0"}).encode()
    for name, host in HOSTS.items():
        url = f"https://{host}/payment/rest/getOrderStatusExtended.do"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body), timeout=20, context=SSL) as r:
                data = json.loads(r.read().decode("utf-8"))
            print(f"{name} ({host}): errorCode={data.get('errorCode')} {data.get('errorMessage', '')}")
        except Exception as e:
            print(f"{name} ({host}): ошибка {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
