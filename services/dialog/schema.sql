CREATE SCHEMA IF NOT EXISTS dialog;

CREATE TABLE IF NOT EXISTS dialog.chats (
    chat_id        text PRIMARY KEY,
    account_id     bigint NOT NULL,
    state          text NOT NULL DEFAULT 'bot',   -- bot | human (передан человеку, бот молчит)
    handoff_reason text,
    handoff_at     timestamptz,
    stage          text,
    updated_at     timestamptz NOT NULL DEFAULT now()
);


-- Спорные ситуации: что спросил клиент, что предлагал бот, что ответил Макс. Отвеченные — примеры для LLM
CREATE TABLE IF NOT EXISTS dialog.handoffs (
    id           bigserial PRIMARY KEY,
    chat_id      text NOT NULL,
    account_id   bigint NOT NULL,
    reason       text NOT NULL,
    summary      text,
    client_text  text,
    context      jsonb,                 -- последние реплики [[кто, текст], ...]
    bot_draft    text,                  -- что бот хотел ответить (если было)
    owner_answer text,
    answered_by  text,
    status       text NOT NULL DEFAULT 'open',   -- open | answered | resumed
    created_at   timestamptz NOT NULL DEFAULT now(),
    answered_at  timestamptz
);

-- Какие сообщения в VK — алерты по какому случаю (чтобы понять ответ свайпом)
CREATE TABLE IF NOT EXISTS dialog.alert_msgs (
    handoff_id  bigint NOT NULL REFERENCES dialog.handoffs,
    vk_peer     bigint NOT NULL,
    conv_msg_id bigint NOT NULL,
    PRIMARY KEY (vk_peer, conv_msg_id)
);

ALTER TABLE dialog.handoffs ADD COLUMN IF NOT EXISTS repinged_at timestamptz;   -- ночью: повторный алерт
ALTER TABLE dialog.handoffs ADD COLUMN IF NOT EXISTS deferred_at timestamptz;   -- ночью: клиенту «ответим утром»

-- Чат у человека (state='human'): handoff_reason='owner' — Макс сам написал в Avito, бот молчит.
-- Клиент ждёт ответа: час — «нужно время» + пинг, дальше пинг раз в час до конца дня; ночью — «ответим утром».
-- Отметки относятся к текущему ожиданию (раньше его начала — не в счёт). 8 ч без Макса — чат снова у бота.
ALTER TABLE dialog.chats ADD COLUMN IF NOT EXISTS owner_at timestamptz;          -- последний ответ Макса
ALTER TABLE dialog.chats ADD COLUMN IF NOT EXISTS wait_pinged_at timestamptz;    -- последний пинг Максу
ALTER TABLE dialog.chats ADD COLUMN IF NOT EXISTS wait_nudged_at timestamptz;    -- клиенту «нужно время»
ALTER TABLE dialog.chats ADD COLUMN IF NOT EXISTS wait_deferred_at timestamptz;  -- клиенту «ответим утром»

-- Скидка новым: один раз напомнили (молчит 5 мин после прайса или сомневается)
ALTER TABLE dialog.chats ADD COLUMN IF NOT EXISTS discount_nudged_at timestamptz;
-- Спрашивал «нужно каждый раз входить?» — ответили «нет»: после оплаты только пустая учётка (sales.PICK)
ALTER TABLE dialog.chats ADD COLUMN IF NOT EXISTS want_empty boolean NOT NULL DEFAULT false;
-- Клиент назвал срок — один раз предложили срок больше (brain.UPSELL), дальше заказ без допродажи
ALTER TABLE dialog.chats ADD COLUMN IF NOT EXISTS upsell_at timestamptz;

-- Утренние сводки (чтобы не слать дважды)
CREATE TABLE IF NOT EXISTS dialog.digests (
    day     date PRIMARY KEY,
    sent_at timestamptz NOT NULL DEFAULT now()
);
