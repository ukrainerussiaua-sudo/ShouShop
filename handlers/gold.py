"""
handlers/gold.py — раздел «Голда»: покупка / продажа по заявке.

Клиент: Каталог → Голда → «Купить Голду» / «Продать Голду» → сумма → комментарий (или «Пропустить»)
        → проверка → «Отправить заявку» → «Заявка №N отправлена».
Админы: всем приходит карточка заявки (тип, сумма, комментарий, клиент) с кнопками
        «Написать клиенту», «Ответить в боте», «В работу / Выполнено / Отклонить».
Ответ админа «в боте» уходит клиенту с кнопкой «Ответить» — получается переписка по заявке прямо в боте.
"""
from __future__ import annotations
import html
import re

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode

from config import ADMIN_IDS, SPARKLE_STARS
from database import db
import keyboards as K
from keyboards import B_GOLD, B_GOLD_BUY, B_GOLD_SELL, B_SKIP, B_GOLD_SEND, ib
import texts as T
from utils import answer_screen, notify_admins, user_label

router = Router()

KIND_T = {"buy": "🛒 Покупка Голды", "sell": "💰 Продажа Голды"}
STATUS_T = {"new": "🆕 Новая", "in_work": "🟡 В работе", "done": "✅ Выполнена", "rejected": "❌ Отклонена"}
MAX_OPEN_PER_USER = 5
_AMOUNT_RE = re.compile(r"^\d+(?:\.\d+)?$")


class Gold(StatesGroup):
    amount = State()
    comment = State()
    confirm = State()


class GoldMsg(StatesGroup):
    user_reply = State()
    admin_reply = State()


def rate_for(kind: str) -> float:
    """Необязательный ориентир курса для заявки; если значение не задано, цена не показывается."""
    return max(0.0, db.get_float("gold_buy_rate" if kind == "buy" else "gold_sell_rate", 0.0))


# ═════════════════════════════════════════════════════════
# Карточка заявки для админов
# ═════════════════════════════════════════════════════════
def card_text(r: dict, admin_name: str = "") -> str:
    rate = rate_for(r["kind"])
    amount = float(r["amount"])
    est = f"\n💱 По курсу: ≈ {T._m(amount * rate)}" if rate > 0 else ""
    comment = html.escape(r.get("comment") or "") or "—"
    who = f"\n👮 {html.escape(admin_name)}" if admin_name else ""
    return (f"📩 <b>Заявка №{r['id']}</b> — {KIND_T.get(r['kind'], r['kind'])}\n\n"
            f"👤 {user_label(int(r['user_id']), r.get('username'))}\n"
            f"💰 Сумма: <b>{amount:g}</b>{est}\n"
            f"💬 Комментарий: {comment}\n"
            f"🕒 {r.get('created_at', '')}\n"
            f"📌 Статус: {STATUS_T.get(r['status'], r['status'])}{who}")


def card_kb(r: dict) -> InlineKeyboardMarkup:
    rid = r["id"]
    first = []
    if r.get("username"):
        first.append(ib("Написать клиенту", "chat", url=f"https://t.me/{r['username']}"))
    first.append(ib("Ответить в боте", "send", callback_data=f"gold_reply:{rid}"))
    rows = [first]
    if r["status"] == "new":
        rows.append([ib("Взять в работу", "clock", callback_data=f"gold_st:{rid}:in_work")])
    if r["status"] in ("new", "in_work"):
        rows.append([ib("Выполнено", "ok", callback_data=f"gold_st:{rid}:done"),
                     ib("Отклонить", "cancel", callback_data=f"gold_st:{rid}:rejected")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_card(bot: Bot, chat_id: int, r: dict):
    """Отправляет одному админу карточку заявки с кнопками управления."""
    try:
        await bot.send_message(chat_id, card_text(r), reply_markup=card_kb(r))
    except Exception as e:
        print(f"[gold card] {e!r}")
        await bot.send_message(chat_id, card_text(r))


# ═════════════════════════════════════════════════════════
# Клиент: раздел и мастер заявки
# ═════════════════════════════════════════════════════════
async def show_gold(msg: Message, state: FSMContext):
    await state.set_state(None)
    await state.update_data(screen="gold", gold=None)
    await answer_screen(msg, T.txt_gold(), K.kb_gold_menu())


@router.message(F.text == B_GOLD)
async def on_gold(msg: Message, state: FSMContext):
    await show_gold(msg, state)


@router.message(F.text.in_({B_GOLD_BUY, B_GOLD_SELL}))
async def on_gold_kind(msg: Message, state: FSMContext):
    kind = "buy" if msg.text == B_GOLD_BUY else "sell"
    await state.set_state(Gold.amount)
    await state.update_data(screen="gold_amount", gold={"kind": kind})
    await answer_screen(msg, T.txt_gold_amount(kind, rate_for(kind)), K.kb_back_only())


@router.message(Gold.amount, F.text)
async def on_gold_amount(msg: Message, state: FSMContext):
    raw = re.sub(r"[\s\u00a0]", "", msg.text or "").replace(",", ".")
    if not _AMOUNT_RE.match(raw) or not (0 < float(raw) <= 1_000_000_000):
        return await msg.answer("❌ Введите сумму числом, например: 1500")
    data = await state.get_data()
    g = dict(data.get("gold") or {})
    g["amount"] = float(raw)
    await state.set_state(Gold.comment)
    await state.update_data(screen="gold_comment", gold=g)
    await answer_screen(msg, T.txt_gold_comment(), K.kb_gold_comment())


@router.message(Gold.comment, F.text)
async def on_gold_comment(msg: Message, state: FSMContext):
    data = await state.get_data()
    g = dict(data.get("gold") or {})
    if "amount" not in g:
        return await show_gold(msg, state)
    g["comment"] = "" if msg.text == B_SKIP else msg.text.strip()[:500]
    await state.set_state(Gold.confirm)
    await state.update_data(screen="gold_confirm", gold=g)
    await answer_screen(msg, T.txt_gold_confirm(g["kind"], g["amount"], g["comment"], rate_for(g["kind"])),
                        K.kb_gold_confirm())


@router.message(Gold.confirm, F.text == B_GOLD_SEND)
async def on_gold_send(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    g = data.get("gold") or {}
    if "amount" not in g or "kind" not in g:
        return await show_gold(msg, state)
    uid = msg.from_user.id
    await state.set_state(None)                       # защита от двойного нажатия
    await state.update_data(gold=None, screen="main")
    if db.count_open_gold(uid) >= MAX_OPEN_PER_USER:
        await msg.answer("⚠️ У вас уже много открытых заявок. Дождитесь ответа администратора.",
                         reply_markup=K.kb_main_menu(uid in ADMIN_IDS))
        return
    try:
        rid = db.add_gold_request(uid, msg.from_user.username or "", g["kind"], g["amount"], g.get("comment", ""))
    except Exception as e:
        print(f"[gold] не записал заявку: {e!r}")
        await msg.answer("⚠️ Не удалось создать заявку. Попробуйте ещё раз чуть позже.",
                         reply_markup=K.kb_main_menu(uid in ADMIN_IDS))
        return
    r = db.get_gold_request(rid)
    await answer_screen(msg, T.txt_gold_sent(rid), K.kb_main_menu(uid in ADMIN_IDS))
    if r:
        n = await notify_admins(bot, card_text(r), card_kb(r))
        if n == 0:
            print(f"[gold] заявка №{rid}: ни один админ не получил уведомление (проверьте ADMIN_IDS и /start у админов)")


@router.message(Gold.confirm, F.text)
async def on_gold_confirm_other(msg: Message):
    await msg.answer("Нажмите «Отправить заявку» или «Отмена» кнопками ⬇️")


# ═════════════════════════════════════════════════════════
# Админ: статусы и ответы
# ═════════════════════════════════════════════════════════
_STATUS_RULES = {"in_work": ("new",), "done": ("new", "in_work"), "rejected": ("new", "in_work")}
_USER_NOTE = {
    "in_work": "🟡 Заявка №{id} взята в работу. Скоро с вами свяжутся.",
    "done": "✅ Заявка №{id} выполнена. Спасибо, что выбрали нас!",
    "rejected": "❌ Заявка №{id} отклонена. Если есть вопросы — напишите в поддержку.",
}


@router.callback_query(F.data.startswith("gold_st:"))
async def on_gold_status(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    try:
        _, rid_s, new_status = cb.data.split(":")
        rid = int(rid_s)
        assert new_status in _STATUS_RULES
    except (ValueError, AssertionError):
        return await cb.answer("Некорректная кнопка", show_alert=True)
    if not db.claim_gold_status(rid, _STATUS_RULES[new_status], new_status, cb.from_user.id):
        return await cb.answer("Заявка уже обработана другим админом или закрыта.", show_alert=True)
    r = db.get_gold_request(rid)
    await cb.answer(STATUS_T[new_status])
    who = cb.from_user.username and f"@{cb.from_user.username}" or cb.from_user.full_name
    try:
        await cb.message.edit_text(card_text(r, who), reply_markup=card_kb(r), parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"[gold status edit] {e!r}")
    try:
        await bot.send_message(int(r["user_id"]), _USER_NOTE[new_status].format(id=rid))
    except Exception:
        pass


@router.callback_query(F.data.startswith("gold_reply:"))
async def on_gold_reply_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("Нет доступа", show_alert=True)
    rid = int(cb.data.split(":")[1])
    r = db.get_gold_request(rid)
    if not r:
        return await cb.answer("Заявка не найдена", show_alert=True)
    await state.set_state(GoldMsg.admin_reply)
    await state.update_data(reply_rid=rid)
    await cb.answer()
    await cb.message.answer(f"✍️ Напишите ответ клиенту по заявке №{rid} — он придёт ему в бот.\n"
                            f"Чтобы отменить, нажмите «В админку».", reply_markup=K.kb_admin_back())


@router.message(GoldMsg.admin_reply, F.text)
async def on_gold_admin_reply(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id not in ADMIN_IDS:
        return
    rid = (await state.get_data()).get("reply_rid")
    r = db.get_gold_request(rid) if rid else None
    await state.clear()
    if not r:
        return await msg.answer("❌ Заявка не найдена.", reply_markup=K.kb_admin_menu())
    try:
        await bot.send_message(
            int(r["user_id"]),
            f"💬 <b>Ответ по заявке №{rid}</b>\n\n{html.escape(msg.text)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [ib("Ответить", "send", callback_data=f"gold_ur:{rid}")]]))
        await msg.answer(f"✅ Ответ по заявке №{rid} отправлен клиенту.", reply_markup=K.kb_admin_menu())
    except Exception as e:
        await msg.answer(f"❌ Не удалось доставить (клиент мог заблокировать бота): {html.escape(repr(e))}",
                         reply_markup=K.kb_admin_menu())


@router.callback_query(F.data.startswith("gold_ur:"))
async def on_gold_user_reply_start(cb: CallbackQuery, state: FSMContext):
    rid = int(cb.data.split(":")[1])
    r = db.get_gold_request(rid)
    if not r or int(r["user_id"]) != cb.from_user.id:
        return await cb.answer("Заявка не найдена", show_alert=True)
    await state.set_state(GoldMsg.user_reply)
    await state.update_data(reply_rid=rid, screen="gold_msg")
    await cb.answer()
    await cb.message.answer(f"✍️ Напишите сообщение администратору по заявке №{rid}:", reply_markup=K.kb_cancel_only())


@router.message(GoldMsg.user_reply, F.text)
async def on_gold_user_reply(msg: Message, state: FSMContext, bot: Bot):
    rid = (await state.get_data()).get("reply_rid")
    r = db.get_gold_request(rid) if rid else None
    await state.set_state(None)
    uid = msg.from_user.id
    if not r or int(r["user_id"]) != uid:
        return await msg.answer("❌ Заявка не найдена.", reply_markup=K.kb_main_menu(uid in ADMIN_IDS))
    first = []
    if r.get("username"):
        first.append(ib("Написать клиенту", "chat", url=f"https://t.me/{r['username']}"))
    first.append(ib("Ответить в боте", "send", callback_data=f"gold_reply:{rid}"))
    await notify_admins(
        bot, f"💬 <b>Сообщение по заявке №{rid}</b>\n\n👤 {user_label(uid, msg.from_user.username, msg.from_user.first_name)}\n\n"
             f"{html.escape(msg.text[:3000])}\n\n📌 Статус заявки: {STATUS_T.get(r['status'], r['status'])}",
        InlineKeyboardMarkup(inline_keyboard=[first]))
    await msg.answer("✅ Сообщение отправлено администратору.", reply_markup=K.kb_main_menu(uid in ADMIN_IDS))
