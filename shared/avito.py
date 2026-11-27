"""Клиент Avito API: токен, чаты, сообщения, объявления."""
import asyncio
import ssl
import time

import certifi
import httpx

BASE = "https://api.avito.ru"


class AvitoError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status   # HTTP-код; 4xx — повторять бессмысленно


class AvitoAuthError(AvitoError):
    """Не удалось получить доступ; сообщение не содержит ключей или токена."""


class AvitoClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient | None = None):
        self._cid, self._secret = client_id, client_secret
        self._http = http or httpx.AsyncClient(
            base_url=BASE, timeout=30, verify=ssl.create_default_context(cafile=certifi.where()))
        self._token: str | None = None
        self._token_exp = 0.0
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    async def _get_token(self) -> str:
        async with self._token_lock:
            return await self._load_token()

    async def _load_token(self) -> str:
        if not self._token or time.time() > self._token_exp - 300:
            r = await self._http.post("/token", data={
                "grant_type": "client_credentials", "client_id": self._cid, "client_secret": self._secret})
            try:
                d = r.json()
            except ValueError:
                raise AvitoAuthError(f"Avito: некорректный ответ сервиса токенов (HTTP {r.status_code}).", r.status_code) from None
            if r.status_code >= 400 or not isinstance(d, dict) or not d.get("access_token"):
                code = d.get("error") if isinstance(d, dict) else None
                reason = "ключи приложения не авторизованы" if code == "unauthorized_client" else "токен не выдан"
                raise AvitoAuthError(f"Avito: {reason} (HTTP {r.status_code}). Проверьте доступ приложения и ключи API.", r.status_code)
            self._token, self._token_exp = d["access_token"], time.time() + d.get("expires_in", 3600)
        return self._token

    async def _req(self, method: str, path: str, **kw) -> dict | list:
        r = None
        refreshed = False
        for attempt in range(5):
            token = await self._get_token()
            try:
                r = await self._http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kw)
            except httpx.TransportError as e:
                # обрыв/таймаут: GET повторяем; POST — только если запрос точно не ушёл (не соединились),
                # иначе отправка могла пройти — решает вызывающий (gateway проверяет чат перед повтором)
                if attempt == 4 or (method != "GET" and not isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))):
                    raise AvitoError(f"{method} {path}: {type(e).__name__} {e}") from e
                await asyncio.sleep(2 ** attempt)
                continue
            invalid_token = r.status_code == 401
            if r.status_code == 403:
                try:
                    body = r.json()
                    invalid_token = body.get("result", {}).get("message") == "invalid access token"
                except (ValueError, AttributeError):
                    invalid_token = False
            if invalid_token and not refreshed:
                refreshed = True
                if self._token == token:
                    self._token = None
                continue
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise AvitoError(f"{method} {path}: {r.status_code} {r.text[:200]}", r.status_code)
            return r.json() if r.content else {}
        raise AvitoError(f"{method} {path}: не удалось после повторов ({r.status_code if r else 'сеть'})",
                         r.status_code if r else None)

    async def me(self) -> dict:
        return await self._req("GET", "/core/v1/accounts/self")

    async def chats(self, user_id: int, limit: int = 100, offset: int = 0) -> list[dict]:
        d = await self._req("GET", f"/messenger/v2/accounts/{user_id}/chats", params={
            "limit": limit, "offset": offset, "chat_types": "u2i,u2u"})
        return d.get("chats", [])

    async def chat(self, user_id: int, chat_id: str) -> dict:
        return await self._req("GET", f"/messenger/v2/accounts/{user_id}/chats/{chat_id}")

    async def messages(self, user_id: int, chat_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """Сообщения чата, от новых к старым."""
        d = await self._req("GET", f"/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/",
                            params={"limit": limit, "offset": offset})
        return d if isinstance(d, list) else d.get("messages", [])

    async def send_text(self, user_id: int, chat_id: str, text: str) -> dict:
        return await self._req("POST", f"/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages",
                               json={"message": {"text": text}, "type": "text"})

    async def mark_read(self, user_id: int, chat_id: str) -> None:
        await self._req("POST", f"/messenger/v1/accounts/{user_id}/chats/{chat_id}/read")

    async def items(self, page: int = 1, per_page: int = 100) -> list[dict]:
        d = await self._req("GET", "/core/v1/items", params={"page": page, "per_page": per_page})
        return d.get("resources", [])
