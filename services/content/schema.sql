CREATE SCHEMA IF NOT EXISTS content;

-- approve_vk / approve_tg: '1' — пост ждёт согласования, '0' — публикуется сам; started_at — берём посты новее
CREATE TABLE IF NOT EXISTS content.settings (
    key   text PRIMARY KEY,
    value text NOT NULL
);

-- Кто согласует: участники сообщества согласования, написавшие ему хотя бы раз
CREATE TABLE IF NOT EXISTS content.reviewers (
    vk_id    bigint PRIMARY KEY,
    added_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS content.posts (
    id          bigserial PRIMARY KEY,
    source      text NOT NULL,             -- vk:xbox1store
    source_key  text NOT NULL UNIQUE,      -- <owner_id>_<post_id> исходного поста
    source_date timestamptz NOT NULL,
    title       text NOT NULL,             -- первая строка исходного поста
    text        text,                      -- пересказ, который уйдёт на стену
    ad          text NOT NULL DEFAULT '',  -- пометка «возможна реклама» (причина) или пусто
    photos      jsonb NOT NULL DEFAULT '[]',   -- ссылки на фото источника в исходном порядке
    card_att    text NOT NULL DEFAULT '',  -- фото, загруженные для карточки согласования
    status      text NOT NULL,             -- pending | publishing | queued | published | skipped | expired
    note        text,                      -- почему пропущен / последняя ошибка публикации
    decided_by  text,                      -- vk:<id> или auto
    vk_post_id  bigint,
    created_at  timestamptz NOT NULL DEFAULT now(),
    decided_at  timestamptz
);
-- Отправленные карточки согласования: после решения удаляем их из чатов, чтобы не висели
CREATE TABLE IF NOT EXISTS content.cards (
    post_id bigint NOT NULL REFERENCES content.posts,
    vk_id   bigint NOT NULL,
    cmid    bigint NOT NULL,
    PRIMARY KEY (post_id, vk_id, cmid)
);
-- Служебные ответы бота (меню, подтверждения, списки): удаляем из чата через несколько минут, чтобы не копились
CREATE TABLE IF NOT EXISTS content.temp_msgs (
    vk_id     bigint NOT NULL,
    cmid      bigint NOT NULL,
    delete_at timestamptz NOT NULL,
    PRIMARY KEY (vk_id, cmid)
);
-- Откуда копируем: сообщества VK и открытые каналы Telegram; список правится из сообщества согласования
CREATE TABLE IF NOT EXISTS content.sources (
    target   text NOT NULL,             -- vk | tg: куда идут посты этого источника
    name     text NOT NULL,             -- короткое имя: vk.com/<name> или t.me/<name>
    title    text NOT NULL DEFAULT '',
    added_by text,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (target, name)
);
-- Куда публикуем: vk — стена сообщества, tg — Telegram-канал (посты из Telegram идут только в Telegram)
ALTER TABLE content.posts ADD COLUMN IF NOT EXISTS target text NOT NULL DEFAULT 'vk';
CREATE INDEX IF NOT EXISTS posts_pending_idx ON content.posts (created_at) WHERE status = 'pending';
