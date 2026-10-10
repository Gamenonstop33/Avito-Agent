"""Пересказ чужого поста через LLM и публикация на стену нашего сообщества с перезалитыми фото.

Фото грузит токен пользователя-руководителя (ключ сообщества грузить фото не может). Сейчас (VK_ALBUM_ID) — в альбом
сообщества суточным токеном мини-приложения: такое фото принадлежит сообществу и прикрепляется к записи, созданной
ключом сообщества VK_POST_TOKEN. Без альбома фото уходит в скрытый альбом пользователя, и фото целевого аккаунта VK
к записи ключа сообщества не прикрепляет — только к записи, созданной его же токеном Standalone-приложения
(VK_USER_POSTS=1, Kate; с 05.10 VK его ограничил: Flood control). Токен мини-приложения записи не публикует.
Фото только перезаливаются — вложение-ссылка показывает чужое сообщество как автора.
"""
import asyncio
import time
from datetime import datetime

import httpx

from shared.llm import LLM
from shared.vk import VK

REWRITE = """Ты редактор сообщества ВКонтакте о Xbox и играх. Перескажи пост своими словами.
Правила:
- сохрани все факты, цифры, даты, названия и смысл; ничего не выдумывай и не добавляй;
- первая строка — короткий заголовок, дальше абзацы через пустую строку; списки оставь списками с «•»;
- объём примерно как у исходного поста, живой разговорный тон, на «вы», максимум 1–2 эмодзи;
- убери всё, что продаёт или продвигает автора поста: призывы купить/оформить/приобрести, «у нас», «в личных сообщениях группы», «здесь», ссылки, упоминания сообществ и магазинов;
- если пост подводит к покупке у автора (ключ, подписка, апгрейд, «вот вам решение») — оставь только саму новость, а предложения про покупку и свойства продаваемого товара убери целиком; текст не должен намекать, что что-то продаётся;
- слова «сегодня», «завтра», «вчера» замени на точные даты или прошедшее время, считая от даты публикации исходного поста и сегодняшней даты;
- без хэштегов и без ссылок.
Пересказываешь любой пост, ничего не отбрасываешь. Отдельно отметь в "ad", если реклама — основное содержание
исходного поста: без продающей части не остаётся самостоятельной новости, либо это рекламная интеграция
стороннего бренда, розыгрыш, объявление о самом сообществе. Обычная новость с одной продающей фразой в конце
(«приобрести можно у нас») — не реклама, "ad" пустое. Решать, публиковать ли пост, будет человек.
Ответ — JSON: {"text": "...", "ad": "коротко, почему это реклама" или ""}"""


def photo_urls(post: dict) -> list[str]:
    """Ссылки на фото в исходном порядке (карусель); VK принимает не больше 10 вложений."""
    return [max(x["photo"]["sizes"], key=lambda s: s["width"] * s["height"])["url"]
            for x in post.get("attachments", []) if x["type"] == "photo"][:10]


async def rewrite(llm: LLM, post: dict, where: str = "сообщества ВКонтакте") -> dict:
    """→ {"text": ..., "ad": пометка «возможна реклама» для согласования} или {"skip": причина}.

    Рекламу не отбрасываем — только помечаем, решает человек при согласовании."""
    if post.get("copy_history"):
        return {"skip": "репост чужой записи"}
    user = (f"Пост опубликован {datetime.fromtimestamp(post['date']):%d.%m.%Y}, "
            f"сегодня {datetime.now():%d.%m.%Y}.\n\n{post['text']}")
    d = await llm.chat_json(REWRITE.replace("сообщества ВКонтакте", where), user, temperature=0.6)
    out = (d.get("text") or "").strip()
    if not out or any(s in out.lower() for s in ("http", "vk.com", "vk.ru", "t.me", "[club", "[id")):
        return {"skip": "LLM вернула пустой текст или ссылку"}
    ad = "помечен VK как реклама" if post.get("marked_as_ads") else (d.get("ad") or "").strip()
    return {"text": out, "ad": ad}


async def _upload(vk: VK, http: httpx.AsyncClient, url: str, server: dict, save: str, key: bool = True,
                  field: str = "photo", item: str = "photo", **save_args) -> str:
    """field — имя файла в форме загрузки, item — что сервер загрузки возвращает и что передаётся в save."""
    data = (await http.get(url)).content
    up = {}
    for i in range(8):  # сервер загрузки временами отвечает пустым photo, помогает пауза и повтор
        srv = await vk.call(**server)
        name, ctype = (("image.jpg", "image/jpeg"), ("photo.png", "image/png"))[i % 2]
        try:
            up = (await http.post(srv["upload_url"], files={field: (name, data, ctype)})).json()
        except (ValueError, httpx.TransportError) as e:  # сервер загрузки вернул не JSON или оборвал соединение
            up = {"error": repr(e)[:150]}
        if up.get(item) and up[item] != "[]":
            p = (await vk.call(save, server=up["server"], hash=up["hash"], **{item: up[item]}, **save_args))[0]
            return f"photo{p['owner_id']}_{p['id']}" + (f"_{p['access_key']}" if key and p.get("access_key") else "")
        await asyncio.sleep(3 + 3 * i)
    raise RuntimeError(f"VK не принял фото ({len(data)} байт): {str(up)[:200]}")


async def upload_wall(user: VK, http: httpx.AsyncClient, group_id: int, url: str) -> str:
    """Фото для стены сообщества — токеном руководителя. VK сохраняет его в скрытый альбом пользователя,
    а в альбом сообщества переносит при публикации (ключ доступа во вложении не нужен)."""
    return await _upload(user, http, url, {"method": "photos.getWallUploadServer", "group_id": group_id},
                         "photos.saveWallPhoto", key=False, group_id=group_id)


async def upload_album(user: VK, http: httpx.AsyncClient, group_id: int, album_id: int, url: str) -> str:
    """Фото в альбом сообщества — токеном руководителя (годится и суточный токен мини-приложения); владелец фото —
    сообщество, поэтому VK прикрепляет его к записи, созданной ключом сообщества."""
    return await _upload(user, http, url, {"method": "photos.getUploadServer", "album_id": album_id,
                                           "group_id": group_id},
                         "photos.save", key=False, field="file1", item="photos_list", album_id=album_id,
                         group_id=group_id)


async def upload_message(group: VK, http: httpx.AsyncClient, url: str) -> str:
    """Фото для сообщения от сообщества (карточка согласования) — ключом сообщества."""
    return await _upload(group, http, url, {"method": "photos.getMessagesUploadServer", "peer_id": 0},
                         "photos.saveMessagesPhoto")


HOLD_DAYS = 7  # на столько откладываем запись на время проверки вложений


async def publish(poster: VK, user: VK, http: httpx.AsyncClient, group_id: int, text: str, urls: list[str],
                  album_id: int = 0) -> int:
    """Публикует пост от имени сообщества, фото — в исходном порядке. Возвращает id записи.

    VK может молча отбросить вложения (так было, когда фото грузил не тот руководитель, чей ключ сообщества),
    поэтому пост с фото сначала создаётся отложенным, проверяется и только потом выходит на стену."""
    if not urls:
        return (await poster.call("wall.post", owner_id=-group_id, from_group=1, message=text))["post_id"]
    att = [await (upload_album(user, http, group_id, album_id, u) if album_id else upload_wall(user, http, group_id, u))
           for u in urls]
    pid = (await poster.call("wall.post", owner_id=-group_id, from_group=1, message=text, attachments=",".join(att),
                             publish_date=int(time.time()) + HOLD_DAYS * 86400))["post_id"]
    try:
        await asyncio.sleep(1)
        held = (await user.call("wall.get", owner_id=-group_id, filter="postponed", count=100))["items"]
        got = sum(a["type"] == "photo" for p in held if p["id"] == pid for a in p.get("attachments", []))
        if got < len(att):
            raise RuntimeError(f"VK не прикрепил фото ({got} из {len(att)}), пост на стену не вышел.")
        return (await poster.call("wall.post", owner_id=-group_id, post_id=pid))["post_id"]
    except Exception as e:  # черновик не оставляем: иначе повтор создаст второй, и через HOLD_DAYS выйдут оба
        try:  # токен Standalone-приложения может убрать черновик сам, токен мини-приложения — нет
            await user.call("wall.delete", owner_id=-group_id, post_id=pid)
        except Exception:
            raise RuntimeError(f"{e} Черновик остался в «Отложенных» сообщества (на {HOLD_DAYS} дней) — "
                               "удалите его там.") from e
        raise
