"""Telegram: чтение открытых каналов через веб-версию t.me/s/<канал> (без аккаунта) и публикация ботом.

Посты приводятся к тому же виду, что посты стены VK (owner_id, id, date, text, attachments, copy_history),
поэтому отбор, пересказ и карточки согласования — общие с VK.
"""
import json
import re
from datetime import datetime
from html.parser import HTMLParser

import httpx

CAPTION_MAX = 1024  # лимит подписи к фото в Telegram


class _Channel(HTMLParser):
    """Разбирает страницу t.me/s/<канал> в список постов."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts, self.cur, self.depth = [], None, 0  # depth > 0 — мы внутри блока текста поста

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if "tgme_widget_message" in cls and a.get("data-post"):
            self.cur = {"post": a["data-post"], "text": [], "photos": [], "video": False, "fwd": False, "date": 0}
            self.posts.append(self.cur)
            return
        if not self.cur:
            return
        if self.depth:
            self.depth += tag == "div"
            if tag == "br":
                self.cur["text"].append("\n")
        elif "tgme_widget_message_text" in cls:
            self.depth = 1
        elif "tgme_widget_message_photo_wrap" in cls:
            m = re.search(r"background-image:url\('([^']+)'\)", a.get("style") or "")
            if m:
                self.cur["photos"].append(m.group(1))
        elif any(c.startswith(("tgme_widget_message_video", "tgme_widget_message_roundvideo")) for c in cls):
            self.cur["video"] = True
        elif any(c.startswith("tgme_widget_message_forwarded_from") for c in cls):
            self.cur["fwd"] = True
        elif tag == "time" and "time" in cls and a.get("datetime"):
            self.cur["date"] = int(datetime.fromisoformat(a["datetime"]).timestamp())

    def handle_endtag(self, tag):
        if self.depth and tag == "div":
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.cur["text"].append(data)


def parse_channel(html: str) -> list[dict]:
    p = _Channel()
    p.feed(html)
    out = []
    for m in p.posts:
        name, num = m["post"].rsplit("/", 1)
        att = [{"type": "photo", "photo": {"sizes": [{"width": 1, "height": 1, "url": u}]}} for u in m["photos"]]
        out.append({"owner_id": f"tg:{name}", "id": int(num), "date": m["date"], "text": "".join(m["text"]).strip(),
                    "attachments": att + ([{"type": "video"}] if m["video"] else []),
                    **({"copy_history": [{}]} if m["fwd"] else {})})
    return out


async def fetch_channel(http: httpx.AsyncClient, name: str) -> list[dict]:
    r = await http.get(f"https://t.me/s/{name}")
    r.raise_for_status()
    return parse_channel(r.text)


async def publish(http: httpx.AsyncClient, token: str, channel: str, text: str, urls: list[str]) -> int:
    """Публикует пост в канал от имени бота: фото перезаливаются файлами, в исходном порядке. → id сообщения.

    Подпись к фото в Telegram не длиннее 1024 символов — длинный текст уходит отдельным сообщением следом."""
    api = f"https://api.telegram.org/bot{token}/"

    async def call(method: str, data: dict, files: dict | None = None) -> dict:
        d = (await http.post(api + method, data=data, files=files)).json()
        if not d.get("ok"):
            raise RuntimeError(f"Telegram {method}: {d.get('description')}")
        return d["result"]

    if not urls:
        return (await call("sendMessage", {"chat_id": channel, "text": text}))["message_id"]
    photos = [(await http.get(u)).content for u in urls[:10]]
    caption = {"caption": text} if len(text) <= CAPTION_MAX else {}
    if len(photos) == 1:
        first = (await call("sendPhoto", {"chat_id": channel, **caption}, {"photo": ("photo.jpg", photos[0])}))["message_id"]
    else:
        media = [{"type": "photo", "media": f"attach://f{i}", **(caption if i == 0 else {})} for i in range(len(photos))]
        res = await call("sendMediaGroup", {"chat_id": channel, "media": json.dumps(media, ensure_ascii=False)},
                         {f"f{i}": (f"photo{i}.jpg", b) for i, b in enumerate(photos)})
        first = res[0]["message_id"]
    if not caption:
        await call("sendMessage", {"chat_id": channel, "text": text})
    return first
