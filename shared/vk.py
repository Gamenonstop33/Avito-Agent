"""Минимальный клиент VK API для бота сообщества (Bots Long Poll)."""
import asyncio
import json
import logging
import random
import ssl
import time

import certifi
import httpx

API = "https://api.vk.com/method/"
V = "5.199"
log = logging.getLogger("vk")


TEMPORARY = {1, 6, 9, 10, 29}  # неизвестная ошибка, слишком часто, flood control, ошибка сервера, лимит метода


class VKError(Exception):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


class VK:
    def __init__(self, token: str, group_id: int):
        self.token, self.group_id = token, group_id
        self.on_sent = None   # async (peer_id, conversation_message_id): owner-bot запоминает, чтобы убрать через сутки
        self._http = httpx.AsyncClient(timeout=40, verify=ssl.create_default_context(cafile=certifi.where()))

    async def close(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, **params) -> dict:
        r = await self._http.post(API + method, data={**params, "access_token": self.token, "v": V})
        d = r.json()
        if "error" in d:
            e = d["error"]
            raise VKError(f"{method}: {e.get('error_msg')} (код {e.get('error_code')})", e.get("error_code") or 0)
        return d["response"]

    async def upload_photo(self, peer_id: int, data: bytes, budget: float = 60) -> str:
        """Загружает картинку для сообщения → строка вложения «photo<owner>_<id>_<key>».
        Сервер загрузки VK (pu.vk.com) часто отвечает 504 «temporarily unavailable» (09.10 — ~40% запросов,
        и с хоста тоже), удачная загрузка идёт ~10 с — повторяем с новым адресом, пока не выйдет budget секунд."""
        end, n = time.monotonic() + budget, 0
        while True:
            n += 1
            try:
                srv = await self.call("photos.getMessagesUploadServer", peer_id=peer_id)
                r = await self._http.post(srv["upload_url"], files={"photo": ("image.jpg", data, "image/jpeg")})
                r.raise_for_status()
                up = r.json()
                if up.get("photo") in (None, "", "[]"):
                    raise VKError(f"сервер загрузки не принял фото: {str(up)[:200]}")
                p = (await self.call("photos.saveMessagesPhoto", photo=up["photo"], server=up["server"],
                                     hash=up["hash"]))[0]
                return f"photo{p['owner_id']}_{p['id']}" + (f"_{p['access_key']}" if p.get("access_key") else "")
            except (httpx.HTTPError, ValueError, KeyError, VKError) as e:
                if time.monotonic() > end:
                    raise VKError(f"фото не загрузилось за {n} попыток: {e}") from e
                log.warning("VK: загрузка фото, попытка %d: %s", n, str(e)[:150])
                await asyncio.sleep(1)

    async def send(self, peer_id: int, text: str, keyboard: dict | None = None,
                   attachment: str | None = None) -> int | None:
        """Возвращает conversation_message_id последней части (для ответов свайпом)."""
        parts = [text[i:i + 4000] for i in range(0, len(text), 4000)]  # лимит VK ~4096 символов
        conv_id = None
        for n, part in enumerate(parts):
            extra = {"keyboard": json.dumps(keyboard, ensure_ascii=False)} if keyboard and n == len(parts) - 1 else {}
            if attachment and n == len(parts) - 1:
                extra["attachment"] = attachment
            r = await self.call("messages.send", peer_ids=str(peer_id), message=part,
                                random_id=random.randint(1, 2 ** 31), **extra)
            conv_id = r[0].get("conversation_message_id") if isinstance(r, list) and r else None
            if conv_id and self.on_sent:
                await self.on_sent(peer_id, conv_id)
        return conv_id

    async def listen(self, with_events: bool = False):
        """Бесконечный генератор событий message_new. Обрывы сети переживает сам: ждёт и переподключается.

        with_events — отдаёт ещё и нажатия callback-кнопок (message_event) как {"event": {...}}."""
        srv = await self.call("groups.getLongPollServer", group_id=self.group_id)
        ts = srv["ts"]
        while True:
            try:
                r = (await self._http.get(srv["server"], params={"act": "a_check", "key": srv["key"], "ts": ts,
                                                                 "wait": 25})).json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("VK Long Poll: %s — переподключаюсь", type(e).__name__)
                await asyncio.sleep(3)
                try:
                    srv = await self.call("groups.getLongPollServer", group_id=self.group_id)
                    ts = srv["ts"]
                except (httpx.HTTPError, ValueError, VKError):
                    pass
                continue
            if "failed" in r:
                if r["failed"] == 1:
                    ts = r["ts"]
                else:
                    srv = await self.call("groups.getLongPollServer", group_id=self.group_id)
                    ts = srv["ts"]
                continue
            ts = r["ts"]
            for u in r.get("updates", []):
                if u["type"] == "message_new":
                    yield u["object"]["message"]
                elif with_events and u["type"] == "message_event":
                    yield {"event": u["object"]}


def button(label: str, cmd: str, color: str = "secondary", callback: bool = False) -> dict:
    """callback=True — нажатие не пишет сообщение в чат, а приходит событием message_event."""
    return {"action": {"type": "callback" if callback else "text", "label": label,
                       "payload": json.dumps({"cmd": cmd})}, "color": color}


def keyboard(rows: list[list[dict]], inline: bool = False) -> dict:
    return {"inline": True, "buttons": rows} if inline else {"one_time": False, "buttons": rows}


def payload_cmd(msg: dict) -> str | None:
    try:
        return json.loads(msg.get("payload") or "{}").get("cmd")
    except (ValueError, AttributeError):
        return None
