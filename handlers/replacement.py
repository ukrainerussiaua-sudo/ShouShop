"""
handlers/replacement.py — заявки на замену аккаунта (клиент — меню; админу видео приходит
с inline-кнопками «Подтвердить / Отклонить», т.к. они привязаны к конкретному сообщению).
"""
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode

from config import ADMIN_IDS, CUR
import countries as C
from database import db
import keyboards as K
from keyboards import B_REPLACE, B_CANCEL
from utils import answer_screen

router = Router()


class ReplacementRequest(StatesGroup):
    waiting_account_number = State()
    waiting_issue          = State()
    waiting_issued_at      = State()
    waiting_video          = State()


async def _cancel(msg: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(None)
    await state.update_data(screen="acc")
    await msg.answer("❌ Отменено.", reply_markup=K.kb_purchased())


@router.message(F.text == B_REPLACE)
async def on_replace_start(msg: Message, state: FSMContext):
    pid = (await state.get_data()).get("cur_pid")
    p = db.get_purchase(pid) if pid else None
    if not p or p["user_id"] != msg.from_user.id:
        return await msg.answer("❌ Сначала выберите аккаунт в «Мои аккаунты».")
    if any(x["status"] in ("pending", "approved") for x in db.get_replacements_for_purchase(pid)):
        return await msg.answer("ℹ️ По этому аккаунту заявка уже подана или рассмотрена.",
                                reply_markup=K.kb_purchased())
    await state.update_data(purchase_id=pid)
    await state.set_state(ReplacementRequest.waiting_account_number)
    await answer_screen(msg, "📝 <b>ЗАЯВКА НА ЗАМЕНУ</b>\n\n"
                             "Шаг 1/4 — Введите номер аккаунта (телефон), с которым проблема:",
                        K.kb_cancel_only())


@router.message(ReplacementRequest.waiting_account_number, F.text == B_CANCEL)
@router.message(ReplacementRequest.waiting_issue, F.text == B_CANCEL)
@router.message(ReplacementRequest.waiting_issued_at, F.text == B_CANCEL)
@router.message(ReplacementRequest.waiting_video, F.text == B_CANCEL)
async def on_cancel(msg: Message, state: FSMContext):
    await _cancel(msg, state)


@router.message(ReplacementRequest.waiting_account_number, F.text)
async def on_repl_phone(msg: Message, state: FSMContext):
    await state.update_data(account_number=msg.text.strip())
    await state.set_state(ReplacementRequest.waiting_issue)
    await msg.answer("Шаг 2/4 — Опишите, что не так с аккаунтом:")


@router.message(ReplacementRequest.waiting_issue, F.text)
async def on_repl_issue(msg: Message, state: FSMContext):
    await state.update_data(issue_text=msg.text.strip())
    await state.set_state(ReplacementRequest.waiting_issued_at)
    await msg.answer("Шаг 3/4 — Укажите дату и время выдачи (например: <code>28.06.2026 14:30</code>):",
                     parse_mode=ParseMode.HTML)


@router.message(ReplacementRequest.waiting_issued_at, F.text)
async def on_repl_date(msg: Message, state: FSMContext):
    await state.update_data(issued_at=msg.text.strip())
    await state.set_state(ReplacementRequest.waiting_video)
    await msg.answer("🎥 Шаг 4/4 — <b>Обязательно</b> прикрепите видео-доказательство проблемы.\n\n"
                     "Без видео заявка не будет принята.", parse_mode=ParseMode.HTML)


@router.message(ReplacementRequest.waiting_video)
async def on_repl_video(msg: Message, state: FSMContext, bot: Bot):
    file_id = None
    if msg.video:
        file_id = msg.video.file_id
    elif msg.video_note:
        file_id = msg.video_note.file_id
    elif msg.document and msg.document.mime_type and "video" in msg.document.mime_type:
        file_id = msg.document.file_id
    if not file_id:
        return await msg.answer("❌ Это не видео. Прикрепите видео или нажмите «❌ Отмена».")

    data = await state.get_data()
    await state.set_state(None)
    await state.update_data(screen="acc")
    pid = data["purchase_id"]
    p = db.get_purchase(pid)

    rid = db.add_replacement_request(
        msg.from_user.id, pid, data.get("account_number", ""), data.get("issue_text", ""),
        data.get("issued_at", ""), file_id)

    await msg.answer("✅ <b>Заявка отправлена!</b>\n\nЕсли подтвердится — деньги вернутся на баланс.",
                     parse_mode=ParseMode.HTML, reply_markup=K.kb_purchased())

    ci = (C.get(p["country_code"]) or {}) if p else {}
    caption = (
        f"📝 <b>ЗАЯВКА НА ЗАМЕНУ #{rid}</b>\n\n"
        f"👤 {msg.from_user.id} (@{msg.from_user.username or 'нет'})\n"
        f"🌍 {ci.get('flag', '')} {ci.get('name', p['country_code'] if p else '?')}\n"
        f"💰 Сумма заказа: {float(p['price_paid'] if p else 0):.0f} {CUR}\n"
        f"🔗 Lolz: <code>{(p or {}).get('logins', '')}</code>\n\n"
        f"📱 Номер: <code>{data.get('account_number', '')}</code>\n"
        f"⚠️ Проблема: {data.get('issue_text', '')}\n"
        f"⏰ Выдан: {data.get('issued_at', '')}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"repl_ok_{rid}"),
        InlineKeyboardButton(text="❌ Отклонить",  callback_data=f"repl_no_{rid}"),
    ]])
    for aid in ADMIN_IDS:
        try:
            await bot.send_video(aid, file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception as e:
            print(f"[Replacement] send to admin {aid}: {e}")


@router.callback_query(F.data.startswith("repl_ok_"))
async def on_repl_approve(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer()
    rid = int(callback.data[len("repl_ok_"):])
    req = db.get_replacement_request(rid)
    if not req or req["status"] != "pending":
        return await callback.answer("Уже обработана", show_alert=True)
    p = db.get_purchase(req["purchase_id"])
    refund = float(p["price_paid"]) if p else 0.0
    already = any(x["id"] != rid and x["status"] == "approved"
                  for x in db.get_replacements_for_purchase(req["purchase_id"]))
    if already:
        db.claim_replacement(rid, "rejected")
        return await callback.answer("По этой покупке уже был возврат", show_alert=True)
    if not db.claim_replacement(rid, "approved"):      # атомарно: второй клик/второй админ не сработает
        return await callback.answer("Уже обработана", show_alert=True)
    db.add_balance(req["user_id"], refund)
    if p:
        db.set_purchase_status(p["id"], "refunded")      # аккаунт пропадает из «Мои аккаунты»
    try:
        await bot.send_message(req["user_id"],
                               f"✅ Заявка #{rid} подтверждена! Возврат: +{refund:.0f} {CUR} на баланс.")
    except Exception:
        pass
    await callback.message.edit_caption(
        caption=(callback.message.caption or "") + f"\n\n✅ ПОДТВЕРЖДЕНО (+{refund:.0f} {CUR})",
        parse_mode=ParseMode.HTML, reply_markup=None)
    await callback.answer("Подтверждено")


@router.callback_query(F.data.startswith("repl_no_"))
async def on_repl_reject(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer()
    rid = int(callback.data[len("repl_no_"):])
    req = db.get_replacement_request(rid)
    if not req or req["status"] != "pending":
        return await callback.answer("Уже обработана", show_alert=True)
    if not db.claim_replacement(rid, "rejected"):
        return await callback.answer("Уже обработана", show_alert=True)
    try:
        await bot.send_message(req["user_id"], f"❌ Заявка #{rid} отклонена.")
    except Exception:
        pass
    await callback.message.edit_caption(
        caption=(callback.message.caption or "") + "\n\n❌ ОТКЛОНЕНО",
        parse_mode=ParseMode.HTML, reply_markup=None)
    await callback.answer("Отклонено")
