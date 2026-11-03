"""
texts.py — все тексты экранов (Mango Shop, цены в гривнах).
Обычные эмодзи в тексте автоматически становятся премиум (emoji.PremiumEmojiMiddleware).
"""
from config import (
    PEPE, DIAMOND, PLANE, USDT, QUESTION, SPARKLE_BALL, SPARKLE_STARS,
    EMOJI_HEART, EMOJI_CHECK, EMOJI_GLOBE, EMOJI_INFO,
    SHOP_NAME, SHOP_CHANNEL, SHOP_CHAT, SUPPORT_ACCOUNT, REVIEWS_CHANNEL, REVIEWS_CHANNEL_URL,
    DOC_PRIVACY_URL, DOC_TERMS_URL, DOC_RULES_URL,
    BOT_USERNAME, GUARANTEE_HOURS, REF_BONUS, CUR,
)
import html
import countries as C


def _m(v: float) -> str:
    """Деньги: 12.5 → «12.5 ₴», 100 → «100 ₴»."""
    s = f"{float(v):.2f}".rstrip("0").rstrip(".")
    return f"{s} {CUR}"


def _links() -> str:
    return (f"{EMOJI_HEART} Канал — {SHOP_CHANNEL}\n"
            f"{EMOJI_HEART} Чат — {SHOP_CHAT}\n"
            f"{EMOJI_HEART} Поддержка — {SUPPORT_ACCOUNT}")


def txt_shop() -> str:
    """Главное меню SHOU SHOP."""
    return "🏠 Выберите раздел:"


def txt_subscribe(first_name: str) -> str:
    from config import REQUIRED_CHANNEL, REQUIRED_CHANNEL_URL
    ch = f'<a href="{REQUIRED_CHANNEL_URL}">{html.escape(REQUIRED_CHANNEL)}</a>' if REQUIRED_CHANNEL else ""
    return (f"🔒 <b>Подписка обязательна</b>\n\nПривет, {html.escape(first_name)}!\n\n"
            f"Для использования бота необходимо подписаться на канал {ch}.\n\n"
            f"Подпишитесь и нажмите «Проверить подписку».")


def txt_catalog() -> str:
    return (f"{DIAMOND} <b>КАТАЛОГ</b>\n\nВыберите категорию: аккаунты, звёзды, Premium, NFT подарки, Голда или покупка TON.")


def txt_countries() -> str:
    return (f"{DIAMOND} <b>TELEGRAM АККАУНТЫ</b>\n\n"
            f"Выберите страну — цена указана на кнопке.\n"
            f"Новорег · без спамблока · автовыдача 24/7")


def txt_no_products() -> str:
    return (f"{DIAMOND} <b>TELEGRAM АККАУНТЫ</b>\n\n❌ Сейчас нет доступных товаров. "
            f"Загляните позже.")


def txt_confirm(ci: dict, price: int, discount: float = 0) -> str:
    final = price - discount
    disc = f"\n🏷 Скидка промокода: -{_m(discount)} (к оплате {_m(final)})" if discount else ""
    return (
        f"{QUESTION} <b>Вы действительно хотите купить аккаунт "
        f"{ci['flag']} {ci['name']} новорег без спамблока за {_m(price)}?</b>\n\n"
        f"🌍 Страна: {ci['name']} [{ci['code']}]\n"
        f"🆕 Тип: новорег\n"
        f"🛡 Спамблок: нет\n"
        f"💰 Цена: <b>{_m(price)}</b>{disc}\n\n"
        f"✅ Гарантия {GUARANTEE_HOURS} ч · авто-выдача · мгновенный код\n"
        f"⚠️ Используйте VPN/Proxy соответствующей страны"
    )


TXT_NO_ACCOUNT = "❌ <b>Нет аккаунта.</b>\n\nСейчас нет подходящего аккаунта. Попробуйте позже."
TXT_NO_ACCOUNTS = "❌ <b>Аккаунтов нету.</b>\n\nПо этой стране сейчас ничего нет в наличии. Попробуйте позже."


def txt_support() -> str:
    return (
        f"{DIAMOND} <b>ПОДДЕРЖКА</b>\n\n"
        f"Возник вопрос или проблема с заказом? Напишите нам:\n\n"
        f"{_links()}"
    )


def txt_nofunds(bal: float, need: float) -> str:
    return (f"❌ <b>Недостаточно средств</b>\n\n🪙 Баланс: {_m(bal)}\n"
            f"{USDT} К оплате: {_m(need)}\n❌ Не хватает: {_m(need - bal)}")


def txt_topup() -> str:
    return (f"{USDT} <b>ПОПОЛНИТЬ БАЛАНС</b>\n\nВыберите способ пополнения (баланс — в гривнах):\n\n"
            f"💳 <b>Перевод на карту</b> — переводите ₴ на карту, зачисление после проверки платежа\n"
            f"🪙 <b>Crypto Bot</b> — оплата криптой, зачисление автоматически\n"
            f"💠 <b>TON</b> — отправьте любую сумму TON с вашим ID в комментарии")


def txt_topup_amount(method: str, min_amount: float) -> str:
    return (f"{USDT} <b>{method.upper()}</b>\n\nВыберите сумму кнопкой или введите свою "
            f"(минимум {_m(min_amount)}):")


def txt_purchase_ready(ci: dict, phone: str, guar: str) -> str:
    return (
        f"{SPARKLE_STARS} <b>ПОКУПКА УСПЕШНА!</b>\n\n"
        f"{ci['flag']} <b>{ci['name']} [{ci['code']}]</b>\n\n"
        f"📱 <b>Ваш номер:</b> <code>{html.escape(str(phone))}</code>\n"
        f"⏰ Гарантия до: {guar}\n\n"
        f"1) Откройте Telegram и введите номер\n"
        f"2) Нажмите «Получить код» в меню ⬇️"
    )


def txt_purchase_code(ci: dict, phone: str, code: str, guar: str) -> str:
    return (
        f"{SPARKLE_STARS} <b>КОД ПОЛУЧЕН</b>\n\n"
        f"{ci['flag']} <b>{ci['name']} [{ci['code']}]</b>\n\n"
        f"📱 <b>Номер:</b> <code>{html.escape(str(phone))}</code>\n"
        f"🔑 <b>Код:</b> <code>{html.escape(str(code))}</code>\n\n"
        f"⏰ Гарантия до: {guar}"
    )


def txt_acc_detail(p: dict) -> str:
    ci = C.get(p["country_code"]) or {"flag": "🌍", "name": "?"}
    return (
        f"{PEPE} <b>{SHOP_NAME.upper()}</b>\n{ci['flag']} <b>{ci['name']}</b>\n"
        f"{PLANE} Покупка: {str(p['purchase_date']).split()[0]}\n\n"
        f"📱 Номер: <code>{html.escape(str(p['phone_number']))}</code>\n"
        f"⏰ Гарантия до: {p['guaranteed_until']}\n\n"
        f"Нажмите «Получить код» для актуального кода."
    )


def txt_my_accounts(purchases: list[dict]) -> str:
    if not purchases:
        return f"{PEPE} <b>МОИ АККАУНТЫ</b>\n\nУ вас пока нет покупок."
    return f"{PEPE} <b>МОИ АККАУНТЫ</b>\n\nВыберите аккаунт кнопкой в меню ⬇️"


def txt_profile(u: dict) -> str:
    reg = str(u.get("reg_date", "")).split()[0]
    return (
        f"{DIAMOND} <b>ПРОФИЛЬ</b>\n\n"
        f"Аккаунт: @{u.get('username') or 'не указан'}\n"
        f"🆔 ID: <code>{u['user_id']}</code>\n"
        f"💰 Баланс: {_m(float(u.get('balance', 0)))}\n"
        f"📅 Регистрация: {reg}\n"
        f"✅ Статус: {u.get('status', 'Активен')}"
    )


def txt_useful_info() -> str:
    lines = [f"📚 <b>Информация</b>\n", _links()]
    docs = []
    if DOC_RULES_URL:
        docs.append(f"{EMOJI_INFO} <a href=\"{DOC_RULES_URL}\">Правила</a>")
    if DOC_PRIVACY_URL:
        docs.append(f"{EMOJI_GLOBE} <a href=\"{DOC_PRIVACY_URL}\">Политика Конфиденциальности</a>")
    if DOC_TERMS_URL:
        docs.append(f"{EMOJI_INFO} <a href=\"{DOC_TERMS_URL}\">Пользовательское соглашение</a>")
    if docs:
        lines += ["", *docs]
    return "\n".join(lines)


def txt_referral(u: dict) -> str:
    return (
        f"{SPARKLE_STARS} <b>РЕФЕРАЛЬНАЯ ПРОГРАММА</b>\n\n"
        f"Пригласи друга и получай бонус.\n\n"
        f"🔗 <b>Твоя ссылка:</b>\nhttps://t.me/{BOT_USERNAME}?start={u['user_id']}\n\n"
        f"💰 За каждую покупку приглашённого — <b>{_m(REF_BONUS)}</b> на баланс."
    )


# ── Fragment: звёзды / Premium ─────────────────────────────
TXT_FRAG_SOON = f"{DIAMOND} <b>Скоро в продаже</b>\n\nЭтот товар пока недоступен. Загляните позже."


def txt_stars() -> str:
    return (f"{SPARKLE_STARS} <b>TELEGRAM STARS</b>\n\n"
            f"Выберите пакет кнопкой или нажмите «Другое количество» (от 50).\n"
            f"⚡ Звёзды приходят на указанный @username автоматически.")


def txt_premium() -> str:
    return (f"{DIAMOND} <b>TELEGRAM PREMIUM</b>\n\n"
            f"Выберите срок — подписка подарком придёт на указанный @username.\n"
            f"⚠️ Не подходит, если у получателя уже активен Premium.")


def txt_frag_ask_user(title: str) -> str:
    return (f"{QUESTION} <b>{title}</b>\n\nОтправьте @username получателя "
            f"(или нажмите «Себе»).")


def txt_frag_confirm(title: str, target: str, name: str, price: int) -> str:
    who = f"{html.escape(name)} (@{html.escape(target)})" if name else f"@{html.escape(target)}"
    return (f"{QUESTION} <b>Подтвердите заказ</b>\n\n"
            f"📦 {title}\n👤 Получатель: {who}\n💰 К оплате: <b>{_m(price)}</b>\n\n"
            f"Оплата с баланса бота. Проверьте @username — отменить после оплаты нельзя.")


def txt_frag_done(title: str, target: str) -> str:
    return (f"{SPARKLE_STARS} <b>ЗАКАЗ ОТПРАВЛЕН</b>\n\n📦 {title}\n👤 @{html.escape(target)}\n\n"
            f"Fragment обычно доставляет за 1–3 минуты. Если не пришло — напишите в поддержку {SUPPORT_ACCOUNT}.")


# ── Голда ──────────────────────────────────────────────────
def txt_gold() -> str:
    return (f"🥇 <b>ГОЛДА</b>\n\n"
            f"Здесь вы можете <b>купить</b> или <b>продать</b> Голду.\n\n"
            f"Выберите действие, укажите сумму и (по желанию) комментарий — заявка уйдёт администратору, "
            f"он свяжется с вами в этом боте или в личных сообщениях.")


def txt_gold_amount(kind: str, rate: float) -> str:
    verb = "купить" if kind == "buy" else "продать"
    rate_line = (f"\n\n💱 Курс: 1 Голда = <b>{_m(rate)}</b>" if rate > 0 else "")
    return f"✍️ <b>Напишите сумму</b>, которую хотите {verb} (количество Голды числом).{rate_line}"


def txt_gold_comment() -> str:
    return "💬 Оставьте комментарий к заявке или нажмите кнопку «Пропустить»."


def txt_gold_confirm(kind: str, amount: float, comment: str, rate: float) -> str:
    kind_t = "🛒 Покупка Голды" if kind == "buy" else "💰 Продажа Голды"
    est = f"\n💱 Ориентировочно: <b>{_m(amount * rate)}</b>" if rate > 0 else ""
    return (f"{QUESTION} <b>Проверьте заявку</b>\n\n"
            f"📌 Тип: {kind_t}\n"
            f"💰 Сумма: <b>{amount:g}</b>{est}\n"
            f"💬 Комментарий: {html.escape(comment) if comment else '—'}\n\n"
            f"Если всё верно — отправьте заявку.")


def txt_gold_sent(rid: int) -> str:
    return (f"{SPARKLE_STARS} <b>Заявка №{rid} отправлена!</b>\n\n"
            f"Администратор уже получил её и скоро свяжется с вами. "
            f"Ответ придёт сюда, в бот.")


# ── Покупка TON ────────────────────────────────────────────
def txt_ton(price: float, bal: float, mn: float, mx: float) -> str:
    return (f"💠 <b>КУПИТЬ TON</b>\n\n"
            f"Курс: 1 TON = <b>{_m(price)}</b>\n"
            f"Лимиты за один заказ: от {mn:g} до {mx:g} TON\n"
            f"💼 Ваш баланс: {_m(bal)}\n\n"
            f"Выберите пакет кнопкой или нажмите «Своё количество TON». "
            f"TON отправляются автоматически на ваш адрес.")


TXT_TON_SOON = "💠 <b>Покупка TON</b>\n\nСейчас недоступна. Загляните позже."


def txt_ton_ask_address(amount: float, price: float) -> str:
    return (f"{QUESTION} <b>{amount:g} TON — {_m(price)}</b>\n\n"
            f"Отправьте ваш <b>TON-адрес</b> (кошелёк, куда отправить, формат UQ… или EQ…).\n"
            f"⚠️ Проверьте адрес внимательно — перевод необратим. Биржевые адреса требуют memo/комментарий — "
            f"на них лучше не отправлять, используйте личный кошелёк (Tonkeeper, @wallet и т.п.).")


def txt_ton_confirm(amount: float, addr: str, price: float) -> str:
    return (f"{QUESTION} <b>Подтвердите заказ</b>\n\n"
            f"💠 Количество: <b>{amount:g} TON</b>\n"
            f"👛 Адрес: <code>{html.escape(addr)}</code>\n"
            f"💰 К оплате: <b>{_m(price)}</b>\n\n"
            f"Оплата с баланса бота. Отменить после оплаты нельзя — сверьте адрес.")


def txt_ton_done(amount: float, addr: str) -> str:
    return (f"{SPARKLE_STARS} <b>TON ОТПРАВЛЕНЫ</b>\n\n💠 {amount:g} TON\n👛 <code>{html.escape(addr)}</code>\n\n"
            f"Обычно приходят в течение минуты. Если нет — напишите в поддержку {SUPPORT_ACCOUNT}.")


# ── Пополнение: карта / TON ────────────────────────────────
def txt_card_pay(amount: float, number: str, holder: str, bank: str, ttl_h: int) -> str:
    bank_line = f"🏦 Банк: {html.escape(bank)}\n" if bank else ""
    return (f"💳 <b>ПЕРЕВОД НА КАРТУ</b>\n\n"
            f"💰 Сумма: <b>{_m(amount)}</b>\n\n"
            f"💳 Карта: <code>{html.escape(number)}</code>\n"
            f"👤 Получатель: <b>{html.escape(holder)}</b>\n{bank_line}\n"
            f"1) Переведите <b>ровно {_m(amount)}</b>\n"
            f"2) Нажмите «Я оплатил» и отправьте скриншот или чек перевода\n"
            f"3) Администратор проверит и пополнит баланс\n\n"
            f"⏰ Заявка действует {ttl_h} ч.")


def txt_mono_pay(amount: float, number: str, holder: str, bank: str, ttl_min: int) -> str:
    bank_line = f"🏦 Банк: {html.escape(bank)}\n" if bank else ""
    return (f"💳 <b>ПЕРЕВОД НА КАРТУ</b>\n\n"
            f"💰 Сумма к оплате: <b>{amount:.2f} {CUR}</b>\n\n"
            f"💳 Карта: <code>{html.escape(number)}</code>\n"
            f"👤 Получатель: <b>{html.escape(holder)}</b>\n{bank_line}\n"
            f"1) Переведите <b>ровно {amount:.2f} {CUR}</b> — обязательно с копейками, по ним бот находит ваш платёж\n"
            f"2) Нажмите «Проверить оплату» — бот сверит поступление и пополнит баланс\n\n"
            f"⏰ На оплату даётся <b>{ttl_min} минут</b>. На баланс зачисляется ровно переведённая сумма.")


def txt_ton_topup(addr: str, uid: int, rate: float, sell: bool = False) -> str:
    if sell:
        head = ("💠 <b>ПРОДАЖА TON</b>\n\nВы продаёте TON магазину — оплата зачисляется на ваш баланс в ₴ "
                "(им можно платить за любые товары).\n\n")
        rate_line = f"💱 Курс выкупа: 1 TON = <b>{_m(rate)}</b> (берётся в момент зачисления)\n"
    else:
        head = "💠 <b>ПОПОЛНЕНИЕ ЧЕРЕЗ TON</b>\n\n"
        rate_line = f"💱 Курс: 1 TON = <b>{_m(rate)}</b> (берётся в момент зачисления)\n"
    return (head +
            f"Отправьте <b>любую сумму TON</b> на адрес:\n<code>{html.escape(addr)}</code>\n\n"
            f"⚠️ <b>Обязательно</b> укажите в комментарии (memo) ваш ID:\n<code>{uid}</code>\n\n"
            f"{rate_line}"
            f"⏱ Время не ограничено. Баланс пополнится автоматически, когда транзакция подтвердится в сети "
            f"(обычно 1–2 минуты) — или нажмите «Проверить баланс».\n\n"
            f"Без комментария или с чужим ID деньги автоматически не зачислятся — тогда пишите в поддержку {SUPPORT_ACCOUNT}.")
