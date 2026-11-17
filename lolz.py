"""
lolz.py — клиент Lolz Market API (https://api.lzt.market).

Эндпоинты (сверены с официальной библиотекой LOLZTEAM):
  GET  /telegram                              — поиск аккаунтов Telegram
  GET  /{item_id}                             — карточка аккаунта
  POST /{item_id}/fast-buy                    — купить (с проверкой валидности)
  GET  /{item_id}/telegram-login-code         — код входа
  POST /{item_id}/telegram-reset-authorizations — сброс сессий
  GET  /me                                    — профиль/баланс токена

Формат ответов Lolz местами меняется, поэтому разбор везде «терпимый»
(несколько вариантов ключей).
"""
from __future__ import annotations
import asyncio
import logging
import re
import time
from typing import Any

import aiohttp

from config import LOLZ_TOKEN, LOLZ_BASE_URL, LOLZ_SPAM, LOLZ_ORIGINS

log = logging.getLogger("lolz")

# Допустимые значения origin[] (GET /telegram, документация Lolz Market).
# ВАЖНО: саморег называется "self_registration". Значение "self_registered" Lolz не знает —
# с ним выдача всегда пустая, и бот пишет «нет аккаунтов без спамблока» по любой стране.
VALID_ORIGINS = frozenset({
    "brute", "phishing", "stealer", "autoreg", "personal", "resale",
    "dummy", "self_registration", "retrieve_via_support",
})
# Старые/разговорные варианты → правильное значение (чтобы не ломаться на старом .env и bot_settings).
_ORIGIN_ALIASES = {
    "self_registered": "self_registration",
    "selfregistered": "self_registration",
    "self_registrated": "self_registration",
    "self_reg": "self_registration",
    "selfreg": "self_registration",
    "self": "self_registration",
    "samoreg": "self_registration",
    "auto": "autoreg",
    "auto_reg": "autoreg",
    "autoregistered": "autoreg",
    "auto_registered": "autoreg",
}


def normalize_origins(origins: list[str] | None) -> list[str]:
    """Приводит origin[] к значениям, которые знает Lolz. Неизвестные — отбрасывает с предупреждением."""
    out: list[str] = []
    for raw in origins or []:
        o = str(raw).strip().lower()
        if not o:
            continue
        o = _ORIGIN_ALIASES.get(o, o)
        if o not in VALID_ORIGINS:
            log.warning("Lolz origin %r неизвестен — пропускаю (допустимо: %s)", raw, ", ".join(sorted(VALID_ORIGINS)))
            continue
        if o not in out:
            out.append(o)
    return out


class LolzError(Exception):
    """Lolz однозначно ответил отказом (аккаунт продан, мало денег, и т.п.)."""


class LolzNetworkError(Exception):
    """Результат неизвестен (таймаут/обрыв) — запрос мог дойти до Lolz."""


class _Limiter:
    """Минимальный интервал между запросами (Lolz банит за частые запросы)."""
    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self):
        async with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.interval:
                await asyncio.sleep(self.interval - delta)
            self._last = time.monotonic()


_search_limiter = _Limiter(3.1)   # поиск: ~20 запросов/мин
_api_limiter    = _Limiter(0.7)   # остальное


async def _request(method: str, path: str, *, params=None, json=None,
                   search: bool = False, retries: int = 2) -> dict:
    url = f"{LOLZ_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {LOLZ_TOKEN}", "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=40)
    last_err: Exception | None = None

    for attempt in range(retries + 1):
        await (_search_limiter if search else _api_limiter).wait()
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.request(method, url, params=params, json=json,
                                     headers=headers) as r:
                    try:
                        data = await r.json(content_type=None)
                    except Exception:
                        data = {"raw": await r.text()}
                    if r.status == 429:
                        await asyncio.sleep(3 + attempt * 3)
                        last_err = LolzError("Lolz: слишком много запросов, попробуйте позже")
                        continue
                    if r.status >= 500:
                        # Для покупки 5xx = результат неизвестен
                        raise LolzNetworkError(f"Lolz {r.status}")
                    if r.status >= 400 or (isinstance(data, dict) and data.get("errors")):
                        raise LolzError(_error_text(data, r.status))
                    return data if isinstance(data, dict) else {"data": data}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_err = LolzNetworkError(str(e))
            # повторяем только безопасные (GET) запросы
            if method != "GET":
                break
            await asyncio.sleep(1.5)
    if last_err:
        raise last_err
    raise LolzNetworkError("Lolz недоступен")


def _error_text(data: Any, status: int) -> str:
    if isinstance(data, dict):
        errs = data.get("errors") or data.get("error") or data.get("message")
        if isinstance(errs, list):
            return "; ".join(str(e) for e in errs)
        if errs:
            return str(errs)
    return f"Lolz вернул ошибку {status}"


# ───────────────────────── helpers по полям ─────────────────────────
def item_id(it: dict) -> int:
    return int(it.get("item_id") or it.get("id") or 0)


def item_price(it: dict) -> float:
    """Цена в рублях (запрашиваем currency=rub)."""
    for k in ("price", "rub_price"):
        if it.get(k) is not None:
            try:
                return float(it[k])
            except (TypeError, ValueError):
                pass
    return 0.0


def item_country(it: dict) -> str:
    """ISO-код страны аккаунта. Если Lolz вернул не 2-буквенный код (название/другой формат) — '' (не проверяем)."""
    c = str(it.get("telegram_country") or it.get("country") or "").strip().upper()
    return c if len(c) == 2 and c.isalpha() else ""


def item_phone(it: dict) -> str:
    login = (it.get("loginData") or {}).get("login") if isinstance(it.get("loginData"), dict) else ""
    return str(it.get("telegram_phone") or login or it.get("phone") or "")


_SPAM_YES = {"yes", "true", "1", "y", "да", "spam", "spamblock", "has_spam", "limited"}


def item_has_spam(it: dict) -> bool:
    """
    True только если спамблок помечен ЯВНО. Неизвестное → False (спам=no уже отфильтровал Lolz на сервере).
    У Lolz telegram_spam_block — число: -1 = не проверялось / нет, 0 = нет, >0 = есть.
    Поэтому -1 НЕ спам (раньше bool(-1) == True отбрасывал вообще все аккаунты).
    """
    for k in ("telegram_spam", "telegram_spam_block", "spam", "spamblock"):
        if k in it:
            v = it[k]
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return v > 0
            if isinstance(v, str):
                t = v.strip().lower()
                try:
                    return float(t) > 0
                except ValueError:
                    return t in _SPAM_YES
            return False
    return False


def item_premium(it: dict) -> bool:
    return bool(it.get("telegram_premium"))


def item_origin_label(it: dict) -> str:
    """Короткая метка происхождения для кнопки: авторег / саморег / ''."""
    o = str(it.get("item_origin") or it.get("origin") or "").lower()
    if "auto" in o:
        return "авторег"
    if "self" in o or "samo" in o:
        return "саморег"
    return ""


_BAD_STATES = {"paid", "sold", "closed", "deleted", "awaiting", "moderation", "stuck",
               "pending", "reserved", "rejected", "cancelled", "canceled", "hidden"}


def item_available(it: dict) -> bool:
    """Аккаунт можно покупать. Режем только явно проданные/закрытые состояния (поиск и так отдаёт «живые»)."""
    st = str(it.get("item_state") or it.get("state") or "active").strip().lower()
    return st not in _BAD_STATES


# ───────────────────────── публичные методы ─────────────────────────
def item_origin_norm(it: dict) -> str:
    """Происхождение аккаунта в виде значения Lolz ('' если в ответе его нет)."""
    o = str(it.get("item_origin") or it.get("origin") or "").strip().lower()
    return _ORIGIN_ALIASES.get(o, o)


async def search_telegram_page(iso: str, page: int = 1,
                               origins: list[str] | None = None,
                               relax_origin: bool = False) -> tuple[list[dict], bool]:
    """
    Одна страница выдачи Lolz: аккаунты Telegram нужной страны, САМЫЕ ДЕШЁВЫЕ первыми,
    без спамблока, только с нужным происхождением (авторег / саморег).
    Возвращает (аккаунты после фильтрации, есть ли ещё страницы на Lolz).

    relax_origin=True — запасной режим: параметр origin[] НЕ отправляется на Lolz (спамблок и страна
    остаются серверными фильтрами), а происхождение проверяется у себя по полю item_origin.
    Включается из handlers/shop.py, только если строгий запрос вернул пусто.
    """
    origins = normalize_origins(LOLZ_ORIGINS if origins is None else origins)
    params: list[tuple[str, str]] = [
        ("currency", "rub"),
        ("order_by", "price_to_up"),
        ("country[]", iso.upper()),
        ("page", str(page)),
    ]
    if LOLZ_SPAM:
        params.append(("spam", LOLZ_SPAM))
    if not relax_origin:
        for o in origins:
            params.append(("origin[]", o))

    data = await _request("GET", "/telegram", params=params, search=True)
    raw = [it for it in (data.get("items") or []) if isinstance(it, dict)]

    out = []
    dropped = {"no_id": 0, "country": 0, "spam": 0, "state": 0, "origin": 0}
    for it in raw:
        if not item_id(it):
            dropped["no_id"] += 1
            continue
        c = item_country(it)
        if c and c != iso.upper():          # страна точно не та (проверяем только при корректном ISO-коде)
            dropped["country"] += 1
            continue
        if LOLZ_SPAM == "no" and item_has_spam(it):
            dropped["spam"] += 1
            continue
        if not item_available(it):
            dropped["state"] += 1
            continue
        if relax_origin and origins:        # origin[] не слали → проверяем сами (если поле есть)
            o = item_origin_norm(it)
            if o and o not in origins:
                dropped["origin"] += 1
                continue
        out.append(it)

    if raw and not out:                      # Lolz отдал аккаунты, а мы всё отбросили — покажем почему
        sample = {k: v for k, v in raw[0].items()
                  if k.startswith("telegram") or k in ("item_id", "price", "item_state", "state", "item_origin", "origin", "country")}
        log.warning("search iso=%s: все %d аккаунтов отброшены клиентским фильтром %s. Пример полей: %s",
                    iso.upper(), len(raw), dropped, str(sample)[:700])

    per_page = int(data.get("perPage") or 0)
    total = int(data.get("totalItems") or 0)
    log.info("search iso=%s page=%s origins=%s%s spam=%s → lolz_total=%s raw=%d kept=%d dropped=%s",
             iso.upper(), page, origins or "-", " (relaxed: origin[] не отправлен)" if relax_origin else "",
             LOLZ_SPAM or "-", total, len(raw), len(out), dropped)
    if total and per_page:
        has_more = page * per_page < total
    elif per_page:
        has_more = len(raw) >= per_page
    else:
        has_more = False                    # Lolz не сказал про пагинацию — считаем, что это всё
    return out, has_more
