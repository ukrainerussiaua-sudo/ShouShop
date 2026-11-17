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
from keyboards import (A_FRAG_PRICES, A_FRAG_STATUS, A_STAR_PACKS, A_PACK_ADD, A_PACK_DEL, A_PACKS_BACK)
import re
from config import CUR

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))


class AdmFrag(StatesGroup):
    pick = State()
    value = State()
    packs = State()        # список пакетов
    pack_price = State()   # новая цена выбранного пакета
    add_qty = State()      # добавить пакет: количество звёзд
    add_price = State()    # добавить пакет: цена
    pack_del = State()     # удалить пакет


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
    if t == A_STAR_PACKS:
        return await _show_packs(msg, state)
    if t.startswith("1 звезда"):
        key, label, cur = "stars_price", "цену за 1 звезду", db.get_stars_price()
        hint = ("Можно дробное, например 1.65. Цена = количество × эта цена (округляется вверх до целых ₴). "
                "Действует на все пакеты и «Любое количество», КРОМЕ пакетов с отдельной ценой (см. «Пакеты звёзд»).")
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


# ── Пакеты звёзд: список и цены каждого пакета ───────────────
def _pack_price(n: int) -> int:
    from handlers.store import star_price
    return star_price(n)


async def _show_packs(msg: Message, state: FSMContext, note: str = ""):
    await state.set_state(AdmFrag.packs)
    packs = db.get_star_packs()
    per = db.get_stars_price()
    await msg.answer(
        f"{note}⭐ <b>ПАКЕТЫ ЗВЁЗД</b>\n\n"
        f"Нажмите пакет — задать цену. «Добавить пакет» — новая кнопка у клиентов (например 550).\n"
        f"Цена за 1 звезду сейчас: <b>{f'{per:g} {CUR}' if per else 'не задана'}</b> — по ней считаются пакеты "
        f"без своей цены и «Любое количество».",
        parse_mode=ParseMode.HTML,
        reply_markup=K.kb_admin_star_packs([(n, _pack_price(n)) for n in packs]))


@router.message(F.text == A_PACKS_BACK)
async def on_packs_back(msg: Message, state: FSMContext):
    await state.set_state(AdmFrag.pick)
    await _show(msg)


@router.message(AdmFrag.packs, F.text == A_PACK_ADD)
async def on_pack_add(msg: Message, state: FSMContext):
    await state.set_state(AdmFrag.add_qty)
    await msg.answer(f"Сколько звёзд в новом пакете? Целое число от {fragment.STARS_MIN} до 100000.\nНапример: 550",
                     reply_markup=K.kb_admin_back())


@router.message(AdmFrag.add_qty, F.text)
async def on_pack_add_qty(msg: Message, state: FSMContext):
    raw = msg.text.strip()
    if not raw.isdigit() or not fragment.STARS_MIN <= int(raw) <= 100_000:
        return await msg.answer(f"❌ Введите целое число от {fragment.STARS_MIN} до 100000:")
    n = int(raw)
    await state.update_data(new_pack=n)
    await state.set_state(AdmFrag.add_price)
    await msg.answer(f"Цена пакета {n} звёзд в {CUR} (целое или дробное).\n"
                     f"<b>0</b> — считать автоматически: {n} × цена за 1 звезду.",
                     parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_back())


@router.message(AdmFrag.add_price, F.text)
async def on_pack_add_price(msg: Message, state: FSMContext):
    try:
        v = float(msg.text.replace(CUR, "").replace(",", ".").strip())
        assert 0 <= v <= 10_000_000
    except (ValueError, AssertionError):
        return await msg.answer("❌ Введите число ≥ 0:")
    n = (await state.get_data()).get("new_pack")
    if not n:
        return await _show_packs(msg, state)
    db.set_star_packs(db.get_star_packs() + [n])
    db.set_setting(f"star_pack:{n}", f"{v:g}")
    await _show_packs(msg, state, f"✅ Пакет <b>{n}</b> добавлен: <b>{_pack_price(n)} {CUR}</b>\n\n")


@router.message(AdmFrag.packs, F.text == A_PACK_DEL)
async def on_pack_del(msg: Message, state: FSMContext):
    await state.set_state(AdmFrag.pack_del)
    await msg.answer("Какой пакет удалить? Клиенты его больше не увидят.",
                     reply_markup=K.kb_admin_star_packs_del(db.get_star_packs()))


@router.message(AdmFrag.pack_del, F.text.regexp(r"^Удалить (\d+)$"))
async def on_pack_del_pick(msg: Message, state: FSMContext):
    n = int(re.match(r"^Удалить (\d+)$", msg.text).group(1))
    db.set_star_packs([x for x in db.get_star_packs() if x != n])
    db.set_setting(f"star_pack:{n}", "0")
    await _show_packs(msg, state, f"🗑 Пакет <b>{n}</b> удалён.\n\n")


@router.message(AdmFrag.packs, F.text.regexp(r"^Пакет (\d+) · "))
async def on_pack_pick(msg: Message, state: FSMContext):
    n = int(re.match(r"^Пакет (\d+) · ", msg.text).group(1))
    await state.update_data(pack_n=n)
    await state.set_state(AdmFrag.pack_price)
    await msg.answer(f"Пакет <b>{n} звёзд</b>. Сейчас: <b>{_pack_price(n)} {CUR}</b>\n\n"
                     f"Введите новую цену в {CUR}.\n<b>0</b> — убрать свою цену (считать как {n} × цена за 1 звезду).",
                     parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_back())


@router.message(AdmFrag.pack_price, F.text)
async def on_pack_price(msg: Message, state: FSMContext):
    try:
        v = float(msg.text.replace(CUR, "").replace(",", ".").strip())
        assert 0 <= v <= 10_000_000
    except (ValueError, AssertionError):
        return await msg.answer("❌ Введите число ≥ 0:")
    n = (await state.get_data()).get("pack_n")
    if not n:
        return await _show_packs(msg, state)
    db.set_setting(f"star_pack:{n}", f"{v:g}")
    await _show_packs(msg, state, f"✅ {n} звёзд = <b>{_pack_price(n)} {CUR}</b>\n\n")


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
