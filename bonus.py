"""
bonus.py — SHOU Бонус: спины за покупки + слот-машина 🎰. ВСЕ числа настраиваются админом (🛠 Админ → 🎰 Спины и бонусы)
и хранятся в bot_settings под ключом «bonus:cfg» (JSON) — менять код или .env не нужно.

Настройки (cfg):
  on            — раздел включён (кнопка в профиле видна, спины начисляются)
  tiers         — [[от скольки ⭐ в заказе, сколько спинов], ...]; начисляется самая высокая подходящая ступень
  prizes        — выигрыш в бонусных ⭐ за комбинацию: 777 / BAR / виноград / лимон
  min_withdraw  — минимум бонусных ⭐ для вывода
  premium_spins — спинов за любой заказ Premium (0 = не начислять)
  ref_spins     — спинов пригласившему за каждую покупку приглашённого (0 = не начислять)
"""
from __future__ import annotations
import copy
import json

from database import db

CFG_KEY = "bonus:cfg"

DEFAULT_CFG: dict = {
    "on": True,
    "tiers": [[200, 1], [500, 3], [1000, 7], [2000, 15]],
    "prizes": {"777": 7, "bar": 5, "grape": 1, "lemon": 1},
    "min_withdraw": 100,
    "premium_spins": 0,
    "ref_spins": 1,
}
DICE_PRIZE = {64: "777", 1: "bar", 22: "grape", 43: "lemon"}      # значение 🎰 в Telegram → ключ приза
PRIZE_LABEL = {"777": "7️⃣ 777 (три семёрки)", "bar": "🟫 три BAR", "grape": "🍇 три винограда", "lemon": "🍋 три лимона"}
MAX_TIERS = 12


def load() -> dict:
    """Текущие настройки (недостающие поля добираются из DEFAULT_CFG, мусор в БД ничего не ломает)."""
    cfg = copy.deepcopy(DEFAULT_CFG)
    try:
        raw = json.loads(db.get_setting(CFG_KEY, "") or "{}")
    except ValueError:
        raw = {}
    if not isinstance(raw, dict):
        return cfg
    try:
        if "on" in raw:
            cfg["on"] = bool(raw["on"])
        if isinstance(raw.get("tiers"), list):
            tiers = {}
            for t in raw["tiers"]:
                a, b = int(t[0]), int(t[1])
                if a > 0 and b > 0:
                    tiers[a] = b
            cfg["tiers"] = [[a, tiers[a]] for a in sorted(tiers)]        # пустой список разрешён: «спины за звёзды выключены»
        if isinstance(raw.get("prizes"), dict):
            for k in cfg["prizes"]:
                if k in raw["prizes"]:
                    cfg["prizes"][k] = max(0, int(raw["prizes"][k]))
        for k in ("min_withdraw", "premium_spins", "ref_spins"):
            if k in raw:
                cfg[k] = max(0, int(raw[k]))
    except (TypeError, ValueError, IndexError):
        return copy.deepcopy(DEFAULT_CFG)
    return cfg


def save(cfg: dict) -> None:
    db.set_setting(CFG_KEY, json.dumps(cfg, ensure_ascii=False))


def reset() -> dict:
    cfg = copy.deepcopy(DEFAULT_CFG)
    save(cfg)
    return cfg


# ── начисление ────────────────────────────────────────────
def spins_for_stars(cfg: dict, stars_qty: int) -> int:
    best = 0
    for need, spins in sorted(cfg["tiers"]):
        if stars_qty >= need:
            best = spins
    return best


def add_spins(uid: int, n: int) -> None:
    b = db.bonus_get(uid)
    db.bonus_set(uid, b["spins"] + n, b["stars"])


def award_order(uid: int, kind: str, qty: int) -> int:
    """Спины за выполненный заказ: stars → по ступеням (qty = число звёзд), premium → фиксированно. Возвращает начисленное."""
    cfg = load()
    if not cfg["on"]:
        return 0
    n = spins_for_stars(cfg, int(qty)) if kind == "stars" else (cfg["premium_spins"] if kind == "premium" else 0)
    if n > 0:
        add_spins(uid, n)
    return n


def ref_spin(uid: int) -> None:
    """Пригласившему — спины за покупку приглашённого."""
    cfg = load()
    if not cfg["on"] or cfg["ref_spins"] <= 0:
        return
    ref = (db.find_user_by_id(uid) or {}).get("referrer_id")
    if ref:
        add_spins(int(ref), cfg["ref_spins"])


def prize_for_dice(cfg: dict, dice_value: int) -> int:
    key = DICE_PRIZE.get(dice_value)
    return int(cfg["prizes"].get(key, 0)) if key else 0


# ── текст ─────────────────────────────────────────────────
def spin_word(n: int) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return "спинов"
    return "спин" if n % 10 == 1 else "спина" if 2 <= n % 10 <= 4 else "спинов"


def spins_str(n: int) -> str:
    return f"{n} {spin_word(n)}"
