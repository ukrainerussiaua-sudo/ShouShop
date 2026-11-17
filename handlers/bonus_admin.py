"""
handlers/bonus_admin.py — админка «Спины и бонусы» (SHOU Бонус).
Админ сам решает: включён ли раздел, от скольки ⭐ и сколько спинов давать (любое число ступеней), сколько бонусных ⭐
платит каждая комбинация слота, минимальный вывод, спины за Premium и за рефералов, а также может вручную выдать/забрать спины.
Все значения хранятся в bot_settings (см. bonus.py) и применяются сразу — перезапуск бота не нужен.
"""
from __future__ import annotations
import html
import re

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton as Btn, InlineKeyboardMarkup, Message

import bonus
import i18n
from config import ADMIN_IDS, BONUS_NAME
from database import db
from keyboards import A_BONUS

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class _Raw(BaseMiddleware):
    """Админ-экраны всегда на русском (без автоперевода), даже если у админа выбран UK/EN."""
    async def __call__(self, handler, event, data):
        tok = i18n.NO_TRANSLATE.set(True)
        try:
            return await handler(event, data)
        finally:
            i18n.NO_TRANSLATE.reset(tok)


router.message.outer_middleware(_Raw())
router.callback_query.outer_middleware(_Raw())


class BonusAdm(StatesGroup):
    tier = State()
    prize = State()
    minwd = State()
    prem = State()
    ref = State()
    give = State()


def _b(text: str, data: str) -> Btn:
    return Btn(text=text, callback_data=data)


# ── главный экран ─────────────────────────────────────────
def _panel_text(note: str = "") -> str:
    c = bonus.load()
    pr = c["prizes"]
    tiers = "\n".join(f"  • от <b>{a}</b> ⭐ → <b>{bonus.spins_str(n)}</b>" for a, n in c["tiers"]) or "  — ступеней нет (за звёзды спины не дают)"
    prem = f"<b>{bonus.spins_str(c['premium_spins'])}</b> за заказ" if c["premium_spins"] else "не начисляются"
    ref = f"<b>+{bonus.spins_str(c['ref_spins'])}</b> за покупку приглашённого" if c["ref_spins"] else "не начисляются"
    return (f"{note}🎰 <b>СПИНЫ И БОНУСЫ — {html.escape(BONUS_NAME)} Бонус</b>\n\n"
            f"Раздел: {'✅ включён' if c['on'] else '⛔ выключен (кнопка в профиле скрыта, спины не начисляются)'}\n\n"
            f"<b>Спины за Stars</b> (по числу ⭐ в заказе, берётся самая высокая подходящая ступень):\n{tiers}\n\n"
            f"<b>Спины за Premium:</b> {prem}\n"
            f"<b>Спины рефереру:</b> {ref}\n\n"
            f"<b>Призы слота</b> (бонусные ⭐):\n"
            f"  • {bonus.PRIZE_LABEL['777']} → <b>{pr['777']}</b>\n  • {bonus.PRIZE_LABEL['bar']} → <b>{pr['bar']}</b>\n"
            f"  • {bonus.PRIZE_LABEL['grape']} → <b>{pr['grape']}</b>\n  • {bonus.PRIZE_LABEL['lemon']} → <b>{pr['lemon']}</b>\n\n"
            f"<b>Минимальный вывод:</b> {c['min_withdraw']} ⭐")


def _panel_kb() -> InlineKeyboardMarkup:
    on = bonus.load()["on"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [_b("⛔ Выключить раздел" if on else "✅ Включить раздел", "bn:toggle")],
        [_b("📊 Ступени (⭐ → спины)", "bn:tiers"), _b("🎰 Призы слота", "bn:prizes")],
        [_b("👑 Спины за Premium", "bn:prem"), _b("👥 Спины за рефералов", "bn:ref")],
        [_b("💸 Мин. вывод", "bn:minwd"), _b("🎁 Выдать/забрать", "bn:give")],
        [_b("📈 Статистика", "bn:stats"), _b("♻️ Сбросить к стандарту", "bn:reset")],
    ])


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_b("↩️ Отмена", "bn:menu")]])


async def _show_panel(target: Message | CallbackQuery, note: str = "") -> None:
    text, kb = _panel_text(note), _panel_kb()
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            if "not modified" not in str(e):
                await target.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await target.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.message(F.text == A_BONUS)
@router.message(Command("bonus"))
async def on_open(msg: Message, state: FSMContext):
    await state.clear()
    await _show_panel(msg)


@router.callback_query(F.data == "bn:menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await _show_panel(cb)


@router.callback_query(F.data == "bn:toggle")
async def cb_toggle(cb: CallbackQuery):
    c = bonus.load()
    c["on"] = not c["on"]
    bonus.save(c)
    await cb.answer("Включено" if c["on"] else "Выключено")
    await _show_panel(cb)


# ── ступени ───────────────────────────────────────────────
def _tiers_kb(c: dict) -> InlineKeyboardMarkup:
    rows = [[_b(f"🗑 от {a} ⭐ → {bonus.spins_str(n)}", f"bn:tdel:{a}")] for a, n in c["tiers"]]
    rows.append([_b("➕ Добавить / изменить ступень", "bn:tadd")])
    rows.append([_b("↩️ Назад", "bn:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_tiers(cb: CallbackQuery, note: str = ""):
    c = bonus.load()
    text = (f"{note}📊 <b>СТУПЕНИ СПИНОВ</b>\n\nСколько спинов получает клиент за заказ Stars. "
            "Нажмите на ступень, чтобы <b>удалить</b> её, или добавьте новую.\n"
            "Начисляется одна — самая высокая ступень, до которой дотянул заказ "
            "(например, 200→1, 500→3: заказ на 600 ⭐ даст 3 спина).")
    try:
        await cb.message.edit_text(text, reply_markup=_tiers_kb(c), parse_mode=ParseMode.HTML)
    except Exception as e:
        if "not modified" not in str(e):
            print(f"[bonus_admin tiers] {e!r}")


@router.callback_query(F.data == "bn:tiers")
async def cb_tiers(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await _show_tiers(cb)


@router.callback_query(F.data.startswith("bn:tdel:"))
async def cb_tier_del(cb: CallbackQuery):
    a = int(cb.data.split(":")[2])
    c = bonus.load()
    c["tiers"] = [t for t in c["tiers"] if t[0] != a]
    bonus.save(c)
    await cb.answer("Ступень удалена")
    await _show_tiers(cb, f"🗑 Ступень «от {a} ⭐» удалена.\n\n")


@router.callback_query(F.data == "bn:tadd")
async def cb_tier_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BonusAdm.tier)
    await cb.answer()
    await cb.message.answer("Введите два числа: <b>от скольки ⭐</b> и <b>сколько спинов</b>.\n\n"
                            "Например: <code>200 1</code> — от 200 ⭐ дарим 1 спин.\n"
                            "Если ступень с таким порогом уже есть, она обновится.",
                            parse_mode=ParseMode.HTML, reply_markup=_cancel_kb())


def _ints(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"-?\d+", text or "")]


@router.message(BonusAdm.tier, F.text)
async def on_tier(msg: Message, state: FSMContext):
    n = _ints(msg.text)
    if len(n) != 2 or not (1 <= n[0] <= 10_000_000) or not (1 <= n[1] <= 1000):
        return await msg.answer("❌ Нужно два числа, например <code>200 1</code> (⭐ от 1, спинов от 1 до 1000).",
                                parse_mode=ParseMode.HTML, reply_markup=_cancel_kb())
    c = bonus.load()
    t = dict((a, s) for a, s in c["tiers"])
    if n[0] not in t and len(t) >= bonus.MAX_TIERS:
        return await msg.answer(f"❌ Максимум {bonus.MAX_TIERS} ступеней — удалите лишнюю.", reply_markup=_cancel_kb())
    t[n[0]] = n[1]
    c["tiers"] = [[a, t[a]] for a in sorted(t)]
    bonus.save(c)
    await state.clear()
    await msg.answer(f"✅ Ступень сохранена: от <b>{n[0]}</b> ⭐ → <b>{bonus.spins_str(n[1])}</b>.", parse_mode=ParseMode.HTML)
    await _show_panel(msg)


# ── призы слота ───────────────────────────────────────────
@router.callback_query(F.data == "bn:prizes")
async def cb_prizes(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    pr = bonus.load()["prizes"]
    rows = [[_b(f"{bonus.PRIZE_LABEL[k]} → {pr[k]} ⭐", f"bn:pz:{k}")] for k in bonus.PRIZE_LABEL]
    rows.append([_b("↩️ Назад", "bn:menu")])
    await cb.message.edit_text("🎰 <b>ПРИЗЫ СЛОТА</b>\n\nСколько бонусных ⭐ выигрывает клиент за комбинацию. "
                               "Нажмите на комбинацию, чтобы изменить. <b>0</b> — комбинация ничего не платит.",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith("bn:pz:"))
async def cb_prize_pick(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    if key not in bonus.PRIZE_LABEL:
        return await cb.answer()
    await state.set_state(BonusAdm.prize)
    await state.update_data(prize_key=key)
    await cb.answer()
    await cb.message.answer(f"{bonus.PRIZE_LABEL[key]}\nСейчас: <b>{bonus.load()['prizes'][key]}</b> ⭐\n\n"
                            "Введите новый выигрыш (целое число ⭐, 0 — не платить):",
                            parse_mode=ParseMode.HTML, reply_markup=_cancel_kb())


@router.message(BonusAdm.prize, F.text)
async def on_prize(msg: Message, state: FSMContext):
    n = _ints(msg.text)
    if len(n) != 1 or not (0 <= n[0] <= 100_000):
        return await msg.answer("❌ Введите одно целое число от 0 до 100000.", reply_markup=_cancel_kb())
    key = (await state.get_data()).get("prize_key")
    c = bonus.load()
    c["prizes"][key] = n[0]
    bonus.save(c)
    await state.clear()
    await _show_panel(msg, f"✅ {bonus.PRIZE_LABEL[key]} → <b>{n[0]}</b> ⭐\n\n")


# ── одиночные числа: Premium / рефералы / мин. вывод ──
_SINGLE = {
    "prem":  (BonusAdm.prem,  "premium_spins", "👑 Спины за Premium",
              "Сколько спинов давать за каждый заказ Premium (любой срок). <b>0</b> — не давать.", 0, 1000),
    "ref":   (BonusAdm.ref,   "ref_spins",     "👥 Спины рефереру",
              "Сколько спинов получает пригласивший за каждую покупку приглашённого. <b>0</b> — не давать.", 0, 1000),
    "minwd": (BonusAdm.minwd, "min_withdraw",  "💸 Минимальный вывод",
              "Минимум бонусных ⭐ для заявки на вывод. <b>0</b> — любая сумма больше нуля.", 0, 1_000_000),
}


@router.callback_query(F.data.in_({"bn:prem", "bn:ref", "bn:minwd"}))
async def cb_single(cb: CallbackQuery, state: FSMContext):
    st, field, title, hint, _, _ = _SINGLE[cb.data.split(":")[1]]
    await state.set_state(st)
    await cb.answer()
    await cb.message.answer(f"{title}\nСейчас: <b>{bonus.load()[field]}</b>\n\n{hint}\nВведите число:",
                            parse_mode=ParseMode.HTML, reply_markup=_cancel_kb())


async def _save_single(msg: Message, state: FSMContext, key: str):
    _, field, title, _, lo, hi = _SINGLE[key]
    n = _ints(msg.text)
    if len(n) != 1 or not (lo <= n[0] <= hi):
        return await msg.answer(f"❌ Введите одно целое число от {lo} до {hi}.", reply_markup=_cancel_kb())
    c = bonus.load()
    c[field] = n[0]
    bonus.save(c)
    await state.clear()
    await _show_panel(msg, f"✅ {title}: <b>{n[0]}</b>\n\n")


@router.message(BonusAdm.prem, F.text)
async def on_prem(msg: Message, state: FSMContext):
    await _save_single(msg, state, "prem")


@router.message(BonusAdm.ref, F.text)
async def on_ref(msg: Message, state: FSMContext):
    await _save_single(msg, state, "ref")


@router.message(BonusAdm.minwd, F.text)
async def on_minwd(msg: Message, state: FSMContext):
    await _save_single(msg, state, "minwd")


# ── ручная выдача ─────────────────────────────────────────
@router.callback_query(F.data == "bn:give")
async def cb_give(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BonusAdm.give)
    await cb.answer()
    await cb.message.answer("🎁 <b>Выдать / забрать спины</b>\n\nФормат: <code>ID_пользователя спины [бонусные_⭐]</code>\n"
                            "Примеры:\n<code>123456789 5</code> — выдать 5 спинов\n"
                            "<code>123456789 -2</code> — забрать 2 спина\n"
                            "<code>123456789 0 50</code> — выдать 50 бонусных ⭐ (без спинов)\n\n"
                            "Пользователь должен хотя бы раз запускать бота.",
                            parse_mode=ParseMode.HTML, reply_markup=_cancel_kb())


@router.message(BonusAdm.give, F.text)
async def on_give(msg: Message, state: FSMContext, bot: Bot):
    n = _ints(msg.text)
    if len(n) not in (2, 3) or n[0] <= 0 or abs(n[1]) > 100_000 or (len(n) == 3 and abs(n[2]) > 1_000_000):
        return await msg.answer("❌ Формат: <code>ID спины [⭐]</code>, например <code>123456789 5</code>.",
                                parse_mode=ParseMode.HTML, reply_markup=_cancel_kb())
    uid, d_spins, d_stars = n[0], n[1], (n[2] if len(n) == 3 else 0)
    if not db.find_user_by_id(uid):
        return await msg.answer("❌ Пользователь не найден (он ещё не запускал бота). Проверьте ID:", reply_markup=_cancel_kb())
    b = db.bonus_get(uid)
    spins, stars = max(0, b["spins"] + d_spins), max(0, b["stars"] + d_stars)
    db.bonus_set(uid, spins, stars)
    await state.clear()
    notified = ""
    if d_spins > 0:
        tok = i18n.NO_TRANSLATE.set(False)                      # клиенту — на его языке
        try:
            await bot.send_message(uid, f"🎰 Бонус: +{d_spins} спин(ов) в разделе «Профиль → {BONUS_NAME} Бонус».")
            notified = "\nКлиент уведомлён."
        except Exception:
            notified = "\nКлиенту не удалось отправить уведомление."
        finally:
            i18n.NO_TRANSLATE.reset(tok)
    await _show_panel(msg, f"✅ <code>{uid}</code>: спинов <b>{spins}</b>, бонусных ⭐ <b>{stars}</b>.{notified}\n\n")


# ── статистика / сброс ────────────────────────────────────
@router.callback_query(F.data == "bn:stats")
async def cb_stats(cb: CallbackQuery):
    t = db.bonus_totals()
    await cb.answer()
    await cb.message.answer("📈 <b>Статистика бонусов</b>\n\n"
                            f"Игроков с остатком: <b>{t['users']}</b>\n"
                            f"Неиспользованных спинов: <b>{t['spins']}</b>\n"
                            f"Бонусных ⭐ на счетах (ещё не выведено): <b>{t['stars']}</b>",
                            parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "bn:reset")
async def cb_reset(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text("♻️ Вернуть <b>все настройки бонуса</b> к стандартным значениям "
                               "(ступени 200→1, 500→3, 1000→7, 2000→15, призы 7/5/1/1, вывод от 100)? "
                               "Спины и ⭐ клиентов не затрагиваются.",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                   [_b("✅ Да, сбросить", "bn:reset_ok"), _b("↩️ Отмена", "bn:menu")]]),
                               parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "bn:reset_ok")
async def cb_reset_ok(cb: CallbackQuery):
    bonus.reset()
    await cb.answer("Сброшено")
    await _show_panel(cb, "♻️ Настройки сброшены к стандартным.\n\n")
