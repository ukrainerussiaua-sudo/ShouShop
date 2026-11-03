"""
handlers/shop.py — меню, навигация, подбор и покупка аккаунтов (Lolz), код, профиль.
Вся навигация — через ReplyKeyboard (кнопки в меню под полем ввода).
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, User
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command

import i18n
import lolz
import countries as C
from config import (
    ADMIN_IDS, SUPPORT_ACCOUNT, GUARANTEE_HOURS, MAX_BUY_TRIES,
    LOLZ_RUB_TO_UAH, REF_BONUS, CUR,
)
from database import db
import keyboards as K
from keyboards import (
    B_CATALOG, B_TG, B_SUPPORT, B_PROFILE, B_BACK, B_HOME, B_CANCEL, B_BUY, B_GETCODE, B_RESET,
    B_RESET_YES, B_MYACC, B_HISTORY, B_REFERRAL, B_INFO, B_LANG,
)
import texts as T
from utils import (
    SHOP_PHOTO, PROFILE_PHOTO, INFO_PHOTO, SUPPORT_PHOTO, TELEGRAM_PHOTO, CATALOG_PHOTO,
    answer_screen, run_with_dots, is_subscribed, notify_admins, user_label, money,
    send_lang_chooser, send_subscribe_screen,
)

router = Router()

# Навигация «Назад»: экран → родитель
PARENT = {
    "catalog": "main", "telegram": "catalog", "card": "telegram", "support": "main",
    "profile": "main", "accounts": "profile", "acc": "accounts",
    "info": "main", "history": "profile", "referral": "profile",
    "reset": "acc", "purchased": "main",
    "stars": "catalog", "premium": "catalog", "frag_user": "catalog", "frag_confirm": "catalog",
    "gold": "catalog", "gold_amount": "gold", "gold_comment": "gold", "gold_confirm": "gold",
    "nft": "catalog",
    "ton": "catalog", "ton_addr": "ton", "ton_confirm": "ton",
    "topup": "main", "topup_amount": "topup", "topup_receipt": "topup",
}

_user_locks: dict[int, asyncio.Lock] = {}


def _lock(uid: int) -> asyncio.Lock:
    return _user_locks.setdefault(uid, asyncio.Lock())


def _is_adm(uid: int) -> bool:
    return uid in ADMIN_IDS


def _country_by_button(text: str) -> str | None:
    """Кнопка «🇺🇸 США · 49 ₴» → ключ страны. Цену в тексте не сверяем (её могли поменять в админке)."""
    if not isinstance(text, str):          # фото/документ/стикер: у сообщения нет текста
        return None
    for k in C.for_sale():
        base = K.country_button(k, with_price=False)
        if text == base or text.startswith(base + " · "):
            return k
    return None


def _rub_rate() -> float:
    """Сколько ₴ стоит 1 ₽ (цены Lolz — в рублях). Настраивается в админке («Себестоимость»)."""
    r = db.get_float("rub_uah_rate", LOLZ_RUB_TO_UAH)
    return r if r > 0 else LOLZ_RUB_TO_UAH


def _cost_uah(it: dict) -> float:
    """Себестоимость аккаунта в ₴ = цена Lolz (₽) × курс."""
    return round(lolz.item_price(it) * _rub_rate(), 2)


async def _find_candidates(ck: str) -> list[dict]:
    """Аккаунты страны на Lolz (новорег, без спамблока), от САМОГО ДЕШЁВОГО к дорогим."""
    iso = C.get(ck)["iso"]
    origins = db.get_lolz_origins()
    items, _ = await lolz.search_telegram_page(iso, 1, origins)
    if not items and origins:
        # Строгий поиск (origin[] на стороне Lolz) пуст — пробуем без origin[], происхождение проверим сами.
        lolz.log.warning("search iso=%s: пусто с origin[]=%s → пробую без origin[]", iso, origins)
        items, _ = await lolz.search_telegram_page(iso, 1, origins, relax_origin=True)
    items = [it for it in items if lolz.item_price(it) > 0]
    return sorted(items, key=lolz.item_price)


# ═════════════════════════════════════════════════════════
# Экраны
# ═════════════════════════════════════════════════════════
async def show_main(msg: Message, state: FSMContext):
    await state.set_state(None)
    await state.update_data(screen="main", card=None)
    await answer_screen(msg, T.txt_shop(), K.kb_main_menu(_is_adm(msg.chat.id)), SHOP_PHOTO())


async def show_catalog(msg: Message, state: FSMContext):
    await state.set_state(None)
    await state.update_data(screen="catalog", card=None)
    await answer_screen(msg, T.txt_catalog(), K.kb_catalog(), CATALOG_PHOTO())


async def show_countries(msg: Message, state: FSMContext):
    """Категория «Telegram аккаунты»: страны кнопками (цена — на кнопке)."""
    await state.set_state(None)
    await state.update_data(screen="telegram", card=None)
    C.for_sale(force=True)                      # свежие цены, если админ только что менял
    text = T.txt_countries() if C.for_sale() else T.txt_no_products()
    await answer_screen(msg, text, K.kb_countries(), TELEGRAM_PHOTO())


async def show_support(msg: Message, state: FSMContext):
    await state.set_state(None)
    await state.update_data(screen="support")
    await answer_screen(msg, T.txt_support(), K.kb_back_only(), SUPPORT_PHOTO())
    await msg.answer("Нужна помощь? Создайте тикет — переписка с поддержкой сохранится в боте.",
                     reply_markup=K.kb_ticket_create())


async def show_card(msg: Message, state: FSMContext, ck: str):
    """Подтверждение покупки: «Вы действительно хотите купить аккаунт ... за N ₴?»."""
    price = C.price(ck)
    ci = C.get(ck)
    if not ci or price <= 0:
        await msg.answer("❌ Этот товар сейчас недоступен.")
        return await show_countries(msg, state)
    disc = min(db.get_active_discount(msg.chat.id), price)
    await state.set_state(None)
    await state.update_data(screen="card", card={"country": ck, "price": price})
    await answer_screen(msg, T.txt_confirm(ci, price, disc), K.kb_confirm(price - disc))


async def show_profile(msg: Message, state: FSMContext):
    await state.set_state(None)
    await state.update_data(screen="profile")
    u = db.get_user(msg.chat.id, msg.from_user.username or "")
    u["user_id"] = msg.chat.id
    await answer_screen(msg, T.txt_profile(u), K.kb_profile(), PROFILE_PHOTO())


async def show_accounts(msg: Message, state: FSMContext):
    await state.set_state(None)
    purchases = db.get_purchases(msg.chat.id)
    labels, mapping = [], {}
    for n, p in enumerate(purchases[:30], 1):
        ci = C.get(p["country_code"]) or {"flag": "🌍"}
        label = f'{n}. {ci["flag"]} {p["phone_number"]} · {str(p["purchase_date"]).split()[0]}'
        labels.append(label)
        mapping[label] = p["id"]
    await state.update_data(screen="accounts", accs=mapping)
    await answer_screen(msg, T.txt_my_accounts(purchases), K.kb_accounts(labels))


async def show_account(msg: Message, state: FSMContext, pid: int):
    p = db.get_purchase(pid)
    if not p or p["user_id"] != msg.chat.id:
        return await msg.answer("❌ Аккаунт не найден")
    await state.set_state(None)
    await state.update_data(screen="acc", cur_pid=pid)
    await answer_screen(msg, T.txt_acc_detail(p), K.kb_purchased())


async def go_back(msg: Message, state: FSMContext):
    data = await state.get_data()
    screen = data.get("screen", "main")
    parent = PARENT.get(screen, "main")
    if parent == "catalog":
        return await show_catalog(msg, state)
    if parent == "telegram":
        return await show_countries(msg, state)
    if parent == "profile":
        return await show_profile(msg, state)
    if parent == "accounts":
        return await show_accounts(msg, state)
    if parent == "acc" and data.get("cur_pid"):
        return await show_account(msg, state, data["cur_pid"])
    if parent == "gold":
        from handlers.gold import show_gold
        return await show_gold(msg, state)
    if parent == "ton":
        from handlers.ton_shop import show_ton
        return await show_ton(msg, state)
    if parent == "topup":
        from handlers.topup import show_topup_menu
        return await show_topup_menu(msg, state)
    return await show_main(msg, state)


# ═════════════════════════════════════════════════════════
# /start + обязательная подписка (проверка на КАЖДОЕ действие — в main.SubscriptionMiddleware)
# ═════════════════════════════════════════════════════════
def _register(user: User, ref: int | None):
    """Создаёт пользователя; реферал привязывается только новому."""
    is_new = db.find_user_by_id(user.id) is None
    db.get_user(user.id, user.username or "")
    if is_new:
        i18n.set_lang(user.id, i18n.lang_of(user.id) or "ru")      # язык, выбранный на первом экране, записываем в БД
    if is_new and ref and ref != user.id and db.find_user_by_id(ref):
        db.set_referrer(user.id, ref)      # бонус — только за ПОКУПКУ приглашённого


async def _finish_start(msg: Message, state: FSMContext, payload: str | None = None):
    """После /start открывает нужный раздел по ссылке из канала (NFT-карточка, звёзды, премиум)."""
    from handlers import store
    key = (payload or "").strip().casefold()
    if key.startswith("nft_") and key[4:].isdigit():
        await show_main(msg, state)
        return await store.show_nft_card(msg, state, int(key[4:]))
    await show_main(msg, state)
    if key in {"catalog", "nft"}:
        return await store.on_nft(msg, state)
    if key == "stars":
        return await store.on_stars(msg, state)
    if key == "premium":
        return await store.on_premium(msg, state)
    if key == "gram":
        return await store.on_gram(msg, state)


@router.message(CommandStart())
async def on_start(msg: Message, state: FSMContext):
    await state.clear()
    args = (msg.text or "").split()
    payload = args[1] if len(args) > 1 else None
    ref = int(payload) if payload and payload.isdigit() else None
    _register(msg.from_user, ref)
    await _finish_start(msg, state, payload)


@router.message(F.text == B_LANG)
async def on_lang_menu(msg: Message):
    await send_lang_chooser(msg)


@router.callback_query(F.data.startswith("lang:"))
async def on_lang(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """Выбор языка. Новый пользователь → дальше проверка подписки на канал → регистрация → меню. Старый — сразу меню."""
    lang = cb.data.split(":", 1)[1]
    if lang not in i18n.LANGS:
        return await cb.answer()
    uid = cb.from_user.id
    existed = db.find_user_by_id(uid) is not None
    i18n.set_lang(uid, lang)
    await cb.answer("✅")
    msg = cb.message
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception:
        pass
    data = await state.get_data()
    payload = data.get("pending_start")
    if existed:
        await state.update_data(pending_start=None, pending_ref=None)
        return await _finish_start(msg, state, payload)
    ref = data.get("pending_ref")
    if uid in ADMIN_IDS or await is_subscribed(bot, uid, use_cache=False):
        await state.update_data(pending_start=None, pending_ref=None)
        _register(cb.from_user, ref)
        return await _finish_start(msg, state, payload)
    await send_subscribe_screen(msg, cb.from_user.first_name or "")      # проверка на канал


@router.callback_query(F.data == "check_sub")
async def on_check_sub(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not await is_subscribed(bot, cb.from_user.id, use_cache=False):
        return await cb.answer("❌ Вы ещё не подписались на канал. Подпишитесь и нажмите «Проверить подписку».", show_alert=True)
    await cb.answer("✅ Спасибо!")
    data = await state.get_data()
    ref = data.get("pending_ref")
    payload = data.get("pending_start")
    await state.update_data(pending_start=None, pending_ref=None)
    _register(cb.from_user, ref)
    msg = cb.message
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception:
        pass
    await _finish_start(msg, state, payload)


@router.message(Command("menu"))
@router.message(F.text == B_HOME)
async def on_home(msg: Message, state: FSMContext):
    await show_main(msg, state)


@router.message(F.text.in_({B_BACK, B_CANCEL}))
async def on_back(msg: Message, state: FSMContext):
    await go_back(msg, state)


# ═════════════════════════════════════════════════════════
# Каталог → Telegram аккаунты → страна → подтверждение → покупка
# ═════════════════════════════════════════════════════════
@router.message(F.text == B_CATALOG)
async def on_catalog(msg: Message, state: FSMContext):
    await show_catalog(msg, state)


@router.message(F.text == B_TG)
async def on_telegram(msg: Message, state: FSMContext):
    await show_countries(msg, state)


@router.message(F.text == B_SUPPORT)
async def on_support(msg: Message, state: FSMContext):
    await show_support(msg, state)


@router.message(F.text.func(lambda t: _country_by_button(t) is not None))
async def on_country(msg: Message, state: FSMContext):
    await show_card(msg, state, _country_by_button(msg.text))


@router.message(F.text.startswith(B_BUY))
async def on_buy(msg: Message, state: FSMContext, bot: Bot):
    """
    Подтвердил → бот ищет на Lolz САМЫЙ ДЕШЁВЫЙ аккаунт страны (новорег, без спамблока):
      • аккаунтов нет вообще                          → «Аккаунтов нету»
      • самый дешёвый дороже, чем мы за него берём    → «Нет аккаунта»
      • иначе покупаем самый дешёвый (если его только что разобрали — пробуем следующий).
    Деньги списываются только когда аккаунт найден; не купили → возврат.
    """
    uid = msg.from_user.id
    async with _lock(uid):
        data = await state.get_data()
        card = data.get("card")
        if not card:
            await msg.answer("Сначала выберите товар.")
            return await show_countries(msg, state)

        ck = card["country"]
        ci = C.get(ck)
        price = C.price(ck)
        if not ci or price <= 0:
            await msg.answer("❌ Этот товар сейчас недоступен.")
            return await show_countries(msg, state)
        if price != card["price"]:               # админ поменял цену, пока клиент смотрел карточку
            await msg.answer("ℹ️ Цена изменилась — проверьте и подтвердите ещё раз.")
            return await show_card(msg, state, ck)

        disc = min(db.get_active_discount(uid), price)
        final = price - disc                     # столько реально заплатит клиент
        bal = db.balance(uid)
        if bal < final:
            await answer_screen(msg, T.txt_nofunds(bal, final), K.kb_nofunds())
            return

        # Закрываем карточку, чтобы двойной тап не купил дважды
        await state.update_data(card=None)

        found, err = await run_with_dots(msg, "🔎 Ищу аккаунт", _find_candidates(ck))
        if err is not None:
            print(f"[search] {err!r}")
            await msg.answer("⚠️ Не удалось получить список аккаунтов. Попробуйте ещё раз.")
            return await show_countries(msg, state)
        if not found:
            await msg.answer(T.TXT_NO_ACCOUNTS, parse_mode=ParseMode.HTML)
            return await show_countries(msg, state)
        # Сравниваем с тем, что заплатит клиент: иначе с промокодом ушли бы в минус
        cands = [it for it in found if _cost_uah(it) <= final]
        if not cands:                            # самый дешёвый дороже нашей цены
            await msg.answer(T.TXT_NO_ACCOUNT, parse_mode=ParseMode.HTML)
            return await show_countries(msg, state)

        if not db.try_spend(uid, final):          # атомарное списание: баланс не уйдёт в минус
            await answer_screen(msg, T.txt_nofunds(db.balance(uid), final), K.kb_nofunds())
            return

        bought, last_err = None, None
        for it in cands[:MAX_BUY_TRIES]:
            iid = lolz.item_id(it)
            res, err = await run_with_dots(msg, "⏳ Покупаю", lolz.fast_buy(iid, lolz.item_price(it)))
            if err is None:
                bought = (iid, res, _cost_uah(it))
                break
            if isinstance(err, lolz.LolzError):  # однозначный отказ (уже продан и т.п.) → следующий
                last_err = err
                continue
            # Результат неизвестен (обрыв/таймаут) — деньги НЕ возвращаем автоматически
            await msg.answer(
                "⚠️ Не получили подтверждение покупки. Деньги заморожены, "
                f"поддержка проверит заказ вручную: {SUPPORT_ACCOUNT}")
            await _alert_admins(bot, f"⚠️ Неопределённая покупка Lolz\nuser={uid}\nitem={iid}\n"
                                     f"сумма={final} {CUR}\nошибка={err!r}\n"
                                     "Проверьте покупки на Lolz и верните баланс при необходимости.")
            return await show_main(msg, state)

        if bought is None:
            db.add_balance(uid, final)           # все отказы однозначные — возвращаем деньги
            await msg.answer("❌ Не удалось купить аккаунт — его только что забрали.\n"
                             "Деньги возвращены на баланс, попробуйте ещё раз.")
            await _alert_admins(bot, f"⚠️ Lolz отказал в покупке (user={uid}, {ck}, {final} {CUR})\n"
                                     f"Последняя ошибка: {last_err}\n"
                                     "Если это «недостаточно средств» — пополните баланс Lolz.")
            return await show_countries(msg, state)

        iid, res, cost_uah = bought
        phone = lolz.item_phone(res)
        if not phone:
            it2, _ = await run_with_dots(msg, "📱 Получаю номер", lolz.get_item(iid))
            phone = lolz.item_phone(it2 or {})
        phone = phone or f"(см. Lolz #{iid})"

        try:
            pid = db.add_purchase(uid, ck, phone, f"lolz:{iid}", "", price_paid=final)
        except Exception as e:
            await _alert_admins(bot, f"🚨 Куплено на Lolz, но не записано в БД!\nuser={uid}\nitem={iid}\n"
                                     f"phone={phone}\nсумма={final}\n{e!r}")
            await msg.answer("⚠️ Аккаунт куплен, но возникла ошибка записи. Поддержка уже уведомлена.")
            return await show_main(msg, state)

        if disc > 0:
            db.set_active_discount(uid, 0)
        u = db.find_user_by_id(uid) or {}
        ref_paid = 0.0
        if u.get("referrer_id") and REF_BONUS > 0:
            db.add_balance(u["referrer_id"], REF_BONUS)
            ref_paid = REF_BONUS

        # журнал продаж (грязная/чистая выручка) + уведомление админам
        db.add_sale(uid, "account", f"{ci['name']} {phone}", 1, final, cost_uah, ref_paid)
        await notify_admins(bot, f"🛒 <b>Продажа: аккаунт</b>\n\n👤 {user_label(uid, u.get('username'), msg.from_user.first_name)}\n"
                                 f"{ci['flag']} {ci['name']} · <code>{phone}</code>\n"
                                 f"💰 {money(final)} · себестоимость {money(cost_uah)} · "
                                 f"прибыль {money(final - cost_uah - ref_paid)}")

        guar = (datetime.now() + timedelta(hours=GUARANTEE_HOURS)).strftime("%d.%m.%Y %H:%M")
        await state.update_data(screen="purchased", cur_pid=pid)
        await answer_screen(msg, T.txt_purchase_ready(ci, phone, guar), K.kb_purchased())
        await msg.answer("⭐️ Покупка завершена! Оцените товар — выберите от 1 до 5 звёзд.",
                         reply_markup=K.kb_leave_review("account", pid))


async def _alert_admins(bot: Bot, text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            pass


# ═════════════════════════════════════════════════════════
# Код и сброс сессий
# ═════════════════════════════════════════════════════════
def _owned_purchase(data: dict, uid: int) -> dict | None:
    pid = data.get("cur_pid")
    p = db.get_purchase(pid) if pid else None
    return p if p and p["user_id"] == uid else None


def _lolz_id(p: dict) -> int | None:
    s = str(p.get("logins") or "")
    return int(s[5:]) if s.startswith("lolz:") and s[5:].isdigit() else None


async def _poll_code(iid: int, tries: int = 4) -> str | None:
    for i in range(tries):
        try:
            code = await lolz.get_login_code(iid)
        except lolz.LolzError:
            code = None
        if code:
            return code
        if i < tries - 1:
            await asyncio.sleep(3)
    return None


@router.message(F.text == B_GETCODE)
async def on_get_code(msg: Message, state: FSMContext):
    data = await state.get_data()
    p = _owned_purchase(data, msg.from_user.id)
    if not p:
        await msg.answer("Сначала выберите аккаунт в «Мои аккаунты».")
        return await show_accounts(msg, state)
    iid = _lolz_id(p)
    if not iid:
        return await msg.answer("Это старый аккаунт — обратитесь в поддержку за кодом.")

    code, err = await run_with_dots(msg, "🔎 Ищу код", _poll_code(iid))
    if err is not None or not code:
        return await msg.answer(
            "❌ Код ещё не пришёл.\nЗапросите код входа в приложении Telegram "
            "(введите номер) и нажмите «🔑 Получить код» ещё раз.",
            reply_markup=K.kb_purchased())

    ci = C.get(p["country_code"]) or {"flag": "🌍", "name": "?", "code": ""}
    await answer_screen(msg, T.txt_purchase_code(ci, p["phone_number"], code, p["guaranteed_until"]),
                        K.kb_purchased())


@router.message(F.text == B_RESET)
async def on_reset(msg: Message, state: FSMContext):
    data = await state.get_data()
    if not _owned_purchase(data, msg.from_user.id):
        return await show_accounts(msg, state)
    await state.update_data(screen="reset")
    await answer_screen(msg, "🔄 <b>Сбросить сессии?</b>\n\nВсе активные входы в аккаунт будут завершены. "
                             "Делайте это после того, как вошли сами.", K.kb_reset_confirm())


@router.message(F.text == B_RESET_YES)
async def on_reset_yes(msg: Message, state: FSMContext):
    data = await state.get_data()
    p = _owned_purchase(data, msg.from_user.id)
    iid = _lolz_id(p) if p else None
    if not iid:
        return await show_accounts(msg, state)
    ok, err = await run_with_dots(msg, "⏳ Сбрасываю сессии", lolz.reset_authorizations(iid))
    await state.update_data(screen="acc")
    await msg.answer("✅ Сессии сброшены!" if (ok and err is None) else
                     f"❌ Не удалось сбросить{': ' + str(err) if err else ''}",
                     reply_markup=K.kb_purchased())


# ═════════════════════════════════════════════════════════
# Профиль, мои аккаунты, история, рефералы, инфо
# ═════════════════════════════════════════════════════════
@router.message(F.text == B_PROFILE)
async def on_profile(msg: Message, state: FSMContext):
    await show_profile(msg, state)


@router.message(F.text == B_MYACC)
async def on_my_accounts(msg: Message, state: FSMContext):
    await show_accounts(msg, state)


@router.message(F.text.regexp(r"^\d+\. .* · \d{2}\.\d{2}\.\d{4}$"))
async def on_pick_account(msg: Message, state: FSMContext):
    pid = ((await state.get_data()).get("accs") or {}).get(msg.text)
    if not pid:
        return await show_accounts(msg, state)
    await show_account(msg, state, pid)


@router.message(F.text == B_HISTORY)
async def on_history(msg: Message, state: FSMContext):
    await state.update_data(screen="history")
    history = db.get_topup_history(msg.from_user.id, limit=15)
    if not history:
        text = "🧾 <b>ИСТОРИЯ ПОПОЛНЕНИЙ</b>\n\nПока пусто."
    else:
        lines = ["🧾 <b>ИСТОРИЯ ПОПОЛНЕНИЙ</b>\n"]
        for h in history:
            lines.append(f"📅 {h['created_at']} — <b>+{money(float(h['amount']))}</b> ({h['method']})")
        text = "\n".join(lines)
    await answer_screen(msg, text, K.kb_back_only())


@router.message(F.text == B_REFERRAL)
async def on_referral(msg: Message, state: FSMContext):
    await state.update_data(screen="referral")
    u = db.get_user(msg.from_user.id, msg.from_user.username or "")
    u["user_id"] = msg.from_user.id
    await answer_screen(msg, T.txt_referral(u), K.kb_back_only())


@router.message(F.text == B_INFO)
async def on_info(msg: Message, state: FSMContext):
    await state.update_data(screen="info")
    await answer_screen(msg, T.txt_useful_info(), K.kb_back_only(), INFO_PHOTO())
