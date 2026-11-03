"""
main.py — точка входа бота.
"""
import asyncio
import logging
from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.types import CallbackQuery, Message, ErrorEvent
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, ADMIN_IDS
from handlers import (
    shop, topup, promo, replacement, admin, admin_extra, fallback, fragment_shop, fragment_admin, gold, ton_shop,
    reviews, support, nft, store,
)
from handlers import channel_posts
from utils import is_banned, is_subscribed, send_subscribe_screen, send_lang_chooser
from database import db
import i18n
from payments import watch_pending
from tondeposit import watch_ton_deposits
from tonrate import watch_ton_rate
from monopay import watch_mono
from emoji import PremiumEmojiMiddleware
from utils import PhotoIdCache, notify_admins


class BanMiddleware(BaseMiddleware):
    """Заблокированным пользователям бот не отвечает (кроме слова Banned)."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and is_banned(user.id):
            if hasattr(event, "answer") and not hasattr(event, "data"):
                await event.answer("Banned")
            return
        return await handler(event, data)


class LangMiddleware(BaseMiddleware):
    """
    Язык бота (русский / украинский). Идёт ДО проверки подписки:
      • новый пользователь (нет языка и нет в БД) → показываем выбор языка, остальное не обрабатываем;
      • старый пользователь без выбора → русский (как было);
      • украинские кнопки подменяем на русские канонические — хендлеры язык не знают (см. i18n.py);
      • CUR_UID — чтобы ответы на нажатия (всплывающие уведомления) тоже переводились.
    """
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)
        uid = user.id
        lang = i18n.lang_of(uid)
        if not lang and db.find_user_by_id(uid):
            lang = "ru"
            i18n.set_lang(uid, "ru")
        is_lang_cb = isinstance(event, CallbackQuery) and (event.data or "").startswith("lang:")
        if not lang and not is_lang_cb:
            text = getattr(event, "text", None) or ""
            state = data.get("state")
            parts = text.split()
            if state and parts[:1] == ["/start"] and len(parts) > 1:
                await state.update_data(pending_start=parts[1])            # диплинк/реферал не теряем до регистрации
                if parts[1].isdigit():
                    await state.update_data(pending_ref=int(parts[1]))
            target = event.message if isinstance(event, CallbackQuery) else event
            if isinstance(event, CallbackQuery):
                await event.answer()
            if target is not None:
                await send_lang_chooser(target)
            return
        token = i18n.CUR_UID.set(uid)
        try:
            if lang in i18n.TRANSLATED and isinstance(event, Message) and event.text:
                ru = i18n.button_to_ru(event.text, lang)
                if ru != event.text:
                    try:
                        object.__setattr__(event, "text", ru)
                    except Exception:
                        event = event.model_copy(update={"text": ru})
            data["lang"] = lang
            return await handler(event, data)
        finally:
            i18n.CUR_UID.reset(token)


class SubscriptionMiddleware(BaseMiddleware):
    """
    Обязательная подписка на канал: пока человек не подписан, ЛЮБОЕ его сообщение/кнопка
    приводит только к экрану «Подпишись на канал». Админы и кнопка «Я подписался» — пропускаются.
    """
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user or user.id in ADMIN_IDS:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and (event.data == "check_sub" or (event.data or "").startswith("lang:")):
            return await handler(event, data)
        if await is_subscribed(data["bot"], user.id):
            return await handler(event, data)

        # Не подписан. Сохраняем payload /start, чтобы после подписки открыть нужный NFT или привязать реферала.
        text = getattr(event, "text", None) or ""
        state = data.get("state")
        parts = text.split()
        if state and parts[:1] == ["/start"] and len(parts) > 1:
            await state.update_data(pending_start=parts[1])
            if parts[1].isdigit():
                await state.update_data(pending_ref=int(parts[1]))

        target = event.message if isinstance(event, CallbackQuery) else event
        if isinstance(event, CallbackQuery):
            await event.answer()
        if target is not None:
            await send_subscribe_screen(target, user.first_name or "")
        return


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx пишет строку на КАЖДЫЙ запрос к Supabase (фоновая проверка оплат — раз в ~3.5 с) и забивает лог.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(i18n.TranslateMiddleware())       # русский → украинский / английский для пользователей с языком «uk» / «en»
    bot.session.middleware(PremiumEmojiMiddleware())     # обычные эмодзи в тексте → премиум (см. emoji.py)
    bot.session.middleware(PhotoIdCache())               # file_id баннеров: картинка заливается один раз, дальше мгновенно
    dp = Dispatcher(storage=MemoryStorage())

    _err_seen: dict[str, float] = {}

    @dp.error()
    async def on_error(event: ErrorEvent):
        """Любая ошибка в хендлере → в консоль и админам в чат (раньше бот просто молчал)."""
        import time, traceback, html as _h
        logging.getLogger("bot").error("handler error", exc_info=event.exception)
        key = repr(event.exception)[:200]
        if time.monotonic() - _err_seen.get(key, 0) < 60:
            return
        _err_seen[key] = time.monotonic()
        tb = "".join(traceback.format_exception(event.exception))[-1500:]
        try:
            await notify_admins(bot, f"⚠️ <b>Ошибка бота</b>\n<pre>{_h.escape(tb)}</pre>")
        except Exception:
            pass
    dp.message.outer_middleware(BanMiddleware())
    dp.callback_query.outer_middleware(BanMiddleware())
    dp.message.outer_middleware(LangMiddleware())
    dp.callback_query.outer_middleware(LangMiddleware())
    dp.message.outer_middleware(SubscriptionMiddleware())
    dp.callback_query.outer_middleware(SubscriptionMiddleware())

    # Порядок важен: глобальные кнопки меню (shop) раньше обработчиков ввода (topup/promo/replacement)
    dp.include_router(admin.router)
    dp.include_router(admin_extra.router)
    dp.include_router(fragment_admin.router)
    dp.include_router(store.router)                   # SHOU SHOP: новый интерфейс и оплата по чеку (раньше старых)
    dp.include_router(shop.router)
    dp.include_router(reviews.router)
    dp.include_router(nft.router)
    dp.include_router(channel_posts.router)
    dp.include_router(support.router)
    # fragment_shop / gold / ton_shop / topup отключены: баланса и пополнений больше нет
    dp.include_router(promo.router)
    dp.include_router(replacement.router)
    dp.include_router(fallback.router)

    asyncio.create_task(watch_ton_rate(bot))          # курс TON с сайта (CoinGecko) для режима «Сайт»
    print("🤖 Бот запущен...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:                                  # uvloop (Linux/macOS) — заметно быстрее обычного цикла событий
        import uvloop
        uvloop.install()
    except Exception:
        pass
    asyncio.run(main())
