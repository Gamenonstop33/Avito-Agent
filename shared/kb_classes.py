"""Классы базы знаний: core (Game Pass, всегда в промпте) и дополнительные, подключаемые по теме вопроса."""
import re

CLASSES = {
    "core": "подписка Xbox Game Pass Ultimate",
    "games": "игры и ключи для Xbox (отдельная покупка игр, GTA, предзаказы, игры на личный аккаунт)",
    "steam": "Steam: игры и ключи Steam/Rockstar, смена региона Steam",
    "claude": "Claude AI (не Xbox — ведёт владелец сам)",
}
# Steam не продаём (30.10): чаты про Steam выделяются в свой класс, чтобы не попасть в базу знаний; раздела steam нет
KB = ("core", "games")
EXTRA = [c for c in KB if c != "core"]

_STEAM = re.compile(r"(?i)steam|стим|rockstar|рокстар|смен\w* регион")
_GAMEPASS = re.compile(r"(?i)game\s*pass|gamepass|ultimate|ультимейт|гейм\s*пасс|подписк")
_GAMES = re.compile(r"(?i)\bключ|\bигр[аыу]\b|купить игру|gta|гта|forza|форз|fifa|фифа|ea\s*fc|call of duty|cod\b"
                    r"|diablo|hogwarts|cyberpunk|киберпанк|red dead|rdr|mortal kombat|предзаказ|\bdlc\b")


# Объявление про Steam: такие чаты — не покупка (клиент не «покупавший», в продлении и рассылках их нет).
# Steam Deck — это ПК, не Steam (как brain.STEAM_WORD)
STEAM_ITEM = re.compile(r"(?i)steam(?!\s*deck)|(?<![а-яё])стим(?:а|е|у|ом|овск\w*)?(?![а-яё])(?!\s*дек)")


def steam_item(item_title: str | None) -> bool:
    return bool(item_title and STEAM_ITEM.search(item_title))


# Объявления про Claude (PREMIUM, 20.11, Леонид: «их не отправляй в бота, а пиши сразу мне в вк одному уведомления»):
# не Xbox — бот в чате молчит, каждое сообщение клиента — админу в VK (worker.side_chat); не покупка, в рассылках,
# сводках, напоминаниях, примерах ответов владельца, правилах и базе знаний их нет. CLAUDE_RE — и для SQL (~*)
CLAUDE_RE = "claude|клод"
CLAUDE_ITEM = re.compile("(?i)" + CLAUDE_RE)


def claude_item(item_title: str | None) -> bool:
    return bool(item_title and CLAUDE_ITEM.search(item_title))


def outside_item(item_title: str | None) -> bool:
    """Объявление не про то, что продаёт бот (Steam, Claude): чат — не покупка Xbox."""
    return steam_item(item_title) or claude_item(item_title)


def classify_dialog(item_title: str | None, client_texts: list[str]) -> str:
    """Класс чата: по названию объявления, а для личных переписок — по словам клиента."""
    if item_title:
        if claude_item(item_title):
            return "claude"
        if _STEAM.search(item_title):
            return "steam"
        if re.search(r"(?i)game\s*pass|ultimate", item_title) or item_title.lower().startswith("подписк"):
            return "core"
        return "games"
    text = " ".join(client_texts)
    if _STEAM.search(text):
        return "steam"
    if _GAMES.search(text) and not _GAMEPASS.search(text):
        return "games"
    return "core"


def route(text: str) -> list[str]:
    """Какие дополнительные классы явно нужны для сообщения клиента (дёшево, без LLM)."""
    return ["games"] if _GAMES.search(text or "") else []
