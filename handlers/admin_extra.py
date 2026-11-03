"""
handlers/admin_extra.py — дополнительные страницы админ-панели:
  • «Последние покупки» — журнал продаж с выручкой и прибылью;
  • «Последние пополнения» — кто, на сколько и чем пополнил баланс;
  • «TON без привязки» — переводы на адрес приёма без ID/с неверным ID;
  • редактор настроек TON, реквизитов карты и себестоимости.
"""
from __future__ import annotations
import html

from aiogram import Router, F
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.enums import ParseMode

import ton_wallet
import tondeposit
import tonrate
from config import ADMIN_IDS, CUR, LOLZ_RUB_TO_UAH
from database import db
import keyboards as K
from keyboards import (
    A_LAST_SALES, A_LAST_TOPUPS, A_UNMATCHED, A_TON_SET, A_CARD_SET, A_COST_SET, TO_ADMIN,
)
from utils import money, user_label

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))

KIND_T = {"account": "✈️ Аккаунт", "stars": "⭐ Stars", "premium": "💎 Premium", "ton": "💠 TON"}


# ═════════════════════════════════════════════════════════
# Списки
# ═════════════════════════════════════════════════════════
def _who(uid: int, cache: dict) -> str:
    if uid not in cache:
        u = db.find_user_by_id(uid) or {}
        cache[uid] = f"@{u['username']}" if u.get("username") else f"id {uid}"
    return html.escape(cache[uid])


@router.message(F.text == A_LAST_SALES)
async def on_last_sales(msg: Message):
    rows = db.get_sales(20)
    if not rows:
        return await msg.answer("🛒 Продаж пока нет (или не создана таблица sales — блок «14» в supabase_schema.sql).")
    cache: dict = {}
    lines = []
    for r in rows:
        price, cost, ref = float(r["price"]), float(r["cost"]), float(r.get("ref_bonus") or 0)
        lines.append(f"{KIND_T.get(r['kind'], r['kind'])} · {html.escape(r.get('title') or '')}\n"
                     f"   👤 {_who(int(r['user_id']), cache)} · 🕒 {r.get('created_at', '')}\n"
                     f"   💰 {money(price)} · себест. {money(cost)} · прибыль <b>{money(price - cost - ref)}</b>")
    total = sum(float(r["price"]) for r in rows)
    await msg.answer(f"🛒 <b>ПОСЛЕДНИЕ ПОКУПКИ</b> (20)\n\n" + "\n\n".join(lines) + f"\n\nИтого по списку: {money(total)}",
                     parse_mode=ParseMode.HTML)


@router.message(F.text == A_LAST_TOPUPS)
async def on_last_topups(msg: Message):
    rows = db.get_recent_topups(25)
    if not rows:
        return await msg.answer("💳 Пополнений пока нет.")
    cache: dict = {}
    lines = [f"📅 {r.get('created_at', '')} · {_who(int(r['user_id']), cache)} · <b>+{money(float(r['amount']))}</b> · "
             f"{html.escape(r.get('method') or '')}" for r in rows]
    await msg.answer("💳 <b>ПОСЛЕДНИЕ ПОПОЛНЕНИЯ</b> (25)\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(F.text == A_UNMATCHED)
async def on_unmatched(msg: Message):
    rows = db.list_unmatched_ton(15)
    if not rows:
        return await msg.answer("💠 Непривязанных TON-переводов нет.")
    lines = [f"📅 {r.get('created_at', '')} · <b>{float(r['amount_ton']):g} TON</b>\n"
             f"   комментарий: <code>{html.escape(r.get('comment') or '') or '—'}</code>\n"
             f"   от: <code>{html.escape(r.get('sender') or '')}</code>" for r in rows]
    await msg.answer("💠 <b>TON БЕЗ ПРИВЯЗКИ</b>\n\nПереводы без ID / с несуществующим ID — автоматически не зачислены. "
                     "Найдите клиента и начислите вручную («Начислить баланс»).\n\n" + "\n\n".join(lines),
                     parse_mode=ParseMode.HTML)


# ═════════════════════════════════════════════════════════
# Редактор настроек
# ═════════════════════════════════════════════════════════
# (ключ, подпись кнопки, тип, единица, подсказка)
GROUPS: dict[str, dict] = {
    "ton": {
        "title": "💠 <b>НАСТРОЙКИ TON</b>",
        "items": [
            ("ton_deposit_address", "Адрес приёма TON", "addr", "",
             "Куда клиенты шлют TON для пополнения баланса (с их ID в комментарии). Пусто — адрес кошелька бота."),
            ("ton_rate_mode", "Источник курса", "mode", "",
             "Сайт — рыночный курс CoinGecko (TON→UAH), обновляется каждую минуту. Свой — число, которое вы вводите вручную. Нажмите, чтобы переключить."),
            ("ton_rate_manual", "Свой курс", "float", f" {CUR}/TON",
             "Базовый курс 1 TON в ₴, если выбран источник «Свой»."),
            ("ton_margin_buy", "Покупка у клиента (−)", "signed", f" {CUR}",
             "Сколько ₴ ВЫЧИТАЕМ из курса, когда покупаем TON у клиента и при пополнении через TON. "
             "Можно ввести 3 или -3 — в обоих случаях при курсе 130 клиент получает 127 ₴ за 1 TON."),
            ("ton_margin_sell", "Продажа клиенту (+)", "signed", f" {CUR}",
             "Сколько ₴ ДОБАВЛЯЕМ к курсу, когда продаём TON клиенту («Купить TON»). Пример: 5 → при курсе 130 клиент платит 135 ₴ за 1 TON."),
            ("ton_min_deposit", "Мин. пополнение", "float", " TON",
             "Переводы меньше этой суммы не зачисляются. 0 — принимать любые."),
            ("ton_min_buy", "Мин. покупка", "float", " TON", "Минимум TON за один заказ."),
            ("ton_max_buy", "Макс. покупка", "float", " TON", "Максимум TON за один заказ."),
        ]},
    "card": {
        "title": "💳 <b>РЕКВИЗИТЫ КАРТЫ</b>",
        "items": [
            ("card_number", "Номер карты", "card", "", "Номер карты, куда клиенты переводят ₴."),
            ("card_holder", "Получатель", "text", "", "ФИО владельца карты (как увидит клиент)."),
            ("card_bank", "Банк", "text", "", "Название банка (необязательно)."),
            ("min_topup", "Мин. пополнение", "float", f" {CUR}", "Минимальная сумма пополнения (карта/Crypto Bot)."),
            ("max_topup", "Макс. пополнение", "float", f" {CUR}", "Максимальная сумма пополнения."),
        ]},
    "cost": {
        "title": "💸 <b>СЕБЕСТОИМОСТЬ</b> (для чистой прибыли в статистике)",
        "items": [
            ("rub_uah_rate", "Курс Lolz", "float", f" {CUR} за 1 ₽",
             f"Сколько ₴ стоит 1 ₽ (цены Lolz — в рублях). Нужен для сравнения цены Lolz с вашей ценой и для себестоимости аккаунтов. "
             f"По умолчанию {LOLZ_RUB_TO_UAH:g}."),
            ("cost_star", "Звезда", "float", f" {CUR}/шт", "Во сколько вам обходится 1 звезда."),
            ("cost_premium_3", "Premium 3 мес", "float", f" {CUR}", "Себестоимость Premium на 3 месяца."),
            ("cost_premium_6", "Premium 6 мес", "float", f" {CUR}", "Себестоимость Premium на 6 месяцев."),
            ("cost_premium_12", "Premium 12 мес", "float", f" {CUR}", "Себестоимость Premium на 12 месяцев."),
            ("cost_ton", "TON", "float", f" {CUR}/TON", "Во сколько вам обходится 1 TON (по какому курсу вы его покупаете)."),
        ]},
}
ENTRY = {A_TON_SET: "ton", A_CARD_SET: "card", A_COST_SET: "cost"}


class AdmSet(StatesGroup):
    pick = State()
    value = State()


def _display(key: str, kind: str, unit: str) -> str:
    raw = (db.get_setting(key, "") or "").strip()
    if kind == "mode":
        return "Сайт (CoinGecko)" if tonrate.mode() == "site" else "Свой"
    if not raw:
        if key == "ton_deposit_address":
            a = tondeposit.deposit_address()
            return f"{a[:6]}…{a[-4:]} (кошелёк бота)" if a else "—"
        if key == "rub_uah_rate":
            return f"{LOLZ_RUB_TO_UAH:g}{unit} (по умолч.)"
        return "—"
    if kind in ("float", "signed"):
        try:
            return f"{float(raw):g}{unit}"
        except ValueError:
            return raw
    if kind == "addr":
        return f"{raw[:6]}…{raw[-4:]}"
    if kind == "card":
        return "•••• " + raw.replace(" ", "")[-4:]
    return raw[:25]


def _label(g: dict, item: tuple) -> str:
    key, name, kind, unit, _ = item
    return f"{name} · {_display(key, kind, unit)}"


async def _show_group(msg: Message, state: FSMContext, gkey: str, note: str = ""):
    g = GROUPS[gkey]
    await state.set_state(AdmSet.pick)
    await state.update_data(set_group=gkey)
    extra = ""
    if gkey == "ton":
        dep = tondeposit.deposit_address()
        base = tonrate.base_rate()
        site = tonrate.site_rate()
        src = (f"сайт: {site:g} {CUR}" if site else "сайт: ⚠️ нет свежего курса") if tonrate.mode() == "site" else "ваш курс"
        buy, sell = tonrate.buy_rate(), tonrate.sell_price()
        spread = f"\nСпред: {sell - buy:g} {CUR} с каждого TON при обороте туда-обратно" if buy and sell else ""
        warn = "\n⚠️ Цена продажи не выше цены выкупа — вы теряете деньги на обороте!" if buy and sell and sell <= buy else ""
        extra = (f"\n📈 Базовый курс: <b>{base:g} {CUR}</b> ({src})\n"
                 f"⬇️ Курс TON-пополнения (база − вычет): <b>{buy:g} {CUR}</b>\n"
                 f"⬆️ Продажа клиенту (покупка TON): <b>{sell:g} {CUR}</b>{spread}{warn}\n\n"
                 f"TON-пополнение баланса: {'✅ включено' if tondeposit.is_enabled() else '❌ выключено (нужен адрес и курс > 0)'}\n"
                 f"Покупка TON клиентами: "
                 f"{'✅ включена' if ton_wallet.is_configured() and sell > 0 else '❌ выключена (нужны TON_MNEMONIC и курс > 0)'}\n")
    await msg.answer(f"{note}{g['title']}\n\nНажмите параметр, чтобы изменить.\n{extra}",
                     parse_mode=ParseMode.HTML,
                     reply_markup=K.kb_admin_settings([_label(g, it) for it in g["items"]]))


@router.message(F.text.in_(set(ENTRY)))
async def on_group(msg: Message, state: FSMContext):
    await _show_group(msg, state, ENTRY[msg.text])


def _find_item(gkey: str, text: str):
    for it in GROUPS[gkey]["items"]:
        if text == _label(GROUPS[gkey], it) or text.startswith(it[1] + " · "):
            return it
    return None


@router.message(AdmSet.pick, F.text)
async def on_pick(msg: Message, state: FSMContext):
    gkey = (await state.get_data()).get("set_group")
    it = _find_item(gkey, msg.text) if gkey in GROUPS else None
    if not it:
        return await msg.answer("Выберите параметр кнопкой ⬇️")
    key, name, kind, unit, hint = it
    if kind == "mode":                      # переключатель: Сайт ⇄ Свой
        new = "manual" if tonrate.mode() == "site" else "site"
        db.set_setting(key, new)
        if new == "site":
            await tonrate.refresh()
        note = f"✅ Источник курса: <b>{_display(key, kind, unit)}</b>\n"
        if new == "manual" and tonrate.base_rate() <= 0:
            note += "⚠️ Свой курс не задан — введите его кнопкой «Свой курс».\n"
        return await _show_group(msg, state, gkey, note + "\n")
    await state.set_state(AdmSet.value)
    await state.update_data(set_key=key)
    clear = ("\n«-» — очистить значение." if kind in ("text", "addr", "card")
             else "\n0 — не использовать." if kind == "signed" else "\n0 — выключить / не использовать.")
    await msg.answer(f"<b>{name}</b>\nСейчас: {_display(key, kind, unit)}\n\n{html.escape(hint)}{clear}\n\nВведите новое значение:",
                     parse_mode=ParseMode.HTML, reply_markup=K.kb_admin_back())


@router.message(AdmSet.value, F.text)
async def on_value(msg: Message, state: FSMContext):
    data = await state.get_data()
    gkey, key = data.get("set_group"), data.get("set_key")
    it = next((i for i in GROUPS.get(gkey, {}).get("items", []) if i[0] == key), None)
    if not it:
        return await _show_group(msg, state, "ton") if gkey == "ton" else await msg.answer("Ошибка, откройте раздел заново.")
    _, name, kind, unit, _ = it
    raw = msg.text.strip()
    clearing = raw in ("-", "—")

    if kind == "float":
        try:
            v = float(raw.replace(",", ".").replace(CUR, "").strip())
            assert 0 <= v <= 10_000_000
        except (ValueError, AssertionError):
            return await msg.answer("❌ Введите число ≥ 0 (например 12.5):")
        db.set_setting(key, f"{v:g}")
    elif kind == "signed":
        try:
            v = float(raw.replace(",", ".").replace("−", "-").replace(CUR, "").replace("+", "").strip())
            assert -100000 <= v <= 100000
        except (ValueError, AssertionError):
            return await msg.answer("❌ Введите число (например 4 или 5.5; можно с минусом):")
        db.set_setting(key, f"{v:g}")
    elif clearing:
        db.set_setting(key, "")
    elif kind == "addr":
        if not tondeposit.valid_address(raw):
            return await msg.answer("❌ Это не похоже на корректный TON-адрес (UQ…/EQ…, 48 символов). Введите ещё раз:")
        db.set_setting(key, raw)
    elif kind == "card":
        digits = "".join(ch for ch in raw if ch.isdigit())
        if not 12 <= len(digits) <= 19:
            return await msg.answer("❌ Номер карты — от 12 до 19 цифр. Введите ещё раз:")
        db.set_setting(key, " ".join(digits[i:i + 4] for i in range(0, len(digits), 4)))
    else:
        db.set_setting(key, raw[:100])

    warn = ""
    if key == "ton_deposit_address":
        tondeposit.reset_since()        # старую историю адреса не зачитываем
        warn = ("\n⚠️ Адрес изменён: учитываются только переводы, пришедшие с этого момента. "
                "Пока все клиенты не завершили старые пополнения — не меняйте адрес без нужды.\n")
    await _show_group(msg, state, gkey, f"✅ {name}: <b>{_display(key, kind, unit)}</b>{warn}\n\n")
