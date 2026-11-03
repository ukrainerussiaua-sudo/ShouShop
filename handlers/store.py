"""
handlers/store.py — SHOU SHOP: новый интерфейс и оплата БЕЗ баланса.

Сценарий любого товара:
  выбор товара → [Оплатить] → реквизиты (карта, получатель, сумма) → [Далее] →
  клиент присылает скриншот/квитанцию → админ нажимает «Подтвердить» → заказ выполняется.

Разделы: Каталог NFT-подарков, Звёзды, Премиум, GRAM, Профиль (+ бонус-слоты, рефералы, язык), Отзывы, Аренда NFT.
Админ-команды: /gram_rate 67 · /usd_rate (фиксированный курс 47) · /pack 50 45 · /rent_text текст
"""
from __future__ import annotations
import asyncio
import html
import json
import math
import re

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (CallbackQuery, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions, Message)

import fragment
import ton_wallet
from config import (ADMIN_IDS, SHOP_NAME, SUPPORT_URL, REVIEWS_CHANNEL_URL, BOT_USERNAME, MENU_PHOTO_PATH,
                    GRAM_PHOTO_PATH, REVIEWS_PHOTO_PATH, BONUS_PHOTO_PATH, RENT_PHOTO_PATH, PAY_PHOTO_PATH,
                    PROFILE_PHOTO_PATH, STARS_PHOTO_PATH, PREMIUM_PHOTO_PATH, NFT_PHOTO_PATH,
                    CARD_NUMBER, CARD_HOLDER, CARD_BANK, BONUS_NAME, RENT_URL)
from database import db
import keyboards as K
from keyboards import B_STARS, B_PREMIUM, B_GRAM, B_NFT, B_PROFILE, B_REVIEWS, B_RENT, ib
from utils import _photo, notify_admins, user_label, send_lang_chooser

router = Router()
IS_ADMIN = F.from_user.id.in_(ADMIN_IDS)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_locks: dict[int, asyncio.Lock] = {}

STAR_PACKS = (50, 100, 150, 250, 350, 500, 750, 1000, 1500, 2500)
PACKS_PER_PAGE = 6
NFT_PER_PAGE = 8
GRAM_MIN, GRAM_MAX = 1, 100_000_000
SPIN_TIERS = ((2000, 20), (1000, 10), (500, 5), (100, 2), (50, 1))        # звёзд в заказе → спинов
SLOT_REWARD = {64: 7, 1: 5, 22: 1, 43: 1}                                  # значение 🎰 → бонусные звёзды
WITHDRAW_MIN = 100
FILTERS = (("new", "новые"), ("cheap", "дешёвые"), ("expensive", "дорогие"))
USD_UAH_RATE = 47.0
class Buy(StatesGroup):
    username = State()
    qty = State()
    gram_qty = State()
    gram_addr = State()
    nft_user = State()
    receipt = State()


# ══════════════════ вспомогательное ══════════════════
def _usd_rate() -> float:
    """Фиксированный пользовательский курс: 1 USD = 47 UAH."""
    return USD_UAH_RATE


def fm(uah: float, cur: str = "uah") -> str:
    """Цена в выбранной валюте: 400₴ или 9.64$."""
    if cur == "usd":
        return f"{uah / _usd_rate():.2f}$"
    return f"{int(round(float(uah))):,}".replace(",", " ") + "₴"


async def _cur(state: FSMContext) -> str:
    return (await state.get_data()).get("cur", "uah")


def _cur_btn(cur: str, cb: str):
    return ib(f"Валюта: {'$' if cur == 'usd' else '₴'}", "repeat", callback_data=cb, style="primary")


def _photo_or(path: str, *fallbacks: str):
    for p in (path, *fallbacks):
        ph = _photo(p)
        if ph:
            return ph
    return None


async def _screen(target: Message, text: str, kb=None, photo=None):
    """Отправка экрана: баннер с подписью или просто текст."""
    if photo:
        try:
            return await target.answer_photo(photo, caption=text, reply_markup=kb)
        except Exception as e:
            print(f"[store photo] {e!r}")
    return await target.answer(text, reply_markup=kb, link_preview_options=NO_PREVIEW)


async def _edit(cb: CallbackQuery, text: str, kb=None):
    """Редактирует экран (и подпись у фото, и обычный текст)."""
    m = cb.message
    try:
        if m.photo or m.document or m.video:
            await m.edit_caption(caption=text, reply_markup=kb)
        else:
            await m.edit_text(text, reply_markup=kb, link_preview_options=NO_PREVIEW)
    except Exception as e:
        if "not modified" not in str(e):
            print(f"[store edit] {e!r}")


def card_info() -> tuple[str, str, str]:
    return (db.get_setting("card_number", "").strip() or CARD_NUMBER,
            db.get_setting("card_holder", "").strip() or CARD_HOLDER,
            db.get_setting("card_bank", "").strip() or CARD_BANK)


# ══════════════════ цены ══════════════════
DEFAULT_STAR_PRICES = {50: 45, 100: 75, 150: 120, 250: 200, 350: 280, 500: 390}
DEFAULT_PREMIUM_PRICES = {3: 620, 6: 800, 12: 1300}


def star_price(n: int) -> int:
    """Цена пакета: /pack N цена → таблица по умолчанию → цена за 1 звезду из админки → 0.78 ₴/звезда."""
    custom = db.get_float(f"star_pack:{n}", 0.0)
    if custom > 0:
        return math.ceil(custom)
    if n in DEFAULT_STAR_PRICES:
        return DEFAULT_STAR_PRICES[n]
    p = db.get_stars_price() or 0.78
    return math.ceil(n * p)


def premium_price(m: int) -> int:
    return int(db.get_premium_price(m) or DEFAULT_PREMIUM_PRICES.get(m, 0))


def _price(kind: str, qty: int) -> int:
    return star_price(qty) if kind == "stars" else premium_price(qty)


def _cost(kind: str, qty: int) -> float:
    if kind == "stars":
        return round(qty * db.get_float("cost_star", 0.0), 2)
    return round(db.get_float(f"cost_premium_{qty}", 0.0), 2)


def _title(kind: str, qty: int) -> str:
    return f"{qty} звёзд Telegram" if kind == "stars" else f"Telegram Premium на {qty} {_months_word(qty)}"


def gram_price(qty: int) -> int:
    return math.ceil(qty * (db.get_float("gram_rate", 67.0) or 67.0))


# ══════════════════ главное меню / отзывы / аренда ══════════════════
async def show_main(msg: Message, state: FSMContext):
    from handlers.shop import show_main as _m
    await _m(msg, state)


@router.message(F.text == B_REVIEWS)
async def on_reviews(msg: Message, state: FSMContext):
    await state.set_state(None)
    kb = InlineKeyboardMarkup(inline_keyboard=[[ib("Отзывы", "chat", url=REVIEWS_CHANNEL_URL)]])
    await _screen(msg, "💬 Отзывы наших покупателей:", kb, _photo_or(REVIEWS_PHOTO_PATH, MENU_PHOTO_PATH))


@router.message(F.text == B_RENT)
async def on_rent(msg: Message, state: FSMContext):
    await state.set_state(None)
    text = db.get_setting("rent_text", "").strip() or "🕓 <b>Аренда NFT</b>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[ib("Перейти", "rent", url=RENT_URL, style="primary")]])
    await _screen(msg, text, kb, _photo_or(RENT_PHOTO_PATH, MENU_PHOTO_PATH))


async def _to_main(cb: CallbackQuery):
    """«Назад»: то же сообщение превращается в «Выберите раздел:» с баннером меню. Сообщения не удаляются."""
    from texts import txt_shop
    m = cb.message
    ph = _photo_or(MENU_PHOTO_PATH)
    try:
        if m.photo and ph:
            await m.edit_media(InputMediaPhoto(media=ph, caption=txt_shop()))
        elif m.photo or m.document or m.video:
            await m.edit_caption(caption=txt_shop())
        else:
            await m.edit_text(txt_shop())
    except Exception as e:
        if "not modified" not in str(e):
            print(f"[store back] {e!r}")


@router.callback_query(F.data == "st:home")
async def on_home_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await cb.answer()
    await _to_main(cb)


# ══════════════════ ЗВЁЗДЫ ══════════════════
def _stars_kb(page: int, cur: str) -> InlineKeyboardMarkup:
    packs = [n for n in STAR_PACKS if star_price(n) > 0]
    pages = max(1, -(-len(packs) // PACKS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    rows = [[_cur_btn(cur, f"st:cur:stars:{page}")],
            [ib("Любое количество звёзд", "stars", callback_data="st:sany")]]
    for n in packs[page * PACKS_PER_PAGE:(page + 1) * PACKS_PER_PAGE]:
        rows.append([ib(f"{n} звёзд - {fm(star_price(n), cur)}", "stars", callback_data=f"st:sp:{n}")])
    prev_p = page - 1 if page > 0 else None
    next_p = page + 1 if page < pages - 1 else None
    rows.append([
        ib("«" if prev_p is not None else "•", callback_data=f"st:stars:{prev_p}" if prev_p is not None else "st:noop"),
        ib(str(page + 1), callback_data="st:noop"),
        ib("»" if next_p is not None else "•", callback_data=f"st:stars:{next_p}" if next_p is not None else "st:noop"),
    ])
    rows.append([ib("Назад", "back", callback_data="st:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_STARS_TEXT = "🌟 <b>Звёзды Telegram</b>\nВыберите пакет или любое количество звёзд."


@router.message(F.text == B_STARS)
async def on_stars(msg: Message, state: FSMContext):
    await state.set_state(None)
    await _screen(msg, _STARS_TEXT, _stars_kb(0, await _cur(state)), _photo_or(STARS_PHOTO_PATH, MENU_PHOTO_PATH))


@router.callback_query(F.data.startswith("st:stars:"))
async def on_stars_page(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _edit(cb, _STARS_TEXT, _stars_kb(int(cb.data.split(":")[2]), await _cur(state)))


@router.callback_query(F.data == "st:noop")
async def on_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("st:cur:"))
async def on_toggle_cur(cb: CallbackQuery, state: FSMContext):
    """Переключатель валюты ₴ ↔ $ для текущего экрана."""
    cur = "usd" if await _cur(state) == "uah" else "uah"
    await state.update_data(cur=cur)
    await cb.answer(f"Валюта: {'$' if cur == 'usd' else '₴'}")
    parts = cb.data.split(":")
    where, arg = parts[2], (parts[3] if len(parts) > 3 else "0")
    if where == "stars":
        await _edit(cb, _STARS_TEXT, _stars_kb(int(arg), cur))
    elif where == "nft":
        await _render_nft_list(cb, state, int(arg))


async def _ask_username(target: Message, state: FSMContext, kind: str, qty: int):
    if _price(kind, qty) <= 0:
        return await target.answer("❌ Этот товар сейчас недоступен.")
    await state.set_state(Buy.username)
    await state.update_data(order={"kind": kind, "qty": qty})
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ib("Себе", "profile", callback_data="st:self")],
        [ib("Отмена", "back", callback_data="st:cancelflow")]])
    await target.answer(f"<b>{_title(kind, qty)}</b>\n\nВведите @username получателя:", reply_markup=kb)


@router.callback_query(F.data.startswith("st:sp:"))
async def on_star_pack(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _ask_username(cb.message, state, "stars", int(cb.data.split(":")[2]))


@router.callback_query(F.data == "st:sany")
async def on_star_any(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Buy.qty)
    await cb.message.answer(f"Введите количество звёзд (от {fragment.STARS_MIN} до 100000):")


@router.message(Buy.qty, F.text)
async def on_qty(msg: Message, state: FSMContext):
    raw = msg.text.strip()
    if not raw.isdigit() or not fragment.STARS_MIN <= int(raw) <= 100_000:
        return await msg.answer(f"❌ Введите целое число от {fragment.STARS_MIN} до 100000:")
    await _ask_username(msg, state, "stars", int(raw))


# ══════════════════ ПРЕМИУМ ══════════════════
_PREMIUM_TEXT = ("👑 <b>Telegram Premium</b>\nПодарком — отправляется как Gift (без входа в аккаунт, "
                 "нужен только @username). Выберите вариант ниже")


def _months_word(m: int) -> str:
    return "месяц" if m == 1 else ("месяца" if m in (2, 3, 4) else "месяцев")


def _premium_kb() -> InlineKeyboardMarkup:
    rows = [[ib(f"{m} {_months_word(m)} - {fm(premium_price(m))}", "crown", callback_data=f"st:pm:{m}")]
            for m in fragment.PREMIUM_MONTHS if premium_price(m) > 0]
    rows.append([ib("Назад", "back", callback_data="st:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == B_PREMIUM)
async def on_premium(msg: Message, state: FSMContext):
    await state.set_state(None)
    await _screen(msg, _PREMIUM_TEXT, _premium_kb(), _photo_or(PREMIUM_PHOTO_PATH, MENU_PHOTO_PATH))


@router.callback_query(F.data.startswith("st:pm:"))
async def on_premium_pick(cb: CallbackQuery, state: FSMContext):
    m = int(cb.data.split(":")[2])
    await cb.answer()
    if m in fragment.PREMIUM_MONTHS:
        await _ask_username(cb.message, state, "premium", m)


# ── получатель для Stars/Premium ──
@router.callback_query(F.data == "st:cancelflow")
async def on_cancel_flow(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await cb.answer("Отменено")
    if cb.message.photo:                       # экран GRAM с баннером → возврат в меню
        return await _to_main(cb)
    await _edit(cb, "❌ Отменено.")


@router.callback_query(Buy.username, F.data == "st:self")
async def on_self(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    if not cb.from_user.username:
        return await cb.message.answer("❌ У вас не задан @username в Telegram. Введите чужой @username вручную.")
    await _recipient(cb.message, state, cb.from_user.username, cb.from_user)


@router.message(Buy.username, F.text)
async def on_username(msg: Message, state: FSMContext):
    await _recipient(msg, state, msg.text, msg.from_user)


async def _recipient(msg: Message, state: FSMContext, raw: str, user):
    order = (await state.get_data()).get("order")
    if not order:
        return await state.set_state(None)
    target = fragment.normalize_username(raw or "")
    if not target:
        return await msg.answer("❌ Это не похоже на @username. Пример: @durov (минимум 5 символов).")
    kind, qty = order["kind"], order["qty"]
    price = _price(kind, qty)
    if price <= 0:
        return await msg.answer("❌ Этот товар сейчас недоступен.")
    status = await msg.answer("🔎 Проверяю получателя…")
    try:
        found = await fragment.check_recipient(kind, target, qty)
    except fragment.RecipientError as e:
        return await status.edit_text(f"❌ {e}\nПроверьте @username и отправьте ещё раз.")
    except Exception as e:
        print(f"[store check] {e!r}")
        await status.edit_text("⚠️ Сервис временно недоступен. Попробуйте позже.")
        await state.set_state(None)
        await notify_admins(msg.bot, f"⚠️ Fragment: проверка получателя не удалась\n{e!r}\nВозможно, протухли cookies.")
        return
    await status.edit_text(f"✅ Получатель найден: @{target}")
    await state.set_state(None)
    oid = db.order_add(user.id, kind, _title(kind, qty), qty, f"@{target}", price, _cost(kind, qty))
    cur = await _cur(state)
    await _order_card(msg, oid, f"👤 Получатель: @{target} ({html.escape(found.get('name', ''))})", cur)


# ══════════════════ карточка заказа → оплата → чек ══════════════════
async def _order_card(msg: Message, oid: int, extra: str, cur: str = "uah"):
    o = db.order_get(oid)
    text = (f"🧾 <b>Заказ #{oid}</b>\n\n"
            f"📦 Товар: <b>{html.escape(o['title'])}</b>\n{extra}\n"
            f"💰 К оплате: <b>{fm(o['amount'], 'uah')}</b>"
            + (f" ({fm(o['amount'], 'usd')})" if cur == "usd" else ""))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ib("Оплатить", "ok", callback_data=f"st:pay:{oid}")],
        [ib("Отмена", "cancel", callback_data=f"st:oc:{oid}")]])
    await msg.answer(text, reply_markup=kb, link_preview_options=NO_PREVIEW)


def _own(oid: int, uid: int) -> dict | None:
    o = db.order_get(oid)
    return o if o and int(o["user_id"]) == uid else None


@router.callback_query(F.data.startswith("st:oc:"))
async def on_order_cancel(cb: CallbackQuery, state: FSMContext):
    o = _own(int(cb.data.split(":")[2]), cb.from_user.id)
    if o and o["status"] in ("draft", "awaiting_receipt"):
        db.order_claim(o["id"], ("draft", "awaiting_receipt"), "canceled")
        await state.set_state(None)
        await cb.answer("Заказ отменён")
        return await _edit(cb, f"❌ Заказ #{o['id']} отменён.")
    await cb.answer("Заказ уже нельзя отменить.", show_alert=True)


def _item_line(o: dict) -> str:
    """«Товар: …» человекочитаемо, как в Grizz."""
    return html.escape(o["title"])


def _who_line(o: dict) -> str:
    if o["kind"] == "gram":
        return f"Адрес: <code>{html.escape(o['target'])}</code>"
    if o["kind"] == "nft":
        return f"Получатель: {html.escape(_nft_label(o).split('→')[-1].strip())}"
    return f"Получатель: {html.escape(o['target'])}"


def _pay_text(o: dict) -> str:
    number, holder, bank = card_info()
    amount = int(round(float(o["amount"])))
    return (f"📋 <b>Заказ #{o['id']}</b>\n"
            f"Товар: {_item_line(o)}\n{_who_line(o)}\n"
            f"Сумма к оплате: <b>{amount} грн | {amount / _usd_rate():.2f} $</b>\n\n"
            f"Переведите на карту ровно {amount} грн:\n"
            + (f"{html.escape(bank)}\n" if bank else "")
            + f"<code>{html.escape(number)}</code>\n"
            f"Получатель: {html.escape(holder)}\n\n"
            "<blockquote>После оплаты пришлите сюда скриншот, файл или ссылку на квитанцию.</blockquote>")


def _cancel_btn(oid: int):
    return ib("Отменить заказ", "cancel", callback_data=f"st:oc:{oid}", style="danger")


@router.callback_query(F.data.startswith("st:pay:"))
async def on_pay(cb: CallbackQuery, state: FSMContext):
    o = _own(int(cb.data.split(":")[2]), cb.from_user.id)
    if not o or o["status"] not in ("draft", "awaiting_receipt"):
        return await cb.answer("Заказ не найден или уже оплачен.", show_alert=True)
    number, holder, _ = card_info()
    if not (number and holder):
        return await cb.answer("Оплата временно недоступна. Напишите в поддержку.", show_alert=True)
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ib("Далее", "next", callback_data=f"st:next:{o['id']}", style="primary")],
        [_cancel_btn(o["id"])]])
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # баннер «ОПЛАТА ЗАКАЗА»: положите images/pay.jpg — без файла экран идёт текстом
    await _screen(cb.message, _pay_text(o), kb, _photo(PAY_PHOTO_PATH))


@router.callback_query(F.data.startswith("st:next:"))
async def on_next(cb: CallbackQuery, state: FSMContext):
    o = _own(int(cb.data.split(":")[2]), cb.from_user.id)
    if not o or o["status"] not in ("draft", "awaiting_receipt"):
        return await cb.answer("Заказ не найден или уже оплачен.", show_alert=True)
    db.order_claim(o["id"], ("draft", "awaiting_receipt"), "awaiting_receipt")
    await state.set_state(Buy.receipt)
    await state.update_data(pay_oid=o["id"])
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[_cancel_btn(o["id"])]])
    await cb.message.answer(f"📎 Отправьте <b>скриншот, файл или ссылку на квитанцию</b> об оплате "
                            f"{int(round(float(o['amount'])))} грн одним сообщением.", reply_markup=kb)


_URL_RE = re.compile(r"https?://\S{6,}", re.I)


@router.message(Buy.receipt, F.photo | F.document | F.text)
async def on_receipt(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    oid = (await state.get_data()).get("pay_oid")
    o = _own(int(oid), uid) if oid else None
    if not o or o["status"] != "awaiting_receipt":
        await state.set_state(None)
        return await msg.answer("⚠️ Заказ не найден или чек уже отправлен.")
    link = None
    if msg.photo:
        f = msg.photo[-1]
        fid, uniq, is_photo = f.file_id, f.file_unique_id, True
    elif msg.document:
        fid, uniq, is_photo = msg.document.file_id, msg.document.file_unique_id, False
    else:
        m = _URL_RE.search(msg.text or "")
        if not m:
            return await msg.answer("Отправьте фото/скриншот, файл-квитанцию или ссылку на неё. "
                                    "Для отмены нажмите «Отменить заказ».")
        link = m.group(0)[:300]
        fid, uniq, is_photo = link, "link:" + link[:150], False
    if not db.claim_receipt(f"tg:{uniq}", uid, float(o["amount"])):
        await notify_admins(bot, f"🚨 <b>Повторный чек!</b> {user_label(uid, msg.from_user.username)} "
                                 f"прислал уже использованный чек (заказ #{oid}).")
        return await msg.answer("❌ Этот чек уже использовался. Отправьте актуальную квитанцию.")

    if o["kind"] == "nft":                                    # бронируем подарок на время проверки чека
        gid = int(o["target"].split(":")[0])
        async with _locks.setdefault(gid, asyncio.Lock()):
            if not db.nft_reserve(gid, uid):
                db.order_claim(oid, "awaiting_receipt", "canceled", note="NFT уже забрали")
                await state.set_state(None)
                return await msg.answer("❌ Этот NFT уже забрали. Выберите другой в каталоге.")
        from handlers.nft import _sync_channel_post
        g = db.nft_get(gid)
        if g:
            await _sync_channel_post(bot, g, status="reserved")

    if not db.order_claim(oid, "awaiting_receipt", "review", receipt_id=fid, receipt_photo=is_photo):
        return await msg.answer("⚠️ Чек по этому заказу уже отправлен.")
    await state.set_state(None)
    u = db.find_user_by_id(uid) or {}
    first = [ib("Написать клиенту", "chat", url=f"https://t.me/{u['username']}")] if u.get("username") else []
    kb = InlineKeyboardMarkup(inline_keyboard=[
        *([first] if first else []),
        [ib("Подтвердить", "ok", callback_data=f"so:ok:{oid}"), ib("Отклонить", "cancel", callback_data=f"so:no:{oid}")]])
    caption = (f"💳 <b>Проверьте оплату — заказ #{oid}</b>\n\n"
               f"👤 {user_label(uid, u.get('username'), msg.from_user.first_name)}\n"
               f"📦 {html.escape(o['title'])}\n🎯 {html.escape(o['target'] if o['kind'] != 'nft' else _nft_label(o))}\n"
               f"💰 Ожидается: <b>{fm(o['amount'])}</b>"
               + (f"\n🔗 Ссылка на квитанцию: {html.escape(link)}" if link else ""))
    media = {} if link else ({"photo": fid} if is_photo else {"document": fid})
    n = await notify_admins(bot, caption, kb, **media)
    await msg.answer("✅ Чек отправлен администратору. Как только оплату подтвердят — заказ будет выполнен, "
                     "мы пришлём уведомление.", reply_markup=K.kb_main_menu(uid in ADMIN_IDS))
    if n == 0:
        print(f"[store] чек заказа #{oid}: ни один админ не получил сообщение")


# ══════════════════ GRAM ══════════════════
@router.message(F.text == B_GRAM)
async def on_gram(msg: Message, state: FSMContext):
    await state.set_state(Buy.gram_qty)
    rate = db.get_float("gram_rate", 67.0) or 67.0
    kb = InlineKeyboardMarkup(inline_keyboard=[[ib("Назад", "back", callback_data="st:cancelflow")]])
    await _screen(msg, f"💎 <b>Покупка GRAM</b>\nКурс: 1 GRAM = <b>{rate:g} грн</b>\n\n"
                       f"Отправьте количество GRAM сообщением (от 1 до 100 000 000).",
                  kb, _photo_or(GRAM_PHOTO_PATH, MENU_PHOTO_PATH))


@router.message(Buy.gram_qty, F.text)
async def on_gram_qty(msg: Message, state: FSMContext):
    raw = msg.text.replace(" ", "").strip()
    if not raw.isdigit() or not GRAM_MIN <= int(raw) <= GRAM_MAX:
        return await msg.answer("❌ Введите целое число от 1 до 100 000 000.")
    qty = int(raw)
    await state.update_data(gram_qty=qty)
    await state.set_state(Buy.gram_addr)
    kb = InlineKeyboardMarkup(inline_keyboard=[[ib("Назад", "back", callback_data="st:cancelflow")]])
    price = gram_price(qty)
    await msg.answer(f"GRAM: {qty} шт.\nСумма: <b>{price} грн | {price / _usd_rate():.2f} $</b>\n\n"
                     "Отправьте адрес TON-кошелька, на который зачислить GRAM.", reply_markup=kb)


@router.message(Buy.gram_addr, F.text)
async def on_gram_addr(msg: Message, state: FSMContext):
    addr = msg.text.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{30,70}", addr):
        return await msg.answer("❌ Это не похоже на адрес кошелька. Проверьте и отправьте ещё раз.")
    qty = int((await state.get_data()).get("gram_qty") or 0)
    await state.set_state(None)
    oid = db.order_add(msg.from_user.id, "gram", f"{qty} GRAM", qty, addr, gram_price(qty), 0.0)
    await _order_card(msg, oid, f"👛 Адрес: <code>{html.escape(addr)}</code>", await _cur(state))


# ══════════════════ NFT-каталог ══════════════════
def _nft_items(flt: str) -> list[dict]:
    from handlers.nft import _in_sale
    rows = _in_sale()
    if flt == "cheap":
        return sorted(rows, key=lambda g: (float(g["price"]), g["id"]))
    if flt == "expensive":
        return sorted(rows, key=lambda g: (-float(g["price"]), g["id"]))
    return sorted(rows, key=lambda g: -int(g["id"]))               # новые — сверху


def _nft_label(o: dict) -> str:
    try:
        gid = int(str(o["target"]).split(":")[0])
        g = db.nft_get(gid)
        t = json.loads(o.get("note") or "{}")
        who = f"@{t['username']}" if t.get("username") else f"ID {t.get('recipient_user_id')}"
        return f"NFT: {g['title'] if g else gid} → {who}"
    except Exception:
        return str(o.get("target"))


def _nft_emoji_button(label: str, gift: dict, callback_data: str, *, fallback_icon: str = "gift",
                      style: str | None = None):
    """NFT-specific button: custom emoji ID as button icon, regular emoji in button text."""
    chosen = str(gift.get("emoji_id") or "").strip()
    if chosen.isdigit():
        return ib(label, chosen, callback_data=callback_data, **({"style": style} if style else {}))
    if chosen:
        label = f"{chosen} {label}"
        fallback_icon = None
    return ib(label, fallback_icon, callback_data=callback_data, **({"style": style} if style else {}))


async def _nft_kb(state: FSMContext, page: int):
    d = await state.get_data()
    cur, flt = d.get("cur", "uah"), d.get("nft_flt", "new")
    items = _nft_items(flt)
    pages = max(1, -(-len(items) // NFT_PER_PAGE))
    page = max(0, min(page, pages - 1))
    flt_name = dict(FILTERS)[flt]
    rows = [[_cur_btn(cur, f"st:cur:nft:{page}"), ib(f"Фильтр: {flt_name}", "filter", callback_data=f"st:nflt:{page}", style="primary")]]
    for g in items[page * NFT_PER_PAGE:(page + 1) * NFT_PER_PAGE]:
        rows.append([_nft_emoji_button(f"{g['title']} — {fm(g['price'], cur)}", g,
                                       callback_data=f"st:ng:{g['id']}")])
    if pages > 1:
        rows.append([
            ib("«" if page > 0 else "•", callback_data=f"st:np:{page - 1}" if page > 0 else "st:noop"),
            ib(f"{page + 1}/{pages}", callback_data="st:noop"),
            ib("»" if page < pages - 1 else "•", callback_data=f"st:np:{page + 1}" if page < pages - 1 else "st:noop")])
    rows.append([ib("Назад", "back", callback_data="st:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows), bool(items)


async def _render_nft_list(cb: CallbackQuery, state: FSMContext, page: int):
    kb, has = await _nft_kb(state, page)
    await _edit(cb, "🎁 <b>NFT-каталог</b>\nВыберите подарок:" if has else
                "🎁 <b>NFT-каталог</b>\n\nСейчас в продаже нет NFT. Загляните позже!", kb)


@router.message(F.text == B_NFT)
async def on_nft(msg: Message, state: FSMContext):
    await state.set_state(None)
    kb, has = await _nft_kb(state, 0)
    await _screen(msg, "🎁 <b>NFT-каталог</b>\nВыберите подарок:" if has else
                  "🎁 <b>NFT-каталог</b>\n\nСейчас в продаже нет NFT. Загляните позже!",
                  kb, _photo_or(NFT_PHOTO_PATH, MENU_PHOTO_PATH))


@router.callback_query(F.data.startswith("st:np:"))
async def on_nft_page(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _render_nft_list(cb, state, int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("st:nflt:"))
async def on_nft_filter(cb: CallbackQuery, state: FSMContext):
    keys = [k for k, _ in FILTERS]
    cur = (await state.get_data()).get("nft_flt", "new")
    await state.update_data(nft_flt=keys[(keys.index(cur) + 1) % len(keys)])
    await cb.answer()
    await _render_nft_list(cb, state, 0)


def _nft_card_text(g: dict, cur: str) -> str:
    return (f"🎁 <b>{html.escape(g['title'])}</b>\n\n💰 Цена: <b>{fm(g['price'], cur)}</b>\n"
            f"🔗 <a href=\"{html.escape(g['link'], quote=True)}\">Открыть подарок в Telegram</a>")


def _nft_ok(g) -> bool:
    return bool(g) and g.get("status") == "available" and float(g.get("price") or 0) > 0


async def show_nft_card(msg: Message, state: FSMContext, gid: int):
    """Карточка NFT (и из каталога, и по ссылке из канала /start nft_ID)."""
    g = db.nft_get(gid)
    if not _nft_ok(g):
        return await msg.answer("⚪ Этот NFT уже продан, забронирован или снят с продажи.")
    cur = await _cur(state) if state else "uah"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_nft_emoji_button(f"Купить · {fm(g['price'], cur)}", g,
                           callback_data=f"st:nb:{gid}", fallback_icon="ok", style="primary")],
        [ib("К списку", "back", callback_data="st:np:0")]])
    await msg.answer(_nft_card_text(g, cur), reply_markup=kb,
                     link_preview_options=LinkPreviewOptions(is_disabled=False, url=g["link"], show_above_text=True))


@router.callback_query(F.data.startswith("st:ng:"))
async def on_nft_open(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await show_nft_card(cb.message, state, int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("st:nb:"))
async def on_nft_buy(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split(":")[2])
    if not _nft_ok(db.nft_get(gid)):
        return await cb.answer("Товар уже недоступен", show_alert=True)
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ib("На мой аккаунт", "profile", callback_data=f"st:nd:self:{gid}")],
        [ib("Другому (@username)", "people", callback_data=f"st:nd:other:{gid}")],
        [ib("Отмена", "back", callback_data="st:cancelflow")]])
    await cb.message.answer("📬 <b>Куда отправить NFT?</b>", reply_markup=kb)


@router.callback_query(F.data.startswith("st:nd:"))
async def on_nft_dest(cb: CallbackQuery, state: FSMContext):
    _, _, mode, gid_s = cb.data.split(":")
    gid = int(gid_s)
    await cb.answer()
    if mode == "self":
        t = {"mode": "self", "username": cb.from_user.username or "", "recipient_user_id": cb.from_user.id,
             "buyer_id": cb.from_user.id}
        return await _nft_order(cb.message, state, cb.from_user.id, gid, t)
    await state.set_state(Buy.nft_user)
    await state.update_data(nft_gid=gid)
    await cb.message.answer("Введите @username получателя NFT:")


@router.message(Buy.nft_user, F.text)
async def on_nft_user(msg: Message, state: FSMContext):
    m = re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{4,31})", msg.text.strip())
    if not m:
        return await msg.answer("❌ Некорректный username. Пример: <code>@username</code> (5–32 символа).")
    gid = int((await state.get_data()).get("nft_gid") or 0)
    known = db.find_user_by_username(m.group(1))
    t = {"mode": "username", "username": m.group(1),
         "recipient_user_id": int(known["user_id"]) if known and known.get("user_id") else None,
         "buyer_id": msg.from_user.id}
    await state.set_state(None)
    await _nft_order(msg, state, msg.from_user.id, gid, t)


async def _nft_order(msg: Message, state: FSMContext, uid: int, gid: int, target: dict):
    g = db.nft_get(gid)
    if not _nft_ok(g):
        return await msg.answer("⚪ Этот NFT уже недоступен.")
    oid = db.order_add(uid, "nft", f"NFT {g['title']}", 1, f"{gid}", float(g["price"]), float(g.get("cost") or 0))
    db.order_update(oid, note=json.dumps(target, ensure_ascii=False))
    who = f"@{target['username']}" if target.get("username") else f"ID {target.get('recipient_user_id')}"
    await _order_card(msg, oid, f"📬 Получатель: {html.escape(who)}", await _cur(state))


# ══════════════════ админ: подтверждение оплаты ══════════════════
def _adm_name(cb: CallbackQuery) -> str:
    raw = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name
    return html.escape(raw or "Администратор")


async def _finish_admin_msg(cb: CallbackQuery, suffix: str, kb=None):
    base = cb.message.caption or cb.message.text or ""
    try:
        if cb.message.caption is not None or cb.message.photo or cb.message.document:
            await cb.message.edit_caption(caption=f"{base}\n\n{suffix}", reply_markup=kb)
        else:
            await cb.message.edit_text(f"{base}\n\n{suffix}", reply_markup=kb)
    except Exception as e:
        print(f"[store admin edit] {e!r}")


def award_spins(uid: int, stars_qty: int) -> int:
    for need, spins in SPIN_TIERS:
        if stars_qty >= need:
            b = db.bonus_get(uid)
            db.bonus_set(uid, b["spins"] + spins, b["stars"])
            return spins
    return 0


def _ref_spin(uid: int):
    """Реферал: пригласившему +1 спин за покупку приглашённого (баланса в магазине больше нет)."""
    ref = (db.find_user_by_id(uid) or {}).get("referrer_id")
    if ref:
        b = db.bonus_get(int(ref))
        db.bonus_set(int(ref), b["spins"] + 1, b["stars"])


@router.callback_query(F.data.startswith("so:ok:"), IS_ADMIN)
async def adm_ok(cb: CallbackQuery, bot: Bot):
    oid = int(cb.data.split(":")[2])
    o = db.order_get(oid)
    if not o or not db.order_claim(oid, "review", "processing"):
        return await cb.answer("Заказ уже обработан.", show_alert=True)
    uid, kind = int(o["user_id"]), o["kind"]
    await cb.answer("Выполняю…")

    if kind in ("stars", "premium"):
        target = o["target"].lstrip("@")
        qty = int(o["qty"])
        try:
            res = await (fragment.buy_stars(target, qty) if kind == "stars" else fragment.gift_premium(target, qty))
        except fragment.FragmentError as e:                    # определённый отказ: деньги за заказ у вас — решайте вручную
            db.order_claim(oid, "processing", "review", note=str(e)[:200])
            return await _finish_admin_msg(cb, f"⚠️ Fragment отказал: {html.escape(str(e))}\nМожно повторить или отклонить.",
                                           cb.message.reply_markup)
        except Exception as e:                                 # TON мог уйти — не повторяем
            db.order_claim(oid, "processing", "uncertain", note=repr(e)[:200])
            return await _finish_admin_msg(cb, f"🚨 Неясный итог: {html.escape(repr(e))}\nПроверьте кошелёк/Fragment вручную. Не повторяйте!")
        db.order_claim(oid, "processing", "paid")
        foid = db.add_fragment_order(uid, kind, qty, target, float(o["amount"]))
        db.set_fragment_order(foid, "done", (res or {}).get("req_id", ""))
        db.add_sale(uid, kind, f"{o['title']} → @{target}", qty, float(o["amount"]), float(o["cost"]), 0.0)
        spins = award_spins(uid, qty) if kind == "stars" else 0
        _ref_spin(uid)
        text = f"✅ <b>Заказ #{oid} выполнен!</b>\n\n{html.escape(o['title'])} → @{target}\n\nСпасибо за покупку!"
        if spins:
            text += f"\n🎰 Бонус: +{spins} спин(ов) в разделе «Профиль → {BONUS_NAME} Бонус»."
        try:
            await bot.send_message(uid, text, reply_markup=K.kb_leave_review("fragment", foid))
        except Exception as e:
            print(f"[store notify] {e!r}")
        return await _finish_admin_msg(cb, f"✅ Подтверждено и выдано ({_adm_name(cb)})")

    if kind == "gram":
        qty, addr = int(o["qty"]), o["target"]
        auto_max = db.get_float("gram_auto_max", 100.0)         # безопасный лимит автоотправки, /gram_auto 500
        why_manual = ""
        if not ton_wallet.is_configured():
            why_manual = "кошелёк TON не настроен"
        elif qty > auto_max:
            why_manual = f"сумма больше лимита автоотправки ({auto_max:g})"
        else:
            try:
                if await ton_wallet.get_balance_ton() < qty + 0.2:
                    why_manual = "на кошельке бота не хватает TON"
            except Exception as e:
                why_manual = f"не удалось проверить баланс кошелька ({e!r})"[:120]
        if not why_manual:
            try:
                tx = await ton_wallet.send_ton(addr, qty)
            except fragment.FragmentError as e:                # точно НЕ ушло — можно повторить или отклонить
                db.order_claim(oid, "processing", "review", note=str(e)[:200])
                return await _finish_admin_msg(cb, f"⚠️ TON не отправлены: {html.escape(str(e))}\nМожно повторить или отклонить.",
                                               cb.message.reply_markup)
            except Exception as e:                             # мог уйти — НЕ повторяем
                db.order_claim(oid, "processing", "uncertain", note=repr(e)[:200])
                return await _finish_admin_msg(cb, f"🚨 Неясный итог отправки TON: {html.escape(repr(e))}\n"
                                                   f"Проверьте кошелёк бота в обозревателе. Не повторяйте!")
            db.order_claim(oid, "processing", "paid")
            try:
                ton_oid = db.add_ton_order(uid, addr, qty, float(o["amount"]))
                db.set_ton_order(ton_oid, "done", str(tx)[:60])
            except Exception as e:
                print(f"[store ton order] {e!r}")
                ton_oid = oid
            db.add_sale(uid, "ton", f"{qty:g} TON → {addr[:8]}…", qty, float(o["amount"]),
                        round(qty * db.get_float("cost_ton", 0.0), 2), 0.0)
            _ref_spin(uid)
            try:
                await bot.send_message(uid, f"✅ <b>Заказ #{oid} выполнен!</b>\n\n{qty} GRAM отправлены на адрес:\n"
                                            f"<code>{html.escape(addr)}</code>\n\nСпасибо за покупку!",
                                       reply_markup=K.kb_leave_review("ton", ton_oid))
            except Exception as e:
                print(f"[store notify] {e!r}")
            return await _finish_admin_msg(cb, f"✅ Подтверждено, TON отправлены автоматически ({_adm_name(cb)})")
        # запасной путь: вручную
        db.order_claim(oid, "processing", "paid_manual")
        try:
            await bot.send_message(uid, f"✅ Оплата заказа #{oid} подтверждена. Отправим {qty} GRAM на ваш адрес — "
                                        "вы получите уведомление.")
        except Exception:
            pass
        kb = InlineKeyboardMarkup(inline_keyboard=[[ib("GRAM отправлены", "ok", callback_data=f"so:done:{oid}")]])
        return await _finish_admin_msg(cb, f"✅ Оплата принята ({_adm_name(cb)}). Авто-отправка не сработала: {why_manual}.\n"
                                           f"Отправьте {qty} TON вручную на адрес:\n{html.escape(addr)}", kb)

    if kind == "nft":
        from handlers.nft import _save_delivery_target, _notify_manual_order
        gid = int(o["target"].split(":")[0])
        g = db.nft_get(gid)
        target = json.loads(o.get("note") or "{}")
        _save_delivery_target(gid, target)
        db.order_claim(oid, "processing", "paid_manual")
        db.nft_update(gid, note="Оплачено по чеку, ждёт выдачи")
        u = db.find_user_by_id(uid) or {}
        if g:
            await _notify_manual_order(bot, g, uid, float(o["amount"]), u.get("username"), None)
        try:
            await bot.send_message(uid, f"✅ Оплата заказа #{oid} подтверждена. Администратор передаст NFT получателю — "
                                        "вы получите уведомление.")
        except Exception:
            pass
        return await _finish_admin_msg(cb, f"✅ Оплата принята ({_adm_name(cb)}). Заявка на выдачу NFT отправлена отдельным сообщением.")


@router.callback_query(F.data.startswith("so:done:"), IS_ADMIN)
async def adm_done(cb: CallbackQuery, bot: Bot):
    oid = int(cb.data.split(":")[2])
    o = db.order_get(oid)
    if not o or not db.order_claim(oid, "paid_manual", "paid"):
        return await cb.answer("Уже обработано.", show_alert=True)
    db.add_sale(int(o["user_id"]), "gram", f"{o['title']} → {o['target'][:12]}…", float(o["qty"]),
                float(o["amount"]), float(o["cost"]), 0.0)
    _ref_spin(int(o["user_id"]))
    await cb.answer("Готово")
    try:
        await bot.send_message(int(o["user_id"]), f"✅ <b>Заказ #{oid} выполнен!</b> {html.escape(o['title'])} отправлены на ваш адрес.")
    except Exception:
        pass
    await _finish_admin_msg(cb, f"✅ Выдано ({_adm_name(cb)})")


@router.callback_query(F.data.startswith("so:no:"), IS_ADMIN)
async def adm_no(cb: CallbackQuery, bot: Bot):
    oid = int(cb.data.split(":")[2])
    o = db.order_get(oid)
    if not o or not db.order_claim(oid, "review", "rejected"):
        return await cb.answer("Заказ уже обработан.", show_alert=True)
    if o["kind"] == "nft":                                     # снимаем бронь
        gid = int(o["target"].split(":")[0])
        from handlers.nft import _sync_channel_post, _clear_delivery_target
        if db.nft_update_if_status(gid, "reserved", status="available", buyer_id=None, reserved_at=None, note=""):
            _clear_delivery_target(gid)
            g = db.nft_get(gid)
            if g:
                await _sync_channel_post(bot, g, status="available")
    await cb.answer("Отклонено")
    try:
        await bot.send_message(int(o["user_id"]), f"❌ Оплата заказа #{oid} не подтверждена — платёж не найден или чек не подошёл. "
                                                  "Если вы оплатили, напишите в поддержку и приложите чек.")
    except Exception:
        pass
    await _finish_admin_msg(cb, f"❌ Отклонено ({_adm_name(cb)})")


# ══════════════════ профиль ══════════════════
def _profile_text(uid: int, username: str | None) -> str:
    u = db.get_user(uid, username or "")
    st = db.user_stats(uid)
    reg = (str(u.get("reg_date", "")).split() or ["—"])[0]
    return (f"🐹 <b>Профиль</b>\n"
            f"🕓 С нами с {reg}\n"
            f"✅ Выполнено заказов: {st['orders']}\n"
            f"💵 Потрачено: {st['spent']:g} грн | {st['spent'] / _usd_rate():.2f} $\n"
            f"⭐ Звёзд куплено: {st['stars']}\n"
            f"👑 Месяцев Premium: {st['months']}\n"
            f"🎁 NFT куплено: {st['nft']}")


def _profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [ib(f"{BONUS_NAME} Бонус", callback_data="st:bonus", style="primary")],
        [ib("Реферальная система", "people", callback_data="st:ref")],
        [ib("Язык / Мова / Language", "globe", callback_data="st:lang")],
        [ib("Назад", "back", callback_data="st:home")]])


@router.message(F.text == B_PROFILE)
async def on_profile(msg: Message, state: FSMContext):
    await state.set_state(None)
    await _screen(msg, _profile_text(msg.from_user.id, msg.from_user.username), _profile_kb(),
                  _photo_or(PROFILE_PHOTO_PATH, MENU_PHOTO_PATH))


@router.callback_query(F.data == "st:lang")
async def on_lang_cb(cb: CallbackQuery):
    await cb.answer()
    await send_lang_chooser(cb.message)


@router.callback_query(F.data == "st:ref")
async def on_ref_cb(cb: CallbackQuery):
    await cb.answer()
    link = f"https://t.me/{BOT_USERNAME}?start={cb.from_user.id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[ib("Назад", "back", callback_data="st:prof")]])
    await _edit(cb, "👥 <b>Реферальная система</b>\n\nПриглашайте друзей — за каждую покупку приглашённого "
                    f"вы получаете <b>+1 бесплатный спин</b> в {BONUS_NAME} Бонус.\n\n🔗 Ваша ссылка:\n{link}", kb)


@router.callback_query(F.data == "st:prof")
async def on_prof_cb(cb: CallbackQuery):
    await cb.answer()
    await _edit(cb, _profile_text(cb.from_user.id, cb.from_user.username), _profile_kb())


# ── бонус: слоты ──
def _bonus_text(uid: int) -> str:
    b = db.bonus_get(uid)
    return (f"🎁 <b>{BONUS_NAME.upper()} БОНУС</b>\n\n"
            "Покупай звёзды ⭐ — получай бесплатные 🎰 спины!\n\n"
            "После покупки тебе автоматически начисляются спины:\n"
            "<blockquote>⭐ 50 → 🎰 1 спин\n⭐ 100 → 🎰 2 спина\n⭐ 500 → 🎰 5 спинов\n"
            "⭐ 1000 → 🎰 10 спинов\n⭐ 2000 → 🎰 20 спинов</blockquote>\n"
            f"🎰 777 — 7 бонусных ⭐, три BAR — 5⭐, три винограда или лимона — 1⭐. Вывод от {WITHDRAW_MIN}⭐.\n\n"
            "Крути и забирай свой бонус 🍀\n\n"
            f"<b>Спинов:</b> {b['spins']}\n<b>Бонусный баланс:</b> {b['stars']}⭐")


def _bonus_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [ib("КРУТИТЬ", callback_data="st:spin", style="primary")],
        [ib("Вывести", "money_out", callback_data="st:wd")]])


@router.callback_query(F.data == "st:bonus")
async def on_bonus(cb: CallbackQuery):
    await cb.answer()
    await _screen(cb.message, _bonus_text(cb.from_user.id), _bonus_kb(), _photo_or(BONUS_PHOTO_PATH, MENU_PHOTO_PATH))


@router.callback_query(F.data == "st:spin")
async def on_spin(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    async with _locks.setdefault(uid + 10**12, asyncio.Lock()):
        b = db.bonus_get(uid)
        if b["spins"] <= 0:
            return await cb.answer("Нет спинов. Покупайте звёзды — спины начисляются автоматически.", show_alert=True)
        db.bonus_set(uid, b["spins"] - 1, b["stars"])
        await cb.answer()
        dice = await bot.send_dice(cb.message.chat.id, emoji="🎰")
        await asyncio.sleep(3.5)                               # дожидаемся конца анимации
        win = SLOT_REWARD.get(dice.dice.value, 0)
        b = db.bonus_get(uid)
        db.bonus_set(uid, b["spins"], b["stars"] + win)
    await bot.send_message(cb.message.chat.id, f"🎰 Выигрыш: <b>+{win}⭐</b>" if win else "🎰 В этот раз не повезло — крутите ещё!")
    await _edit(cb, _bonus_text(uid), _bonus_kb())


@router.callback_query(F.data == "st:wd")
async def on_withdraw(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    async with _locks.setdefault(uid + 10**12, asyncio.Lock()):
        b = db.bonus_get(uid)
        if b["stars"] < WITHDRAW_MIN:
            return await cb.answer(f"Вывод от {WITHDRAW_MIN}⭐. Сейчас: {b['stars']}⭐", show_alert=True)
        db.bonus_set(uid, b["spins"], 0)
    await cb.answer("Заявка отправлена")
    kb = InlineKeyboardMarkup(inline_keyboard=[[ib("Выплачено", "ok", callback_data=f"st:wdok:{uid}:{b['stars']}")]])
    await notify_admins(bot, f"💸 <b>Вывод бонусных звёзд</b>\n👤 {user_label(uid, cb.from_user.username, cb.from_user.first_name)}\n"
                             f"⭐ Сумма: <b>{b['stars']}</b>", kb)
    await _edit(cb, _bonus_text(uid) + "\n\n✅ Заявка на вывод отправлена администратору.", _bonus_kb())


@router.callback_query(F.data.startswith("st:wdok:"), IS_ADMIN)
async def on_withdraw_ok(cb: CallbackQuery, bot: Bot):
    _, _, uid, n = cb.data.split(":")
    await cb.answer("Отмечено")
    try:
        await bot.send_message(int(uid), f"✅ Бонусные {n}⭐ выплачены!")
    except Exception:
        pass
    await _finish_admin_msg(cb, f"✅ Выплачено ({_adm_name(cb)})")


# ══════════════════ админ-команды настроек ══════════════════
@router.message(Command("gram_rate"), IS_ADMIN)
async def cmd_gram_rate(msg: Message):
    p = (msg.text or "").split()
    if len(p) < 2:
        return await msg.answer(f"Курс GRAM сейчас: {db.get_float('gram_rate', 67.0):g} ₴. Изменить: /gram_rate 67")
    db.set_setting("gram_rate", p[1].replace(",", "."))
    await msg.answer(f"✅ 1 GRAM = {p[1]} ₴")


@router.message(Command("gram_auto"), IS_ADMIN)
async def cmd_gram_auto(msg: Message):
    p = (msg.text or "").split()
    if len(p) < 2:
        return await msg.answer(f"Лимит автоотправки TON: {db.get_float('gram_auto_max', 100.0):g}. Заказы больше — вручную. "
                                "Изменить: /gram_auto 500")
    db.set_setting("gram_auto_max", p[1].replace(",", "."))
    await msg.answer(f"✅ Автоотправка TON до {p[1]} за заказ.")


@router.message(Command("usd_rate"), IS_ADMIN)
async def cmd_usd_rate(msg: Message):
    p = (msg.text or "").split()
    if len(p) < 2:
        return await msg.answer(f"Фиксированный курс магазина: 1 $ = {_usd_rate():g} ₴. Пересчёт применяется автоматически.")
    await msg.answer(f"Курс задан фиксированно: 1 $ = {_usd_rate():g} ₴. Изменение через команду отключено.")


@router.message(Command("pack"), IS_ADMIN)
async def cmd_pack(msg: Message):
    p = (msg.text or "").split()
    if len(p) < 3 or not p[1].isdigit():
        return await msg.answer("Цена пакета звёзд: /pack 50 45  (50 звёзд = 45 ₴). Цена 0 — вернуть расчёт по цене за 1 звезду.")
    db.set_setting(f"star_pack:{int(p[1])}", p[2].replace(",", "."))
    await msg.answer(f"✅ {p[1]} звёзд = {p[2]} ₴. Доступные пакеты: {', '.join(map(str, STAR_PACKS))}")


@router.message(Command("rent_text"), IS_ADMIN)
async def cmd_rent_text(msg: Message):
    text = (msg.html_text or "").partition(" ")[2].strip()
    if not text:
        return await msg.answer("Текст раздела «Аренда NFT»: /rent_text ваш текст (HTML разрешён)")
    db.set_setting("rent_text", text)
    await msg.answer("✅ Текст «Аренда NFT» обновлён.")
