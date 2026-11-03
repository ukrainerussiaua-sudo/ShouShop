"""
countries.py — список стран магазина. Источник: Supabase (shop_countries),
запасной вариант — config.COUNTRIES. Кэш 60 сек, чтобы не дёргать БД на каждое нажатие.
Формат страны: {"flag", "name", "iso", "code"}.
"""
from __future__ import annotations
import time

from config import COUNTRIES as _FALLBACK
from database import db

_TTL = 60.0
_cache: dict = {"ts": 0.0, "all": {}, "active": {}}
_pcache: dict = {"ts": 0.0, "data": {}}


def _row_to_country(r: dict) -> dict:
    return {"flag": r.get("flag") or "🌍", "name": r.get("name") or r["key"],
            "iso": str(r.get("iso") or "").upper(), "code": r.get("phone_code") or ""}


def _refresh(force: bool = False) -> None:
    if not force and _cache["all"] and time.monotonic() - _cache["ts"] < _TTL:
        return
    rows = db.get_shop_countries()
    if rows:
        allc = {r["key"]: _row_to_country(r) for r in rows}
        active = {r["key"]: allc[r["key"]] for r in rows
                  if int(r.get("active") or 0) == 1 and allc[r["key"]]["iso"]}
    else:                                   # таблицы нет / пусто → запасной список из config
        allc = dict(_FALLBACK)
        active = dict(_FALLBACK)
    _cache.update(ts=time.monotonic(), all=allc, active=active)


def _refresh_prices(force: bool = False) -> None:
    if not force and time.monotonic() - _pcache["ts"] < _TTL:
        return
    _pcache.update(ts=time.monotonic(), data=db.get_country_prices())


def price(key: str | None) -> int:
    """Цена страны в ₽ (0 = не задана → товар скрыт)."""
    _refresh_prices()
    return int(_pcache["data"].get(key or "", 0))


def set_price(key: str, value: int) -> None:
    """Сохраняет цену и сразу обновляет кэш (клиенты увидят её без ожидания TTL)."""
    db.set_country_price(key, value)
    _refresh_prices(force=True)


def for_sale(force: bool = False) -> dict[str, dict]:
    """Страны, которые видит клиент: активные И с заданной ценой."""
    if force:
        _refresh_prices(True)
    return {k: c for k, c in all_countries(force).items() if price(k) > 0}


def all_countries(force: bool = False) -> dict[str, dict]:
    """Активные страны в порядке sort_order (для меню выбора)."""
    _refresh(force)
    return _cache["active"]


def get(key: str | None) -> dict | None:
    """Страна по ключу, включая скрытые (чтобы старые покупки показывались нормально)."""
    if not key:
        return None
    _refresh()
    return _cache["all"].get(key) or _FALLBACK.get(key)
