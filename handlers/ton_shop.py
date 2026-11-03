"""
handlers/ton_shop.py — «Купить TON»: клиент платит с баланса (₴), бот отправляет TON на его адрес со своего горячего кошелька.
Цену за 1 TON, минимум и максимум задаёт админ («💠 Настройки TON»).

Деньги: баланс списывается ДО отправки. Определённый отказ до broadcast (FragmentError) → автовозврат.
Неясный итог (TON мог уйти) → возврата нет, админам 🚨-алерт, заказ в ton_orders = uncertain.
"""
from __future__ import annotations
import asyncio
import html
import math
import re

from aiogram import Router, F, Bot
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

import ton_wallet
import tondeposit
import tonrate
from fragment import FragmentError, FragmentUncertain
from config import ADMIN_IDS, REF_BONUS
from database import db
import keyboards as K
from keyboards import B_TON, B_TON_PAY, B_TON_CUSTOM
import texts as T
from utils import answer_screen, run_with_dots, notify_admins, user_label, money

router = Router()
_locks: dict[int, asyncio.Lock] = {}
_PACK_RE = re.compile(r"^(\d+(?:\.\d+)?) TON · ")


class TonBuy(StatesGroup):
    qty_custom = State()
    address = State()
    confirm = State()


# ── настройки / цены ───────────────────────────────────────
def unit_price() -> float:
    """Цена 1 TON для клиента = база + наценка продажи (tonrate.py)."""
    return tonrate.sell_price()


def limits() -> tuple[float, float]:
    mn = db.get_float("ton_min_buy", 1.0) or 1.0
    mx = db.get_float("ton_max_buy", 100.0) or 100.0
    return mn, max(mx, mn)


def order_price(qty: float) -> int:
    p = unit_price()
    return math.ceil(qty * p - 1e-9) if p > 0 else 0


def is_available() -> bool:
    return ton_wallet.is_configured() and unit_price() > 0


# ── экраны ────────────────────────────────────────────────
async def show_ton(msg: Message, state: FSMContext):
    await state.set_state(None)
    await state.update_data(screen="ton", ton_order=None)
    if not is_available():
        return await answer_screen(msg, T.TXT_TON_SOON, K.kb_back_only())
    mn, mx = limits()
    kb = K.kb_ton_packs(order_price, tuple(n for n in K.TON_PACKS if mn <= n <= mx))
    await answer_screen(msg, T.txt_ton(unit_price(), db.balance(msg.chat.id), mn, mx), kb)


async def _ask_address(msg: Message, state: FSMContext, qty: float):
    mn, mx = limits()
    if not is_available() or not mn <= qty <= mx or order_price(qty) <= 0:
        await msg.answer("❌ Это количество сейчас недоступно.")
        return await show_ton(msg, state)
    price = order_price(qty)
    await state.set_state(TonBuy.address)
    await state.update_data(screen="ton_addr", ton_order={"qty": qty, "price": price})
    await answer_screen(msg, T.txt_ton_ask_address(qty, price), K.kb_ton_address())


@router.message(F.text == B_TON)
async def on_ton(msg: Message, state: FSMContext):
    await show_ton(msg, state)


@router.message(F.text.regexp(r"^\d+(?:\.\d+)? TON · "))
async def on_ton_pack(msg: Message, state: FSMContext):
    await _ask_address(msg, state, float(_PACK_RE.match(msg.text).group(1)))


@router.message(F.text == B_TON_CUSTOM)
async def on_ton_custom(msg: Message, state: FSMContext):
    if not is_available():
        return await show_ton(msg, state)
    mn, mx = limits()
    await state.set_state(TonBuy.qty_custom)
    await state.update_data(screen="ton")
    await msg.answer(f"Введите количество TON (от {mn:g} до {mx:g}), можно дробное, например 2.5:",
                     reply_markup=K.kb_back_only())


@router.message(TonBuy.qty_custom, F.text)
async def on_ton_custom_value(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip().replace(",", ".")
    mn, mx = limits()
    try:
        qty = round(float(raw), 3)
        assert mn <= qty <= mx
    except (ValueError, AssertionError):
        return await msg.answer(f"❌ Введите число от {mn:g} до {mx:g}:")
    await _ask_address(msg, state, qty)


# ── адрес → подтверждение ────────────────────────────────
@router.message(TonBuy.address, F.text)
async def on_ton_address(msg: Message, state: FSMContext):
    order = (await state.get_data()).get("ton_order")
    if not order:
        return await show_ton(msg, state)
    addr = (msg.text or "").strip()
    if not tondeposit.valid_address(addr):
        return await msg.answer("❌ Это не похоже на TON-адрес. Он выглядит как UQ… или EQ… (48 символов). "
                                "Скопируйте адрес из вашего кошелька и отправьте ещё раз.")
    price = order_price(order["qty"])
    if price <= 0:
        return await show_ton(msg, state)
    await state.set_state(TonBuy.confirm)
    await state.update_data(screen="ton_confirm", ton_order={"qty": order["qty"], "price": price, "address": addr})
    await answer_screen(msg, T.txt_ton_confirm(order["qty"], addr, price), K.kb_ton_confirm(price))


# ── оплата и отправка ────────────────────────────────────
@router.message(TonBuy.confirm, F.text.startswith(B_TON_PAY))
async def on_ton_pay(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    async with _locks.setdefault(uid, asyncio.Lock()):
        order = (await state.get_data()).get("ton_order")
        if not order or "address" not in order:
            return await show_ton(msg, state)
        qty, addr = order["qty"], order["address"]
        mn, mx = limits()
        price = order_price(qty)
        if not is_available() or price <= 0 or not mn <= qty <= mx:
            await msg.answer("❌ Это количество сейчас недоступно.")
            return await show_ton(msg, state)
        if price != order["price"]:                          # админ поменял цену, пока клиент думал
            await state.update_data(ton_order={"qty": qty, "price": price, "address": addr})
            await msg.answer("ℹ️ Цена изменилась — проверьте и подтвердите ещё раз.")
            return await answer_screen(msg, T.txt_ton_confirm(qty, addr, price), K.kb_ton_confirm(price))

        bal = db.balance(uid)
        if bal < price:
            return await answer_screen(msg, T.txt_nofunds(bal, price), K.kb_nofunds())

        await state.update_data(ton_order=None)              # защита от двойного тапа
        await state.set_state(None)
        if not db.try_spend(uid, price):
            return await answer_screen(msg, T.txt_nofunds(db.balance(uid), price), K.kb_nofunds())

        try:
            oid = db.add_ton_order(uid, addr, qty, price)
        except Exception as e:
            db.add_balance(uid, price)                       # заказ не записан → ничего не отправляли
            await notify_admins(bot, f"🚨 Не смог записать TON-заказ в БД: {html.escape(repr(e))}\nВыполните блок «15» из supabase_schema.sql.")
            await msg.answer("⚠️ Ошибка, деньги возвращены на баланс. Попробуйте позже.")
            return await _home(msg, state)

        res, err = await run_with_dots(msg, "⏳ Отправляю TON", ton_wallet.send_ton(addr, qty))
        u = db.find_user_by_id(uid) or {}

        if err is None:                                      # ✅ успех
            db.set_ton_order(oid, "done", str(res)[:60])
            ref_paid = 0.0
            if u.get("referrer_id") and REF_BONUS > 0:
                db.add_balance(u["referrer_id"], REF_BONUS)
                ref_paid = REF_BONUS
            cost = round(qty * db.get_float("cost_ton", 0.0), 2)
            db.add_sale(uid, "ton", f"{qty:g} TON → {addr[:8]}…", qty, price, cost, ref_paid)
            await notify_admins(bot, f"💠 <b>Продажа: TON</b>\n\n👤 {user_label(uid, u.get('username'), msg.from_user.first_name)}\n"
                                     f"💠 {qty:g} TON → <code>{html.escape(addr)}</code>\n💰 {money(price)} · заказ #{oid}")
            await answer_screen(msg, T.txt_ton_done(qty, addr), K.kb_main_menu(uid in ADMIN_IDS))
            await msg.answer("⭐️ Покупка завершена! Оцените товар — выберите от 1 до 5 звёзд.",
                             reply_markup=K.kb_leave_review("ton", oid))
            return

        if isinstance(err, FragmentError):                   # определённый отказ ДО отправки → возврат
            db.add_balance(uid, price)
            db.set_ton_order(oid, "refunded", str(err))
            await notify_admins(bot, f"⚠️ Не удалось отправить TON, клиенту вернули {money(price)}\nзаказ #{oid}, user={uid}, "
                                     f"{qty:g} TON → <code>{html.escape(addr)}</code>\n{html.escape(str(err))}")
            await msg.answer("❌ Не удалось отправить TON. Деньги возвращены на баланс, попробуйте позже.")
            return await _home(msg, state)

        # FragmentUncertain или неожиданная ошибка: TON мог уйти → деньги не возвращаем
        db.set_ton_order(oid, "uncertain", repr(err))
        await notify_admins(bot, f"🚨 <b>TON: неясный итог заказа #{oid}</b>\nuser={uid}, {qty:g} TON → "
                                 f"<code>{html.escape(addr)}</code>, {money(price)}\n{html.escape(repr(err))}\n"
                                 f"Проверьте кошелёк бота в обозревателе и при необходимости верните баланс вручную.")
        await msg.answer("⚠️ Заказ в обработке — подтверждение задерживается. Деньги заморожены, поддержка проверит вручную.")
        await _home(msg, state)


async def _home(msg: Message, state: FSMContext):
    from handlers.shop import show_main
    await show_main(msg, state)


@router.message(TonBuy.confirm, F.text)
async def on_ton_confirm_other(msg: Message):
    await msg.answer("Нажмите «Оплатить TON» или «Отмена» кнопками ⬇️")
