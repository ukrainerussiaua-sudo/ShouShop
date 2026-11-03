"""
handlers/topup.py — пополнение баланса (всё в гривнах).

Способы:
  💳 Перевод на карту — клиент переводит ₴ на карту, жмёт «Я оплатил», шлёт чек (скрин/PDF); админам приходит чек с кнопками
                        «Подтвердить / Другая сумма / Отклонить». Один и тот же чек дважды не принимается.
  🪙 Crypto Bot       — счёт в UAH, зачисление автоматически (payments.watch_pending).
  💠 TON              — любая сумма TON на адрес из админки + ID клиента в комментарии; курс задаёт админ
                        (tondeposit.py: автопоиск + кнопка «Проверить баланс»).
Любое зачисление → billing.credit_topup → клиенту сообщение, админам уведомление «кто и на сколько пополнил».
"""
import asyncio
import html
import re
import time
import uuid

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode

from config import (
    EMOJI_CRYPTOBOT, ADMIN_IDS, SUPPORT_URL, CUR, CARD_NUMBER, CARD_HOLDER, CARD_BANK,
    MIN_TOPUP_DEFAULT, CARD_PAYMENT_TTL_H, MONOBANK_PAYMENT_TTL_MIN,
)
from database import db
from payments import crypto_create_invoice, crypto_is_configured, credit_payment
import monopay
from billing import credit_topup
import tondeposit
import keyboards as K
from keyboards import B_TOPUP, B_CARD, B_CRYPTO, B_TON_TOPUP, ib
import texts as T
from utils import TOPUP_PHOTO, answer_screen, is_banned, notify_admins, user_label, money

router = Router()


class TopUp(StatesGroup):
    method = State()
    amount = State()
    receipt = State()


class AdmCard(StatesGroup):
    amount = State()


# ── настройки ──────────────────────────────────────────────
def card_info() -> tuple[str, str, str]:
    """(номер, получатель, банк): админка важнее .env."""
    return (db.get_setting("card_number", "").strip() or CARD_NUMBER,
            db.get_setting("card_holder", "").strip() or CARD_HOLDER,
            db.get_setting("card_bank", "").strip() or CARD_BANK)


def card_enabled() -> bool:
    n, h, _ = card_info()
    return bool(n and h)


def topup_limits() -> tuple[float, float]:
    mn = db.get_float("min_topup", MIN_TOPUP_DEFAULT) or MIN_TOPUP_DEFAULT
    mx = db.get_float("max_topup", 100000.0) or 100000.0
    return mn, max(mx, mn)


# ── меню ───────────────────────────────────────────────────
async def show_topup_menu(msg: Message, state: FSMContext):
    card, crypto, ton = card_enabled(), crypto_is_configured(), tondeposit.is_enabled()
    if not (card or crypto or ton):
        await state.set_state(None)
        return await msg.answer("⚠️ Пополнение временно недоступно. Напишите в поддержку.",
                                reply_markup=K.kb_main_menu(msg.from_user.id in ADMIN_IDS))
    await state.set_state(TopUp.method)
    await state.update_data(screen="topup", topup_method=None, card_pid=None)
    await answer_screen(msg, T.txt_topup(), K.kb_topup_methods(card, crypto, ton), TOPUP_PHOTO())


@router.message(F.text == B_TOPUP)
async def on_topup_menu(msg: Message, state: FSMContext):
    await show_topup_menu(msg, state)


def _parse_amount(text: str, mn: float, mx: float) -> float | None:
    t = re.sub(r"[^\d.,]", "", text or "").replace(",", ".")
    try:
        v = round(float(t), 2)
    except ValueError:
        return None
    return v if mn <= v <= mx else None


@router.message(TopUp.method, F.text.in_({B_CARD, B_CRYPTO}))
async def on_method(msg: Message, state: FSMContext):
    method = "card" if msg.text == B_CARD else "crypto"
    if (method == "card" and not card_enabled()) or (method == "crypto" and not crypto_is_configured()):
        return await msg.answer("⚠️ Этот способ сейчас недоступен. Выберите другой.")
    mn, _ = topup_limits()
    await state.set_state(TopUp.amount)
    await state.update_data(topup_method=method, screen="topup_amount")
    await answer_screen(msg, T.txt_topup_amount(msg.text, mn), K.kb_topup_amounts())


@router.message(TopUp.method, F.text == B_TON_TOPUP)
async def on_ton_topup(msg: Message, state: FSMContext):
    if not tondeposit.is_enabled():
        return await msg.answer("⚠️ Пополнение через TON сейчас недоступно. Выберите другой способ.")
    uid = msg.from_user.id
    await state.set_state(None)
    await state.update_data(screen="topup")
    await msg.answer(
        T.txt_ton_topup(tondeposit.deposit_address(), uid, tondeposit.deposit_rate()),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [ib("Проверить баланс", "repeat", callback_data="ton_check")],
            [ib("Отменить", "cancel", callback_data="ton_cancel")]]))
    await msg.answer("Меню ⬇️", reply_markup=K.kb_main_menu(uid in ADMIN_IDS))


@router.message(TopUp.method, F.text)
async def on_method_other(msg: Message):
    await msg.answer("Выберите способ пополнения кнопкой ⬇️")


# ── сумма → счёт ──────────────────────────────────────────
@router.message(TopUp.amount, F.text)
async def on_amount(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    method = data.get("topup_method")
    mn, mx = topup_limits()
    amount = _parse_amount(msg.text, mn, mx)
    if amount is None:
        return await msg.answer(f"❌ Введите сумму числом: от {money(mn)} до {money(mx)}.")
    uid = msg.from_user.id

    if method == "crypto":
        inv_id, pay_url = await crypto_create_invoice(amount, uid)
        if not inv_id or not pay_url:
            return await msg.answer("❌ Не удалось создать счёт Crypto Bot. Попробуйте позже или выберите другой способ.")
        await state.set_state(None)
        db.add_pending(str(inv_id), uid, amount, "crypto")
        await msg.answer(
            f"{EMOJI_CRYPTOBOT} <b>ОПЛАТА ЧЕРЕЗ CRYPTO BOT</b>\n\n🪙 Сумма: <b>{money(amount)}</b>\n\n"
            "Нажмите кнопку, оплатите — баланс пополнится автоматически.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [ib(f"Оплатить {amount:.0f} {CUR}", "dollar", url=pay_url)]]))
        return await msg.answer("Меню ⬇️", reply_markup=K.kb_main_menu(uid in ADMIN_IDS))

    if method == "card":
        if not card_enabled():
            return await msg.answer("⚠️ Оплата картой сейчас недоступна.")
        number, holder, bank = card_info()
        if monopay.is_configured():                       # автопроверка по Monobank API
            pid, total = monopay.create_payment(amount, uid)
            await state.set_state(None)
            await state.update_data(screen="topup")
            await msg.answer(
                T.txt_mono_pay(total, number, holder, bank, MONOBANK_PAYMENT_TTL_MIN),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [ib("Проверить оплату", "ok", callback_data=f"mono_check:{pid}")],
                    [ib("Отмена", "cancel", callback_data=f"mono_cancel:{pid}")],
                    [ib("Написать в поддержку", "chat", url=SUPPORT_URL)]]))
            return await msg.answer("Меню ⬇️", reply_markup=K.kb_main_menu(uid in ADMIN_IDS))
        pid = f"card_{uuid.uuid4().hex[:12]}"
        db.add_pending(pid, uid, amount, "card")
        await state.set_state(None)
        await state.update_data(screen="topup")
        await msg.answer(
            T.txt_card_pay(amount, number, holder, bank, CARD_PAYMENT_TTL_H),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [ib("Я оплатил", "ok", callback_data=f"card_paid:{pid}")],
                [ib("Отмена", "cancel", callback_data=f"card_cancel:{pid}")],
                [ib("Написать в поддержку", "chat", url=SUPPORT_URL)]]))
        return await msg.answer("Меню ⬇️", reply_markup=K.kb_main_menu(uid in ADMIN_IDS))

    await show_topup_menu(msg, state)


# ── карта через Monobank API: «Проверить оплату» ───────────
_checking: set[str] = set()


def _own_mono_pending(pid: str, uid: int) -> dict | None:
    p = db.get_pending(pid)
    if not p or int(p.get("user_id", 0)) != uid or p.get("method") != "monobank":
        return None
    return p


@router.callback_query(F.data.startswith("mono_check:"))
async def mono_check(cb: CallbackQuery, bot: Bot):
    if is_banned(cb.from_user.id):
        return await cb.answer("Заблокировано", show_alert=True)
    pid = cb.data.split(":", 1)[1]
    p = _own_mono_pending(pid, cb.from_user.id)
    if not p:
        return await cb.answer("Заявка не найдена.", show_alert=True)
    if p.get("status") == "paid":
        return await cb.answer("Уже зачислено.", show_alert=True)
    if p.get("status") != "pending" or monopay.is_expired(p):
        return await cb.answer("Время оплаты истекло или заявка закрыта. Создайте новую.", show_alert=True)
    if pid in _checking:
        return await cb.answer("Проверка уже идёт, подождите.", show_alert=True)
    _checking.add(pid)
    try:
        await cb.answer()
        pressed = time.monotonic()
        prog = await cb.message.answer("⏳ Идёт проверка платежа, подождите минуту…")
        res = await monopay.verify(pid, pressed)
        if res == "paid":
            fresh = db.get_pending(pid)
            await credit_payment(bot, fresh, "Карта (Monobank)")
            text = "✅ Платёж найден! Баланс пополнен."
        elif res == "closed":
            text = "✅ Платёж уже обработан — проверьте баланс."
        elif res == "expired":
            text = "⏰ Время оплаты истекло. Создайте новую заявку через «Пополнить»."
        elif res == "pending":
            text = (f"❌ Платёж не найден.\n\nПроверьте, что вы перевели ровно <b>{money(float(p['amount']))}</b> (с копейками) на указанную карту. "
                    f"Банк иногда зачисляет с задержкой — подождите пару минут и нажмите «Проверить оплату» ещё раз.")
        else:
            text = "⚠️ Не удалось связаться с банком. Повторите проверку через минуту."
        try:
            await prog.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            await cb.message.answer(text, parse_mode=ParseMode.HTML)
    finally:
        _checking.discard(pid)


@router.callback_query(F.data.startswith("mono_cancel:"))
async def mono_cancel(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    p = _own_mono_pending(pid, cb.from_user.id)
    if not p:
        return await cb.answer("Заявка не найдена.", show_alert=True)
    if p.get("status") == "pending":
        db.claim_pending(pid, "canceled")
    await cb.answer("Отменено.")
    try:
        await cb.message.edit_text("❌ Заявка на пополнение отменена.")
    except Exception:
        pass


# ── карта: «Я оплатил» → чек ───────────────────────────────
def _own_card_pending(pid: str, uid: int) -> dict | None:
    p = db.get_pending(pid)
    if not p or int(p.get("user_id", 0)) != uid or p.get("method") != "card":
        return None
    return p


@router.callback_query(F.data.startswith("card_paid:"))
async def card_paid(cb: CallbackQuery, state: FSMContext):
    if is_banned(cb.from_user.id):
        return await cb.answer("Заблокировано", show_alert=True)
    pid = cb.data.split(":", 1)[1]
    p = _own_card_pending(pid, cb.from_user.id)
    if not p:
        return await cb.answer("Заявка не найдена.", show_alert=True)
    st = p.get("status")
    if st == "review":
        return await cb.answer("Чек уже отправлен администратору — ожидайте подтверждения.", show_alert=True)
    if st == "paid":
        return await cb.answer("Уже зачислено.", show_alert=True)
    if st != "pending":
        return await cb.answer("Эта заявка больше не активна. Создайте новую.", show_alert=True)
    await state.set_state(TopUp.receipt)
    await state.update_data(card_pid=pid, screen="topup_receipt")
    await cb.answer()
    await cb.message.answer(f"📸 Отправьте <b>скриншот или PDF-чек</b> перевода на {money(float(p['amount']))} одним сообщением.",
                            parse_mode=ParseMode.HTML, reply_markup=K.kb_cancel_only())


@router.callback_query(F.data.startswith("card_cancel:"))
async def card_cancel(cb: CallbackQuery):
    pid = cb.data.split(":", 1)[1]
    p = _own_card_pending(pid, cb.from_user.id)
    if not p:
        return await cb.answer("Заявка не найдена.", show_alert=True)
    if p.get("status") == "review":
        return await cb.answer("Чек уже у администратора — отменить нельзя. Напишите в поддержку.", show_alert=True)
    if p.get("status") == "pending":
        db.claim_pending(pid, "canceled")
    await cb.answer("Отменено.")
    try:
        await cb.message.edit_text("❌ Заявка на пополнение отменена.")
    except Exception:
        pass


@router.message(TopUp.receipt, F.photo | F.document)
async def on_receipt(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    pid = (await state.get_data()).get("card_pid")
    p = _own_card_pending(pid, uid) if pid else None
    if not p or p.get("status") != "pending":
        await state.set_state(None)
        return await msg.answer("⚠️ Заявка не найдена или уже отправлена. Создайте новую через «Пополнить».",
                                reply_markup=K.kb_main_menu(uid in ADMIN_IDS))
    amount = float(p["amount"])
    if msg.photo:
        f = msg.photo[-1]
        file_id, uniq, is_photo = f.file_id, f.file_unique_id, True
    else:
        file_id, uniq, is_photo = msg.document.file_id, msg.document.file_unique_id, False

    u = db.find_user_by_id(uid) or {}
    if not db.claim_receipt(f"tg:{uniq}", uid, amount):          # этот самый файл уже присылали
        await notify_admins(bot, f"🚨 <b>Повторный чек!</b>\n\n👤 {user_label(uid, u.get('username'))} прислал уже "
                                 f"использованный файл на {money(amount)}. Заявка не передана.")
        return await msg.answer("❌ Этот чек уже использовался. Отправьте актуальный чек вашего перевода.")
    if not db.claim_pending_from(pid, "pending", "review"):
        return await msg.answer("⚠️ Чек по этой заявке уже отправлен.")

    await state.set_state(None)
    await state.update_data(card_pid=None, screen="main")
    first = []
    if u.get("username"):
        first.append(ib("Написать клиенту", "chat", url=f"https://t.me/{u['username']}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        first or [ib("Профиль не открыт", "info", callback_data="noop")],
        [ib("Подтвердить", "ok", callback_data=f"card_ok:{pid}"),
         ib("Отклонить", "cancel", callback_data=f"card_no:{pid}")],
        [ib("Другая сумма", "edit", callback_data=f"card_edit:{pid}")]])
    caption = (f"💳 <b>Пополнение картой — проверьте чек</b>\n\n👤 {user_label(uid, u.get('username'), msg.from_user.first_name)}\n"
               f"💰 Заявлено: <b>{money(amount)}</b>\n🧾 Заявка: <code>{pid}</code>")
    n = await notify_admins(bot, caption, kb, **({"photo": file_id} if is_photo else {"document": file_id}))
    await msg.answer("✅ Чек отправлен администратору. Обычно проверка занимает до 15 минут — "
                     "баланс пополнится автоматически после подтверждения.",
                     reply_markup=K.kb_main_menu(uid in ADMIN_IDS))
    if n == 0:
        print(f"[topup] чек {pid}: ни один админ не получил сообщение")


@router.message(TopUp.receipt, F.text)
async def on_receipt_text(msg: Message):
    await msg.answer("Отправьте именно фото/скриншот или файл-чек. Для отмены нажмите «Отмена».")


# ── карта: решения админа ─────────────────────────────────
async def _finish_admin_msg(cb: CallbackQuery, suffix: str):
    base = cb.message.caption or cb.message.text or ""
    try:
        if cb.message.caption is not None or cb.message.photo or cb.message.document:
            await cb.message.edit_caption(caption=f"{html.escape(base)}\n\n{suffix}", reply_markup=None)
        else:
            await cb.message.edit_text(f"{html.escape(base)}\n\n{suffix}", reply_markup=None)
    except Exception as e:
        print(f"[card edit] {e!r}")


def _admin_name(cb: CallbackQuery) -> str:
    return f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer("У клиента нет @username — используйте имя в карточке.", show_alert=True)


@router.callback_query(F.data.startswith("card_ok:"))
async def card_ok(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    pid = cb.data.split(":", 1)[1]
    p = db.get_pending(pid)
    if not p or p.get("method") != "card":
        return await cb.answer("Заявка не найдена.", show_alert=True)
    if not db.claim_pending_from(pid, "review", "paid"):
        return await cb.answer("Заявка уже обработана.", show_alert=True)
    await cb.answer("Зачисляю…")
    await credit_topup(bot, int(p["user_id"]), float(p["amount"]), "Перевод на карту", f"подтвердил {html.escape(_admin_name(cb))}")
    await _finish_admin_msg(cb, f"✅ Подтверждено ({_admin_name(cb)}): +{money(float(p['amount']))}")


@router.callback_query(F.data.startswith("card_no:"))
async def card_no(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    pid = cb.data.split(":", 1)[1]
    p = db.get_pending(pid)
    if not p or p.get("method") != "card":
        return await cb.answer("Заявка не найдена.", show_alert=True)
    if not db.claim_pending_from(pid, "review", "rejected"):
        return await cb.answer("Заявка уже обработана.", show_alert=True)
    await cb.answer("Отклонено")
    try:
        await bot.send_message(int(p["user_id"]), "❌ Пополнение картой не подтверждено — платёж не найден или чек не подошёл. "
                                                  "Если вы уверены, что оплатили — напишите в поддержку и приложите чек.")
    except Exception:
        pass
    await _finish_admin_msg(cb, f"❌ Отклонено ({_admin_name(cb)})")


@router.callback_query(F.data.startswith("card_edit:"))
async def card_edit(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    pid = cb.data.split(":", 1)[1]
    p = db.get_pending(pid)
    if not p or p.get("status") != "review":
        return await cb.answer("Заявка уже обработана.", show_alert=True)
    await state.set_state(AdmCard.amount)
    await state.update_data(adm_card_pid=pid)
    await cb.answer()
    await cb.message.answer(f"Введите фактическую сумму ({CUR}), которую нужно зачислить по заявке <code>{pid}</code>:",
                            parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_back())


@router.message(AdmCard.amount, F.text)
async def card_edit_amount(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id not in ADMIN_IDS:
        return
    try:
        amount = round(float(msg.text.replace(",", ".").replace(CUR, "").strip()), 2)
        assert 0 < amount <= 1_000_000
    except (ValueError, AssertionError):
        return await msg.answer("❌ Введите число больше нуля:")
    pid = (await state.get_data()).get("adm_card_pid")
    p = db.get_pending(pid) if pid else None
    await state.clear()
    if not p or not db.claim_pending_from(pid, "review", "paid"):
        return await msg.answer("Заявка уже обработана.", reply_markup=K.kb_admin_menu())
    await credit_topup(bot, int(p["user_id"]), amount, "Перевод на карту",
                       f"сумма изменена админом (заявлено {money(float(p['amount']))})")
    await msg.answer(f"✅ Зачислено {money(amount)}.", reply_markup=K.kb_admin_menu())


# ── TON: проверить баланс / отменить ─────────────────────
@router.callback_query(F.data == "ton_check")
async def ton_check(cb: CallbackQuery, bot: Bot):
    if is_banned(cb.from_user.id):
        return await cb.answer("Заблокировано", show_alert=True)
    if not tondeposit.is_enabled():
        return await cb.answer("Пополнение через TON сейчас недоступно.", show_alert=True)
    await cb.answer("🔍 Проверяю сеть…")
    uid = cb.from_user.id
    try:
        credited = await tondeposit.scan_once(bot, min_interval=5)
    except Exception as e:
        print(f"[ton_check] {e!r}")
        return await cb.message.answer("⚠️ Не удалось связаться с сетью TON. Повторите проверку через минуту.")
    mine = [c for c in (credited or []) if c["uid"] == uid]
    bal = db.balance(uid)
    if mine:
        tot_ton = sum(c["ton"] for c in mine)
        tot_uah = sum(c["uah"] for c in mine)
        return await cb.message.answer(f"✅ <b>Перевод найден!</b>\n\n💠 {tot_ton:g} TON → +{money(tot_uah)}\n💼 Баланс: <b>{money(bal)}</b>",
                                       parse_mode=ParseMode.HTML)
    last = db.get_ton_deposits(uid, 3)
    hist = ("\n\nПоследние пополнения TON:\n" + "\n".join(f"• {d['created_at']} — {float(d['amount_ton']):g} TON → +{money(float(d['amount_uah']))}" for d in last)) if last else ""
    note = ("⏳ Проверка выполнялась только что — подождите несколько секунд." if credited is None
            else "❌ Новых переводов с вашим ID пока не найдено.")
    await cb.message.answer(f"{note}\nТранзакция может подтверждаться в сети 1–2 минуты. Убедитесь, что в комментарии указан ваш ID: "
                            f"<code>{uid}</code>.\n💼 Баланс: <b>{money(bal)}</b>{hist}", parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "ton_cancel")
async def ton_cancel(cb: CallbackQuery):
    await cb.answer("Отменено.")
    try:
        await cb.message.delete()
    except Exception:
        try:
            await cb.message.edit_text("❌ Пополнение через TON отменено.")
        except Exception:
            pass
