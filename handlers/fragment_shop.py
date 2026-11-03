"""
handlers/fragment_shop.py — продажа Telegram Stars и Premium (через Fragment, см. fragment.py).
Каталог → ⭐ Stars / 💎 Premium → пакет → @username → подтверждение → списание с баланса → Fragment.
Деньги: списываются перед покупкой; при ОПРЕДЕЛЁННОМ отказе (до отправки TON) — возвращаются;
при неясном итоге (TON мог уйти) — не возвращаются автоматически, админам уходит алерт.
"""
from __future__ import annotations
import asyncio
import math
import re

from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode

import fragment
from config import ADMIN_IDS, REF_BONUS, CUR
from database import db
import keyboards as K
from keyboards import B_STARS, B_PREMIUM, B_FRAG_PAY, B_FRAG_CUSTOM, B_FRAG_SELF
import texts as T
from utils import answer_screen, run_with_dots, STARS_PHOTO, PREMIUM_PHOTO, notify_admins, user_label, money

router = Router()
_locks: dict[int, asyncio.Lock] = {}


class Frag(StatesGroup):
    qty_custom = State()
    username = State()
    confirm = State()


# ── цены ──────────────────────────────────────────────────
def stars_price(qty: int) -> int:
    p = db.get_stars_price()
    return math.ceil(qty * p) if p > 0 else 0


def premium_prices() -> dict[int, int]:
    return {m: db.get_premium_price(m) for m in fragment.PREMIUM_MONTHS}


def _price(kind: str, qty: int) -> int:
    return stars_price(qty) if kind == "stars" else db.get_premium_price(qty)


def _cost(kind: str, qty: int) -> float:
    """Себестоимость заказа в ₴ — из админки («Себестоимость»); не задана → 0 (чистая = грязная)."""
    if kind == "stars":
        return round(qty * db.get_float("cost_star", 0.0), 2)
    return round(db.get_float(f"cost_premium_{qty}", 0.0), 2)


def _title(kind: str, qty: int) -> str:
    return f"⭐ {qty} Telegram Stars" if kind == "stars" else f"💎 Telegram Premium · {qty} мес"


async def _alert_admins(bot: Bot, text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            pass


# ── экраны ────────────────────────────────────────────────
async def show_stars(msg: Message, state: FSMContext):
    await state.set_state(None)
    await state.update_data(screen="stars", order=None)
    if not fragment.is_configured() or db.get_stars_price() <= 0:
        return await answer_screen(msg, T.TXT_FRAG_SOON, K.kb_back_only(), STARS_PHOTO())
    await answer_screen(msg, T.txt_stars(), K.kb_stars(stars_price), STARS_PHOTO())


async def show_premium(msg: Message, state: FSMContext):
    await state.set_state(None)
    await state.update_data(screen="premium", order=None)
    prices = premium_prices()
    if not fragment.is_configured() or not any(prices.values()):
        return await answer_screen(msg, T.TXT_FRAG_SOON, K.kb_back_only(), PREMIUM_PHOTO())
    await answer_screen(msg, T.txt_premium(), K.kb_premium(prices), PREMIUM_PHOTO())


async def _ask_username(msg: Message, state: FSMContext, kind: str, qty: int):
    if _price(kind, qty) <= 0:
        await msg.answer("❌ Этот товар сейчас недоступен.")
        return await (show_stars(msg, state) if kind == "stars" else show_premium(msg, state))
    await state.set_state(Frag.username)
    await state.update_data(screen="frag_user", order={"kind": kind, "qty": qty})
    await answer_screen(msg, T.txt_frag_ask_user(_title(kind, qty)),
                        K.kb_frag_username(msg.from_user.username))


@router.message(F.text == B_STARS)
async def on_stars(msg: Message, state: FSMContext):
    await show_stars(msg, state)


@router.message(F.text == B_PREMIUM)
async def on_premium(msg: Message, state: FSMContext):
    await show_premium(msg, state)


@router.message(F.text.regexp(r"^(\d+) звёзд · "))
async def on_stars_pack(msg: Message, state: FSMContext):
    qty = int(re.match(r"^(\d+) звёзд · ", msg.text).group(1))
    if qty not in K.STAR_PACKS:
        return await show_stars(msg, state)
    await _ask_username(msg, state, "stars", qty)


@router.message(F.text == B_FRAG_CUSTOM)
async def on_stars_custom(msg: Message, state: FSMContext):
    if stars_price(fragment.STARS_MIN) <= 0:
        return await show_stars(msg, state)
    await state.set_state(Frag.qty_custom)
    await state.update_data(screen="stars")
    await msg.answer(f"Введите количество звёзд (от {fragment.STARS_MIN} до 100000):",
                     reply_markup=K.kb_back_only())


@router.message(Frag.qty_custom, F.text)
async def on_stars_custom_value(msg: Message, state: FSMContext):
    raw = msg.text.strip()
    if not raw.isdigit() or not fragment.STARS_MIN <= int(raw) <= 100_000:
        return await msg.answer(f"❌ Введите целое число от {fragment.STARS_MIN} до 100000:")
    await _ask_username(msg, state, "stars", int(raw))


@router.message(F.text.regexp(r"^(\d+) мес · "))
async def on_premium_pick(msg: Message, state: FSMContext):
    months = int(re.match(r"^(\d+) мес", msg.text).group(1))
    if months not in fragment.PREMIUM_MONTHS:
        return await show_premium(msg, state)
    await _ask_username(msg, state, "premium", months)


# ── получатель → подтверждение ───────────────────────────
@router.message(Frag.username, F.text)
async def on_username(msg: Message, state: FSMContext, bot: Bot):
    order = (await state.get_data()).get("order")
    if not order:
        return await show_stars(msg, state)
    raw = msg.from_user.username if msg.text == B_FRAG_SELF else msg.text
    target = fragment.normalize_username(raw or "")
    if not target:
        return await msg.answer("❌ Это не похоже на @username. Пример: @durov (минимум 5 символов).")
    kind, qty = order["kind"], order["qty"]
    price = _price(kind, qty)
    if price <= 0:
        return await show_stars(msg, state)

    found, err = await run_with_dots(msg, "🔎 Проверяю получателя", fragment.check_recipient(kind, target, qty))
    if isinstance(err, fragment.RecipientError):
        return await msg.answer(f"❌ {err}\nПроверьте @username и отправьте ещё раз.")
    if err is not None:
        print(f"[fragment check] {err!r}")
        await _alert_admins(bot, f"⚠️ Fragment: проверка получателя не удалась\n{err!r}\n"
                                 "Возможно, протухли cookies (см. «🔌 Fragment / TON»).")
        await msg.answer("⚠️ Сервис временно недоступен. Попробуйте позже.")
        return await (show_stars(msg, state) if kind == "stars" else show_premium(msg, state))

    await state.set_state(Frag.confirm)
    await state.update_data(screen="frag_confirm",
                            order={"kind": kind, "qty": qty, "target": target, "price": price})
    await answer_screen(msg, T.txt_frag_confirm(_title(kind, qty), target, found.get("name", ""), price),
                        K.kb_frag_confirm(price))


# ── оплата ───────────────────────────────────────────────
@router.message(Frag.confirm, F.text.startswith(B_FRAG_PAY))
async def on_pay(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    async with _locks.setdefault(uid, asyncio.Lock()):
        order = (await state.get_data()).get("order")
        if not order or "target" not in order:
            return await show_stars(msg, state)
        kind, qty, target = order["kind"], order["qty"], order["target"]
        price = _price(kind, qty)
        if price <= 0:
            await msg.answer("❌ Этот товар сейчас недоступен.")
            return await show_stars(msg, state)
        if price != order["price"]:                       # админ поменял цену, пока клиент думал
            await msg.answer("ℹ️ Цена изменилась — подтвердите ещё раз.")
            return await _ask_username(msg, state, kind, qty)

        bal = db.balance(uid)
        if bal < price:
            return await answer_screen(msg, T.txt_nofunds(bal, price), K.kb_nofunds())

        await state.update_data(order=None)               # защита от двойного тапа
        await state.set_state(None)
        if not db.try_spend(uid, price):
            return await answer_screen(msg, T.txt_nofunds(db.balance(uid), price), K.kb_nofunds())

        title = _title(kind, qty)
        try:
            oid = db.add_fragment_order(uid, kind, qty, target, price)
        except Exception as e:
            db.add_balance(uid, price)                    # заказ не записан → ничего не покупали
            await _alert_admins(bot, f"🚨 Не смог записать fragment-заказ в БД: {e!r}")
            await msg.answer("⚠️ Ошибка, деньги возвращены на баланс. Попробуйте ещё раз.")
            return await show_main_safe(msg, state)

        buy = fragment.buy_stars(target, qty) if kind == "stars" else fragment.gift_premium(target, qty)
        res, err = await run_with_dots(msg, "⏳ Отправляю заказ", buy)

        if err is None:                                   # ✅ успех
            db.set_fragment_order(oid, "done", res.get("req_id", ""))
            u = db.find_user_by_id(uid) or {}
            ref_paid = 0.0
            if u.get("referrer_id") and REF_BONUS > 0:
                db.add_balance(u["referrer_id"], REF_BONUS)
                ref_paid = REF_BONUS
            cost = _cost(kind, qty)
            db.add_sale(uid, kind, f"{title} → @{target}", qty, price, cost, ref_paid)
            await notify_admins(bot, f"✅ <b>Продажа: {title}</b>\n\n👤 {user_label(uid, u.get('username'), msg.from_user.first_name)}\n"
                                     f"🎯 Получатель: @{target}\n💰 {money(price)} · себестоимость {money(cost)} · "
                                     f"прибыль {money(price - cost - ref_paid)} (заказ #{oid})")
            await answer_screen(msg, T.txt_frag_done(title, target), K.kb_main_menu(uid in ADMIN_IDS))
            await msg.answer("⭐️ Покупка завершена! Оцените товар — выберите от 1 до 5 звёзд.",
                             reply_markup=K.kb_leave_review("fragment", oid))
            return

        if isinstance(err, fragment.FragmentError):      # определённый отказ до отправки TON
            db.add_balance(uid, price)
            db.set_fragment_order(oid, "refunded", str(err))
            print(f"[fragment refund] #{oid} {err!r}")
            if not isinstance(err, fragment.RecipientError):
                await _alert_admins(bot, f"⚠️ Fragment отказал, клиенту вернули {price} {CUR}\n"
                                         f"заказ #{oid}, user={uid}, {title} → @{target}\n{err}")
            await msg.answer(f"❌ Не удалось выполнить заказ: {err}\nДеньги возвращены на баланс."
                             if isinstance(err, fragment.RecipientError) else
                             "❌ Не удалось выполнить заказ. Деньги возвращены на баланс, попробуйте позже.")
            return await show_main_safe(msg, state)

        # FragmentUncertain или неожиданная ошибка: TON мог уйти → деньги не возвращаем
        db.set_fragment_order(oid, "uncertain", repr(err))
        await _alert_admins(bot, f"🚨 Fragment: неясный итог заказа #{oid}\nuser={uid}, {title} → @{target}, "
                                 f"{price} {CUR}\n{err!r}\nПроверьте кошелёк/Fragment и при необходимости "
                                 f"верните баланс вручную.")
        await msg.answer("⚠️ Заказ в обработке — подтверждение задерживается. Деньги заморожены, "
                         "поддержка проверит заказ вручную.")
        await show_main_safe(msg, state)


async def show_main_safe(msg: Message, state: FSMContext):
    from handlers.shop import show_main
    await show_main(msg, state)


@router.message(Frag.confirm, F.text)
async def on_confirm_other(msg: Message):
    await msg.answer("Нажмите «✅ Оплатить» или «❌ Отмена» кнопками ⬇️")
