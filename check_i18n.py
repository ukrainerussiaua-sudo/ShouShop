"""
check_i18n.py — ищет клиентские русские тексты, которых нет в переводе (i18n_uk.py / i18n_uk_extra.py).
Запуск:  python check_i18n.py          (aiogram не нужен)
Админ-хендлеры пропускаются: админка остаётся на русском. Найденное добавьте в i18n_uk_extra.py.
"""
import ast, glob, re, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import i18n
from i18n_uk import PAIRS, COUNTRY_NAMES
from i18n_uk_extra import EXTRA

SKIP = {"i18n_uk.py", "i18n_uk_extra.py", "i18n.py", "tests_e2e.py", "check_i18n.py", "emoji.py", "lolz.py", "fragment.py",
        "ton_wallet.py", "tondeposit.py", "tonrate.py", "monopay.py", "payments.py", "database.py", "config.py", "main.py",
        "handlers/admin.py", "handlers/admin_extra.py", "handlers/fragment_admin.py"}
W = re.compile(r"[А-Яа-яЁё]{3,}")
RU_ONLY = re.compile(r"[ЫыЭэЪъЁё]")
words = lambda d, side: {w.lower() for k, v in d.items() for w in W.findall(k if side == 0 else v)}
ALL = {**PAIRS, **COUNTRY_NAMES, **EXTRA}
RU_V, UK_V = words(ALL, 0), words(ALL, 1)
RU_FUNC = set("что или как при если ещё вы нет это всё нужно после перед чтобы только также можно будет пока сейчас когда где был была были нажмите введите выберите отправьте напишите попробуйте пожалуйста удалось".split())

bad = 0
for f in sorted(glob.glob("*.py") + glob.glob("handlers/*.py")):
    if f in SKIP:
        continue
    tree = ast.parse(open(f, encoding="utf8").read())
    skip_ids = set()                      # докстринги и аргументы print() — клиент их не видит
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant):
            skip_ids.add(id(n.body[0].value))
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print":
            for sub in ast.walk(n):
                skip_ids.add(id(sub))
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)) or id(n) in skip_ids:
            continue
        s = n.value
        if "\n    " in s or len(s) > 600 or s.lstrip().startswith(("[", "(")):
            continue                      # докстринги и лог-сообщения
        t = i18n.tr(s)
        left = [w for w in W.findall(t) if RU_ONLY.search(w) or w.lower() in RU_FUNC
                or (w.lower() in RU_V and w.lower() not in UK_V)]
        if left:
            bad += 1
            print(f"{f}:{n.lineno}: {s[:110]!r}  ← осталось: {sorted(set(left))[:5]}")
print(f"\nНепереведённых фрагментов: {bad}")
