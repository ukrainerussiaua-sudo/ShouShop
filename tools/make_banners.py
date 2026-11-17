"""Генератор баннеров-заглушек: по одному на каждый экран и язык (images/<имя>_<ru|uk|en>.jpg).
На картинке написан язык, название экрана и ТОЧНОЕ имя файла — просто подмените файл своим баннером с тем же именем.
Запуск из корня проекта:  python tools/make_banners.py          (нужен Pillow)
Свои баннеры не перезаписываются: файл, у которого нет метки заглушки (см. MARK), пропускается. Принудительно: --force"""
import os, sys
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "images")
MARK = b"SHOUPLACEHOLDER"                    # метка в JPEG-комментарии: так генератор узнаёт свои заглушки
FONT = next((p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf",
                         "C:/Windows/Fonts/arialbd.ttf", "/Library/Fonts/Arial Bold.ttf") if os.path.exists(p) or "/" not in p), None)

# имя файла → (RU, UK, EN)
SCREENS = {
    "home":     ("Главное меню", "Головне меню", "Main menu"),
    "sub":      ("Подписка на канал", "Підписка на канал", "Channel subscription"),
    "profile":  ("Профиль", "Профіль", "Profile"),
    "topup":    ("Пополнение баланса", "Поповнення балансу", "Top up balance"),
    "info":     ("Полезная информация", "Корисна інформація", "Useful info"),
    "support":  ("Поддержка", "Підтримка", "Support"),
    "telegram": ("Номера Telegram", "Номери Telegram", "Telegram numbers"),
    "catalog":  ("Каталог", "Каталог", "Catalog"),
    "nft":      ("NFT-подарки", "NFT-подарунки", "NFT gifts"),
    "stars":    ("Звёзды", "Зірки", "Stars"),
    "premium":  ("Telegram Premium", "Telegram Premium", "Telegram Premium"),
    "gram":     ("Gram", "Gram", "Gram"),
    "reviews":  ("Отзывы", "Відгуки", "Reviews"),
    "rent":     ("Аренда NFT", "Оренда NFT", "NFT rental"),
    "pay":      ("Оплата заказа", "Оплата замовлення", "Order payment"),
}
HINT = {"ru": "Заглушка · назови файл", "uk": "Заглушка · назви файл", "en": "Placeholder · name the file"}
COLORS = {"ru": ((30, 20, 70), (110, 59, 200)), "uk": ((12, 40, 84), (30, 110, 200)), "en": ((14, 62, 56), (40, 168, 130))}


def font(size):
    return ImageFont.truetype(FONT, size) if FONT else ImageFont.load_default()


def centered(d, y, text, f, fill, W=880):
    w = d.textlength(text, font=f)
    d.text(((W - w) / 2, y), text, font=f, fill=fill)


def make(path, lang, title, fname, W=880, H=480, langs=None):
    top, bot = COLORS[lang]
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    badge = langs or lang.upper()
    bf = font(30)
    bw = d.textlength(badge, font=bf) + 52
    d.rounded_rectangle([W - bw - 20, 20, W - 20, 76], 14, outline="white", width=3)
    d.text((W - bw - 20 + 26, 28), badge, font=bf, fill="white")
    f1 = font(66)
    while d.textlength("SHOU SHOP", font=f1) > W - 80:
        f1 = font(f1.size - 2)
    centered(d, 120, "SHOU SHOP", f1, "white")
    f2 = font(44)
    while d.textlength(title, font=f2) > W - 80:
        f2 = font(f2.size - 2)
    centered(d, 214, title, f2, (230, 225, 255))
    centered(d, 318, HINT.get(lang, HINT["en"]) if not langs else "Placeholder · name the file", font(26), (205, 200, 245))
    centered(d, 358, fname, font(40), "white")
    im.save(path, "JPEG", quality=90, comment=MARK)


def is_mine(path):
    if not os.path.exists(path):
        return True
    try:
        return Image.open(path).info.get("comment", b"") == MARK
    except Exception:
        return False


def main():
    force = "--force" in sys.argv
    n = skipped = 0
    for name, titles in SCREENS.items():
        for lang, title in zip(("ru", "uk", "en"), titles):
            fn = f"{name}_{lang}.jpg"
            p = os.path.join(OUT, fn)
            if not force and not is_mine(p):
                skipped += 1
                continue
            make(p, lang, title, fn)
            n += 1
    p = os.path.join(OUT, "lang.jpg")                      # экран выбора языка — один общий баннер
    if force or is_mine(p):
        make(p, "uk", "Language · Мова · Язык", "lang.jpg", langs="RU · UK · EN")
        n += 1
    print(f"готово: {n} баннеров, пропущено своих: {skipped}")


if __name__ == "__main__":
    main()
