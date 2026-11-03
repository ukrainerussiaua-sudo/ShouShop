"""Обработка ручных постов в канале магазина.

Администратор публикует фото с подписью `s`; бот заменяет подпись на «Продано! / Sold!»
и добавляет кнопки каталога, Telegram Stars и канала отзывов.
"""
from __future__ import annotations
import html

from aiogram import Router, Bot
from aiogram.types import Message

from config import BOT_USERNAME, SHOP_CHANNEL, NFT_CHANNEL, REVIEWS_CHANNEL_URL
import keyboards as K
from utils import notify_admins

router = Router()
_SOLD_MARKERS = {"s", "/s", "#s"}


def _channel_matches(msg: Message) -> bool:
    """Ограничиваем обработку каналом магазина/NFT, не затрагивая канал отзывов."""
    chat = msg.chat
    configured = (SHOP_CHANNEL, NFT_CHANNEL)
    for raw in configured:
        value = str(raw or "").strip()
        if not value:
            continue
        if value.startswith("@"):
            if (chat.username or "").casefold() == value[1:].casefold():
                return True
        else:
            try:
                if int(value) == chat.id:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def _caption() -> str:
    return "💞 <b>Продано! / Sold!</b>"


@router.channel_post()
async def on_channel_post(msg: Message, bot: Bot):
    """Фото с подписью `s` превращается в пост «Продано! / Sold!» с нужными кнопками."""
    if not _channel_matches(msg):
        return
    marker = (msg.caption or "").strip().casefold()
    # Only a photo with the exact service marker is transformed. This keeps normal
    # channel posts untouched and leaves the photo itself in place.
    if marker not in _SOLD_MARKERS or not msg.photo:
        return

    try:
        me = await bot.get_me()
        username = me.username or BOT_USERNAME
        markup = K.kb_shop_channel(username, REVIEWS_CHANNEL_URL)
        await bot.edit_message_caption(
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            caption=_caption(),
            parse_mode="HTML",
            reply_markup=markup,
        )
        print(f"[channel posts] transformed marker post {msg.chat.id}/{msg.message_id}")
    except Exception as e:
        print(f"[channel posts] can't transform {msg.chat.id}/{msg.message_id}: {e!r}")
        await notify_admins(
            bot,
            "⚠️ <b>Не удалось добавить кнопки к посту в канале.</b>\n"
            f"Канал: <code>{html.escape(msg.chat.title or str(msg.chat.id))}</code>\n"
            f"Сообщение: <code>{msg.message_id}</code>\n"
            "Проверьте, что бот — администратор канала и может редактировать сообщения.\n"
            f"<code>{html.escape(repr(e))[:350]}</code>",
        )
