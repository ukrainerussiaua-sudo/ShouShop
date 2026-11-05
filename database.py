"""
database.py — обёртка над Supabase.
Все методы такие же, как были в SQLite-версии, чтобы не менять хэндлеры.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from supabase import create_client, Client
import json
import time
from config import SUPABASE_URL, SUPABASE_KEY, GUARANTEE_HOURS, MARKUP_PERCENT, LOLZ_ORIGINS


def _kyiv_now() -> datetime:
    """Текущее время по Киеву (сервер хостинга обычно в UTC). Без tzdata — UTC+3/+2 по правилам DST ЕС."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Kyiv"))
    except Exception:
        from datetime import timezone, timedelta
        u = datetime.now(timezone.utc)
        y = u.year
        def last_sun(m):
            d = datetime(y, m, 31, 1, tzinfo=timezone.utc)
            return d - timedelta(days=(d.weekday() + 1) % 7)
        return u + timedelta(hours=3 if last_sun(3) <= u < last_sun(10) else 2)


class Database:
    def __init__(self):
        self.sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    def _fetch_all(self, table: str, columns: str = "*", order_col: str | None = None,
                   desc: bool = False, page: int = 1000) -> list[dict]:
        """Все строки таблицы постранично (Supabase/PostgREST режет ответ на 1000 строк)."""
        out: list[dict] = []
        start = 0
        while True:
            q = self.sb.table(table).select(columns)
            if order_col:
                q = q.order(order_col, desc=desc)
            rows = q.range(start, start + page - 1).execute().data or []
            out.extend(rows)
            if len(rows) < page:
                return out
            start += page

    # ──────────────── USERS ────────────────

    def user_count(self) -> int:
        r = self.sb.table("users").select("user_id", count="exact").execute()
        return r.count or 0

    def get_user(self, uid: int, username: str = "") -> dict:
        r = self.sb.table("users").select("*").eq("user_id", uid).execute()
        if r.data:
            u = r.data[0]
            if username and username != u.get("username", ""):
                self.sb.table("users").update({"username": username}).eq("user_id", uid).execute()
                u["username"] = username
            return u
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        row = {"user_id": uid, "username": username, "balance": 0.0,
               "reg_date": now, "status": "Активен", "referrer_id": 0,
               "purchases_count": 0, "active_discount": 0.0}
        try:
            self.sb.table("users").insert(row).execute()
        except Exception:                       # параллельный /start уже создал строку
            r = self.sb.table("users").select("*").eq("user_id", uid).execute()
            if r.data:
                return r.data[0]
            raise
        return row

    def balance(self, uid: int) -> float:
        r = self.sb.table("users").select("balance").eq("user_id", uid).execute()
        return float(r.data[0]["balance"]) if r.data else 0.0

    def find_user_by_id(self, uid: int) -> dict | None:
        r = self.sb.table("users").select("*").eq("user_id", uid).execute()
        return r.data[0] if r.data else None

    # ──────────────── ЯЗЫК (users.lang: 'ru' | 'uk') ────────────────
    _lang_warned = False

    def get_lang(self, uid: int) -> str:
        try:
            r = self.sb.table("users").select("lang").eq("user_id", uid).execute()
            return (r.data[0].get("lang") or "") if r.data else ""
        except Exception as e:
            if not self._lang_warned:
                Database._lang_warned = True
                print(f"[DB get_lang] {e!r} — выполните ALTER TABLE users ADD COLUMN lang из supabase_schema.sql (без него язык хранится только в памяти)")
            return ""

    def set_lang(self, uid: int, lang: str) -> bool:
        """False — пользователя ещё нет в БД (язык запишет _register) или нет колонки lang."""
        try:
            r = self.sb.table("users").update({"lang": lang}).eq("user_id", uid).execute()
            return bool(r.data)
        except Exception as e:
            if not self._lang_warned:
                Database._lang_warned = True
                print(f"[DB set_lang] {e!r} — выполните ALTER TABLE users ADD COLUMN lang из supabase_schema.sql")
            return False

    def find_user_by_username(self, username: str) -> dict | None:
        r = self.sb.table("users").select("*").ilike("username", username).execute()
        return r.data[0] if r.data else None

    def _cas_balance(self, uid: int, delta: float, require_enough: bool) -> bool:
        """
        Атомарное изменение баланса: UPDATE ... WHERE balance = <то, что прочитали>.
        Если баланс успел измениться (параллельная покупка/пополнение) — перечитываем и повторяем.
        """
        for _ in range(8):
            r = self.sb.table("users").select("balance").eq("user_id", uid).execute()
            if not r.data:
                return False
            cur = float(r.data[0]["balance"] or 0)
            if require_enough and cur + 1e-9 < -delta:
                return False
            new = round(cur + delta, 2)
            upd = (self.sb.table("users").update({"balance": new})
                   .eq("user_id", uid).eq("balance", r.data[0]["balance"]).execute())
            if upd.data:
                return True
        raise RuntimeError(f"balance CAS failed for {uid}")

    def add_balance(self, uid: int, amount: float):
        self._cas_balance(uid, amount, require_enough=False)

    def try_spend(self, uid: int, amount: float) -> bool:
        """Списать amount, только если хватает денег. Атомарно: двойная трата невозможна."""
        if amount <= 0:
            return True
        return self._cas_balance(uid, -amount, require_enough=True)

    _ban_cache: dict = {}

    def is_banned_cached(self, uid: int, ttl: float = 30.0) -> bool:
        """Бан-проверка на КАЖДОЕ обновление — кэшируем на 30 с (сбрасывается при блокировке/разблокировке)."""
        hit = self._ban_cache.get(uid)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        u = self.find_user_by_id(uid)
        banned = bool(u and u.get("status") == "Заблокирован")
        self._ban_cache[uid] = (time.monotonic(), banned)
        return banned

    def set_user_status(self, uid: int, status: str):
        self.sb.table("users").update({"status": status}).eq("user_id", uid).execute()
        self._ban_cache.pop(uid, None)

    def set_active_discount(self, uid: int, amount: float):
        self.sb.table("users").update({"active_discount": amount}).eq("user_id", uid).execute()

    def get_active_discount(self, uid: int) -> float:
        r = self.sb.table("users").select("active_discount").eq("user_id", uid).execute()
        return float(r.data[0]["active_discount"]) if r.data else 0.0

    def set_referrer(self, uid: int, ref_id: int):
        self.sb.table("users").update({"referrer_id": ref_id}).eq("user_id", uid).execute()

    def get_all_user_ids(self) -> list[int]:
        return [row["user_id"] for row in self._fetch_all("users", "user_id", "user_id")]

    def get_all_topups(self, limit: int = 50) -> list[dict]:
        r = (self.sb.table("topup_history")
             .select("*")
             .order("id", desc=True)
             .limit(limit)
             .execute())
        return r.data or []

    def get_all_purchases_log(self, limit: int = 50) -> list[dict]:
        r = (self.sb.table("purchases")
             .select("id,user_id,country_code,phone_number,purchase_date,price_paid")
             .order("id", desc=True)
             .limit(limit)
             .execute())
        return r.data or []

    # ──────────────── PURCHASES ────────────────

    def add_purchase(self, uid: int, country: str, phone: str,
                     logins: str, pwd: str, price_paid: float = 0.0) -> int:
        now  = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        guar = (datetime.now() + timedelta(hours=GUARANTEE_HOURS)).strftime("%d.%m.%Y %H:%M:%S")
        row  = {"user_id": uid, "country_code": country, "phone_number": phone,
                "logins": logins, "password": pwd, "purchase_date": now,
                "guaranteed_until": guar, "price_paid": price_paid,
                "status": "active", "review_sent": 0}
        r = self.sb.table("purchases").insert(row).execute()
        pid = r.data[0]["id"]
        # increment purchases_count
        cnt = (self.find_user_by_id(uid) or {}).get("purchases_count", 0)
        self.sb.table("users").update({"purchases_count": cnt + 1}).eq("user_id", uid).execute()
        return pid

    def get_purchases(self, uid: int) -> list[dict]:
        r = (self.sb.table("purchases")
             .select("*")
             .eq("user_id", uid)
             .eq("status", "active")
             .order("id", desc=True)
             .execute())
        return r.data or []

    def get_purchase(self, pid: int) -> dict | None:
        r = self.sb.table("purchases").select("*").eq("id", pid).execute()
        return r.data[0] if r.data else None

    def mark_review_sent(self, pid: int):
        self.sb.table("purchases").update({"review_sent": 1}).eq("id", pid).execute()


    # ──────────────── REVIEWS ────────────────
    def get_review_for_order(self, order_type: str, order_id: int) -> dict | None:
        try:
            r = (self.sb.table("bot_reviews").select("*")
                 .eq("order_type", order_type).eq("order_id", order_id).limit(1).execute())
            return r.data[0] if r.data else None
        except Exception as e:
            print(f"[DB get_review_for_order] {e!r} — run the bot_reviews migration from supabase_schema.sql")
            raise

    def next_review_number(self) -> int:
        """Следующий номер отзыва: не меньше REVIEW_START_NUMBER (979), дальше +1 к максимальному."""
        from config import REVIEW_START_NUMBER
        r = self.sb.table("bot_reviews").select("id").order("id", desc=True).limit(1).execute()
        top = int(r.data[0]["id"]) if r.data else 0
        return max(int(REVIEW_START_NUMBER), top + 1)

    def add_review(self, order_type: str, order_id: int, uid: int, customer_name: str,
                   product_title: str, price_paid: float, rating: int, comment: str = "",
                   photo_id: str = "") -> dict:
        """Сохраняет отзыв с номером ≥ REVIEW_START_NUMBER. order_type='manual' — отзыв, добавленный админом
        (order_id = сам номер отзыва, чтобы не нарушать UNIQUE(order_type, order_id))."""
        now = _kyiv_now().strftime("%d.%m.%Y / %H:%M")
        last_err = None
        for _ in range(6):
            no = self.next_review_number()
            row = {"id": no, "order_type": order_type,
                   "order_id": no if order_type == "manual" else order_id, "user_id": uid,
                   "customer_name": customer_name[:120], "product_title": product_title[:200],
                   "price_paid": round(float(price_paid), 2), "rating": int(rating),
                   "comment": comment[:1000], "created_at": now, "posted": False}
            if photo_id:
                row["photo_id"] = photo_id
            try:
                r = self.sb.table("bot_reviews").insert(row).execute()
                return r.data[0]
            except Exception as e:
                last_err = e
                msg = str(e)
                if photo_id and "photo_id" in msg:       # колонки photo_id ещё нет — сохраняем без неё
                    photo_id = ""
                    continue
                if "duplicate" in msg.lower() or "23505" in msg:
                    if order_type != "manual" and "order_type" in msg:
                        raise                              # отзыв на эту покупку уже есть
                    continue                               # гонка за номер — берём следующий
                raise
        raise last_err

    def mark_review_posted(self, review_id: int, channel_message_id: int | None = None):
        data = {"posted": True}
        if channel_message_id is not None:
            data["channel_message_id"] = int(channel_message_id)
        self.sb.table("bot_reviews").update(data).eq("id", review_id).execute()

    # ──────────────── SUPPORT TICKETS ────────────────
    def create_ticket(self, uid: int, username: str, customer_name: str, first_message: str) -> int:
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        r = self.sb.table("support_tickets").insert({
            "user_id": uid, "username": username[:100], "customer_name": customer_name[:120],
            "status": "open", "created_at": now, "updated_at": now,
        }).execute()
        tid = r.data[0]["id"]
        self.add_ticket_message(tid, "user", uid, first_message, update_status=False)
        return tid

    def get_ticket(self, ticket_id: int) -> dict | None:
        r = self.sb.table("support_tickets").select("*").eq("id", ticket_id).limit(1).execute()
        return r.data[0] if r.data else None

    def add_ticket_message(self, ticket_id: int, sender_type: str, sender_id: int,
                           message: str, update_status: bool = True):
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self.sb.table("support_ticket_messages").insert({
            "ticket_id": ticket_id, "sender_type": sender_type, "sender_id": sender_id,
            "message": message[:4000], "created_at": now,
        }).execute()
        if update_status:
            status = "answered" if sender_type == "admin" else "open"
            self.sb.table("support_tickets").update({"status": status, "updated_at": now}).eq("id", ticket_id).execute()

    def set_ticket_status(self, ticket_id: int, status: str):
        self.sb.table("support_tickets").update({
            "status": status, "updated_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        }).eq("id", ticket_id).execute()

    def get_user_purchases_all(self, uid: int) -> list[dict]:
        r = (self.sb.table("purchases")
             .select("*")
             .eq("user_id", uid)
             .order("id", desc=True)
             .execute())
        return r.data or []

    def get_user_turnover(self, uid: int) -> dict:
        """Оборот клиента: сколько потратил на покупки и сколько пополнил всего (все способы)."""
        purchases = (self.sb.table("purchases").select("price_paid")
                     .eq("user_id", uid).execute().data or [])
        topups = (self.sb.table("topup_history").select("amount")
                  .eq("user_id", uid).execute().data or [])
        spent = sum(float(p.get("price_paid") or 0) for p in purchases)
        topped_up = sum(float(t.get("amount") or 0) for t in topups)
        return {"spent": spent, "topped_up": topped_up, "purchases_count": len(purchases)}

    # ──────────────── PROMO CODES ────────────────

    def get_promo(self, code: str) -> dict | None:
        r = (self.sb.table("promo_codes")
             .select("*")
             .eq("code", code)
             .eq("active", 1)
             .execute())
        if not r.data:
            return None
        promo = r.data[0]
        if promo.get("uses", 0) >= promo.get("max_uses", 100):
            return None
        if promo.get("expires_at"):
            try:
                if datetime.now() > datetime.strptime(promo["expires_at"], "%d.%m.%Y %H:%M:%S"):
                    return None
            except ValueError:
                pass
        return promo

    def has_used_promo(self, code: str, uid: int) -> bool:
        r = (self.sb.table("promo_uses")
             .select("code")
             .eq("code", code)
             .eq("user_id", uid)
             .execute())
        return bool(r.data)

    def use_promo(self, code: str, uid: int) -> bool:
        """
        Атомарно «занимает» промокод для пользователя (PK code+user_id) и увеличивает uses
        с проверкой лимита. True — можно выдавать бонус; False — уже использован / лимит исчерпан.
        """
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        try:
            self.sb.table("promo_uses").insert({"code": code, "user_id": uid, "used_at": now}).execute()
        except Exception:
            return False                                   # этот юзер уже использовал код
        for _ in range(8):
            promo = self.get_promo(code)                   # проверяет active / лимит / срок
            if not promo:
                break
            r = (self.sb.table("promo_codes").update({"uses": promo["uses"] + 1})
                 .eq("code", code).eq("uses", promo["uses"]).execute())
            if r.data:
                return True
        self.sb.table("promo_uses").delete().eq("code", code).eq("user_id", uid).execute()
        return False

    def add_promo(self, code: str, discount: float, mx: int = 100,
                  ptype: str = "balance", country_code: str | None = None,
                  expires_days: int = 0, once_per_user: int = 1):
        expires_at = None
        if expires_days and int(expires_days) > 0:
            expires_at = (datetime.now() + timedelta(days=int(expires_days))).strftime("%d.%m.%Y %H:%M:%S")
        row = {"code": code, "discount": discount, "max_uses": mx, "uses": 0,
               "active": 1, "type": ptype, "country_code": country_code,
               "expires_at": expires_at, "once_per_user": once_per_user}
        self.sb.table("promo_codes").upsert(row).execute()

    def list_promos(self) -> list[dict]:
        r = self.sb.table("promo_codes").select("*").order("uses", desc=True).execute()
        return r.data or []

    # ──────────────── PENDING PAYMENTS ────────────────

    def add_pending(self, pid: str, uid: int, amount: float, method: str = "", amount_uah: float = 0):
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self.sb.table("pending_payments").upsert({
            "payment_id": pid, "user_id": uid, "amount": amount, "amount_uah": amount_uah,
            "status": "pending", "method": method, "created_at": now
        }).execute()

    def get_pending(self, pid: str) -> dict | None:
        r = self.sb.table("pending_payments").select("*").eq("payment_id", pid).execute()
        return r.data[0] if r.data else None

    def update_pending_status(self, pid: str, status: str):
        self.sb.table("pending_payments").update({"status": status}).eq("payment_id", pid).execute()

    def claim_pending(self, pid: str, new_status: str) -> bool:
        """pending → new_status ровно один раз (защита от двойного зачисления). True — «выиграли» мы."""
        r = (self.sb.table("pending_payments").update({"status": new_status})
             .eq("payment_id", pid).eq("status", "pending").execute())
        return bool(r.data)

    def claim_pending_from(self, pid: str, old_status: str, new_status: str) -> bool:
        """old_status → new_status ровно один раз (карта: review → paid / rejected)."""
        r = (self.sb.table("pending_payments").update({"status": new_status})
             .eq("payment_id", pid).eq("status", old_status).execute())
        return bool(r.data)

    def list_open_pending(self) -> list[dict]:
        r = self.sb.table("pending_payments").select("*").eq("status", "pending").execute()
        return r.data or []

    # ──────────────── TOPUP HISTORY ────────────────

    def add_topup(self, uid: int, amount: float, method: str):
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self.sb.table("topup_history").insert({
            "user_id": uid, "amount": amount, "method": method, "created_at": now
        }).execute()

    def get_recent_topups(self, limit: int = 15) -> list[dict]:
        try:
            return (self.sb.table("topup_history").select("*").order("id", desc=True).limit(limit).execute().data or [])
        except Exception as e:
            print(f"[DB get_recent_topups] {e!r}")
            return []

    def get_topup_history(self, uid: int, limit: int = 15) -> list[dict]:
        r = (self.sb.table("topup_history")
             .select("*")
             .eq("user_id", uid)
             .order("id", desc=True)
             .limit(limit)
             .execute())
        return r.data or []

    # ──────────────── REPLACEMENT REQUESTS ────────────────

    def add_replacement_request(self, uid: int, purchase_id: int, account_number: str,
                                 issue_text: str, issued_at: str, video_file_id: str) -> int:
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        r = self.sb.table("replacement_requests").insert({
            "user_id": uid, "purchase_id": purchase_id,
            "account_number": account_number, "issue_text": issue_text,
            "issued_at": issued_at, "video_file_id": video_file_id,
            "status": "pending", "created_at": now
        }).execute()
        return r.data[0]["id"]

    def get_replacement_request(self, rid: int) -> dict | None:
        r = self.sb.table("replacement_requests").select("*").eq("id", rid).execute()
        return r.data[0] if r.data else None

    def list_pending_replacements(self) -> list[dict]:
        r = (self.sb.table("replacement_requests")
             .select("*")
             .eq("status", "pending")
             .order("id", desc=True)
             .limit(20)
             .execute())
        return r.data or []

    def get_replacements_for_purchase(self, purchase_id: int) -> list[dict]:
        r = self.sb.table("replacement_requests").select("*").eq("purchase_id", purchase_id).execute()
        return r.data or []

    def claim_replacement(self, rid: int, status: str) -> bool:
        """pending → approved/rejected ровно один раз."""
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        r = (self.sb.table("replacement_requests").update({"status": status, "resolved_at": now})
             .eq("id", rid).eq("status", "pending").execute())
        return bool(r.data)

    def set_purchase_status(self, pid: int, status: str):
        self.sb.table("purchases").update({"status": status}).eq("id", pid).execute()

    def resolve_replacement_request(self, rid: int, status: str):
        now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self.sb.table("replacement_requests").update(
            {"status": status, "resolved_at": now}
        ).eq("id", rid).execute()

    # ──────────────────────────────────────────
    # BOT SETTINGS
    # ──────────────────────────────────────────
    _SET_TTL = 15.0
    _set_cache: dict = {}

    def get_setting(self, key: str, default: str = "") -> str:
        """Настройка из bot_settings. Кэш 15 с — каждое нажатие больше не ходит в БД за ценами/курсами."""
        hit = self._set_cache.get(key)
        if hit and time.monotonic() - hit[0] < self._SET_TTL:
            return hit[1] if hit[1] is not None else default
        try:
            r = self.sb.table("bot_settings").select("value").eq("key", key).execute()
            val = r.data[0]["value"] if r.data else None
        except Exception:
            return default
        self._set_cache[key] = (time.monotonic(), val)
        return val if val is not None else default

    def set_setting(self, key: str, value: str):
        try:
            self.sb.table("bot_settings").upsert({"key": key, "value": value}).execute()
            self._set_cache[key] = (time.monotonic(), value)
        except Exception as e:
            print(f"[DB set_setting] {e}")

    # ──────────────────────────────────────────
    # USED RECEIPTS — захист від повторного використання
    # ──────────────────────────────────────────
    def is_receipt_used(self, code: str) -> bool:
        try:
            r = self.sb.table("used_receipts").select("code").eq("code", code).execute()
            return bool(r.data)
        except Exception:
            return False

    def claim_receipt(self, code: str, user_id: int, amount: float = 0) -> bool:
        """Атомарно помечает квитанцию использованной (PK code). False — уже была использована."""
        try:
            self.sb.table("used_receipts").insert({
                "code": code, "user_id": user_id, "amount": amount,
                "used_at": datetime.now().isoformat()}).execute()
            return True
        except Exception:
            return False

    def mark_receipt_used(self, code: str, user_id: int, amount: float = 0):
        from datetime import datetime
        try:
            self.sb.table("used_receipts").upsert({
                "code": code,
                "user_id": user_id,
                "amount": amount,
                "used_at": datetime.now().isoformat(),
            }).execute()
        except Exception as e:
            print(f"[DB mark_receipt_used] {e}")

    def get_recent_receipts(self, limit: int = 20) -> list[dict]:
        try:
            r = (self.sb.table("used_receipts")
                 .select("*")
                 .order("used_at", desc=True)
                 .limit(limit)
                 .execute())
            return r.data or []
        except Exception:
            return []

    # ──────────────────────────────────────────
    # USED MONO TX — защита от повторного зачисления одной транзакции Monobank
    # ──────────────────────────────────────────
    def is_mono_tx_used(self, tx_id: str) -> bool:
        try:
            r = self.sb.table("used_mono_tx").select("tx_id").eq("tx_id", tx_id).execute()
            return bool(r.data)
        except Exception:
            return False

    def claim_mono_tx(self, tx_id: str, payment_id: str, amount_uah: float = 0) -> bool:
        """Атомарно помечает транзакцию Monobank использованной (PK tx_id). False — уже была занята."""
        try:
            self.sb.table("used_mono_tx").insert({
                "tx_id": tx_id, "payment_id": payment_id, "amount_uah": amount_uah,
                "used_at": datetime.now().isoformat()}).execute()
            return True
        except Exception:
            return False

    # ──────────────── НАЦЕНКА / СТАТИСТИКА ────────────────

    def get_markup(self) -> float:   # устарело: цены теперь задаются по странам (get_country_prices)
        try:
            return float(self.get_setting("markup_percent", str(MARKUP_PERCENT)))
        except ValueError:
            return MARKUP_PERCENT

    def set_markup(self, percent: float):
        self.set_setting("markup_percent", str(percent))

    def get_purchase_by_lolz(self, lolz_item_id: int) -> dict | None:
        r = (self.sb.table("purchases").select("*")
             .eq("logins", f"lolz:{lolz_item_id}").limit(1).execute())
        return r.data[0] if r.data else None

    # ──────────────── СТРАНЫ / ПРОИСХОЖДЕНИЕ ────────────────

    def get_shop_countries(self) -> list[dict]:
        """Все строки shop_countries (и скрытые тоже — нужны для старых покупок). [] при ошибке."""
        try:
            r = self.sb.table("shop_countries").select("*").order("sort_order").execute()
            return r.data or []
        except Exception as e:
            print(f"[DB get_shop_countries] {e}")
            return []

    # Цены стран: bot_settings, ключ "price_<ключ страны>" (например price_us = 49).
    # Лежат в уже существующей таблице → менять схему Supabase не нужно.
    PRICE_PREFIX = "price_"

    def get_country_prices(self) -> dict[str, int]:
        """{ключ страны: цена в ₴}. Нулевые / битые значения пропускаются."""
        try:
            r = self.sb.table("bot_settings").select("key,value").execute()
        except Exception as e:
            print(f"[DB get_country_prices] {e}")
            return {}
        out: dict[str, int] = {}
        for row in r.data or []:
            k = str(row.get("key") or "")
            if not k.startswith(self.PRICE_PREFIX):
                continue
            try:
                v = int(float(row.get("value") or 0))
            except ValueError:
                continue
            if v > 0:
                out[k[len(self.PRICE_PREFIX):]] = v
        return out

    def set_country_price(self, key: str, price: int):
        """price <= 0 — товар скрыт от клиентов."""
        self.set_setting(self.PRICE_PREFIX + key, str(int(price)))

    # ──────────────── FRAGMENT: звёзды / Premium ────────────────
    # Цены лежат в bot_settings: stars_price (₴ за 1 звезду, дробное), premium_3/6/12 (₴). 0 = товар выключен.
    def get_stars_price(self) -> float:
        try:
            return max(0.0, float(self.get_setting("stars_price", "0") or 0))
        except ValueError:
            return 0.0

    def get_premium_price(self, months: int) -> int:
        try:
            return max(0, int(float(self.get_setting(f"premium_{months}", "0") or 0)))
        except ValueError:
            return 0

    def add_fragment_order(self, uid: int, kind: str, qty: int, target: str, price: float) -> int:
        row = {"user_id": uid, "kind": kind, "quantity": qty, "target": target, "price_paid": price,
               "status": "created", "note": "", "created_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S")}
        r = self.sb.table("fragment_orders").insert(row).execute()
        return r.data[0]["id"]

    def set_fragment_order(self, oid: int, status: str, note: str = ""):
        try:
            self.sb.table("fragment_orders").update({"status": status, "note": note[:500]}).eq("id", oid).execute()
        except Exception as e:
            print(f"[DB set_fragment_order] {e}")

    def get_fragment_orders(self, uid: int, limit: int = 10) -> list[dict]:
        r = (self.sb.table("fragment_orders").select("*").eq("user_id", uid)
             .order("id", desc=True).limit(limit).execute())
        return r.data or []


    # ──────────────── NFT-ПОДАРКИ ────────────────
    def nft_add(self, slug: str, title: str, price: float, cost: float = 0.0, owned_gift_id: str = "",
                status: str = "available") -> dict:
        row = {"slug": slug, "title": title[:120], "link": f"https://t.me/nft/{slug}", "price": round(price, 2),
               "cost": round(cost, 2), "owned_gift_id": owned_gift_id or "", "status": status,
               "created_at": _kyiv_now().strftime("%d.%m.%Y %H:%M")}
        return self.sb.table("nft_gifts").insert(row).execute().data[0]

    def nft_get(self, nid: int) -> dict | None:
        r = self.sb.table("nft_gifts").select("*").eq("id", nid).execute()
        return r.data[0] if r.data else None

    def nft_by_slug(self, slug: str) -> dict | None:
        r = self.sb.table("nft_gifts").select("*").ilike("slug", slug).execute()
        return r.data[0] if r.data else None

    def nft_list(self, statuses: list[str] | None = None) -> list[dict]:
        q = self.sb.table("nft_gifts").select("*")
        if statuses:
            q = q.in_("status", statuses)
        return q.order("id", desc=True).limit(500).execute().data or []

    def nft_update(self, nid: int, **fields) -> None:
        self.sb.table("nft_gifts").update(fields).eq("id", nid).execute()

    def nft_delete(self, nid: int) -> None:
        self.sb.table("nft_gifts").delete().eq("id", nid).execute()

    def nft_reserve(self, nid: int, uid: int) -> bool:
        """Атомарно: резервируем подарок, только если он ещё в продаже (двое не купят один)."""
        r = (self.sb.table("nft_gifts")
             .update({"status": "reserved", "buyer_id": uid, "reserved_at": _kyiv_now().strftime("%d.%m.%Y %H:%M:%S")})
             .eq("id", nid).eq("status", "available").select("id").execute())
        return bool(r.data)

    def nft_update_if_status(self, nid: int, expected_status: str, **fields) -> bool:
        """Атомарное изменение статуса NFT — защита от повторных нажатий админских кнопок."""
        r = (self.sb.table("nft_gifts").update(fields)
             .eq("id", nid).eq("status", expected_status).select("id").execute())
        return bool(r.data)

    # ──────────────── НАСТРОЙКИ: числа ────────────────
    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(str(self.get_setting(key, "") or "").replace(",", ".") or default)
        except ValueError:
            return default


    # ──────────────── ЗАКАЗЫ С ОПЛАТОЙ ПО ЧЕКУ (shop_orders) ────────────────
    def order_add(self, uid: int, kind: str, title: str, qty: float, target: str,
                  amount: float, cost: float = 0.0) -> int:
        row = {"user_id": uid, "kind": kind, "title": title[:200], "qty": qty, "target": target[:200],
               "amount": round(amount, 2), "cost": round(cost, 2), "status": "draft",
               "created_ts": int(time.time())}
        return self.sb.table("shop_orders").insert(row).execute().data[0]["id"]

    def order_get(self, oid: int) -> dict | None:
        r = self.sb.table("shop_orders").select("*").eq("id", oid).execute()
        return r.data[0] if r.data else None

    def order_claim(self, oid: int, old: str | tuple, new: str, **fields) -> bool:
        """Атомарно меняет статус заказа (защита от двойного нажатия / двух админов)."""
        olds = [old] if isinstance(old, str) else list(old)
        q = self.sb.table("shop_orders").update({"status": new, **fields}).eq("id", oid).in_("status", olds)
        return bool(q.execute().data)

    def order_update(self, oid: int, **fields) -> None:
        self.sb.table("shop_orders").update(fields).eq("id", oid).execute()

    def orders_open(self, limit: int = 20) -> list[dict]:
        try:
            return (self.sb.table("shop_orders").select("*").in_("status", ["review", "paid_manual"])
                    .order("id", desc=True).limit(limit).execute().data or [])
        except Exception:
            return []

    def user_stats(self, uid: int) -> dict:
        """Профиль: выполнено заказов, потрачено, звёзд, месяцев Premium, NFT."""
        out = {"orders": 0, "spent": 0.0, "stars": 0, "months": 0, "nft": 0}
        try:
            rows = self.sb.table("sales").select("kind,qty,price").eq("user_id", uid).execute().data or []
        except Exception:
            return out
        for r in rows:
            out["orders"] += 1
            out["spent"] += float(r.get("price") or 0)
            k, q = r.get("kind"), float(r.get("qty") or 0)
            if k == "stars":
                out["stars"] += int(q)
            elif k == "premium":
                out["months"] += int(q)
            elif k == "nft":
                out["nft"] += 1
        return out

    # ──────────────── БОНУС: спины и бонусные звёзды ────────────────
    def bonus_get(self, uid: int) -> dict:
        try:
            v = json.loads(self.get_setting(f"bonus:{uid}", "") or "{}")
        except ValueError:
            v = {}
        return {"spins": int(v.get("spins", 0)), "stars": int(v.get("stars", 0))}

    def bonus_set(self, uid: int, spins: int, stars: int) -> None:
        self.set_setting(f"bonus:{uid}", json.dumps({"spins": max(0, spins), "stars": max(0, stars)}))

    # ──────────────── ЖУРНАЛ ПРОДАЖ (sales) ────────────────
    # Одна строка на каждую УСПЕШНУЮ продажу: аккаунт / stars / premium / ton.
    # price — сколько заплатил клиент (грязная выручка), cost — себестоимость, ref_bonus — реферальная выплата.
    # Чистая прибыль = price − cost − ref_bonus.
    def add_sale(self, uid: int, kind: str, title: str, qty: float, price: float,
                 cost: float = 0.0, ref_bonus: float = 0.0) -> int | None:
        now = datetime.now()
        row = {"user_id": uid, "kind": kind, "title": title[:200], "qty": qty, "price": round(price, 2),
               "cost": round(cost, 2), "ref_bonus": round(ref_bonus, 2),
               "created_at": now.strftime("%d.%m.%Y %H:%M:%S"), "created_ts": int(now.timestamp())}
        try:
            return self.sb.table("sales").insert(row).execute().data[0]["id"]
        except Exception as e:
            print(f"[DB add_sale] {e!r} — выполните блок «14. ЖУРНАЛ ПРОДАЖ» из supabase_schema.sql")
            return None

    def get_sales(self, limit: int = 15) -> list[dict]:
        try:
            return (self.sb.table("sales").select("*").order("id", desc=True).limit(limit).execute().data or [])
        except Exception as e:
            print(f"[DB get_sales] {e!r}")
            return []

    def all_sales(self) -> list[dict]:
        try:
            return self._fetch_all("sales", "*", "id")
        except Exception as e:
            print(f"[DB all_sales] {e!r}")
            return []

    def stats(self) -> dict:
        """Данные для админской статистики: периоды, товары, пополнения, баланс клиентов."""
        now = datetime.now()
        day0 = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        bounds = {"today": day0, "week": int(time.time()) - 7 * 86400,
                  "month": int(time.time()) - 30 * 86400, "all": 0}
        sales = self.all_sales()

        def agg(rows):
            g = sum(float(r.get("price") or 0) for r in rows)
            c = sum(float(r.get("cost") or 0) for r in rows)
            rb = sum(float(r.get("ref_bonus") or 0) for r in rows)
            return {"count": len(rows), "gross": g, "cost": c, "ref": rb, "net": g - c - rb}

        periods = {k: agg([r for r in sales if int(r.get("created_ts") or 0) >= ts]) for k, ts in bounds.items()}
        by_kind = {k: agg([r for r in sales if r.get("kind") == k]) for k in ("account", "stars", "premium", "ton", "nft")}

        users = self._fetch_all("users", "user_id,balance,reg_date")
        new_today = 0
        for u in users:
            try:
                if datetime.strptime(u.get("reg_date") or "", "%d.%m.%Y %H:%M:%S").date() == now.date():
                    new_today += 1
            except ValueError:
                pass
        topups = self._fetch_all("topup_history", "amount,method")
        by_method: dict[str, list[float]] = {}
        for t in topups:
            m = by_method.setdefault(t.get("method") or "?", [0, 0.0])
            m[0] += 1
            m[1] += float(t.get("amount") or 0)
        return {
            "users": len(users), "new_today": new_today,
            "balances": sum(float(u.get("balance") or 0) for u in users),
            "periods": periods, "by_kind": by_kind, "topups": by_method,
            "gold_open": len(self.list_gold_requests(("new", "in_work"), 500)),
        }

    # ──────────────── ЗАЯВКИ НА ГОЛДУ ────────────────
    def add_gold_request(self, uid: int, username: str, kind: str, amount: float, comment: str) -> int:
        row = {"user_id": uid, "username": username or "", "kind": kind, "amount": amount,
               "comment": comment or "", "status": "new", "admin_id": 0,
               "created_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S")}
        return self.sb.table("gold_requests").insert(row).execute().data[0]["id"]

    def get_gold_request(self, rid: int) -> dict | None:
        r = self.sb.table("gold_requests").select("*").eq("id", rid).execute()
        return r.data[0] if r.data else None

    def claim_gold_status(self, rid: int, from_statuses: tuple, new_status: str, admin_id: int) -> bool:
        """Атомарная смена статуса: меняется, только если сейчас статус из from_statuses."""
        for st in from_statuses:
            r = (self.sb.table("gold_requests").update({"status": new_status, "admin_id": admin_id})
                 .eq("id", rid).eq("status", st).execute())
            if r.data:
                return True
        return False

    def list_gold_requests(self, statuses: tuple = ("new", "in_work"), limit: int = 15) -> list[dict]:
        out: list[dict] = []
        try:
            for st in statuses:
                out += (self.sb.table("gold_requests").select("*").eq("status", st)
                        .order("id", desc=True).limit(limit).execute().data or [])
        except Exception as e:
            print(f"[DB list_gold_requests] {e!r}")
        return sorted(out, key=lambda r: r["id"], reverse=True)[:limit]

    def count_open_gold(self, uid: int) -> int:
        try:
            n = 0
            for st in ("new", "in_work"):
                n += len(self.sb.table("gold_requests").select("id").eq("user_id", uid).eq("status", st).execute().data or [])
            return n
        except Exception:
            return 0

    # ──────────────── ПОКУПКА TON (клиент покупает TON за баланс) ────────────────
    def add_ton_order(self, uid: int, address: str, amount_ton: float, price: float) -> int:
        row = {"user_id": uid, "address": address, "amount_ton": amount_ton, "price_paid": price,
               "status": "created", "note": "", "created_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S")}
        return self.sb.table("ton_orders").insert(row).execute().data[0]["id"]

    def set_ton_order(self, oid: int, status: str, note: str = ""):
        try:
            self.sb.table("ton_orders").update({"status": status, "note": note[:500]}).eq("id", oid).execute()
        except Exception as e:
            print(f"[DB set_ton_order] {e}")


    def get_ton_order(self, oid: int) -> dict | None:
        r = self.sb.table("ton_orders").select("*").eq("id", oid).limit(1).execute()
        return r.data[0] if r.data else None

    def get_fragment_order(self, oid: int) -> dict | None:
        r = self.sb.table("fragment_orders").select("*").eq("id", oid).limit(1).execute()
        return r.data[0] if r.data else None

    # ──────────────── TON-ДЕПОЗИТЫ (пополнение баланса через TON) ────────────────
    # PK = tx_hash → одна и та же транзакция не может быть зачислена дважды (ни одному, ни разным людям).
    def claim_ton_tx(self, tx_hash: str, uid: int, amount_ton: float, amount_uah: float,
                     rate: float, status: str = "claimed", sender: str = "", comment: str = "") -> bool:
        try:
            self.sb.table("ton_deposits").insert({
                "tx_hash": tx_hash, "user_id": uid, "amount_ton": amount_ton, "amount_uah": amount_uah,
                "rate": rate, "status": status, "sender": sender, "comment": comment[:200],
                "created_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S")}).execute()
            return True
        except Exception:
            return False

    def set_ton_tx_status(self, tx_hash: str, status: str):
        try:
            self.sb.table("ton_deposits").update({"status": status}).eq("tx_hash", tx_hash).execute()
        except Exception as e:
            print(f"[DB set_ton_tx_status] {e}")

    def seen_ton_txs(self, hashes: list[str]) -> set[str]:
        if not hashes:
            return set()
        try:
            r = self.sb.table("ton_deposits").select("tx_hash").in_("tx_hash", hashes).execute()
            return {row["tx_hash"] for row in (r.data or [])}
        except Exception as e:
            print(f"[DB seen_ton_txs] {e!r}")
            raise

    def get_ton_deposits(self, uid: int, limit: int = 3) -> list[dict]:
        try:
            return (self.sb.table("ton_deposits").select("*").eq("user_id", uid)
                    .eq("status", "credited").order("seq", desc=True).limit(limit).execute().data or [])
        except Exception:
            return []

    def list_unmatched_ton(self, limit: int = 15) -> list[dict]:
        try:
            return (self.sb.table("ton_deposits").select("*").eq("status", "unmatched")
                    .order("seq", desc=True).limit(limit).execute().data or [])
        except Exception:
            return []

    def get_lolz_origins(self) -> list[str]:
        """Происхождение для фильтра Lolz. Пустая строка в bot_settings = без фильтра."""
        raw = self.get_setting("lolz_origins", ",".join(LOLZ_ORIGINS))
        return [x.strip() for x in raw.split(",") if x.strip()]


db = Database()
