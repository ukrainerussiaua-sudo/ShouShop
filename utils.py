"""
utils.py — общие хелперы.
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from aiogram import Bot
from aiogram.types import (
    Message, CallbackQuery, FSInputFile, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.enums import ParseMode
import html
from config import (
    ADMIN_IDS, CUR, SHOP_PHOTO_PATH, PROFILE_PHOTO_PATH, TOPUP_PHOTO_PATH, SUB_PHOTO_PATH,
    INFO_PHOTO_PATH, SUPPORT_PHOTO_PATH, TELEGRAM_PHOTO_PATH, CATALOG_PHOTO_PATH, NFT_PHOTO_PATH,
    STARS_PHOTO_PATH, PREMIUM_PHOTO_PATH, LANG_PHOTO_PATH,
    REQUIRED_CHANNEL, REQUIRED_CHANNEL_URL,
)
from database import db


def is_banned(uid: int) -> bool:
    return db.is_banned_cached(uid)


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


_photo_ids: dict[str, str] = {}      # путь к баннеру → file_id в Telegram (после первой отправки файл больше не заливается)


_BANNER_ALIAS = {"menu": "home", "shop": "home"}      # menu.jpg = баннер главного меню → ищем и home_<язык>.jpg
_BANNER_EXT = (".jpg", ".jpeg", ".png", ".webp")
_variant_cache: dict[tuple[str, str], str] = {}


def banner_path(path: str, lang: str = "") -> str:
    """Баннер под язык пользователя. Для images/<имя>.jpg ищет images/<имя>_<язык>.(jpg|jpeg|png|webp)
    (например home_ru.jpg, home_uk.jpg, home_en.jpg, sub_en.jpg). Нет такого файла — берёт общий <имя>.jpg."""
    lang = lang or "ru"
    key = (path, lang)
    hit = _variant_cache.get(key)
    if hit is not None and (hit == path or os.path.exists(hit)):
        return hit
    d, fn = os.path.split(path)
    base, ext = os.path.splitext(fn)
    names = [base] + ([_BANNER_ALIAS[base]] if base in _BANNER_ALIAS else [])
    res = path
    for n in names:
        for e in dict.fromkeys((ext, *_BANNER_EXT)):
            cand = os.path.join(d, f"{n}_{lang}{e}")
            if os.path.exists(cand):
                res = cand
                break
        else:
            continue
        break
    _variant_cache[key] = res
    return res


def _photo(path):
    """Баннер: язык пользователя → файл <имя>_<язык>.jpg (если есть), иначе общий. Если уже отправляли — берём готовый file_id
    (мгновенно), иначе файл с диска (его file_id запомнит PhotoIdCache)."""
    import i18n
    path = banner_path(path, i18n.lang_of(i18n.CUR_UID.get()) or "ru")
    if not os.path.exists(path):
        return None
    fid = _photo_ids.get(path)
    return fid if fid else FSInputFile(path)


try:
    from aiogram.client.session.middlewares.base import BaseRequestMiddleware as _BRM
except Exception:                        # pragma: no cover
    _BRM = object


class PhotoIdCache(_BRM):
    """Исходящий middleware: запоминает file_id баннеров из images/ после первой отправки/редактирования —
    дальше экраны с картинкой грузятся без повторной заливки файла (самая частая причина «долгой» реакции бота)."""

    async def __call__(self, make_request, bot, method):
        res = await make_request(bot, method)
        try:
            src = getattr(method, "photo", None)
            if src is None:
                media = getattr(method, "media", None)
                src = getattr(media, "media", None) if media is not None and not isinstance(media, (list, str)) else None
            path = getattr(src, "path", None)
            if path and isinstance(src, FSInputFile):
                obj = getattr(res, "result", res)
                sizes = getattr(obj, "photo", None)
                if sizes:
                    _photo_ids[str(path)] = sizes[-1].file_id
        except Exception as e:           # noqa — кэш не должен ломать отправку
            print(f"[photo cache] {e!r}")
        return res


def SHOP_PHOTO():    return _photo(SHOP_PHOTO_PATH)
def PROFILE_PHOTO(): return _photo(PROFILE_PHOTO_PATH)
def TOPUP_PHOTO():   return _photo(TOPUP_PHOTO_PATH)
def SUB_PHOTO():     return _photo(SUB_PHOTO_PATH)
def INFO_PHOTO():    return _photo(INFO_PHOTO_PATH)
def SUPPORT_PHOTO(): return _photo(SUPPORT_PHOTO_PATH)
def TELEGRAM_PHOTO(): return _photo(TELEGRAM_PHOTO_PATH)
def CATALOG_PHOTO(): return _photo(CATALOG_PHOTO_PATH)
def NFT_PHOTO():     return _photo(NFT_PHOTO_PATH)
def STARS_PHOTO():   return _photo(STARS_PHOTO_PATH)
def PREMIUM_PHOTO(): return _photo(PREMIUM_PHOTO_PATH)


# ── Обязательная подписка на канал ─────────────────────────
_sub_ok: dict[int, float] = {}     # uid → когда последний раз подтвердили подписку
_SUB_TTL = 180.0
_sub_warned = False


async def is_subscribed(bot: Bot, uid: int, use_cache: bool = True) -> bool:
    """
    Подписан ли пользователь на REQUIRED_CHANNEL. Положительный ответ кэшируется на минуту,
    отрицательный — нет (после подписки пускаем сразу).
    Если бот не видит канал (не админ / неверное имя) — НЕ блокируем всех, но громко пишем в лог.
    """
    global _sub_warned
    if not REQUIRED_CHANNEL:                 # канала ещё нет — подписку не проверяем
        return True
    if use_cache and time.monotonic() - _sub_ok.get(uid, -1e9) < _SUB_TTL:
        return True
    try:
        m = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=uid)
    except Exception as e:
        if not _sub_warned:
            _sub_warned = True
            logging.getLogger("sub").error(
                "Не могу проверить подписку на %s: %r. Сделайте бота АДМИНИСТРАТОРОМ канала — "
                "пока этого нет, обязательная подписка не работает.", REQUIRED_CHANNEL, e)
        return True
    ok = m.status not in ("left", "kicked", "banned")
    if m.status == "restricted":
        ok = bool(getattr(m, "is_member", True))
    if ok:
        _sub_ok[uid] = time.monotonic()
    return ok


def kb_subscribe_inline() -> InlineKeyboardMarkup:
    """Две кнопки: «Подписаться» (primary — акцентный цвет клиента, обычно фиолетовый/синий) и «Проверить» (success — зелёная).
    Telegram не даёт задать произвольный цвет: доступны только primary / success / danger."""
    from keyboards import ib
    return InlineKeyboardMarkup(inline_keyboard=[
        [ib("Подписаться", "bell", url=REQUIRED_CHANNEL_URL, style="primary")],
        [ib("Проверить подписку", "ok", callback_data="check_sub", style="success")],
    ])


async def send_lang_chooser(msg: Message):
    """Экран выбора языка (русский / украинский / английский). Показывается новым пользователям ДО проверки подписки
    и в «Профиль → Язык». Баннер (необязательно): images/lang.jpg."""
    from keyboards import ib
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [ib("🇷🇺 Русский", callback_data="lang:ru")],
        [ib("🇺🇦 Українська", callback_data="lang:uk")],
        [ib("🇬🇧 English", callback_data="lang:en")]])
    await answer_screen(msg, "🌐 <b>Выберите язык / Оберіть мову / Choose language</b>", kb, _photo(LANG_PHOTO_PATH))


async def send_subscribe_screen(msg: Message, first_name: str):
    import texts as T          # локально: texts тянет lolz/countries
    await answer_screen(msg, T.txt_subscribe(first_name or "пользователь"),
                        kb_subscribe_inline(), SUB_PHOTO())


async def answer_screen(msg: Message, text: str, kb, photo=None):
    """Отправляет экран (фото+подпись или просто текст) вместе с меню-клавиатурой."""
    try:
        if photo:
            return await msg.answer_photo(photo, caption=text, parse_mode=ParseMode.HTML,
                                          reply_markup=kb)
    except Exception as e:
        print(f"[answer_screen photo] {e}")
    return await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                            disable_web_page_preview=True)


async def edit_screen(cb, text: str, kb=None, photo=None):
    """«Назад/К списку»: возвращает экран С БАННЕРОМ.
    • сообщение с баннером → меняем подпись;
    • обычное текстовое сообщение (например, карточка товара) и есть баннер → удаляем его и шлём экран с баннером;
    • иначе — обычное редактирование текста."""
    from aiogram.types import LinkPreviewOptions
    m = cb.message
    try:
        if m.photo or m.document or m.video:
            return await m.edit_caption(caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        if photo:
            try:
                await m.delete()
            except Exception:
                pass                      # не удалилось (старое сообщение) — редактируем текстом ниже
            else:
                return await answer_screen(m, text, kb, photo)
        return await m.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML,
                                 link_preview_options=LinkPreviewOptions(is_disabled=True))
    except Exception as e:
        if "not modified" not in str(e):
            print(f"[edit_screen] {e!r}")


async def run_with_dots(msg: Message, label: str, coro):
    """
    Показывает «label.» → «label..» → «label...» пока выполняется coro.
    Возвращает (результат, исключение). Статус-сообщение удаляется.
    """
    status = await msg.answer(f"{label}.")
    task = asyncio.ensure_future(coro)
    i = 1
    while not task.done():
        await asyncio.wait({task}, timeout=1.0)
        if task.done():
            break
        i = i % 3 + 1
        try:
            await status.edit_text(f"{label}{'.' * i}")
        except Exception:
            pass
    try:
        await status.delete()
    except Exception:
        pass
    try:
        return task.result(), None
    except Exception as e:  # noqa
        return None, e


# ── Редактирование inline-сообщений ──
async def safe_edit(callback: CallbackQuery, photo, text: str, kb):
    """Редактирует сообщение; если есть фото — меняет медиа."""
    try:
        msg = callback.message
        if photo and (msg.photo or msg.document):
            await msg.edit_media(
                InputMediaPhoto(media=photo, caption=text, parse_mode=ParseMode.HTML),
                reply_markup=kb)
        elif photo:
            await msg.answer_photo(photo, caption=text,
                                   parse_mode=ParseMode.HTML, reply_markup=kb)
            try:
                await msg.delete()
            except Exception:
                pass
        else:
            try:
                await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception as e:
        print(f"[safe_edit] {e}")
        try:
            if photo:
                await callback.message.answer_photo(photo, caption=text,
                                                    parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            pass


async def safe_edit_or_answer(callback: CallbackQuery, text: str, reply_markup=None):
    """
    Универсальная замена callback.message.edit_text().
    Работает как с обычными сообщениями, так и с фото-сообщениями
    (где edit_text падает с 'there is no text in the message to edit').
    """
    msg = callback.message
    try:
        if msg.photo or msg.document or msg.video or msg.animation:
            # Фото-сообщение: редактируем caption или отправляем новое
            try:
                await msg.edit_caption(caption=text, parse_mode=ParseMode.HTML,
                                       reply_markup=reply_markup)
            except Exception:
                await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception as e:
        print(f"[safe_edit_or_answer] {e}")
        try:
            await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception:
            pass




# ── Общие хелперы ──────────────────────────────────────────
def money(v: float) -> str:
    """12.5 → «12.5 ₴», 100.0 → «100 ₴»."""
    return f"{float(v):,.2f}".rstrip("0").rstrip(".").replace(",", " ") + f" {CUR}"


def user_label(uid: int, username: str | None = None, name: str | None = None) -> str:
    """HTML: @username (id) либо кликабельное имя."""
    if username:
        return f"@{html.escape(username)} (<code>{uid}</code>)"
    return f'<a href="tg://user?id={uid}">{html.escape(name or "клиент")}</a> (<code>{uid}</code>)'


async def notify_admins(bot: Bot, text: str, reply_markup=None, photo: str | None = None,
                        document: str | None = None) -> int:
    """Рассылает сообщение всем админам. photo/document — file_id. Возвращает, скольким дошло.
    Если Telegram не принял reply_markup (например, URL-кнопка на закрытый профиль) — шлём без неё."""
    sent = 0
    for aid in ADMIN_IDS:
        for kb in ((reply_markup, None) if reply_markup else (None,)):
            try:
                if photo:
                    await bot.send_photo(aid, photo, caption=text, reply_markup=kb)
                elif document:
                    await bot.send_document(aid, document, caption=text, reply_markup=kb)
                else:
                    await bot.send_message(aid, text, reply_markup=kb, disable_web_page_preview=True)
                sent += 1
                break
            except Exception as e:
                print(f"[notify_admins] {aid}: {e!r}")
    return sent
