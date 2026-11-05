"""
Отзывы.
  • Клиент: после покупки — оценка 1–5, комментарий (по желанию) → публикация в REVIEWS_CHANNEL.
  • Нумерация общая и начинается с REVIEW_START_NUMBER (979): «Відгук #979», «#980» …
  • Звёзды в посте — премиум-эмодзи (config.REVIEW_STAR_EMOJI_ID, по умолчанию звезда из пака tgiosicons).
  • /emojiids — вспомогательная команда админа: показывает ID премиум-эмодзи (ссылка на пак t.me/addemoji/… или сами эмодзи).
"""
from __future__ import annotations
import asyncio
import html
import re
from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InputMediaPhoto, Message, User
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

import countries as C
import i18n
import keyboards as K
from config import (ADMIN_IDS, BOT_USERNAME, CUR, REVIEWS_CHANNEL, REVIEW_TAG_PREFIX,
                    REVIEW_STAR_EMOJI_ID, REVIEW_STAR_EMPTY_EMOJI_ID)
from database import db
from emoji import I, tag, premiumize
from keyboards import B_REVIEW_DONE, B_REVIEW_NOPHOTO
from utils import notify_admins

router = Router()
IS_ADMIN = F.from_user.id.in_(ADMIN_IDS)


class ReviewFlow(StatesGroup):
    comment = State()
    photo = State()


# ══════════════════════════════════════════════════════════
# Оформление поста с клиентским отзывом
# ══════════════════════════════════════════════════════════
def review_tag(no: int) -> str:
    """#979 (или #<префикс>979, если задан REVIEW_TAG_PREFIX)."""
    return f"#{REVIEW_TAG_PREFIX}{no}"


def stars_html(rating: int) -> str:
    """Звёзды премиум-эмодзи: заполненные; пустые — только если задан REVIEW_STAR_EMPTY_EMOJI_ID."""
    rating = max(0, min(5, int(rating)))
    out = tag(REVIEW_STAR_EMOJI_ID or I["stars"], "⭐") * rating
    if REVIEW_STAR_EMPTY_EMOJI_ID:
        out += tag(REVIEW_STAR_EMPTY_EMOJI_ID, "☆") * (5 - rating)
    return out


def build_review_text(row: dict, product: str = "", price: float = 0.0) -> str:
    no = int(row["id"])
    name = html.escape(str(row.get("customer_name") or "Клієнт"))
    lines = [f"📊 <b>Відгук {review_tag(no)}</b>", "",
             f"👤 <b>Клієнт:</b> {name}",
             f"⭐ <b>Оцінка:</b> {stars_html(int(row.get('rating') or 0))}"]
    if product:
        lines += ["", f"🛍 <b>Товар:</b> {html.escape(i18n.tr(product, 'uk'))}",
                  f"💰 <b>Сума:</b> {float(price or 0):g} {CUR}"]
    created = str(row.get("created_at") or datetime.now().strftime("%d.%m.%Y / %H:%M"))
    lines += ["", f"🗓 <b>Дата:</b> {html.escape(created)}"]
    comment = (row.get("comment") or "").strip()
    if comment:
        lines += ["", f"💬 <b>Відгук:</b> {html.escape(comment)}"]
    return "\n".join(lines)


MAX_PHOTOS = 10
_locks: dict[int, asyncio.Lock] = {}
_last_group: dict[int, str] = {}


def _split_photos(photo_id) -> list[str]:
    if isinstance(photo_id, str):
        photo_id = photo_id.split(",")
    return [p.strip() for p in (photo_id or []) if p and p.strip()][:MAX_PHOTOS]


async def post_review(bot: Bot, row: dict, product: str = "", price: float = 0.0, photo_id=None) -> int:
    """Публикует отзыв в канал: без фото — текст, 1 фото — фото с подписью, несколько — альбом (подпись на первом).
    Возвращает message_id (первого сообщения)."""
    photos = _split_photos(photo_id if photo_id is not None else row.get("photo_id"))
    text = build_review_text(row, product, price)
    kb = K.kb_review_buy(BOT_USERNAME)
    fits = len(text) <= 1000                       # подпись к фото ≤ 1024 символов (с запасом на теги эмодзи)
    if len(photos) > 1:
        media = [InputMediaPhoto(media=p, caption=premiumize(text) if (i == 0 and fits) else None, parse_mode="HTML")
                 for i, p in enumerate(photos)]
        msgs = await bot.send_media_group(REVIEWS_CHANNEL, media)
        first = msgs[0].message_id
        # Telegram не разрешает reply_markup у send_media_group. Раньше, когда подпись помещалась,
        # альбом уходил вообще без кнопок. Поэтому всегда публикуем отдельную CTA-клавиатуру.
        if not fits:
            await bot.send_message(REVIEWS_CHANNEL, text, reply_markup=kb, disable_web_page_preview=True)
        else:
            await bot.send_message(
                REVIEWS_CHANNEL,
                "🛍️ <b>Хочеш так само?</b> Обирай товар у Mango Shop:",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        m_id = first
    elif photos:
        if fits:
            m = await bot.send_photo(REVIEWS_CHANNEL, photos[0], caption=text, reply_markup=kb)
        else:
            await bot.send_photo(REVIEWS_CHANNEL, photos[0])
            m = await bot.send_message(REVIEWS_CHANNEL, text, reply_markup=kb, disable_web_page_preview=True)
        m_id = m.message_id
    else:
        m = await bot.send_message(REVIEWS_CHANNEL, text, reply_markup=kb, disable_web_page_preview=True)
        m_id = m.message_id
    try:
        db.mark_review_posted(int(row["id"]), m_id)
    except Exception as e:
        print(f"[reviews] mark_review_posted: {e!r}")
    return m_id


async def _collect_photo(msg: Message, state: FSMContext, key: str) -> bool:
    """Добавляет file_id присланного фото в state[key]. True — если нужно ответить (на альбом отвечаем один раз)."""
    uid = msg.from_user.id
    async with _locks.setdefault(uid, asyncio.Lock()):
        d = await state.get_data()
        ph = list(d.get(key) or [])
        fid = msg.photo[-1].file_id
        if fid not in ph and len(ph) < MAX_PHOTOS:
            ph.append(fid)
        await state.update_data(**{key: ph})
        if msg.media_group_id:
            if _last_group.get(uid) == msg.media_group_id:
                return False
            _last_group[uid] = msg.media_group_id
    return True


# ══════════════════════════════════════════════════════════
# Клиент: отзыв после покупки
# ══════════════════════════════════════════════════════════
def _order_for_review(uid: int, kind: str, oid: int) -> dict | None:
    """Resolve a real, completed order and never trust product/price from callback data."""
    if kind == "account":
        row = db.get_purchase(oid)
        if not row or int(row.get("user_id") or 0) != uid or row.get("status") != "active":
            return None
        ci = C.get(row.get("country_code", "")) or {"name": "Telegram", "flag": "🌍"}
        return {"kind": kind, "id": oid, "title": f"Аккаунт {ci['name']}",
                "price": float(row.get("price_paid") or 0)}
    if kind == "fragment":
        row = db.get_fragment_order(oid)
        if not row or int(row.get("user_id") or 0) != uid or row.get("status") != "done":
            return None
        qty = int(row.get("quantity") or 0)
        if row.get("kind") == "stars":
            title = f"{qty} Telegram Stars"
        elif row.get("kind") == "premium":
            title = f"Telegram Premium · {qty} мес."
        else:
            return None
        return {"kind": kind, "id": oid, "title": title,
                "price": float(row.get("price_paid") or 0)}
    if kind == "nft":
        row = db.nft_get(oid)
        if not row or int(row.get("buyer_id") or 0) != uid or row.get("status") != "sold":
            return None
        return {"kind": kind, "id": oid, "title": f"NFT подарок {row.get('title') or row.get('slug')}",
                "price": float(row.get("price_paid") or 0)}
    if kind == "ton":
        row = db.get_ton_order(oid)
        if not row or int(row.get("user_id") or 0) != uid or row.get("status") != "done":
            return None
        qty = float(row.get("amount_ton") or 0)
        return {"kind": kind, "id": oid, "title": f"TON · {qty:g} TON",
                "price": float(row.get("price_paid") or 0)}
    return None


def _display_name(user: User) -> str:
    # Username shown without @; if it is not set, use the visible Telegram display name.
    return (user.username or user.full_name or "Клієнт").lstrip("@").strip()[:120]


@router.callback_query(F.data.startswith("review:start:"))
async def review_start(cb: CallbackQuery, state: FSMContext):
    try:
        _, _, kind, oid_s = cb.data.split(":", 3)
        oid = int(oid_s)
    except (ValueError, AttributeError):
        return await cb.answer("Не удалось определить покупку", show_alert=True)
    order = _order_for_review(cb.from_user.id, kind, oid)
    if not order:
        return await cb.answer("Покупка не найдена или ещё не завершена", show_alert=True)
    try:
        existing = db.get_review_for_order(kind, oid)
    except Exception:
        return await cb.answer("Отзывы временно недоступны. Сообщите поддержке.", show_alert=True)
    if existing:
        return await cb.answer("Вы уже оставили отзыв на эту покупку", show_alert=True)
    await state.clear()
    await state.update_data(review_order=order)
    await cb.answer()
    if cb.message:
        await cb.message.answer(
            f"⭐️ <b>Оцените покупку</b>\n\n🛍 {html.escape(order['title'])}\n💰 {order['price']:g} {CUR}\n\nВыберите оценку от 1 до 5 звёзд:",
            reply_markup=K.kb_review_rating(kind, oid),
        )


@router.callback_query(F.data.startswith("review:rate:"))
async def review_rate(cb: CallbackQuery, state: FSMContext):
    try:
        _, _, kind, oid_s, rating_s = cb.data.split(":", 4)
        oid, rating = int(oid_s), int(rating_s)
        if rating not in range(1, 6):
            raise ValueError
    except (ValueError, AttributeError):
        return await cb.answer("Оценка должна быть от 1 до 5", show_alert=True)
    order = _order_for_review(cb.from_user.id, kind, oid)
    if not order:
        return await cb.answer("Покупка не найдена или ещё не завершена", show_alert=True)
    try:
        if db.get_review_for_order(kind, oid):
            return await cb.answer("Вы уже оставили отзыв на эту покупку", show_alert=True)
    except Exception:
        return await cb.answer("Отзывы временно недоступны. Сообщите поддержке.", show_alert=True)
    await state.set_state(ReviewFlow.comment)
    await state.update_data(review_order=order, review_rating=rating,
                            review_customer_name=_display_name(cb.from_user))
    await cb.answer(f"Оценка: {rating}/5")
    if cb.message:
        await cb.message.answer(
            f"Спасибо! Ваша оценка: {stars_html(rating)}\n\nМожете добавить комментарий к отзыву одним сообщением или нажать «Без комментария».",
            reply_markup=K.kb_review_skip(),
        )


async def _to_photo_step(user: User, msg: Message | None, state: FSMContext, comment: str):
    await state.set_state(ReviewFlow.photo)
    await state.update_data(review_comment=comment, review_photos=[])
    if msg:
        await msg.answer("📸 Хотите добавить фото или скриншоты к отзыву? Отправьте их (можно несколько), затем нажмите «Готово». "
                         "Или нажмите «Без фото».", reply_markup=K.kb_review_photo())


@router.callback_query(F.data == "review:skip")
async def review_skip(cb: CallbackQuery, state: FSMContext):
    if await state.get_state() != ReviewFlow.comment.state:
        return await cb.answer("Сначала выберите оценку", show_alert=True)
    await cb.answer()
    await _to_photo_step(cb.from_user, cb.message, state, "")


@router.message(ReviewFlow.comment, F.text)
async def review_comment(msg: Message, state: FSMContext):
    comment = (msg.text or "").strip()
    if not comment:
        return await msg.answer("Комментарий пустой. Напишите текст или нажмите «Без комментария».",
                                 reply_markup=K.kb_review_skip())
    await _to_photo_step(msg.from_user, msg, state, comment[:1000])


@router.message(ReviewFlow.photo, F.photo)
async def review_photo(msg: Message, state: FSMContext):
    if await _collect_photo(msg, state, "review_photos"):
        await msg.answer("✅ Фото принято. Пришлите ещё или нажмите «Готово».", reply_markup=K.kb_review_photo())


@router.message(ReviewFlow.photo, F.text.in_({B_REVIEW_DONE, B_REVIEW_NOPHOTO}))
async def review_photo_done(msg: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    photos = [] if msg.text == B_REVIEW_NOPHOTO else list(d.get("review_photos") or [])
    await _publish_review(msg.from_user, d, d.get("review_comment") or "", photos, msg, bot, state)


@router.message(ReviewFlow.photo)
async def review_photo_other(msg: Message):
    await msg.answer("Отправьте фото (именно как фотографию) или нажмите «Готово» / «Без фото».",
                     reply_markup=K.kb_review_photo())


async def _publish_review(user: User, data: dict, comment: str, photos: list, msg: Message | None,
                          bot: Bot, state: FSMContext):
    order = data.get("review_order") or {}
    rating = int(data.get("review_rating") or 0)
    if not order or rating not in range(1, 6):
        await state.clear()
        if msg:
            await msg.answer("Не удалось найти покупку для отзыва. Откройте покупку ещё раз и нажмите «Оставить отзыв».",
                             reply_markup=K.kb_main_menu(user.id in ADMIN_IDS))
        return
    menu = K.kb_main_menu(user.id in ADMIN_IDS)
    try:
        # Re-check ownership/status/duplicate at final submit to prevent forged or repeated callback use.
        fresh = _order_for_review(user.id, order.get("kind", ""), int(order.get("id") or 0))
        if not fresh:
            await state.clear()
            if msg:
                await msg.answer("Покупка не найдена или больше недоступна для отзыва.", reply_markup=menu)
            return
        existing = db.get_review_for_order(fresh["kind"], fresh["id"])
        if existing:
            await state.clear()
            if msg:
                await msg.answer("Вы уже оставили отзыв на эту покупку. Спасибо!", reply_markup=menu)
            return
        row = db.add_review(
            fresh["kind"], fresh["id"], user.id,
            str(data.get("review_customer_name") or _display_name(user)),
            fresh["title"], fresh["price"], rating, comment, photo_id=",".join(photos),
        )
    except Exception as e:
        await state.clear()
        print(f"[reviews] cannot save review: {e!r}")
        if msg:
            await msg.answer("⚠️ Не удалось сохранить отзыв. Попробуйте ещё раз позже или напишите в поддержку.",
                             reply_markup=menu)
        return

    no = review_tag(int(row["id"]))
    try:
        await post_review(bot, row, product=str(row.get("product_title") or fresh["title"]),
                          price=float(row.get("price_paid") or 0), photo_id=photos)
        if msg:
            await msg.answer(f"✅ Спасибо! Отзыв {no} опубликован в канале отзывов.", reply_markup=menu)
    except Exception as e:
        print(f"[reviews] cannot publish to {REVIEWS_CHANNEL}: {e!r}")
        try:
            await notify_admins(bot, f"⚠️ Отзыв {no} сохранён в базе, но не опубликован в {REVIEWS_CHANNEL}.\n{html.escape(repr(e))}")
        except Exception:
            pass
        if msg:
            await msg.answer(f"⚠️ Отзыв {no} сохранён, но не удалось опубликовать его в канале. Администрация уведомлена.",
                             reply_markup=menu)
    finally:
        await state.clear()


# ══════════════════════════════════════════════════════════
# /emojiids — ID премиум-эмодзи (ссылка на пак или сами эмодзи в сообщении)
# ══════════════════════════════════════════════════════════
@router.message(Command("emojiids"), IS_ADMIN)
async def cmd_emojiids(msg: Message, bot: Bot):
    text = msg.text or msg.caption or ""
    lines: list[str] = []
    m = re.search(r"(?:t\.me|telegram\.me)/(?:addemoji|addstickers)/([A-Za-z0-9_]+)", text)
    if m:
        try:
            st = await bot.get_sticker_set(m.group(1))
            for s in st.stickers:
                if s.custom_emoji_id:
                    lines.append(f"{s.emoji or '•'} {s.custom_emoji_id}")
        except Exception as e:
            return await msg.answer(f"Не удалось получить пак: <code>{html.escape(repr(e))[:300]}</code>")
    for holder in (msg, msg.reply_to_message):
        if not holder:
            continue
        src = holder.text or holder.caption or ""
        for ent in (holder.entities or holder.caption_entities or []):
            if ent.type == "custom_emoji" and ent.custom_emoji_id:
                lines.append(f"{src[ent.offset:ent.offset + ent.length]} {ent.custom_emoji_id}")
    if not lines:
        return await msg.answer("Отправьте <code>/emojiids https://t.me/addemoji/НАЗВАНИЕ_ПАКА</code> — или команду вместе с премиум-эмодзи "
                                "(либо ответом на сообщение с ними). В ответ придут их ID.")
    chunk, size = [], 0
    for ln in lines:
        if size + len(ln) > 3500:
            await msg.answer("<code>" + "\n".join(chunk) + "</code>")
            chunk, size = [], 0
        chunk.append(ln)
        size += len(ln) + 1
    if chunk:
        await msg.answer("<code>" + "\n".join(chunk) + "</code>")
