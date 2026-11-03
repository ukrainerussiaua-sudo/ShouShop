"""
handlers/fragment_admin.py — админка Stars/Premium: цены и проверка Fragment/TON.
"""
from aiogram import Router, F
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode

import fragment
import ton_wallet
from config import ADMIN_IDS
from database import db
import keyboards as K
from keyboards import A_FRAG_PRICES, A_FRAG_STATUS
from config import CUR

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))


class AdmFrag(StatesGroup):
    pick = State()
    value = State()


def _premium() -> dict[int, int]:
    return {m: db.get_premium_price(m) for m in fragment.PREMIUM_MONTHS}


async def _show(msg: Message, note: str = ""):
    await msg.answer(
        f"{note}⭐ <b>ЦЕНЫ STARS / PREMIUM</b>\n\nВыберите позицию, чтобы задать цену (₴).\n"
        f"«—» = не задана → клиенты товар не видят.",
        parse_mode=ParseMode.HTML,
        reply_markup=K.kb_admin_frag_prices(db.get_stars_price(), _premium()))


@router.message(F.text == A_FRAG_PRICES)
async def on_prices(msg: Message, state: FSMContext):
    await state.set_state(AdmFrag.pick)
    await _show(msg)


@router.message(AdmFrag.pick, F.text)
async def on_pick(msg: Message, state: FSMContext):
    t = msg.text
    if t.startswith("1 звезда"):
        key, label, cur = "stars_price", "цену за 1 звезду", db.get_stars_price()
        hint = "Можно дробное, например 1.65. Пакет округляется вверх до целых ₴."
    elif t.startswith("Premium "):
        try:
            months = int(t.split()[1])
            assert months in fragment.PREMIUM_MONTHS
        except (IndexError, ValueError, AssertionError):
            return await msg.answer("Выберите позицию кнопкой ⬇️")
        key, label, cur = f"premium_{months}", f"цену Premium на {months} мес", db.get_premium_price(months)
        hint = "Целое число ₴."
    else:
        return await msg.answer("Выберите позицию кнопкой ⬇️")
    await state.update_data(frag_key=key, frag_label=label)
    await state.set_state(AdmFrag.value)
    await msg.answer(f"Сейчас: {cur:g} {CUR}\n\nВведите {label} в {CUR}. <b>0</b> — выключить товар.\n{hint}",
                     parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_back())


@router.message(AdmFrag.value, F.text)
async def on_value(msg: Message, state: FSMContext):
    try:
        v = float(msg.text.replace(CUR, "").replace(",", ".").strip())
        assert 0 <= v <= 1_000_000
    except (ValueError, AssertionError):
        return await msg.answer("❌ Введите число ≥ 0:")
    d = await state.get_data()
    key = d["frag_key"]
    if key != "stars_price":
        v = int(round(v))
    db.set_setting(key, f"{v:g}")
    await state.set_state(AdmFrag.pick)
    await _show(msg, f"✅ {d['frag_label']}: <b>{v:g} {CUR}</b>\n\n")


@router.message(F.text == A_FRAG_STATUS)
async def on_status(msg: Message):
    if not fragment.is_configured():
        return await msg.answer("🔌 Fragment не настроен: заполните FRAGMENT_STEL_* и TON_MNEMONIC в .env.")
    ok, detail = await fragment.session_valid()
    lines = [f"🔌 <b>Fragment</b>: {'✅ страница/API доступны' if ok else '❌ ' + detail}"]
    if ok:
        lines.append(f"ℹ️ {detail}")
    try:
        status = await ton_wallet.get_wallet_status()
        state = status["account_state"]
        state_labels = {
            "active": "✅ активен",
            "uninitialized": "🟡 не развёрнут — активируется автоматически перед первой покупкой",
            "nonexist": "🟡 ещё не создан — активируется автоматически после пополнения",
            "frozen": "⛔ заморожен — автоматическая отправка остановлена",
            "unknown": "❔ состояние не сообщено TON Center",
        }
        state_text = state_labels.get(state, state)
        lines.append(
            f"👛 Кошелёк: <code>{status['address']}</code>\n"
            f"💎 Баланс: <b>{status['balance_ton']:.3f} TON</b>\n"
            f"📡 Состояние: <b>{state_text}</b> · seqno: <code>{status['seqno']}</code>"
        )
    except Exception as e:
        lines.append(f"👛 Кошелёк: ❌ {e!r}")
    lines.append(f"⭐ Цена звезды: {db.get_stars_price():g} {CUR}")
    await msg.answer("\n".join(lines), parse_mode=ParseMode.HTML)
