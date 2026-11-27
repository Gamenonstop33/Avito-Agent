"""Клиент Platega (app.platega.io): оплата по СБП QR — транзакция, ссылка НСПК, статус.

Доступ — Merchant ID и API-ключ из личного кабинета Platega, «Настройки проекта» (PLATEGA_MERCHANT_ID /
PLATEGA_SECRET), заголовки X-MerchantId / X-Secret. Документация — https://docs.platega.io.
Ссылка клиенту — прямая НСПК (qr.nspk.ru, GET /h2h/{id}: режим H2H включает менеджер Platega); не включён —
ссылка на платёжную страницу Platega (redirect). Транзакция живёт ~15 мин (expiresIn), оплату видим опросом
GET /transaction/{id} (callback не нужен).
"""
import re
import zlib
from datetime import timedelta

import httpx

from shared.config import env

BASE = env("PLATEGA_URL") or "https://app.platega.io"
RETURN_URL = env("PLATEGA_RETURN_URL") or "https://www.avito.ru/"   # куда вернуть после оплаты на странице Platega
SBP_QR = 2                     # paymentMethod: СБП (QR-код)
CONFIRMED, CANCELED, PENDING = "CONFIRMED", "CANCELED", "PENDING"
TTL = timedelta(minutes=15)    # жизнь транзакции, если в ответе нет expiresIn


class PlategaError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status   # HTTP-код ответа: 400 — не найдено / неверный запрос, 401 — неверная пара ключей


def lifetime(expires_in) -> timedelta:
    """expiresIn «HH:MM:SS» → timedelta; нет или не разобрать — TTL."""
    m = re.fullmatch(r"\s*(\d+):(\d{1,2}):(\d{1,2})\s*", str(expires_in or ""))
    if not m:
        return TTL
    h, mi, s = map(int, m.groups())
    return timedelta(hours=h, minutes=mi, seconds=s) or TTL


def buyer_email(client_id: int | None, chat_id: str) -> str:
    """metadata.user_email (обязательно): один покупатель — один адрес. Почту клиента мы не собираем — по документации
    постоянный id покупателя на известном почтовом домене; нет id клиента Avito — постоянный номер чата."""
    key = client_id if client_id else f"chat{zlib.crc32(chat_id.encode())}"
    return f"{key}@mail.ru"


class PlategaClient:
    def __init__(self, merchant: str = "", secret: str = "", http: httpx.AsyncClient | None = None):
        self._headers = {"X-MerchantId": merchant or env("PLATEGA_MERCHANT_ID"),
                         "X-Secret": secret or env("PLATEGA_SECRET")}
        self._http = http or httpx.AsyncClient(base_url=BASE, timeout=30)

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kw) -> dict:
        # без повторов: POST /transaction/process не идемпотентен (повтор — вторая транзакция)
        try:
            r = await self._http.request(method, path, headers=self._headers, **kw)
        except httpx.TransportError as e:
            raise PlategaError(f"{method} {path}: {type(e).__name__} {e}") from e
        if r.status_code >= 400:
            raise PlategaError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}", r.status_code)
        try:
            return r.json()
        except ValueError as e:
            raise PlategaError(f"{method} {path}: не JSON {r.text[:200]}") from e

    async def create(self, number: str, rub: int, description: str, client_id: int | None, chat_id: str) -> dict:
        """Транзакция СБП QR на сумму в рублях → {id, url, ttl}: url — ссылка НСПК, если H2H включён, иначе
        платёжная страница Platega; ttl — сколько живёт транзакция."""
        meta = {"user_email": buyer_email(client_id, chat_id)}
        if client_id:
            meta["userId"] = str(client_id)
        d = await self._call("POST", "/transaction/process", json={
            "paymentMethod": SBP_QR, "paymentDetails": {"amount": rub, "currency": "RUB"},
            "description": description[:250], "return": RETURN_URL, "failedUrl": RETURN_URL,
            "payload": number, "orderId": number, "metadata": meta})
        if not d.get("transactionId"):
            raise PlategaError(f"POST /transaction/process: нет transactionId в ответе {str(d)[:200]}")
        url = await self.qr(d["transactionId"]) or d.get("redirect")
        if not url:
            raise PlategaError("POST /transaction/process: нет ни ссылки НСПК, ни redirect")
        return {"id": d["transactionId"], "url": url, "ttl": lifetime(d.get("expiresIn"))}

    async def qr(self, transaction_id: str) -> str | None:
        """Ссылка НСПК (qr.nspk.ru) на транзакцию: на телефоне открывает выбор банка. H2H не включён — None."""
        try:
            return (await self._call("GET", f"/h2h/{transaction_id}")).get("qr") or None
        except PlategaError:
            return None

    async def status(self, transaction_id: str) -> dict:
        """GET /transaction/{id}: status (CONFIRMED — оплачен, CANCELED, PENDING, CHARGEBACKED), paymentDetails, …"""
        return await self._call("GET", f"/transaction/{transaction_id}")
