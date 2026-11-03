"""
handlers/promo.py — активация промокода (балансовые и скидочные).
"""
import html
from aiogram import Router, F
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode

from config import DIAMOND, CUR
from database import db
import keyboards as K
from keyboards import B_PROMO, B_BACK
from utils import answer_screen

router = Router()


class Promo(StatesGroup):
    code = State()


@router.message(F.text == B_PROMO)
async def on_promos(msg: Message, state: FSMContext):
    await state.set_state(Promo.code)
    await state.update_data(screen="promo")
    await answer_screen(msg, "💡 <b>АКТИВАЦИЯ ПРОМОКОДА</b>\n\nОтправьте промокод в чат:",
                        K.kb_back_only())


@router.message(Promo.code, F.text)
async def on_promo_input(msg: Message, state: FSMContext):
    code = msg.text.strip()
    safe = html.escape(code)
    uid = msg.from_user.id
    await state.set_state(None)
    await state.update_data(screen="profile")

    promo = db.get_promo(code)
    if not promo:
        return await msg.answer(f"❌ Промокод <b>{safe}</b> не найден, неактивен или просрочен.",
                                parse_mode=ParseMode.HTML, reply_markup=K.kb_profile())
    if promo.get("once_per_user", 1) and db.has_used_promo(code, uid):
        return await msg.answer(f"❌ Вы уже использовали промокод <b>{safe}</b>.",
                                parse_mode=ParseMode.HTML, reply_markup=K.kb_profile())

    if promo.get("type") == "free_account":
        return await msg.answer("❌ Этот тип промокода больше не поддерживается.",
                                reply_markup=K.kb_profile())
    if not db.use_promo(code, uid):          # атомарно: двойной тап / лимит активаций
        return await msg.answer(f"❌ Промокод <b>{safe}</b> уже использован или закончился.",
                                parse_mode=ParseMode.HTML, reply_markup=K.kb_profile())

    if promo.get("type", "balance") == "discount":
        db.set_active_discount(uid, promo["discount"])
        text = (f"✅ <b>Промокод активирован!</b>\n\n{DIAMOND} Скидка {promo['discount']:.0f} {CUR} "
                f"применится к следующей покупке.")
    else:
        db.add_balance(uid, promo["discount"])
        text = (f"✅ <b>Промокод активирован!</b>\n\n{DIAMOND} +{promo['discount']:.0f} {CUR}\n"
                f"🪙 Баланс: {db.balance(uid):.1f} {CUR}")
    await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=K.kb_profile())
