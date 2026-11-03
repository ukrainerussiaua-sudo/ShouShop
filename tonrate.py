"""
tonrate.py — курс TON и наценки.

База курса (₴ за 1 TON) — на выбор админа («Настройки TON → Источник курса»):
  • «Сайт»  — рыночный курс CoinGecko (TON→UAH), обновляется фоном раз в TON_RATE_REFRESH_S сек;
  • «Свой»  — число, которое админ вводит вручную.
От базы считаются две цены:
  • принимаем TON от клиента через пополнение баланса переводом: база − наценка покупки;
  • продаём TON клиенту («Купить TON»):                                        база + наценка продажи.
Пример: база 130, покупка −4, продажа +5 → клиент получает 126 ₴ за 1 TON, а платит за 1 TON 135 ₴.
Безопасность: курс сайта старше TON_RATE_MAX_AGE_S (сбой источника) не используется — TON-операции встают на паузу, а не идут по устаревшей цене.
"""
from __future__ import annotations
import asyncio
import time

import aiohttp

import config as cfg
from database import db

SET_MODE = "ton_rate_mode"          # site | manual
SET_MANUAL = "ton_rate_manual"
SET_MARGIN_BUY = "ton_margin_buy"   # сколько ₴ вычитаем из базы, когда покупаем TON у клиента
SET_MARGIN_SELL = "ton_margin_sell" # сколько ₴ добавляем к базе, когда продаём TON клиенту

_site = {"rate": 0.0, "ts": 0.0}
_fail_since = 0.0
_alerted = False


def mode() -> str:
    return "manual" if (db.get_setting(SET_MODE, "site") or "site").strip() == "manual" else "site"


def site_rate() -> float:
    """Последний курс с сайта, если он свежий; иначе 0."""
    if _site["rate"] > 0 and time.time() - _site["ts"] <= cfg.TON_RATE_MAX_AGE_S:
        return _site["rate"]
    return 0.0


def base_rate() -> float:
    if mode() == "manual":
        return max(0.0, db.get_float(SET_MANUAL, 0.0))
    return site_rate()


def margin_buy() -> float:
    return db.get_float(SET_MARGIN_BUY, 0.0)


def margin_sell() -> float:
    return db.get_float(SET_MARGIN_SELL, 0.0)


def buy_rate() -> float:
    """₴, которые клиент получает за 1 TON. Знак ввода не меняет смысл: -3 и 3 оба означают курс −3 ₴."""
    b = base_rate()
    r = b - abs(margin_buy())
    return round(r, 2) if b > 0 and r > 0 else 0.0


def sell_price() -> float:
    """₴, которые клиент платит за 1 TON при покупке. 0 = недоступно."""
    b = base_rate()
    r = b + margin_sell()
    return round(r, 2) if b > 0 and r > 0 else 0.0


async def fetch_site_rate() -> float | None:
    headers = {"x-cg-demo-api-key": cfg.COINGECKO_API_KEY} if cfg.COINGECKO_API_KEY else {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": "the-open-network", "vs_currencies": "uah"}, headers=headers) as r:
                js = await r.json()
        v = float(js["the-open-network"]["uah"])
        return v if v > 0 else None
    except Exception as e:
        print(f"[tonrate] CoinGecko: {e!r}")
        return None


async def refresh() -> float | None:
    """Обновляет кэш курса сайта. Возвращает курс или None при сбое."""
    v = await fetch_site_rate()
    if v:
        _site["rate"], _site["ts"] = v, time.time()
    return v


async def watch_ton_rate(bot) -> None:
    """Фоновый цикл: держит курс сайта свежим; при долгом сбое один раз предупреждает админов."""
    global _fail_since, _alerted
    from utils import notify_admins
    while True:
        try:
            v = await refresh()
            if v:
                if _alerted:
                    await notify_admins(bot, f"✅ Курс TON с сайта снова доступен: {v:g} ₴. TON-операции возобновлены.")
                _fail_since, _alerted = 0.0, False
            else:
                _fail_since = _fail_since or time.time()
                if (not _alerted and mode() == "site" and time.time() - _fail_since > 600):
                    _alerted = True
                    await notify_admins(bot, "🚨 Не получается получить курс TON с сайта уже >10 минут. Пока он недоступен, "
                                             "покупка TON и пополнение баланса через TON приостановлены. "
                                             "Можно переключить источник на «Свой» в «Настройки TON».")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[tonrate watch] {e!r}")
        await asyncio.sleep(cfg.TON_RATE_REFRESH_S)
