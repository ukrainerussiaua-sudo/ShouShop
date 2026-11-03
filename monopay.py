"""
monopay.py — оплата переводом на карту Monobank с АВТОПРОВЕРКОЙ по Monobank Open API (api.monobank.ua).

Как это работает:
  1. Клиент вводит сумму → бот выдаёт ему УНИКАЛЬНУЮ сумму с копейками (например 100.37 ₴) — по ней платёж однозначно
     опознаётся, даже если двое платят «100 грн» одновременно. На баланс зачисляется ровно то, что перевели.
  2. Клиент переводит на карту и жмёт «Проверить оплату». Бот пишет «идёт проверка, подождите минуту», запрашивает выписку
     по счёту (GET /personal/statement) и ищет входящий платёж на эту сумму. Нашёл — зачисляет; не нашёл за минуту — «платежа нет».
  3. Параллельно фоновый цикл сам проверяет открытые заявки раз в минуту (клиент мог оплатить и не нажать кнопку).
  4. Транзакция «занимается» атомарно (таблица used_mono_tx, PK = id транзакции): одну реальную оплату нельзя зачесть дважды.

Лимит Monobank: выписка — не чаще 1 раза в 60 секунд НА ТОКЕН. Поэтому запросы идут через одну очередь и один общий кэш выписки:
проверка клиента ждёт свежую выписку (до ~минуты), а не «старую», сделанную до его оплаты.

Токен: застосунок Monobank → «Ще» → «Інше» → «Monobank Open API» (api.monobank.ua). Карта, реквизиты которой видит клиент,
ДОЛЖНА принадлежать этому токену (иначе бот не увидит поступления). Без токена модуль не активен (is_configured() == False)
и бот работает по схеме «чек + подтверждение админом».
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from datetime import datetime as _dt

import aiohttp

from config import MONOBANK_API_TOKEN, MONOBANK_ACCOUNT_ID, MONOBANK_PAYMENT_TTL_MIN
from database import db

MONO_BASE_URL = "https://api.monobank.ua"
_MIN_INTERVAL_S = 61.0          # Monobank: не чаще 1 запроса выписки в 60 с на токен

_lock = asyncio.Lock()
_stmt: dict = {"txs": [], "fetched_at": 0.0, "last_call": 0.0, "ok": False}


def is_configured() -> bool:
    return bool(MONOBANK_API_TOKEN)


# ── создание платежа ───────────────────────────────────────
def _unique_total(amount_uah: float) -> float:
    """Целая сумма + уникальные копейки 01–99 (не совпадающие с другими открытыми платежами Monobank)."""
    base_kop = int(round(amount_uah)) * 100
    used = {round(float(p.get("amount") or 0) * 100) for p in db.list_open_pending() if p.get("method") == "monobank"}
    free = [base_kop + k for k in range(1, 100) if base_kop + k not in used]
    kop = random.choice(free) if free else base_kop + random.randint(1, 99)
    return kop / 100


def create_payment(amount_uah: float, uid: int) -> tuple[str, float]:
    """Создаёт заявку (pending_payments, method='monobank'). Возвращает (payment_id, сумма к оплате с копейками)."""
    total = _unique_total(amount_uah)
    pid = f"mono_{uuid.uuid4().hex[:12]}"
    db.add_pending(pid, uid, total, "monobank", amount_uah=total)
    return pid, total


# ── выписка (общая очередь + кэш) ──────────────────────────
async def _fetch(from_ts: int) -> list[dict] | None:
    url = f"{MONO_BASE_URL}/personal/statement/{MONOBANK_ACCOUNT_ID}/{from_ts}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers={"X-Token": MONOBANK_API_TOKEN},
                             timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    print(f"[monopay] statement HTTP {resp.status}: {(await resp.text())[:300]}")
                    return None
                data = await resp.json()
                return data if isinstance(data, list) else None
    except Exception as e:
        print(f"[monopay] statement exception: {e!r}")
        return None


async def refresh(not_before: float) -> bool:
    """
    Гарантирует, что в кэше лежит выписка, ЗАПРОШЕННАЯ не раньше not_before (time.monotonic()).
    Если нужно — ждёт, пока Monobank разрешит следующий запрос (до ~61 с). True — выписка свежая.
    """
    async with _lock:
        if _stmt["ok"] and _stmt["fetched_at"] >= not_before:
            return True                               # кто-то уже сходил за выпиской после нашего нажатия
        wait = _MIN_INTERVAL_S - (time.monotonic() - _stmt["last_call"])
        if wait > 0:
            await asyncio.sleep(wait)
        started = time.monotonic()
        _stmt["last_call"] = started
        txs = await _fetch(int(time.time()) - (MONOBANK_PAYMENT_TTL_MIN + 15) * 60)
        if txs is None:
            _stmt["ok"] = False
            return False
        _stmt.update(txs=txs, fetched_at=started, ok=True)
        return True


# ── сопоставление ──────────────────────────────────────────
def _created_ts(p: dict) -> int:
    try:
        return int(_dt.strptime(p["created_at"], "%d.%m.%Y %H:%M:%S").timestamp())
    except (ValueError, TypeError, KeyError):
        return int(time.time()) - MONOBANK_PAYMENT_TTL_MIN * 60


def match_cached(p: dict) -> bool:
    """Ищет в кэше выписки входящий платёж на сумму заявки и ЗАНИМАЕТ его. True — найден и занят этой заявкой."""
    target_kop = round(float(p.get("amount") or 0) * 100)
    if target_kop <= 0:
        return False
    since = _created_ts(p) - 120
    for tx in _stmt["txs"]:
        if int(tx.get("amount", 0)) != target_kop:          # копейки; входящий — положительное число
            continue
        if int(tx.get("time", 0)) < since:                   # платёж был ДО создания заявки — не наш
            continue
        tx_id = str(tx.get("id") or "")
        if not tx_id or db.is_mono_tx_used(tx_id):
            continue
        if db.claim_mono_tx(tx_id, p["payment_id"], target_kop / 100):
            return True
    return False


def is_expired(p: dict) -> bool:
    return time.time() - _created_ts(p) > MONOBANK_PAYMENT_TTL_MIN * 60


async def verify(pid: str, not_before: float) -> str:
    """
    Проверка по кнопке клиента. Возвращает: 'paid' | 'pending' | 'expired' | 'error' | 'closed' | 'not_configured'.
    'paid' значит «транзакция найдена и занята»; баланс зачисляет вызывающий код (payments.credit_payment).
    """
    if not is_configured():
        return "not_configured"
    p = db.get_pending(pid)
    if not p or p.get("status") != "pending":
        return "closed"
    if is_expired(p):
        return "expired"
    if not await refresh(not_before):
        return "error"
    p = db.get_pending(pid)                                  # за время ожидания мог зачислить фоновый цикл
    if not p or p.get("status") != "pending":
        return "closed"
    return "paid" if match_cached(p) else "pending"


# ── фоновая проверка ───────────────────────────────────────
async def watch_mono(bot) -> None:
    """Раз в минуту (если есть открытые заявки) сама ищет оплаты и зачисляет; просроченные закрывает."""
    from payments import credit_payment, _safe_send
    while True:
        try:
            opened = [p for p in await asyncio.to_thread(db.list_open_pending) if p.get("method") == "monobank"]
            live = []
            for p in opened:
                if is_expired(p):
                    if db.claim_pending(p["payment_id"], "timeout"):
                        await _safe_send(bot, int(p["user_id"]), "⏰ Время оплаты истекло. Создайте новый счёт.")
                else:
                    live.append(p)
            if live and is_configured() and await refresh(time.monotonic()):
                for p in live:
                    fresh = db.get_pending(p["payment_id"])
                    if fresh and fresh.get("status") == "pending" and match_cached(fresh):
                        await credit_payment(bot, fresh, "Карта (Monobank)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[watch_mono] {e!r}")
        await asyncio.sleep(5)
