from services.content import logic
from services.content.vk_wall import photo_urls


def post(pid, date, **kw):
    return {"owner_id": -1, "id": pid, "date": date, "text": "Заголовок\nтекст", **kw}


def test_candidates_only_new_unseen_oldest_first():
    items = [post(5, 500), post(4, 400, is_pinned=1), post(3, 300), post(2, 200), post(1, 100)]
    got = logic.candidates(items, since_ts=150, known={"-1_3"})
    assert [p["id"] for p in got] == [2, 5]  # закреплённый, старый и уже виденный не берём


def test_skip_reason():
    assert logic.skip_reason(post(1, 1)) is None
    assert logic.skip_reason(post(1, 1, marked_as_ads=1)) is None  # рекламу не отсекаем, только помечаем
    assert logic.skip_reason(post(1, 1, attachments=[{"type": "video"}])) == "пост с видео"
    assert logic.skip_reason(post(1, 1, copy_history=[{}])) == "репост чужой записи"
    assert logic.skip_reason(post(1, 1, text="  ")) == "без текста"


def test_photo_urls_keep_order_and_limit():
    def ph(n):
        return {"type": "photo", "photo": {"sizes": [{"width": 10, "height": 10, "url": f"s{n}"},
                                                      {"width": 99, "height": 99, "url": f"big{n}"}]}}
    p = post(1, 1, attachments=[ph(n) for n in range(12)] + [{"type": "link"}])
    assert photo_urls(p) == [f"big{n}" for n in range(10)]


def test_card_shows_ad_mark_and_note():
    row = {"id": 7, "source_key": "-1_5", "ad": "продажа ключа", "text": "Текст поста"}
    c = logic.card(row, note="Сам опубликовать не смог: нет токена")
    assert "№7" in c and "⚠ Возможна реклама: продажа ключа" in c and "❗ Сам опубликовать" in c
    assert c.endswith("Текст поста")
    assert "Возможна реклама" not in logic.card(row | {"ad": ""})


def test_menu_reflects_state():
    rows = logic.menu(approve_vk=True, approve_tg=False)["buttons"]
    assert rows[0][0]["action"]["label"] == "ВК: согласование ВКЛ" and rows[0][0]["color"] == "positive"
    assert rows[1][0]["action"]["label"] == "ТГ: согласование ВЫКЛ" and rows[1][0]["color"] == "negative"


TG_HTML = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="somechan/41">
  <div class="tgme_widget_message_forwarded_from accent_color">Forwarded from <a>X</a></div>
  <div class="tgme_widget_message_text js-message_text" dir="auto">Репост</div>
  <a class="tgme_widget_message_date" href="#"><time datetime="2026-10-02T13:00:56+00:00" class="time">13:00</time></a>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="somechan/42">
  <a class="tgme_widget_message_photo_wrap blured" style="width:400px;background-image:url('https://cdn/a.jpg')"></a>
  <a class="tgme_widget_message_photo_wrap blured" style="width:400px;background-image:url('https://cdn/b.jpg')"></a>
  <div class="tgme_widget_message_text js-message_text" dir="auto"><b>Заголовок</b><br/><br/>Текст &amp; <i class="emoji"><b>🔥</b></i> <a href="https://x">ссылка</a></div>
  <a class="tgme_widget_message_date" href="#"><time datetime="2026-10-02T14:01:29+00:00" class="time">14:01</time></a>
 </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message js-widget_message" data-post="somechan/43">
  <a class="tgme_widget_message_video_player blured"><i class="tgme_widget_message_video_thumb"></i></a>
  <div class="tgme_widget_message_text js-message_text" dir="auto">Видео</div>
  <a class="tgme_widget_message_date" href="#"><time datetime="2026-10-02T12:00:00+00:00" class="time">12:00</time></a>
 </div>
</div>
"""


def test_tg_channel_parsed_like_vk_wall():
    from services.content.tg import parse_channel
    fwd, post, video = parse_channel(TG_HTML)
    assert (post["owner_id"], post["id"]) == ("tg:somechan", 42)
    assert post["text"] == "Заголовок\n\nТекст & 🔥 ссылка"
    assert photo_urls(post) == ["https://cdn/a.jpg", "https://cdn/b.jpg"]          # порядок как в посте
    assert post["date"] == 1790949689 and logic.skip_reason(post) is None
    assert logic.skip_reason(fwd) == "репост чужой записи" and logic.skip_reason(video) == "пост с видео"
    assert [p["id"] for p in logic.candidates([video, post, fwd], since_ts=0, known={"tg:somechan_41"})] == [43, 42]  # от старых к новым


def test_card_names_platform_and_source():
    tg_row = {"id": 9, "source_key": "tg:somechan_42", "ad": "", "text": "Т", "target": "tg"}
    assert "→ Telegram" in logic.card(tg_row) and "https://t.me/somechan/42" in logic.card(tg_row)
    vk_row = {"id": 7, "source_key": "-1_5", "ad": "", "text": "Т", "target": "vk"}
    assert "→ ВК" in logic.card(vk_row) and "https://vk.com/wall-1_5" in logic.card(vk_row)


def test_parse_source_accepts_only_links():
    assert logic.parse_source("https://vk.com/xbox1store") == ("vk", "xbox1store")
    assert logic.parse_source(" vk.ru/club123456?w=wall-1_2 ") == ("vk", "club123456")
    assert logic.parse_source("https://m.vk.com/some.public/") == ("vk", "some.public")
    assert logic.parse_source("https://t.me/GameHub_Official") == ("tg", "gamehub_official")
    assert logic.parse_source("t.me/s/gamehub_official") == ("tg", "gamehub_official")
    for bad in ("xbox1store", "@gamehub_official", "https://example.com/x", "добавь паблик", ""):
        assert logic.parse_source(bad) is None
    assert logic.wall_args("club123456") == {"owner_id": -123456} and logic.wall_args("xbox1store") == {"domain": "xbox1store"}


def test_sources_text_groups_by_platform():
    t = logic.sources_text([{"target": "vk", "name": "xbox1store", "title": "Xbox Store"},
                            {"target": "tg", "name": "gamehub_official", "title": ""}])
    assert "• https://vk.com/xbox1store — Xbox Store" in t and "• https://t.me/gamehub_official" in t
    assert "— пока нет" in logic.sources_text([])


def test_decision_commands_carry_platform():
    kb = logic.card_keyboard(5, True, "tg")["buttons"][0]
    assert [b["action"]["payload"] for b in kb] == ['{"cmd": "pub:tg:5"}', '{"cmd": "skip:tg:5"}']
    assert logic.parse_decision("pub:tg:5") == ("pub", "tg", 5)
    assert logic.parse_decision("skip:vk:12") == ("skip", "vk", 12)
    assert logic.parse_decision("pub:7") == ("pub", None, 7)  # карточка старого вида
    assert logic.parse_decision("pending") is None and logic.parse_decision("pub:tg:x") is None


def test_sources_text_only_own_platforms():
    rows = [{"target": "vk", "name": "a", "title": ""}, {"target": "tg", "name": "b", "title": ""}]
    only_tg = logic.sources_text(rows, ("tg",))
    assert "t.me/b" in only_tg and "vk.com" not in only_tg and "В ВК" not in only_tg


def test_retryable_only_temporary_failures():
    import httpx
    from shared.vk import VKError
    assert logic.retryable(VKError("users.get: Flood control (код 9)", 9))
    assert logic.retryable(httpx.ConnectTimeout("нет связи"))
    assert not logic.retryable(VKError("users.get: User authorization failed (код 5)", 5))
    assert not logic.retryable(RuntimeError("VK не прикрепил фото (0 из 4)"))


def test_token_reminder_morning_until_refreshed():
    from datetime import datetime, timedelta, timezone
    msk = timezone(timedelta(hours=3))
    at = lambda h, m=0: datetime(2026, 10, 7, h, m, tzinfo=msk)  # noqa: E731
    old = at(9, 30).timestamp()  # вчерашний токен истекает в 9:30
    assert not logic.token_reminder_due(at(9), old, 0, 20)          # до 10:00 не просим
    assert logic.token_reminder_due(at(10, 5), old, 0, 20)          # утром — просим
    assert not logic.token_reminder_due(at(11), old, at(10, 5).timestamp(), 20)  # повтор не раньше чем через 2 ч
    assert logic.token_reminder_due(at(12, 10), old, at(10, 5).timestamp(), 20)
    assert not logic.token_reminder_due(at(20, 30), old, 0, 20)     # вечером не беспокоим
    fresh = at(10, 7).timestamp() + 86400                           # обновили сегодня — молчим
    assert not logic.token_reminder_due(at(12, 10), fresh, at(10, 5).timestamp(), 20)


def test_token_url_and_queue():
    from services.content.vk_token import parse_url
    url = "https://oauth.vk.ru/blank.html#access_token=vk1.a.abc&expires_in=86400&user_id=1131098429"
    assert parse_url(url) == ("vk1.a.abc", 86400, 1131098429)
    assert parse_url("просто текст") is None
    assert logic.retryable(logic.TokenNeeded("токен истёк"))


def test_token_code_from_url():
    from services.content.vk_token import parse_code
    assert parse_code("https://oauth.vk.ru/blank.html?code=4f1c2a9e&state=") == "4f1c2a9e"
    assert parse_code("https://oauth.vk.com/blank.html#code=abc123") == "abc123"
    assert parse_code("https://oauth.vk.com/blank.html#access_token=vk1.a.x&user_id=1") is None
    assert parse_code("https://oauth.vk.com/blank.html?code=s1s2") == "s1s2"


def test_take_token_by_code(monkeypatch):
    import asyncio
    from services.content import worker

    class DB:
        def __init__(self):
            self.kv = {}

        async def fetchval(self, q, *a):
            return self.kv.get(a[0]) if a else None

        async def execute(self, q, *a):
            self.kv[a[0]] = a[1]

        async def fetch(self, q, *a):
            return []

    async def exchange(code):
        return ("vk1.a.tok", 86400, 1131098429) if code == "good" else ("vk1.a.other", 86400, 42)

    async def ok(token):
        return None

    monkeypatch.setattr(worker, "exchange", exchange)
    monkeypatch.setattr(worker, "check", ok)
    monkeypatch.setenv("VK_USER_ID", "1131098429")
    c = worker.Content(DB(), None, None, None, None, 0, None)

    async def run():
        bad = await c.take_token("https://oauth.vk.ru/blank.html?code=other")
        assert "другого аккаунта" in bad and "vk_user_token" not in c.db.kv
        good = await c.take_token("https://oauth.vk.ru/blank.html?code=good")
        assert good.startswith("✅") and c.db.kv["vk_user_token"] == "vk1.a.tok"
        assert await c.user_token() == "vk1.a.tok"
        await asyncio.sleep(0)  # фоновый повтор очереди
    asyncio.run(run())
