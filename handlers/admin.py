"""
handlers/admin.py — компактная админ-панель (тоже кнопками в меню).
Пул аккаунтов / цены по странам / огр. товар больше не нужны: аккаунты берутся с Lolz,
цена каждой страны — фиксированная, задаётся здесь («💲 Цены»).
"""
import asyncio
import html
from aiogram import Router, F, Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode
from aiogram.filters import Command

import lolz
import countries as C
from config import ADMIN_IDS, CUR
import i18n
from database import db
import keyboards as K
from keyboards import (
    B_ADMIN, A_STATS, A_LOLZ_BAL, A_GIVE, A_USER, A_PROMO, A_PRICES, A_BROADCAST, TO_ADMIN,
    A_BC_SEND, A_BAN, A_UNBAN, A_PROMO_BAL, A_PROMO_DISC, B_HOME,
    A_MENU_PRODUCTS, A_MENU_PAYMENTS, A_MENU_SERVICE,
    A_USER_PURCHASES, A_PURCH_CODE, A_PURCH_RESET, A_BACK_TO_USER,
)
from utils import run_with_dots, money

LOLZ_ITEM_URL = "https://lzt.market/{iid}"

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))   # весь роутер только для админов


class Adm(StatesGroup):
    give_id = State()
    give_amount = State()
    user_query = State()
    user_actions = State()
    user_purchases = State()
    user_purchase_detail = State()
    promo_type = State()
    promo_code = State()
    promo_value = State()
    promo_uses = State()
    price_pick = State()
    price_value = State()
    bc_text = State()
    bc_confirm = State()


async def show_admin(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\nУправление магазином, товарами, платежами и пользователями. "
                     "Редкие технические инструменты собраны в отдельных разделах.",
                     parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_menu())


@router.message(F.text == B_HOME)
async def on_home_from_admin(msg: Message, state: FSMContext):
    """Выход из любого админ-диалога в главное меню."""
    from handlers.shop import show_main
    await show_main(msg, state)


@router.message(F.text == B_ADMIN)
@router.message(Command("admin"))
@router.message(F.text == TO_ADMIN)
async def on_admin(msg: Message, state: FSMContext):
    await show_admin(msg, state)


@router.message(F.text == A_MENU_PRODUCTS)
async def on_admin_products_menu(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("🛍 <b>ТОВАРЫ И ЦЕНЫ</b>\n\nВыберите, какие цены или товары нужно настроить.",
                     parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_products_menu())


@router.message(F.text == A_MENU_PAYMENTS)
async def on_admin_payments_menu(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("💳 <b>ПЛАТЕЖИ</b>\n\nРеквизиты, TON и переводы, которые не удалось автоматически привязать.",
                     parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_payments_menu())


@router.message(F.text == A_MENU_SERVICE)
async def on_admin_service_menu(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("🧰 <b>ЖУРНАЛ И СЕРВИС</b>\n\nИстория операций, балансы поставщиков и технические параметры.",
                     parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_service_menu())


# ── Статистика / Lolz ─────────────────────────────────────
@router.message(F.text == A_STATS)
async def on_stats(msg: Message):
    s = db.stats()
    P, K_ = s["periods"], s["by_kind"]

    def block(title: str, d: dict) -> str:
        return (f"<b>{title}</b>\n"
                f"  🛒 Продаж: {d['count']}\n"
                f"  💰 Грязная выручка: {money(d['gross'])}\n"
                f"  💸 Себестоимость: {money(d['cost'])}\n"
                f"  🎁 Реф. выплаты: {money(d['ref'])}\n"
                f"  ✅ Чистая прибыль: <b>{money(d['net'])}</b>\n")

    kinds = {"account": "✈️ Аккаунты", "stars": "⭐ Stars", "premium": "💎 Premium", "ton": "💠 TON", "nft": "🎁 NFT"}
    by_kind = "\n".join(f"{kinds[k]}: {v['count']} шт · выручка {money(v['gross'])} · прибыль {money(v['net'])}"
                        for k, v in K_.items() if v["count"])
    topups = "\n".join(f"{m}: {n} шт · {money(sm)}" for m, (n, sm) in sorted(s["topups"].items())) or "—"
    total_topups = sum(sm for _, sm in s["topups"].values())
    await msg.answer(
        f"📊 <b>СТАТИСТИКА</b>\n\n"
        f"👥 Пользователей: {s['users']} (новых сегодня: {s['new_today']})\n"
        f"💼 На балансах клиентов: {money(s['balances'])}\n"
        f"{block('📅 Сегодня', P['today'])}\n{block('🗓 7 дней', P['week'])}\n"
        f"{block('📆 30 дней', P['month'])}\n{block('🏁 За всё время', P['all'])}\n"
        f"<b>По товарам (всё время)</b>\n{by_kind or '—'}\n\n"
        f"<b>Пополнения баланса (всего {money(total_topups)})</b>\n{topups}\n\n"
        f"ℹ️ Чистая прибыль = выручка − себестоимость − реферальные выплаты. Себестоимость Stars/Premium/TON "
        f"берётся из «Себестоимость» (если не задана — считается 0), аккаунтов — реальная цена Lolz.",
        parse_mode=ParseMode.HTML)


@router.message(F.text == A_LOLZ_BAL)
async def on_lolz_balance(msg: Message):
    try:
        bal = await lolz.my_balance()
        from handlers.shop import _rub_rate
        await msg.answer(f"💰 Баланс на Lolz Market: <b>{bal:.2f} ₽</b> (≈ {money(bal * _rub_rate())})\n"
                         f"Баланс самой площадки Lolz — в рублях. Пополняйте его заранее — с него бот покупает аккаунты.",
                         parse_mode=ParseMode.HTML)
    except Exception as e:
        await msg.answer(f"❌ Не удалось получить баланс Lolz: {e}")


# ── Начислить баланс ──────────────────────────────────────
@router.message(F.text == A_GIVE)
async def on_give(msg: Message, state: FSMContext):
    await state.set_state(Adm.give_id)
    await msg.answer("Введите Telegram ID пользователя:", reply_markup=K.kb_admin_back())


@router.message(Adm.give_id, F.text)
async def on_give_id(msg: Message, state: FSMContext):
    if not msg.text.strip().lstrip("-").isdigit() or not db.find_user_by_id(int(msg.text)):
        return await msg.answer("❌ Пользователь не найден. Введите ID ещё раз:")
    await state.update_data(target=int(msg.text))
    await state.set_state(Adm.give_amount)
    await msg.answer(f"Сумма ({CUR}, можно отрицательную для списания):")


@router.message(Adm.give_amount, F.text)
async def on_give_amount(msg: Message, state: FSMContext, bot: Bot):
    try:
        amount = float(msg.text.replace(",", "."))
    except ValueError:
        return await msg.answer("❌ Введите число:")
    target = (await state.get_data())["target"]
    db.add_balance(target, amount)
    await msg.answer(f"✅ Баланс {target}: {db.balance(target):.1f} {CUR}", reply_markup=K.kb_admin_menu())
    if amount > 0:
        try:
            await bot.send_message(target, f"💰 Вам начислено +{amount:.0f} {CUR}")
        except Exception:
            pass
    await state.clear()


# ── Пользователь: поиск / карточка / покупки / бан ────────
@router.message(F.text == A_USER)
async def on_user(msg: Message, state: FSMContext):
    await state.set_state(Adm.user_query)
    await msg.answer("Введите ID или @username:", reply_markup=K.kb_admin_back())


async def _show_user_card(msg: Message, state: FSMContext, uid: int):
    u = db.find_user_by_id(uid)
    if not u:
        await msg.answer("❌ Пользователь пропал из базы.", reply_markup=K.kb_admin_menu())
        return await state.clear()
    t = db.get_user_turnover(uid)
    await state.update_data(target=uid)
    await state.set_state(Adm.user_actions)
    await msg.answer(
        f"👤 <b>{u['user_id']}</b> @{u.get('username') or '—'}\n\n"
        f"💰 Баланс в боте: <b>{float(u.get('balance', 0)):.1f} {CUR}</b>\n"
        f"📦 Потрачено на покупки: <b>{t['spent']:.1f} {CUR}</b>\n"
        f"💳 Пополнено всего: <b>{t['topped_up']:.1f} {CUR}</b>\n"
        f"🧾 Покупок: {t['purchases_count']}\n"
        f"📅 Регистрация: {u.get('reg_date', '')}\n"
        f"✅ Статус: {u.get('status')}",
        parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_user())


@router.message(Adm.user_query, F.text)
async def on_user_query(msg: Message, state: FSMContext):
    q = msg.text.strip()
    u = db.find_user_by_id(int(q)) if q.isdigit() else db.find_user_by_username(q.lstrip("@"))
    if not u:
        return await msg.answer("❌ Не найден. Попробуйте ещё раз:")
    await _show_user_card(msg, state, u["user_id"])


@router.message(Adm.user_actions, F.text.in_({A_BAN, A_UNBAN}))
async def on_user_action(msg: Message, state: FSMContext):
    target = (await state.get_data())["target"]
    db.set_user_status(target, "Заблокирован" if msg.text == A_BAN else "Активен")
    await msg.answer("✅ Готово", reply_markup=K.kb_admin_menu())
    await state.clear()


# ── Покупки клиента: список → детали → код / сброс сессии ─
def _lolz_iid(p: dict) -> int | None:
    s = str(p.get("logins") or "")
    return int(s[5:]) if s.startswith("lolz:") and s[5:].isdigit() else None


@router.message(Adm.user_actions, F.text == A_USER_PURCHASES)
async def on_user_purchases(msg: Message, state: FSMContext):
    data = await state.get_data()
    target = data.get("target")
    purchases = db.get_user_purchases_all(target)
    if not purchases:
        return await msg.answer("У клиента ещё нет покупок.", reply_markup=K.kb_admin_user())

    labels, mapping = [], {}
    for n, p in enumerate(purchases[:30], 1):
        ci = C.get(p.get("country_code")) or {"flag": "🌍"}
        status_icon = "✅" if p.get("status") == "active" else "♻️"
        label = f'{n}. {status_icon} {ci["flag"]} {p.get("phone_number")} · {str(p.get("purchase_date", "")).split()[0]}'
        labels.append(label)
        mapping[label] = p["id"]
    await state.update_data(admin_purchases=mapping)
    await state.set_state(Adm.user_purchases)
    await msg.answer(f"🛒 <b>ПОКУПКИ КЛИЕНТА</b> ({len(purchases)})\n\nВыберите аккаунт:",
                     parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_user_purchases(labels))


@router.message(Adm.user_purchases, F.text == A_BACK_TO_USER)
async def on_purchases_back(msg: Message, state: FSMContext):
    data = await state.get_data()
    await _show_user_card(msg, state, data.get("target"))


@router.message(Adm.user_purchases, F.text)
async def on_purchase_pick(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = (data.get("admin_purchases") or {}).get(msg.text)
    if not pid:
        return await msg.answer("Выберите аккаунт кнопкой ⬇️")
    p = db.get_purchase(pid)
    if not p:
        return await msg.answer("❌ Аккаунт не найден.")
    await state.update_data(cur_admin_pid=pid)
    await state.set_state(Adm.user_purchase_detail)

    ci = C.get(p.get("country_code")) or {"flag": "🌍", "name": "?"}
    iid = _lolz_iid(p)
    status = "✅ Активна" if p.get("status") == "active" else f"♻️ {p.get('status')}"
    text = (
        f"{ci['flag']} <b>{ci['name']}</b>\n\n"
        f"📱 Номер: <code>{p.get('phone_number', '—')}</code>\n"
        f"🔐 Пароль: <code>{p.get('password') or '—'}</code>\n"
        f"💰 Цена покупки: <b>{float(p.get('price_paid') or 0):.1f} {CUR}</b>\n"
        f"📅 Куплен: {p.get('purchase_date', '—')}\n"
        f"⏰ Гарантия до: {p.get('guaranteed_until', '—')}\n"
        f"📌 Статус: {status}\n"
        f"🆔 ID покупки: <code>{p['id']}</code>"
    )
    kb_inline = None
    if iid:
        kb_inline = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть на Lolz", url=LOLZ_ITEM_URL.format(iid=iid))]])
    await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_purchase_detail())
    if kb_inline:
        await msg.answer("Ссылка на карточку товара ⬇️", reply_markup=kb_inline)


@router.message(Adm.user_purchase_detail, F.text == A_BACK_TO_USER)
async def on_purchase_detail_back(msg: Message, state: FSMContext):
    await on_user_purchases(msg, state)


@router.message(Adm.user_purchase_detail, F.text == A_PURCH_CODE)
async def on_purchase_code(msg: Message, state: FSMContext):
    data = await state.get_data()
    p = db.get_purchase(data.get("cur_admin_pid"))
    iid = _lolz_iid(p) if p else None
    if not iid:
        return await msg.answer("❌ У этого аккаунта нет привязки к Lolz (старая покупка).")
    code, err = await run_with_dots(msg, "🔎 Запрашиваю код", lolz.get_login_code(iid))
    if err is not None or not code:
        return await msg.answer("❌ Код ещё не пришёл. Попробуйте ещё раз через полминуты.",
                                reply_markup=K.kb_admin_purchase_detail())
    await msg.answer(f"🔑 Код входа: <code>{code}</code>", parse_mode=ParseMode.HTML,
                     reply_markup=K.kb_admin_purchase_detail())


@router.message(Adm.user_purchase_detail, F.text == A_PURCH_RESET)
async def on_purchase_reset(msg: Message, state: FSMContext):
    data = await state.get_data()
    p = db.get_purchase(data.get("cur_admin_pid"))
    iid = _lolz_iid(p) if p else None
    if not iid:
        return await msg.answer("❌ У этого аккаунта нет привязки к Lolz (старая покупка).")
    ok, err = await run_with_dots(msg, "⏳ Сбрасываю сессии", lolz.reset_authorizations(iid))
    await msg.answer("✅ Сессии сброшены!" if (ok and err is None) else
                     f"❌ Не удалось сбросить{': ' + str(err) if err else ''}",
                     reply_markup=K.kb_admin_purchase_detail())


# ── Промокод ──────────────────────────────────────────────
@router.message(F.text == A_PROMO)
async def on_promo(msg: Message, state: FSMContext):
    await state.set_state(Adm.promo_type)
    await msg.answer("Тип промокода:", reply_markup=K.kb_admin_promo_type())


@router.message(Adm.promo_type, F.text.in_({A_PROMO_BAL, A_PROMO_DISC}))
async def on_promo_type(msg: Message, state: FSMContext):
    await state.update_data(ptype="balance" if msg.text == A_PROMO_BAL else "discount")
    await state.set_state(Adm.promo_code)
    await msg.answer("Введите код (например SUMMER):", reply_markup=K.kb_admin_back())


@router.message(Adm.promo_code, F.text)
async def on_promo_code(msg: Message, state: FSMContext):
    await state.update_data(code=msg.text.strip())
    await state.set_state(Adm.promo_value)
    await msg.answer(f"Сумма в {CUR} (бонус на баланс / скидка):")


@router.message(Adm.promo_value, F.text)
async def on_promo_value(msg: Message, state: FSMContext):
    try:
        v = float(msg.text.replace(",", "."))
    except ValueError:
        return await msg.answer("❌ Введите число:")
    await state.update_data(value=v)
    await state.set_state(Adm.promo_uses)
    await msg.answer("Сколько активаций максимум?")


@router.message(Adm.promo_uses, F.text)
async def on_promo_uses(msg: Message, state: FSMContext):
    if not msg.text.strip().isdigit():
        return await msg.answer("❌ Введите целое число:")
    d = await state.get_data()
    db.add_promo(d["code"], d["value"], int(msg.text), ptype=d["ptype"])
    await msg.answer(f"✅ Промокод <b>{d['code']}</b> создан.", parse_mode=ParseMode.HTML,
                     reply_markup=K.kb_admin_menu())
    await state.clear()


# ── Цены по странам ───────────────────────────────────────
def _price_key_by_button(text: str) -> str | None:
    for k in C.all_countries():
        base = K.country_button(k, with_price=False)
        if text == base or text.startswith(base + " · "):
            return k
    return None


async def _show_price_list(msg: Message, note: str = ""):
    C.for_sale(force=True)      # свежие страны и цены
    await msg.answer(
        f"{note}💲 <b>ЦЕНЫ ПО СТРАНАМ</b>\n\nВыберите страну, чтобы задать цену ({CUR}).\n"
        f"«—» = цена не задана → клиенты эту страну не видят.",
        parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_prices())


@router.message(F.text == A_PRICES)
async def on_prices(msg: Message, state: FSMContext):
    await state.set_state(Adm.price_pick)
    await _show_price_list(msg)


@router.message(Adm.price_pick, F.text)
async def on_price_pick(msg: Message, state: FSMContext):
    key = _price_key_by_button(msg.text)
    if not key:
        return await msg.answer("Выберите страну кнопкой ⬇️")
    ci = C.get(key)
    cur = C.price(key)
    await state.update_data(price_key=key)
    await state.set_state(Adm.price_value)
    await msg.answer(
        f"{ci['flag']} <b>{ci['name']}</b>\nСейчас: {f'{cur} {CUR}' if cur else 'не задана'}\n\n"
        f"Введите цену в {CUR} (целое число). <b>0</b> — скрыть страну от клиентов.\n"
        f"Например: 49",
        parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_back())


@router.message(Adm.price_value, F.text)
async def on_price_value(msg: Message, state: FSMContext):
    raw = msg.text.strip().replace(CUR, "").replace(",", ".").strip()
    try:
        v = float(raw)
        assert v >= 0 and v == int(v) and v <= 1_000_000
    except (ValueError, AssertionError):
        return await msg.answer("❌ Введите целое число ≥ 0 (например 49):")
    key = (await state.get_data())["price_key"]
    ci = C.get(key)
    C.set_price(key, int(v))
    await state.set_state(Adm.price_pick)
    note = (f"✅ {ci['flag']} {ci['name']}: <b>{int(v)} {CUR}</b>\n\n" if v
            else f"✅ {ci['flag']} {ci['name']}: скрыта от клиентов\n\n")
    await _show_price_list(msg, note)


# ── Рассылка ──────────────────────────────────────────────
@router.message(F.text == A_BROADCAST)
async def on_bc(msg: Message, state: FSMContext):
    await state.set_state(Adm.bc_text)
    await msg.answer("Отправьте сообщение для рассылки.\n\n"
                     "Работает всё, что вы оформили в Telegram: <b>жирный</b>, <i>курсив</i>, цитаты, спойлеры, ссылки, "
                     "<b>премиум-эмодзи</b> (вставляйте их как обычно). Обычные эмодзи бот сам заменит на премиум. "
                     "Можно прислать фото / видео / GIF / файл с подписью.",
                     reply_markup=K.kb_admin_back())


def _bc_payload(msg: Message) -> dict | None:
    """Сообщение админа → то, что уйдёт пользователям. html_text сохраняет форматирование, цитаты и премиум-эмодзи."""
    html_text = msg.html_text or ""
    if msg.photo:
        return {"kind": "photo", "file": msg.photo[-1].file_id, "html": html_text}
    if msg.video:
        return {"kind": "video", "file": msg.video.file_id, "html": html_text}
    if msg.animation:
        return {"kind": "animation", "file": msg.animation.file_id, "html": html_text}
    if msg.document:
        return {"kind": "document", "file": msg.document.file_id, "html": html_text}
    if msg.text:
        return {"kind": "text", "file": None, "html": html_text}
    return None


async def _bc_send_to(bot: Bot, chat_id: int, p: dict, kb=None):
    kw = dict(parse_mode=ParseMode.HTML, reply_markup=kb)
    if p["kind"] == "text":
        return await bot.send_message(chat_id, p["html"], disable_web_page_preview=True, **kw)
    cap = p["html"] or None
    send = {"photo": bot.send_photo, "video": bot.send_video,
            "animation": bot.send_animation, "document": bot.send_document}[p["kind"]]
    return await send(chat_id, p["file"], caption=cap, **kw)


@router.message(Adm.bc_text, F.text | F.photo | F.video | F.animation | F.document)
async def on_bc_text(msg: Message, state: FSMContext, bot: Bot):
    p = _bc_payload(msg)
    if not p:
        return
    await state.update_data(bc=p)
    await state.set_state(Adm.bc_confirm)
    await msg.answer("Предпросмотр — так увидят пользователи:")
    tok = i18n.NO_TRANSLATE.set(True)
    try:
        await _bc_send_to(bot, msg.chat.id, p)
    except Exception as e:
        await msg.answer(f"⚠️ Telegram не принял это сообщение: <code>{html.escape(str(e))[:300]}</code>\n"
                         "Премиум-эмодзи в рассылке нужен Telegram Premium у владельца бота; попробуйте убрать их или упростить текст.")
    finally:
        i18n.NO_TRANSLATE.reset(tok)
    await msg.answer("Отправить всем?", reply_markup=K.kb_admin_broadcast_confirm())


@router.message(Adm.bc_confirm, F.text == A_BC_SEND)
async def on_bc_send(msg: Message, state: FSMContext, bot: Bot):
    from aiogram.exceptions import TelegramRetryAfter
    p = (await state.get_data())["bc"]
    await state.clear()
    ok = fail = 0
    tok = i18n.NO_TRANSLATE.set(True)           # текст рассылки не переводим автоматически
    try:
        for uid in db.get_all_user_ids():
            for attempt in range(2):
                try:
                    await _bc_send_to(bot, uid, p)
                    ok += 1
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    fail += 1
                    break
            await asyncio.sleep(0.05)
    finally:
        i18n.NO_TRANSLATE.reset(tok)
    await msg.answer(f"📢 Рассылка: доставлено {ok}, не доставлено {fail}", reply_markup=K.kb_admin_menu())
