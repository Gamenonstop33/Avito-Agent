"""VK «🔑 Выдать учётку» (20.10): запрос владельца свободным текстом — «ссылка приставка 2 месяца», «ссылка пк 4 месяца».
LLM приводит его к виду {чат, консоль/ПК, срок}; код проверяет: чат — только тот, что есть в тексте, срок 1–24,
платформа консоль или ПК (не сказано — консоль). LLM недоступна — тот же разбор регулярками."""
import logging
import re

log = logging.getLogger("owner_bot")

CHAT = re.compile(r"u2[iu]-[\w~-]+")   # u2u- — чаты не по объявлению, в Avito такие тоже есть
MONTHS = re.compile(r"(?i)(?<![\d.,])(\d{1,2})\s*(?:мес|m(?![a-z])|м(?![а-я]))")
PC = re.compile(r"(?i)(?<![а-яa-z])(?:пк|pc|комп(?:ьютер)?[а-я]{0,3}|ноут[а-я]*)(?![а-яa-z])")   # «на компе»
HELP = ("🔑 Пришлите одним сообщением: ссылку на чат Avito, приставка или ПК, срок.\n"
        "Например:\nhttps://www.avito.ru/profile/messenger/channel/u2i-… приставка 2 месяца\n"
        "https://www.avito.ru/profile/messenger/channel/u2i-… пк 4 месяца\n\n"
        "Выдам учётку по нашим правилам (место как в прошлый раз, иначе свободное), впишу клиента в таблицу, "
        "закрою его заказ как оплаченный, клиенту отправлю шаги и инструкцию, Вам — данные учётки.")
PROMPT = """Владелец магазина подписок Xbox просит выдать клиенту учётку. В сообщении: ссылка на чат Avito, платформа и
срок в месяцах. Верни JSON {"chat_id": "...", "product": "console" | "pc", "months": число}.
chat_id — идентификатор чата из ссылки (часть после /channel/, начинается с u2i- или u2u-), без изменений.
product: приставка, консоль, xbox, иксбокс, хбокс — "console"; пк, компьютер, комп, pc — "pc"; не сказано — "console".
months — срок цифрой («два месяца» → 2, «полгода» → 6, «год» → 12). Чего нет в сообщении — null."""


async def parse(llm, text: str) -> dict | str:
    """{chat_id, product, months} или текст, что не так."""
    chats = CHAT.findall(text)
    if len(set(chats)) != 1:
        return ("Не нашёл ссылку на чат Avito 🤷" if not chats else "В сообщении несколько чатов — пришлите по одному.") \
            + "\n\n" + HELP
    data = {}
    try:
        data = await llm.chat_json(PROMPT, text, temperature=0, max_tokens=200)
    except Exception as e:   # разбор регулярками ниже
        log.warning("выдача учётки: LLM не разобрала запрос: %s", e)
    months = data.get("months")
    months = int(months) if isinstance(months, (int, float)) or str(months).isdigit() else None
    if not months or not 1 <= months <= 24:
        m = MONTHS.search(text)
        months = int(m.group(1)) if m else None
    if not months or not 1 <= months <= 24:
        return "Не понял срок — напишите, например, «2 месяца».\n\n" + HELP
    # слово «пк» в тексте — точно ПК (LLM могла пропустить); иначе как поняла LLM, не поняла — консоль
    product = "pc" if PC.search(text) else data.get("product") if data.get("product") in ("console", "pc") else "console"
    return {"chat_id": chats[0], "product": product, "months": months}
