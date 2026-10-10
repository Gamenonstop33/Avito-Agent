"""Классы базы знаний: core (Game Pass, всегда в промпте) и дополнительные, подключаемые по теме вопроса."""
import re

CLASSES = {
    "core": "подписка Xbox Game Pass Ultimate",
    "games": "игры и ключи для Xbox (отдельная покупка игр, GTA, предзаказы, игры на личный аккаунт)",
    "steam": "Steam: игры и ключи Steam/Rockstar, смена региона Steam",
}
EXTRA = [c for c in CLASSES if c != "core"]

_STEAM = re.compile(r"(?i)steam|стим|rockstar|рокстар|смен\w* регион")
_GAMEPASS = re.compile(r"(?i)game\s*pass|gamepass|ultimate|ультимейт|гейм\s*пасс|подписк")
_GAMES = re.compile(r"(?i)\bключ|\bигр[аыу]\b|купить игру|gta|гта|forza|форз|fifa|фифа|ea\s*fc|call of duty|cod\b"
                    r"|diablo|hogwarts|cyberpunk|киберпанк|red dead|rdr|mortal kombat|предзаказ|\bdlc\b")


def classify_dialog(item_title: str | None, client_texts: list[str]) -> str:
    """Класс чата: по названию объявления, а для личных переписок — по словам клиента."""
    if item_title:
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
    return [c for c, rx in (("steam", _STEAM), ("games", _GAMES)) if rx.search(text or "")]
