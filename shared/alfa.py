"""Клиент платёжного шлюза Альфа-Банка (интернет-эквайринг, REST): заказ, ссылка СБП, статус, возврат.

Доступ — логин -api и пароль (ALFA_USER / ALFA_PASSWORD). Боевой адрес pay.alfabank.ru,
тестовый alfa.rbsuat.com (у него своя пара логин/пароль). Сертификаты шлюза выданы УЦ Минцифры — корень в shared/certs.
"""
import ssl

import httpx

from shared.config import ROOT, env

BASE = env("ALFA_URL") or "https://pay.alfabank.ru"
CA = ROOT / "shared" / "certs" / "russian_trusted_root_ca.pem"
RETURN_URL = env("ALFA_RETURN_URL") or "https://www.avito.ru/"   # куда вернуть после оплаты на платёжной странице

# orderStatus в getOrderStatusExtended
REGISTERED, HOLD, PAID, REVERSED, REFUNDED, ACS, DECLINED = range(7)


class AlfaError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code   # errorCode шлюза: 5 — доступ запрещён, 6 — заказ не найден


class AlfaClient:
    def __init__(self, user: str = "", password: str = "", http: httpx.AsyncClient | None = None):
        self._auth = {"userName": user or env("ALFA_USER"), "password": password or env("ALFA_PASSWORD")}
        self._http = http or httpx.AsyncClient(
            base_url=BASE, timeout=30, verify=ssl.create_default_context(cafile=str(CA)))

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, **params) -> dict:
        # POST без повторов: register.do не идемпотентен (повтор с тем же orderNumber — ошибка шлюза)
        try:
            r = await self._http.post(f"/payment/rest/{method}", data={**self._auth, **params})
        except httpx.TransportError as e:
            raise AlfaError(f"{method}: {type(e).__name__} {e}") from e
        if r.status_code >= 400:
            raise AlfaError(f"{method}: HTTP {r.status_code}")
        d = r.json()
        code = str(d.get("errorCode", "0"))
        if code != "0":
            raise AlfaError(f"{method}: {code} {d.get('errorMessage', '')}", int(code) if code.isdigit() else None)
        return d

    async def register(self, number: str, rub: int, description: str, ttl: int = 1200) -> tuple[str, str]:
        """Заказ на сумму в рублях → (orderId шлюза, ссылка на платёжную страницу). ttl — жизнь заказа, с."""
        d = await self._call("register.do", orderNumber=number, amount=rub * 100, description=description[:99],
                             returnUrl=RETURN_URL, sessionTimeoutSecs=ttl, language="ru")
        return d["orderId"], d["formUrl"]

    async def sbp_link(self, order_id: str) -> str:
        """Ссылка СБП (qr.nspk.ru) на заказ: на телефоне открывает выбор банка, на ПК показывает QR."""
        return (await self._call("sbp/c2b/qr/dynamic/get.do", mdOrder=order_id))["payload"]

    async def status(self, order_id: str) -> dict:
        """getOrderStatusExtended: orderStatus (PAID — оплачен), amount в копейках, actionCodeDescription, …"""
        return await self._call("getOrderStatusExtended.do", orderId=order_id, language="ru")

    async def refund(self, order_id: str, rub: int) -> None:
        await self._call("refund.do", orderId=order_id, amount=rub * 100)
