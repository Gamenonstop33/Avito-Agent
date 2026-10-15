from datetime import datetime

from services.dialog.brain import parse_decision
from services.dialog.gamepass import block, find, parse_site, popular

GAMES = [
    {"title_en": "EA SPORTS FC™ 26 Xbox Series X|S", "title_ru": "EA SPORTS FC™ 26 для Xbox Series X|S",
     "console": True, "pc": False, "ea_play": True, "ru": None},
    {"title_en": "EA SPORTS FC™ 25 - PC", "title_ru": "EA SPORTS FC™ 25 - PC", "console": False, "pc": True,
     "ea_play": True, "ru": None},
    {"title_en": "Call of Duty®: Modern Warfare® III - Cross-Gen Bundle",
     "title_ru": "Call of Duty®: Modern Warfare® III - Набор Cross-Gen", "console": True, "pc": False, "ea_play": False, "ru": None},
    {"title_en": "Assassin's Creed Valhalla", "title_ru": "Assassin's Creed Valhalla", "console": True, "pc": False,
     "ea_play": False, "ru": None},
    {"title_en": "Forza Horizon 5 Standard Edition", "title_ru": "Forza Horizon 5: стандартное издание",
     "console": True, "pc": True, "ea_play": False, "ru": None},
]


def titles(q):
    return [g["title_en"] for g in find(GAMES, q)]


def test_find_exact_part_number():
    assert titles("EA Sports FC 26") == ["EA SPORTS FC™ 26 Xbox Series X|S"]
    assert titles("FC 27") == [] and titles("FIFA 23") == []
    assert titles("Call of Duty Modern Warfare 3") == ["Call of Duty®: Modern Warfare® III - Cross-Gen Bundle"]


def test_find_typos_and_edition_words():
    assert titles("Assassins Creed Valhalla") == ["Assassin's Creed Valhalla"]
    assert titles("Forza Horizon 5 Standard Edition") == ["Forza Horizon 5 Standard Edition"]
    assert titles("Grand Theft Auto V") == []


def test_block_and_games_field():
    st = {"site": 659, "console": 621, "pc": 604, "updated": datetime(2026, 10, 6), "popular": ["Forza Horizon 5"],
          "new": [("Gears of War: E-Day", datetime(2026, 10, 6))]}
    text = block(st, {"FC 26": find(GAMES, "FC 26"), "GTA 5": []})
    assert "06.10" in text and "больше 650 игр" in text and "ПОПУЛЯРНЫЕ" in text and "Gears of War: E-Day (06.10)" in text
    assert "«FC 26»: есть — EA SPORTS FC™ 26 Xbox Series X|S (консоль, EA Play)" in text and "«GTA 5»: в подписке нет" in text
    assert "не загружен" in block(None, {})
    assert parse_decision('{"reply": "", "handoff": null, "games": ["EA Sports FC 26", 5, ""]}').games == ["EA Sports FC 26"]


PAGE = """<style>#game-list-results{}</style><div class="game-slide-title">Gears of War: E-Day</div>
<div class="game-slide-date">Добавлена: 06.10.2026</div><div id="game-list-results"><ul class="game-list">
<li><div class="game-thumbnail "><span class="ru-subtitles-badge-clickable" data-game-id="1">RU</span>
<div class="game-title">Gears of War: E-Day</div></div></li>
<li><div class="game-thumbnail "><div class="game-title">Halo 3</div></div></li></ul></div>"""


def test_parse_site():
    games = {g["title_en"]: g for g in parse_site(PAGE)}
    assert games["Gears of War: E-Day"]["ru"] and games["Gears of War: E-Day"]["added"].isoformat() == "2026-10-06"
    assert not games["Halo 3"]["ru"] and games["Halo 3"]["added"] is None and games["Halo 3"]["console"] is None


def test_popular_in_subscription_without_repeats():
    rows = [{"title": "Grand Theft Auto V", "chats": 400}, {"title": "Forza Horizon 5", "chats": 150},
            {"title": "Forza", "chats": 30}, {"title": "EA Sports FC", "chats": 80}, {"title": "EA SPORTS FC", "chats": 5}]
    assert popular(rows, GAMES) == ["Forza Horizon 5", "EA Sports FC"]   # GTA нет в подписке, «Forza» — повтор
