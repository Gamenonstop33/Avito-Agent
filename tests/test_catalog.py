from shared.catalog import diff, parse, render

PRODUCTS = [
    {"code": "console", "title": "🎮 XBOX ULTIMATE дом консоль (играете со своего аккаунта)",
     "keywords": ["консол", "xbox", "дом"]},
    {"code": "pc", "title": "💻 ПК", "keywords": ["пк", "pc", "компьютер"]},
]
PRICES = {"console": {2: 1090, 6: 2290, 20: 6890}, "pc": {1: 400, 20: 3900}}

MAX_TEMPLATE = """🎮 XBOX ULTIMATE дом консоль (играете со своего аккаунта)
2 мес   -  1090₽
6 мес   -  2 290₽ 💫 ХИТ
14 мес  - 5090₽ 2 🎁
20мес  - 6890₽ 2 🎁

🎁 Игра в подарок

💻 ПК
🎮1 мес  — 400₽
🎮20 мес — 3 900 ₽"""


def test_parse_max_format():
    p = parse(MAX_TEMPLATE, PRODUCTS)
    assert not p.errors
    assert p.changes == {"console": {2: 1090, 6: 2290, 14: 5090, 20: 6890}, "pc": {1: 400, 20: 3900}}


def test_roundtrip_render_parse():
    assert parse(render(PRODUCTS, PRICES), PRODUCTS).changes == PRICES


def test_section_replaced_whole_other_untouched():
    p = parse("консоль\n2 мес — 1090\n6 мес — 2390\n9 мес — 2600", PRODUCTS)
    assert diff(PRICES, p.changes) == [  # 20 мес стёрли → удаляется; ПК не присылали → не трогаем
        ("console", 6, 2290, 2390), ("console", 9, None, 2600), ("console", 20, 6890, None)]


def test_errors():
    assert parse("6 мес — 2390", PRODUCTS).errors            # нет раздела
    assert parse("ПК\n3 мес — 5", PRODUCTS).errors          # странная цена


def test_personal_price_section_not_taken_by_xbox_keyword():
    products = PRODUCTS + [{"code": "personal", "title": "🎮 XBOX Game Pass Ultimate на Ваш аккаунт",
                            "keywords": ["ваш аккаунт", "личный аккаунт", "личн"]}]
    text = "🎮 XBOX Game Pass Ultimate на Ваш аккаунт:\n4 мес -4 190 Р.\n6 мес -4 890 Р.\n\n" + MAX_TEMPLATE
    res = parse(text, products)
    assert res.changes["personal"] == {4: 4190, 6: 4890} and 2 in res.changes["console"]


def test_gifts_in_price_and_note_line_parses_back():
    products = [dict(PRODUCTS[0], gifts={10: 1, 14: 1}, raffle={14}, note="🎁 Игра в подарок от 10 мес"), PRODUCTS[1]]
    prices = {"console": {6: 2290, 10: 3590, 14: 5090}, "pc": {1: 400}}
    text = render(products, prices)
    assert "10 мес — 3 590₽ 🎁\n" in text and "14 мес — 5 090₽ 🎁 + розыгрыш" in text and "🎁 Игра в подарок от 10 мес" in text
    res = parse(text, products)
    assert res.changes == prices and not res.errors


def test_returning_client_price_has_no_gift_or_raffle():
    import asyncio
    from shared import catalog

    class FakeDB:
        async def fetch(self, q, *a):
            if "catalog.products" in q:
                return [{"code": "console", "title": "Консоль", "keywords": ["консол"], "on_request": False,
                         "note": "🎁 Игра в подарок от 10 мес; розыгрыш: {розыгрыш}"}]
            return [{"product": "console", "months": 10, "price_rub": 3590, "gifts": 1, "raffle": False},
                    {"product": "console", "months": 14, "price_rub": 5090, "gifts": 1, "raffle": True}]

        async def fetchval(self, q, *a):
            return "GTA 6"

    new, _ = asyncio.run(catalog.price_parts(FakeDB()))
    old, _ = asyncio.run(catalog.price_parts(FakeDB(), gifts=False))
    assert "+ розыгрыш" in new and "GTA 6" in new
    assert "🎁" not in old and "розыгрыш" not in old
