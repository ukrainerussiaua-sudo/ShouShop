"""NFT-каталог с ручной выдачей, заявками админам и публикацией товаров в Telegram-канал."""
from __future__ import annotations
import asyncio
import html
import json
import re
import time
import unicodedata

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.methods.base import TelegramMethod
from aiogram.types import BusinessConnection, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

import keyboards as K
import texts as T
from config import ADMIN_IDS, BOT_USERNAME, CUR, REF_BONUS, NFT_CHANNEL, NFT_WAREHOUSE_IDS, NFT_BUSINESS_CONNECTION_ID
from database import db
from keyboards import A_NFT, B_NFT, B_SKIP, ib
from utils import notify_admins, user_label, money, answer_screen, NFT_PHOTO

router = Router()
IS_ADMIN = F.from_user.id.in_(ADMIN_IDS)
PAGE = 8
_locks: dict[int, asyncio.Lock] = {}
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_BOT_USERNAME_CACHE = ""
_POST_KEY_PREFIX = "nft_channel_post:"
_DELIVERY_KEY_PREFIX = "nft_delivery_target:"


class NFTPurchaseFlow(StatesGroup):
    recipient_username = State()


# ══════════════════════════════════════════════════════════
# Bot API: свои методы (работают в любой версии aiogram)
# ══════════════════════════════════════════════════════════
class _GetBusinessGifts(TelegramMethod[dict]):
    __returning__ = dict
    __api_method__ = "getBusinessAccountGifts"
    business_connection_id: str
    offset: str | None = None
    limit: int | None = None


# ══════════════════════════════════════════════════════════
# Склад (Business-подключение)
# ══════════════════════════════════════════════════════════
def conn_id() -> str:
    return (db.get_setting("nft_conn_id", "") or NFT_BUSINESS_CONNECTION_ID or "").strip()


async def fetch_warehouse(bot: Bot) -> dict[str, dict]:
    """Уникальные подарки склада: {slug_lower: {owned_gift_id, name, title, can_transfer, stars, next_transfer}}."""
    cid = conn_id()
    if not cid:
        raise RuntimeError("Склад не подключён (Настройки Telegram → Business → Чат-боты)")
    out: dict[str, dict] = {}
    offset: str | None = None
    for _ in range(30):
        res = await bot(_GetBusinessGifts(business_connection_id=cid, offset=offset or None, limit=100))
        for g in res.get("gifts", []) or []:
            if g.get("type") != "unique":
                continue
            ug = g.get("gift") or {}
            name = str(ug.get("name") or "")
            if not name or not g.get("owned_gift_id"):
                continue
            model = (ug.get("model") or {}).get("sticker") or {}
            out[name.lower()] = {
                "emoji_id": str(model.get("custom_emoji_id") or ""),
                "owned_gift_id": str(g["owned_gift_id"]), "name": name,
                "title": pretty_title(name),
                "can_transfer": bool(g.get("can_be_transferred", True)),
                "stars": int(g.get("transfer_star_count") or 0),
                "next_transfer": int(g.get("next_transfer_date") or 0),
            }
        offset = res.get("next_offset")
        if not offset:
            break
    return out


def parse_slug(text: str) -> str | None:
    t = (text or "").strip()
    m = re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/nft/([A-Za-z0-9_\-]+)", t, re.I)
    if m:
        return m.group(1)
    return t if re.fullmatch(r"[A-Za-z0-9_]+-\d+", t) else None


def pretty_title(slug: str) -> str:
    """PlushPepe-1234 → «Plush Pepe #1234»."""
    name, _, num = slug.rpartition("-")
    if not name:
        return slug
    return f"{re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)} #{num}"


def _num(text: str) -> float | None:
    try:
        v = float((text or "").replace(",", ".").replace(" ", "").replace("₴", ""))
        return v if v == v and v < 1e7 else None
    except ValueError:
        return None


def _delivery_key(gid: int) -> str:
    return f"{_DELIVERY_KEY_PREFIX}{int(gid)}"


def _save_delivery_target(gid: int, target: dict) -> bool:
    """Persist the delivery destination using existing bot_settings (no DB migration needed)."""
    raw = json.dumps(target, ensure_ascii=False, separators=(",", ":"))
    db.set_setting(_delivery_key(gid), raw)
    # set_setting intentionally handles DB exceptions; verify persistence before charging the buyer.
    return db.get_setting(_delivery_key(gid), "") == raw


def _load_delivery_target(gid: int) -> dict | None:
    raw = db.get_setting(_delivery_key(gid), "")
    try:
        value = json.loads(raw) if raw else None
        return value if isinstance(value, dict) and value.get("mode") in {"self", "username"} else None
    except (TypeError, ValueError):
        return None


def _clear_delivery_target(gid: int) -> None:
    db.set_setting(_delivery_key(gid), "")


def _delivery_label(target: dict | None) -> str:
    if not target:
        return "Не указан (проверьте заявку вручную)"
    if target.get("mode") == "self":
        uid = int(target.get("recipient_user_id") or target.get("buyer_id") or 0)
        username = str(target.get("username") or "").lstrip("@")
        account = f"@{username}" if username else f"ID {uid}"
        return f"На аккаунт покупателя: {account}"
    username = str(target.get("username") or "").lstrip("@")
    if not username:
        return "Другому пользователю (username не указан)"
    known_id = target.get("recipient_user_id")
    return f"Другому пользователю: @{username}" + (f" (ID {known_id})" if known_id else "")


def _kb_delivery_choice(gid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [ib("На мой аккаунт", "profile", callback_data=f"nft:dest:self:{gid}")],
        [ib("Другому пользователю (@username)", "people", callback_data=f"nft:dest:other:{gid}")],
        [ib("Отмена", "back", callback_data=f"nft:dest:cancel:{gid}")],
    ])


def _kb_delivery_cancel(gid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        ib("Отмена", "back", callback_data=f"nft:dest:cancel:{gid}")
    ]])


async def _show_nft_confirmation(gid: int, target: dict, state: FSMContext, *, cb: CallbackQuery | None = None,
                                 msg: Message | None = None, bot: Bot | None = None,
                                 edit_message: tuple[int | str, int] | None = None):
    """Re-check availability/balance and show final confirmation with the selected recipient."""
    actor = cb.from_user if cb else (msg.from_user if msg else None)
    if actor is None:
        return
    g = db.nft_get(gid)
    if not g or g.get("status") != "available" or float(g.get("price") or 0) <= 0:
        if cb:
            await cb.answer("Товар уже недоступен", show_alert=True)
            gifts = _in_sale()
            return await _edit(cb, _txt_list(gifts), _kb_list(gifts, 0) if gifts else None)
        if msg:
            await msg.answer("Этот NFT уже недоступен. Выберите другой товар.")
        await state.clear()
        return
    price = float(g["price"])
    bal = db.balance(actor.id)
    if bal < price:
        await state.clear()
        if cb:
            await cb.answer()
            return await cb.message.answer(T.txt_nofunds(bal, price), reply_markup=K.kb_nofunds())
        if msg:
            return await msg.answer(T.txt_nofunds(bal, price), reply_markup=K.kb_nofunds())
    target = dict(target)
    target["buyer_id"] = actor.id
    await state.update_data(nft_purchase_gid=gid, nft_delivery_target=target)
    target_line = html.escape(_delivery_label(target))
    text = (f"🧾 <b>Подтвердите заявку на NFT</b>\n\n"
            f"🎁 {html.escape(g['title'])}\n💰 Цена: <b>{money(price)}</b>\n"
            f"📬 Получатель: <b>{target_line}</b>\n\n"
            "После подтверждения деньги спишутся с баланса, а администраторы вручную передадут подарок "
            "указанному аккаунту. После выполнения вы сможете оставить отзыв с фото.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ib(f"Подтвердить · {money(price)}", "ok", callback_data=f"nft:c:{g['id']}:{int(round(price * 100))}")],
        [ib("Изменить получателя", "edit", callback_data=f"nft:dest:change:{gid}")],
        [ib("Отмена", "back", callback_data=f"nft:v:{g['id']}")],
    ])
    if cb:
        await cb.answer()
        return await _edit(cb, text, kb, link_preview_options=NO_PREVIEW)
    if msg:
        if bot and edit_message:
            try:
                return await bot.edit_message_text(
                    text, chat_id=edit_message[0], message_id=edit_message[1],
                    reply_markup=kb, link_preview_options=NO_PREVIEW,
                )
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    return None
                print(f"[nft] couldn't replace recipient prompt with confirmation: {e!r}")
        return await msg.answer(text, reply_markup=kb, link_preview_options=NO_PREVIEW)


async def _edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None, **kw):
    # Пустая inline-клавиатура удаляет старые кнопки (например, «Купить» после оформления заявки).
    keyboard = kb if kb is not None else InlineKeyboardMarkup(inline_keyboard=[])
    try:
        await cb.message.edit_text(text, reply_markup=keyboard, **kw)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await cb.message.answer(text, reply_markup=kb, **kw)


async def _alert(bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None):
    try:
        await notify_admins(bot, text, reply_markup=reply_markup)
    except Exception as e:
        print(f"[nft] alert failed: {e!r}")


def _channel_post(gid: int) -> dict | None:
    """Посты NFT храним в bot_settings, чтобы не требовалась отдельная таблица/миграция."""
    raw = db.get_setting(f"{_POST_KEY_PREFIX}{gid}", "")
    try:
        value = json.loads(raw) if raw else None
        return value if isinstance(value, dict) and value.get("message_id") and value.get("chat_id") else None
    except (TypeError, ValueError):
        return None


def _save_channel_post(gid: int, chat_id: str | int, message_id: int):
    db.set_setting(f"{_POST_KEY_PREFIX}{gid}", json.dumps(
        {"chat_id": str(chat_id), "message_id": int(message_id)}, ensure_ascii=False))


async def _bot_username(bot: Bot) -> str:
    global _BOT_USERNAME_CACHE
    if _BOT_USERNAME_CACHE:
        return _BOT_USERNAME_CACHE
    try:
        me = await bot.get_me()
        if me.username:
            _BOT_USERNAME_CACHE = me.username.lstrip("@")
    except Exception as e:
        print(f"[nft channel] get_me failed: {e!r}")
    if not _BOT_USERNAME_CACHE:
        _BOT_USERNAME_CACHE = (BOT_USERNAME or "").lstrip("@")
    return _BOT_USERNAME_CACHE


async def _sync_channel_post(bot: Bot, g: dict, status: str | None = None) -> bool:
    """Создаёт/обновляет пост товара; при продаже/снятии убирает кнопку покупки."""
    gid = int(g["id"])
    status = status or str(g.get("status") or "hidden")
    is_available = status == "available" and float(g.get("price") or 0) > 0
    saved = _channel_post(gid)
    link = html.escape(str(g.get("link") or f"https://t.me/nft/{g.get('slug', '')}"), quote=True)
    title = html.escape(str(g.get("title") or "NFT"))
    price = money(g.get("price") or 0)
    # Явно передаём пустую клавиатуру для снятых/проданных товаров: None может оставить старые кнопки в Telegram.
    markup = InlineKeyboardMarkup(inline_keyboard=[])
    if is_available:
        username = await _bot_username(bot)
        if not username:
            await _alert(bot, "⚠️ Не удалось опубликовать NFT: не задан BOT_USERNAME в .env.")
            return False
        deep_link = f"https://t.me/{username}?start=nft_{gid}"
        text = (f"🎁 <b>{title}</b>\n\n💰 Цена: <b>{price}</b>\n"
                f"🔗 <a href=\"{link}\">Открыть NFT в Telegram</a>\n\n"
                "Нажмите «Купить», чтобы открыть карточку товара в магазине.")
        # Channel posts use a Unicode button label: Telegram's custom-emoji button icons
        # are intended for private/group/supergroup bot messages unless the bot bought usernames.
        chosen_emoji = str(g.get("emoji_id") or "").strip()
        channel_label = f"{chosen_emoji} Купить" if chosen_emoji and not chosen_emoji.isdigit() else "🎁 Купить"
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            ib(channel_label, "ok", url=deep_link, style="primary")
        ]])
    else:
        if status == "sold":
            state_line = "✅ <b>ПРОДАНО</b> — этот NFT уже купили."
        elif status == "reserved":
            state_line = "⏳ <b>ВРЕМЕННО НЕДОСТУПЕН</b> — заявка на выдачу обрабатывается."
        elif status == "refunding":
            state_line = "⚠️ <b>ВРЕМЕННО НЕДОСТУПЕН</b> — оплата или возврат по заявке проверяется."
        else:
            state_line = "⚪ <b>СНЯТ С ПРОДАЖИ</b> — сейчас купить этот NFT нельзя."
        text = f"🎁 <b>{title}</b>\n\n💰 Цена: <b>{price}</b>\n{state_line}\n🔗 <a href=\"{link}\">Открыть NFT в Telegram</a>"

    if saved:
        try:
            # Новые посты — фото + caption; старая версия проекта могла сохранить обычный text-message.
            try:
                await bot.edit_message_caption(
                    chat_id=saved["chat_id"], message_id=int(saved["message_id"]),
                    caption=text, reply_markup=markup, parse_mode="HTML",
                )
            except TelegramBadRequest as caption_error:
                if "message is not modified" in str(caption_error).lower():
                    return True
                # Для ранее опубликованных текстовых постов редактируем text, а не caption.
                await bot.edit_message_text(
                    text, chat_id=saved["chat_id"], message_id=int(saved["message_id"]),
                    reply_markup=markup, link_preview_options=NO_PREVIEW,
                )
            return True
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return True
            # Если старый пост удалили/бот потерял доступ, доступный товар можно опубликовать заново.
            if not is_available:
                print(f"[nft channel] can't update post #{gid}: {e!r}")
                await _alert(bot, f"⚠️ <b>Не удалось обновить старый пост NFT #{gid}</b>\n{title}\n"
                                  "Проверьте пост в канале вручную. Даже если старая кнопка «Купить» осталась, бот проверяет статус товара перед оформлением.")
                return False
        except Exception as e:
            if not is_available:
                print(f"[nft channel] can't update post #{gid}: {e!r}")
                await _alert(bot, f"⚠️ <b>Не удалось обновить старый пост NFT #{gid}</b>\n{title}\n"
                                  f"<code>{html.escape(repr(e))[:250]}</code>")
                return False

    if not is_available:
        return True
    try:
        banner = NFT_PHOTO()
        if banner:
            sent = await bot.send_photo(
                chat_id=NFT_CHANNEL, photo=banner, caption=text,
                reply_markup=markup, parse_mode="HTML",
            )
        else:
            sent = await bot.send_message(
                chat_id=NFT_CHANNEL, text=text, reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )
        _save_channel_post(gid, NFT_CHANNEL, sent.message_id)
        return True
    except Exception as e:
        print(f"[nft channel] publish #{gid} failed: {e!r}")
        await _alert(bot, f"⚠️ <b>Не удалось опубликовать NFT в канале</b>\n{title} · {price}\n"
                          f"Канал: <code>{html.escape(str(NFT_CHANNEL))}</code>\n"
                          f"Проверьте, что бот администратор канала с правом публикации.\n"
                          f"<code>{html.escape(repr(e))[:250]}</code>")
        return False


# ══════════════════════════════════════════════════════════
# Клиент: каталог → карточка → оплата → заявка на ручную выдачу
# ══════════════════════════════════════════════════════════
def _in_sale() -> list[dict]:
    rows = [g for g in db.nft_list(["available"]) if float(g.get("price") or 0) > 0]
    return sorted(rows, key=lambda g: (float(g["price"]), g["id"]))


def _kb_list(gifts: list[dict], page: int) -> InlineKeyboardMarkup:
    pages = max(1, -(-len(gifts) // PAGE))
    page = max(0, min(page, pages - 1))
    rows = [[_nft_button(f"{g['title']} · {money(g['price'])}", g,
                         callback_data=f"nft:v:{g['id']}")]
            for g in gifts[page * PAGE:(page + 1) * PAGE]]
    nav = []
    if page > 0:
        nav.append(ib("Назад", "back", callback_data=f"nft:p:{page - 1}"))
    if pages > 1:
        nav.append(ib(f"Стр. {page + 1}/{pages}", icon=None, callback_data="nft:noop"))
    if page < pages - 1:
        nav.append(ib("Вперёд", "fwd", callback_data=f"nft:p:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _txt_list(gifts: list[dict]) -> str:
    if not gifts:
        return "🎁 <b>NFT ПОДАРКИ</b>\n\nСейчас в продаже нет NFT. Загляните позже!"
    return ("🎁 <b>NFT ПОДАРКИ</b>\n\nВыберите подарок из списка. После оплаты деньги списываются с баланса, "
            "а заявка поступает администраторам для ручной выдачи.")


def _kb_nft_card(g: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_nft_button(f"Купить · {money(g['price'])}", g, callback_data=f"nft:b:{g['id']}",
                     fallback_icon="ok", style="primary")],
        [ib("К списку", "back", callback_data="nft:p:0")],
    ])


async def show_nft_menu(msg: Message, state: FSMContext):
    """Открывает NFT-раздел с баннером-заглушкой и доступными товарами."""
    await state.set_state(None)
    await state.update_data(screen="nft")
    gifts = _in_sale()
    await answer_screen(msg, _txt_list(gifts), _kb_list(gifts, 0) if gifts else None, NFT_PHOTO())


@router.message(F.text == B_NFT)
async def on_nft_menu(msg: Message, state: FSMContext):
    await show_nft_menu(msg, state)


@router.callback_query(F.data.startswith("nft:p:"))
async def on_nft_page(cb: CallbackQuery):
    gifts = _in_sale()
    await cb.answer()
    await _edit(cb, _txt_list(gifts), _kb_list(gifts, int(cb.data.split(":")[2])) if gifts else None)


@router.callback_query(F.data == "nft:noop")
async def on_nft_noop(cb: CallbackQuery):
    await cb.answer()


def _card(g: dict, bal: float) -> str:
    return (f"🎁 <b>{html.escape(g['title'])}</b>\n\n💰 Цена: <b>{money(g['price'])}</b>\n"
            f"🪙 Ваш баланс: {money(bal)}\n"
            f"🔗 <a href=\"{html.escape(g['link'], quote=True)}\">Открыть подарок в Telegram</a>\n\n"
            "После оплаты заявка уйдёт администраторам. Они вручную передадут подарок; "
            "при отказе деньги вернутся на баланс.")


async def show_nft_deeplink(msg: Message, gid: int):
    """Совместимость: карточка NFT теперь в handlers/store.py."""
    from handlers.store import show_nft_card
    from aiogram.fsm.context import FSMContext
    await show_nft_card(msg, None, gid)


async def _render_view(cb: CallbackQuery, gid: int) -> bool:
    """Показывает карточку подарка (без cb.answer). False — товар недоступен."""
    g = db.nft_get(gid)
    if not g or g.get("status") != "available" or float(g.get("price") or 0) <= 0:
        gifts = _in_sale()
        await _edit(cb, _txt_list(gifts), _kb_list(gifts, 0) if gifts else None)
        return False
    await _edit(cb, _card(g, db.balance(cb.from_user.id)), _kb_nft_card(g),
                link_preview_options=LinkPreviewOptions(is_disabled=False, url=g["link"], show_above_text=True))
    return True


@router.callback_query(F.data.startswith("nft:v:"))
async def on_nft_view(cb: CallbackQuery, state: FSMContext):
    # Открытие карточки/отмена подтверждения сбрасывает незавершённый выбор получателя.
    await state.clear()
    if await _render_view(cb, int(cb.data.split(":")[2])):
        await cb.answer()
    else:
        await cb.answer("Товар уже недоступен", show_alert=True)


@router.callback_query(F.data.startswith("nft:b:"))
async def on_nft_buy(cb: CallbackQuery, state: FSMContext):
    g = db.nft_get(int(cb.data.split(":")[2]))
    if not g or g.get("status") != "available" or float(g.get("price") or 0) <= 0:
        return await cb.answer("Товар уже недоступен", show_alert=True)
    price = float(g["price"])
    bal = db.balance(cb.from_user.id)
    if bal < price:
        await cb.answer()
        return await cb.message.answer(T.txt_nofunds(bal, price), reply_markup=K.kb_nofunds())
    await state.clear()
    await state.update_data(nft_purchase_gid=int(g["id"]))
    await cb.answer()
    text = (f"📬 <b>Куда отправить NFT?</b>\n\n🎁 {html.escape(g['title'])}\n"
            f"💰 Цена: <b>{money(price)}</b>\n\n"
            "Выберите свой аккаунт или укажите @username получателя. Пока выдача ручная: администратор "
            "передаст подарок выбранному аккаунту после оплаты.")
    await _edit(cb, text, _kb_delivery_choice(int(g["id"])), link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.startswith("nft:dest:self:"))
async def on_nft_destination_self(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split(":")[3])
    username = (cb.from_user.username or "").lstrip("@")
    target = {"mode": "self", "username": username, "recipient_user_id": cb.from_user.id}
    return await _show_nft_confirmation(gid, target, state, cb=cb)


@router.callback_query(F.data.startswith("nft:dest:other:"))
async def on_nft_destination_other(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split(":")[3])
    g = db.nft_get(gid)
    if not g or g.get("status") != "available":
        return await cb.answer("Товар уже недоступен", show_alert=True)
    await state.update_data(nft_purchase_gid=gid, nft_prompt_chat_id=cb.message.chat.id,
                            nft_prompt_message_id=cb.message.message_id)
    await state.set_state(NFTPurchaseFlow.recipient_username)
    await cb.answer()
    await _edit(cb, f"👤 <b>Кому отправить NFT?</b>\n\nВведите username получателя, например <code>@username</code>.\n\n"
                    "Покупка пока не оплачена. После ввода username вы сможете проверить данные и подтвердить заказ.",
                _kb_delivery_cancel(gid), link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.startswith("nft:dest:change:"))
async def on_nft_destination_change(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split(":")[3])
    g = db.nft_get(gid)
    if not g or g.get("status") != "available":
        return await cb.answer("Товар уже недоступен", show_alert=True)
    await state.update_data(nft_purchase_gid=gid)
    await cb.answer()
    await _edit(cb, f"📬 <b>Куда отправить NFT?</b>\n\n🎁 {html.escape(g['title'])}\nВыберите получателя:",
                _kb_delivery_choice(gid), link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.startswith("nft:dest:cancel:"))
async def on_nft_destination_cancel(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split(":")[3])
    await state.clear()
    await cb.answer("Покупка отменена")
    await _render_view(cb, gid)


@router.message(NFTPurchaseFlow.recipient_username, F.text)
async def on_nft_recipient_username(msg: Message, state: FSMContext, bot: Bot):
    raw = (msg.text or "").strip()
    if raw.casefold() in {"/cancel", "отмена", "cancel"}:
        data = await state.get_data()
        gid = int(data.get("nft_purchase_gid") or 0)
        prompt_chat_id, prompt_message_id = data.get("nft_prompt_chat_id"), data.get("nft_prompt_message_id")
        await state.clear()
        if gid:
            g = db.nft_get(gid)
            if g and g.get("status") == "available" and prompt_chat_id and prompt_message_id:
                try:
                    await bot.edit_message_text(_card(g, db.balance(msg.from_user.id)),
                                                chat_id=prompt_chat_id, message_id=int(prompt_message_id),
                                                reply_markup=_kb_nft_card(g), link_preview_options=NO_PREVIEW)
                    return
                except TelegramBadRequest:
                    pass
            if g and g.get("status") == "available":
                await msg.answer(_card(g, db.balance(msg.from_user.id)), reply_markup=_kb_nft_card(g),
                                 link_preview_options=NO_PREVIEW)
        else:
            await msg.answer("Выбор получателя отменён.")
        return
    match = re.fullmatch(r"@?([A-Za-z][A-Za-z0-9_]{4,31})", raw)
    if not match:
        return await msg.answer("❌ Некорректный username. Введите, например <code>@username</code> (5–32 символа).")
    data = await state.get_data()
    gid = int(data.get("nft_purchase_gid") or 0)
    if not gid:
        await state.clear()
        return await msg.answer("Сессия покупки истекла. Откройте NFT ещё раз.")
    username = match.group(1)
    known = None
    try:
        known = db.find_user_by_username(username)
    except Exception:
        known = None
    target = {"mode": "username", "username": username,
              "recipient_user_id": int(known["user_id"]) if known and known.get("user_id") else None}
    prompt_chat_id = data.get("nft_prompt_chat_id")
    prompt_message_id = data.get("nft_prompt_message_id")
    await state.update_data(nft_delivery_target=target)
    await state.set_state(None)
    edit_message = (prompt_chat_id, int(prompt_message_id)) if prompt_chat_id and prompt_message_id else None
    await _show_nft_confirmation(gid, target, state, msg=msg, bot=bot, edit_message=edit_message)


def _kb_order(gid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [ib("Выполнено", "ok", callback_data=f"nftord:done:{gid}")],
        [ib("Отказать / вернуть деньги", "cancel", callback_data=f"nftord:refuse:{gid}")],
        [ib("Открыть заявку в админке", "list", callback_data=f"nftadm:rqi:{gid}")],
    ])


async def _notify_manual_order(bot: Bot, g: dict, uid: int, price: float, username: str | None, first_name: str | None):
    who = user_label(uid, username, first_name)
    target = _load_delivery_target(int(g["id"]))
    target_text = html.escape(_delivery_label(target))
    text = (f"🆕 <b>НОВАЯ ЗАЯВКА НА NFT #{g['id']}</b>\n\n"
            f"🎁 Товар: <b>{html.escape(g['title'])}</b>\n"
            f"💰 Оплачено по чеку: <b>{money(price)}</b>\n"
            f"👤 Покупатель: {who}\n"
            f"🆔 ID покупателя: <code>{uid}</code>\n"
            f"📬 <b>Отправьте этот NFT получателю:</b> {target_text}\n"
            + (f"🆔 ID получателя: <code>{target['recipient_user_id']}</code>\n" if target.get("recipient_user_id") else "")
            + (f"👤 Юзернейм получателя: @{html.escape(target['username'])}\n" if target.get("username") else "")
            +
            f"🔗 <a href=\"{html.escape(g['link'], quote=True)}\">Ссылка на NFT</a>\n\n"
            "Передайте NFT получателю вручную, затем нажмите «Выполнено». Если выдать нельзя — «Отказать / вернуть деньги» (деньги вернуть клиенту вручную).")
    await _alert(bot, text, reply_markup=_kb_order(int(g["id"])))


@router.callback_query(F.data.startswith("nft:c:"))
async def on_nft_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    uid = cb.from_user.id
    try:
        _, _, gid_s, cents_s = cb.data.split(":")
        gid, cents = int(gid_s), int(cents_s)
    except ValueError:
        return await cb.answer("Ошибка", show_alert=True)

    data = await state.get_data()
    target = data.get("nft_delivery_target") if int(data.get("nft_purchase_gid") or 0) == gid else None
    if not isinstance(target, dict) or target.get("mode") not in {"self", "username"}:
        await cb.answer("Сначала выберите получателя NFT", show_alert=True)
        g_for_target = db.nft_get(gid)
        if g_for_target and g_for_target.get("status") == "available":
            return await _edit(cb, f"📬 <b>Куда отправить NFT?</b>\n\n{html.escape(g_for_target['title'])}",
                               _kb_delivery_choice(gid), link_preview_options=NO_PREVIEW)
        return
    if target.get("mode") == "self" and int(target.get("recipient_user_id") or 0) != uid:
        return await cb.answer("Получатель аккаунта не совпадает с покупателем. Выберите заново.", show_alert=True)
    if target.get("mode") == "username" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", str(target.get("username") or "")):
        return await cb.answer("Username получателя указан неверно. Выберите получателя заново.", show_alert=True)

    # Блокировка по NFT, а не по покупателю: два клиента не смогут одновременно купить один товар.
    async with _locks.setdefault(gid, asyncio.Lock()):
        g = db.nft_get(gid)
        if not g or g.get("status") != "available" or float(g.get("price") or 0) <= 0:
            return await cb.answer("Товар уже недоступен", show_alert=True)
        price = float(g["price"])
        if int(round(price * 100)) != cents:
            await cb.answer("Цена изменилась — проверьте карточку и подтвердите ещё раз.", show_alert=True)
            return await _render_view(cb, gid)
        bal = db.balance(uid)
        if bal < price:
            await cb.answer()
            return await cb.message.answer(T.txt_nofunds(bal, price), reply_markup=K.kb_nofunds())
        try:
            reserved = db.nft_reserve(gid, uid)
        except Exception as e:
            # Списание ещё не запускали. Если запрос резерва успел пройти до сетевой ошибки,
            # пытаемся безопасно снять резерв, чтобы не заблокировать неоплаченный товар.
            try:
                current = db.nft_get(gid)
                if current and current.get("status") == "reserved" and int(current.get("buyer_id") or 0) == uid:
                    released = db.nft_update_if_status(gid, "reserved", status="available", buyer_id=None,
                                                       reserved_at=None, note="")
                    if released:
                        _clear_delivery_target(gid)
                        refreshed = db.nft_get(gid)
                        if refreshed:
                            await _sync_channel_post(bot, refreshed, status="available")
                    else:
                        await _alert(bot, f"⚠️ Резерв NFT #{gid} остался после ошибки. Списание не запускалось; "
                                          "проверьте статус и снимите резерв вручную, не выдавайте товар.")
            except Exception as recovery_error:
                await _alert(bot, f"⚠️ Не удалось проверить/снять резерв NFT #{gid}. Списание не запускалось. "
                                  f"Проверьте статус вручную.\n<code>{html.escape(repr(recovery_error))[:250]}</code>")
            print(f"[nft] reserve failed for #{gid}: {e!r}")
            await cb.answer("Не удалось создать заявку. Проверьте товар и попробуйте снова.", show_alert=True)
            return
        if not reserved:
            return await cb.answer("Этот NFT уже забрали", show_alert=True)
        target_to_save = dict(target)
        target_to_save["buyer_id"] = uid
        if not _save_delivery_target(gid, target_to_save):
            released = False
            try:
                released = db.nft_update_if_status(gid, "reserved", status="available", buyer_id=None,
                                                   reserved_at=None, note="")
            except Exception as e:
                print(f"[nft] couldn't release reserve after target persistence failure #{gid}: {e!r}")
            if released:
                _clear_delivery_target(gid)
                await _sync_channel_post(bot, db.nft_get(gid) or g, status="available")
                await cb.answer("Не удалось сохранить получателя. Оплата не списана — попробуйте позже.", show_alert=True)
            else:
                note = "Получатель не сохранился, списание не запускалось. Проверить и снять резерв вручную."
                try:
                    db.nft_update_if_status(gid, "reserved", status="refunding", note=note)
                    current = db.nft_get(gid)
                except Exception:
                    current = None
                await _alert(bot, f"⚠️ Не удалось сохранить получателя NFT #{gid}; списание не запускалось. Проверьте резерв вручную.",
                             reply_markup=_kb_request_card(current) if current and current.get("status") == "refunding" else None)
                await cb.answer("Не удалось сохранить получателя. Администраторы проверят заявку.", show_alert=True)
            return
        try:
            charged = db.try_spend(uid, price)
        except Exception as e:
            # Ошибка сети могла случиться как до, так и после списания. Не выдаём товар и не
            # делаем повторный автоматический возврат: переводим заказ в ручную сверку.
            note = ("Не удалось подтвердить результат списания. ПРОВЕРИТЬ БАЛАНС. "
                    "Если сумма списана — вручную верните её перед закрытием заявки; не выдавать NFT до проверки.")
            changed = False
            try:
                changed = db.nft_update_if_status(gid, "reserved", status="refunding", note=note)
            except Exception as state_error:
                print(f"[nft] unable to flag uncertain payment #{gid}: {state_error!r}")
            try:
                current = db.nft_get(gid)
            except Exception:
                current = None
            await _alert(bot, f"🚨 <b>Нужна проверка списания по NFT #{gid}</b>\n"
                              f"Покупатель: <code>{uid}</code> · сумма: {money(price)}\n"
                              "Результат операции с балансом не подтверждён. Проверьте баланс клиента до любых действий. "
                              "Если списание прошло, сначала верните деньги вручную; не нажимайте «Выполнено».\n"
                              f"<code>{html.escape(repr(e))[:250]}</code>",
                         reply_markup=_kb_request_card(current) if current and current.get("status") == "refunding" else None)
            if current and current.get("status") == "refunding":
                await _sync_channel_post(bot, current, status="refunding")
            elif not changed:
                await _alert(bot, f"⚠️ Заказ NFT #{gid} мог остаться в резерве при ошибке проверки списания. "
                                  "Не выдавайте товар, пока не проверите статус и баланс вручную.")
            await cb.answer("Покупка отправлена на проверку. Не повторяйте попытку.", show_alert=True)
            try:
                await _edit(cb, "⚠️ <b>Покупка требует проверки</b>\n\nНе повторяйте покупку. Администраторы проверят результат операции с балансом и сообщат вам о решении.", None,
                            link_preview_options=NO_PREVIEW)
            except Exception:
                pass
            return
        if not charged:
            released = False
            try:
                released = db.nft_update_if_status(gid, "reserved", status="available", buyer_id=None,
                                                   reserved_at=None, note="")
            except Exception as e:
                print(f"[nft] couldn't release unpaid reserve #{gid}: {e!r}")
            if released:
                _clear_delivery_target(gid)
                refreshed = db.nft_get(gid)
                if refreshed:
                    await _sync_channel_post(bot, refreshed, status="available")
            else:
                note = "Списание не выполнено: недостаточно средств. Требуется снять неоплаченный резерв вручную."
                try:
                    db.nft_update_if_status(gid, "reserved", status="refunding", note=note)
                    current = db.nft_get(gid)
                except Exception:
                    current = None
                await _alert(bot, f"⚠️ Не удалось снять неоплаченный резерв NFT #{gid}. "
                                  f"Покупатель: <code>{uid}</code>. Деньги за этот NFT не списаны; проверьте статус вручную.",
                             reply_markup=_kb_request_card(current) if current and current.get("status") == "refunding" else None)
                if current and current.get("status") == "refunding":
                    await _sync_channel_post(bot, current, status="refunding")
            await cb.answer()
            return await cb.message.answer(T.txt_nofunds(db.balance(uid), price), reply_markup=K.kb_nofunds())

        try:
            db.nft_update(gid, note="Ожидает ручной выдачи администратором")
        except Exception as e:
            # Резерв и списание уже зафиксированы; не теряем заявку из-за сбоя записи необязательной заметки.
            print(f"[nft] could not set note for order #{gid}: {e!r}")
            await _alert(bot, f"⚠️ NFT-заказ #{gid} оплачен и зарезервирован, но не удалось обновить заметку. "
                              "Заявка должна быть в очереди админки.")
        g = {**g, "status": "reserved", "buyer_id": uid,
             "note": "Ожидает ручной выдачи администратором"}
        await cb.answer("Заявка отправлена")
        await _edit(cb, f"✅ <b>Заявка оформлена!</b>\n\n🎁 {html.escape(g['title'])}\n"
                        f"💰 Списано с баланса: <b>{money(price)}</b>\n"
                        f"📬 Получатель: <b>{html.escape(_delivery_label(target_to_save))}</b>\n\n"
                        "Администраторы получили заявку и вручную выдадут подарок указанному аккаунту. "
                        "После успешной выдачи вы получите кнопку «Оставить отзыв» — можно будет добавить комментарий и фото. "
                        "При отказе деньги вернутся на баланс.", None,
                    link_preview_options=NO_PREVIEW)
        # Даже если канал/уведомление временно недоступны, заказ останется в очереди админки.
        await _sync_channel_post(bot, g, status="reserved")
        try:
            await _notify_manual_order(bot, g, uid, price, cb.from_user.username, cb.from_user.first_name)
        except Exception as e:
            print(f"[nft] notify manual order #{gid} failed: {e!r}")
            await _alert(bot, f"⚠️ Заявка NFT #{gid} списана с баланса, но уведомление не доставлено. "
                              f"Проверьте «NFT каталог → Заявки на выдачу».")


# ══════════════════════════════════════════════════════════
# Админ: ручная обработка заявок
# ══════════════════════════════════════════════════════════
def _request_rows() -> list[dict]:
    return sorted(db.nft_list(["reserved", "refunding"]), key=lambda x: int(x.get("id") or 0), reverse=True)


def _request_text(g: dict) -> str:
    uid = int(g.get("buyer_id") or 0)
    user = db.find_user_by_id(uid) or {}
    who = user_label(uid, user.get("username"), None) if uid else "не указан"
    if g.get("status") == "refunding":
        state = "⚠️ <b>Нужно проверить списание / возврат</b>"
    else:
        state = "🟡 <b>Ожидает ручной выдачи</b>"
    target_text = html.escape(_delivery_label(_load_delivery_target(int(g["id"]))))
    return (f"🧾 <b>Заявка NFT #{g['id']}</b>\n\n"
            f"🎁 Товар: <b>{html.escape(g['title'])}</b>\n"
            f"💰 Сумма: <b>{money(g.get('price_paid') or g.get('price') or 0)}</b>\n"
            f"👤 Покупатель: {who}\n🆔 ID: <code>{uid}</code>\n"
            f"📬 Получатель: <b>{target_text}</b>\n"
            f"📅 Создана: {html.escape(str(g.get('reserved_at') or '—'))}\n"
            f"🔗 <a href=\"{html.escape(g.get('link') or '', quote=True)}\">Ссылка на NFT</a>\n\n"
            f"Статус: {state}"
            + (f"\n📝 {html.escape(str(g.get('note') or ''))}" if g.get("note") else ""))


def _kb_request_card(g: dict) -> InlineKeyboardMarkup:
    rows = []
    if g.get("status") == "reserved":
        rows.append([ib("Выполнено", "ok", callback_data=f"nftord:done:{g['id']}")])
        rows.append([ib("Отказать / вернуть деньги", "cancel", callback_data=f"nftord:refuse:{g['id']}")])
    elif g.get("status") == "refunding":
        rows.append([ib("Проверил баланс — закрыть заявку", "verified", callback_data=f"nftord:refund_checked:{g['id']}")])
    rows.append([ib("К списку заявок", "back", callback_data="nftadm:reqs")])
    rows.append([ib("В каталог NFT", "gift", callback_data=f"nftadm:g:{g['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_requests(cb: CallbackQuery):
    rows = _request_rows()
    if not rows:
        return await _edit(cb, "📥 <b>ЗАЯВКИ НА ВЫДАЧУ</b>\n\nОткрытых заявок нет.",
                           InlineKeyboardMarkup(inline_keyboard=[[ib("Назад", "back", callback_data="nftadm:home")]]))
    kb = [[ib(f"#{g['id']} · {g['title']} · {money(g.get('price') or 0)}" +
              (" · проверка оплаты/возврата" if g.get("status") == "refunding" else ""), "inbox",
              callback_data=f"nftadm:rqi:{g['id']}")] for g in rows[:50]]
    kb.append([ib("Обновить", "repeat", callback_data="nftadm:reqs"),
               ib("Назад", "back", callback_data="nftadm:home")])
    await _edit(cb, f"📥 <b>ЗАЯВКИ НА ВЫДАЧУ</b> ({len(rows)})\n\nВыберите заказ. После ручной передачи NFT нажмите «Выполнено»; отказ вернёт оплату.",
                InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data == "nftadm:reqs", IS_ADMIN)
async def adm_requests(cb: CallbackQuery):
    await cb.answer()
    await _render_requests(cb)


@router.callback_query(F.data.startswith("nftadm:rqi:"), IS_ADMIN)
async def adm_request_card(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = db.nft_get(gid)
    if not g or g.get("status") not in ("reserved", "refunding"):
        await cb.answer("Заявка уже закрыта", show_alert=True)
        return await _render_requests(cb)
    await cb.answer()
    await _edit(cb, _request_text(g), _kb_request_card(g), link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.startswith("nftord:done:"), IS_ADMIN)
async def adm_order_done(cb: CallbackQuery, bot: Bot):
    gid = int(cb.data.split(":")[2])
    g = db.nft_get(gid)
    if not g or g.get("status") != "reserved" or not g.get("buyer_id"):
        return await cb.answer("Заявка уже закрыта или не найдена", show_alert=True)
    uid, price = int(g["buyer_id"]), float(g.get("price") or 0)
    delivery_target = _load_delivery_target(gid)
    delivery_label = _delivery_label(delivery_target)
    user = db.find_user_by_id(uid) or {}
    now = time.strftime("%d.%m.%Y %H:%M:%S")
    if not db.nft_update_if_status(gid, "reserved", status="sold", price_paid=price, sold_at=now, note=""):
        return await cb.answer("Заявку уже обработал другой администратор", show_alert=True)
    await cb.answer("Заказ отмечен выполненным")
    await _sync_channel_post(bot, g, status="sold")

    ref_paid = 0.0
    if user.get("referrer_id") and REF_BONUS > 0:
        try:
            db.add_balance(int(user["referrer_id"]), REF_BONUS)
            ref_paid = REF_BONUS
        except Exception as e:
            await _alert(bot, f"⚠️ Не удалось начислить реферальный бонус за NFT #{gid}: <code>{html.escape(repr(e))[:250]}</code>")
    db.add_sale(uid, "nft", f"NFT {g['title']}", 1, price, float(g.get("cost") or 0), ref_paid)
    try:
        await bot.send_message(uid,
            f"✅ <b>Заказ выполнен!</b>\n\nNFT «{html.escape(g['title'])}» отмечен как выданный.\n"
            f"📬 Получатель: <b>{html.escape(delivery_label)}</b>\n\n"
            "Спасибо за покупку! Если подарок уже получен, нажмите «Оставить отзыв» — оценку можно дополнить комментарием и фото/скриншотом.",
            reply_markup=K.kb_leave_review("nft", gid), link_preview_options=NO_PREVIEW)
    except Exception as e:
        await _alert(bot, f"⚠️ Заказ NFT #{gid} выполнен, но не удалось уведомить покупателя <code>{uid}</code>: "
                          f"<code>{html.escape(repr(e))[:200]}</code>")
    try:
        await cb.message.edit_text(f"✅ <b>Заявка NFT #{gid} выполнена</b>\n\n"
                                   f"{html.escape(g['title'])} · {money(price)}\nПокупатель: <code>{uid}</code>\n"
                                   f"Получатель: <b>{html.escape(delivery_label)}</b>",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[]), link_preview_options=NO_PREVIEW)
    except Exception:
        pass


@router.callback_query(F.data.startswith("nftord:refuse:"), IS_ADMIN)
async def adm_order_refuse(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = db.nft_get(gid)
    if not g or g.get("status") != "reserved":
        return await cb.answer("Заявка уже закрыта или не найдена", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ib("Да, отказать и вернуть деньги", "cancel", callback_data=f"nftord:refund:{gid}")],
        [ib("Отмена", "back", callback_data=f"nftadm:rqi:{gid}")],
    ])
    await cb.answer()
    await _edit(cb, f"⚠️ <b>Отказать по заявке #{gid}?</b>\n\n{html.escape(g['title'])} · "
                    f"{money(g.get('price') or 0)}\n\nПокупателю вернётся вся списанная сумма, а товар снова появится в продаже.",
                kb, link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.startswith("nftord:refund:"), IS_ADMIN)
async def adm_order_refund(cb: CallbackQuery, bot: Bot):
    gid = int(cb.data.split(":")[2])
    g = db.nft_get(gid)
    if not g or g.get("status") != "reserved" or not g.get("buyer_id"):
        return await cb.answer("Заявка уже закрыта или не найдена", show_alert=True)
    uid, price = int(g["buyer_id"]), float(g.get("price") or 0)
    # Состояние refunding блокирует повторную выплату, если админ нажмёт несколько раз или сеть оборвётся.
    if not db.nft_update_if_status(gid, "reserved", status="refunding", note="Возврат обрабатывается; при сбое нужна проверка"):
        return await cb.answer("Заявку уже обрабатывает другой администратор", show_alert=True)
    try:
        db.add_balance(uid, price)
    except Exception as e:
        await _alert(bot, f"🚨 <b>Не удалось подтвердить возврат по NFT #{gid}</b>\nПользователь: <code>{uid}</code>\n"
                          f"Сумма: {money(price)}\nСтатус оставлен как «проверка возврата» во избежание повторного возврата. "
                          f"Проверьте баланс в БД вручную.\n<code>{html.escape(repr(e))[:300]}</code>")
        await cb.answer("Ошибка возврата — заявка помечена для ручной проверки", show_alert=True)
        try:
            await cb.message.edit_text(f"⚠️ <b>Возврат NFT #{gid} требует проверки</b>\n\n"
                                       "Не запускайте повторный возврат. Проверьте баланс клиента; если оплата списана, сначала верните сумму вручную. "
            "После проверки нажмите «Проверил баланс — закрыть заявку».",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[]), link_preview_options=NO_PREVIEW)
        except Exception:
            pass
        return

    if not db.nft_update_if_status(gid, "refunding", status="available", buyer_id=None,
                                   reserved_at=None, note=""):
        await _alert(bot, f"⚠️ Возврат по NFT #{gid} был выполнен для <code>{uid}</code> ({money(price)}), "
                          "но статус товара не обновился автоматически. Проверьте запись в БД вручную.")
        await cb.answer("Деньги возвращены, но статус товара требует проверки", show_alert=True)
        return
    _clear_delivery_target(gid)
    await cb.answer("Деньги возвращены, товар снова в продаже")
    await _sync_channel_post(bot, g, status="available")
    try:
        await bot.send_message(uid,
            f"↩️ <b>По заявке отказано</b>\n\nNFT «{html.escape(g['title'])}» не будет выдан. "
            f"На баланс возвращено <b>{money(price)}</b>.", link_preview_options=NO_PREVIEW)
    except Exception as e:
        await _alert(bot, f"⚠️ Деньги по NFT #{gid} возвращены, но клиенту <code>{uid}</code> не удалось отправить сообщение: "
                          f"<code>{html.escape(repr(e))[:200]}</code>")
    try:
        await cb.message.edit_text(f"↩️ <b>По заявке NFT #{gid} отказано</b>\n\n"
                                   f"{html.escape(g['title'])} · возвращено {money(price)}. Товар снова в продаже.",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[]), link_preview_options=NO_PREVIEW)
    except Exception:
        pass


@router.callback_query(F.data.startswith("nftord:refund_checked:"), IS_ADMIN)
async def adm_order_refund_checked(cb: CallbackQuery, bot: Bot):
    """Ручное восстановление после ошибки возврата: админ сначала проверяет баланс клиента."""
    gid = int(cb.data.split(":")[2])
    g = db.nft_get(gid)
    if not g or g.get("status") != "refunding" or not g.get("buyer_id"):
        return await cb.answer("Заявка уже закрыта или не найдена", show_alert=True)
    uid, price = int(g["buyer_id"]), float(g.get("price") or 0)
    payment_uncertain = "результат списания" in str(g.get("note") or "").lower() or "недостаточно средств" in str(g.get("note") or "").lower()
    if not db.nft_update_if_status(gid, "refunding", status="available", buyer_id=None,
                                   reserved_at=None, note="Проверка завершена администратором"):
        return await cb.answer("Не удалось обновить статус — проверьте заявку", show_alert=True)
    _clear_delivery_target(gid)
    await cb.answer("Проверка завершена; товар возвращён в продажу")
    await _sync_channel_post(bot, g, status="available")
    try:
        user_message = (f"✅ <b>Проверка по NFT #{gid} завершена.</b>\n\n"
                        f"Товар «{html.escape(g['title'])}» снова в продаже. Если баланс выглядит неверно, обратитесь в поддержку.")
        if not payment_uncertain:
            user_message = (f"✅ <b>Заявка NFT #{gid} закрыта после проверки администратором.</b>\n\n"
                            f"Товар «{html.escape(g['title'])}» снова в продаже. Проверьте баланс: сумма возврата — {money(price)}.")
        await bot.send_message(uid, user_message, link_preview_options=NO_PREVIEW)
    except Exception:
        pass
    try:
        await cb.message.edit_text(f"✅ <b>Заявка NFT #{gid} закрыта после ручной проверки</b>\n\n"
                                   f"{html.escape(g['title'])} · {money(price)}\nТовар снова в продаже.",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[]), link_preview_options=NO_PREVIEW)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
# Необязательное Business-подключение: нужно только для сверки наличия на аккаунте-складе.
# ══════════════════════════════════════════════════════════
@router.business_connection()
async def on_business_connection(bc: BusinessConnection, bot: Bot):
    uid = bc.user.id
    uname = f"@{bc.user.username}" if bc.user.username else str(uid)
    if uid not in ADMIN_IDS and uid not in NFT_WAREHOUSE_IDS:
        return await _alert(bot, f"⚠️ Чужой аккаунт {uname} (<code>{uid}</code>) подключил бота как Business-бота — "
                                 f"проигнорировано. Если это ваш склад, добавьте его id в NFT_WAREHOUSE_IDS.")
    if bc.is_enabled:
        db.set_setting("nft_conn_id", bc.id)
        db.set_setting("nft_conn_user", uname)
        await _alert(bot, f"🔌 <b>Склад NFT подключён:</b> {uname}\nПодключение используется только для сверки наличия. "
                          "Выдача покупателям остаётся ручной.")
    elif db.get_setting("nft_conn_id", "") == bc.id:
        db.set_setting("nft_conn_id", "")
        await _alert(bot, f"🔌 Склад NFT {uname} отключён — сверка наличия недоступна, ручная выдача продолжает работать.")


# ══════════════════════════════════════════════════════════
# Админ: «NFT каталог»
# ══════════════════════════════════════════════════════════
class NftAdd(StatesGroup):
    link = State()
    emoji = State()
    price = State()
    cost = State()


def _read_button_emoji(msg: Message) -> str | None:
    """Return a Telegram custom_emoji_id or one plain Unicode emoji."""
    for entity in (msg.entities or []):
        if getattr(entity, "type", None) == "custom_emoji" and getattr(entity, "custom_emoji_id", None):
            return str(entity.custom_emoji_id)

    value = (msg.text or "").strip()
    if not value or len(value) > 16 or any(ch.isalnum() for ch in value):
        return None
    # Accept normal emoji, including joined/variation-selector sequences; reject punctuation-only input.
    if any(unicodedata.category(ch) in {"So", "Sk"} for ch in value):
        return value
    return None


def _nft_button(label: str, gift: dict, callback_data: str | None = None, *, fallback_icon: str = "gift",
                style: str | None = None, url: str | None = None) -> InlineKeyboardButton:
    """Use an NFT's chosen Premium emoji as the button icon, or show a standard emoji in its text."""
    chosen = str(gift.get("emoji_id") or "").strip()
    if chosen.isdigit():
        icon = chosen
    elif chosen:
        label = f"{chosen} {label}"
        icon = None
    else:
        icon = fallback_icon

    button_args = {}
    if callback_data:
        button_args["callback_data"] = callback_data
    if url:
        button_args["url"] = url
    if style:
        button_args["style"] = style
    return ib(label, icon, **button_args)


class NftEdit(StatesGroup):
    price = State()


_ST = {"available": "🟢", "reserved": "🟡", "refunding": "⚠️", "sold": "✅", "hidden": "⚪"}
_ST_TXT = {"available": "в продаже", "reserved": "заявка ожидает выдачи", "refunding": "нужна проверка возврата",
           "sold": "продан", "hidden": "снят с продажи"}


def _kb_admin_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [ib("Добавить NFT", "plus", callback_data="nftadm:add")],
        [ib("Список", "list", callback_data="nftadm:l:0"), ib("Синхронизировать", "repeat", callback_data="nftadm:sync")],
        [ib("Заявки на выдачу", "inbox", callback_data="nftadm:reqs")],
        [ib("Опубликовать товары в канал", "megaphone", callback_data="nftadm:puball")],
        [ib("Инструкция", "info", callback_data="nftadm:help")],
    ])


def _admin_home_text() -> str:
    rows = db.nft_list()
    cnt = {s: sum(1 for r in rows if r["status"] == s) for s in _ST}
    pending = cnt["reserved"]
    refunding = cnt["refunding"]
    return ("🎁 <b>NFT КАТАЛОГ</b>\n\n"
            "🧑‍💻 <b>Режим выдачи: вручную</b> — после оплаты заявка приходит админам; продавец передаёт NFT и жмёт «Выполнено».\n\n"
            f"🟢 В продаже: {cnt['available']} · 📥 Ожидают выдачи: {pending}\n"
            f"⚠️ Проверка возврата: {refunding} · ✅ Продано: {cnt['sold']} · ⚪ Снято: {cnt['hidden']}\n\n"
            f"📢 Канал товаров: <code>{html.escape(str(NFT_CHANNEL))}</code>")


@router.message(F.text == A_NFT, IS_ADMIN)
async def adm_nft_home(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(_admin_home_text(), reply_markup=_kb_admin_home())


@router.callback_query(F.data == "nftadm:home", IS_ADMIN)
async def adm_cb_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await _edit(cb, _admin_home_text(), _kb_admin_home())


@router.callback_query(F.data == "nftadm:help", IS_ADMIN)
async def adm_help(cb: CallbackQuery):
    await cb.answer()
    await _edit(cb,
        "🎁 <b>Как работает ручная выдача NFT</b>\n\n"
        "1. Нажмите «Добавить NFT» и последовательно пришлите ссылку на подарок, эмодзи для его кнопки, цену и себестоимость.\n"
        "2. Товар появится в каталоге и автоматически опубликуется в канале с ценой и кнопкой «Купить».\n"
        "3. Кнопка в канале открывает карточку именно этого NFT в боте. При подтверждении покупки бот списывает баланс, "
        "резервирует товар и отправляет админам заявку.\n"
        "4. Администратор вручную передаёт NFT покупателю, затем жмёт «Выполнено». Клиент получает уведомление.\n"
        "5. Кнопка «Отказать / вернуть деньги» возвращает всю сумму на баланс и снова выставляет товар в канал.\n\n"
        f"Канал публикации: <code>{html.escape(str(NFT_CHANNEL))}</code>. Бот должен быть администратором этого канала "
        "с правом публикации сообщений. Кнопка «Опубликовать товары в канал» поможет разместить уже существующие позиции.\n\n"
        "Подключение Telegram Business и «Синхронизировать» необязательны: они нужны только для дополнительной сверки склада.",
        InlineKeyboardMarkup(inline_keyboard=[[ib("Назад", "back", callback_data="nftadm:home")]]),
        link_preview_options=NO_PREVIEW)


# ── добавление ──
@router.callback_query(F.data == "nftadm:add", IS_ADMIN)
async def adm_add(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(NftAdd.link)
    await cb.answer()
    await cb.message.answer("🎁 <b>Новый NFT</b>\n\nШаг 1 из 4 — пришлите ссылку на подарок "
                            "(<code>https://t.me/nft/PlushPepe-1234</code>).", reply_markup=K.kb_admin_flow_step(skip=False))


@router.message(NftAdd.link, F.text, IS_ADMIN)
async def adm_add_link(msg: Message, state: FSMContext, bot: Bot):
    slug = parse_slug(msg.text)
    if not slug:
        return await msg.answer("Не вижу ссылку вида <code>t.me/nft/Название-123</code>. Пришлите ещё раз.")
    if db.nft_by_slug(slug):
        return await msg.answer("Этот подарок уже есть в каталоге. Пришлите другую ссылку.")
    owned, warn = "", ""
    if conn_id():
        try:
            info = (await fetch_warehouse(bot)).get(slug.lower())
        except Exception as e:
            warn = "\nℹ️ Не удалось сверить склад; товар всё равно можно выставить для ручной выдачи."
            print(f"[nft] optional warehouse lookup failed while adding {slug}: {e!r}")
        else:
            if info:
                owned = info["owned_gift_id"]
            else:
                warn = "\nℹ️ На подключённом складе не найден — перед выдачей проверьте наличие вручную."
    else:
        warn = "\nℹ️ Выдача будет ручной; перед подтверждением заявки администратор проверит наличие подарка."
    await state.update_data(nft_slug=slug, nft_owned=owned)
    await state.set_state(NftAdd.emoji)
    await msg.answer(
        f"✅ {html.escape(pretty_title(slug))}{warn}\n\n"
        "Шаг 2 из 4 — отправьте эмодзи для кнопки этого подарка.\n"
        "Можно отправить обычный эмодзи или Premium Custom Emoji одним сообщением.\n"
        "Именно он будет отображаться рядом с названием подарка в каталоге NFT.\n"
        "Для иконки Premium Custom Emoji у владельца бота должен быть Telegram Premium; обычный эмодзи работает без него.",
        reply_markup=K.kb_admin_back(),
    )


@router.message(NftAdd.emoji, F.text == K.TO_ADMIN, IS_ADMIN)
async def adm_add_emoji_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Добавление NFT отменено.", reply_markup=K.kb_admin_menu())


@router.message(NftAdd.emoji, F.text, IS_ADMIN)
async def adm_add_emoji(msg: Message, state: FSMContext):
    chosen = _read_button_emoji(msg)
    if not chosen:
        return await msg.answer(
            "Не удалось распознать эмодзи. Отправьте один обычный или Premium Custom Emoji отдельным сообщением."
        )
    await state.update_data(nft_emoji=chosen)
    await state.set_state(NftAdd.price)
    await msg.answer(f"✅ Эмодзи сохранён.\n\nШаг 3 из 4 — цена в {CUR} (числом).",
                     reply_markup=K.kb_admin_flow_step(skip=False))


@router.message(NftAdd.price, F.text, IS_ADMIN)
async def adm_add_price(msg: Message, state: FSMContext):
    v = _num(msg.text)
    if v is None or v <= 0:
        return await msg.answer("Нужно число больше нуля, например <code>1500</code>.")
    await state.update_data(nft_price=v)
    await state.set_state(NftAdd.cost)
    await msg.answer(f"Шаг 4 из 4 — себестоимость в {CUR} (для статистики прибыли) или «Пропустить».",
                     reply_markup=K.kb_admin_flow_step(skip=True))


@router.message(NftAdd.cost, F.text, IS_ADMIN)
async def adm_add_cost(msg: Message, state: FSMContext, bot: Bot):
    cost = 0.0
    if msg.text != B_SKIP:
        v = _num(msg.text)
        if v is None or v < 0:
            return await msg.answer("Нужно число (0 или больше) либо «Пропустить».")
        cost = v
    d = await state.get_data()
    await state.clear()
    try:
        g = db.nft_add(d["nft_slug"], pretty_title(d["nft_slug"]), float(d["nft_price"]), cost, d.get("nft_owned", ""))
        if d.get("nft_emoji"):
            db.nft_update(g["id"], emoji_id=d["nft_emoji"])
            g = db.nft_get(g["id"]) or {**g, "emoji_id": d["nft_emoji"]}
    except Exception as e:
        return await msg.answer(f"⚠️ Не удалось сохранить (выполнен ли блок «15. NFT-ПОДАРКИ» из supabase_schema.sql?):\n"
                                f"<code>{html.escape(repr(e))[:300]}</code>", reply_markup=K.kb_admin_menu())
    await msg.answer(f"✅ <b>{html.escape(g['title'])}</b> добавлен в каталог.\nЦена: {money(g['price'])} · "
                     f"себестоимость: {money(cost)}", reply_markup=K.kb_admin_menu())
    published = await _sync_channel_post(bot, g)
    if not published:
        await msg.answer("⚠️ Товар добавлен в каталог, но публикация в канал не удалась. Проверьте NFT_CHANNEL и права бота.")
    await msg.answer(_admin_home_text(), reply_markup=_kb_admin_home())


# ── список / карточка ──
@router.callback_query(F.data.startswith("nftadm:l:"), IS_ADMIN)
async def adm_list(cb: CallbackQuery):
    await cb.answer()
    await _render_list(cb, int(cb.data.split(":")[2]))


@router.callback_query(F.data == "nftadm:puball", IS_ADMIN)
async def adm_publish_all(cb: CallbackQuery, bot: Bot):
    await cb.answer("Публикую товары…")
    rows = _in_sale()
    ok = fail = 0
    for g in rows:
        if await _sync_channel_post(bot, g, status="available"):
            ok += 1
        else:
            fail += 1
    await cb.message.answer(f"📢 <b>Публикация товаров завершена</b>\n\nОпубликовано/обновлено: {ok}\nОшибок: {fail}\n"
                            f"Канал: <code>{html.escape(str(NFT_CHANNEL))}</code>",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [ib("Открыть NFT-каталог", "gift", callback_data="nftadm:home")]
                            ]))


async def _render_list(cb: CallbackQuery, page: int):
    rows = db.nft_list()
    pages = max(1, -(-len(rows) // PAGE))
    page = max(0, min(page, pages - 1))
    kb_rows = [[ib(f"{_ST.get(g['status'], '•')} {g['title']} · {money(g['price'])}", "gift", callback_data=f"nftadm:g:{g['id']}")]
               for g in rows[page * PAGE:(page + 1) * PAGE]]
    nav = []
    if page > 0:
        nav.append(ib("Назад", "back", callback_data=f"nftadm:l:{page - 1}"))
    if page < pages - 1:
        nav.append(ib("Вперёд", "fwd", callback_data=f"nftadm:l:{page + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([ib("В меню NFT", "back", callback_data="nftadm:home")])
    await _edit(cb, f"📋 <b>Все NFT</b> ({len(rows)}) · стр. {page + 1}/{pages}\n🟢 в продаже · 🟡 резерв · ✅ продан · ⚪ скрыт"
                    if rows else "Каталог пуст. Нажмите «Добавить NFT» или «Синхронизировать».",
                InlineKeyboardMarkup(inline_keyboard=kb_rows))


def _kb_admin_card(g: dict) -> InlineKeyboardMarkup:
    rows = []
    if g["status"] in ("available", "hidden"):
        rows.append([ib("Изменить цену", "edit", callback_data=f"nftadm:p:{g['id']}")])
    if g["status"] in ("available", "hidden"):
        rows.append([ib("Скрыть" if g["status"] == "available" else "Показать в каталоге", "ban" if g["status"] == "available" else "ok",
                        callback_data=f"nftadm:v:{g['id']}")])
    if g["status"] in ("reserved", "refunding"):
        rows.append([ib("Открыть заявку на выдачу", "inbox", callback_data=f"nftadm:rqi:{g['id']}")])
    elif g["status"] == "sold":
        rows.append([ib("Архивировать запись", "trash", callback_data=f"nftadm:d:{g['id']}")])
    else:
        rows.append([ib("Удалить из каталога", "trash", callback_data=f"nftadm:d:{g['id']}")])
    rows.append([ib("К списку", "back", callback_data="nftadm:l:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _admin_card_text(g: dict) -> str:
    t = (f"🎁 <b>{html.escape(g['title'])}</b>\n🔗 <a href=\"{html.escape(g['link'])}\">{html.escape(g['slug'])}</a>\n\n"
         f"Статус: {_ST.get(g['status'], '')} {_ST_TXT.get(g['status'], g['status'])}\n"
         f"💰 Цена: {money(g['price'])} · себестоимость: {money(g.get('cost') or 0)}")
    if g["status"] == "sold":
        t += f"\n👤 Покупатель: <code>{g.get('buyer_id')}</code> · {money(g.get('price_paid') or 0)} · {g.get('sold_at') or ''}"
    if g.get("note"):
        t += f"\n📝 {html.escape(str(g['note']))}"
    return t


async def _show_card(cb: CallbackQuery, gid: int, answer: bool = True):
    g = db.nft_get(gid)
    if not g:
        if answer:
            await cb.answer("Не найден", show_alert=True)
        return await _render_list(cb, 0)
    if answer:
        await cb.answer()
    await _edit(cb, _admin_card_text(g), _kb_admin_card(g), link_preview_options=NO_PREVIEW)


@router.callback_query(F.data.startswith("nftadm:g:"), IS_ADMIN)
async def adm_card(cb: CallbackQuery):
    await _show_card(cb, int(cb.data.split(":")[2]))


@router.callback_query(F.data.startswith("nftadm:v:"), IS_ADMIN)
async def adm_toggle(cb: CallbackQuery, bot: Bot):
    gid = int(cb.data.split(":")[2])
    g = db.nft_get(gid)
    if g and g["status"] in ("available", "hidden"):
        if g["status"] == "hidden" and float(g["price"]) <= 0:
            return await cb.answer("Сначала задайте цену", show_alert=True)
        new_status = "hidden" if g["status"] == "available" else "available"
        if not db.nft_update_if_status(gid, g["status"], status=new_status, note=""):
            await cb.answer("Статус уже изменился. Откройте карточку заново.", show_alert=True)
            return await _show_card(cb, gid, answer=False)
        await cb.answer()
        await _sync_channel_post(bot, db.nft_get(gid) or g, status=new_status)
        return await _show_card(cb, gid, answer=False)
    await _show_card(cb, gid)


@router.callback_query(F.data.startswith("nftadm:r:"), IS_ADMIN)
async def adm_release(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = db.nft_get(gid)
    if g and g["status"] == "reserved":
        await cb.answer("Нельзя просто снять резерв: нажмите «Отказать», чтобы вернуть деньги покупателю.", show_alert=True)
        return await _show_card(cb, gid)
    await _show_card(cb, gid)


@router.callback_query(F.data.startswith("nftadm:d:"), IS_ADMIN)
async def adm_delete(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = db.nft_get(gid)
    if g and g.get("status") in ("reserved", "refunding"):
        return await cb.answer("Нельзя удалить NFT с открытой заявкой. Сначала закройте заявку.", show_alert=True)
    await cb.answer()
    prompt = ("Архивировать проданный NFT? Запись останется в истории покупок и отзывов." if g and g.get("status") == "sold"
              else "Удалить этот NFT из каталога? Пост в канале будет помечен как снятый с продажи.")
    await _edit(cb, prompt,
                InlineKeyboardMarkup(inline_keyboard=[[ib("Да, удалить", "trash", callback_data=f"nftadm:dc:{gid}")],
                                                      [ib("Отмена", "back", callback_data=f"nftadm:g:{gid}")]]))


@router.callback_query(F.data.startswith("nftadm:dc:"), IS_ADMIN)
async def adm_delete_ok(cb: CallbackQuery, bot: Bot):
    gid = int(cb.data.split(":")[2])
    g = db.nft_get(gid)
    if g and g["status"] in ("reserved", "refunding"):
        return await cb.answer("Нельзя удалить NFT с открытой заявкой. Закройте её через «Выполнено» или «Отказать».", show_alert=True)
    if g and g["status"] == "sold":
        db.nft_update(gid, note="Архивная запись — сохранена для истории покупок")
        await _sync_channel_post(bot, db.nft_get(gid) or g, status="sold")
    elif g:
        # Сначала условно снимаем с продажи; если параллельно началась покупка, статус не даст удалить заказ.
        expected = g.get("status")
        if expected not in ("available", "hidden") or not db.nft_update_if_status(
                gid, expected, status="hidden", note="Удалён администратором"):
            return await cb.answer("Статус изменился — обновите карточку и повторите действие.", show_alert=True)
        db.nft_delete(gid)
        await _sync_channel_post(bot, g, status="hidden")
    await cb.answer("Удалено")
    await _render_list(cb, 0)


# ── цена ──
@router.callback_query(F.data.startswith("nftadm:p:"), IS_ADMIN)
async def adm_price(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split(":")[2])
    g = db.nft_get(gid)
    if not g or g.get("status") not in ("available", "hidden"):
        return await cb.answer("Цену можно менять только у товара в продаже или снятого с продажи", show_alert=True)
    await state.clear()
    await state.set_state(NftEdit.price)
    await state.update_data(nft_edit=gid)
    await cb.answer()
    await cb.message.answer(f"Новая цена в {CUR} (числом):", reply_markup=K.kb_admin_flow_step(skip=False))


@router.message(NftEdit.price, F.text, IS_ADMIN)
async def adm_price_set(msg: Message, state: FSMContext, bot: Bot):
    v = _num(msg.text)
    if v is None or v <= 0:
        return await msg.answer("Нужно число больше нуля.")
    gid = int((await state.get_data()).get("nft_edit") or 0)
    await state.clear()
    g = db.nft_get(gid)
    if not g:
        return await msg.answer("Подарок не найден.", reply_markup=K.kb_admin_menu())
    if g.get("status") not in ("available", "hidden"):
        return await msg.answer("Статус NFT изменился, поэтому цена не сохранена. Обновите карточку в админке.",
                                reply_markup=K.kb_admin_menu())
    fields = {"price": round(v, 2)}
    if g["status"] == "hidden" and not g.get("note"):
        fields["status"] = "available"                    # скрытый «без причины» (после синхронизации) — выводим в продажу
    db.nft_update(gid, **fields)
    await msg.answer(f"✅ Цена: {money(v)}" + (" · подарок в продаже" if "status" in fields else ""),
                     reply_markup=K.kb_admin_menu())
    g = db.nft_get(gid)
    if g:
        await _sync_channel_post(bot, g)
    await msg.answer(_admin_card_text(g), reply_markup=_kb_admin_card(g), link_preview_options=NO_PREVIEW)


# ── синхронизация со склада ──
@router.callback_query(F.data == "nftadm:sync", IS_ADMIN)
async def adm_sync(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    try:
        wh = await fetch_warehouse(bot)
    except Exception as e:
        return await cb.message.answer(f"❌ Не удалось прочитать склад: <code>{html.escape(repr(e))[:300]}</code>")
    rows = db.nft_list()
    known = {r["slug"].lower(): r for r in rows}
    added = fixed = hidden = 0
    for key, info in wh.items():
        r = known.get(key)
        if not r:
            try:
                ng = db.nft_add(info["name"], info["title"], 0.0, 0.0, info["owned_gift_id"], status="hidden")
                if info.get("emoji_id"):
                    db.nft_update(ng["id"], emoji_id=info["emoji_id"])
                added += 1
            except Exception as e:
                print(f"[nft sync] {e!r}")
        elif r.get("owned_gift_id") != info["owned_gift_id"] and r["status"] != "sold":
            db.nft_update(r["id"], owned_gift_id=info["owned_gift_id"])
            fixed += 1
        # Do not replace an emoji selected explicitly by the admin. Warehouse emoji is a fallback only.
        if r and info.get("emoji_id") and not str(r.get("emoji_id") or "").strip():
            try:
                db.nft_update(r["id"], emoji_id=info["emoji_id"])
            except Exception as e:
                print(f"[nft sync emoji] {e!r}")
    for r in rows:
        if r["status"] == "available" and r["slug"].lower() not in wh:
            db.nft_update(r["id"], status="hidden", note="нет на складе")
            hidden += 1
            await _sync_channel_post(bot, db.nft_get(r["id"]) or r, status="hidden")
    await cb.message.answer(f"🔄 <b>Синхронизация</b>\nНа складе уникальных подарков: {len(wh)}\n"
                            f"➕ Добавлено (скрыты, без цены): {added}\n🔧 Обновлено id: {fixed}\n"
                            f"⚪ Скрыто (нет на складе): {hidden}\n🖼 Иконок моделей найдено: {sum(1 for i in wh.values() if i.get('emoji_id'))} из {len(wh)}\n\nУстановите цены в «Список» — подарок после этого сам выйдет в продажу.",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[ib("Список", "list", callback_data="nftadm:l:0")]]))
