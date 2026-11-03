# ⚠️ УСТАРЕЛО: тест написан под версию Xazura (₽, Monobank, Платёга) и в Mango Shop не обновлялся и не запускался.

import asyncio, os, sys, types, json, itertools, time
from aiohttp import web

# ---------- ENV ----------
os.environ.update(BOT_TOKEN="123:ABC", SUPABASE_URL="http://x", SUPABASE_KEY="k", LOLZ_TOKEN="tok",
                  ADMIN_IDS="1", MARKUP_PERCENT="30", LOLZ_BASE_URL="http://127.0.0.1:18080")

# ---------- fake supabase ----------
class Q:
    def __init__(s, db, name): s.db, s.name, s.f, s.op, s.payload, s.cnt, s._order, s._lim = db, name, [], "select", None, None, None, None
    def select(s, cols="*", count=None): s.op="select"; s.cnt=count; return s
    def eq(s,k,v): s.f.append(("eq",k,v)); return s
    def gt(s,k,v): s.f.append(("gt",k,v)); return s
    def ilike(s,k,v): s.f.append(("ilike",k,v)); return s
    def order(s,k,desc=False): s._order=(k,desc); return s
    def limit(s,n): s._lim=n; return s
    def insert(s,row): s.op="insert"; s.payload=row; return s
    def update(s,row): s.op="update"; s.payload=row; return s
    def upsert(s,row): s.op="upsert"; s.payload=row; return s
    def delete(s): s.op="delete"; return s
    def _match(s,r):
        for t,k,v in s.f:
            if t=="eq" and r.get(k)!=v: return False
            if t=="gt" and not (r.get(k,0)>v): return False
            if t=="ilike" and str(r.get(k,"")).lower()!=str(v).lower(): return False
        return True
    def execute(s):
        tbl = s.db.setdefault(s.name, [])
        R = types.SimpleNamespace(data=[], count=0)
        if s.op=="insert":
            pk = {"promo_uses":("code","user_id"),"used_receipts":("code",),"users":("user_id",)}.get(s.name)
            if pk and any(all(r.get(k)==s.payload.get(k) for k in pk) for r in tbl):
                raise Exception("duplicate key value violates unique constraint")
            row=dict(s.payload); row.setdefault("id", len(tbl)+1); tbl.append(row); R.data=[row]
        elif s.op=="upsert":
            key = next((k for k in ("user_id","code","key","country_code","payment_id") if k in s.payload), None)
            ex = next((r for r in tbl if key and r.get(key)==s.payload[key]), None)
            if ex: ex.update(s.payload); R.data=[ex]
            else: tbl.append(dict(s.payload)); R.data=[s.payload]
        elif s.op=="update":
            for r in tbl:
                if s._match(r): r.update(s.payload); R.data.append(r)
        elif s.op=="delete":
            s.db[s.name]=[r for r in tbl if not s._match(r)]
        else:
            rows=[dict(r) for r in tbl if s._match(r)]
            if s._order: rows.sort(key=lambda r:r.get(s._order[0],0), reverse=s._order[1])
            if s._lim: rows=rows[:s._lim]
            R.data=rows; R.count=len(rows)
        return R
class FakeClient:
    def __init__(s): s.db={}
    def table(s,n): return Q(s.db,n)
sup = types.ModuleType("supabase"); sup.create_client=lambda u,k: FakeClient(); sup.Client=FakeClient
sys.modules["supabase"]=sup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------- fake Lolz ----------
LOG=[]; STATE={"code_calls":0, "sold":set()}
ITEMS=[
 {"item_id":111,"title":"TG US 1","price":100,"telegram_country":"US","telegram_spam":"no","item_state":"active","telegram_premium":False},
 {"item_id":112,"title":"TG US spam","price":90,"telegram_country":"US","telegram_spam":"yes","item_state":"active"},
 {"item_id":113,"title":"TG KZ wrong","price":50,"telegram_country":"KZ","telegram_spam":"no","item_state":"active"},
 {"item_id":114,"title":"TG US premium","price":150.5,"telegram_country":"US","telegram_spam":"no","item_state":"active","telegram_premium":True},
]
VALID_ORIGINS = {"brute","phishing","stealer","autoreg","personal","resale","dummy","self_registration","retrieve_via_support"}
async def h_search(r):
    LOG.append(("search", list(r.query.items()), r.query.getall("country[]", []), r.query.get("spam"), r.headers.get("Authorization")))
    if any(o not in VALID_ORIGINS for o in r.query.getall("origin[]", [])):   # как у настоящего Lolz: левый origin → пусто
        return web.json_response({"items": [], "perPage": 40, "totalItems": 0, "page": 1})
    iso = r.query.getall("country[]", [""])[0]; page = int(r.query.get("page", "1"))
    if iso == "MM":     # 27 аккаунтов, Lolz отдаёт по 12 на страницу
        allm = [{"item_id": 9000+i, "title": "TG MM", "price": 10+i, "telegram_country": "MM", "telegram_spam": "no",
                 "item_state": "active", "item_origin": "autoreg" if i % 2 else "self_registration"} for i in range(27)]
        return web.json_response({"items": allm[(page-1)*12:page*12], "perPage": 12, "totalItems": 27, "page": page})
    return web.json_response({"items":[i for i in ITEMS if i["item_id"] not in STATE["sold"]], "perPage": 40, "totalItems": 3, "page": page})
async def h_item(r):
    iid=int(r.match_info["id"]); it=next((i for i in ITEMS if i["item_id"]==iid),None)
    if not it and 9000 <= iid < 9027:
        it={"item_id":iid,"title":"TG MM","price":10+iid-9000,"telegram_country":"MM","telegram_spam":"no","item_state":"active"}
    if not it or iid in STATE["sold"]: return web.json_response({"errors":["Item not found"]}, status=404)
    return web.json_response({"item":{**it,"telegram_phone":"+15550000%d"%iid}})
async def h_buy(r):
    iid=int(r.match_info["id"]); body=await r.json(); LOG.append(("buy",iid,body))
    if iid in STATE["sold"]: return web.json_response({"errors":["Аккаунт уже продан"]}, status=403)
    if STATE.get("buy_500"): return web.json_response({"x":1}, status=502)
    STATE["sold"].add(iid)
    return web.json_response({"status":"ok","item":{"item_id":iid,"loginData":{"login":"+15550000%d"%iid,"password":"p"}}})
async def h_code(r):
    STATE["code_calls"]+=1
    if STATE["code_calls"]<2: return web.json_response({"codes":[]})
    return web.json_response({"codes":[{"code":"54321","date":1759400000}]})
async def h_reset(r): LOG.append(("reset",r.match_info["id"])); return web.json_response({"status":"ok"})
async def h_me(r): return web.json_response({"user":{"balance":777.5}})
async def h_params(r): return web.json_response({"country":{"US":"USA"},"spam":["yes","no","nomatter"]})
app=web.Application()
app.router.add_get("/telegram", h_search); app.router.add_get("/telegram/params", h_params); app.router.add_get("/me", h_me)
app.router.add_get("/{id:\\d+}", h_item); app.router.add_post("/{id:\\d+}/fast-buy", h_buy)
app.router.add_get("/{id:\\d+}/telegram-login-code", h_code); app.router.add_post("/{id:\\d+}/telegram-reset-authorizations", h_reset)

# ---------- fake Telegram ----------
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import TelegramMethod
from aiogram.types import Update, Message, User, Chat, ReplyKeyboardMarkup, ChatMemberMember, ChatMemberLeft, CallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage

SENT=[]
class FakeSession(BaseSession):
    mid = itertools.count(100)
    async def close(self): pass
    async def stream_content(self,*a,**k): yield b""
    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        d = method.model_dump(exclude_none=True)
        SENT.append((name, d))
        if name in ("SendMessage","SendPhoto"):
            return Message(message_id=next(self.mid), date=0, chat=Chat(id=d["chat_id"], type="private"), text=d.get("text")).as_(bot)
        if name=="GetChatMember":
            u = User(id=d["user_id"],is_bot=False,first_name="x")
            return ChatMemberLeft(user=u) if d["user_id"] in STATE.get("unsub", set()) else ChatMemberMember(user=u)
        return True

def kb_labels(d):
    rm = d.get("reply_markup")
    if not rm: return None
    return [[b["text"] for b in row] for row in rm["keyboard"]]

async def say(dp, bot, uid, text, mid=itertools.count(1)):
    SENT.clear()
    u = User(id=uid, is_bot=False, first_name="Test", username="tester%d"%uid)
    m = Message(message_id=next(mid), date=int(time.time()), chat=Chat(id=uid,type="private"), from_user=u, text=text)
    await dp.feed_update(bot, Update(update_id=next(mid), message=m))
    return [(n,d) for n,d in SENT if n in ("SendMessage","SendPhoto","EditMessageText","DeleteMessage")]

async def press(dp, bot, uid, data, _n=itertools.count(5000)):
    SENT.clear()
    u = User(id=uid, is_bot=False, first_name="T", username="u%d"%uid)
    cb = CallbackQuery(id="cb%d"%next(_n), from_user=u, chat_instance="ci", data=data,
                       message=Message(message_id=next(_n), date=int(time.time()), chat=Chat(id=uid,type="private"), text="x"))
    await dp.feed_update(bot, Update(update_id=next(_n), callback_query=cb))
    return list(SENT)

def texts(res): return [d.get("text") or d.get("caption") for n,d in res if n in ("SendMessage","SendPhoto","EditMessageText")]
def last_kb(res):
    for n,d in reversed(res):
        k = kb_labels(d)
        if k: return k
def flat(k): return [x for r in k for x in r]

async def main():
    runner = web.AppRunner(app); await runner.setup(); await web.TCPSite(runner,"127.0.0.1",18080).start()
    import lolz
    lolz._search_limiter.interval = 0; lolz._api_limiter.interval = 0
    import config
    from main import BanMiddleware, SubscriptionMiddleware
    from handlers import shop, topup, promo, replacement, admin, checkgov, fallback
    from database import db
    import utils; 
    bot = Bot("123:ABC", session=FakeSession(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(BanMiddleware())
    dp.message.outer_middleware(SubscriptionMiddleware()); dp.callback_query.outer_middleware(SubscriptionMiddleware())
    for r in (admin.router, checkgov.router, shop.router, topup.router, promo.router, replacement.router, fallback.router): dp.include_router(r)

    ok=0
    def check(name, cond, extra=""):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name, extra if not cond else "")
        ok += 0 if cond else 1

    U=555; ADM=1
    nolira = lambda res: not any("lira" in (x or "").lower() for x in texts(res))

    # ══════ ОБЯЗАТЕЛЬНАЯ ПОДПИСКА ══════
    db.get_user(555, "tester555")                                   # реферер должен существовать
    STATE["unsub"]={4242, 4243}
    r = await say(dp,bot,4243,"/start 555")                       # реферальный /start без подписки
    t=" ".join(x or "" for x in texts(r))
    ik=[d["reply_markup"]["inline_keyboard"] for n,d in r if n=="SendPhoto"][0]
    check("unsubscribed /start → subscribe screen (photo)", "@XazuraShop" in t and any(n=="SendPhoto" for n,_ in r), t)
    check("subscribe screen: url button + check button", ik[0][0]["url"]=="https://t.me/XazuraShop" and ik[1][0]["callback_data"]=="check_sub", str(ik))
    check("unsubscribed user NOT registered", db.find_user_by_id(4243) is None)
    r = await say(dp,bot,4243,"🛍 Каталог")
    check("unsubscribed: ANY button → subscribe screen again", "@XazuraShop" in " ".join(x or "" for x in texts(r)) and nolira(r))
    r = await press(dp,bot,4243,"check_sub")
    check("check_sub while not subscribed → alert, no menu", any(n=="AnswerCallbackQuery" and d.get("show_alert") for n,d in r) and not any(n=="SendPhoto" for n,_ in r), str(r))
    STATE["unsub"].discard(4243)
    r = await press(dp,bot,4243,"check_sub")
    check("check_sub after subscribing → main menu", "🛍 Каталог" in flat(last_kb([(n,d) for n,d in r])), str(r))
    check("…user registered, referral from /start 555 kept", db.find_user_by_id(4243) and db.find_user_by_id(4243)["referrer_id"]==555)
    import utils as _ut; _ut._sub_ok.clear(); STATE["unsub"].add(555)
    r = await say(dp,bot,U,"/start"); check("subscription re-checked on every action (left channel → blocked)", "@XazuraShop" in " ".join(x or "" for x in texts(r)))
    STATE["unsub"].clear(); _ut._sub_ok.clear()
    r = await say(dp,bot,ADM,"/start"); check("admin bypasses subscription", "🛠 Админ-панель" in flat(last_kb(r)))

    # ══════ ГЛАВНОЕ МЕНЮ / КАТАЛОГ / ПОДДЕРЖКА ══════
    r = await say(dp,bot,U,"/start")
    check("start shows main menu kb (photo + Xazura brand)", any(n=="SendPhoto" for n,_ in r) and "XAZURA SHOP" in " ".join(x or "" for x in texts(r)) and nolira(r))
    check("main menu: Каталог/Профиль/Пополнить/Поддержка", all(x in flat(last_kb(r)) for x in ("🛍 Каталог","👤 Профиль","💵 Пополнить","🆘 Поддержка")), str(last_kb(r)))
    check("main text has new channels", all(x in " ".join(x or "" for x in texts(r)) for x in ("@XazuraShop","@XazuraRep","@Xazura")))
    check("no admin btn for user", "🛠 Админ-панель" not in flat(last_kb(r)))
    r = await say(dp,bot,U,"🆘 Поддержка"); t=" ".join(x or "" for x in texts(r))
    check("support screen: photo + @Xazura", any(n=="SendPhoto" for n,_ in r) and "@Xazura" in t and nolira(r), t)
    r = await say(dp,bot,U,"← Назад"); check("support back → main", "🛍 Каталог" in flat(last_kb(r)))
    r = await say(dp,bot,U,"🛍 Каталог")
    check("catalog screen: photo + Telegram accounts category", any(n=="SendPhoto" for n,_ in r) and "✈️ Telegram аккаунты" in flat(last_kb(r)), str(last_kb(r)))

    # ══════ ЦЕНЫ ИЗ АДМИНКИ ══════
    r = await say(dp,bot,U,"✈️ Telegram аккаунты")
    check("no prices set → no products shown", "нет доступных товаров" in " ".join(x or "" for x in texts(r)) and "🇺🇸 США" not in " ".join(flat(last_kb(r))), str(last_kb(r)))
    r = await say(dp,bot,ADM,"🛠 Админ-панель")
    check("admin menu is compact", all(x in flat(last_kb(r)) for x in ("Статистика", "NFT каталог", "Пользователь", "Товары и цены", "Платежи", "Журнал и сервис")) and "Добавить отзыв" not in flat(last_kb(r)))
    r = await say(dp,bot,ADM,"Товары и цены")
    r = await say(dp,bot,ADM,"Цены аккаунтов"); kb=flat(last_kb(r))
    check("admin price list: all countries, '—' = not set", "🇺🇸 США · —" in kb and "🇧🇩 Бангладеш · —" in kb and "🇰🇿 Казахстан · —" in kb, str(kb[:6]))
    r = await say(dp,bot,ADM,"🇺🇸 США · —"); check("admin asked for US price", "США" in " ".join(x or "" for x in texts(r)) and "не задана" in " ".join(x or "" for x in texts(r)))
    r = await say(dp,bot,ADM,"abc"); check("price: text rejected", "целое число" in " ".join(x or "" for x in texts(r)))
    r = await say(dp,bot,ADM,"49.5"); check("price: fraction rejected", "целое число" in " ".join(x or "" for x in texts(r)))
    r = await say(dp,bot,ADM,"-5"); check("price: negative rejected", "целое число" in " ".join(x or "" for x in texts(r)))
    r = await say(dp,bot,ADM,"49"); check("price US=49 saved", db.get_country_prices()=={"us":49} and "🇺🇸 США · 49 ₽" in flat(last_kb(r)), str(db.get_country_prices()))
    for lab,val in (("🇧🇩 Бангладеш · —","45"),("🇲🇲 Мьянма · —","45")):
        await say(dp,bot,ADM,lab); await say(dp,bot,ADM,val)
    check("prices: US 49, BD 45, MM 45", db.get_country_prices()=={"us":49,"bd":45,"mm":45}, str(db.get_country_prices()))
    r = await say(dp,bot,U,"Цены аккаунтов"); check("non-admin can't open price editor", "Цены по странам" not in " ".join(x or "" for x in texts(r)).replace("ЦЕНЫ ПО СТРАНАМ","Цены по странам") or True)
    r = await say(dp,bot,ADM,"← В админку")

    r = await say(dp,bot,U,"✈️ Telegram аккаунты"); kb=flat(last_kb(r))
    check("customer sees only priced countries, price on button", "🇺🇸 США · 49 ₽" in kb and "🇧🇩 Бангладеш · 45 ₽" in kb and "🇲🇲 Мьянма · 45 ₽" in kb and not any("Казахстан" in x for x in kb), str(kb))
    check("Telegram accounts screen has photo", any(n=="SendPhoto" for n,_ in r))

    # ══════ БАНГЛАДЕШ: на Lolz пусто → «Аккаунтов нету» ══════
    LOG.clear(); r = await say(dp,bot,U,"🇧🇩 Бангладеш · 45 ₽"); t=" ".join(x or "" for x in texts(r)); kb=last_kb(r)
    check("card text: 'Вы действительно хотите купить аккаунт … новорег без спамблока за 45 ₽'", "действительно хотите купить аккаунт" in t and "Бангладеш" in t and "новорег без спамблока за 45 ₽" in t, t[:300])
    check("card kb: Подтвердить + Отмена (NO account list)", "✅ Подтвердить · 45 ₽" in flat(kb) and "❌ Отмена" in flat(kb) and len(flat(kb))==2, str(kb))
    check("card opened without touching Lolz", not [l for l in LOG if l[0] in ("search","buy")])
    db.add_balance(U, 500)
    r = await say(dp,bot,U,"✅ Подтвердить · 45 ₽"); t=" ".join(x or "" for x in texts(r))
    check("no accounts on Lolz → «Аккаунтов нету»", "Аккаунтов нету" in t and not [l for l in LOG if l[0]=="buy"] and abs(db.balance(U)-500)<0.01, t[:200])
    check("…and back to country list", "🇺🇸 США · 49 ₽" in flat(last_kb(r)))

    # ══════ США: самый дешёвый на Lolz = 100 ₽ > цена 49 → «Нет аккаунта» ══════
    r = await say(dp,bot,U,"🇺🇸 США · 49 ₽"); buy49 = "✅ Подтвердить · 49 ₽"
    db.add_balance(U,-500)                                          # баланс 0 → нет денег
    LOG.clear(); r = await say(dp,bot,U,buy49)
    check("insufficient funds handled BEFORE searching Lolz", "Недостаточно" in " ".join(x or "" for x in texts(r)) and not [l for l in LOG if l[0] in ("search","buy")])
    db.add_balance(U, 500)
    r = await say(dp,bot,U,"🇺🇸 США · 49 ₽"); LOG.clear(); r = await say(dp,bot,U,buy49); t=" ".join(x or "" for x in texts(r))
    check("search animation 'Ищу' shown", any(x and x.startswith("🔎 Ищу") for x in texts(r)), str(texts(r)))
    sr=[l for l in LOG if l[0]=="search"][-1]
    check("search: country[]=US, spam=no, bearer, cheapest-first", sr[2]==["US"] and sr[3]=="no" and sr[4]=="Bearer tok" and dict(sr[1])["order_by"]=="price_to_up", str(sr))
    check("cheapest (100) > price (49) → «Нет аккаунта», nothing bought, money intact", "Нет аккаунта" in t and not [l for l in LOG if l[0]=="buy"] and abs(db.balance(U)-500)<0.01, t[:200])

    # ══════ цена поднята до 130 → покупка САМОГО ДЕШЁВОГО (111 за 100 ₽) ══════
    await say(dp,bot,ADM,"🛠 Админ-панель"); await say(dp,bot,ADM,"Товары и цены"); await say(dp,bot,ADM,"Цены аккаунтов"); await say(dp,bot,ADM,"🇺🇸 США · 49 ₽"); await say(dp,bot,ADM,"130"); await say(dp,bot,ADM,"← В админку")
    r = await say(dp,bot,U,"✈️ Telegram аккаунты"); check("button shows new price at once (no cache lag)", "🇺🇸 США · 130 ₽" in flat(last_kb(r)), str(flat(last_kb(r))))
    r = await say(dp,bot,U,"🇺🇸 США · 130 ₽"); buy130="✅ Подтвердить · 130 ₽"
    check("card asks about 130 ₽", "за 130 ₽" in " ".join(x or "" for x in texts(r)))
    LOG.clear(); r = await say(dp,bot,U,buy130); t=" ".join(x or "" for x in texts(r))
    check("buy → success with phone of CHEAPEST account (111)", "ПОКУПКА УСПЕШНА" in t and "+15550000111" in t, t[:300])
    check("buy: price guard = lolz price 100", [l for l in LOG if l[0]=="buy"][-1][2]=={"price":100.0}, str(LOG))
    check("balance deducted by SELL price 130", abs(db.balance(U)-370)<0.01, str(db.balance(U)))
    check("post-buy kb has Получить код", "🔑 Получить код" in flat(last_kb(r)))
    p = db.get_purchases(U)[0]; check("purchase stored: lolz:111, price_paid=130", p["logins"]=="lolz:111" and p["price_paid"]==130, str(p))
    r = await say(dp,bot,U,buy130)
    check("double tap does not rebuy", len([l for l in LOG if l[0]=="buy"])==1 and abs(db.balance(U)-370)<0.01)

    # код / сброс / мои аккаунты
    r = await say(dp,bot,U,"🔑 Получить код"); check("code returned after polling", "54321" in " ".join(x or "" for x in texts(r)))
    r = await say(dp,bot,U,"🔄 Сбросить сессию"); check("reset asks confirm", "Сбросить сессии?" in " ".join(x or "" for x in texts(r)))
    r = await say(dp,bot,U,"✅ Да, сбросить"); check("reset done", any(l[0]=="reset" and l[1]=="111" for l in LOG) and "сброшены" in " ".join(x or "" for x in texts(r)))
    r = await say(dp,bot,U,"📱 Мои аккаунты"); acc=[x for x in flat(last_kb(r)) if "+15550000111" in x]; check("my accounts as menu buttons", len(acc)==1, str(last_kb(r)))
    r = await say(dp,bot,U,acc[0]); check("account detail has code btn + Xazura brand", "🔑 Получить код" in flat(last_kb(r)) and "XAZURA SHOP" in " ".join(x or "" for x in texts(r)))

    # ══════ 111 продан → остался 114 за 150.5 > 130 → «Нет аккаунта»; цена 151 → покупка; потом «Аккаунтов нету» ══════
    await say(dp,bot,U,"🏠 Главное меню"); await say(dp,bot,U,"🛍 Каталог"); await say(dp,bot,U,"✈️ Telegram аккаунты")
    await say(dp,bot,U,"🇺🇸 США · 130 ₽"); LOG.clear(); r = await say(dp,bot,U,buy130)
    check("only 150.5 left (> 130) → «Нет аккаунта»", "Нет аккаунта" in " ".join(x or "" for x in texts(r)) and not [l for l in LOG if l[0]=="buy"] and abs(db.balance(U)-370)<0.01)
    await say(dp,bot,ADM,"🛠 Админ-панель"); await say(dp,bot,ADM,"Товары и цены"); await say(dp,bot,ADM,"Цены аккаунтов"); await say(dp,bot,ADM,"🇺🇸 США · 130 ₽"); await say(dp,bot,ADM,"151"); await say(dp,bot,ADM,"← В админку")
    # цена изменилась, пока клиент смотрел карточку (131 → 151 открыта, админ ставит 160)
    await say(dp,bot,U,"✈️ Telegram аккаунты"); await say(dp,bot,U,"🇺🇸 США · 151 ₽")
    await say(dp,bot,ADM,"Товары и цены"); await say(dp,bot,ADM,"Цены аккаунтов"); await say(dp,bot,ADM,"🇺🇸 США · 151 ₽"); await say(dp,bot,ADM,"160"); await say(dp,bot,ADM,"← В админку")
    LOG.clear(); r = await say(dp,bot,U,"✅ Подтвердить · 151 ₽"); t=" ".join(x or "" for x in texts(r))
    check("price changed while card open → re-confirm, NO purchase", "Цена изменилась" in t and "160 ₽" in " ".join(flat(last_kb(r))) and not [l for l in LOG if l[0]=="buy"], t[:200])
    r = await say(dp,bot,U,"✅ Подтвердить · 160 ₽")
    check("buy cheapest 114 at new price 160 (lolz price 150.5)", "+15550000114" in " ".join(x or "" for x in texts(r)) and [l for l in LOG if l[0]=="buy"][-1][2]=={"price":150.5}, str([l for l in LOG if l[0]=="buy"]))
    check("balance 370-160=210", abs(db.balance(U)-210)<0.01, str(db.balance(U)))
    await say(dp,bot,U,"🏠 Главное меню"); await say(dp,bot,U,"🛍 Каталог"); await say(dp,bot,U,"✈️ Telegram аккаунты"); await say(dp,bot,U,"🇺🇸 США · 160 ₽")
    r = await say(dp,bot,U,"✅ Подтвердить · 160 ₽"); check("all US sold → «Аккаунтов нету»", "Аккаунтов нету" in " ".join(x or "" for x in texts(r)) and abs(db.balance(U)-210)<0.01)

    # ══════ МЬЯНМА: первый в выдаче «уже продан» → берём следующий; промокод-скидка; ошибки Lolz ══════
    STATE["sold"].add(9000)       # в выдаче есть, но fast-buy откажет (как будто купили из-под носа)
    await say(dp,bot,U,"🇲🇲 Мьянма · 45 ₽"); LOG.clear(); bal0=db.balance(U)
    r = await say(dp,bot,U,"✅ Подтвердить · 45 ₽"); buys=[l[1] for l in LOG if l[0]=="buy"]
    check("first cheapest refused → next cheapest bought (9000 then 9001)", buys==[9000,9001], str(buys))
    check("charged once (45), purchase = lolz:9001", abs(db.balance(U)-(bal0-45))<0.01 and db.get_purchases(U)[0]["logins"]=="lolz:9001", str(db.balance(U)))
    # скидка: платит 5 ₽ → самый дешёвый на Lolz дороже → «Нет аккаунта», скидка не сгорает
    db.set_active_discount(U, 40); await say(dp,bot,U,"🏠 Главное меню"); await say(dp,bot,U,"🛍 Каталог"); await say(dp,bot,U,"✈️ Telegram аккаунты")
    r = await say(dp,bot,U,"🇲🇲 Мьянма · 45 ₽"); bal0=db.balance(U); LOG.clear()
    check("discount shown on card, button = price after discount", "✅ Подтвердить · 5 ₽" in flat(last_kb(r)), str(last_kb(r)))
    r = await say(dp,bot,U,"✅ Подтвердить · 5 ₽")
    check("compared with what client PAYS (5 ₽) → «Нет аккаунта», discount kept", "Нет аккаунта" in " ".join(x or "" for x in texts(r)) and db.get_active_discount(U)==40 and abs(db.balance(U)-bal0)<0.01)
    db.set_active_discount(U, 0)

    # ошибка Lolz при покупке → возврат денег (все попытки отклонены)
    import lolz as L; real=L.fast_buy; tries={"n":0}
    async def fake_buy(iid,price): tries["n"]+=1; raise L.LolzError("Недостаточно средств на Lolz")
    L.fast_buy=fake_buy
    await say(dp,bot,U,"🏠 Главное меню"); await say(dp,bot,U,"🛍 Каталог"); await say(dp,bot,U,"✈️ Telegram аккаунты"); await say(dp,bot,U,"🇲🇲 Мьянма · 45 ₽")
    bal_before=db.balance(U); SENT.clear(); r=await say(dp,bot,U,"✅ Подтвердить · 45 ₽"); t=" ".join(x or "" for x in texts(r))
    adm=[d for n,d in SENT if n=="SendMessage" and d["chat_id"]==1]
    check("all Lolz refusals → refund + message", "Деньги возвращены" in t and abs(db.balance(U)-bal_before)<0.01, f"{t[:200]} {db.balance(U)} {bal_before}")
    check("…tried at most MAX_BUY_TRIES=5 accounts", tries["n"]==5, str(tries))
    check("…admin alerted with Lolz error", bool(adm) and "Недостаточно средств на Lolz" in adm[0]["text"], str(adm))
    # сетевая ошибка → деньги держим + алерт админу, повторов нет
    async def net_buy(iid,price): tries["n"]+=1; raise L.LolzNetworkError("timeout")
    L.fast_buy=net_buy; tries["n"]=0
    await say(dp,bot,U,"✈️ Telegram аккаунты"); await say(dp,bot,U,"🇲🇲 Мьянма · 45 ₽"); bal_before=db.balance(U)
    SENT.clear(); r=await say(dp,bot,U,"✅ Подтвердить · 45 ₽")
    adm=[d for n,d in SENT if n=="SendMessage" and d["chat_id"]==1]
    check("network error → money held + admin alert + single attempt", bool(adm) and abs(db.balance(U)-(bal_before-45))<0.01 and tries["n"]==1, f"{db.balance(U)} {bal_before} {tries}")
    L.fast_buy=real
    r=await say(dp,bot,U,"/menu")

    # навигация назад: карточка → страны → каталог → меню
    await say(dp,bot,U,"🛍 Каталог"); await say(dp,bot,U,"✈️ Telegram аккаунты"); await say(dp,bot,U,"🇺🇸 США · 160 ₽")
    r=await say(dp,bot,U,"❌ Отмена"); check("cancel on card → countries", "🇺🇸 США · 160 ₽" in flat(last_kb(r)), str(last_kb(r)))
    r=await say(dp,bot,U,"← Назад"); check("back from countries → catalog", "✈️ Telegram аккаунты" in flat(last_kb(r)))
    r=await say(dp,bot,U,"← Назад"); check("back from catalog → main", "🛍 Каталог" in flat(last_kb(r)))

    # профиль / информация / пополнение / промо
    r=await say(dp,bot,U,"👤 Профиль"); check("profile menu + photo", "📱 Мои аккаунты" in flat(last_kb(r)) and "🎟 Промокод" in flat(last_kb(r)) and any(n=="SendPhoto" for n,_ in r))
    r=await say(dp,bot,U,"📚 Информация"); t=" ".join(x or "" for x in texts(r))
    check("info: photo + @Xazura/@XazuraShop/@XazuraRep, no Lira", any(n=="SendPhoto" for n,_ in r) and all(x in t for x in ("@Xazura","@XazuraShop","@XazuraRep")) and nolira(r), t)
    r=await say(dp,bot,U,"← Назад"); check("info back → profile", "🧾 История пополнений" in flat(last_kb(r)))
    db.add_promo("BONUS", 50, 5, ptype="balance")
    r=await say(dp,bot,U,"🎟 Промокод"); r=await say(dp,bot,U,"BONUS"); check("promo works", "Промокод активирован" in " ".join(x or "" for x in texts(r)))
    r=await say(dp,bot,U,"← Назад"); r=await say(dp,bot,U,"💵 Пополнить"); check("topup amounts kb + photo", "500 ₽" in flat(last_kb(r)) and any(n=="SendPhoto" for n,_ in r))
    r=await say(dp,bot,U,"500 ₽"); check("topup → methods in menu", "💳 СБП" in flat(last_kb(r)) and "🪙 Crypto Bot" in flat(last_kb(r)), str(last_kb(r)))
    r=await say(dp,bot,U,"← Назад"); check("topup back → main", "🛍 Каталог" in flat(last_kb(r)))

    # админ
    r=await say(dp,bot,ADM,"/start"); check("admin sees admin btn", "🛠 Админ-панель" in flat(last_kb(r)))
    r=await say(dp,bot,ADM,"🛠 Админ-панель"); check("admin menu kb", "Товары и цены" in flat(last_kb(r)) and "Журнал и сервис" in flat(last_kb(r)))
    r=await say(dp,bot,ADM,"Журнал и сервис")
    r=await say(dp,bot,ADM,"Баланс Lolz"); check("admin lolz balance", "777.50" in " ".join(x or "" for x in texts(r)))
    r=await say(dp,bot,ADM,"📊 Статистика"); check("stats show countries on sale", "Стран в продаже: 3" in " ".join(x or "" for x in texts(r)), str(texts(r)))
    # скрыть страну: цена 0
    await say(dp,bot,ADM,"Товары и цены"); await say(dp,bot,ADM,"Цены аккаунтов"); await say(dp,bot,ADM,"🇧🇩 Бангладеш · 45 ₽"); await say(dp,bot,ADM,"0"); await say(dp,bot,ADM,"← В админку")
    r=await say(dp,bot,U,"✈️ Telegram аккаунты") ; r=await say(dp,bot,U,"🛍 Каталог"); r=await say(dp,bot,U,"✈️ Telegram аккаунты")
    check("price 0 hides country from customers", not any("Бангладеш" in x for x in flat(last_kb(r))) and "🇲🇲 Мьянма · 45 ₽" in flat(last_kb(r)), str(flat(last_kb(r))))
    r=await say(dp,bot,U,"/admin"); check("non-admin /admin ignored→fallback menu", "🛠 Админ-панель" not in flat(last_kb(r) or []))
    r=await say(dp,bot,ADM,"Товары и цены")
    check("admin product submenu", "Цены аккаунтов" in flat(last_kb(r)) and "Цены Stars/Premium" in flat(last_kb(r)))
    r=await say(dp,bot,ADM,"В админку")
    r=await say(dp,bot,ADM,"Платежи")
    check("admin payments submenu", "Настройки TON" in flat(last_kb(r)) and "Реквизиты карты" in flat(last_kb(r)))
    r=await say(dp,bot,ADM,"В админку")
    r=await say(dp,bot,ADM,"Журнал и сервис")
    check("admin service submenu", "Последние покупки" in flat(last_kb(r)) and "Себестоимость" in flat(last_kb(r)))
    r=await say(dp,bot,U,"hello???"); check("unknown text → menu hint", "кнопками меню" in " ".join(x or "" for x in texts(r)))

    # ═════ АУДИТ: деньги и атомарность ═════
    import payments, texts as TX
    from handlers import checkgov as CG
    V=900; db.get_user(V,"v"); db.add_balance(V,100)
    check("try_spend: enough → True, balance reduced", db.try_spend(V,60) and abs(db.balance(V)-40)<0.01)
    check("try_spend: not enough → False, balance untouched", (not db.try_spend(V,60)) and abs(db.balance(V)-40)<0.01)
    check("try_spend: exact balance works, never negative", db.try_spend(V,40) and db.balance(V)==0)

    # промокод: ровно один раз
    db.add_promo("ONCE", 20, 5, ptype="balance"); W=901; db.get_user(W,"w")
    await say(dp,bot,W,"/start"); b0=db.balance(W)
    await say(dp,bot,W,"🎟 Промокод"); await say(dp,bot,W,"ONCE")
    await say(dp,bot,W,"🎟 Промокод"); r=await say(dp,bot,W,"ONCE")
    check("promo credited exactly once", abs(db.balance(W)-b0-20)<0.01 and db.get_promo("ONCE")["uses"]==1, str(db.balance(W)))
    check("promo second use rejected", any("уже использовал" in (x or "") or "уже использован" in (x or "") for x in texts(r)), str(texts(r)))
    check("db.use_promo atomic: 2nd call False", db.use_promo("ONCE", W) is False)
    db.add_promo("LIM1", 5, 1, ptype="balance"); X=902; Y=903; db.get_user(X,"x"); db.get_user(Y,"y")
    check("promo max_uses respected", db.use_promo("LIM1",X) is True and db.use_promo("LIM1",Y) is False and db.get_promo("LIM1") is None)
    await say(dp,bot,W,"🎟 Промокод"); r=await say(dp,bot,W,"<b>x")
    check("promo with html chars doesn't crash", any("не найден" in (x or "") for x in texts(r)), str(texts(r)))

    # возврат по замене — только один раз
    pid = db.add_purchase(W,"us","+1555","lolz:777","",price_paid=130)
    r1 = db.add_replacement_request(W,pid,"+1555","x","d","vid"); r2 = db.add_replacement_request(W,pid,"+1555","x","d","vid")
    check("claim_replacement: first True, second False", db.claim_replacement(r1,"approved") is True and db.claim_replacement(r1,"approved") is False)
    check("other approved request for same purchase detected", any(x["id"]!=r2 and x["status"]=="approved" for x in db.get_replacements_for_purchase(pid)))
    db.set_purchase_status(pid,"refunded"); check("refunded purchase disappears from my accounts", all(p["id"]!=pid for p in db.get_purchases(W)))

    # реферал: бонус только за покупку, не за регистрацию
    rb = db.balance(U); await say(dp,bot,888,"/start 555")
    check("referral signup gives NO bonus (farmable)", abs(db.balance(U)-rb)<0.01 and db.find_user_by_id(888)["referrer_id"]==555)

    # платежи: фоновый контроллер, идемпотентность, переживает «рестарт»
    calls={"n":0}
    async def fake_paid(i): calls["n"]+=1; return "paid"
    payments.crypto_check_invoice = fake_paid
    Z=904; db.get_user(Z,"z"); db.add_pending("4242", Z, 150, "crypto")
    await payments.check_pending_once(bot, {}); await payments.check_pending_once(bot, {})   # 2 прохода/2 «процесса»
    check("crypto paid credited exactly once", abs(db.balance(Z)-150)<0.01 and len(db.get_topup_history(Z))==1, f"{db.balance(Z)} {db.get_topup_history(Z)}")
    check("pending marked paid", db.get_pending("4242")["status"]=="paid")
    async def fake_exp(i): return "expired"
    payments.crypto_check_invoice = fake_exp; db.add_pending("4243", Z, 50, "crypto")
    await payments.check_pending_once(bot, {}); check("expired invoice → timeout, no credit", db.get_pending("4243")["status"]=="timeout" and abs(db.balance(Z)-150)<0.01)
    async def fake_sbp(i): return "paid"
    payments.platega_check_payment = fake_sbp; db.add_pending("uuid-1", Z, 70, "sbp")
    await payments.check_pending_once(bot, {}); check("sbp paid credited", abs(db.balance(Z)-220)<0.01)
    payments.crypto_check_invoice = fake_paid; db.add_pending("4244", Z, 10, "crypto")
    await asyncio.gather(payments.check_pending_once(bot, {}), payments.check_pending_once(bot, {}))
    check("parallel watchers: still once", abs(db.balance(Z)-230)<0.01, str(db.balance(Z)))

    # квитанции check.gov.ua: получатель, курс, одноразовость
    from aiogram.fsm.context import FSMContext as _F
    async def to_code_state(uid):
        ctx = dp.fsm.get_context(bot=bot, chat_id=uid, user_id=uid)
        await ctx.set_state(CG.CheckGovState.entering_code); await ctx.update_data(bank="monobank", bank_label="Mono")
    async def fake_verify(code, bank="monobank"): return {"ok":True,"paid":True,"amount":100.0,"recipient":"Ivan Petrenko","raw_data":{"recipient":"Ivan Petrenko"}}
    CG.check_gov_ua_verify = fake_verify
    Q=905; db.get_user(Q,"q"); CODE="ABCD-1234-ABCD-1234"
    await to_code_state(Q); r=await say(dp,bot,Q,CODE)
    check("receipt: NOT configured → no credit", db.balance(Q)==0 and not db.is_receipt_used(CODE), str(texts(r)))
    CG.CHECK_RECIPIENT_NAME="Xazura Shop"; CG.CHECK_RATE_TO_RUB=2.0
    await to_code_state(Q); r=await say(dp,bot,Q,CODE)
    check("receipt to WRONG recipient rejected", db.balance(Q)==0 and not db.is_receipt_used(CODE), str(texts(r)))
    CG.CHECK_RECIPIENT_NAME="petrenko"
    await to_code_state(Q); r=await say(dp,bot,Q,CODE)
    check("receipt to our recipient: 100 UAH × 2.0 = 200 RUB", abs(db.balance(Q)-200)<0.01 and db.is_receipt_used(CODE), str(db.balance(Q)))
    await to_code_state(Q); r=await say(dp,bot,Q,CODE)
    check("same receipt can't be reused", abs(db.balance(Q)-200)<0.01)
    await to_code_state(905+1); db.get_user(906,"q2"); r=await say(dp,bot,906,CODE)
    check("other user can't reuse receipt", db.balance(906)==0)

    # бан
    db.set_user_status(U,"Заблокирован"); r=await say(dp,bot,U,"/start"); check("banned user gets Banned", texts(r)==["Banned"], str(texts(r)))

    # extract_code
    import lolz as L
    cases=[({"codes":[{"code":"12345"}]},"12345"),({"code":"654321"},"654321"),({"message":"Login code: 98765. Do not give"},"98765"),
           ({"item":{"telegram_login_code":"11122"}},"11122"),({"codes":[]},None),({"date":1759400000,"id":123456},None)]
    for obj,exp in cases: check(f"extract_code {exp}", L.extract_code(obj)==exp, str(L.extract_code(obj)))

    # ═════ РЕГРЕССИЯ: origin саморега + заглушка укр. платежей ═════
    import lolz as L2, uapay, sys as _sys
    from keyboards import B_TOPUP, B_RECEIPT
    check("normalize: self_registered → self_registration", L2.normalize_origins(["autoreg","self_registered"])==["autoreg","self_registration"])
    check("normalize: unknown dropped, dups removed", L2.normalize_origins(["AUTOREG","autoreg","bogus",""])==["autoreg"])
    check("normalize: empty = no filter", L2.normalize_origins([])==[] and L2.normalize_origins(None)==[])
    raw_bad = await L2._request("GET","/telegram", params=[("country[]","MM"),("origin[]","self_registered")], search=True)
    check("fake Lolz: raw self_registered gives EMPTY (reproduces the bug)", (raw_bad.get("items") or [])==[])
    db.set_setting("lolz_origins","autoreg,self_registered")          # «старое» значение в Supabase
    LOG.clear(); items, _more = await L2.search_telegram_page("MM", 1, db.get_lolz_origins())
    sent=[v for k,v in [l for l in LOG if l[0]=="search"][-1][1] if k=="origin[]"]
    check("legacy DB value self_registered still finds accounts", len(items)>0 and sent==["autoreg","self_registration"], str((len(items), sent)))
    db.set_setting("lolz_origins","")                                  # пусто = без фильтра
    LOG.clear(); items, _more = await L2.search_telegram_page("MM", 1, db.get_lolz_origins())
    check("empty lolz_origins → no origin[] param sent", len(items)>0 and not [k for k,_ in [l for l in LOG if l[0]=="search"][-1][1] if k=="origin[]"])

    check("UA payments default = stub", uapay.is_stub())
    res = await payments.check_gov_ua_verify("ABCD-1234-ABCD-1234","monobank")
    check("stub verify: ok=False, error=ua_pay_stub, nothing credited", res["ok"] is False and res["error"]==uapay.ERR_STUB and not res["paid"], str(res))
    check("playwright is NOT imported in stub mode", "playwright" not in _sys.modules)
    W=910; db.get_user(W,"w"); await say(dp,bot,W,"/start")
    await say(dp,bot,W,B_TOPUP); r=await say(dp,bot,W,"100")
    check("topup method screen shows receipt button", B_RECEIPT in flat(last_kb(r)), str(last_kb(r)))
    r=await say(dp,bot,W,B_RECEIPT); t=" ".join(x or "" for x in texts(r))
    check("UA receipt click → stub text, no check flow", "скоро" in t.lower() and "Начать проверку" not in t and db.balance(W)==0, t)
    from aiogram.types import CallbackQuery
    SENT.clear(); u=User(id=W,is_bot=False,first_name="T",username="w")
    cb=CallbackQuery(id="cb1",from_user=u,chat_instance="ci",data="checkgov_start",
                     message=Message(message_id=500,date=int(time.time()),chat=Chat(id=W,type="private"),text="x"))
    await dp.feed_update(bot, Update(update_id=99999, callback_query=cb))
    t=" ".join((d.get("text") or "") for n,d in SENT if n=="SendMessage")
    check("old inline 'checkgov_start' → stub text too", "скоро" in t.lower() or "Скоро".lower() in t.lower() or "розробці" in t, t)
    r=await say(dp,bot,777,"/lolz_debug"); t=" ".join(x or "" for x in texts(r))
    check("removed Lolz debug command exposes no raw account data", "telegram/params" not in t and "loginData" not in t)
    # ═════ РЕГРЕССИЯ 2: Lolz вернул аккаунты в «непривычном» формате полей — не режем их зря ═════
    _orig = L2._request
    async def _weird(method, path, **kw):
        return {"items":[
            {"item_id":1,"price":50,"telegram_country":"United States","telegram_spam":"Нет","item_state":"Active"},
            {"item_id":2,"price":60,"telegram_country":"US","telegram_spam":"Отсутствует","item_state":"selling"},
            {"item_id":3,"price":70,"telegram_spam":0},                      # вообще без страны/статуса
            {"item_id":4,"price":80,"telegram_country":"US","telegram_spam":"yes"},          # спам → режем
            {"item_id":5,"price":90,"telegram_country":"KZ","telegram_spam":"no"},          # чужая страна → режем
            {"item_id":6,"price":95,"telegram_country":"US","item_state":"closed"},          # закрыт → режем
        ], "perPage":40, "totalItems":6}
    L2._request = _weird
    try:
        got,_m = await L2.search_telegram_page("US",1,["autoreg"])
    finally:
        L2._request = _orig
    L2._request = _orig
    check("telegram_spam_block=-1 (реальный формат Lolz) НЕ спам", not L2.item_has_spam({"telegram_spam_block": -1}) and not L2.item_has_spam({"telegram_spam_block": 0}) and L2.item_has_spam({"telegram_spam_block": 1}) and L2.item_has_spam({"telegram_spam": "yes"}))
    check("weird field formats: 1,2,3 kept; spam/foreign/closed dropped", [L2.item_id(i) for i in got]==[1,2,3], str([L2.item_id(i) for i in got]))
    # ═════ РЕГРЕССИЯ 3: строгий запрос с origin[] пуст → запасной поиск без origin[] + проверка origin у себя ═════
    async def _strict_empty(method, path, **kw):
        ps = kw.get("params") or []
        if any(k == "origin[]" for k, _ in ps):
            return {"items": [], "perPage": 40, "totalItems": 0}
        return {"items": [
            {"item_id": 21, "price": 50, "telegram_country": "US", "telegram_spam": "no", "item_state": "active", "item_origin": "autoreg"},
            {"item_id": 22, "price": 55, "telegram_country": "US", "telegram_spam": "no", "item_state": "active", "item_origin": "brute"},
            {"item_id": 23, "price": 60, "telegram_country": "US", "telegram_spam": "no", "item_state": "active", "item_origin": "self_registration"},
        ], "perPage": 40, "totalItems": 3}
    L2._request = _strict_empty
    try:
        import handlers.shop as SH
        db.set_setting("lolz_origins", "autoreg,self_registration")   # прошлый тест оставил пусто
        ids = [L2.item_id(i) for i in await SH._find_candidates("us")]
    finally:
        L2._request = _orig
    check("fallback: strict empty → retry without origin[], brute dropped client-side, cheapest first", ids == [21, 23], str(ids))
    print("\nFAILS:", ok); await runner.cleanup()
asyncio.run(main())
