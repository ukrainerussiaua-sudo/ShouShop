"""Simple threaded support tickets: users open a ticket, exchange messages with admins and close it."""
from __future__ import annotations
import html

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from config import ADMIN_IDS
from database import db
import keyboards as K
from utils import notify_admins

router = Router()


class TicketFlow(StatesGroup):
    message = State()


def _ticket_msg_header(ticket: dict) -> str:
    who = ticket.get("customer_name") or ticket.get("username") or str(ticket.get("user_id", ""))
    return (f"🎫 <b>Тикет #{ticket['id']}</b>\n"
            f"👤 Клиент: {html.escape(str(who))}\n"
            f"🆔 ID: <code>{ticket['user_id']}</code>\n"
            f"Статус: {html.escape(str(ticket.get('status', 'open')))}")


@router.callback_query(F.data == "ticket:new")
async def ticket_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(TicketFlow.message)
    await state.update_data(ticket_action="new")
    await cb.answer()
    if cb.message:
        await cb.message.answer("🎫 <b>Создание тикета</b>\n\nОпишите вопрос или проблему одним сообщением. Не отправляйте пароли и секретные ключи.")


@router.callback_query(F.data.startswith("ticket:reply:"))
async def ticket_reply_start(cb: CallbackQuery, state: FSMContext):
    try:
        tid = int(cb.data.rsplit(":", 1)[1])
    except (ValueError, AttributeError):
        return await cb.answer("Некорректный номер тикета", show_alert=True)
    ticket = db.get_ticket(tid)
    if not ticket:
        return await cb.answer("Тикет не найден", show_alert=True)
    is_admin = cb.from_user.id in ADMIN_IDS
    if not is_admin and int(ticket.get("user_id") or 0) != cb.from_user.id:
        return await cb.answer("Это не ваш тикет", show_alert=True)
    if ticket.get("status") == "closed":
        return await cb.answer("Тикет закрыт. Создайте новый, если вопрос остался.", show_alert=True)
    await state.clear()
    await state.set_state(TicketFlow.message)
    await state.update_data(ticket_action="reply", ticket_id=tid, ticket_role="admin" if is_admin else "user")
    await cb.answer()
    if cb.message:
        await cb.message.answer(f"✉️ Ответ в тикет #{tid}. Напишите сообщение одним сообщением.")


@router.callback_query(F.data.startswith("ticket:close:"))
async def ticket_close(cb: CallbackQuery):
    try:
        tid = int(cb.data.rsplit(":", 1)[1])
    except (ValueError, AttributeError):
        return await cb.answer("Некорректный номер тикета", show_alert=True)
    ticket = db.get_ticket(tid)
    if not ticket:
        return await cb.answer("Тикет не найден", show_alert=True)
    if cb.from_user.id not in ADMIN_IDS and int(ticket.get("user_id") or 0) != cb.from_user.id:
        return await cb.answer("Это не ваш тикет", show_alert=True)
    if ticket.get("status") == "closed":
        return await cb.answer("Тикет уже закрыт")
    db.set_ticket_status(tid, "closed")
    await cb.answer("Тикет закрыт")
    if cb.message:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    if cb.from_user.id in ADMIN_IDS:
        try:
            await cb.bot.send_message(ticket["user_id"], f"✅ Тикет #{tid} закрыт поддержкой.")
        except Exception:
            pass
    else:
        await notify_admins(cb.bot, f"🔒 Клиент закрыл тикет #{tid} (user {ticket['user_id']}).")


@router.message(TicketFlow.message, F.text)
async def ticket_message(msg: Message, state: FSMContext, bot: Bot):
    text = (msg.text or "").strip()
    if not text:
        return await msg.answer("Сообщение пустое. Напишите текст тикета.")
    if len(text) > 4000:
        return await msg.answer("Сообщение слишком длинное. Максимум 4000 символов.")

    data = await state.get_data()
    action = data.get("ticket_action")
    role = data.get("ticket_role", "user")

    if action == "new":
        try:
            ticket_id = db.create_ticket(msg.from_user.id, msg.from_user.username or "",
                                         msg.from_user.full_name or "Клиент", text)
            ticket = db.get_ticket(ticket_id) or {"id": ticket_id, "user_id": msg.from_user.id,
                                                  "username": msg.from_user.username or "",
                                                  "customer_name": msg.from_user.full_name or "Клиент", "status": "open"}
        except Exception as e:
            print(f"[support] cannot create ticket: {e!r}")
            await state.clear()
            return await msg.answer("⚠️ Не удалось создать тикет. Проверьте миграцию support_tickets в базе или напишите в поддержку.")
        await state.clear()
        header = _ticket_msg_header(ticket)
        await msg.answer(f"✅ Тикет #{ticket_id} создан. Поддержка ответит здесь в чате с ботом.\n\nВы можете продолжить диалог кнопкой ниже.",
                         reply_markup=K.kb_ticket_actions(ticket_id))
        admin_text = f"{header}\n\n💬 <b>Сообщение клиента:</b>\n{html.escape(text)}"
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, admin_text, reply_markup=K.kb_ticket_actions(ticket_id, can_close=False))
            except Exception:
                pass
        return

    try:
        tid = int(data.get("ticket_id") or 0)
        ticket = db.get_ticket(tid)
        if not ticket or ticket.get("status") == "closed":
            await state.clear()
            return await msg.answer("Тикет не найден или уже закрыт. Создайте новый тикет, если вопрос остался.")
        if role == "admin":
            if msg.from_user.id not in ADMIN_IDS:
                await state.clear()
                return await msg.answer("Недостаточно прав для ответа в этот тикет.")
            db.add_ticket_message(tid, "admin", msg.from_user.id, text)
            await bot.send_message(ticket["user_id"], f"💬 <b>Ответ поддержки · тикет #{tid}</b>\n\n{html.escape(text)}",
                                   reply_markup=K.kb_ticket_actions(tid))
            await state.clear()
            return await msg.answer(f"✅ Ответ отправлен клиенту по тикету #{tid}.")

        if int(ticket.get("user_id") or 0) != msg.from_user.id:
            await state.clear()
            return await msg.answer("Это не ваш тикет.")
        db.add_ticket_message(tid, "user", msg.from_user.id, text)
        await state.clear()
        await msg.answer(f"✅ Сообщение добавлено в тикет #{tid}. Поддержка получит его и ответит здесь.",
                         reply_markup=K.kb_ticket_actions(tid))
        admin_text = f"{_ticket_msg_header(ticket)}\n\n💬 <b>Новое сообщение клиента:</b>\n{html.escape(text)}"
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, admin_text,
                                       reply_markup=K.kb_ticket_actions(tid, can_close=False))
            except Exception:
                pass
    except Exception as e:
        print(f"[support] ticket message error: {e!r}")
        await state.clear()
        await msg.answer("⚠️ Не удалось отправить сообщение в тикет. Попробуйте позже.")
