CREATE SCHEMA IF NOT EXISTS gateway;

-- Аккаунты Avito и режим работы бота на каждом
CREATE TABLE IF NOT EXISTS gateway.accounts (
    user_id     bigint PRIMARY KEY,
    name        text,
    started_at  timestamptz,               -- NULL = бот остановлен; отвечаем только на сообщения после этого момента
    all_chats   boolean NOT NULL DEFAULT false,
    whitelist   text[]  NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS gateway.chats (
    id          text PRIMARY KEY,
    account_id  bigint NOT NULL REFERENCES gateway.accounts,
    item_title  text,
    item_url    text,
    client_name text,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gateway.messages (
    id          text PRIMARY KEY,
    chat_id     text NOT NULL REFERENCES gateway.chats,
    direction   text NOT NULL,              -- in | out
    origin      text NOT NULL,              -- client | bot | owner | system
    type        text NOT NULL,
    text        text,
    content     jsonb,
    created_at  timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat_idx ON gateway.messages (chat_id, created_at);

-- Входящие события для dialog (очередь)
CREATE TABLE IF NOT EXISTS gateway.events (
    id           bigserial PRIMARY KEY,
    account_id   bigint NOT NULL,
    chat_id      text NOT NULL,
    kind         text NOT NULL,             -- client_message | owner_message
    message_id   text UNIQUE,
    payload      jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);
CREATE INDEX IF NOT EXISTS events_pending_idx ON gateway.events (id) WHERE processed_at IS NULL;

-- Исходящие сообщения (очередь на отправку)
CREATE TABLE IF NOT EXISTS gateway.outbox (
    id          bigserial PRIMARY KEY,
    account_id  bigint NOT NULL,
    chat_id     text NOT NULL,
    text        text NOT NULL,
    send_after  timestamptz NOT NULL DEFAULT now(),
    status      text NOT NULL DEFAULT 'pending',   -- pending | sent | failed
    message_id  text,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON gateway.outbox (send_after) WHERE status = 'pending';
-- рассылки уходят и в чаты вне белого списка (решение 03.10); ответы бота — только белый список
ALTER TABLE gateway.outbox ADD COLUMN IF NOT EXISTS broadcast boolean NOT NULL DEFAULT false;
ALTER TABLE gateway.outbox ADD COLUMN IF NOT EXISTS attempts int NOT NULL DEFAULT 0;   -- сбой сети/Avito — повтор

ALTER TABLE gateway.chats ADD COLUMN IF NOT EXISTS client_id bigint;   -- Avito user_id клиента

-- Сообщение удалено в Avito (Макс удалил ответ бота и т.п.): текст оставляем — для обучения, в историю LLM не идёт
ALTER TABLE gateway.messages ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
-- «⛔ Стоп Агент»: с этого момента сообщения Макса всё равно отдаём в dialog (чат за ним и после «▶️ Старт»)
ALTER TABLE gateway.accounts ADD COLUMN IF NOT EXISTS stopped_at timestamptz;

-- Не клиенты (свои аккаунты заказчика и т.п.): бот не отвечает, рассылок и напоминаний нет; сообщения храним
CREATE TABLE IF NOT EXISTS gateway.ignored (
    client_id bigint PRIMARY KEY,
    note      text,
    added_at  timestamptz NOT NULL DEFAULT now()
);
INSERT INTO gateway.ignored (client_id, note) VALUES (390946417, 'PREMIUM MARKET — второй аккаунт заказчика')
ON CONFLICT DO NOTHING;
