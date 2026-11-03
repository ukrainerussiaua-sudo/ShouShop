"""
billing.py — единая точка зачисления пополнений. Все способы (Crypto Bot, карта, TON) приходят сюда:
баланс + история + сообщение клиенту + уведомление админам «кто и на сколько пополнил».
"""
from __future__ import annotations
import html

from aiogram import Bot
from aiogram.enums import ParseMode

from config import CUR
from database import db
from utils import notify_admins, user_label, money


async def credit_topup(bot: Bot, uid: int, amount: float, method: str, details: str = "") -> float:
    """Зачисляет amount (₴) на баланс, пишет историю, уведомляет клиента и админов. Возвращает новый баланс."""
    db.add_balance(uid, amount)
    db.add_topup(uid, amount, method)
    bal = db.balance(uid)

    try:
        await bot.send_message(uid, f"✅ <b>Баланс пополнен!</b>\n\n💰 +{money(amount)} ({html.escape(method)})\n"
                                    f"💼 Баланс: <b>{money(bal)}</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"[credit_topup] клиент {uid} недоступен: {e!r}")

    u = db.find_user_by_id(uid) or {}
    extra = f"\n🧾 {details}" if details else ""
    await notify_admins(
        bot,
        f"💰 <b>Пополнение баланса</b>\n\n👤 {user_label(uid, u.get('username'))}\n"
        f"💵 Сумма: <b>+{money(amount)}</b>\n🏷 Способ: {html.escape(method)}{extra}\n"
        f"💼 Баланс клиента: {money(bal)}")
    return bal
