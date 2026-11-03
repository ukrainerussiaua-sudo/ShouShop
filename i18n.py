"""
i18n.py — язык бота: русский / украинский / английский.

Идея: ВЕСЬ код бота пишет и понимает русский («канонический» язык). Для пользователя с языком «uk» два прозрачных слоя:
  • ИСХОДЯЩИЙ (TranslateMiddleware, на сессии бота): перед отправкой переводит text/caption и тексты кнопок
    русский → украинский по таблице фрагментов i18n_uk.py (самые длинные фрагменты первыми, одним проходом);
  • ВХОДЯЩИЙ (LangMiddleware, в main.py): если пользователь нажал украинскую кнопку, текст сообщения подменяется
    на русский канонический, поэтому ни один хендлер не знает про язык. Обычный набранный текст не трогается.
Язык хранится в users.lang ('ru' | 'uk') и кэшируется. Выбор — при первом /start (до проверки подписки на канал),
сменить можно в «Профиль → Язык / Мова».
Фрагмент без перевода в i18n_uk.py просто останется русским — ничего не ломается.
"""
from __future__ import annotations
import contextvars
import re
from functools import lru_cache

from i18n_uk import PAIRS, COUNTRY_NAMES
try:
    from i18n_uk_extra import EXTRA
except Exception:                       # pragma: no cover
    EXTRA = {}
try:
    from i18n_en import EN, EN_COUNTRY_NAMES
except Exception:                       # pragma: no cover
    EN, EN_COUNTRY_NAMES = {}, {}

try:
    from aiogram.client.session.middlewares.base import BaseRequestMiddleware
except Exception:                       # pragma: no cover
    BaseRequestMiddleware = object

LANGS = ("ru", "uk", "en")
TRANSLATED = ("uk", "en")             # языки, для которых включён перевод «русский → язык»
CUR_UID: contextvars.ContextVar[int] = contextvars.ContextVar("cur_uid", default=0)
_cache: dict[int, str] = {}
NO_TRANSLATE: contextvars.ContextVar[bool] = contextvars.ContextVar("no_translate", default=False)   # админ-рассылка: не переводить


# ── язык пользователя ──────────────────────────────────────
def lang_of(uid: int) -> str:
    """'ru' | 'uk' | '' (ещё не выбирал)."""
    if not uid:
        return ""
    v = _cache.get(uid)
    if v:
        return v
    from database import db
    try:
        v = db.get_lang(uid)
    except Exception as e:
        print(f"[i18n] get_lang: {e!r}")
        v = ""
    if v in LANGS:
        _cache[uid] = v
        return v
    return ""


def set_lang(uid: int, lang: str) -> None:
    """Запоминает язык в кэше и (если пользователь уже в БД) в users.lang. Новому пользователю язык запишет _register()."""
    if lang not in LANGS:
        return
    _cache[uid] = lang
    from database import db
    try:
        db.set_lang(uid, lang)
    except Exception as e:
        print(f"[i18n] set_lang: {e!r}")


# ── перевод ────────────────────────────────────────────────
_LET = "A-Za-zА-Яа-яЁёІіЇїЄєҐґ"
_MAPS: dict[str, dict[str, str]] = {
    "uk": {**PAIRS, **COUNTRY_NAMES, **EXTRA},
    "en": {**EN, **EN_COUNTRY_NAMES},
}


def _compile(mapping: dict[str, str]) -> re.Pattern | None:
    parts = []
    for k in sorted(mapping, key=len, reverse=True):
        if not k:
            continue
        pre = rf"(?<![{_LET}])" if k[:1].isalpha() else ""
        post = rf"(?![{_LET}])" if k[-1:].isalpha() else ""
        parts.append(pre + re.escape(k) + post)
    return re.compile("|".join(parts)) if parts else None


_RES: dict[str, re.Pattern | None] = {lg: _compile(m) for lg, m in _MAPS.items()}


@lru_cache(maxsize=8192)
def _tr_cached(text: str, lang: str) -> str:
    rx, mp = _RES.get(lang), _MAPS.get(lang)
    if rx is None or not mp:
        return text
    return rx.sub(lambda m: mp[m.group(0)], text)


def tr(text: str, lang: str | None = None) -> str:
    """Русский (канонический) текст → язык пользователя (uk/en). lang не указан — язык текущего пользователя (CUR_UID).
    Для 'ru' и неизвестных языков возвращает текст как есть. Не найденные фрагменты остаются русскими. Результат кэшируется."""
    if not text:
        return text
    if lang is None:
        lang = lang_of(CUR_UID.get()) or "uk"      # обратная совместимость: раньше tr() был только русский → украинский
    if lang not in TRANSLATED:
        return text
    return _tr_cached(text, lang)


# ── обратное преобразование нажатой кнопки: переведённая → русская (для хендлеров) ──
_rev_exact: dict[str, dict[str, str]] | None = None
_REV_COUNTRY = {lg: {tl: ru for ru, tl in m.items() if tl != ru}
                for lg, m in (("uk", COUNTRY_NAMES), ("en", EN_COUNTRY_NAMES))}
_REV_RULES: dict[str, list[tuple[re.Pattern, str]]] = {
    "uk": [
        (re.compile(r"^(\d+) зірок · (.*)$", re.S), r"\1 звёзд · \2"),
        (re.compile(r"^(\d+) міс · (.*)$", re.S), r"\1 мес · \2"),
        (re.compile(r"^Підтвердити · (.*)$", re.S), r"Подтвердить · \1"),
        (re.compile(r"^Сплатити TON · (.*)$", re.S), r"Оплатить TON · \1"),
        (re.compile(r"^Сплатити · (.*)$", re.S), r"Оплатить · \1"),
    ],
    "en": [
        (re.compile(r"^(\d+) stars · (.*)$", re.S), r"\1 звёзд · \2"),
        (re.compile(r"^(\d+) mo · (.*)$", re.S), r"\1 мес · \2"),
        (re.compile(r"^Confirm · (.*)$", re.S), r"Подтвердить · \1"),
        (re.compile(r"^Pay TON · (.*)$", re.S), r"Оплатить TON · \1"),
        (re.compile(r"^Pay · (.*)$", re.S), r"Оплатить · \1"),
    ],
}
_COUNTRY_BTN = re.compile(r"^(\S+) (.+?) · (.+)$", re.S)


def _build_rev() -> dict[str, dict[str, str]]:
    import keyboards as K
    out: dict[str, dict[str, str]] = {}
    for lg in TRANSLATED:
        rev: dict[str, str] = {}
        for name, val in vars(K).items():
            if isinstance(val, str) and (name.startswith("B_") or name == "TO_ADMIN"):
                t = tr(val, lg)
                if t != val:
                    if t in rev and rev[t] != val:
                        print(f"[i18n] коллизия кнопок ({lg}): {t!r} ← {rev[t]!r} / {val!r}")
                    rev[t] = val
        out[lg] = rev
    return out


def button_to_ru(text: str, lang: str = "uk") -> str:
    """Если text — переведённая кнопка (статическая или с ценой/страной), вернёт её русский вариант; иначе text без изменений."""
    global _rev_exact
    if lang not in TRANSLATED:
        return text
    if _rev_exact is None:
        _rev_exact = _build_rev()
    rev = _rev_exact.get(lang, {})
    if text in rev:
        return rev[text]
    for rx, rep in _REV_RULES.get(lang, []):
        if rx.match(text):
            return rx.sub(rep, text)
    m = _COUNTRY_BTN.match(text)
    rc = _REV_COUNTRY.get(lang, {})
    if m and m.group(2) in rc:
        return f"{m.group(1)} {rc[m.group(2)]} · {m.group(3)}"
    return text


# ── разметка (кнопки) ──────────────────────────────────────
def tr_markup(rm, lang: str | None = None):
    """ReplyKeyboardMarkup / InlineKeyboardMarkup → копия с переведёнными подписями кнопок."""
    if rm is None:
        return rm
    if hasattr(rm, "inline_keyboard"):
        rows = [[b.model_copy(update={"text": tr(b.text, lang)}) for b in row] for row in rm.inline_keyboard]
        return rm.model_copy(update={"inline_keyboard": rows})
    if hasattr(rm, "keyboard"):
        rows = [[b.model_copy(update={"text": tr(b.text, lang)}) if hasattr(b, "model_copy") else b for b in row]
                for row in rm.keyboard]
        upd = {"keyboard": rows}
        if getattr(rm, "input_field_placeholder", None):
            upd["input_field_placeholder"] = tr(rm.input_field_placeholder, lang)
        return rm.model_copy(update=upd)
    return rm


class TranslateMiddleware(BaseRequestMiddleware):
    """Исходящий middleware сессии бота: переводит сообщения для пользователей с языком 'uk'."""

    async def __call__(self, make_request, bot, method):
        try:
            chat_id = getattr(method, "chat_id", None)
            uid = chat_id if isinstance(chat_id, int) and chat_id > 0 else CUR_UID.get()
            lg = lang_of(uid) if uid and not NO_TRANSLATE.get() else ""
            if lg in TRANSLATED:
                for field in ("text", "caption"):
                    v = getattr(method, field, None)
                    if isinstance(v, str) and v:
                        setattr(method, field, tr(v, lg))
                media = getattr(method, "media", None)           # edit_message_media: подпись внутри InputMedia
                if media is not None and not isinstance(media, (list, str)) and getattr(media, "caption", None):
                    method.media = media.model_copy(update={"caption": tr(media.caption, lg)})   # InputMedia неизменяем → копия
                rm = getattr(method, "reply_markup", None)
                if rm is not None:
                    setattr(method, "reply_markup", tr_markup(rm, lg))
        except Exception as e:           # noqa — перевод не должен ронять отправку
            print(f"[i18n translate] {e!r}")
        return await make_request(bot, method)
