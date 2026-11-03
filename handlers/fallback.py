"""
handlers/fallback.py — любой текст вне диалога → показываем меню.
Подключается ПОСЛЕДНИМ.
"""
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext

from config import ADMIN_IDS
import keyboards as K

router = Router()


@router.message(StateFilter(None), F.text)
async def on_unknown(msg: Message, state: FSMContext):
    await msg.answer("Пользуйтесь кнопками меню ⬇️\n(если список устарел — выберите страну заново)",
                     reply_markup=K.kb_main_menu(msg.from_user.id in ADMIN_IDS))
