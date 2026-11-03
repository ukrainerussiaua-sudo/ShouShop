"""
ton_wallet.py — свой TON-кошелёк бота (v4R2 из seed-фразы) + отправка транзакций через toncenter.
Нужен для оплаты заказов на Fragment. Фраза хранится ТОЛЬКО в .env (TON_MNEMONIC).
Рекомендация: отдельный «горячий» кошелёк, на котором лежит немного TON.
"""
from __future__ import annotations
import asyncio
import base64
import logging
import time
from dataclasses import dataclass

import aiohttp

import config as cfg

log = logging.getLogger("ton_wallet")

_send_lock = asyncio.Lock()      # одна транзакция за раз (иначе конфликт seqno)


def is_configured() -> bool:
    """Задана ли seed-фраза горячего кошелька (нужна для покупки TON клиентами и для Fragment)."""
    return bool(cfg.TON_MNEMONIC)


def _errors():
    import fragment                      # поздний импорт: избегаем циклов
    return fragment.FragmentError, fragment.FragmentUncertain


@dataclass
class Wallet:
    obj: object
    raw_address: str
    friendly: str
    public_key_hex: str
    state_init_b64: str


_wallet: Wallet | None = None


def get_wallet() -> Wallet:
    global _wallet
    if _wallet is not None:
        return _wallet
    FragmentError, _ = _errors()
    try:
        from tonsdk.contract.wallet import Wallets, WalletVersionEnum
        from tonsdk.utils import bytes_to_b64str
    except ImportError:
        raise FragmentError("Не установлен tonsdk (pip install tonsdk)")
    words = cfg.TON_MNEMONIC.split()
    if len(words) != 24:
        raise FragmentError("TON_MNEMONIC должен содержать 24 слова")
    ver = {"v3r2": WalletVersionEnum.v3r2, "v4r2": WalletVersionEnum.v4r2}.get(cfg.TON_WALLET_VERSION)
    if ver is None:
        raise FragmentError("TON_WALLET_VERSION: v3r2 или v4r2 (v5/W5 tonsdk не поддерживает)")
    _, pub, _priv, w = Wallets.from_mnemonics(words, ver, 0)
    state_init = w.create_state_init()["state_init"]
    _wallet = Wallet(
        obj=w,
        raw_address=w.address.to_string(False, False, False),
        friendly=w.address.to_string(True, True, False),
        public_key_hex=pub.hex() if isinstance(pub, (bytes, bytearray)) else str(pub),
        state_init_b64=bytes_to_b64str(state_init.to_boc(False)),
    )
    return _wallet


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if cfg.TONCENTER_API_KEY:
        h["X-API-Key"] = cfg.TONCENTER_API_KEY
    return h


async def _get_info(sess: aiohttp.ClientSession, address: str) -> dict:
    for attempt in range(4):
        async with sess.get(f"{cfg.TONCENTER_URL}/getWalletInformation",
                            params={"address": address}, headers=_headers()) as r:
            if r.status == 429:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            js = await r.json()
            if not js.get("ok"):
                raise RuntimeError(f"toncenter: {js}")
            return js["result"]
    raise RuntimeError("toncenter: rate limit")


def _account_state(info: dict) -> str:
    """Нормализует статусы TON Center / lite-server к единому набору."""
    value = str(info.get("account_state") or info.get("state") or "").strip().lower()
    aliases = {
        "uninit": "uninitialized",
        "uninitialized": "uninitialized",
        "non-existent": "nonexist",
        "nonexistent": "nonexist",
        "nonexist": "nonexist",
        "active": "active",
        "frozen": "frozen",
    }
    return aliases.get(value, value)


async def _broadcast_boc(sess: aiohttp.ClientSession, boc: str) -> None:
    """Broadcast one signed BOC. Any ambiguous response is treated as uncertain."""
    FragmentError, FragmentUncertain = _errors()
    try:
        async with sess.post(f"{cfg.TONCENTER_URL}/sendBoc", json={"boc": boc},
                             headers=_headers()) as r:
            js = await r.json()
            status = r.status
    except Exception as e:
        raise FragmentUncertain(f"TON Center не ответил на broadcast ({e!r}); сообщение могло уйти")
    if not js.get("ok"):
        if status in (400, 422):
            raise FragmentError(f"TON Center отклонил сообщение: {js}")
        raise FragmentUncertain(f"TON Center вернул неоднозначный ответ: {js}")


async def _wait_for_wallet_activation(sess: aiohttp.ClientSession, address: str,
                                      old_seqno: int = 0, timeout_s: int = 90) -> dict:
    """Ждёт подтверждённой активации кошелька после deploy-сообщения."""
    FragmentUncertain = _errors()[1]
    deadline = time.time() + timeout_s
    last_state = "unknown"
    last_seqno = old_seqno
    while time.time() < deadline:
        await asyncio.sleep(4)
        try:
            info = await _get_info(sess, address)
            last_state = _account_state(info) or "unknown"
            last_seqno = int(info.get("seqno") or 0)
            if last_state == "active" and last_seqno > old_seqno:
                return info
            if last_state == "frozen":
                raise FragmentUncertain("TON-кошелёк стал frozen во время активации; нужна ручная проверка")
        except FragmentUncertain:
            raise
        except Exception:
            # RPC может кратковременно не видеть только что отправленную транзакцию.
            continue
    raise FragmentUncertain(
        f"Сообщение активации отправлено, но подтверждение не получено за {timeout_s} сек. "
        f"Последний статус: {last_state}, seqno={last_seqno}. Не повторяйте покупку "
        "до проверки транзакции в обозревателе TON.")


async def _initialize_wallet_if_needed(sess: aiohttp.ClientSession, wallet: Wallet,
                                       info: dict) -> dict:
    """Разворачивает заранее пополненный v3r2/v4r2 кошелёк одним init-сообщением.

    Вызывается только для account_state=uninitialized/nonexist. Для active-кошелька
    seqno=0 является допустимым значением и не считается ошибкой активации.
    """
    FragmentError, FragmentUncertain = _errors()
    state = _account_state(info)
    if state == "active":
        return info
    if state not in ("uninitialized", "nonexist"):
        raise FragmentError(f"Нельзя автоматически активировать кошелёк из состояния {state!r}.")

    balance = int(info.get("balance") or 0)
    if balance <= 0:
        raise FragmentError(
            "TON-кошелёк ещё не развёрнут и не имеет баланса для оплаты комиссии активации. "
            "Сначала пополните именно адрес, полученный из TON_MNEMONIC.")

    try:
        # Официальный путь tonsdk для первичного развёртывания: StateInit + signed external msg.
        query = wallet.obj.create_init_external_message()
        message = query.get("message") if isinstance(query, dict) else None
        if message is None:
            raise RuntimeError("tonsdk не вернул init message")
        from tonsdk.utils import bytes_to_b64str
        boc = bytes_to_b64str(message.to_boc(False))
    except Exception as e:
        raise FragmentError(f"Не удалось подготовить автоматическую активацию TON-кошелька: {e!r}")

    log.info("Deploying pre-funded TON wallet %s (state=%s, balance=%.3f TON)",
             wallet.friendly, state, balance / 1e9)
    await _broadcast_boc(sess, boc)
    return await _wait_for_wallet_activation(sess, wallet.friendly, old_seqno=0, timeout_s=90)


async def get_wallet_status() -> dict:
    """Возвращает публичный статус кошелька без раскрытия ключей/seed-фразы."""
    w = get_wallet()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as sess:
        info = await _get_info(sess, w.friendly)
    balance_nano = int(info.get("balance") or 0)
    return {
        "address": w.friendly,
        "balance_nano": balance_nano,
        "balance_ton": balance_nano / 1e9,
        "seqno": int(info.get("seqno") or 0),
        "account_state": _account_state(info) or "unknown",
        "is_wallet": info.get("wallet"),
    }


async def get_balance_ton() -> float:
    status = await get_wallet_status()
    return float(status["balance_ton"])


def _payload_cell(payload: str | None):
    if not payload:
        return None
    from tonsdk.boc import Cell
    s = payload.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return Cell.one_from_boc(base64.b64decode(s))


async def send_messages(messages: list[dict]) -> str:
    """
    Подписывает и отправляет транзакцию из ответа Fragment. Возвращает base64-BOC.
    До broadcast любая ошибка = FragmentError (деньги вернуть можно).
    После broadcast неизвестный итог = FragmentUncertain.
    """
    FragmentError, FragmentUncertain = _errors()
    if len(messages) != 1:
        raise FragmentError(f"Fragment вернул {len(messages)} сообщений, поддерживается 1")
    msg = messages[0]
    amount = int(msg["amount"])
    w = get_wallet()

    async with _send_lock:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            try:
                info = await _get_info(sess, w.friendly)
            except Exception as e:
                raise FragmentError(f"toncenter недоступен: {e!r}")
            balance = int(info.get("balance") or 0)
            seqno = int(info.get("seqno") or 0)
            state = _account_state(info)
            is_wallet = info.get("wallet")

            if state == "frozen":
                raise FragmentError(
                    "TON-кошелёк заморожен (frozen). Автоматическая отправка остановлена, "
                    "чтобы не потерять TON; требуется отдельная проверка состояния кошелька.")
            if state not in ("", "unknown", "active", "uninitialized", "nonexist"):
                raise FragmentError(f"Неизвестное состояние TON-кошелька: {state!r}. Перевод остановлен.")
            # Для нового/неразвёрнутого аккаунта TON Center иногда отвечает wallet=false:
            # контракт ещё не активен, но адрес уже однозначно получен из локального StateInit.
            if is_wallet is False and state not in ("uninitialized", "nonexist"):
                raise FragmentError(
                    "TON Center не распознал активный адрес как wallet-контракт. Проверьте "
                    "TON_WALLET_VERSION и соответствие TON_MNEMONIC кошельку Fragment.")

            # Если контракт ещё не развёрнут, сначала выполняем отдельную безопасную активацию.
            # После подтверждения заново читаем баланс/seqno и только затем платим Fragment.
            if state in ("uninitialized", "nonexist"):
                deploy_reserve = 150_000_000  # 0.15 TON на deploy и сетевые комиссии
                if balance < amount + deploy_reserve:
                    raise FragmentError(
                        f"Для первой активации и оплаты нужно примерно {(amount + deploy_reserve) / 1e9:.3f} TON, "
                        f"на кошельке {balance / 1e9:.3f} TON.")
                info = await _initialize_wallet_if_needed(sess, w, info)
                balance = int(info.get("balance") or 0)
                seqno = int(info.get("seqno") or 0)
                state = _account_state(info)
                if state != "active":
                    raise FragmentUncertain(
                        f"TON-кошелёк после активации имеет статус {state!r}; покупка не отправлялась. "
                        "Проверьте состояние транзакции в обозревателе TON.")

            # Для активного кошелька seqno=0 допустим; это не признак неактивности.
            fee_reserve = 50_000_000  # 0.05 TON для комиссии обычной транзакции
            if balance < amount + fee_reserve:
                raise FragmentError(
                    f"На TON-кошельке бота мало средств: {balance / 1e9:.3f} TON, "
                    f"нужно ≈ {(amount + fee_reserve) / 1e9:.3f} TON с резервом на комиссию.")

            try:
                query = w.obj.create_transfer_message(
                    to_addr=msg["address"], amount=amount, seqno=seqno,
                    payload=_payload_cell(msg.get("payload")))
                from tonsdk.utils import bytes_to_b64str
                boc = bytes_to_b64str(query["message"].to_boc(False))
            except Exception as e:
                raise FragmentError(f"Не смог собрать транзакцию: {e!r}")

            # ── broadcast ──
            await _broadcast_boc(sess, boc)

            # ── ждём, пока seqno вырастет (транзакция принята сетью) ──
            deadline = time.time() + 90
            while time.time() < deadline:
                await asyncio.sleep(4)
                try:
                    info2 = await _get_info(sess, w.friendly)
                    next_seqno = int(info2.get("seqno") or 0)
                    next_state = _account_state(info2)
                    if next_state == "frozen":
                        raise FragmentUncertain(
                            "после отправки TON-кошелёк стал frozen; требуется ручная проверка")
                    # Для обычного и первого (deploy) платежа ждём увеличения seqno.
                    # Если состояние явно ещё uninitialized, транзакция не считается завершённой.
                    if next_seqno > seqno and next_state == "active":
                        return boc
                except FragmentUncertain:
                    raise
                except Exception:
                    pass
            if seqno == 0:
                raise FragmentUncertain(
                    "первое сообщение отправлено в сеть, но кошелёк не перешёл в active/seqno>0 "
                    "за 90 секунд. Не повторяйте покупку до проверки транзакции в обозревателе TON.")
            raise FragmentUncertain(
                "seqno не вырос за 90 секунд — транзакция могла уйти. Не повторяйте её до ручной проверки.")


async def send_ton(address: str, amount_ton: float) -> str:
    """Простой перевод TON с горячего кошелька бота (без комментария). Ошибки — как в send_messages:
    FragmentError = точно НЕ ушло (деньги клиенту вернуть можно), FragmentUncertain = могло уйти."""
    FragmentError, _ = _errors()
    nano = int(round(float(amount_ton) * 1e9))
    if nano <= 0:
        raise FragmentError("Нулевая сумма перевода")
    return await send_messages([{"address": address, "amount": nano}])
