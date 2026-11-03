"""
keyboards.py — ВСЕ кнопки бота (ReplyKeyboard у поля ввода) + inline-хелпер.
Тексты кнопок вынесены в константы B_* / A_* — по ним работают хендлеры.

Премиум-эмодзи на кнопках (гайд emoji.wivvi.net): в тексте кнопки эмодзи НЕТ, иконка идёт в поле
icon_custom_emoji_id. Сопоставление «текст → иконка» — в _ICON / _RULES ниже. Нужен Bot API 9.4+ и Telegram Premium у владельца бота.
"""
from __future__ import annotations
import re
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
import countries as C
from config import CUR
from emoji import I

# ── Тексты кнопок (без эмодзи) ─────────────────────────────
B_CATALOG    = "Каталог"
B_TG         = "Telegram аккаунты"
B_SUPPORT    = "Поддержка"
B_PROFILE    = "Профиль"
B_TOPUP      = "Пополнить"
B_ADMIN      = "Админ-панель"
B_BACK       = "Назад"
B_HOME       = "Главное меню"
B_CANCEL     = "Отмена"
B_BUY        = "Подтвердить"
B_GETCODE    = "Получить код"
B_RESET      = "Сбросить сессию"
B_RESET_YES  = "Да, сбросить"
B_REPLACE    = "Заявка на замену"
B_MYACC      = "Мои аккаунты"
B_HISTORY    = "История пополнений"
B_PROMO      = "Промокод"
B_REFERRAL   = "Рефералы"
B_LANG       = "Язык / Мова / Language"
B_INFO       = "Информация"
B_CARD       = "Перевод на карту"
B_CRYPTO     = "Crypto Bot"
B_TON_TOPUP  = "TON (пополнение)"
B_STARS      = "Звёзды"
B_PREMIUM    = "Премиум"
B_FRAG_PAY   = "Оплатить"
B_FRAG_CUSTOM = "Другое количество"
B_FRAG_SELF  = "Себе"
# Голда
B_GOLD       = "Голда"
B_GOLD_BUY   = "Купить Голду"
B_GOLD_SELL  = "Продать Голду"
B_SKIP       = "Пропустить"
B_REVIEW_DONE    = "Готово"
B_REVIEW_NOPHOTO = "Без фото"
B_GOLD_SEND  = "Отправить заявку"
# Покупка TON
B_TON        = "Купить TON"
B_TON_PAY    = "Оплатить TON"
B_TON_CUSTOM = "Своё количество TON"

# админ
TO_ADMIN     = "В админку"
A_STATS      = "Статистика"
A_PRICES     = "Цены аккаунтов"
A_LOLZ_BAL   = "Баланс Lolz"
A_GIVE       = "Начислить баланс"
A_USER       = "Пользователь"
A_PROMO      = "Создать промокод"
A_BROADCAST  = "Рассылка"
A_BC_SEND    = "Отправить всем"
A_BAN        = "Заблокировать"
A_UNBAN      = "Разблокировать"
A_PROMO_BAL  = "На баланс"
A_PROMO_DISC = "Скидка на покупку"
A_USER_PURCHASES = "Покупки клиента"
A_PURCH_CODE      = "Код клиента"
A_PURCH_RESET     = "Сброс сессии (клиент)"
A_BACK_TO_USER    = "К клиенту"
A_FRAG_PRICES     = "Цены Stars/Premium"
A_FRAG_STATUS     = "Fragment / TON"
A_LAST_SALES      = "Последние покупки"
A_LAST_TOPUPS     = "Последние пополнения"
A_TON_SET         = "Настройки TON"
A_CARD_SET        = "Реквизиты карты"
A_COST_SET        = "Себестоимость"
A_UNMATCHED       = "TON без привязки"
A_NFT             = "NFT каталог"
A_MENU_PRODUCTS   = "Товары и цены"
A_MENU_PAYMENTS   = "Платежи"
A_MENU_SERVICE    = "Журнал и сервис"
B_NFT             = "NFT подарки"
B_GRAM            = "GRAM"
B_REVIEWS         = "Отзывы"
B_RENT            = "Аренда NFT"

# ── Иконки кнопок: точный текст → emoji-id ─────────────────
_ICON: dict[str, str] = {
    B_CATALOG: I["store"], B_TG: I["telegram"], B_SUPPORT: I["chat"], B_PROFILE: I["profile"],
    B_TOPUP: I["wallet"], B_ADMIN: I["settings"], B_BACK: I["back"], B_HOME: I["home"],
    B_CANCEL: I["cancel"], B_BUY: I["ok"], B_GETCODE: I["unlock"], B_RESET: I["repeat"],
    B_RESET_YES: I["ok"], B_REPLACE: I["doc"], B_MYACC: I["list"], B_HISTORY: I["recent"],
    B_PROMO: I["tag"], B_REFERRAL: I["people"], B_LANG: I["globe"], B_INFO: I["info"],
    B_CARD: I["atm"], B_CRYPTO: I["dollar"], B_TON_TOPUP: I["ton"],
    B_FRAG_PAY: I["ok"],
    B_FRAG_CUSTOM: I["edit"], B_FRAG_SELF: I["profile"],
    B_GOLD: I["gold"], B_GOLD_BUY: I["tag_fill"], B_GOLD_SELL: I["money_out"],
    B_SKIP: I["fwd"], B_GOLD_SEND: I["send"], B_REVIEW_DONE: I["ok"], B_REVIEW_NOPHOTO: I["fwd"],
    B_TON: I["ton"], B_TON_PAY: I["ok"], B_TON_CUSTOM: I["edit"],
    TO_ADMIN: I["back"], A_STATS: I["stats"], A_PRICES: I["tag_fill"], A_LOLZ_BAL: I["wallet"],
    A_GIVE: I["money_out"], A_USER: I["profile"], A_PROMO: I["tag"], A_BROADCAST: I["megaphone"],
    A_BC_SEND: I["send"], A_BAN: I["lock"], A_UNBAN: I["unlock"], A_PROMO_BAL: I["wallet"],
    A_PROMO_DISC: I["tag"], A_USER_PURCHASES: I["list"], A_PURCH_CODE: I["unlock"],
    A_PURCH_RESET: I["repeat"], A_BACK_TO_USER: I["back"], A_FRAG_PRICES: I["stars"],
    A_FRAG_STATUS: I["ton"], A_LAST_SALES: I["list"], A_LAST_TOPUPS: I["recent"],
    A_TON_SET: I["gear"], A_CARD_SET: I["atm"], A_COST_SET: I["analytics"],
    A_UNMATCHED: I["search"],
    A_NFT: I["gift"], A_MENU_PRODUCTS: I["store"], A_MENU_PAYMENTS: I["wallet"],
    A_MENU_SERVICE: I["gear"], B_GRAM: I["ton"], B_STARS: I["stars"], B_PREMIUM: I["premium"],
    B_NFT: I["nft"], B_REVIEWS: I["crown"], B_RENT: I["rent"],
}
# Динамические кнопки (с ценой/количеством): шаблон → иконка
_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^\d+ звёзд · "), I["stars"]),
    (re.compile(r"^1 звезда · "), I["stars"]),
    (re.compile(r"^\d+ мес · "), I["premium"]),
    (re.compile(r"^Premium \d+ мес · "), I["premium"]),
    (re.compile(r"^[\d.]+ TON · "), I["ton"]),
    (re.compile(r"^" + re.escape(B_BUY) + r" · "), I["ok"]),
    (re.compile(r"^" + re.escape(B_FRAG_PAY) + r" · "), I["ok"]),
    (re.compile(r"^" + re.escape(B_TON_PAY) + r" · "), I["ok"]),
]


def icon_for(text: str) -> str | None:
    if text in _ICON:
        return _ICON[text]
    for rx, ic in _RULES:
        if rx.match(text):
            return ic
    return None


def _btn(text: str) -> KeyboardButton:
    ic = icon_for(text)
    return KeyboardButton(text=text, icon_custom_emoji_id=ic) if ic else KeyboardButton(text=text)


def _kb(rows: list[list[str]], placeholder: str | None = None) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[_btn(t) for t in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder=placeholder,
    )


def ib(text: str, icon: str | None = None, **kw) -> InlineKeyboardButton:
    """Inline-кнопка: текст без эмодзи + премиум-иконка по имени из emoji.I (url=… или callback_data=…)."""
    if icon:
        return InlineKeyboardButton(text=text, icon_custom_emoji_id=I.get(icon, icon), **kw)
    return InlineKeyboardButton(text=text, **kw)


def kb_leave_review(order_type: str, order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        ib("Оставить отзыв", "star", callback_data=f"review:start:{order_type}:{order_id}")
    ]])


def kb_review_rating(order_type: str, order_id: int) -> InlineKeyboardMarkup:
    buttons = [
        ib(f"{n}", "stars", callback_data=f"review:rate:{order_type}:{order_id}:{n}")
        for n in range(1, 6)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:3], buttons[3:]])


def kb_admin_flow_step(skip: bool = True, done: bool = False) -> ReplyKeyboardMarkup:
    rows = [[B_REVIEW_DONE, B_SKIP]] if done else ([[B_SKIP]] if skip else [])
    rows.append([TO_ADMIN])
    return _kb(rows)


def kb_review_photo() -> ReplyKeyboardMarkup:
    """Клиент: шаг «добавьте фото/скриншоты»."""
    return _kb([[B_REVIEW_DONE], [B_REVIEW_NOPHOTO]])


def kb_review_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[ib("Без комментария", "ok", callback_data="review:skip")]])


def kb_shop_channel(bot_username: str, reviews_url: str = "") -> InlineKeyboardMarkup:
    """Клавиатура для служебного фото `s`: каталог, Stars и отзывы."""
    base = f"https://t.me/{bot_username.lstrip('@')}?start="
    # Bot API supports semantic button styles. `primary` uses the client's accent
    # colour (often purple); Telegram does not allow setting an arbitrary hex colour.
    catalog = InlineKeyboardButton(
        text="Бот Каталог", url=base + "catalog", style="primary",
        icon_custom_emoji_id=I.get("bot", ""),
    )
    stars = ib("Звёзды", "stars", url=base + "stars")
    if reviews_url:
        reviews = ib("Отзывы", "star", url=reviews_url)
        rows = [[catalog], [stars, reviews]]
    else:
        rows = [[catalog], [stars]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_review_buy(bot_username: str) -> InlineKeyboardMarkup:
    """Один CTA в канале отзывов — переход в каталог Mango Shop."""
    base = f"https://t.me/{bot_username.lstrip('@')}?start="
    return InlineKeyboardMarkup(inline_keyboard=[[
        ib("Перейти в бота", "bot", url=base + "catalog")
    ]])


def kb_ticket_create() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[ib("Создать тикет", "chat", callback_data="ticket:new")]])


def kb_ticket_actions(ticket_id: int, can_close: bool = True) -> InlineKeyboardMarkup:
    row = [ib("Ответить", "chat", callback_data=f"ticket:reply:{ticket_id}")]
    if can_close:
        row.append(ib("Закрыть тикет", "cancel", callback_data=f"ticket:close:{ticket_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


# ── Клиент ─────────────────────────────────────────────────
def kb_main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    # Дизайн SHOU SHOP: каталог NFT сверху, три товара, затем профиль / отзывы / аренда
    rows = [[B_NFT], [B_STARS, B_PREMIUM, B_GRAM], [B_PROFILE, B_REVIEWS, B_RENT]]
    if is_admin:
        rows.append([B_ADMIN])
    return _kb(rows)


def kb_catalog() -> ReplyKeyboardMarkup:
    # Stars + Premium идут рядом, NFT — отдельной строкой сразу после них,
    # TON и Голда объединены в одну строку.
    return _kb([[B_TG], [B_STARS, B_PREMIUM], [B_NFT], [B_TON, B_GOLD], [B_BACK]])


def country_button(key: str, with_price: bool = True) -> str:
    """Кнопка товара: «🇺🇸 США · 49 ₴». Без цены — только «🇺🇸 США» (для админских списков)."""
    c = C.get(key)
    base = f"{c['flag']} {c['name']}"
    return f"{base} · {C.price(key)} {CUR}" if with_price else base


def kb_countries() -> ReplyKeyboardMarkup:
    btns = [country_button(k) for k in C.for_sale()]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([B_BACK])
    return _kb(rows)


def kb_confirm(price: float) -> ReplyKeyboardMarkup:
    return _kb([[f"{B_BUY} · {price:g} {CUR}"], [B_CANCEL]])      # :g → «45 ₴», а не «45.0 ₴»


def kb_purchased() -> ReplyKeyboardMarkup:
    return _kb([[B_GETCODE], [B_RESET, B_REPLACE], [B_MYACC, B_HOME]])


def kb_reset_confirm() -> ReplyKeyboardMarkup:
    return _kb([[B_RESET_YES], [B_CANCEL]])


def kb_nofunds() -> ReplyKeyboardMarkup:
    return _kb([[B_TOPUP], [B_BACK]])


def kb_profile() -> ReplyKeyboardMarkup:
    return _kb([[B_MYACC], [B_HISTORY, B_PROMO], [B_REFERRAL, B_LANG], [B_BACK]])


def kb_accounts(labels: list[str]) -> ReplyKeyboardMarkup:
    rows = [[l] for l in labels]
    rows.append([B_BACK])
    return _kb(rows)


def kb_back_only() -> ReplyKeyboardMarkup:
    return _kb([[B_BACK]])


def kb_cancel_only() -> ReplyKeyboardMarkup:
    return _kb([[B_CANCEL]])


# ── Пополнение ─────────────────────────────────────────────
def kb_topup_methods(card: bool, crypto: bool, ton: bool) -> ReplyKeyboardMarkup:
    first = [b for b, on in ((B_CARD, card), (B_CRYPTO, crypto)) if on]
    rows = [first] if first else []
    if ton:
        rows.append([B_TON_TOPUP])
    rows.append([B_BACK])
    return _kb(rows)


def kb_topup_amounts() -> ReplyKeyboardMarkup:
    return _kb([[f"100 {CUR}", f"200 {CUR}"], [f"500 {CUR}", f"1000 {CUR}"], [B_BACK]],
               placeholder="Или введите сумму")


# ── Голда ──────────────────────────────────────────────────
def kb_gold_menu() -> ReplyKeyboardMarkup:
    return _kb([[B_GOLD_BUY, B_GOLD_SELL], [B_BACK]])


def kb_gold_comment() -> ReplyKeyboardMarkup:
    return _kb([[B_SKIP], [B_CANCEL]], placeholder="Комментарий к заявке")


def kb_gold_confirm() -> ReplyKeyboardMarkup:
    return _kb([[B_GOLD_SEND], [B_CANCEL]])


# ── Покупка TON ────────────────────────────────────────────
TON_PACKS = (1, 5, 10, 25)


def kb_ton_packs(price_fn, packs=TON_PACKS) -> ReplyKeyboardMarkup:
    btns = [f"{n:g} TON · {price_fn(n)} {CUR}" for n in packs]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([B_TON_CUSTOM])
    rows.append([B_BACK])
    return _kb(rows)


def kb_ton_address() -> ReplyKeyboardMarkup:
    return _kb([[B_BACK]], placeholder="Ваш TON-адрес (UQ… / EQ…)")


def kb_ton_confirm(price: float) -> ReplyKeyboardMarkup:
    return _kb([[f"{B_TON_PAY} · {price:g} {CUR}"], [B_CANCEL]])


# ── Админ ──────────────────────────────────────────────────
def kb_admin_menu() -> ReplyKeyboardMarkup:
    """Короткая главная админка: только ежедневные действия и входы в разделы."""
    return _kb([
        [A_STATS, A_NFT],
        [A_USER, A_GIVE],
        [A_PROMO, A_BROADCAST],
        [A_MENU_PRODUCTS],
        [A_MENU_PAYMENTS],
        [A_MENU_SERVICE],
        [B_HOME],
    ], placeholder="Выберите раздел админ-панели")


def kb_admin_products_menu() -> ReplyKeyboardMarkup:
    return _kb([[A_PRICES, A_FRAG_PRICES], [TO_ADMIN]], placeholder="Цены товаров")


def kb_admin_payments_menu() -> ReplyKeyboardMarkup:
    return _kb([[A_TON_SET, A_CARD_SET], [A_UNMATCHED], [TO_ADMIN]], placeholder="Платёжные настройки")


def kb_admin_service_menu() -> ReplyKeyboardMarkup:
    return _kb([[A_LAST_SALES, A_LAST_TOPUPS], [A_LOLZ_BAL, A_FRAG_STATUS], [A_COST_SET], [TO_ADMIN]],
               placeholder="Журнал и диагностика")


def kb_admin_prices() -> ReplyKeyboardMarkup:
    """Все активные страны: «🇺🇸 США · 49 ₴» или «🇧🇩 Бангладеш · —» (цена не задана → клиентам не видна)."""
    btns = []
    for k in C.all_countries():
        p = C.price(k)
        btns.append(f"{country_button(k, False)} · {p} {CUR}" if p else f"{country_button(k, False)} · —")
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([TO_ADMIN])
    return _kb(rows)


def kb_admin_back() -> ReplyKeyboardMarkup:
    return _kb([[TO_ADMIN]])


def kb_admin_user() -> ReplyKeyboardMarkup:
    return _kb([[A_USER_PURCHASES], [A_BAN, A_UNBAN], [TO_ADMIN]])


def kb_admin_user_purchases(labels: list[str]) -> ReplyKeyboardMarkup:
    rows = [[l] for l in labels] if labels else []
    rows.append([A_BACK_TO_USER])
    return _kb(rows)


def kb_admin_purchase_detail() -> ReplyKeyboardMarkup:
    return _kb([[A_PURCH_CODE, A_PURCH_RESET], [A_BACK_TO_USER]])


def kb_admin_promo_type() -> ReplyKeyboardMarkup:
    return _kb([[A_PROMO_BAL, A_PROMO_DISC], [TO_ADMIN]])


def kb_admin_broadcast_confirm() -> ReplyKeyboardMarkup:
    return _kb([[A_BC_SEND], [TO_ADMIN]])


def kb_admin_settings(labels: list[str]) -> ReplyKeyboardMarkup:
    rows = [[l] for l in labels]
    rows.append([TO_ADMIN])
    return _kb(rows)


# ── Fragment: звёзды / Premium ─────────────────────────────
STAR_PACKS = (50, 100, 250, 500, 1000, 2500)


def kb_stars(price_fn) -> ReplyKeyboardMarkup:
    btns = [f"{n} звёзд · {price_fn(n)} {CUR}" for n in STAR_PACKS]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([B_FRAG_CUSTOM])
    rows.append([B_BACK])
    return _kb(rows)


def kb_premium(prices: dict[int, int]) -> ReplyKeyboardMarkup:
    rows = [[f"{m} мес · {p} {CUR}"] for m, p in prices.items() if p > 0]
    rows.append([B_BACK])
    return _kb(rows)


def kb_frag_username(own_username: str | None) -> ReplyKeyboardMarkup:
    rows = [[B_FRAG_SELF]] if own_username else []
    rows.append([B_BACK])
    return _kb(rows, placeholder="@username получателя")


def kb_frag_confirm(price: int) -> ReplyKeyboardMarkup:
    return _kb([[f"{B_FRAG_PAY} · {price} {CUR}"], [B_CANCEL]])


def kb_admin_frag_prices(stars_price: float, premium: dict[int, int]) -> ReplyKeyboardMarkup:
    rows = [[f"1 звезда · {stars_price:g} {CUR}" if stars_price else "1 звезда · —"]]
    for m, p in premium.items():
        rows.append([f"Premium {m} мес · {p} {CUR}" if p else f"Premium {m} мес · —"])
    rows.append([TO_ADMIN])
    return _kb(rows)
