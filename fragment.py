"""
fragment.py — покупка Telegram Stars и подарок Premium через Fragment.com
(порт логики n0/fragment-tg на Python: aiohttp + TON-кошелёк на tonsdk).

Поток как в fragment-tg:
  1) searchStarsRecipient / searchPremiumGiftRecipient  — найти получателя
  2) initBuyStarsRequest / initGiftPremiumRequest        — создать заявку
  3) getBuyStarsLink / getGiftPremiumLink                — Fragment отдаёт данные TON-транзакции
  4) подпись и отправка транзакции со своего кошелька (ton_wallet.py)
  5) confirm_method                                      — сообщить Fragment о платеже

Исключения:
  RecipientError   — получатель не найден / не подходит (текст можно показать клиенту)
  FragmentError    — ДО отправки TON что-то пошло не так → деньги клиенту можно вернуть
  FragmentUncertain— TON мог уйти, но результат неизвестен → деньги НЕ возвращать, проверить вручную
"""
from __future__ import annotations
import json
import logging
import re
import time

import aiohttp

import config as cfg
import ton_wallet

log = logging.getLogger("fragment")

BASE = "https://fragment.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

STARS_MIN, STARS_MAX = 50, 1_000_000
PREMIUM_MONTHS = (3, 6, 12)


class FragmentError(Exception):
    """Определённый отказ до отправки TON — безопасно вернуть деньги клиенту."""


class RecipientError(FragmentError):
    """Получатель не найден / не может получить товар."""


class FragmentUncertain(Exception):
    """Транзакция могла уйти в сеть, итог неизвестен — деньги не возвращаем автоматически."""


def is_configured() -> bool:
    return cfg.FRAGMENT_CONFIGURED


def normalize_username(text: str) -> str | None:
    u = (text or "").strip().lstrip("@")
    if u.startswith("https://t.me/"):
        u = u[len("https://t.me/"):]
    return u if USERNAME_RE.match(u) else None


def _cookie_header() -> str:
    pairs = {
        "stel_dt": cfg.FRAGMENT_STEL_DT,
        "stel_ssid": cfg.FRAGMENT_STEL_SSID,
        "stel_token": cfg.FRAGMENT_STEL_TOKEN,
        "stel_ton_token": cfg.FRAGMENT_STEL_TON_TOKEN,
    }
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def _session() -> aiohttp.ClientSession:
    """Cookies шлём ЯВНЫМ заголовком на каждый запрос (jar aiohttp мог их не отправлять)."""
    return aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar(),
        headers={"User-Agent": cfg.FRAGMENT_USER_AGENT or UA, "Cookie": _cookie_header(),
                 "Accept-Language": "en-US,en;q=0.9"},
        timeout=aiohttp.ClientTimeout(total=30))


# page-hash кэш: страница → (api_path, время)
_api_cache: dict[str, tuple[str, float]] = {}
_API_TTL = 600


async def _api_path(sess: aiohttp.ClientSession, page: str) -> str:
    cached = _api_cache.get(page)
    if cached and time.time() - cached[1] < _API_TTL:
        return cached[0]
    async with sess.get(f"{BASE}{page}") as r:
        html = await r.text()
    m = re.search(r'"apiUrl"\s*:\s*"([^"]+)"', html)
    if not m:
        raise FragmentError("Fragment: не нашёл apiUrl на странице (сессия устарела или сайт изменился)")
    path = m.group(1).replace("\\/", "/")
    _api_cache[page] = (path, time.time())
    return path


async def _call(sess: aiohttp.ClientSession, page: str, method: str, **params) -> dict:
    path = await _api_path(sess, page)
    data = {"method": method, **{k: str(v) for k, v in params.items() if v is not None}}
    headers = {"X-Requested-With": "XMLHttpRequest", "Origin": BASE, "Referer": f"{BASE}{page}"}
    try:
        async with sess.post(f"{BASE}{path}", data=data, headers=headers) as r:
            txt = await r.text()
            if r.status >= 400:
                raise FragmentError(f"Fragment {method}: HTTP {r.status}")
    except aiohttp.ClientError as e:
        raise FragmentError(f"Fragment {method}: сеть: {e!r}")
    try:
        js = json.loads(txt)
    except ValueError:
        _api_cache.pop(page, None)
        raise FragmentError(f"Fragment {method}: ответ не JSON: {txt[:200]!r}")
    return js


def _tonconnect() -> tuple[str, str]:
    w = ton_wallet.get_wallet()
    account = json.dumps({
        "address": w.raw_address, "chain": "-239",
        "walletStateInit": w.state_init_b64, "publicKey": w.public_key_hex,
    }, separators=(",", ":"))
    device = json.dumps({
        "platform": "linux", "appName": "Tonkeeper", "appVersion": "4.0.0",
        "maxProtocolVersion": 2,
        "features": [{"name": "SendTransaction", "maxMessages": 4}],
    }, separators=(",", ":"))
    return account, device


# kind → (страница, метод поиска, метод заявки, метод ссылки, имя параметра количества)
_KINDS = {
    "stars": ("/stars/buy", "searchStarsRecipient", "initBuyStarsRequest",
              "getBuyStarsLink", "quantity"),
    "premium": ("/premium/gift", "searchPremiumGiftRecipient", "initGiftPremiumRequest",
                "getGiftPremiumLink", "months"),
}


async def _search(sess, kind: str, username: str, qty: int) -> dict:
    page, m_search, _, _, qname = _KINDS[kind]
    js = await _call(sess, page, m_search, query=username, **{qname: qty})
    found = js.get("found")
    if not found or not found.get("recipient"):
        err = js.get("error") or "Пользователь не найден"
        raise RecipientError(str(err))
    return found


async def check_recipient(kind: str, username: str, qty: int) -> dict:
    """Проверка получателя перед оплатой. Возвращает {'recipient', 'name', ...}."""
    if not is_configured():
        raise FragmentError("Fragment не настроен")
    async with _session() as sess:
        return await _search(sess, kind, username, qty)


async def _prepare(sess, kind: str, username: str, qty: int, show_sender: bool) -> dict:
    page, _, m_init, m_link, qname = _KINDS[kind]
    found = await _search(sess, kind, username, qty)
    # payment_method=ton обязателен: без него Fragment отвечает "Access denied"
    init = await _call(sess, page, m_init, recipient=found["recipient"],
                       **{qname: qty}, payment_method="ton")
    req_id = init.get("req_id")
    if not req_id:
        raise FragmentError(f"Fragment {m_init}: нет req_id: {str(init)[:200]}")
    account, device = _tonconnect()
    link = await _call(sess, page, m_link, id=req_id, show_sender=int(show_sender),
                       transaction=1, account=account, device=device)
    tx = link.get("transaction")
    if not tx or not tx.get("messages"):
        # запасной вариант: так же, как браузер (без account/device)
        link2 = await _call(sess, page, m_link, id=req_id, show_sender=int(show_sender),
                            transaction=1)
        if (link2.get("transaction") or {}).get("messages"):
            link, tx = link2, link2["transaction"]
    if not tx or not tx.get("messages"):
        err = link.get("error") or str(link)[:200]
        # самая частая причина — протухли cookies / кошелёк не привязан к сессии Fragment
        raise FragmentError(f"Fragment {m_link}: нет транзакции: {err}")
    return {"req_id": req_id, "tx": tx, "link": link, "page": page}


async def _confirm(sess, prep: dict, boc: str):
    method = prep["link"].get("confirm_method") or prep["tx"].get("confirm_method")
    if not method:
        return
    params = prep["link"].get("confirm_params") or prep["tx"].get("confirm_params") or {}
    try:
        await _call(sess, prep["page"], method, **params, boc=boc)
    except Exception as e:           # деньги уже отправлены — подтверждение не критично
        log.warning("fragment confirm %s: %r", method, e)


async def _buy(kind: str, username: str, qty: int, show_sender: bool | None = None) -> dict:
    # Fragment: show_sender=1 показывает аккаунт, авторизованный в Fragment;
    # show_sender=0 делает покупку анонимной. Всегда используем show_sender=0.
    show_sender = False   # ВСЕГДА анонимно (Stars и Premium), параметр вызова игнорируется
    if not is_configured():
        raise FragmentError("Fragment не настроен")
    async with _session() as sess:
        prep = await _prepare(sess, kind, username, qty, show_sender)
        messages = prep["tx"]["messages"]
        valid_until = prep["tx"].get("validUntil")
        if valid_until and int(valid_until) < time.time() + 20:
            raise FragmentError("Fragment: транзакция уже просрочена")
        boc = await ton_wallet.send_messages(messages)       # FragmentError / FragmentUncertain
        await _confirm(sess, prep, boc)
        return {"req_id": prep["req_id"], "boc": boc}


async def buy_stars(username: str, quantity: int, show_sender: bool | None = None) -> dict:
    if not STARS_MIN <= quantity <= STARS_MAX:
        raise FragmentError(f"Количество звёзд: от {STARS_MIN} до {STARS_MAX}")
    return await _buy("stars", username, quantity, show_sender)


async def gift_premium(username: str, months: int, show_sender: bool | None = None) -> dict:
    if months not in PREMIUM_MONTHS:
        raise FragmentError("Premium: 3, 6 или 12 месяцев")
    return await _buy("premium", username, months, show_sender)


async def session_valid() -> tuple[bool, str]:
    """Безопасная диагностика доступности страницы/API Fragment.

    Не ищет вымышленного тестового получателя: отказ поиска `durov` не доказывает,
    что cookies невалидны. Реальные cookies/username проверяются при поиске заказа.
    """
    if not is_configured():
        return False, "не настроен (.env)"
    try:
        async with _session() as sess:
            path = await _api_path(sess, "/stars/buy")
        return True, "страница Stars и API доступны; cookies и реальный username проверяются при поиске получателя"
    except Exception as e:
        return False, repr(e)[:150]
