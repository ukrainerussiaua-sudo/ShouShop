"""
payments.py — Crypto Bot (счёт в грн) + фоновый контроллер ожидающих платежей.
Перевод на карту подтверждает админ (handlers/topup.py), TON-пополнение ищет tondeposit.py.
"""
from __future__ import annotations
import asyncio
import time as _time
from datetime import datetime as _dt

import aiohttp
from aiogram import Bot
from aiogram.enums import ParseMode

from config import (
    CRYPTO_PAY_TOKEN, CRYPTO_PAY_ASSET, CRYPTO_PAY_BASE_URL,
    CRYPTO_PAY_RETURN_URL, CRYPTO_UAH_TO_USDT_FALLBACK, CARD_PAYMENT_TTL_H, SHOP_NAME,
)
from database import db
from billing import credit_topup


# ══════════════════════════════════════
# CRYPTO BOT
# ══════════════════════════════════════
def crypto_is_configured() -> bool:
    return bool(CRYPTO_PAY_TOKEN)


async def crypto_create_invoice(amount_uah: float, user_id: int):
    """Счёт Crypto Bot в гривнах (fiat UAH — Crypto Bot сам конвертирует) или, если не вышло, в USDT по запасному курсу."""
    if not CRYPTO_PAY_TOKEN:
        return None, None

    base = {
        "description": f"{SHOP_NAME}: пополнение (ID: {user_id})",
        "hidden_message": "Спасибо! Баланс пополнится автоматически.",
        "paid_btn_name": "openBot",
        "paid_btn_url": CRYPTO_PAY_RETURN_URL,
        "payload": str(user_id),
        "allow_comments": False,
        "allow_anonymous": True,
    }

    # Попытка 1: fiat UAH
    fiat_payload = {**base, "currency_type": "fiat", "fiat": "UAH",
                    "amount": f"{amount_uah:.2f}", "accepted_assets": CRYPTO_PAY_ASSET}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{CRYPTO_PAY_BASE_URL}/createInvoice", json=fiat_payload,
                              headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}) as resp:
                data = await resp.json()
                if data.get("ok"):
                    res = data["result"]
                    return res.get("invoice_id"), res.get("bot_invoice_url")
                print(f"[CryptoBot fiat] {data}")
    except Exception as e:
        print(f"[CryptoBot fiat] {e}")

    # Попытка 2: USDT по запасному курсу
    amount_usdt = round(amount_uah / CRYPTO_UAH_TO_USDT_FALLBACK, 2)
    crypto_payload = {**base, "currency_type": "crypto", "asset": CRYPTO_PAY_ASSET, "amount": f"{amount_usdt:.2f}"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{CRYPTO_PAY_BASE_URL}/createInvoice", json=crypto_payload,
                              headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}) as resp:
                data = await resp.json()
                if data.get("ok"):
                    res = data["result"]
                    return res.get("invoice_id"), res.get("bot_invoice_url")
                print(f"[CryptoBot usdt] {data}")
    except Exception as e:
        print(f"[CryptoBot usdt] {e}")
    return None, None


async def crypto_check_invoice(invoice_id: int) -> str | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{CRYPTO_PAY_BASE_URL}/getInvoices", params={"invoice_ids": str(invoice_id)},
                             headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}) as resp:
                data = await resp.json()
                items = data.get("result", {}).get("items", [])
                return items[0]["status"] if items else None
    except Exception as e:
        print(f"[CryptoBot check] {e}")
        return None


# ══════════════════════════════════════
# ФОНОВЫЙ КОНТРОЛЛЕР ПЛАТЕЖЕЙ
# Один цикл смотрит ВСЕ неоплаченные счета из БД, поэтому перезапуск бота их не «теряет»,
# а зачисление идёт через claim_pending → двойной зачёт невозможен.
# ══════════════════════════════════════
CRYPTO_MAX_AGE_H = 24
_FAST_WINDOW_S = 15 * 60       # первые 15 мин проверяем каждые ~4 сек, дальше раз в минуту


def _pending_age_seconds(p: dict) -> float:
    try:
        return (_dt.now() - _dt.strptime(p.get("created_at", ""), "%d.%m.%Y %H:%M:%S")).total_seconds()
    except (ValueError, TypeError):
        return 0.0


async def _safe_send(bot: Bot, uid: int, text: str):
    try:
        await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def credit_payment(bot: Bot, p: dict, label: str):
    """Зачисление оплаченного счёта (claim_pending гарантирует «ровно один раз»)."""
    if not db.claim_pending(p["payment_id"], "paid"):
        return
    await credit_topup(bot, int(p["user_id"]), float(p["amount"]), label)


async def check_pending_once(bot: Bot, last_check: dict[str, float]) -> None:
    """Один проход по открытым счетам. Вынесено отдельно, чтобы тестировать без бесконечного цикла."""
    for p in await asyncio.to_thread(db.list_open_pending):
        pid = p["payment_id"]
        method = p.get("method") or ("crypto" if str(pid).isdigit() else "card")
        age = _pending_age_seconds(p)

        if method == "card":                       # ждёт нажатия «Я оплатил» — только гасим по таймауту
            if age > CARD_PAYMENT_TTL_H * 3600 and db.claim_pending(pid, "timeout"):
                await _safe_send(bot, int(p["user_id"]), "⏰ Заявка на пополнение картой истекла. Создайте новую через «Пополнить».")
            continue
        if method != "crypto":
            continue

        if age > CRYPTO_MAX_AGE_H * 3600:
            if db.claim_pending(pid, "timeout"):
                await _safe_send(bot, int(p["user_id"]), "⏰ Время оплаты истекло. Создайте новый счёт.")
            continue

        interval = 3.5 if age < _FAST_WINDOW_S else 60.0
        if _time.monotonic() - last_check.get(pid, 0.0) < interval:
            continue
        last_check[pid] = _time.monotonic()

        status = await crypto_check_invoice(int(pid))
        if status == "paid":
            await credit_payment(bot, p, "Crypto Bot")
        elif status == "expired" and db.claim_pending(pid, "timeout"):
            await _safe_send(bot, int(p["user_id"]), "⏰ Время оплаты истекло. Создайте новый счёт.")


async def watch_pending(bot: Bot, tick: float = 3.5) -> None:
    """Бесконечный фоновый цикл. Запускается один раз в main.py."""
    last_check: dict[str, float] = {}
    while True:
        try:
            await check_pending_once(bot, last_check)
        except asyncio.CancelledError:
            raise
        except Exception as e:                    # цикл не должен умирать из-за разовой ошибки
            print(f"[watch_pending] {e!r}")
        await asyncio.sleep(tick)
