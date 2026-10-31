CREATE SCHEMA IF NOT EXISTS catalog;

-- Базовые услуги (акции/подарки/розыгрыши сюда не входят — они в рассылках)
CREATE TABLE IF NOT EXISTS catalog.products (
    code     text PRIMARY KEY,
    title    text NOT NULL,             -- заголовок раздела в прайсе
    keywords text[] NOT NULL,           -- по ним узнаём раздел в присланном прайсе
    sort     int NOT NULL DEFAULT 0
);
-- по запросу: в приветствии и {прайс} не показываем, бот называет, только если клиент сам спросил
ALTER TABLE catalog.products ADD COLUMN IF NOT EXISTS on_request boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS catalog.prices (
    product    text REFERENCES catalog.products,
    months     int NOT NULL,
    price_rub  int NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text,
    PRIMARY KEY (product, months)
);

CREATE TABLE IF NOT EXISTS catalog.price_history (
    id         bigserial PRIMARY KEY,
    product    text NOT NULL,
    months     int NOT NULL,
    old_rub    int,
    new_rub    int,                     -- NULL = позиция удалена
    changed_at timestamptz NOT NULL DEFAULT now(),
    changed_by text
);

-- Общие правила скидок; пока одна — 100 ₽ новым клиентам за отзыв
CREATE TABLE IF NOT EXISTS catalog.discounts (
    code       text PRIMARY KEY,
    title      text NOT NULL,
    amount_rub int NOT NULL,
    condition  text NOT NULL,           -- new_client_review | ...
    active     boolean NOT NULL DEFAULT true
);

INSERT INTO catalog.products (code, title, keywords, sort) VALUES
    ('console', '🎮 XBOX ULTIMATE дом консоль (играете со своего аккаунта)', '{консол,xbox,дом}', 1),
    ('pc', '💻 ПК', '{пк,pc,компьютер}', 2)
ON CONFLICT DO NOTHING;
INSERT INTO catalog.products (code, title, keywords, sort, on_request) VALUES
    ('personal', '🎮 XBOX Game Pass Ultimate на Ваш аккаунт', '{ваш аккаунт,личный аккаунт,личн}', 3, true)
ON CONFLICT DO NOTHING;

-- Прайс Макса от 28.09.2026
INSERT INTO catalog.prices (product, months, price_rub, updated_by)
SELECT p, m, r, 'seed' FROM (VALUES
    ('console', 2, 1090), ('console', 3, 1590), ('console', 4, 1790), ('console', 6, 2290),
    ('console', 8, 2890), ('console', 10, 3490), ('console', 12, 4290), ('console', 13, 4590),
    ('console', 14, 5090), ('console', 15, 5390), ('console', 20, 6890),
    ('pc', 1, 400), ('pc', 4, 1200), ('pc', 10, 1800), ('pc', 20, 3900)
) AS v(p, m, r)
WHERE NOT EXISTS (SELECT 1 FROM catalog.prices);

INSERT INTO catalog.discounts (code, title, amount_rub, condition) VALUES
    ('review_new', 'Скидка новым клиентам за отзыв', 100, 'new_client_review')
ON CONFLICT DO NOTHING;

-- Прайс на личный аккаунт клиента, от 06.10.2026
INSERT INTO catalog.prices (product, months, price_rub, updated_by)
SELECT 'personal', m, r, 'seed' FROM (VALUES
    (4, 4190), (6, 4890), (9, 6490), (11, 7690), (12, 8490), (13, 8790), (14, 9690)
) AS v(m, r)
WHERE NOT EXISTS (SELECT 1 FROM catalog.prices WHERE product = 'personal');

-- Подарок (заказчик 06.10.2026): игра в подарок при подписке через доп. аккаунт от 10 мес, от 14 мес — две.
-- В прайсе — «🎁» / «2 🎁» у срока и строка под разделом. Через VK пока не правится (только цены).
ALTER TABLE catalog.products ADD COLUMN IF NOT EXISTS note text;                   -- строка под разделом
ALTER TABLE catalog.prices ADD COLUMN IF NOT EXISTS gifts int NOT NULL DEFAULT 0;  -- игр в подарок
UPDATE catalog.prices SET gifts = CASE WHEN months >= 14 THEN 2 ELSE 1 END
WHERE product = 'console' AND months >= 10
  AND EXISTS (SELECT 1 FROM catalog.products WHERE code = 'console' AND note IS NULL);
UPDATE catalog.products SET note = '🎁 Игра в подарок от 10 мес' WHERE code = 'console' AND note IS NULL;

-- Тексты владельца, которые бот отправляет как есть (код → текст). gift_list — список игр в подарок на выбор
-- (шаблон из чатов, последний раз 05.10.2026); бот предлагает его, Макс потом корректирует в чате.
CREATE TABLE IF NOT EXISTS catalog.templates (
    code       text PRIMARY KEY,
    text       text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text
);
INSERT INTO catalog.templates (code, text, updated_by) VALUES ('gift_list', '🎇 Выберите из списка игру в подарок🎇

💎OUTLAST: BUNDLE OF TERROR
💎Terraria
💎Batman: Arkham Knight
💎PAYDAY 2: CRIMEWAVE EDITION
💎OUTLAST 2
💎Titan Quest
💎Sleeping Dogs Definitive Edition
💎 Metro 2033 Redux
💎FAR CRY 4 GOLD EDITION
💎WATCH DOGS COMPLETE EDITION
💎TOMB RAIDER: DEFINITIVE EDITION
💎VALIANT HEARTS: THE GREAT WAR
💎MONOPOLY FAMILY FUN PACK
💎UNRAVEL TWO
💎INJUSTICE 2
💎STAR WARS BATTLEFRONT ULTIMATE EDITION
💎BLACKSAD: UNDER THE SKIN
💎BATTLEFIELD V DEFINITIVE EDITION
💎NEED FOR SPEED RIVALS
💎THE LEGO NINJAGO
💎PLANTS VS. ZOMBIES GARDEN WARFARE 2
💎DEUS EX: MANKIND DIVIDED
💎BATTLEFIELD 1
💎MURDERED: SOUL SUSPECT

🔸 Игры активируются на ваш аккаунт и остаются навсегда.
🔸 Выдача  в течении 5–10 минут.
🔸 Гарантия и помощь при активации.', 'seed: шаблон из чатов') ON CONFLICT DO NOTHING;

-- Уточнение заказчика 06.10: от 14 мес — не две игры, а игра из списка + участие в розыгрыше топовой игры.
-- Игры для розыгрыша — шаблон raffle_games (ведётся в VK), подставляется в строку под прайсом вместо {розыгрыш}.
-- О розыгрыше бот только сообщает — без учёта участников и проведения.
ALTER TABLE catalog.prices ADD COLUMN IF NOT EXISTS raffle boolean NOT NULL DEFAULT false;
UPDATE catalog.prices SET gifts = 1, raffle = true WHERE gifts = 2
  AND NOT EXISTS (SELECT 1 FROM catalog.templates WHERE code = 'raffle_games');
UPDATE catalog.products SET note = '🎁 Игра в подарок от 10 мес; от 14 мес — ещё и участие в розыгрыше: {розыгрыш}'
WHERE code = 'console' AND note = '🎁 Игра в подарок от 10 мес';
INSERT INTO catalog.templates (code, text, updated_by) VALUES ('raffle_games', 'GTA 6
EA Sports FC 27', 'seed: заказчик 06.10') ON CONFLICT DO NOTHING;
