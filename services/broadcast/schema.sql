CREATE SCHEMA IF NOT EXISTS broadcast;

CREATE TABLE IF NOT EXISTS broadcast.campaigns (
    id          bigserial PRIMARY KEY,
    filter_text text NOT NULL,             -- как Макс описал аудиторию
    filter      jsonb NOT NULL,            -- как это понял бот
    message     text NOT NULL,
    status      text NOT NULL DEFAULT 'active',   -- active | paused | done | cancelled
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS broadcast.recipients (
    campaign_id bigint NOT NULL REFERENCES broadcast.campaigns,
    client_id   bigint NOT NULL,
    chat_id     text NOT NULL,
    priority    int NOT NULL,              -- порядок очереди: кому давно не писали — раньше
    status      text NOT NULL DEFAULT 'queued',   -- queued | sent | cancelled
    sent_at     timestamptz,
    PRIMARY KEY (campaign_id, client_id)
);
CREATE INDEX IF NOT EXISTS recipients_client_idx ON broadcast.recipients (client_id, sent_at) WHERE status = 'sent';
CREATE INDEX IF NOT EXISTS recipients_queue_idx ON broadcast.recipients (campaign_id, priority) WHERE status = 'queued';

-- Тема рассылки: одна тема идёт по кругу — пока все подходящие клиенты не получат, повторно никому не шлём
ALTER TABLE broadcast.campaigns ADD COLUMN IF NOT EXISTS topic text NOT NULL DEFAULT 'без темы';

-- Продление: текст собирается в момент отправки по сроку покупки (закончилась / заканчивается), статус skipped —
-- к моменту отправки продлил или срок вышел давно
ALTER TABLE broadcast.campaigns ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'custom';   -- custom | renew
ALTER TABLE broadcast.recipients ADD COLUMN IF NOT EXISTS purchase_id bigint;
CREATE TABLE IF NOT EXISTS broadcast.settings (key text PRIMARY KEY, value text);
-- «переписка за сутки в любом чате клиента» — ищем чаты клиента
CREATE INDEX IF NOT EXISTS chats_client_idx ON gateway.chats (client_id);
