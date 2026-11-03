-- ════════════════════════════════════════════════════════════
-- MANGO SHOP — ПОЛНАЯ БАЗА С НУЛЯ (Supabase)
-- Supabase → SQL Editor → New query → вставь ВСЁ → Run.
-- Безопасно запускать повторно: существующие данные не удаляются.
-- В .env бота нужен ключ service_role (НЕ anon!).
-- ════════════════════════════════════════════════════════════

-- 1. ПОЛЬЗОВАТЕЛИ
CREATE TABLE IF NOT EXISTS users (
    user_id         BIGINT  PRIMARY KEY,
    username        TEXT    DEFAULT '',
    balance         FLOAT   NOT NULL DEFAULT 0,
    reg_date        TEXT    DEFAULT '',
    status          TEXT    DEFAULT 'Активен',        -- Активен | Заблокирован
    referrer_id     BIGINT  DEFAULT 0,
    purchases_count INT     DEFAULT 0,
    active_discount FLOAT   DEFAULT 0,
    lang            TEXT    DEFAULT ''                -- ru | uk (выбор языка при первом /start)
);
-- для базы, созданной раньше (безопасно запускать повторно):
ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_users_username ON users (lower(username));

-- 2. ПОКУПКИ (logins = 'lolz:<id товара на Lolz>')
CREATE TABLE IF NOT EXISTS purchases (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT,
    country_code     TEXT    DEFAULT '',              -- ключ из shop_countries.key
    phone_number     TEXT    DEFAULT '',
    logins           TEXT    DEFAULT '',
    password         TEXT    DEFAULT '',
    purchase_date    TEXT    DEFAULT '',
    guaranteed_until TEXT    DEFAULT '',
    status           TEXT    DEFAULT 'active',        -- active | refunded
    review_sent      INT     DEFAULT 0,
    price_paid       FLOAT   DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_purchases_user   ON purchases (user_id, status);
CREATE INDEX IF NOT EXISTS idx_purchases_logins ON purchases (logins);

-- 3. ПРОМОКОДЫ
CREATE TABLE IF NOT EXISTS promo_codes (
    code          TEXT PRIMARY KEY,
    discount      FLOAT   DEFAULT 0,
    max_uses      INT     DEFAULT 100,
    uses          INT     DEFAULT 0,
    active        INT     DEFAULT 1,
    type          TEXT    DEFAULT 'balance',          -- balance | discount
    country_code  TEXT,
    expires_at    TEXT,
    once_per_user INT     DEFAULT 1
);

-- 4. ИСПОЛЬЗОВАНИЯ ПРОМОКОДОВ (PK = один код на одного юзера, защита от двойного тапа)
CREATE TABLE IF NOT EXISTS promo_uses (
    code    TEXT,
    user_id BIGINT,
    used_at TEXT DEFAULT '',
    PRIMARY KEY (code, user_id)
);

-- 5. ОЖИДАЮЩИЕ ПЛАТЕЖИ (method: crypto | monobank | card — по нему фоновый контроллер знает, где проверять;
--    card: pending → review (чек у админа) → paid | rejected | canceled | timeout)
CREATE TABLE IF NOT EXISTS pending_payments (
    payment_id TEXT PRIMARY KEY,
    user_id    BIGINT,
    amount     FLOAT   DEFAULT 0,                     -- сумма в ₴, которая зачислится на баланс
    amount_uah FLOAT   DEFAULT 0,                      -- (устарело, не используется)
    status     TEXT    DEFAULT 'pending',             -- pending | review | paid | rejected | timeout | canceled
    method     TEXT    DEFAULT '',
    created_at TEXT    DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_payments (status);
-- Если таблица уже была создана раньше без колонки amount_uah — добавляем безопасно:
ALTER TABLE pending_payments ADD COLUMN IF NOT EXISTS amount_uah FLOAT DEFAULT 0;

-- 6. ИСТОРИЯ ПОПОЛНЕНИЙ
CREATE TABLE IF NOT EXISTS topup_history (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT,
    amount     FLOAT DEFAULT 0,
    method     TEXT  DEFAULT '',
    created_at TEXT  DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_topup_user ON topup_history (user_id);

-- 7. ЗАЯВКИ НА ЗАМЕНУ
CREATE TABLE IF NOT EXISTS replacement_requests (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT,
    purchase_id    BIGINT,
    account_number TEXT DEFAULT '',
    issue_text     TEXT DEFAULT '',
    issued_at      TEXT DEFAULT '',
    video_file_id  TEXT DEFAULT '',
    status         TEXT DEFAULT 'pending',            -- pending | approved | rejected
    created_at     TEXT DEFAULT '',
    resolved_at    TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_repl_purchase ON replacement_requests (purchase_id);
CREATE INDEX IF NOT EXISTS idx_repl_status   ON replacement_requests (status);

-- 8. НАСТРОЙКИ БОТА (ключ → значение)
CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- 9. ИСПОЛЬЗОВАННЫЕ ЧЕКИ (PK = один чек/скрин — один раз; code = 'tg:<file_unique_id>')
CREATE TABLE IF NOT EXISTS used_receipts (
    code    TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    amount  FLOAT,
    used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_used_receipts_user ON used_receipts (user_id);

-- 11. ИСПОЛЬЗОВАННЫЕ ТРАНЗАКЦИИ MONOBANK (PK = id транзакции — защита от зачисления ОДНОЙ
--     и той же реальной оплаты дважды двум разным пользователям / дважды одному)
CREATE TABLE IF NOT EXISTS used_mono_tx (
    tx_id       TEXT PRIMARY KEY,
    payment_id  TEXT NOT NULL,
    amount_uah  FLOAT DEFAULT 0,
    used_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_used_mono_tx_payment ON used_mono_tx (payment_id);

-- 10. СТРАНЫ «ФИЗ. АККАУНТОВ»
CREATE TABLE IF NOT EXISTS shop_countries (
    key         TEXT PRIMARY KEY,                     -- хранится в purchases.country_code
    flag        TEXT NOT NULL DEFAULT '🌍',
    name        TEXT NOT NULL,
    iso         TEXT NOT NULL,                        -- ISO-код для Lolz (US, MM, ID ...)
    phone_code  TEXT NOT NULL DEFAULT '',
    sort_order  INT  NOT NULL DEFAULT 100,            -- меньше = выше в списке
    active      INT  NOT NULL DEFAULT 1               -- 1 показывать, 0 скрыть
);
CREATE INDEX IF NOT EXISTS idx_shop_countries_order ON shop_countries (active, sort_order);

-- 12. ЗАКАЗЫ FRAGMENT (звёзды / Premium)
CREATE TABLE IF NOT EXISTS fragment_orders (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    kind       TEXT   NOT NULL,                 -- stars | premium
    quantity   INT    NOT NULL,                 -- звёзды или месяцы Premium
    target     TEXT   NOT NULL,                 -- @username получателя
    price_paid FLOAT  NOT NULL DEFAULT 0,
    status     TEXT   NOT NULL DEFAULT 'created',  -- created | done | refunded | uncertain
    note       TEXT   DEFAULT '',
    created_at TEXT   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fragment_orders_user ON fragment_orders (user_id, id DESC);


-- 13. ЗАЯВКИ НА ГОЛДУ (kind: buy | sell; status: new | in_work | done | rejected)
CREATE TABLE IF NOT EXISTS gold_requests (
    id         BIGSERIAL PRIMARY KEY,                 -- он же номер заявки
    user_id    BIGINT NOT NULL,
    username   TEXT   DEFAULT '',
    kind       TEXT   NOT NULL,
    amount     FLOAT  NOT NULL DEFAULT 0,
    comment    TEXT   DEFAULT '',
    status     TEXT   NOT NULL DEFAULT 'new',
    admin_id   BIGINT DEFAULT 0,
    created_at TEXT   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gold_status ON gold_requests (status, id DESC);
CREATE INDEX IF NOT EXISTS idx_gold_user   ON gold_requests (user_id);

-- 14. ЖУРНАЛ ПРОДАЖ — основа статистики (грязная / чистая выручка) и «Последних покупок»
CREATE TABLE IF NOT EXISTS sales (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    kind       TEXT   NOT NULL,                       -- account | stars | premium | ton
    title      TEXT   DEFAULT '',
    qty        FLOAT  DEFAULT 0,
    price      FLOAT  NOT NULL DEFAULT 0,             -- сколько заплатил клиент, ₴
    cost       FLOAT  NOT NULL DEFAULT 0,             -- себестоимость, ₴
    ref_bonus  FLOAT  NOT NULL DEFAULT 0,             -- реферальная выплата, ₴
    created_at TEXT   DEFAULT '',
    created_ts BIGINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sales_ts   ON sales (created_ts);
CREATE INDEX IF NOT EXISTS idx_sales_user ON sales (user_id);

-- 15. ПОКУПКА TON КЛИЕНТОМ (бот отправляет TON на адрес клиента)
CREATE TABLE IF NOT EXISTS ton_orders (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    address    TEXT   NOT NULL,
    amount_ton FLOAT  NOT NULL,
    price_paid FLOAT  NOT NULL DEFAULT 0,
    status     TEXT   NOT NULL DEFAULT 'created',     -- created | done | refunded | uncertain
    note       TEXT   DEFAULT '',
    created_at TEXT   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ton_orders_user ON ton_orders (user_id, id DESC);

-- 17. ОТЗЫВЫ — один отзыв на одну успешно выполненную покупку.
CREATE TABLE IF NOT EXISTS bot_reviews (
    id                 BIGSERIAL PRIMARY KEY,
    order_type         TEXT NOT NULL,                 -- account | fragment | ton | nft | manual
    order_id           BIGINT NOT NULL,
    user_id            BIGINT NOT NULL,
    customer_name      TEXT DEFAULT '',
    product_title      TEXT DEFAULT '',
    price_paid         FLOAT NOT NULL DEFAULT 0,
    rating             INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment            TEXT DEFAULT '',
    created_at         TEXT DEFAULT '',
    posted             BOOLEAN NOT NULL DEFAULT FALSE,
    channel_message_id BIGINT,
    photo_id           TEXT DEFAULT '',                -- фото отзыва (file_id), если добавил админ
    UNIQUE (order_type, order_id)
);
-- Если таблица уже создана раньше — выполните один раз:
ALTER TABLE bot_reviews ADD COLUMN IF NOT EXISTS photo_id TEXT DEFAULT '';
-- Legacy order_type='manual' may remain in older rows. New reviews come from completed purchases.
CREATE INDEX IF NOT EXISTS idx_bot_reviews_user ON bot_reviews (user_id, id DESC);

-- 18. ТИКЕТЫ ПОДДЕРЖКИ и сообщения внутри тикета.
CREATE TABLE IF NOT EXISTS support_tickets (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    username     TEXT DEFAULT '',
    customer_name TEXT DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',       -- open | answered | closed
    created_at   TEXT DEFAULT '',
    updated_at   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_support_tickets_user ON support_tickets (user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets (status, id DESC);

CREATE TABLE IF NOT EXISTS support_ticket_messages (
    id          BIGSERIAL PRIMARY KEY,
    ticket_id   BIGINT NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    sender_type TEXT NOT NULL,                       -- user | admin
    sender_id   BIGINT NOT NULL,
    message     TEXT NOT NULL DEFAULT '',
    created_at  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_support_ticket_messages_ticket ON support_ticket_messages (ticket_id, id ASC);

-- 16. TON-ДЕПОЗИТЫ (пополнение баланса переводом TON с ID в комментарии).
--     PK = хэш транзакции → одну и ту же транзакцию нельзя зачислить дважды.
--     status: claimed (занята, идёт зачисление) | credited | unmatched (нет такого ID) | failed (зачислить вручную)
CREATE TABLE IF NOT EXISTS ton_deposits (
    tx_hash    TEXT PRIMARY KEY,
    seq        BIGSERIAL,
    user_id    BIGINT DEFAULT 0,
    amount_ton FLOAT  DEFAULT 0,
    amount_uah FLOAT  DEFAULT 0,
    rate       FLOAT  DEFAULT 0,
    status     TEXT   DEFAULT 'claimed',
    sender     TEXT   DEFAULT '',
    comment    TEXT   DEFAULT '',
    created_at TEXT   DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ton_dep_user ON ton_deposits (user_id, seq DESC);

-- ────────────────────────────────────────────────────────────
-- БЕЗОПАСНОСТЬ: включаем RLS без политик. Бот ходит с ключом service_role (он RLS обходит),
-- а чужой с anon-ключом не сможет ни прочитать, ни изменить баланс/покупки через REST API.
-- ────────────────────────────────────────────────────────────
ALTER TABLE users                ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchases            ENABLE ROW LEVEL SECURITY;
ALTER TABLE promo_codes          ENABLE ROW LEVEL SECURITY;
ALTER TABLE promo_uses           ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_payments     ENABLE ROW LEVEL SECURITY;
ALTER TABLE topup_history        ENABLE ROW LEVEL SECURITY;
ALTER TABLE replacement_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_settings         ENABLE ROW LEVEL SECURITY;
ALTER TABLE used_receipts        ENABLE ROW LEVEL SECURITY;
ALTER TABLE shop_countries       ENABLE ROW LEVEL SECURITY;
ALTER TABLE fragment_orders      ENABLE ROW LEVEL SECURITY;
ALTER TABLE used_mono_tx         ENABLE ROW LEVEL SECURITY;
ALTER TABLE gold_requests        ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales                ENABLE ROW LEVEL SECURITY;
ALTER TABLE ton_orders           ENABLE ROW LEVEL SECURITY;
ALTER TABLE ton_deposits         ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_reviews           ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_tickets      ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_ticket_messages ENABLE ROW LEVEL SECURITY;

-- ────────────────────────────────────────────────────────────
-- НАЧАЛЬНЫЕ ДАННЫЕ
-- ────────────────────────────────────────────────────────────
INSERT INTO bot_settings (key, value) VALUES
    ('markup_percent',       '30'),                       -- наценка к цене Lolz, % (меняется в админке)
    ('lolz_origins',         'autoreg,self_registration'),  -- авторег + саморег; '' = без фильтра
    ('rub_uah_rate',         '0.46')                      -- 1 ₽ (цены Lolz) = N ₴; меняется в админке
ON CONFLICT (key) DO NOTHING;

INSERT INTO shop_countries (key, flag, name, iso, phone_code, sort_order) VALUES
  ('us', '🇺🇸', 'США', 'US', '+1', 10),
  ('mm', '🇲🇲', 'Мьянма', 'MM', '+95', 20),
  ('idn', '🇮🇩', 'Индонезия', 'ID', '+62', 30),
  ('ind', '🇮🇳', 'Индия', 'IN', '+91', 40),
  ('bd', '🇧🇩', 'Бангладеш', 'BD', '+880', 50),
  ('ph', '🇵🇭', 'Филиппины', 'PH', '+63', 60),
  ('vn', '🇻🇳', 'Вьетнам', 'VN', '+84', 70),
  ('br', '🇧🇷', 'Бразилия', 'BR', '+55', 80),
  ('pk', '🇵🇰', 'Пакистан', 'PK', '+92', 90),
  ('ng', '🇳🇬', 'Нигерия', 'NG', '+234', 100),
  ('eg', '🇪🇬', 'Египет', 'EG', '+20', 110),
  ('tr', '🇹🇷', 'Турция', 'TR', '+90', 120),
  ('kz', '🇰🇿', 'Казахстан', 'KZ', '+7', 130),
  ('uz', '🇺🇿', 'Узбекистан', 'UZ', '+998', 140),
  ('ca', '🇨🇦', 'Канада', 'CA', '+1', 150),
  ('mx', '🇲🇽', 'Мексика', 'MX', '+52', 160),
  ('co', '🇨🇴', 'Колумбия', 'CO', '+57', 170),
  ('ar', '🇦🇷', 'Аргентина', 'AR', '+54', 180),
  ('ke', '🇰🇪', 'Кения', 'KE', '+254', 190),
  ('th', '🇹🇭', 'Таиланд', 'TH', '+66', 200),
  ('my', '🇲🇾', 'Малайзия', 'MY', '+60', 210),
  ('kh', '🇰🇭', 'Камбоджа', 'KH', '+855', 220),
  ('np', '🇳🇵', 'Непал', 'NP', '+977', 230),
  ('et', '🇪🇹', 'Эфиопия', 'ET', '+251', 240),
  ('za', '🇿🇦', 'ЮАР', 'ZA', '+27', 250),
  ('pe', '🇵🇪', 'Перу', 'PE', '+51', 260),
  ('gb', '🇬🇧', 'Великобритания', 'GB', '+44', 270),
  ('de', '🇩🇪', 'Германия', 'DE', '+49', 280),
  ('ua', '🇺🇦', 'Украина', 'UA', '+380', 290),
  ('ru', '🇷🇺', 'Россия', 'RU', '+7', 300)
ON CONFLICT (key) DO NOTHING;

-- ════════════════════════════════════════════════════════════
-- Шпаргалка:
--   Скрыть страну:    UPDATE shop_countries SET active = 0 WHERE key = 'ru';
--   Поднять наверх:   UPDATE shop_countries SET sort_order = 1 WHERE key = 'mm';
--   Добавить страну:  INSERT INTO shop_countries (key, flag, name, iso, phone_code, sort_order)
--                     VALUES ('it', '🇮🇹', 'Италия', 'IT', '+39', 310);
--   Только авторег:   UPDATE bot_settings SET value = 'autoreg' WHERE key = 'lolz_origins';
--   Только саморег:   UPDATE bot_settings SET value = 'self_registration' WHERE key = 'lolz_origins';
-- Изменения подхватываются ботом в течение ~60 секунд.
-- ════════════════════════════════════════════════════════════

-- Если база уже создана со СТАРЫМ значением (self_registered) — можно поправить одной командой.
-- (Бот и без этого сам превращает self_registered → self_registration, но лучше держать данные чистыми.)
--   UPDATE bot_settings SET value = 'autoreg,self_registration' WHERE key = 'lolz_origins';


-- ═══════════════════════════════════════════════════════════
-- 15. NFT-ПОДАРКИ (каталог + ручная выдача по заявке админам). Выполните один раз.
-- status: available (в продаже) | reserved (заявка) | refunding (ручная проверка) | sold | hidden (снят)
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS nft_gifts (
    id            BIGSERIAL PRIMARY KEY,
    slug          TEXT NOT NULL,                  -- PlushPepe-1234 (из ссылки t.me/nft/PlushPepe-1234)
    title         TEXT NOT NULL DEFAULT '',       -- «Plush Pepe #1234»
    link          TEXT NOT NULL DEFAULT '',
    price         NUMERIC(12,2) NOT NULL DEFAULT 0,   -- цена в гривнах
    cost          NUMERIC(12,2) NOT NULL DEFAULT 0,   -- себестоимость (для статистики)
    owned_gift_id TEXT DEFAULT '',
    emoji_id      TEXT DEFAULT '',                -- custom_emoji_id модели подарка (иконка на кнопке)
    status        TEXT NOT NULL DEFAULT 'hidden',
    buyer_id      BIGINT,
    price_paid    NUMERIC(12,2) DEFAULT 0,
    note          TEXT DEFAULT '',
    created_at    TEXT,
    reserved_at   TEXT,
    sold_at       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS nft_gifts_slug_uidx ON nft_gifts (lower(slug));
CREATE INDEX IF NOT EXISTS nft_gifts_status_idx ON nft_gifts (status);
ALTER TABLE nft_gifts DISABLE ROW LEVEL SECURITY;


-- ═══════════════════════════════════════════════════════
-- 15. ЗАКАЗЫ С ОПЛАТОЙ ПО ЧЕКУ (SHOU SHOP): без баланса
--     draft → awaiting_receipt → review → paid | rejected | canceled
-- ═══════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS shop_orders (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL,
    kind          TEXT   NOT NULL,                    -- stars | premium | gram | nft
    title         TEXT   DEFAULT '',
    qty           FLOAT  DEFAULT 0,
    target        TEXT   DEFAULT '',                  -- @username получателя / адрес GRAM / id NFT
    amount        FLOAT  NOT NULL DEFAULT 0,          -- к оплате, ₴
    cost          FLOAT  NOT NULL DEFAULT 0,
    status        TEXT   NOT NULL DEFAULT 'draft',
    receipt_id    TEXT   DEFAULT '',
    receipt_photo BOOLEAN DEFAULT TRUE,
    note          TEXT   DEFAULT '',
    created_ts    BIGINT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_shop_orders_user   ON shop_orders (user_id);
CREATE INDEX IF NOT EXISTS idx_shop_orders_status ON shop_orders (status);
ALTER TABLE shop_orders ENABLE ROW LEVEL SECURITY;

-- для уже созданной базы:
ALTER TABLE nft_gifts ADD COLUMN IF NOT EXISTS emoji_id TEXT DEFAULT '';
