"""
tondeposit.py — пополнение баланса переводом TON.

Клиент отправляет ЛЮБУЮ сумму TON на адрес приёма и пишет в комментарии свой Telegram ID.
Бот сам (раз в TON_DEPOSIT_POLL_S сек) и по кнопке «Проверить баланс» читает входящие транзакции адреса через toncenter,
находит комментарий-ID и зачисляет: баланс += TON × курс (курс задаёт админ).

Защита от повторов и «рефов»:
  • хэш каждой транзакции пишется в БД (ton_deposits, PK = tx_hash) ДО зачисления → одну транзакцию нельзя зачесть дважды;
  • транзакции старше момента настройки адреса (ton_deposit_since) игнорируются — старая история кошелька не зачтётся;
  • исходящие/внешние сообщения (без отправителя) пропускаются;
  • перевод без ID или с несуществующим ID не зачисляется, а попадает админам и в «TON без привязки».
"""
from __future__ import annotations
import asyncio
import base64
import re
import time

import aiohttp

import config as cfg
import ton_wallet
import tonrate
from config import CUR
from database import db
from billing import credit_topup
from utils import notify_admins, money

SET_ADDR = "ton_deposit_address"
SET_MIN = "ton_min_deposit"
SET_SINCE = "ton_deposit_since"

_ADDR_FRIENDLY = re.compile(r"^[A-Za-z0-9_\-]{48}$")
_ADDR_RAW = re.compile(r"^-?\d+:[0-9a-fA-F]{64}$")
_UID_RE = re.compile(r"^(?:id)?[\s:#\-]*(\d{4,15})$", re.I)


def valid_address(addr: str) -> bool:
    addr = (addr or "").strip()
    if not (_ADDR_FRIENDLY.match(addr) or _ADDR_RAW.match(addr)):
        return False
    try:                                    # проверка контрольной суммы, если tonsdk установлен
        from tonsdk.utils import Address
        Address(addr)
    except ImportError:
        pass
    except Exception:
        return False
    return True


def deposit_address() -> str | None:
    a = (db.get_setting(SET_ADDR, "") or "").strip()
    if a:
        return a
    if ton_wallet.is_configured():
        try:
            return ton_wallet.get_wallet().friendly
        except Exception:
            return None
    return None


def deposit_rate() -> float:
    """Курс зачисления (₴ за 1 TON) = база − наценка покупки (см. tonrate.py)."""
    return tonrate.buy_rate()


def is_enabled() -> bool:
    return bool(deposit_address()) and deposit_rate() > 0


def reset_since() -> None:
    """Вызывается при смене адреса: всё, что пришло раньше, не зачитываем."""
    db.set_setting(SET_SINCE, str(int(time.time())))


def _since() -> int:
    raw = db.get_setting(SET_SINCE, "")
    if raw.isdigit():
        return int(raw)
    reset_since()
    return int(time.time())


# ── toncenter ──────────────────────────────────────────────
async def _fetch_txs(address: str, limit: int = 60) -> list[dict]:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        for attempt in range(3):
            async with sess.get(f"{cfg.TONCENTER_URL}/getTransactions",
                                params={"address": address, "limit": str(limit)},
                                headers=ton_wallet._headers()) as r:
                if r.status == 429:
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                js = await r.json()
                if not js.get("ok"):
                    raise RuntimeError(f"toncenter: {js}")
                return js.get("result") or []
    raise RuntimeError("toncenter: rate limit")


def _comment(in_msg: dict) -> str:
    md = in_msg.get("msg_data") or {}
    if md.get("@type") == "msg.dataText":
        try:
            return base64.b64decode(md.get("text", "")).decode("utf-8", "ignore").strip()
        except Exception:
            pass
    msg = in_msg.get("message")
    return msg.strip() if isinstance(msg, str) else ""


def parse_incoming(tx: dict) -> dict | None:
    """Входящий внутренний перевод (есть отправитель) или None."""
    im = tx.get("in_msg") or {}
    src = im.get("source") or ""
    if not src:
        return None
    try:
        value = int(im.get("value") or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    tid = tx.get("transaction_id") or {}
    return {"hash": f"{tid.get('lt')}:{tid.get('hash')}", "utime": int(tx.get("utime") or 0),
            "value": value, "src": src, "comment": _comment(im)}


def parse_uid(comment: str) -> int | None:
    m = _UID_RE.match((comment or "").strip())
    return int(m.group(1)) if m else None


# ── сканирование ───────────────────────────────────────────
_scan_lock = asyncio.Lock()
_last_scan = 0.0


async def scan_once(bot, min_interval: float = 0.0) -> list[dict] | None:
    """Один проход: находит новые переводы и зачисляет. Возвращает список зачисленных {uid, ton, uah};
    None — проход пропущен (слишком часто, лимит toncenter)."""
    global _last_scan
    async with _scan_lock:
        if min_interval and time.monotonic() - _last_scan < min_interval:
            return None
        addr = deposit_address()
        if not addr:
            return []
        txs = await _fetch_txs(addr)
        _last_scan = time.monotonic()

        since = _since()
        incoming = [p for p in (parse_incoming(t) for t in txs) if p and p["utime"] >= since]
        if not incoming:
            return []
        seen = db.seen_ton_txs([p["hash"] for p in incoming])
        rate = deposit_rate()
        min_dep = db.get_float(SET_MIN, 0.0)
        credited: list[dict] = []

        for p in sorted(incoming, key=lambda x: x["utime"]):          # старые первыми
            h = p["hash"]
            if h in seen:
                continue
            ton = p["value"] / 1e9
            uid = parse_uid(p["comment"])
            short = h.split(":")[-1][:10]

            if min_dep and ton < min_dep:
                db.claim_ton_tx(h, uid or 0, ton, 0, 0, "ignored", p["src"], p["comment"])
                continue

            if not uid or not db.find_user_by_id(uid):
                if db.claim_ton_tx(h, 0, ton, 0, 0, "unmatched", p["src"], p["comment"]):
                    await notify_admins(
                        bot,
                        f"⚠️ <b>TON без привязки к клиенту</b>\n\n💠 {ton:g} TON\n"
                        f"🧾 Комментарий: <code>{_esc(p['comment']) or '—'}</code>\n"
                        f"📤 От: <code>{p['src']}</code>\n🔗 tx: <code>{short}…</code>\n\n"
                        f"Не зачислено автоматически. Если это вы сами пополняли кошелёк — просто игнорируйте. "
                        f"Если клиент забыл ID — найдите его и начислите вручную («Начислить баланс»).")
                continue

            if rate <= 0:
                continue                         # курс не задан — не трогаем, зачислим, когда админ его поставит
            uah = round(ton * rate, 2)
            if uah <= 0:
                continue
            if not db.claim_ton_tx(h, uid, ton, uah, rate, "claimed", p["src"], p["comment"]):
                continue                         # уже занята другим проходом — это и есть защита от повтора
            try:
                await credit_topup(bot, uid, uah, "TON", f"{ton:g} TON × {rate:g} {CUR} · tx <code>{short}…</code>")
                db.set_ton_tx_status(h, "credited")
                credited.append({"uid": uid, "ton": ton, "uah": uah})
            except Exception as e:
                db.set_ton_tx_status(h, "failed")
                await notify_admins(bot, f"🚨 <b>Не удалось зачислить TON-пополнение</b>\nклиент <code>{uid}</code>, "
                                         f"{ton:g} TON = {money(uah)}\ntx <code>{h}</code>\n{_esc(repr(e))}\n"
                                         f"Сначала проверьте баланс клиента, потом при необходимости начислите вручную.")
        return credited


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def watch_ton_deposits(bot) -> None:
    """Фоновый цикл (запускается в main.py)."""
    while True:
        try:
            if is_enabled():
                await scan_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[ton deposits] {e!r}")
        await asyncio.sleep(cfg.TON_DEPOSIT_POLL_S)
