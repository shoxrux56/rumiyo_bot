import asyncio, html, io, json, os, random, re, shutil, sqlite3, time
from datetime import datetime, time as dtime, timedelta, timezone
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot, Dispatcher, F
from dotenv import load_dotenv
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats, BufferedInputFile, CallbackQuery, ChatMemberUpdated, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)

load_dotenv()

GOALS = {"ish": "💼 Ishlash", "imtihon": "📝 Imtihon", "kino": "🎬 Kino/musiqa",
         "sayohat": "✈️ Sayohat", "boshqa": "🌱 Boshqa"}

DB_PATH = os.getenv("DB_PATH", "data.db")
if DB_PATH != ":memory:":
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
db = sqlite3.connect(DB_PATH)


def tune_db():
    """Ko'p foydalanuvchi uchun: yozish o'qishni to'xtatmaydi (WAL), disk kamroq kutiladi."""
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.Error as e:
        print("PRAGMA o'rnatilmadi:", e)


tune_db()


def migrate():
    """Jadvallarni yaratadi / yangi ustunlarni qo'shadi (zaxiradan tiklangandan keyin ham chaqiriladi)."""
    tune_db()
    db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, goal TEXT, level TEXT)")
    try:  # eski bazada level ustuni bo'lmasa qo'shiladi
        db.execute("ALTER TABLE users ADD COLUMN level TEXT")
    except sqlite3.OperationalError:
        pass
    for col in ("next_at INTEGER DEFAULT 0", "notified INTEGER DEFAULT 1", "paid INTEGER DEFAULT 0", "paid_until INTEGER DEFAULT 0"):  # keyingi dars ochilish vaqti
        try:
            db.execute("ALTER TABLE users ADD COLUMN " + col)
        except sqlite3.OperationalError:
            pass
    for col in ("lang TEXT", "alt_goal TEXT", "alt_level TEXT", "alt_next_at INTEGER DEFAULT 0"):  # til: ru/en; boshqa tilning saqlangan holati
        try:
            db.execute("ALTER TABLE users ADD COLUMN " + col)
        except sqlite3.OperationalError:
            pass
    db.execute("CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT NOT NULL, url TEXT NOT NULL)")
    for col in ("test_url TEXT", "topic TEXT", "ord REAL", "lang TEXT DEFAULT 'ru'"):  # eski bazada test_url / topic ustuni bo'lmasa qo'shiladi
        try:
            db.execute("ALTER TABLE lessons ADD COLUMN " + col)
        except sqlite3.OperationalError:
            pass
    db.execute("CREATE TABLE IF NOT EXISTS pay_wait(user_id INTEGER PRIMARY KEY, plan TEXT)")  # bot qayta ishga tushsa ham «chek kutilmoqda» holati yo'qolmasin
    db.execute("CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, "
               "file_id TEXT, kind TEXT, status TEXT DEFAULT 'pending')")
    db.execute("CREATE TABLE IF NOT EXISTS progress(user_id INTEGER, lesson_id INTEGER, PRIMARY KEY(user_id, lesson_id))")
    try:  # A0 joylashtirishda o'tkazib yuborilgan darslar (bepul dars hisobiga kirmaydi)
        db.execute("ALTER TABLE progress ADD COLUMN skipped INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    db.execute("CREATE TABLE IF NOT EXISTS talk_use(user_id INTEGER, day TEXT, secs INTEGER DEFAULT 0, PRIMARY KEY(user_id, day))")
    db.execute("CREATE TABLE IF NOT EXISTS referrals(invited INTEGER PRIMARY KEY, inviter INTEGER, ok INTEGER DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS payouts(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, card TEXT, "
               "maxid INTEGER, amount INTEGER, status TEXT DEFAULT 'pending', ts INTEGER)")
    for _c in ("amount INTEGER DEFAULT 0", "plan TEXT DEFAULT 'L'", "uniq TEXT"):
        try:
            db.execute("ALTER TABLE payments ADD COLUMN " + _c)
        except sqlite3.OperationalError:
            pass
    db.execute("CREATE TABLE IF NOT EXISTS earnings(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, from_user INTEGER, "
               "amount INTEGER, ts INTEGER, paid INTEGER DEFAULT 0, kind TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, disc INTEGER DEFAULT 0, comm INTEGER DEFAULT 0, "
               "owner INTEGER DEFAULT 0, uses INTEGER DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS user_promo(user_id INTEGER PRIMARY KEY, code TEXT)")
    try:
        db.execute("ALTER TABLE referrals ADD COLUMN ts INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # Rumiyo AI ni o'qitish: "odam shu desa -> Rumiyo shu desin" juftliklari (asosiy nusxa shu yerda, zaxira kanaliga ham ketadi)
    db.execute("CREATE TABLE IF NOT EXISTS rm_pairs(id INTEGER PRIMARY KEY AUTOINCREMENT, q TEXT NOT NULL, a TEXT NOT NULL, "
               "tone INTEGER DEFAULT 1, ts INTEGER DEFAULT 0, UNIQUE(q, a))")
    db.commit()


migrate()


def meta_get(k):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def meta_set(k, v):
    db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, v))
    db.commit()


LEVELS = ["A0", "A1", "A2", "B1", "B2", "C1"]
NAMES = {"v": "lug'at boyligi", "g": "grammatika"}
LESSON_LEVELS = list(LEVELS)  # A0 ham dars darajasi
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
UNLOCK_HOUR = int(os.getenv("UNLOCK_HOUR", "12"))  # keyingi dars shu soatda ochiladi (Toshkent vaqti)
TZ = timezone(timedelta(hours=5))
FREE_LESSONS = int(os.getenv("FREE_LESSONS", "1"))  # boshidagi bepul darslar soni
PRICE = os.getenv("PRICE", "").strip()  # masalan: 199 000 so'm
CARD = os.getenv("CARD", "").strip()    # to'lov uchun karta raqami va egasi
def first_free():
    return "Birinchi dars" if FREE_LESSONS == 1 else f"Birinchi {FREE_LESSONS} dars"


def free_done():
    return "Bepul dars" if FREE_LESSONS == 1 else f"{FREE_LESSONS} ta bepul dars"


PAID_MODE = bool(PRICE and CARD)        # PRICE va CARD yozilmasa, hamma dars bepul
PASS = 0.7  # testdan o'tish chegarasi (to'g'ri javoblar ulushi)
pending = {}  # admin_id -> ("add", daraja) | ("add2", daraja, dars_linki) | ("edit", dars_id, "url"|"test_url")

WEBAPP_URL = os.getenv("WEBAPP_URL")
if not WEBAPP_URL:
    raise SystemExit(".env faylida WEBAPP_URL topilmadi (GitHub Pages manzili)")
EN_WEBAPP_URL = os.getenv("EN_WEBAPP_URL", "https://shoxrux56.github.io/trening_ingilish/test/").strip()  # ingliz daraja testi
LANG_NAMES = {"ru": "🇷🇺 Rus tili", "en": "🇬🇧 Ingliz tili"}
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit(".env faylida BOT_TOKEN topilmadi (.env.example ni .env ga nusxalang)")
_sess = AiohttpSession(timeout=60, proxy=os.getenv("PROXY") or None)  # PROXY=http://127.0.0.1:port (ixtiyoriy)
if os.getenv("FORCE_IPV4", "1") == "1":  # buzuq IPv6 sabab ulanish uzilishini oldini oladi
    import socket
    _sess._connector_init["family"] = socket.AF_INET
BOT_VERSION = "v10-rumiyo"
print("Rumiyo bot ishga tushmoqda:", BOT_VERSION)


class RateBot(Bot):
    """Telegram chegaralaridan oshmaydi: hamma chatlarga jami ~25 so'rov/soniya (limit 30), bitta shaxsiy chatga ~3/soniya,
    bitta guruhga 3 soniyada 1 ta (limit: daqiqasiga 20 ta). 429 (RetryAfter) kelsa, kutib qayta urinadi."""
    G_GAP, PRIV_GAP, GROUP_GAP = 1 / 25, 0.35, 3.1

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._g_next = 0.0
        self._c_next = {}

    async def __call__(self, method, request_timeout=None):
        name = type(method).__name__
        if name != "GetUpdates":
            chat = getattr(method, "chat_id", None)
            now = time.monotonic()
            slot = max(self._g_next, now)
            if isinstance(chat, int):
                slot = max(slot, self._c_next.get(chat, 0.0))
                self._c_next[chat] = slot + (self.GROUP_GAP if chat < 0 else self.PRIV_GAP)
                if len(self._c_next) > 50000:
                    self._c_next = {k: v for k, v in self._c_next.items() if v > now}
            self._g_next = max(self._g_next, slot) + self.G_GAP
            if slot > now:
                await asyncio.sleep(slot - now)
        for i in range(3):
            try:
                return await super().__call__(method, request_timeout=request_timeout)
            except TelegramRetryAfter as e:
                if i == 2 or name == "GetUpdates":
                    raise
                await asyncio.sleep(min(e.retry_after, 30) + 0.5)


bot = RateBot(TOKEN, session=_sess, default=DefaultBotProperties(link_preview_is_disabled=True))  # sekin tarmoqda 60 soniyagacha kutadi
dp = Dispatcher()


def btn(key):
    return InlineKeyboardButton(text=GOALS[key], callback_data=f"goal:{key}")


def user(uid):
    r = db.execute("SELECT goal, level FROM users WHERE user_id=?", (uid,)).fetchone()
    return r if r else (None, None)


def ulang(uid):
    """O'quvchining hozirgi tili: 'ru' yoki 'en' (eski o'quvchilarda bo'sh = rus)."""
    r = db.execute("SELECT lang FROM users WHERE user_id=?", (uid,)).fetchone()
    return r[0] if r and r[0] in LANG_NAMES else "ru"


def webapp_for(uid):
    return EN_WEBAPP_URL if ulang(uid) == "en" else WEBAPP_URL


M_LESSON, M_VIP, M_ME, M_HELP = "📖 Darsni ochish", "💎 VIP obuna", "📈 Mening natijam", "ℹ️ Narx va yordam"
M_LANG = "🌐 Tilni almashtirish"
M_TALK = "🗣 Suhbat"
M_LESSONS = "📚 Darslarim"
M_PUL = "💸 Pul ishlash"


def menu_rows():
    return [[KeyboardButton(text=M_LESSON)], [KeyboardButton(text=M_LESSONS), KeyboardButton(text=M_VIP)], [KeyboardButton(text=M_LANG)]]      # Suhbat va Pul ishlash olib tashlandi


def main_menu():
    """Chatning pastidagi doimiy menyu (daraja aniqlangandan keyin)."""
    return ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True, keyboard=menu_rows())


def test_menu(url):
    """Dars testi tugmasi + doimiy menyu (test paytida ham menyu yo'qolmaydi)."""
    return ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True, keyboard=[
        [KeyboardButton(text="📝 Testni boshlash", web_app=WebAppInfo(url=url))]] + menu_rows())


def placement_site(uid):
    """Yangi o'quvchining daraja testi Rumiyo saytida o'tadi (Rumiyo tanishadi -> test -> natija -> hozir/ertaga)."""
    return bool(uid and RUMIYO_URL and TALK_SECRET and os.getenv("INTRO_OFF") != "1" and os.getenv("PLACEMENT_SITE") != "0")


def pl_link(uid):
    import base64, hashlib, hmac
    body = base64.urlsafe_b64encode(json.dumps({"u": uid, "l": "A1", "e": int(time.time()) + 86400, "d": 0, "g": ulang(uid), "t": "Daraja testi", "s": 0, "n": 0,
                                                "v": 0, "h": 3, "tx": webapp_for(uid), "o": (user(uid)[0] or "")[:40]},
                                               separators=(",", ":"), ensure_ascii=False).encode()).decode().rstrip("=")
    sig = hmac.new(TALK_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{RUMIYO_URL.rstrip('/')}/?t={body}.{sig}"


def classic_test_kb(uid=None):
    """Oddiy usul: chat tagidagi tugma orqali trener ichidagi daraja testi (sayt ishlamasa ham ishlaydi)."""
    url = webapp_for(uid) if uid else WEBAPP_URL
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[KeyboardButton(
        text="📝 Testni boshlash", web_app=WebAppInfo(url=url))]])


def test_kb(uid=None):
    if placement_site(uid) and RUMIYO_URL.startswith("https://"):
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Testni boshlash", web_app=WebAppInfo(url=pl_link(uid)))]])
    return classic_test_kb(uid)


async def ask_test(msg, text, uid):
    """Test tugmasini yuboradi. Rumiyo havolasi xato bersa, odam qotib qolmasin: oddiy test tugmasi yuboriladi."""
    try:
        return await msg.answer(text, reply_markup=test_kb(uid))
    except Exception as e:
        print("test tugmasi (sayt) yuborilmadi, oddiy usulga o'tildi:", type(e).__name__, str(e)[:120])
        return await msg.answer(text, reply_markup=classic_test_kb(uid))


@dp.message(CommandStart())
async def start(m: Message):
    uid = m.from_user.id
    db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))  # xabar yuborish ro'yxati uchun
    parts = (m.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if arg.startswith("r") and arg[1:].isdigit() and not m.from_user.is_bot:
        await ref_join(m.from_user, int(arg[1:]))
    elif arg.startswith("k") and len(arg) > 1 and not m.from_user.is_bot:
        apply_promo(uid, arg[1:])
    db.commit()
    goal, level = user(m.from_user.id)
    if arg == "talk" and level:  # guruhdagi «Suhbat» tugmasidan keldi
        return await menu_talk(m)
    if arg == "dars" and not level:
        for forced in (None, uid):                  # avval oddiy, keyin to'g'ridan-to'g'ri (raqamga qaramay)
            try:
                await apply_site_placements(force_uid=forced)
            except Exception as e:
                print("dars: natija olinmadi:", type(e).__name__, e)
            goal, level = user(uid)
            if level:
                break
        if level and meta_get(f"plsent{uid}"):
            return                                   # natija va dars xabari yuborildi
        if not level:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Qayta tekshirish", callback_data="plcheck")]])
            return await m.answer("⏳ Test natijangiz hali yetib kelmadi. Bir necha soniyadan keyin «🔄 Qayta tekshirish»ni bosing. "
                                  "Test qaytadan boshlanmaydi.", reply_markup=kb)
    if arg == "dars" and level and meta_get(f"plsent{uid}") and now() - int(meta_get(f"plsent{uid}") or 0) < 900:
        meta_set(f"plsent{uid}", "")                    # dars xabari allaqachon yuborilgan: takrorlamaymiz
        return await m.answer("👆 Dars xabari yuqorida. «📖 Darsni ochish» tugmasini bosing.", reply_markup=main_menu())
    if arg == "dars" and level:  # Rumiyo saytidagi «Darsni boshlash» tugmasidan keldi
        meta_set(intro_key(uid), "1")
        return await show_lesson(m, uid, skip_intro=True)
    if level:  # daraja faqat bir marta aniqlanadi, boshiga qaytish yo'q
        await m.answer("👋 Xush kelibsiz! Pastdagi menyudan foydalaning.", reply_markup=main_menu())
        return await show_lesson(m, m.from_user.id)
    if goal:  # maqsad tanlangan, test hali topshirilmagan
        return await ask_test(m, "Darajangizni aniqlash qoldi. Chat tagidagi «📝 Testni boshlash» tugmasini bosing 👇", uid)
    if lang_chosen(uid):  # til tanlangan, maqsad hali yo'q
        return await m.answer(goal_question(ulang(uid)), reply_markup=goal_kb())
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Boshlash", callback_data="go")]])
    intro = ("Salom! Men sizga rus yoki ingliz tilini o'zbek tilida tushuntirib o'rgataman.\n\n"
             "🚀 Har kuni qisqa dars: 5 ta yangi gap (taxminan 15 daqiqa). Eski gaplar esdan chiqmasin deb darsning ichida o'zi qaytadi: "
             "1, 3, 8, 21 kundan keyin. Eshitib yozish va gapirish mashqlari ham bor.\n"
             "Har kuni bitta qisqa dars, A1 dan C1 gacha.\n\n"
             "Avval darajangizni aniqlaymiz, keyin har kuni bitta dars bo'ladi.")
    await m.answer(intro + "\n\nBoshlaymizmi?", reply_markup=kb)


@dp.callback_query(F.data == "plcheck")
async def pl_check(c: CallbackQuery):
    uid = c.from_user.id
    if not user(uid)[1]:
        try:
            await apply_site_placements(force_uid=uid)
        except Exception as e:
            print("plcheck:", type(e).__name__, e)
    if user(uid)[1]:
        await c.answer("✅ Natija keldi")
        return
    await c.answer("Hali kelmadi, yana bir oz kuting", show_alert=True)


def lang_chosen(uid):
    r = db.execute("SELECT lang FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r[0] in LANG_NAMES)


def goal_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("ish"), btn("imtihon")], [btn("kino"), btn("sayohat")], [btn("boshqa")]])


def goal_question(lang):
    return f"{'Ingliz' if lang == 'en' else 'Rus'} tilini nega o'rganmoqchisiz?"


def lang_kb(cur=None):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=("✅ " if k == cur else "") + v, callback_data=f"lang:{k}")] for k, v in LANG_NAMES.items()])


@dp.callback_query(F.data == "go")
async def ask_goal(c: CallbackQuery):
    if user(c.from_user.id)[0]:
        return await c.answer("Maqsad allaqachon tanlangan")
    await c.message.edit_text("Qaysi tilni o'rganmoqchisiz?", reply_markup=lang_kb())
    await c.answer()


def has_started(uid):
    """O'quvchi biror tilda darajasini aniqlagan (hozirgi yoki saqlangan)."""
    r = db.execute("SELECT level, alt_level FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and (r[0] or r[1]))


@dp.callback_query(F.data.startswith("lang:"))
async def lang_cb(c: CallbackQuery):
    uid, new = c.from_user.id, c.data.split(":", 1)[1]
    if new not in LANG_NAMES:
        return await c.answer()
    if not has_started(uid):  # birinchi tanlov: til -> maqsad -> test
        if user(uid)[0]:
            return await c.answer("Maqsad allaqachon tanlangan")
        db.execute("INSERT INTO users(user_id, lang) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang", (uid, new))
        db.commit()
        await c.message.edit_text(goal_question(new), reply_markup=goal_kb())
        return await c.answer()
    if new == ulang(uid):
        return await c.answer("Siz hozir shu tildasiz")
    await c.answer()
    await do_switch(c.message, uid, new)


async def do_switch(m: Message, uid, new):
    """Tilni almashtiradi: hozirgi holat (maqsad, daraja, keyingi dars vaqti) saqlanadi, boshqa tilniki tiklanadi."""
    db.execute("UPDATE users SET lang=?, goal=alt_goal, level=alt_level, next_at=COALESCE(alt_next_at, 0), "
               "alt_goal=goal, alt_level=level, alt_next_at=COALESCE(next_at, 0), notified=1 WHERE user_id=?", (new, uid))
    db.commit()
    goal, level = user(uid)
    await m.answer(f"✅ Til almashdi: {LANG_NAMES[new]}", reply_markup=main_menu())  # menyu tugmalari ham yangilanadi
    if level:
        return await show_lesson(m, uid)
    if goal:
        return await ask_test(m, "Darajangizni aniqlash qoldi. Chat tagidagi «📝 Testni boshlash» tugmasini bosing 👇", uid)
    await m.answer(goal_question(new), reply_markup=goal_kb())


@dp.message(F.text == M_LANG)
@dp.message(Command("til"))
async def menu_lang(m: Message):
    """Bir bosishda ikkinchi tilga o'tadi: rus bo'lsa ingliz, ingliz bo'lsa rus. Ikkala tilning darajasi va darslari alohida saqlanadi."""
    uid = m.from_user.id
    if not has_started(uid):
        return await m.answer("Avval /start bosib, tilni va darajangizni tanlang.")
    await do_switch(m, uid, "ru" if ulang(uid) == "en" else "en")


@dp.callback_query(F.data.startswith("goal:"))
async def got_goal(c: CallbackQuery):
    key = c.data.split(":", 1)[1]
    if key not in GOALS:
        return await c.answer()
    if user(c.from_user.id)[0]:
        return await c.answer("Maqsad allaqachon tanlangan")
    try:
        await c.answer()                                # tugma aylanib qolmasin
    except Exception:
        pass
    db.execute("INSERT INTO users(user_id, goal) VALUES(?, ?) "
               "ON CONFLICT(user_id) DO UPDATE SET goal=excluded.goal", (c.from_user.id, key))
    db.commit()
    try:
        await c.message.edit_text(f"Ajoyib, maqsadingiz: {GOALS[key]}")
    except Exception:
        pass
    await ask_test(c.message, "Endi darajangizni aniqlaymiz, bu 2 daqiqa oladi. Chat tagidagi «📝 Testni boshlash» tugmasini bosing 👇", c.from_user.id)


def result_text(raw):
    """Mini App yuborgan JSON'ni tekshiradi. (daraja, matn) yoki None qaytaradi."""
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    level = d.get("level") if isinstance(d, dict) else None
    if level not in LEVELS:
        return None
    if level == "A0" and not d.get("skip"):
        level = "A1"  # test A0 bermaydi: A0 faqat odam o'zi «noldan boshlayman» desa (eski keshlangan test sahifalari uchun ham himoya)
    if d.get("skip"):
        return level, f"✅ Yaxshi, noldan boshlaymiz ({level})."
    acc = {k: d[k] for k in NAMES
           if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool)}
    near = d.get("near")
    text = f"✅ Darajangiz: {level}" + (f" ({near} ga yaqin)" if near in LEVELS else "") + "\n\n"
    sc, nn = d.get("s"), d.get("n")
    new = isinstance(nn, int) and not isinstance(nn, bool) and 0 <= nn <= 60  # yangi (ingliz) test: blok-blok, har blok 10 ball
    if isinstance(sc, int) and not isinstance(sc, bool) and 0 <= sc <= (nn if new else 20):
        text += f"📊 To'plangan ball: {sc}/{nn}\n\n" if new else f"📊 To'g'ri javoblar: {sc}/20\n\n"
    lv = d.get("lv")
    mx = d.get("m") if isinstance(d.get("m"), int) and not isinstance(d.get("m"), bool) and 1 <= d.get("m") <= 10 else (10 if new else 4)
    if isinstance(lv, list) and len(lv) == 5 and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= mx for x in lv):
        text += "📈 Darajalar bo'yicha: " + " · ".join(f"{n} {x}/{mx}" for n, x in zip(LEVELS[1:], lv)) + "\n\n"
    if len(acc) > 1:
        strong, weak = max(acc, key=acc.get), min(acc, key=acc.get)
        if acc[strong] > acc[weak]:
            text += f"💪 Kuchli tomon: {NAMES[strong]}\n📌 Zaif tomon: {NAMES[weak]}\n\n"
        else:
            text += "Ikkala yo'nalishda natijangiz teng.\n\n"
    return level, text + "Bu taxminiy natija, darslar davomida yanada aniqlashadi."


@dp.message(F.web_app_data)
async def got_result(m: Message):
    uid = m.from_user.id
    old = user(uid)[1]
    lres = lesson_result(m.web_app_data.data)
    if lres is not None:
        return await got_lesson_test(m, uid, lres)
    if old:  # daraja bir marta aniqlanadi: ikkinchi natija qabul qilinmaydi
        await m.answer(f"Darajangiz allaqachon aniqlangan: {old}. Test qayta topshirilmaydi.",
                       reply_markup=main_menu())
        return await show_lesson(m, uid)
    res = result_text(m.web_app_data.data)
    if not res:
        return await m.answer("Natijani o'qib bo'lmadi. Iltimos, testni qaytadan boshlang.")
    level, text = res
    try:
        if json.loads(m.web_app_data.data).get("skip"):
            meta_set(f"sk{uid}", "1")   # «Umuman bilmayman» tugmasi bilan o'zi A0 tanlagan
    except Exception:
        pass
    db.execute("INSERT INTO users(user_id, level) VALUES(?, ?) "
               "ON CONFLICT(user_id) DO UPDATE SET level=excluded.level", (uid, level))
    db.commit()
    await m.answer(text, reply_markup=main_menu())
    await ref_credit(uid)
    await show_lesson(m, uid)


# ---------------- taklif: REF_NEED ta do'st kirsa, VIP o'zi ochiladi ----------------
REF_NEED = int(os.getenv("REF_NEED", "0"))   # 0 = do'st keltirib BEPUL VIP olish o'chiq (pul ishlash tizimi ishlaydi)
PRICE_NUM = int(re.sub(r"\D", "", PRICE) or 0)                      # PRICE ning raqami: 99 000 so'm -> 99000
LAUNCH_PRICE = os.getenv("LAUNCH_PRICE", "").strip()                 # ochilish narxi, masalan: 79 000 so'm
LAUNCH_NUM = int(re.sub(r"\D", "", LAUNCH_PRICE) or 0)
LAUNCH_SLOTS = int(os.getenv("LAUNCH_SLOTS", "50"))                  # ochilish narxida nechta odam oladi
FRIEND_DISC = int(os.getenv("FRIEND_DISC", "20"))                    # taklif havolasi bilan kirgan odamga chegirma, %
REF_PCT = int(os.getenv("REF_PCT", "20"))                            # do'sti to'lasa, taklif qilgan odam oladigan ulush, %
HOLD_DAYS = int(os.getenv("HOLD_DAYS", "3"))                          # komissiya necha kundan keyin yechiladi (soxta chekni tekshirish uchun)
MIN_PAYOUT = int(os.getenv("MIN_PAYOUT", "50000"))                   # pulni yechish uchun eng kam summa


# tarifflar: kalit -> (nom, kun (0=umrbod), narx)
def _num(name, default):
    return int(re.sub(r"\D", "", os.getenv(name, "") or str(default)) or default)


PLANS = {
    "1": ("1 oylik", 30, _num("PRICE_1M", 39000)),
    "3": ("3 oylik", 90, _num("PRICE_3M", 89000)),
    "12": ("12 oylik", 365, _num("PRICE_12M", 199000)),
    "L": ("Umrbod", 0, PRICE_NUM or 99000),  # eski tarif: sotilmaydi, faqat avval olganlar uchun saqlanadi
}


def money(n):
    return f"{int(n):,}".replace(",", " ") + " so'm"


def launch_left():
    if not LAUNCH_NUM or not LAUNCH_SLOTS:
        return 0
    used = db.execute("SELECT COUNT(*) FROM payments WHERE status='approved' AND COALESCE(plan,'L')='L'").fetchone()[0]
    return max(0, LAUNCH_SLOTS - used)


def promo_of(uid):
    return db.execute("SELECT p.code, p.disc, p.comm, p.owner FROM user_promo u JOIN promos p ON p.code=u.code WHERE u.user_id=?",
                      (uid,)).fetchone()


def price_for(uid, plan="L"):
    """(summa, izoh). Ochilish narxi (faqat umrbod) + do'st/promokod chegirmasidan eng foydalisi."""
    base, note = PLANS.get(plan, PLANS["L"])[2], ""
    left = launch_left()
    if left > 0 and plan == "L":
        base, note = LAUNCH_NUM, f"🔥 Ochilish narxi (yana {left} ta joy qoldi)"
    disc, why = 0, ""
    pr = promo_of(uid)
    if pr and pr[1] > disc:
        disc, why = pr[1], f"promokod {pr[0]}"
    if db.execute("SELECT 1 FROM referrals WHERE invited=?", (uid,)).fetchone() and FRIEND_DISC > disc:
        disc, why = FRIEND_DISC, "do'stingiz taklifi"
    if disc and base:
        amount = max(1000, int(round(base * (100 - disc) / 100 / 1000.0)) * 1000)
        note = (note + "\n" if note else "") + f"🎁 Qo'shimcha {disc}% chegirma ({why})"
        return amount, note
    return base, note


def apply_promo(uid, code):
    code = re.sub(r"[^A-Za-z0-9_]", "", code or "").upper()
    if not code or not db.execute("SELECT 1 FROM promos WHERE code=?", (code,)).fetchone():
        return False
    if not db.execute("SELECT 1 FROM user_promo WHERE user_id=?", (uid,)).fetchone():
        db.execute("UPDATE promos SET uses=uses+1 WHERE code=?", (code,))
    db.execute("INSERT OR REPLACE INTO user_promo(user_id, code) VALUES(?, ?)", (uid, code))
    db.commit()
    return True
_bot_un = [None]


async def ref_link(uid):
    if not _bot_un[0]:
        _bot_un[0] = (await bot.get_me()).username
    return f"https://t.me/{_bot_un[0]}?start=r{uid}"


def ref_count(uid):
    return db.execute("SELECT COUNT(*) FROM referrals WHERE inviter=? AND ok=1", (uid,)).fetchone()[0]


REF_ADMIN = os.getenv("REF_ADMIN_NOTICE", "0") == "1"   # 1 bo'lsa admin taklif haqida ma'lumot oladi (tasdiqlash shart emas)
REF_BURST = int(os.getenv("REF_BURST", "3"))   # 10 daqiqada shundan ko'p yangi odam: shubhali, daraja testigacha kutadi


async def ref_join(who, inv):
    """Taklif havolasi orqali kelgan YANGI odam darhol hisoblanadi.
    Himoya: o'zini taklif qila olmaydi; bir odam faqat bir marta (birinchi havola) hisoblanadi; botni sanamaydi;
    bot/darsni allaqachon boshlagan odam hisoblanmaydi; 10 daqiqada juda ko'p yangi odam kelsa (soxta akkauntlar)
    ular daraja testini topshirguncha kutadi va admin xabardor qilinadi."""
    uid = who.id
    if inv == uid or not db.execute("SELECT 1 FROM users WHERE user_id=?", (inv,)).fetchone():
        return
    goal, level = user(uid)
    if goal or level or db.execute("SELECT 1 FROM referrals WHERE invited=?", (uid,)).fetchone():
        return
    now = int(time.time())
    burst = db.execute("SELECT COUNT(*) FROM referrals WHERE inviter=? AND ts>?", (inv, now - 600)).fetchone()[0]
    ok = 0 if burst >= REF_BURST else 1
    db.execute("INSERT OR IGNORE INTO referrals(invited, inviter, ok, ts) VALUES(?, ?, ?, ?)", (uid, inv, ok, now))
    db.commit()
    if ok:
        await ref_progress(inv, who)
    elif burst == REF_BURST and REF_ADMIN:
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, f"⚠️ Shubhali taklif: {inv} havolasi orqali 10 daqiqada {burst + 1}+ yangi odam keldi. "
                                            f"Ular daraja testini topshirguncha hisoblanmaydi.")
            except Exception:
                pass


async def ref_progress(inv, who=None):
    """Hisob oshganda taklif qilgan odamga darhol xabar; REF_NEED ga yetsa VIP ochiladi."""
    n = ref_count(inv)
    give = PAID_MODE and REF_NEED > 0 and n >= REF_NEED and not is_paid(inv)
    if give:
        grant_vip(inv)
    db.commit()
    name = (who.full_name if who else "Do'stingiz")[:40]
    try:
        if give:
            await bot.send_message(inv, f"🎉 {name} sizning havolangiz orqali kirdi. {REF_NEED} ta do'st to'ldi, VIP sizga ochildi!")
            await push_lesson(inv)
        elif REF_NEED > 0 and not is_paid(inv):
            await bot.send_message(inv, f"👥 {name} sizning havolangiz orqali kirdi!\nHozir: {n}/{REF_NEED}")
        elif REF_NEED <= 0:
            await bot.send_message(inv, f"👥 {name} sizning havolangiz orqali kirdi! U VIP olsa, siz {REF_PCT}% pul olasiz 💸")
    except Exception:
        pass
    if give and REF_ADMIN:
        ids = [str(r[0]) for r in db.execute("SELECT invited FROM referrals WHERE inviter=? AND ok=1", (inv,))]
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(aid, f"🎁 Taklif orqali VIP ochildi: {inv}\nTaklif qilinganlar: {', '.join(ids)}")
            except Exception:
                pass


async def ref_credit(invited):
    """Kutib turgan (shubhali) taklif: odam daraja testini topshirganda hisoblanadi."""
    r = db.execute("SELECT inviter FROM referrals WHERE invited=? AND ok=0", (invited,)).fetchone()
    if not r:
        return
    db.execute("UPDATE referrals SET ok=1 WHERE invited=?", (invited,))
    db.commit()
    await ref_progress(r[0])


def example_earn():
    """Do'sti 12 oylik tarifni olsa, taklif qilgan odam oladigan summa (misol uchun)."""
    base = LAUNCH_NUM if launch_left() > 0 and LAUNCH_NUM else PLANS["12"][2]
    price = max(1000, int(round(base * (100 - FRIEND_DISC) / 100 / 1000.0)) * 1000)
    return price * REF_PCT // 100


async def ref_text(uid):
    link = await ref_link(uid)
    return (f"💸 Do'stingizga yuboring: u VIP olsa, siz {money(example_earn())} olasiz.\n"
            f"Batafsil: /pul\n\n{link}")


def earn_sum(uid, paid=None):
    q, a = "SELECT COALESCE(SUM(amount),0) FROM earnings WHERE user_id=?", [uid]
    if paid is not None:
        q += " AND paid=?"; a.append(paid)
    return db.execute(q, a).fetchone()[0]


def _cut():
    return int(time.time()) - HOLD_DAYS * 86400


def earn_ready(uid):   # yechsa bo'ladigan (tekshiruv muddati o'tgan)
    return db.execute("SELECT COALESCE(SUM(amount),0), COALESCE(MAX(id),0) FROM earnings WHERE user_id=? AND paid=0 AND ts<=?",
                      (uid, _cut())).fetchone()


def earn_wait(uid):    # hali kutilayotgan
    return db.execute("SELECT COALESCE(SUM(amount),0) FROM earnings WHERE user_id=? AND paid=0 AND ts>?", (uid, _cut())).fetchone()[0]


def pul_kb(link, ready):
    share = "https://t.me/share/url?url=" + link + "&text=" + "Rus va ingliz tilini bot orqali o'rganing. Shu havola bilan kirsangiz VIP arzonroq:"
    rows = [[InlineKeyboardButton(text="📤 Do'stga yuborish", url=share)]]
    if ready >= MIN_PAYOUT:
        rows.append([InlineKeyboardButton(text="💳 Pulni yechish", callback_data="pul:out")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def pul_screen(uid):
    link = await ref_link(uid)
    ready = earn_ready(uid)[0]
    wait = earn_wait(uid)
    came = ref_count(uid)
    bought = db.execute("SELECT COUNT(*) FROM earnings WHERE user_id=?", (uid,)).fetchone()[0]
    t = ("💸 PUL ISHLASH\n\n"
         "Juda oddiy:\n"
         "1️⃣ Pastdagi tugma bilan do'stingizga yuboring\n"
         f"2️⃣ Do'stingiz VIP olsa, siz {money(example_earn())} olasiz\n"
         f"3️⃣ Do'stingiz ham {FRIEND_DISC}% arzon oladi\n\n"
         "📊 Sizda:\n"
         f"✅ Yechsa bo'ladi: {money(ready)}\n")
    if wait:
        t += f"⏳ Tekshirilmoqda: {money(wait)} ({HOLD_DAYS} kundan keyin ochiladi)\n"
    t += f"👥 Kirganlar: {came} · 🛒 Sotib olganlar: {bought}\n\n"
    t += (f"Pulni yechish {money(MIN_PAYOUT)} dan boshlanadi.\n\n" if ready < MIN_PAYOUT else "Pul tayyor! Pastdagi tugmani bosing 👇\n\n")
    return t + "Sizning havolangiz:\n" + link, pul_kb(link, ready)


@dp.message(Command("pul", "taklif", "balans"))
@dp.message(F.text == M_PUL)
async def cmd_pul(m: Message):
    if not PAID_MODE:
        return await m.answer("Hozircha hamma dars bepul 🙂")
    t, kb = await pul_screen(m.from_user.id)
    await m.answer(t, reply_markup=kb)


await_card = {}   # uid -> vaqt: karta raqami kutilmoqda


@dp.callback_query(F.data == "pul:out")
async def pul_out(c: CallbackQuery):
    uid = c.from_user.id
    if db.execute("SELECT 1 FROM payouts WHERE user_id=? AND status='pending'", (uid,)).fetchone():
        return await c.answer("So'rovingiz allaqachon yuborilgan, kuting.", show_alert=True)
    if earn_ready(uid)[0] < MIN_PAYOUT:
        return await c.answer(f"Kamida {money(MIN_PAYOUT)} kerak", show_alert=True)
    await_card[uid] = time.time()
    await c.message.answer("💳 Karta raqamingizni yozing (16 ta raqam).\nMasalan: 8600 1234 5678 9012\n\nBekor qilish: /pul")
    await c.answer()


@dp.message(Command("yechish"))
async def cmd_yechish(m: Message):
    uid = m.from_user.id
    if earn_ready(uid)[0] < MIN_PAYOUT:
        return await m.answer(f"Hozircha yechsa bo'ladigan pul yo'q. Ko'rish: /pul")
    await_card[uid] = time.time()
    await m.answer("💳 Karta raqamingizni yozing (16 ta raqam).\nMasalan: 8600 1234 5678 9012\n\nBekor qilish: /pul")


def _card_waiting(m):
    return bool(m.text) and m.from_user.id in await_card and time.time() - await_card[m.from_user.id] < 600


@dp.message(F.func(_card_waiting) & ~F.text.startswith("/"))
async def got_card(m: Message):
    uid = m.from_user.id
    card = re.sub(r"\D", "", m.text or "")
    if len(card) != 16:
        return await m.answer("Karta 16 ta raqamdan iborat. Qaytadan yozing yoki /pul bosing.")
    await_card.pop(uid, None)
    amount, maxid = earn_ready(uid)
    if amount < MIN_PAYOUT:
        return await m.answer("Hozircha yechsa bo'ladigan pul yo'q.")
    if db.execute("SELECT 1 FROM payouts WHERE user_id=? AND status='pending'", (uid,)).fetchone():
        return await m.answer("So'rovingiz allaqachon yuborilgan, kuting.")
    other = db.execute("SELECT COUNT(DISTINCT user_id) FROM payouts WHERE card=? AND user_id<>?", (card, uid)).fetchone()[0]
    pid = db.execute("INSERT INTO payouts(user_id, card, maxid, amount, ts) VALUES(?, ?, ?, ?, ?)", (uid, card, maxid, amount, int(time.time()))).lastrowid
    db.commit()
    warn = f"\n⚠️ Bu karta boshqa {other} ta akkaunt uchun ham so'ralgan!" if other else ""
    buyers = db.execute("SELECT COUNT(*) FROM earnings WHERE user_id=? AND paid=0 AND id<=?", (uid, maxid)).fetchone()[0]
    kb = kb_of([("✅ To'landi", f"out:{pid}"), ("❌ Rad etish", f"outno:{pid}")])
    sent = 0
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, f"💸 Pul yechish #{pid}\n👤 {m.from_user.full_name} (ID {uid})\n💰 {money(amount)} ({buyers} ta sotuvdan)\n💳 {card}{warn}", reply_markup=kb)
            sent += 1
        except Exception:
            pass
    await m.answer("✅ So'rov yuborildi. Pul o'tkazilganda sizga xabar keladi." if sent else "So'rovni yuborib bo'lmadi, keyinroq urinib ko'ring.")


@dp.callback_query(F.data.startswith("out:") | F.data.startswith("outno:"))
async def payout_done(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    act, pid = c.data.split(":")
    row = db.execute("SELECT user_id, card, maxid, status FROM payouts WHERE id=?", (int(pid),)).fetchone()
    if not row or row[3] != "pending":
        return await c.answer("Bu so'rov allaqachon ko'rib chiqilgan", show_alert=True)
    uid, card, maxid, _ = row
    if act == "outno":
        db.execute("UPDATE payouts SET status='rejected' WHERE id=?", (int(pid),))
        db.commit()
        try:
            await c.message.edit_text((c.message.text or "") + "\n\n❌ RAD ETILDI", reply_markup=None)
            await bot.send_message(uid, "❌ Pul yechish so'rovi rad etildi. Savol bo'lsa adminga yozing.")
        except Exception:
            pass
        return await c.answer("Bajarildi")
    amount = db.execute("SELECT COALESCE(SUM(amount),0) FROM earnings WHERE user_id=? AND paid=0 AND id<=?", (uid, maxid)).fetchone()[0]
    db.execute("UPDATE earnings SET paid=1 WHERE user_id=? AND paid=0 AND id<=?", (uid, maxid))
    db.execute("UPDATE payouts SET status='paid', amount=? WHERE id=?", (amount, int(pid)))
    db.commit()
    try:
        await c.message.edit_text((c.message.text or "") + f"\n\n✅ TO'LANDI ({money(amount)})", reply_markup=None)
    except Exception:
        pass
    try:
        await bot.send_message(uid, f"✅ {money(amount)} kartangizga o'tkazildi. Rahmat! Yana do'st yuboring: /pul 🚀")
    except Exception:
        pass
    await c.answer("Bajarildi")


@dp.message(Command("kod"))
async def cmd_code(m: Message):
    uid = m.from_user.id
    parts = (m.text or "").split()
    if len(parts) < 2:
        return await m.answer("Promokod bo'lsa yozing: /kod KODINGIZ")
    if not apply_promo(uid, parts[1]):
        return await m.answer("Bunday promokod topilmadi.")
    amount, note = price_for(uid)
    await m.answer(f"✅ Promokod qabul qilindi!\n💰 Sizning narxingiz: {money(amount)}" + (f"\n{note}" if note else "") + "\n\nVIP olish: /vip")


@dp.message(Command("promo"))
async def cmd_promo(m: Message):  # admin: /promo KOD CHEGIRMA% KOMISSIYA% [EGASI_ID]
    if not is_admin(m.from_user.id):
        return
    p = (m.text or "").split()
    if len(p) < 4 or not p[2].isdigit() or not p[3].isdigit():
        return await m.answer("Ishlatish: /promo KOD CHEGIRMA KOMISSIYA [EGASI_ID]\nMasalan: /promo ALI 20 30 123456789\n"
                              "(20% chegirma, egasiga 30% pul. Egasi ID si bo'lmasa, komissiya yo'q.)")
    code = re.sub(r"[^A-Za-z0-9_]", "", p[1]).upper()
    owner = int(p[4]) if len(p) > 4 and p[4].isdigit() else 0
    db.execute("INSERT INTO promos(code, disc, comm, owner) VALUES(?, ?, ?, ?) ON CONFLICT(code) DO UPDATE SET disc=?, comm=?, owner=?",
               (code, int(p[2]), int(p[3]), owner, int(p[2]), int(p[3]), owner))
    db.commit()
    await m.answer(f"✅ Promokod: {code}\nChegirma: {p[2]}%  Egasiga: {p[3]}%  Egasi: {owner or '—'}\n"
                   f"Havola: https://t.me/{_bot_un[0] or (await bot.get_me()).username}?start=k{code}")


@dp.message(Command("sotuv"))
async def cmd_sales(m: Message):  # admin statistika
    if not is_admin(m.from_user.id):
        return
    n_ok = db.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments WHERE status='approved'").fetchone()
    users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    owe = db.execute("SELECT COALESCE(SUM(amount),0) FROM earnings WHERE paid=0").fetchone()[0]
    promos = db.execute("SELECT code, uses FROM promos ORDER BY uses DESC LIMIT 10").fetchall()
    await m.answer(f"📊 Sotuv\n👥 Foydalanuvchilar: {users}\n🛒 Sotib olganlar: {n_ok[0]}\n💰 Tushum: {money(n_ok[1])}\n"
                   f"🔥 Ochilish joylari qoldi: {launch_left()}\n💸 Hamkorlarga qarz: {money(owe)}\n"
                   + ("🏷 Promokodlar: " + ", ".join(f"{c}({u})" for c, u in promos) if promos else ""))


# ---------------- darslar (foydalanuvchi tomoni) ----------------
def now():
    return int(time.time())


NEW_LESSON = [
    "🌅 Xayrli kun! Yangi dars ochildi: bugun yana bir qadam oldinga!",
    "✨ Yangi kun, yangi dars! Siz har kuni o'syapsiz, shunday davom eting.",
    "🌟 Zo'r ketyapsiz! Bugungi dars sizni kutyapti.",
    "💪 Intizomli o'quvchilar shunday bo'ladi: har kuni bir dars. Bugungi dars tayyor!",
    "🚀 Yangi mavzu ochildi! Siz hammasini uddalaysiz.",
    "🌷 Salom! Bugungi darsni boshlaymizmi? Siz bunga arziysiz, mehnatingiz sezilyapti.",
    "🔥 Ofarin! Har kungi odatingiz katta natijaga olib boradi. Yangi dars ochildi!",
    "☀️ Bugun ham o'rganishga vaqt topdingiz, shu bilan allaqachon g'olibsiz. Dars tayyor!",
    "🎯 Maqsadingizga yana bir qadam: yangi dars ochildi!",
    "🧠 Miyangiz yangi so'zlarga tayyor! Bugungi darsga marhamat.",
    "🌈 Yangi dars, yangi imkoniyat! Barakalla, to'xtamayapsiz.",
    "🏅 Siz ko'pchilik boshlab tashlab ketadigan ishni davom ettiryapsiz. Qoyil! Yangi dars ochildi.",
    "📖 Bugungi darsni ochamiz: har kuni oz-ozdan, mana natija!",
    "😊 Xayrli kun! Bugungi yangi mavzu sizga yoqadi, ishonaman.",
    "🌱 Har kuni bir oz o'sish: mana shu kuch. Yangi dars sizni kutyapti!",
    "🎉 Yangi dars ochildi! Siz o'zingizga bergan va'daga sodiqsiz, bu juda yaxshi.",
    "⚡️ Bugun ham zo'r bo'lamiz! Yangi mavzu tayyor.",
    "🕊 Tinch, sabr bilan o'rganing: bugungi dars qiyin emas. Boshlaymiz!",
    "💎 Bilim eng qimmat boylik. Bugungi darsni oling!",
    "🌞 Barakalla, sabringiz uchun! Yangi dars ochildi.",
]


def new_lesson_text(uid):
    t = random.choice(NEW_LESSON)
    done = {r[0] for r in db.execute("SELECT lesson_id FROM progress WHERE user_id=?", (uid,))}
    cur = None  # keyingi dars (daraja holatini o'zgartirmaydi: tabrik xabari yo'qolmasin)
    for lvl in LESSON_LEVELS[LESSON_LEVELS.index(eff(user(uid)[1])):]:
        cur = next((x for x in lessons_of(lvl, ulang(uid)) if x[0] not in done), None)
        if cur:
            break
    if cur:
        r = db.execute("SELECT topic FROM lessons WHERE id=?", (cur[0],)).fetchone()
        if r and r[0]:
            t += f"\n📖 Bugungi mavzu: {r[0]}"
    return t


SPEAK_TIPS = [
    "🗣 Endi o'rganganingizni gapirib mashq qiling: «Suhbat» tugmasini bosing. Gapirmasdan til o'rganilmaydi!",
    "🗣 Dars tugadi, endi gapirish vaqti! «Suhbat» da bugungi darsdagi so'zlarni ishlatib ko'ring.",
    "🗣 Bilganingizni ovoz chiqarib ayting: «Suhbat» ga kiring, hatto bitta jumla ham foyda beradi.",
    "🗣 O'qish yetarli emas: til gapirganda o'rganiladi. «Suhbat» da 10 daqiqa mashq qiling.",
    "🗣 Har darsdan keyin gaplashing: shunda so'zlar xotirada qoladi. «Suhbat» tugmasi sizni kutyapti!",
    "🗣 Xato qilishdan qo'rqmang: xatolar orqali o'rganiladi. «Suhbat» da bugungi darsni gapirib sinab ko'ring.",
    "🗣 Bugungi darsni «Suhbat» da qo'llang. Eng tez o'rganadiganlar har darsdan keyin gaplashadi.",
]
SPEAK_OPEN = ("🎉 «🗣 Suhbat» bo'limi ochildi!\n\nBu yerda boshqa o'quvchi bilan ruscha jonli gaplashasiz (tasodifiy odam, darhol ulanasiz). "
              "Xohlasangiz, suhbat ichida birga «🎮 O'yin» o'ynaysiz: rol olib, navbat bilan gapirasiz, sun'iy intellekt xatolaringizni o'zbekcha tushuntiradi.\n\n"
              "Har darsdan keyin gaplashishni odat qiling: til aynan gapirganda o'rganiladi.")
SPEAK_ALOUD = "🔊 Darsdagi harf va so'zlarni ovoz chiqarib 3 marta takrorlang: eshitib va aytib o'rganish tezroq esda qoladi."


REVIEW_TIPS = [
    "📚 Esdan chiqqan mavzular bo'lsa, pastdagi «Darslarim» tugmasini bosing: tugatgan darslaringiz ro'yxati chiqadi, xohlagan mavzuni tanlab, qayta ko'rishingiz mumkin.",
    "🔁 Takrorlash — bilimning onasi! «Darslarim» tugmasini bosib, o'tgan mavzulardan istaganingizni qayta ochib ko'ring.",
    "📖 Biror mavzu esingizdan chiqqan bo'lsa, xafa bo'lmang: «Darslarim» ga kirib, o'sha darsni tanlang va qayta ko'zdan kechiring.",
    "🧩 Eski mavzularga ko'z yugurtirib turing: «Darslarim» tugmasi orqali xohlagan darsingizni istalgan payt qayta ochasiz.",
]


def review_tip():
    return "\n\n" + random.choice(REVIEW_TIPS)


def speak_tip(uid, was_blocked):
    """Dars tugagach: suhbatni eslatadi (ochilgan bo'lsa), yangi ochilgan bo'lsa tushuntiradi."""
    if not (TALK_URL and TALK_SECRET):
        return ""
    now_blocked = talk_blocked(uid)
    if was_blocked and not now_blocked:
        return "\n\n" + SPEAK_OPEN
    if not now_blocked:
        return "\n\n" + random.choice(SPEAK_TIPS)
    return "\n\n" + SPEAK_ALOUD


def next_unlock(ts):
    """Dars tugagan paytdan keyingi birinchi ertangi kun, soat UNLOCK_HOUR:00 (Toshkent)."""
    d = datetime.fromtimestamp(ts, TZ).date() + timedelta(days=1)
    return int(datetime.combine(d, dtime(UNLOCK_HOUR), TZ).timestamp())


INTRO_LESSONS = 2  # A0 ning birinchi 2 darsi (alifbo, sanash): bepul, bepul hisobiga kirmaydi, kutmasdan o'tiladi


def intro_ids(lang="ru"):
    return [r[0] for r in db.execute("SELECT id FROM lessons WHERE level='A0' AND COALESCE(lang,'ru')=? ORDER BY COALESCE(ord, 1e9 + id), id LIMIT ?",
                                     (lang, INTRO_LESSONS))]


def finish_lesson(uid, lid):
    """Darsni tugatilgan deb belgilaydi va keyingi darsni ertaga soat 12:00 ga qoldiradi.
    Istisno: alifbo va sanash darslari (A0 ning ilk 2 tasi) ketma-ket, kutmasdan o'tiladi."""
    db.execute("INSERT OR IGNORE INTO progress(user_id, lesson_id) VALUES(?, ?)", (uid, lid))
    intro = intro_ids(ulang(uid))
    done = {r[0] for r in db.execute("SELECT lesson_id FROM progress WHERE user_id=?", (uid,))}
    if not (lid in intro and any(i not in done for i in intro)):
        db.execute("UPDATE users SET next_at=?, notified=0 WHERE user_id=?", (next_unlock(now()), uid))
    db.commit()


def done_count(uid):
    """Bepul dars hisobiga kiradigan tugatilgan darslar (o'tkazib yuborilgan va alifbo/sanash darslarisiz)."""
    ids = intro_ids(ulang(uid))
    ph = ",".join("?" * len(ids)) or "NULL"
    return db.execute(f"SELECT COUNT(*) FROM progress WHERE user_id=? AND COALESCE(skipped,0)=0 AND lesson_id NOT IN ({ph})",
                      (uid, *ids)).fetchone()[0]


def finished_count(uid):
    return db.execute("SELECT COUNT(*) FROM progress WHERE user_id=? AND COALESCE(skipped,0)=0", (uid,)).fetchone()[0]


# ---------------- A0: alifbo va sanashni so'rash, darsdan boshlash ----------------
def need_placement(uid):
    goal, level = user(uid)
    if ulang(uid) != "ru" or level != "A0" or meta_get(f"pl{uid}"):   # alifbo/sanash savoli faqat A0 (noldan) tanlaganlarga; A1 natijali odam hech qachon A0 ga tushmaydi
        return False
    return db.execute("SELECT COUNT(*) FROM progress WHERE user_id=?", (uid,)).fetchone()[0] == 0


def pl_kb(prefix):
    return kb_of([("✅ Ha, bilaman", prefix + ":1"), ("❌ Yo'q", prefix + ":0")])


PL_Q1 = "🔤 Rus alifbosini (harflarini) bilasizmi?"
PL_Q2 = "🔢 Rus tilida sanashni bilasizmi?"


@dp.callback_query(F.data.startswith("pl:"))
async def placement_cb(c: CallbackQuery):
    uid = c.from_user.id
    p = c.data.split(":")
    if not need_placement(uid):
        await c.answer("Bu savol allaqachon hal bo'lgan")
        return
    if p[1] == "a":  # alifbo javobi -> sanash savoli
        await edit(c, PL_Q2, pl_kb(f"pl:c:{p[2]}"))
        return await c.answer()
    a, cnt = p[2] == "1", p[3] == "1"
    skip = 0 if not a else (2 if cnt else 1)  # alifbo yo'q: 1-dars; alifbo bor, sanash yo'q: 2-dars; ikkalasi bor: 3-dars
    a0 = lessons_of("A0")
    if skip >= len(a0) and not any(lessons_of(lv) for lv in LESSON_LEVELS[1:]):
        skip = max(len(a0) - 1, 0)  # yuqori darajalarda dars yo'q: o'quvchini bo'sh joyga tushirmaymiz
    for lid, _u, _t in lessons_of("A0")[:skip]:
        db.execute("INSERT OR IGNORE INTO progress(user_id, lesson_id, skipped) VALUES(?, ?, 1)", (uid, lid))
    meta_set(f"pl{uid}", f"{int(a)}{int(cnt)}")
    db.commit()
    start = f"{skip + 1}-darsdan" + (" (alifbo)" if skip == 0 else " (sanash)" if skip == 1 else "")
    await edit(c, f"✅ Tushunarli! Siz A0 ning {start} boshlaysiz.", None)
    await c.answer()
    await show_lesson(c.message, uid)


def is_paid(uid):
    r = db.execute("SELECT paid, COALESCE(paid_until,0) FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r[0] and (r[1] == 0 or r[1] > time.time()))


def is_lifetime(uid):
    r = db.execute("SELECT paid, COALESCE(paid_until,0) FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r[0] and r[1] == 0)


def grant_vip(uid, days=0):
    """days=0 -> umrbod. Muddatli bo'lsa, amaldagi muddatga qo'shiladi."""
    if days <= 0:
        db.execute("UPDATE users SET paid=1, paid_until=0 WHERE user_id=?", (uid,))
        return 0
    r = db.execute("SELECT paid, COALESCE(paid_until,0) FROM users WHERE user_id=?", (uid,)).fetchone()
    start = max(int(time.time()), r[1] if r and r[0] and r[1] else 0)
    until = start + days * 86400
    db.execute("UPDATE users SET paid=1, paid_until=? WHERE user_id=?", (until, uid))
    return until


def paywalled(uid):
    return PAID_MODE and not is_paid(uid) and done_count(uid) >= FREE_LESSONS


def locked(uid):
    return bool(wait_until(uid)) or paywalled(uid)


def price_line(uid, plan="3"):
    amount, note = price_for(uid, plan) if uid else (PLANS[plan][2], "")
    base = PLANS[plan][2]
    if not amount:
        return f"💰 Narx: {PRICE}"
    s = f"💰 Narx: {money(amount)}"
    if amount != base:
        s = f"💰 Narx: {money(base)} → {money(amount)}"
    return s + (f"\n{note}" if note else "")


def plans_text(uid=None):
    out = []
    for k in ("1", "3", "12"):
        nm, days, base = PLANS[k]
        am = price_for(uid, k)[0] if uid else base
        out.append(f"• {nm}: " + (f"{money(base)} → {money(am)}" if am != base else money(am)))
    return "\n".join(out)


def pay_info(uid=None, plan="3"):
    nm, days, _ = PLANS.get(plan, PLANS["L"])
    return (f"💎 VIP — {nm}" + (f" ({days} kun)" if days else " (bir marta to'lov)") + "\n"
            "🇷🇺 rus va 🇬🇧 ingliz tili, ikkalasi ham ochiladi.\n\n"
            f"{price_line(uid, plan)}\n\n"
            + (f"🎁 Yoki bepul: {REF_NEED} ta do'stingizni taklif qiling.\n\n" if REF_NEED > 0 else "")
            + f"💳 Karta: {CARD}\n"
            "To'lagach, chek rasmini shu yerga yuboring. Admin tekshirgach, VIP darhol ochiladi.\n\n"
            "🎟 Promokod bo'lsa: /kod KODINGIZ")


def pay_text(uid=None):
    return (f"🔒 {free_done()} tugadi, zo'r ketyapsiz!\n"
            f"VIP — rus + ingliz tili:\n{plans_text(uid)}" + (f"\n🎁 Yoki {REF_NEED} ta do'st taklif qiling (bepul)." if REF_NEED > 0 else "") + " 👇")


def pro_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="💎 VIP ga o'tish", callback_data="pro:start")]])


def vip_kb(uid):
    """Bepul foydalanuvchiga VIP tugmasi (to'lagan yoki pulli rejim o'chiq bo'lsa tugma yo'q)."""
    return pro_kb() if PAID_MODE and not is_paid(uid) else None


awaiting = set()  # "VIP ga o'tish" ni bosib, chek yuborishi kutilayotganlar


def wait_until(uid):
    if is_paid(uid):  # VIP kutmaydi: istagancha dars o'tadi
        return 0
    r = db.execute("SELECT next_at FROM users WHERE user_id=?", (uid,)).fetchone()
    return r[0] if r and r[0] and r[0] > now() else 0


def wait_text(ts):
    d = datetime.fromtimestamp(ts, TZ)
    left = max(1, -(-(ts - now()) // 60))
    return (f"⏳ Bugungi dars tugadi. Keyingi dars ertaga soat {d:%H:%M} da ochiladi "
            f"({left // 60} soat {left % 60} daqiqadan keyin). Dam oling. Esdan chiqqan mavzularni «📚 Darslarim» orqali qayta ko'rib chiqing 🙂")


def lessons_of(level, lang="ru"):
    return db.execute("SELECT id, url, test_url FROM lessons WHERE level=? AND COALESCE(lang,'ru')=? ORDER BY COALESCE(ord, 1e9 + id), id",
                      (level, lang)).fetchall()


def eff(level):  # o'quvchi o'z darajasidan boshlaydi (A0 ham alohida daraja)
    return level if level in LESSON_LEVELS else "A1"


def current(uid):
    """Keyingi dars: (daraja, (id, url, test_url) yoki None, nechanchi, jami, yangi_daraja).
    Daraja darslari tugasa, daraja avtomatik keyingisiga oshadi."""
    lvl, moved, lang = eff(user(uid)[1]), None, ulang(uid)
    done = {r[0] for r in db.execute("SELECT lesson_id FROM progress WHERE user_id=?", (uid,))}
    while True:
        ls = lessons_of(lvl, lang)
        left = [x for x in ls if x[0] not in done]
        if left:
            return lvl, left[0], len(ls) - len(left) + 1, len(ls), moved
        i = LESSON_LEVELS.index(lvl)
        if not ls or i == len(LESSON_LEVELS) - 1:
            return lvl, None, len(ls), len(ls), moved
        lvl = moved = LESSON_LEVELS[i + 1]
        db.execute("UPDATE users SET level=? WHERE user_id=?", (lvl, uid))
        db.commit()


def lesson_kb(lesson, uid=None):
    if uid and site_lessons(uid):  # VIP: bitta tugma. Hammasi Rumiyo saytida: salom, dars, test, suhbat
        intro = intro_pending(uid)
        if intro:
            meta_set(intro_key(uid), "1")
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="📖 Darsni ochish", web_app=WebAppInfo(url=rm_link(uid, lesson, 0, 0, intro=intro, go=not intro)))]])
    rows = [[InlineKeyboardButton(text="📖 Darsni ochish", web_app=WebAppInfo(url=lesson[1]))]]
    if lesson[2]:  # test linki bor: dars testdan o'tgandan keyin tugaydi
        rows.append([InlineKeyboardButton(text="📝 O'qidim, testga o'tish", callback_data=f"read:{lesson[0]}")])
    else:  # eski darslar (test linki yo'q): o'quvchi o'zi tugatdim deydi
        rows.append([InlineKeyboardButton(text="✅ Darsni tugatdim", callback_data=f"done:{lesson[0]}")])
    if PAID_MODE and uid and not is_paid(uid):  # bepul foydalanuvchiga har doim tugma ko'rinadi
        rows.append([InlineKeyboardButton(text="💎 VIP ga o'tish", callback_data="pro:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_lesson(m: Message, uid, skip_intro=False):
    if need_placement(uid):
        return await m.answer("Boshlashdan oldin ikkita savol:\n\n" + PL_Q1, reply_markup=pl_kb("pl:a"))
    if paywalled(uid):
        return await m.answer(pay_text(uid), reply_markup=pro_kb())
    w = wait_until(uid)
    if w:
        return await m.answer(wait_text(w), reply_markup=vip_kb(uid))
    lvl, lesson, n, total, moved = current(uid)
    if moved:
        await m.answer(level_up_text(moved), parse_mode="HTML")
    if lesson is None:
        return await m.answer(f"{lvl} darajasi darslari tez orada qo'shiladi. Biroz kuting 🙂" if total == 0
                              else "🏆 Siz hozircha barcha darslarni tugatdingiz!", reply_markup=vip_kb(uid))
    await m.answer(f"📚 {lvl} · {n}-dars (jami {total} ta)", reply_markup=lesson_kb(lesson, uid))


def intro_key(uid):
    return f"intro{uid}_{datetime.now(TZ).strftime('%Y-%m-%d')}"


def site_lessons(uid):
    """VIP uchun dars Rumiyo saytining ichida o'tadi (salom, trener, test, suhbat)."""
    return bool(RUMIYO_URL and TALK_SECRET and os.getenv("SUHBAT_ON") != "1" and os.getenv("INTRO_OFF") != "1"
                and (is_paid(uid) or is_admin(uid)))


def intro_pending(uid):
    """Bugun Rumiyo bilan kirish suhbati hali bo'lmagan bo'lsa True."""
    return site_lessons(uid) and not meta_get(intro_key(uid))


@dp.callback_query(F.data == "intro:skip")
async def intro_skip(c: CallbackQuery):
    meta_set(intro_key(c.from_user.id), "1")
    await c.answer()
    await show_lesson(c.message, c.from_user.id, skip_intro=True)


@dp.message(Command("dars"))
async def cmd_lesson(m: Message):
    if not user(m.from_user.id)[1]:
        return await m.answer("Avval /start bosib, darajangizni aniqlang.")
    await show_lesson(m, m.from_user.id)


def test_url_for(lesson):
    return lesson[2] + ("&" if "?" in lesson[2] else "?") + f"l={lesson[0]}"


@dp.callback_query(F.data.startswith("read:"))
async def lesson_read(c: CallbackQuery):
    if locked(c.from_user.id):
        return await c.answer("Keyingi dars hali ochilmagan", show_alert=True)
    lesson = current(c.from_user.id)[1]
    if lesson is None or not lesson[2] or str(lesson[0]) != c.data.split(":", 1)[1]:
        return await c.answer("Bu dars allaqachon tugagan")
    kb = test_menu(test_url_for(lesson))
    await c.message.answer(f"Testdan kamida {int(PASS * 100)}% to'g'ri javob bersangiz, keyingi darsga o'tasiz. "
                           "Chat tagidagi «📝 Testni boshlash» tugmasini bosing 👇", reply_markup=kb)
    await c.answer()


def lesson_result(raw):
    """Dars testi natijasi: {"l": dars_id, "s": to'g'ri, "n": jami}. Boshqa JSON bo'lsa None."""
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict) or "l" not in d:
        return None
    vals = [d.get(k) for k in ("l", "s", "n")]
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
        return (None, 0, 0)
    return tuple(vals)


async def got_lesson_test(m, uid, res, via_site=False):
    lid, s, n = res
    if isinstance(lid, int) and lid <= -1000:   # daraja imtihoni (-1002..-1006) yoki hafta sinovi (-2000)
        return await got_exam(m, uid, lid, s, n)
    lesson = current(uid)[1] if user(uid)[1] and not locked(uid) else None
    if lesson is None or lid != lesson[0] or not lesson[2]:
        return await m.answer("Bu test hozirgi darsingizga mos kelmadi. /dars ni bosing.", reply_markup=main_menu())
    if n < 1 or not 0 <= s <= n:
        return await m.answer("Natijani o'qib bo'lmadi. Testni qaytadan boshlang.")
    if s / n < PASS:
        return await m.answer(f"📊 Natija: {s}/{n}. O'tish uchun kamida {int(PASS * 100)}% kerak.\n"
                              "Darsni yana bir bor o'qib, testni qayta topshiring 👇")
    was = talk_blocked(uid)
    _lv, _l, _no, _tot, _mv = current(uid)
    finish_lesson(uid, lid)
    await m.answer(f"✅ Zo'r! Natija: {s}/{n}. {_lv} · {_no}-dars tugadi." + speak_tip(uid, was) + review_tip(), reply_markup=main_menu())
    if RUMIYO_URL and TALK_SECRET and (is_paid(uid) or is_admin(uid)) and not via_site:
        await m.answer(f"🗣 Bugun o'rganganingizni o'zingiz tanlagan ustoz bilan {'inglizcha' if ulang(uid) == 'en' else 'ruscha'} gaplashib mustahkamlang (10 daqiqa). Ustozni tanlaysiz: do'stona, jahldor, ehtirosli yoki shirin so'z 🙂",
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗣 AI bilan suhbat", web_app=WebAppInfo(url=rm_link(uid, lesson, s, n)))]]))
    try:
        if level_finished(uid, _lv) and _lv in EXAM_LEVELS and not meta_get(f"ex{uid}_{ulang(uid)}_{_lv}"):
            await offer_exam(m, uid, _lv, intro=f"🎉 {_lv} darajasi darslari tugadi!\n\n")
    except Exception as e:
        print("imtihon taklifi:", e)
    await show_lesson(m, uid)


@dp.callback_query(F.data.startswith("done:"))
async def lesson_done(c: CallbackQuery):
    uid = c.from_user.id
    lesson = current(uid)[1] if not locked(uid) else None
    if lesson is None or lesson[2] or str(lesson[0]) != c.data.split(":", 1)[1]:
        return await c.answer("Bu dars allaqachon tugagan")
    was = talk_blocked(uid)
    finish_lesson(uid, lesson[0])
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await c.answer("✅ Zo'r!")
    tip = speak_tip(uid, was) + review_tip()
    await c.message.answer(tip.strip())
    await show_lesson(c.message, uid)


# ---------------- daraja imtihoni va hafta sinovi ----------------
EXAM_PASS = 0.75
EXAM_LEVELS = ["A1", "A2", "B1", "B2", "C1"]


def _tbase(uid):
    b = (EN_TRENER_URL if ulang(uid) == "en" else TRENER_URL).strip()
    return b if b.endswith("/") else b + "/"


def exam_url(uid, lvl):
    return f"{_tbase(uid)}?d=1&m=exam&lv={lvl}&l=-{1001 + LESSON_LEVELS.index(lvl)}"


def week_url(uid):
    return f"{_tbase(uid)}?d=1&m=week&l=-2000"


def wk_link(uid):
    """VIP uchun haftalik og'zaki sinov: Rumiyo bilan ~10 daqiqa ovozli suhbat. Havolada oxirgi tugatilgan darslarning kun raqamlari (dl)."""
    import base64, hashlib, hmac
    days = []
    for (u,) in db.execute("SELECT l.url FROM progress p JOIN lessons l ON l.id=p.lesson_id WHERE p.user_id=? AND COALESCE(p.skipped,0)=0 "
                           "ORDER BY p.rowid DESC LIMIT 6", (uid,)).fetchall():
        d = re.search(r"[?&]d=(\d+)", u or "")
        if d:
            days.append(int(d.group(1)))
    body = base64.urlsafe_b64encode(json.dumps({"u": uid, "l": eff(user(uid)[1]), "e": int(time.time()) + 86400, "d": days[0] if days else 0, "dl": days,
                                                "g": ulang(uid), "t": "Hafta sinovi", "s": 0, "n": 0, "v": 1, "h": 5, "o": (user(uid)[0] or "")[:40]},
                                               separators=(",", ":"), ensure_ascii=False).encode()).decode().rstrip("=")
    sig = hmac.new(TALK_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{RUMIYO_URL.rstrip('/')}/?t={body}.{sig}"


def week_id():
    return datetime.now(TZ).strftime("%G-%V")


def level_finished(uid, lvl):
    ls = lessons_of(lvl, ulang(uid))
    if not ls:
        return False
    done = {r[0] for r in db.execute("SELECT lesson_id FROM progress WHERE user_id=?", (uid,))}
    return all(x[0] in done for x in ls)


async def offer_exam(m, uid, lvl, intro=""):
    if lvl not in EXAM_LEVELS:
        return
    i = EXAM_LEVELS.index(lvl)
    nxt = EXAM_LEVELS[i + 1] if i + 1 < len(EXAM_LEVELS) else None
    await m.answer(intro + f"🎓 {lvl} daraja imtihoni: 30 ta savol (eshitish, tarjima, gap tuzish), o'tish {int(EXAM_PASS * 100)}%.\n"
                   + (f"Topshirsangiz {nxt} darajasi ochiladi va " if nxt else "Topshirsangiz ") + "sertifikat olasiz 🏅\n"
                   "Chat tagidagi «📝 Testni boshlash» tugmasini bosing 👇", reply_markup=test_menu(exam_url(uid, lvl)))


@dp.message(Command("imtihon"))
async def cmd_exam(m: Message):
    uid = m.from_user.id
    lvl = eff(user(uid)[1])
    if lvl not in EXAM_LEVELS:
        lvl = "A1"
    await offer_exam(m, uid, lvl)


@dp.message(Command("hafta"))
async def cmd_week(m: Message):
    uid = m.from_user.id
    if finished_count(uid) < 2:
        return await m.answer("Hafta sinovi uchun avval kamida 2 ta dars o'ting 🙂")
    await m.answer("🏅 Hafta sinovi: o'rgangan gaplaringizdan ~15 ta savol. Jazo yo'q: xatolar takrorlashga qaytadi.\n"
                   "Chat tagidagi «📝 Testni boshlash» tugmasini bosing 👇", reply_markup=test_menu(week_url(uid)))


async def got_exam(m: Message, uid, lid, s, n):
    if not (isinstance(s, int) and isinstance(n, int)) or n < 5 or not 0 <= s <= n:
        return await m.answer("Natijani o'qib bo'lmadi. Testni qaytadan boshlang.", reply_markup=main_menu())
    pct = round(100 * s / n)
    lang = ulang(uid)
    if lid == -2000:   # hafta sinovi
        meta_set(f"wkp{uid}", week_id())
        meta_set(f"wk{uid}", f"{int(time.time())}:{pct}")
        tip = ("🔥 Ajoyib! O'rgangan gaplaringiz yodingizda." if pct >= 80 else
               "👍 Yaxshi! Xato qilgan gaplaringiz ertaga takrorlashda qaytadi." if pct >= 50 else
               "💪 Hech gap yo'q: xato gaplar takrorlashga qaytdi, har kuni oz-ozdan davom eting.")
        return await m.answer(f"🏅 Hafta sinovi: {s}/{n} ({pct}%)\n{tip}", reply_markup=main_menu())
    idx = -lid - 1001
    if not 0 <= idx < len(LESSON_LEVELS) or LESSON_LEVELS[idx] not in EXAM_LEVELS:
        return await m.answer("Bu test mos kelmadi. /imtihon ni bosing.", reply_markup=main_menu())
    lvl = LESSON_LEVELS[idx]
    if s / n < EXAM_PASS:
        return await m.answer(f"📊 {lvl} imtihoni: {s}/{n} ({pct}%). O'tish uchun {int(EXAM_PASS * 100)}% kerak.\n"
                              "Zaif gaplarni 🔁 takrorlab, qayta urinib ko'ring (/imtihon). Siz uddalaysiz 💪", reply_markup=main_menu())
    meta_set(f"ex{uid}_{lang}_{lvl}", f"{s}/{n}")
    nxt = EXAM_LEVELS[EXAM_LEVELS.index(lvl) + 1] if lvl != EXAM_LEVELS[-1] else None
    up = ""
    cur = eff(user(uid)[1])
    if nxt and LESSON_LEVELS.index(cur) <= LESSON_LEVELS.index(lvl):
        db.execute("UPDATE users SET level=?, next_at=0, notified=1 WHERE user_id=?", (nxt, uid))
        db.commit()
        up = f"\n🚀 Daraja oshdi: {nxt}! Yangi dars ochildi."
    await m.answer(f"🎓 TABRIKLAYMIZ!\n\n{LANG_NAMES[lang]} · {lvl} daraja imtihoni topshirildi\n📊 Natija: {s}/{n} ({pct}%)\n"
                   f"🏅 Sertifikat: {lvl} ✅{up}\n\nShu xabarni do'stlaringizga yuboring, ular ham sinab ko'rsin 👇", reply_markup=main_menu())
    if nxt:
        await show_lesson(m, uid)


async def weekly_loop():
    """Yakshanba kuni kunduzi: har bir faol o'quvchiga hafta sinovi taklifi (haftasiga 1 marta)."""
    while True:
        try:
            d = datetime.now(TZ)
            if d.weekday() == 6 and 10 <= d.hour < 20:
                wid = week_id()
                for (uid,) in db.execute("SELECT user_id FROM users WHERE level IS NOT NULL").fetchall():
                    if meta_get(f"wkp{uid}") == wid or finished_count(uid) < 5 or paywalled(uid):
                        continue
                    meta_set(f"wkp{uid}", wid)
                    fc = finished_count(uid)
                    wk = max(0, fc - int(meta_get(f"wkc{uid}") or fc))      # shu haftada tugatilgan darslar
                    meta_set(f"wkc{uid}", str(fc))
                    try:
                        if wk > 0:
                            await send_voice_msg(uid, "weekly", idle=0, extra={"kind": "weekly", "week_done": wk})   # ovozli hisobot: Rumiyo o'zi gapiradi
                    except Exception as e:
                        print("haftalik ovoz:", type(e).__name__)
                    if (is_paid(uid) or is_admin(uid)) and RUMIYO_URL.startswith("https://") and TALK_SECRET:
                        await send_safe(lambda uid=uid: bot.send_message(
                            uid, "🏅 Hafta sinovi! Bu safar Rumiyo sizni og'zaki tekshiradi: o'tgan hafta o'rgangan gaplaringizdan taxminan 10 daqiqalik "
                                 "ovozli o'yin. Jazo yo'q, qaysi gaplar qiyin bo'lganini birga ko'ramiz.",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗣 Rumiyo bilan sinov", web_app=WebAppInfo(url=wk_link(uid)))]])))
                    else:
                        await send_safe(lambda uid=uid: bot.send_message(
                            uid, "🏅 Hafta sinovi! O'tgan hafta o'rgangan gaplaringizdan 5 daqiqalik o'yin. Jazo yo'q, natija sizga ko'rinadi.\n"
                                 "Chat tagidagi «📝 Testni boshlash» tugmasini bosing 👇", reply_markup=test_menu(week_url(uid))))
                    await asyncio.sleep(0.1)
        except Exception as e:
            print("hafta sinovi xatosi:", e)
        await asyncio.sleep(1800)


class _SiteMsg:
    """Rumiyo saytidan kelgan natijani oddiy xabar kabi qayta ishlash uchun (m.answer -> bot.send_message)."""
    def __init__(self, uid):
        self.uid = uid
        self.from_user = None

    async def answer(self, text, **kw):
        return await bot.send_message(self.uid, text, **kw)


def site_cursor(data, key):
    """Sayt bazasi qayta yaratilgan bo'lsa (Render qayta ishga tushdi) "o'qilgan raqam"ni nolga qaytaradi. Joriy raqamni qaytaradi."""
    last = int(meta_get(key) or 0)
    ep = (data or {}).get("epoch")
    seq = (data or {}).get("seq")
    old_ep = meta_get("site_epoch")
    reset = False
    if ep and old_ep and old_ep != ep:
        reset = True
    elif isinstance(seq, int) and seq < last:
        reset = True
    if ep and old_ep != ep:
        meta_set("site_epoch", ep)
    if reset:
        print(f"sayt bazasi yangilangan: {key} {last} -> 0")
        meta_set("siteres_pl", "0")
        meta_set("siteres_last", "0")
        return 0, True
    return last, False


async def site_get(path, params):
    async with aiohttp.ClientSession() as http, http.get(
            RUMIYO_URL.rstrip("/") + path, params=params, headers={"X-Secret": TALK_SECRET},
            timeout=aiohttp.ClientTimeout(total=25)) as r:
        return await r.json() if r.status == 200 else {}


async def apply_site_placements(force_uid=None):
    """Rumiyo saytida o'tgan daraja testlarini olib: darajani saqlaydi, natijani yuboradi, 'hozir/ertaga' qaroriga qarab davom ettiradi.
    force_uid: o'quvchi «Darsni boshlash»ni bosdi, natijasini raqamga qaramay to'g'ridan-to'g'ri so'raymiz."""
    if not (RUMIYO_URL and TALK_SECRET):
        return
    if force_uid:
        data = await site_get("/admin/placements", {"u": force_uid})
        last = None
    else:
        data = await site_get("/admin/placements", {"after": int(meta_get("siteres_pl") or 0)})
        last, reset = site_cursor(data, "siteres_pl")
        if reset:
            data = await site_get("/admin/placements", {"after": 0})      # raqam nolga qaytdi: yangi natijalarni qayta so'raymiz
    for it in data.get("results", []):
        if last is not None:
            last = max(last, int(it["id"]))
            meta_set("siteres_pl", str(last))
        uid = int(it["u"])
        try:
            if user(uid)[1]:
                continue                       # daraja allaqachon bor
            if force_uid and it.get("ts") and now() - int(it["ts"]) > 7200:
                continue                       # eski natija (masalan, nolga qaytarishdan oldingi) qayta tiklanmasin
            res = result_text(it.get("raw") or "")
            if not res:
                continue
            level, text = res
            try:
                if json.loads(it["raw"]).get("skip"):
                    meta_set(f"sk{uid}", "1")
            except Exception:
                pass
            db.execute("INSERT INTO users(user_id, level) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET level=excluded.level", (uid, level))
            db.commit()
            await bot.send_message(uid, text, reply_markup=main_menu())
            await ref_credit(uid)
            if it.get("later"):
                db.execute("UPDATE users SET next_at=?, notified=0 WHERE user_id=?", (next_unlock(now()), uid))
                db.commit()
                await bot.send_message(uid, "🌙 Mayli, birinchi dars ertaga tayyor bo'ladi. Vaqti kelganda o'zim yozaman. Ertagacha ko'rishguncha!")
            else:
                meta_set(f"plsent{uid}", str(now()))
                meta_set(intro_key(uid), "1")          # bugun Rumiyo bilan allaqachon gaplashdi
                await show_lesson(_SiteMsg(uid), uid)
        except Exception as e:
            print("sayt daraja natijasi:", type(e).__name__, e)


async def site_results_loop():
    """Rumiyo saytida (trener ichida) yechilgan test natijalarini ~20 soniyada bir marta olib, darsni tugatadi."""
    await asyncio.sleep(15)
    while True:
        try:
            await apply_site_placements()
        except Exception as e:
            print("sayt daraja so'rovi:", type(e).__name__)
        try:
            if RUMIYO_URL and TALK_SECRET:
                data = await site_get("/admin/results", {"after": int(meta_get("siteres_last") or 0)})
                last, reset = site_cursor(data, "siteres_last")
                if reset:
                    data = await site_get("/admin/results", {"after": 0})
                for it in data.get("results", []):
                    last = max(last, int(it["id"]))
                    meta_set("siteres_last", str(last))
                    try:
                        await got_lesson_test(_SiteMsg(int(it["u"])), int(it["u"]), (int(it["l"]), int(it["s"]), int(it["n"])), via_site=True)
                        w = [str(x) for x in (it.get("w") or []) if str(x).strip()][:3]
                        if w:     # Rumiyo nimani eslab qolganini odamga ko'rsatadi: ishonch va "aqlli" taassurot
                            await bot.send_message(int(it["u"]), "🧠 Rumiyo eslab qoldi. Bugun shu gaplarda adashdingiz:\n" + "\n".join("• " + x for x in w) +
                                                   "\n\nBularni keyingi darslar ichida o'zim sizga qaytarib turaman, to'liq yodlaguncha.")
                    except Exception as e:
                        print("sayt natijasi:", type(e).__name__, e)
        except Exception as e:
            print("sayt natijalari so'rovi:", type(e).__name__)
        await asyncio.sleep(20)


# ---------------- admin panel ----------------
PAGE = 30


def is_admin(uid):
    return uid in ADMIN_IDS


def valid_url(u):
    p = urlparse(u)
    return p.scheme == "https" and bool(p.netloc) and " " not in u and len(u) < 500


def short(u):
    return u[8:] if u.startswith("https://") else u


def admin_home():
    rows = [InlineKeyboardButton(text=f"{lv} ({len(lessons_of(lv))})", callback_data=f"adm:lv:{lv}")
            for lv in LESSON_LEVELS]
    extra = [[InlineKeyboardButton(text="📢 Hammaga xabar", callback_data="adm:bc")],
             [InlineKeyboardButton(text="📋 Linklar", callback_data="adm:links")]]
    extra.append([InlineKeyboardButton(text="🔔 Majburiy obuna", callback_data="adm:sub"),
                  InlineKeyboardButton(text="☁️ Zaxira kanali", callback_data="adm:cloud")])
    extra.append([InlineKeyboardButton(text="🧠 AI ni o'qitish", callback_data="adm:rm"),
                  InlineKeyboardButton(text="🔑 Kalitlar", callback_data="adm:rk")])
    return InlineKeyboardMarkup(inline_keyboard=[rows[0:2], rows[2:4], rows[4:6]] + extra)


def level_view(lv, page=0):
    ls = lessons_of(lv)
    pages = max(1, -(-len(ls) // PAGE))
    page = min(max(page, 0), pages - 1)
    chunk = ls[page * PAGE:(page + 1) * PAGE]
    text = f"📂 {lv} darslari: {len(ls)} ta" + (f" (sahifa {page + 1}/{pages})" if pages > 1 else "") + "\n\n"
    tp = dict(db.execute("SELECT id, topic FROM lessons WHERE level=?", (lv,)).fetchall())
    text += "\n".join(f"{page * PAGE + i}. " + (f"📚 {tp[lid][:40]}" if tp.get(lid) else short(u)[:45]) + ("" if t else " ⚠️ testsiz")
                      for i, (lid, u, t) in enumerate(chunk, 1)) if chunk else "Hali dars yo'q."
    rows, row = [], []
    for i, (lid, _, _t) in enumerate(chunk, 1):
        row.append(InlineKeyboardButton(text=str(page * PAGE + i), callback_data=f"adm:ls:{lid}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm:lv:{lv}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm:lv:{lv}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="➕ Qo'shish", callback_data=f"adm:add:{lv}")])
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="adm:home")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def kb_of(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows])


async def edit(c: CallbackQuery, text, kb):
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:  # xabar o'zgarmagan bo'lsa
        pass


@dp.message(Command("hammasini_nolla"))
async def reset_all(m: Message):
    """Admin: hamma foydalanuvchi (o'zingiz ham) noldan boshlaydi. Darslar, promokodlar, kalitlar va sozlamalar qoladi."""
    if not is_admin(m.from_user.id):
        return
    if (m.text or "").split(maxsplit=1)[1:] != ["HA"]:
        n = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        v = db.execute("SELECT COUNT(*) FROM users WHERE paid=1").fetchone()[0]
        return await m.answer(f"⚠️ {n} ta foydalanuvchi ({v} ta VIP), ularning darajasi, to'lovlari, takliflari va pullari O'CHADI.\n"
                              "Darslar, promokodlar, kalitlar qoladi. Avval zaxira fayl yuboriladi.\n\n"
                              "Tasdiqlash uchun yozing:\n/hammasini_nolla HA")
    path = os.path.join(BACKUP_DIR, "tozalashdan-oldin.db")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    snapshot(path)
    await m.answer_document(FSInputFile(path, filename="tozalashdan-oldin.db"), caption="💾 Eski baza. Saqlab qo'ying (kerak bo'lsa qaytarsa bo'ladi).")
    for t in ("users", "progress", "payments", "pay_wait", "talk_use", "referrals", "payouts", "earnings", "user_promo"):
        db.execute(f"DELETE FROM {t}")
    for (k,) in db.execute("SELECT k FROM meta").fetchall():
        if re.search(r"\d{5,}$", k):                      # foydalanuvchiga tegishli belgilar (sk123456, plsent123456 ...)
            db.execute("DELETE FROM meta WHERE k=?", (k,))
    db.commit()
    seen_users.clear(); flood.clear(); sub_ok.clear(); sub_in.clear()
    await m.answer("✅ Hamma noldan boshladi. Endi /start bosing: o'zingiz ham yangi foydalanuvchisiz.", reply_markup=ReplyKeyboardRemove())


@dp.message(Command("admin"))
async def admin(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return await m.answer(f"Bu bo'lim faqat admin uchun.\nSizning ID: {uid}")
    pending.pop(uid, None)
    await m.answer("🛠 Admin panel\nDaraja bo'limini tanlang:", reply_markup=admin_home())


@dp.callback_query(F.data.startswith("adm:"))
async def admin_cb(c: CallbackQuery):
    uid = c.from_user.id
    if not is_admin(uid):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    p = c.data.split(":")
    act = p[1]
    pending.pop(uid, None)  # har qanday tugma kutilayotgan kiritishni bekor qiladi
    if act == "home":
        await edit(c, "🛠 Admin panel\nDaraja bo'limini tanlang:", admin_home())
    elif act == "bc":
        pending[uid] = ("bc",)
        n = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        await edit(c, f"📢 Hammaga xabar ({n} ta foydalanuvchi).\n\nXabarni yuboring: matn, rasm yoki video (izoh bilan). "
                      "Keyin ko'rib chiqib, tasdiqlaysiz.", kb_of([("❌ Bekor qilish", "adm:home")]))
    elif act == "links":
        await send_links(uid)
    elif act == "rm":
        text, kb = rm_view()
        await edit(c, text, kb)
    elif act in ("rk", "rkdel"):
        pending.pop(uid, None)
        if act == "rkdel" and p[2] in RK_KINDS and p[3].isdigit():
            ks = rk_get(p[2])
            if int(p[3]) < len(ks):
                ks.pop(int(p[3]))
                rk_save(p[2], ks)
                asyncio.create_task(rm_push_keys())
        text, kb = rk_view()
        await edit(c, text, kb)
    elif act == "rkadd":
        mode = p[2] if len(p) > 2 and p[2] in ("strong", "paid") else "free"
        pending[uid] = ("rkey", mode)
        if mode == "strong":
            msg = ("💎 KUCHLI Gemini kalitni (AIza...) yuboring: yangi o'quvchining dastlabki 3 kuni shu kalit va kuchli model bilan ishlaydi. "
                   "Eskisi almashadi. Xabar darhol o'chiriladi.\nKalitni hech kimga va GitHub'ga bermang.")
        elif mode == "paid":
            msg = ("💰 PULLIK Gemini kalitni (AIza...) yuboring: bepul kalitlarning kuchi yetmasa shu ishlaydi (arzon model). "
                   "Eskisi almashadi. Xabar darhol o'chiriladi.\nKalitni hech kimga va GitHub'ga bermang.")
        else:
            msg = ("🆓 Bepul kalitlarni shu yerga yuboring: Groq, OpenRouter yoki Gemini (qaysi ekanini bot o'zi biladi). "
                   "Bir xabarda istalgancha, har qatorga bittadan yozing, yoki ketma-ket yuboring. Xabar darhol o'chiriladi.\n"
                   "Kalitni hech kimga va GitHub'ga bermang.")
        await edit(c, msg, kb_of([("❌ Bekor qilish", "adm:rk")]))
    elif act == "rmadd":
        pending[uid] = ("rm1",)
        await edit(c, "🧠 Yangi gap · 1/2\n\nODAM nima deydi? Shuni yozing (ruscha yoki o'zbekcha).\nMasalan: Salom\n\n"
                      "Bir yo'la ko'p qo'shish: har qatorga bittadan, o'rtasiga => qo'ying:\nSalom => Yana kech keldingmi, necha marta aytdim!",
                   kb_of([("❌ Bekor qilish", "adm:rm")]))
    elif act == "rms" and p[2] in ("#laqab", "#qoida", "#jahl", "#dostona", "#ehtiros", "#shirin"):
        pending[uid] = ("rms", p[2])
        n = db.execute("SELECT COUNT(*) FROM rm_pairs WHERE q=?", (p[2],)).fetchone()[0]
        if p[2] == "#jahl":
            msg = (f"😤 Jahl gaplari (hozir {n} ta)\n\nRumiyoning jahli chiqqanda ishlatadigan koyish va g'azab gaplari. Har qatorga bittadan yozing. Masalan:\n"
                   "Sen garang ekansan-ku!\nEshak ekansan, necha marta aytdim!\nMiyang uyda qoldimi?\n\n"
                   "Rumiyo shu gaplarni o'zbekcha, yozilganidek ishlatadi, o'zidan og'irroq so'kish o'ylab topmaydi. Deyarli har javobda bittasini ishlatadi.")
        elif p[2] == "#laqab":
            msg = (f"🏷 Laqablar (hozir {n} ta)\n\nRumiyo har odamga shu ro'yxatdan har kuni BOSHQA laqab beradi va jahl bilan shu laqab bilan chaqiradi.\n"
                   "Laqablarni yuboring, har qatorga bittadan. Masalan:\nKalla-xon\nDangasa-aka\nJannat-xonim")
        elif p[2] in ("#dostona", "#ehtiros", "#shirin"):
            nm, em, ex = {"#dostona": ("Do'stona ustoz", "😊", "Voy jonim, buni qara-ya, qoyil!\nHechqisi yo'q, azizim, hamma shundan o'tadi.\nYana bir marta ayt-chi, men ishonaman senga!"),
                          "#ehtiros": ("Ehtirosli ustoz", "🔥", "Ahhh, mana shu gap! Yuragim hapriqib ketdi!\nYana, yana, to'xtama, sen buni qila olasan!\nVoy-voy-voy, shunday ham aytiladimi!"),
                          "#shirin": ("Shirin so'z ustoz", "🍯", "Ohh, qanday chiroyli aytdingiz, jonginam!\nHechqisi yo'q, birga qaytaramiz, shoshilmang.\nSizga bu juda yarashadi, davom eting!")}[p[2]]
            msg = (f"{em} {nm} gaplari (hozir {n} ta)\n\nBu ustoz o'quvchi bilan gaplashganda ishlatadigan gaplaringiz: maqtash, hazil, ruhlantirish, tuzatish. Har qatorga bittadan yozing. Masalan:\n"
                   f"{ex}\n\nUstoz bu gaplarning uslubi va so'zlarini olib, vaziyatga qarab ishlatadi. Yodlab takrorlamaydi, o'zidan ham qo'shadi.")
        else:
            msg = (f"📜 Qoidalar (hozir {n} ta)\n\nRumiyoga oddiy o'zbekcha gap bilan tushuntiring, u buni eng muhim ko'rsatma deb bajaradi. Masalan:\n"
                   "Xato qilganda qattiq jahling chiqsin.\nHar safar o'qimaganini bilsang o'shqir.\nTo'g'ri aytsa ham bir oz g'ijin, keyin maqta.\n\nBir xabarda har qatorga bittadan yozing.")
        await edit(c, msg, kb_of([("❌ Bekor qilish", "adm:rm")]))
    elif act == "rmlist":
        text, kb = rm_list(int(p[2]) if len(p) > 2 else 0)
        await edit(c, text, kb)
    elif act == "rmdel":
        db.execute("DELETE FROM rm_pairs WHERE id=?", (int(p[2]),))
        db.commit()
        asyncio.create_task(rm_sync(True))
        text, kb = rm_list(int(p[3]) if len(p) > 3 else 0)
        await edit(c, "🗑 O'chirildi.\n\n" + text, kb)
    elif act == "rmjson":
        await bot.send_document(uid, BufferedInputFile(json.dumps({"settings": rm_settings(), "pairs": rm_all()}, ensure_ascii=False, indent=1).encode("utf-8"),
                                                       filename="rumiyo_baza.json"), caption=f"🧠 Rumiyo bazasi: {rm_count()} ta juftlik. Bu faylni saqlab qo'ying (qo'shimcha zaxira).")
        await c.answer()
    elif act == "rmimp":
        pending[uid] = ("rmimp",)
        await edit(c, "📤 rumiyo_baza.json faylini shu yerga yuboring.\nFayldagi yangi gaplar QO'SHILADI, borlari o'chmaydi.", kb_of([("❌ Bekor qilish", "adm:rm")]))
    elif act == "rmtest":
        await c.answer("Tekshiryapman… (1 daqiqagacha)")
        j = await rm_call("GET", "/admin/selftest")
        txt = ("🔍 Tekshiruv natijasi\n\n" + "\n".join(j["lines"])) if j and j.get("lines") else \
              "⚠️ Server javob bermadi. Render da servis «Live» ekanini va yangi kod yuklanganini tekshiring (1–2 daqiqa kuting), RUMIYO_URL/TALK_URL va TALK_SECRET to'g'riligini ham."
        await edit(c, txt[:3900], kb_of([("🔄 Qayta tekshirish", "adm:rmtest")], [("⬅️ Orqaga", "adm:rm")]))
    elif act == "rmpush":
        ok = await rm_sync(True)
        text, kb = rm_view()
        await edit(c, ("✅ Serverga yuborildi.\n\n" if ok else "⚠️ Serverga yetmadi (RUMIYO_URL / TALK_SECRET ni tekshiring yoki server uxlayapti). Bot o'zi qayta urinadi.\n\n") + text, kb)
    elif act == "backup":
        await send_backup([uid])
    elif act == "sub":
        text, kb = sub_view()
        await edit(c, text, kb)
    elif act == "suba":
        pending[uid] = ("suba",)
        await edit(c, "➕ Majburiy obuna kanali\n\n1) Botni o'sha kanal/guruhga ADMIN qilib qo'shing.\n"
                      "2) Ochiq kanal bo'lsa, nomini yuboring: @mening_kanalim\n\nYopiq guruh/kanal bo'lsa, shunchaki botni unga ADMIN qiling: "
                      "bot o'zi majburiy obunaga qo'shib qo'yadi.",
                   kb_of([("❌ Bekor qilish", "adm:sub")]))
    elif act == "subadd" and len(p) > 2 and re.fullmatch(r"-?\d+", p[2]):
        try:
            chat = await bot.get_chat(int(p[2]))
            me = await bot.me()
            if (await bot.get_chat_member(chat.id, me.id)).status not in ("administrator", "creator"):
                raise ValueError("admin emas")
            link = "" if chat.username else await bot.export_chat_invite_link(chat.id)
        except Exception as e:
            return await c.answer("Qo'shib bo'lmadi: botga «Foydalanuvchilarni taklif qilish» huquqini bering. " + str(e)[:80], show_alert=True)
        entry = ("@" + chat.username) if chat.username else f"{chat.id}|{link}|{(chat.title or 'Guruh')[:40]}"
        chs = sub_channels()
        if entry not in chs:
            chs.append(entry)
            meta_set("sub_channels", json.dumps(chs))
            sub_ok.clear()
        text, kb = sub_view()
        await edit(c, "✅ Majburiy obunaga qo'shildi\n\n" + text, kb)
    elif act == "subd":
        chs = sub_channels()
        if p[2].isdigit() and int(p[2]) < len(chs):
            chs.pop(int(p[2]))
            meta_set("sub_channels", json.dumps(chs))
            sub_ok.clear()
        text, kb = sub_view()
        await edit(c, text, kb)
    elif act in ("groq", "gdel"):
        ks = groq_keys()
        if act == "gdel" and len(p) > 2 and p[2].isdigit() and int(p[2]) < len(ks):
            ks.pop(int(p[2]))
            save_groq_keys(ks)
        pending.pop(uid, None)
        ks = groq_keys()
        lines = [f"{i}. …{k[-4:]}" for i, k in enumerate(ks, 1)]
        rows = [[(f"🗑 {i}-ni o'chirish", f"adm:gdel:{i - 1}")] for i in range(1, len(ks) + 1)]
        await edit(c, "🔑 Groq kalitlari (o'yindagi AI hakam)\n\n" + ("\n".join(lines) if lines else "Hali kalit yo'q.") +
                      "\n\nKalitlar suhbat saytiga botdan o'zi yuboriladi (botning .env ida TALK_URL va TALK_SECRET to'g'ri bo'lsa), har 5 daqiqada yangilanadi. "
                      "Ko'proq kalit = o'yinga ko'proq odam sig'adi.\nKalitlar bazada emas, keys.json da saqlanadi.",
                   kb_of(*rows, [("➕ Kalit qo'shish", "adm:gadd")], [("⬅️ Orqaga", "adm:home")]))
    elif act == "gadd":
        pending[uid] = ("groq",)
        await edit(c, "Yangi Groq kalitini (gsk_ bilan boshlanadi) yuboring. Xabar darhol o'chiriladi.\n"
                      "Faqat o'zingizning Groq tashkilotingiz kalitlarini qo'shing.\nKalitni console.groq.com dan oling, hech kimga va GitHub'ga bermang.",
                   kb_of([("❌ Bekor qilish", "adm:groq")]))
    elif act == "cloud":
        text, kb = await cloud_view()
        await edit(c, text, kb)
    elif act == "cloudset":
        pending[uid] = ("kanal",)
        await edit(c, "🔗 Zaxira kanalini ulash\n\nO'sha YOPIQ kanaldan istalgan postni shu yerga forward qiling "
                      "(yoki kanal ID sini yuboring, masalan -1001234567890).\nBot kanalda admin bo'lishi shart.",
                   kb_of([("❌ Bekor qilish", "adm:cloud")]))
    elif act == "cloudnow":
        try:
            res = await backup_to_channel(force=True)
            msg = {None: "Zaxira kanali ulanmagan yoki baza bo'sh.", True: "✅ Zaxira kanalga yuborildi va qadab qo'yildi.",
                   False: "⚠️ Zaxira yuborildi, lekin QADAB bo'lmadi. Botga kanalda «Xabarlarni qadash» huquqini bering, "
                          "aks holda tiklashda topa olmaydi."}[res]
        except Exception as e:
            msg = f"❌ Yuborib bo'lmadi: {e}"
        await c.message.answer(msg)
    elif act == "cloudrestore":
        await edit(c, "⚠️ Hozirgi baza kanaldagi zaxira bilan ALMASHTIRILADI (hozirgisining nusxasi kompyuterdagi "
                      "backups papkasiga saqlanadi). Davom etamizmi?",
                   kb_of([("✅ Ha, tiklash", "adm:cloudrestoreyes"), ("❌ Yo'q", "adm:cloud")]))
    elif act == "cloudrestoreyes":
        ok, msg = await restore_from_channel()
        await c.message.answer(("✅ " if ok else "❌ ") + msg)
    elif act in ("lv", "add") and p[2] in LESSON_LEVELS:
        lv = p[2]
        if act == "lv":
            text, kb = level_view(lv, int(p[3]) if len(p) > 3 else 0)
            await edit(c, text, kb)
        else:
            pending[uid] = ("add", lv)
            await edit(c, f"➕ {lv} · 1/2: DARS linkini yuboring (https:// bilan boshlansin).",
                       kb_of([("❌ Bekor qilish", f"adm:lv:{lv}")]))
    elif act in ("ls", "ed", "edt", "top", "rm", "rmy"):
        lid = int(p[2])
        row = db.execute("SELECT level, url, test_url FROM lessons WHERE id=?", (lid,)).fetchone()
        if not row:
            return await c.answer("Dars topilmadi", show_alert=True)
        lv, url, turl = row
        n = [x[0] for x in lessons_of(lv)].index(lid) + 1
        if act == "ls":
            tpc = (db.execute("SELECT topic FROM lessons WHERE id=?", (lid,)).fetchone() or [None])[0]
            await edit(c, f"{lv} · {n}-dars\n\n📚 Mavzu: {tpc or 'yozilmagan'}\n📖 Dars: {url}\n📝 Test: {turl or 'yoq'}", kb_of(
                [("✏️ Dars linki", f"adm:ed:{lid}"), ("✏️ Test linki", f"adm:edt:{lid}")],
                [("📚 Mavzu", f"adm:top:{lid}")],
                [("🗑 O'chirish", f"adm:rm:{lid}")], [("⬅️ Orqaga", f"adm:lv:{lv}")]))
        elif act == "ed":
            pending[uid] = ("edit", lid, "url")
            await edit(c, f"✏️ {lv} · {n}-dars. Yangi DARS linkini yuboring.", kb_of([("❌ Bekor qilish", f"adm:ls:{lid}")]))
        elif act == "top":
            pending[uid] = ("topic", lid)
            await edit(c, f"📚 {lv} · {n}-dars mavzusini yozing: dars nima haqida, qanday so'z va iboralar o'tilgan.\n"
                          "Masalan: Salomlashish va tanishuv: Привет, Здравствуйте, Меня зовут...\n"
                          "Mavzu o'quvchiga «Bugungi mavzu» deb ko'rsatiladi va «Darslarim» ro'yxatida chiqadi.", kb_of([("❌ Bekor qilish", f"adm:ls:{lid}")]))
        elif act == "edt":
            pending[uid] = ("edit", lid, "test_url")
            await edit(c, f"✏️ {lv} · {n}-dars. Yangi TEST linkini yuboring.", kb_of([("❌ Bekor qilish", f"adm:ls:{lid}")]))
        elif act == "rm":
            await edit(c, f"🗑 {lv} · {n}-darsni o'chiraymi?\n\n{url}\n{turl or ''}", kb_of(
                [("Ha, o'chirish", f"adm:rmy:{lid}"), ("Yo'q", f"adm:ls:{lid}")]))
        else:
            db.execute("DELETE FROM lessons WHERE id=?", (lid,))
            db.execute("DELETE FROM progress WHERE lesson_id=?", (lid,))
            db.commit()
            text, kb = level_view(lv)
            await edit(c, "🗑 O'chirildi\n\n" + text, kb)
    await c.answer()


# ---------------- doimiy menyu tugmalari ----------------
@dp.message(F.text.in_({M_LESSON, "📚 Hozirgi dars"}))
async def menu_lesson(m: Message):
    await cmd_lesson(m)


@dp.message(F.text == M_VIP)
async def menu_vip(m: Message):
    await start_pro(m.from_user.id, m.answer)


RV_PAGE = 8


def finished_lessons(uid):
    """Tugatilgan darslar (o'tkazib yuborilganlarsiz): [(id, daraja, tartib, mavzu)] daraja va tartib bo'yicha."""
    lang = ulang(uid)  # faqat hozirgi tilning darslari
    rows = db.execute("SELECT l.id, l.level, l.topic FROM progress p JOIN lessons l ON l.id=p.lesson_id "
                      "WHERE p.user_id=? AND COALESCE(p.skipped,0)=0 AND COALESCE(l.lang,'ru')=?", (uid, lang)).fetchall()
    out = []
    for lid, lvl, topic in rows:
        ids = [x[0] for x in lessons_of(lvl, lang)]
        out.append((lid, lvl, (ids.index(lid) + 1) if lid in ids else 0, topic or ""))
    out.sort(key=lambda r: (LEVELS.index(r[1]) if r[1] in LEVELS else 99, r[2]))
    return out


def rv_list(uid, page=0):
    ls = finished_lessons(uid)
    if not ls:
        return "📚 Darslarim\n\nHali tugatgan darsingiz yo'q. Birinchi darsni tugating, keyin bu yerdan qayta ochishingiz mumkin 🙂", None
    pages = max(1, -(-len(ls) // RV_PAGE))
    page = min(max(page, 0), pages - 1)
    rows = [[InlineKeyboardButton(text=f"{lvl}-{n} · {t[:34]}" if t else f"{lvl} · {n}-dars", callback_data=f"rv:{lid}")]
            for lid, lvl, n, t in ls[page * RV_PAGE:(page + 1) * RV_PAGE]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"rvp:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"rvp:{page + 1}"))
    if nav:
        rows.append(nav)
    return f"📚 Darslarim ({len(ls)} ta tugatilgan)\n\nQayta ko'rmoqchi bo'lgan darsni tanlang 👇", InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text == M_LESSONS)
async def menu_lessons(m: Message):
    uid = m.from_user.id
    if not user(uid)[1]:
        return await m.answer("Avval /start bosib, darajangizni aniqlang.")
    text, kb = rv_list(uid)
    await m.answer(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("rvp:"))
async def rv_page(c: CallbackQuery):
    try:
        page = int(c.data.split(":")[1])
    except ValueError:
        return await c.answer()
    text, kb = rv_list(c.from_user.id, page)
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await c.answer()


@dp.callback_query(F.data.startswith("rv:"))
async def rv_open(c: CallbackQuery):
    uid = c.from_user.id
    try:
        lid = int(c.data.split(":")[1])
    except ValueError:
        return await c.answer()
    mine = [x for x in finished_lessons(uid) if x[0] == lid]  # faqat o'zi tugatgan dars
    row = db.execute("SELECT url FROM lessons WHERE id=?", (lid,)).fetchone()
    if not mine or not row:
        return await c.answer("Bu dars topilmadi", show_alert=True)
    _, lvl, n, topic = mine[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 Darsni ochish", web_app=WebAppInfo(url=row[0]))]])
    await c.message.answer(f"📚 {lvl} · {n}-dars" + (f"\n📖 Mavzu: {topic}" if topic else "") +
                           "\n\nBu darsni qayta o'qib, takrorlashingiz mumkin 🙂", reply_markup=kb)
    await c.answer()


@dp.message(F.text == M_ME)
async def menu_me(m: Message):
    uid = m.from_user.id
    goal, level = user(uid)
    if not level:
        return await m.answer("Avval /start bosib, darajangizni aniqlang.")
    n = done_count(uid)
    total_done = finished_count(uid)
    if not PAID_MODE:
        status = "bepul"
    elif is_paid(uid):
        status = "💎 VIP"
    else:
        status = f"bepul ({min(n, FREE_LESSONS)}/{FREE_LESSONS} bepul dars ishlatilgan)"
    await m.answer(f"📈 Mening natijam\n\nTil: {LANG_NAMES[ulang(uid)]}\nDaraja: {level}\nMaqsad: {GOALS.get(goal, '-')}\n"
                   f"Tugatgan darslar: {total_done}\nObuna: {status}")


@dp.message(F.text == M_HELP)
async def menu_help(m: Message):
    text = ("ℹ️ Yordam\n\n📚 Har kuni bitta dars: darsni o'qing, testdan o'ting, keyingi dars ertasi kuni ochiladi.\n"
            "📝 Testdan kamida 70% to'g'ri javob bersangiz, dars tugaydi.\n"
            "🗣 «Suhbat»: boshqa o'quvchi bilan ruscha gaplashasiz, xohlasangiz birga rolli o'yin o'ynaysiz (sun'iy intellekt to'g'ri-xatoni tekshiradi).")
    if PAID_MODE:
        text += (f"\n\n🔤 A0 ning alifbo va sanash darslari bepul, ularni kutmasdan ketma-ket o'tish mumkin.\n"
                 f"🎁 Undan keyin {first_free().lower()} ham bepul.\n💎 VIP: kursning hamma darslari. Narx: {PRICE}\n"
                 "VIP ga o'tish uchun «💎 VIP obuna» tugmasini bosing.")
    await m.answer(text)


# ---------------- Rumiyo AI ni o'qitish (faqat admin) ----------------
RUMIYO_URL = os.getenv("RUMIYO_URL", "").strip().rstrip("/")   # Rumiyo sayti (alohida servis) manzili
if not RUMIYO_URL and os.getenv("SUHBAT_ON") != "1":
    RUMIYO_URL = os.getenv("TALK_URL", "").strip().rstrip("/")   # RUMIYO_URL yo'q bo'lsa, TALK_URL ga qo'yilgan manzil Rumiyo deb olinadi


def rm_count():
    return db.execute("SELECT COUNT(*) FROM rm_pairs").fetchone()[0]


def rm_all():
    return [{"q": q, "a": a, "tone": t} for q, a, t in db.execute("SELECT q, a, tone FROM rm_pairs ORDER BY id")]


def rm_settings():
    try:
        return json.loads(meta_get("rm_settings") or "{}")
    except ValueError:
        return {}


def rm_add(q, a, tone=1):
    q, a = (q or "").strip()[:500], (a or "").strip()[:500]
    if not q or not a:
        return False
    cur = db.execute("INSERT OR IGNORE INTO rm_pairs(q, a, tone, ts) VALUES(?,?,?,?)", (q, a, 1 if tone else 0, int(time.time())))
    db.commit()
    return cur.rowcount > 0


def rm_bulk(text):
    """'odam gapi => Rumiyo javobi' qatorlari -> [(q, a)]. Bunday qator bo'lmasa bo'sh."""
    out = []
    for line in text.splitlines():
        if "=>" in line:
            q, a = line.split("=>", 1)
            if q.strip() and a.strip():
                out.append((q.strip(), a.strip()))
    return out


async def rm_call(method, path, **kw):
    if not (RUMIYO_URL and TALK_SECRET):
        return None
    try:
        async with aiohttp.ClientSession() as http, http.request(method, RUMIYO_URL.rstrip("/") + path, headers={"X-Secret": TALK_SECRET},
                                                                 timeout=aiohttp.ClientTimeout(total=100), **kw) as r:  # tekin server uyg'onguncha kutadi
            return await r.json() if r.status == 200 else None
    except Exception as e:
        print("Rumiyo serveri javob bermadi:", type(e).__name__)
        return None


async def rm_sync(force=False):
    """Botdagi baza = asosiy nusxa. Serverdagi son boshqacha bo'lsa (server qayta yonib, bazasi bo'shab qolgan bo'lsa ham) hammasini qayta yuboradi."""
    if not force:
        j = await rm_call("GET", "/admin/count")
        if j is None or j.get("count") == rm_count():
            return j is not None
    j = await rm_call("POST", "/admin/sync", json={"pairs": rm_all(), "settings": rm_settings()})
    return bool(j and j.get("ok"))


async def rm_pusher():
    while True:
        try:
            await rm_sync()
            await rm_push_keys()
        except Exception as e:
            print("rm_sync:", e)
        await asyncio.sleep(45)         # sayt qayta ishga tushsa, kalit 45 soniyada qaytadi


def rm_view():
    n = rm_count()
    site = "ulangan ✅" if (RUMIYO_URL and TALK_SECRET) else "ULANMAGAN ⚠️ (botning Environment iga RUMIYO_URL va TALK_SECRET kerak)"
    return (f"🧠 AI ni o'qitish (Rumiyo)\n\nBazada: {n} ta gap\nSayt: {site}\n\n"
            "Har bir gap ikki qismdan iborat: odam nima deydi → Rumiyo nima deydi. Rumiyo (jahldor ustoz): bu gaplarga qarab gapiradi, ularni yodlab takrorlamaydi: "
            "uslubini olib, aralashtirib, o'zidan ham qo'shib gapiradi. Qancha ko'p qo'shsangiz, shuncha chechan bo'ladi.\n\n"
            "Gaplar botning bazasida saqlanadi, zaxira kanaliga ham ketadi, server o'chib yonsa bot o'zi qayta yuboradi.",
            kb_of([("➕ Gap qo'shish", "adm:rmadd")], [("🏷 Laqablar", "adm:rms:#laqab"), ("📜 Qoida yozish", "adm:rms:#qoida")],
                  [("😤 Jahl gaplari (koyish)", "adm:rms:#jahl")],
                  [("😊 Do'stona gaplari", "adm:rms:#dostona"), ("🔥 Ehtirosli gaplari", "adm:rms:#ehtiros")],
                  [("🍯 Shirin so'z gaplari", "adm:rms:#shirin")],
                  [("📋 Gaplar ro'yxati", "adm:rmlist:0")],
                  [("🔍 Tekshirish (nega ishlamayapti)", "adm:rmtest")],
                  [("⬅️ Orqaga", "adm:home")]))


def rm_list(page=0):
    n = rm_count()
    page = max(0, min(page, max(0, (n - 1) // 8)))
    rows = db.execute("SELECT id, q, a FROM rm_pairs ORDER BY id DESC LIMIT 8 OFFSET ?", (page * 8,)).fetchall()
    text = f"📋 Gaplar ({n} ta), {page + 1}-sahifa\n\n" + ("\n\n".join(f"{i}. 👤 {q[:120]}\n   🤖 {a[:160]}" for i, (_, q, a) in enumerate(rows, 1)) or "Hali gap yo'q.")
    btns = [[(f"🗑 {i}", f"adm:rmdel:{rid}:{page}") for i, (rid, _, _) in enumerate(rows, 1)][k:k + 4] for k in (0, 4)]
    nav = ([("◀️", f"adm:rmlist:{page - 1}")] if page else []) + ([("▶️", f"adm:rmlist:{page + 1}")] if (page + 1) * 8 < n else [])
    return text, kb_of(*[b for b in btns if b], *([nav] if nav else []),
                       [("📥 Zaxira fayl", "adm:rmjson"), ("📤 Fayldan qo'shish", "adm:rmimp")], [("⬅️ Orqaga", "adm:rm")])


RK_KINDS = {"groq": ("Groq", "gsk_"), "openrouter": ("OpenRouter", "sk-or-"), "gemini": ("Gemini", "AIza"), "gemini_paid": ("Gemini 💰", "AIza"), "gemini_strong": ("Gemini 💎", "AIza")}


def rk_get(kind):
    return groq_keys() if kind == "groq" else _file_keys(kind)


def _file_keys(kind):
    try:
        with open(KEYS_PATH, encoding="utf-8") as f:
            v = json.load(f).get(kind)
        return [k for k in ([v] if isinstance(v, str) else (v or [])) if isinstance(k, str) and k]
    except (OSError, ValueError, AttributeError):
        return []


def rk_save(kind, ks):
    if kind == "groq":
        return save_groq_keys(ks)
    try:
        with open(KEYS_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d[kind] = list(ks)
    with open(KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f)
    try:
        os.chmod(KEYS_PATH, 0o600)
    except OSError:
        pass


async def rm_push_keys():
    """Kalitlarni Rumiyo serveriga yuboradi (server xotirasida turadi; bot har 2 daqiqada qayta yuboradi, shuning uchun server yonsa ham yo'qolmaydi)."""
    j = await rm_call("POST", "/admin/keys", json={k: rk_get(k) for k in RK_KINDS})
    return bool(j and j.get("ok"))


def rk_view():
    lines, rows = [], []
    for kind, title in (("gemini_strong", "💎 KUCHLI (yangi o'quvchi, dastlabki 3 kun, kuchli model)"),
                        ("gemini_paid", "💰 PULLIK (3 kundan keyin: bepul yetmasa shu ishlaydi, arzon model)")):
        ks = rk_get(kind)
        lines.append(f"{title}:\n   Gemini: " + (", ".join("…" + k[-4:] for k in ks) if ks else "yo'q") + "\n")
        icon = "💎" if kind == "gemini_strong" else "💰"
        rows += [[(f"🗑 {icon} Gemini …{k[-4:]}", f"adm:rkdel:{kind}:{i}")] for i, k in enumerate(ks)]
    lines.append("🆓 BEPUL (3 kundan keyin birinchi shular ishlaydi, arzon model; o'yin va zaxira ham):")
    for kind in ("groq", "openrouter", "gemini"):
        name = RK_KINDS[kind][0]
        ks = rk_get(kind)
        lines.append(f"   {name}: " + (", ".join("…" + k[-4:] for k in ks) if ks else "yo'q"))
        rows += [[(f"🗑 🆓 {name} …{k[-4:]}", f"adm:rkdel:{kind}:{i}")] for i, k in enumerate(ks)]
    return ("🔑 Kalitlar\n\n" + "\n".join(lines) +
            "\n\nTartib: yangi o'quvchi 💎, keyin hamma 🆓 bilan boshlaydi, kuchi yetmasa 💰 ga o'tadi, u ham yetmasa Rumiyosiz oddiy rejim.",
            kb_of([("💎 Kuchli kalit", "adm:rkadd:strong"), ("💰 Pullik kalit", "adm:rkadd:paid")], [("🆓 Bepul kalit", "adm:rkadd:free"), ("✅ Tayyor", "adm:rk")], *rows, [("⬅️ Orqaga", "adm:home")]))


def rk_wait(m):
    return bool(m.from_user) and pending.get(m.from_user.id, (None,))[0] == "rkey" and bool(m.text) and not m.text.startswith("/")


@dp.message(rk_wait)
async def rk_input(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return
    raw = m.text
    try:
        await m.delete()  # kalitlar chatda qolmasin
    except Exception:
        pass
    # Bir xabarda istalgancha kalit: qator, bo'sh joy yoki vergul bilan ajratilgan; ketma-ket xabarlar ham qabul qilinadi
    toks = [t for t in re.split(r"[\s,;]+", raw.strip()) if t]
    added, dup, bad = {}, 0, 0
    mode = pending[uid][1] if len(pending.get(uid, ())) > 1 else "free"
    single = {"strong": "gemini_strong", "paid": "gemini_paid"}.get(mode)
    for k in toks:
        if single:
            kind = single if k.startswith("AIza") else None
        else:
            kind = next((kd for kd, (_, pref) in RK_KINDS.items() if kd not in ("gemini_paid", "gemini_strong") and k.startswith(pref)), None)
        if not kind or not (20 < len(k) < 250):
            bad += 1
            continue
        ks = [] if kind in ("gemini_paid", "gemini_strong") else rk_get(kind)
        if k in ks:
            dup += 1
            continue
        rk_save(kind, ks + [k])
        added[kind] = added.get(kind, 0) + 1
    if not added and not dup:
        return await bot.send_message(uid, "Bu Groq (gsk_), OpenRouter (sk-or-) yoki Gemini (AIza) kaliti emas. Qayta yuboring yoki /admin.")
    sent = await rm_push_keys() if added else None
    text, kb = rk_view()
    info = ", ".join(f"{RK_KINDS[kd][0]} +{n}" for kd, n in added.items()) or "yangi kalit yo'q"
    await bot.send_message(uid, f"✅ Qo'shildi: {info}" + (f" · takror: {dup}" if dup else "") + (f" · noto'g'ri: {bad}" if bad else "") +
                           ". Kalitlar chatdan o'chirildi.\n" +
                           ("" if sent is None else "🌐 Rumiyo serveriga yuborildi.\n" if sent else "⚠️ Serverga yetmadi, bot o'zi qayta urinadi.\n") +
                           "Yana kalit yuborishingiz mumkin (istalgancha). Tugatish: «Tayyor».\n\n" + text,
                           reply_markup=kb)


def rm_wait(m):
    return bool(m.from_user) and pending.get(m.from_user.id, (None,))[0] in ("rm1", "rm2", "rms")


@dp.message(F.text & ~F.text.startswith("/"), rm_wait)
async def rm_input(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return
    job, text = pending.pop(uid), m.text.strip()
    if job[0] == "rms":
        new = sum(rm_add(job[1], ln, 1) for ln in text.splitlines() if ln.strip())
        asyncio.create_task(rm_sync(True))
        t, kb = rm_view()
        return await m.answer(f"✅ Saqlandi: {new} ta yangi.\n\n" + t, reply_markup=kb)
    bulk = rm_bulk(text) if job[0] == "rm1" else []
    if bulk:
        new = sum(rm_add(q, a) for q, a in bulk)
    elif job[0] == "rm1":
        pending[uid] = ("rm2", text)
        return await m.answer(f"🧠 2/2\n\n👤 Odam: {text[:300]}\n\nEndi RUMIYO (jahldor ustoz) nima deb javob beradi? Javobini yozing.",
                              reply_markup=kb_of([("❌ Bekor qilish", "adm:rm")]))
    else:
        new = int(rm_add(job[1], text))
    asyncio.create_task(rm_sync(True))
    pending[uid] = ("rm1",)   # ketma-ket qo'shish uchun
    await m.answer((f"✅ Saqlandi: {new} ta yangi." if new else "ℹ️ Bu gap bazada bor edi.") + f" Jami: {rm_count()} ta.\n\n"
                   "Keyingisi: ODAM nima deydi? (tugatish uchun tugmani bosing)", reply_markup=kb_of([("✅ Tugatdim", "adm:rm")]))


def rm_doc_wait(m):
    return bool(m.from_user) and pending.get(m.from_user.id, (None,))[0] == "rmimp"


@dp.message(F.document, rm_doc_wait)
async def rm_import(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return
    pending.pop(uid, None)
    try:
        buf = io.BytesIO()
        await bot.download(m.document, destination=buf)
        d = json.loads(buf.getvalue().decode("utf-8"))
        pairs = d.get("pairs") if isinstance(d, dict) else d
        new = sum(rm_add(x.get("q"), x.get("a"), x.get("tone", 1)) for x in pairs if isinstance(x, dict))
        if isinstance(d, dict) and isinstance(d.get("settings"), dict) and d["settings"]:
            meta_set("rm_settings", json.dumps(d["settings"], ensure_ascii=False))
    except Exception as e:
        return await m.answer(f"Faylni o'qib bo'lmadi: {type(e).__name__}. rumiyo_baza.json ni yuboring.", reply_markup=kb_of([("⬅️ Orqaga", "adm:rm")]))
    asyncio.create_task(rm_sync(True))
    await m.answer(f"✅ {new} ta yangi gap qo'shildi. Jami: {rm_count()} ta.", reply_markup=kb_of([("⬅️ Orqaga", "adm:rm")]))


def rm_link(uid, lesson, s, n, intro=False, go=False):
    """Dars testidan keyin: Rumiyo saytiga imzolangan 24 soatlik havola (kun raqami, mavzu, test natijasi ichida)."""
    import base64, hashlib, hmac
    d = re.search(r"[?&]d=(\d+)", lesson[1] or "")
    topic = db.execute("SELECT topic FROM lessons WHERE id=?", (lesson[0],)).fetchone()
    body = base64.urlsafe_b64encode(json.dumps({"u": uid, "l": eff(user(uid)[1]), "e": int(time.time()) + 86400, "d": int(d.group(1)) if d else 0, "g": ulang(uid),
                                                "t": ((topic[0] if topic else "") or "")[:100], "s": s, "n": n,
                                                "v": 1 if (is_paid(uid) or is_admin(uid)) else 0, "i": lesson[0], "x": lesson[1] or "",
                                                **({"h": 1, "k": current(uid)[2], "p": int(PASS * 100)} if intro else {"h": 2} if go else {})},
                                               separators=(",", ":"), ensure_ascii=False).encode()).decode().rstrip("=")
    sig = hmac.new(TALK_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{RUMIYO_URL.rstrip('/')}/?t={body}.{sig}"


def admin_text_wait(m):  # faqat dars linki kiritilayotgan paytda (boshqa kutishlar o'z handlerlariga o'tadi)
    return bool(m.from_user) and pending.get(m.from_user.id, (None,))[0] in ("add", "add2", "edit")


@dp.message(F.text & ~F.text.startswith("/"), admin_text_wait)
async def admin_input(m: Message):
    uid = m.from_user.id
    if not is_admin(uid) or uid not in pending:
        return
    url = m.text.strip()
    if not valid_url(url):
        return await m.answer("Link https:// bilan boshlanishi kerak. Qayta yuboring yoki /admin ni bosing.")
    job = pending.pop(uid)
    if job[0] == "add":  # 1-qadam: dars linki olindi, endi test linki so'raladi
        pending[uid] = ("add2", job[1], url)
        return await m.answer(f"✅ Dars linki olindi.\n{job[1]} · 2/2: endi shu darsning TEST linkini yuboring.",
                              reply_markup=kb_of([("❌ Bekor qilish", f"adm:lv:{job[1]}")]))
    if job[0] == "add2":
        cur = db.execute("INSERT INTO lessons(level, url, test_url) VALUES(?, ?, ?)", (job[1], job[2], url))
        db.commit()
        pending[uid] = ("topic", cur.lastrowid)
        return await m.answer(f"✅ Qo'shildi. {job[1]}\n\n📚 Endi shu darsning MAVZUSINI yozing: dars nima haqida, "
                              "qanday so'z va iboralar o'tilgan. Masalan: Salomlashish: Привет, Здравствуйте.\n"
                              "Hozir yozmasangiz, keyin darsni ochib «📚 Mavzu» tugmasi bilan yozasiz.",
                              reply_markup=kb_of([("⏭ Keyinroq", f"adm:lv:{job[1]}")]))
    else:
        row = db.execute("SELECT level FROM lessons WHERE id=?", (job[1],)).fetchone()
        if not row:
            return await m.answer("Dars topilmadi.")
        col = "test_url" if job[2] == "test_url" else "url"  # faqat ichki qiymatlar
        db.execute(f"UPDATE lessons SET {col}=? WHERE id=?", (url, job[1]))
        lv, note = row[0], "✅ O'zgartirildi"
    db.commit()
    text, kb = level_view(lv)
    await m.answer(f"{note}\n\n{text}", reply_markup=kb)


async def push_lesson(uid):
    """show_lesson'ning xuddi o'zi, lekin foydalanuvchiga o'zimiz yozamiz (xabarga javob bermasdan)."""
    if need_placement(uid):
        return await bot.send_message(uid, "Boshlashdan oldin ikkita savol:\n\n" + PL_Q1, reply_markup=pl_kb("pl:a"))
    if paywalled(uid):
        return await bot.send_message(uid, pay_text(uid), reply_markup=pro_kb())
    w = wait_until(uid)
    if w:
        return await bot.send_message(uid, wait_text(w), reply_markup=vip_kb(uid))
    lvl, lesson, n, total, moved = current(uid)
    if moved:
        await bot.send_message(uid, level_up_text(moved), parse_mode="HTML")
    if lesson:
        await bot.send_message(uid, f"📚 {lvl} · {n}-dars (jami {total} ta)", reply_markup=lesson_kb(lesson, uid))


# ---------------- to'lov: chek skrinshoti -> admin tasdiqlaydi ----------------
# ---------------- majburiy obuna (kanal/guruh) ----------------
def sub_channels():
    try:
        return [x for x in json.loads(meta_get("sub_channels") or "[]") if isinstance(x, str)]
    except ValueError:
        return []


def sub_parts(ch):
    x = ch.split("|", 2)
    return x[0], (x[1] if len(x) > 1 else ""), (x[2] if len(x) > 2 else "")


def sub_ref(ch):
    c = sub_parts(ch)[0]
    return int(c) if re.fullmatch(r"-?\d+", c) else c


def sub_url(ch):
    c, link, _ = sub_parts(ch)
    return link or f"https://t.me/{c.lstrip('@')}"


def sub_name(ch):
    c, _, title = sub_parts(ch)
    return title or c


LEVEL_INFO = {
    "A1": ("🌱", "Boshlang'ich", "oddiy gaplar va kundalik iboralarni tushunasiz va ayta olasiz"),
    "A2": ("🌿", "Asosiy", "kundalik mavzularda oddiy suhbat qura olasiz"),
    "B1": ("🌳", "O'rta", "mustaqil va ravon suhbatlashasiz, fikringizni tushuntira olasiz"),
    "B2": ("🚀", "O'rta-yuqori", "murakkab mavzularda erkin fikr bildirasiz"),
    "C1": ("👑", "Yuqori", "ruscha deyarli ona tilingizdagidek gapirasiz"),
}
LEVEL_FROM_GIFT = {
    "A1": "🎁 Sovg'a: «Birinchi qadam» nishoni — siz alifbo va sanashdan o'tib, haqiqiy gaplarga keldingiz!",
    "A2": "🎁 Sovg'a: «Ishonchli boshlovchi» nishoni — endi rus tilida o'zingizni tanishtira olasiz!",
    "B1": "🎁 Sovg'a: «Ravon so'zlovchi» nishoni — bu juda katta yutuq, ko'pchilik shu yerda to'xtab qoladi, siz esa yo'q!",
    "B2": "🎁 Sovg'a: «Mohir» nishoni — rus tilida ish ham, o'qish ham sizning qo'lingizda!",
    "C1": "🎁 Sovg'a: «Usta» nishoni — eng yuqori cho'qqilardan biri. Siz bilan faxrlanamiz!",
}
PRAISES = [
    "Bugun yangi darajaga ko'tarilganingiz bilan chin dildan tabriklayman! 🎉",
    "Barakalla! Mehnatingiz zoye ketmadi, natija ko'rinib turibdi! 👏",
    "Zo'r ketyapsiz! Yangi daraja — yangi imkoniyatlar demak! 🌟",
    "Qoyil! Har kuni oz-ozdan, mana natija! 💪",
    "Siz bilan haqiqatan faxrlansa arziydi! Tabriklayman! 🏆",
    "Ajoyib natija! Shunday davom eting, orzuingizga yaqinlashyapsiz! 🚀",
    "Bu oson emas edi, lekin siz uddaladingiz! Tabriklayman! 🔥",
    "Daraja oshdi — demak siz haqiqatan o'syapsiz! Ofarin! 🌱",
    "Sabr va intizom o'z mevasini berdi. Tabriklayman! 🍀",
    "Hayratdaman! Siz juda tez rivojlanyapsiz! ⚡️",
    "Mana sizning g'alabangiz! Bugun faxrlanishga haqlisiz! 🥇",
    "Har bir daraja — o'zingizga bergan va'daning bajarilishi. Tabriklayman! 🤝",
    "Siz buni uddaladingiz! Endi keyingi cho'qqi sari! ⛰",
    "Ajoyib! Ruscha sizga bo'ysuna boshladi! 😎",
    "Tabriklayman, ustoz! Bugun kuningiz muborak bo'lsin! 🌞",
    "Zo'r! Bilimingiz kundan-kunga mustahkamlanyapti! 📚",
    "Siz ko'pchilikdan oldinda ketyapsiz — davom eting! 🏃",
    "Barakalla, jonivor! Yangi darajangiz muborak! 🎊",
    "Mehnat qilgan — topgan! Siz buni isbotladingiz! ✨",
    "Yuragimdan tabriklayman! Siz haqiqiy izlanuvchisiz! 💎",
    "Voy-bo'y, qanday tez! Shunday davom etsangiz, tez orada ravon gapirasiz! 🎯",
    "Yangi daraja — yangi siz! Tabriklayman! 🦋",
    "Siz o'zingizni yengdingiz — eng katta g'alaba shu! 🏅",
    "Qani, olqish sizga! Bugun bayram qilsa bo'ladi! 🥳",
    "Ofarin! Har bir dars sizni orzuingizga yaqinlashtiryapti! 🧭",
    "Bu daraja sizga juda yarashadi! Tabriklayman! 🌹",
    "Shunday o'quvchilarim borligidan xursandman! Tabriklayman! 😊",
    "Intilgan — yetadi! Siz yana bir pog'onaga chiqdingiz! 🪜",
    "Eng zo'ri shundaki, siz to'xtamadingiz. Tabriklayman! 🌈",
    "Bugun tarixingizga yozib qo'ying: yangi darajaga chiqdingiz! 📝",
]


def level_up_text(level):
    ic, title, what = LEVEL_INFO.get(level, ("🎓", "", "yangi bosqichga chiqdingiz"))
    return (f"{ic} <b>{level} darajasiga ko'tarildingiz!</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"{random.choice(PRAISES)}\n\n"
            f"📈 Endi siz {what}.\n\n"
            f"{LEVEL_FROM_GIFT.get(level, '')}\n\n"
            f"Davom etamiz — keyingi dars sizni kutyapti! 💪")



sub_ok = {}  # foydalanuvchi -> a'zoligi tasdiqlangan vaqt (10 daqiqa eslab turadi)
SUB_SEM = asyncio.Semaphore(25)  # bir vaqtda Telegramga 25 tadan ko'p so'rov ketmaydi


sub_in = {}  # (foydalanuvchi, kanal) -> a'zoligi 10 daqiqa eslab turiladi: yangi guruh qo'shilsa, eskilari qayta tekshirilmaydi


async def _is_out(uid, ch):
    if time.time() < sub_in.get((uid, ch), 0):
        return None
    try:
        async with SUB_SEM:
            st = (await asyncio.wait_for(bot.get_chat_member(sub_ref(ch), uid), 5)).status
        if st in ("left", "kicked"):
            return ch
        if len(sub_in) > 300000:
            sub_in.clear()
        sub_in[(uid, ch)] = time.time() + 600
        return None
    except Exception as e:  # bot kanalda admin emas / Telegram sekin: foydalanuvchini to'smaymiz
        print("Majburiy obuna tekshirib bo'lmadi:", ch, str(e)[:80])
        return None


async def not_joined(uid, chs):
    res = await asyncio.gather(*[_is_out(uid, ch) for ch in chs])
    return [r for r in res if r]


async def sub_gate(handler, event, data):
    u = event.from_user
    if isinstance(event, Message) and u and not u.is_bot and (event.text or "").startswith("/start"):
        # Taklif havolasi obuna oynasidan OLDIN hisoblanadi: yangi odam avval kanalga a'zo bo'lishi kerak bo'lsa ham,
        # havola yo'qolmaydi (ilgari obuna oynasi /start ni to'sib, taklif hisoblanmay qolardi).
        parts = event.text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.startswith("k") and len(arg) > 1:
            apply_promo(u.id, arg[1:])
        if arg.startswith("r") and arg[1:].isdigit():
            try:
                await ref_join(u, int(arg[1:]))
            except Exception as e:
                print("taklif xatosi:", type(e).__name__, str(e)[:80])
    chs = sub_channels()
    if not chs or not u or is_admin(u.id) or (isinstance(event, Message) and event.web_app_data):
        return await handler(event, data)
    if time.time() < sub_ok.get(u.id, 0):
        return await handler(event, data)
    miss = await not_joined(u.id, chs)
    if not miss:
        if len(sub_ok) > 200000:
            sub_ok.clear()
        sub_ok[u.id] = time.time() + 600
        return await handler(event, data)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"📢 {sub_name(ch)}", url=sub_url(ch))] for ch in miss]
                              + [[InlineKeyboardButton(text="✅ A'zo bo'ldim, tekshirish", callback_data="sub:check")]])
    if isinstance(event, CallbackQuery):
        if event.data == "sub:check":
            return await event.answer("Siz hali a'zo bo'lmagansiz", show_alert=True)
        await event.answer()
        return await event.message.answer("Botdan foydalanish uchun avval kanalga a'zo bo'ling 👇", reply_markup=kb)
    await event.answer("Botdan foydalanish uchun avval kanalga a'zo bo'ling, keyin «Tekshirish» ni bosing 👇", reply_markup=kb)


seen_users = set()  # bazaga bir marta yoziladi, keyingi bosishlarda disk ishlamaydi
flood = {}  # foydalanuvchi -> oxirgi bosishlar vaqti


async def reg_user(handler, event, data):
    """Botga yozgan hamma xabar yuborish ro'yxatiga tushadi."""
    u = getattr(event, "from_user", None)
    if u and not u.is_bot and not is_admin(u.id):
        t = time.time()
        fl = [x for x in flood.get(u.id, ()) if t - x < 5]
        fl.append(t)
        flood[u.id] = fl
        if len(fl) > 12:  # 5 soniyada 12 tadan ko'p bosish: e'tiborsiz (bot qotmasligi uchun)
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except Exception:
                    pass
            return
    if u and not u.is_bot and u.id not in seen_users:
        try:
            db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (u.id,))
            db.commit()
            seen_users.add(u.id)
        except sqlite3.Error as e:
            print("ro'yxatga olinmadi:", e)
    return await handler(event, data)


async def private_only(handler, event, data):
    """Guruh va kanallarda bot jim: faqat /oyin buyrug'i, o'yin tugmalari va ketayotgan o'yin javoblariga javob beradi."""
    ch = event.chat if isinstance(event, Message) else (event.message.chat if getattr(event, "message", None) else None)
    if ch is not None and ch.type != "private":
        if ch.type in ("group", "supergroup"):
            try:
                if isinstance(event, Message):
                    await group_message(event)
                else:
                    await group_callback(event)
            except Exception as e:
                print("guruh xatosi:", type(e).__name__, str(e)[:100])
        return
    return await handler(event, data)


dp.message.outer_middleware(private_only)
dp.callback_query.outer_middleware(private_only)
dp.message.outer_middleware(reg_user)
dp.callback_query.outer_middleware(reg_user)
dp.message.outer_middleware(sub_gate)
dp.callback_query.outer_middleware(sub_gate)


@dp.callback_query(F.data == "sub:check")
async def sub_check(c: CallbackQuery):
    await c.answer("✅ Rahmat!")
    goal, level = user(c.from_user.id)
    if level:
        return await c.message.answer("✅ Rahmat! Davom etamiz 👇", reply_markup=main_menu())
    if goal:
        return await ask_test(c.message, "✅ Rahmat! Darajangizni aniqlash qoldi: «📝 Testni boshlash» tugmasini bosing 👇", c.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Boshlash", callback_data="go")]])
    await c.message.answer("✅ Rahmat, a'zo bo'ldingiz! Rus tilini o'rganishni boshlaymizmi?", reply_markup=kb)  # /start ni qayta bosish shart emas


# ---------------- media: rasm va ovoz paketlari (trener GitHub Pages: p<n>.bin = ovoz, g<n>.bin = rasm) ----------------
import base64, zlib, struct
from collections import OrderedDict
from aiogram.types import InputMediaPhoto

MEDIA_URL = os.getenv("MEDIA_URL", "https://shoxrux56.github.io/trener/").rstrip("/") + "/"
_POOL = "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"
_pool = []
_packs = OrderedDict()   # (tur, blok) -> {id: (boshi, uzunligi)}, bytes: xotirada ko'pi bilan 6 ta paket
_pk_lock = {}
db.execute("CREATE TABLE IF NOT EXISTS media(k TEXT PRIMARY KEY, fid TEXT)")
_fid = {}


def pool():
    """O'yin uchun qisqa A1 gaplar: (raqam, ruscha, o'zbekcha). Rasm va ovozi bor."""
    if not _pool:
        _pool.extend(json.loads(zlib.decompress(base64.b64decode(_POOL)).decode()))
    return _pool


async def get_pack(kind, blk):
    key = (kind, blk)
    if key in _packs:
        _packs.move_to_end(key)
        return _packs[key]
    lock = _pk_lock.setdefault(key, asyncio.Lock())
    async with lock:
        if key in _packs:
            return _packs[key]
        async with aiohttp.ClientSession() as http, http.get(f"{MEDIA_URL}{kind}{blk}.bin", timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status != 200:
                raise RuntimeError(f"{kind}{blk}.bin {r.status}")
            buf = await r.read()
        c = struct.unpack_from("<I", buf, 0)[0]
        base = 4 + c * 12
        idx = {}
        for i in range(c):
            n, off, ln = struct.unpack_from("<III", buf, 4 + i * 12)
            idx[n] = (base + off, ln)
        _packs[key] = (buf, idx)
        while len(_packs) > 6:
            _packs.popitem(last=False)
        return _packs[key]


async def media_in(kind, n):
    """Telegramga yuborish uchun: avval saqlangan file_id (tez), bo'lmasa paketdan fayl."""
    k = f"{kind}{n}"
    fid = _fid.get(k)
    if fid is None:
        r = db.execute("SELECT fid FROM media WHERE k=?", (k,)).fetchone()
        fid = _fid[k] = r[0] if r else ""
    if fid:
        return fid
    buf, idx = await get_pack(kind, (n - 13) // 100)
    off, ln = idx[n]
    return BufferedInputFile(buf[off:off + ln], filename=f"{n}.mp3" if kind == "p" else f"{n}.png")


def media_save(kind, n, fid):
    k = f"{kind}{n}"
    if fid and not _fid.get(k):
        _fid[k] = fid
        try:
            db.execute("INSERT OR REPLACE INTO media(k, fid) VALUES(?, ?)", (k, fid))
            db.commit()
        except sqlite3.Error:
            pass


# ---------------- GURUH O'YINLARI: /oyin (faqat guruh admini boshlaydi, bot boshqa vaqtda guruhda jim) ----------------
# Hammasi oddiy A1 SO'ZLAR va rasm (emoji) bilan: gap yo'q, matn kam, javob tugma bilan.
WORDS = {  # mavzu -> [(emoji, ruscha, o'zbekcha)]
    "hayvon": [("🐱", "кошка", "mushuk"), ("🐶", "собака", "it"), ("🐴", "лошадь", "ot"), ("🐮", "корова", "sigir"),
               ("🐔", "курица", "tovuq"), ("🐟", "рыба", "baliq"), ("🐦", "птица", "qush"), ("🐭", "мышь", "sichqon"),
               ("🐰", "заяц", "quyon"), ("🐻", "медведь", "ayiq"), ("🐺", "волк", "bo'ri")],
    "meva": [("🍎", "яблоко", "olma"), ("🍌", "банан", "banan"), ("🍉", "арбуз", "tarvuz"), ("🍇", "виноград", "uzum"),
             ("🍋", "лимон", "limon"), ("🍐", "груша", "nok"), ("🍑", "персик", "shaftoli"), ("🍓", "клубника", "qulupnay"),
             ("🍒", "вишня", "olcha"), ("🍈", "дыня", "qovun")],
    "sabzavot": [("🥕", "морковь", "sabzi"), ("🥔", "картофель", "kartoshka"), ("🍅", "помидор", "pomidor"),
                 ("🥒", "огурец", "bodring"), ("🧅", "лук", "piyoz"), ("🌶", "перец", "qalampir"),
                 ("🌽", "кукуруза", "makkajo'xori"), ("🧄", "чеснок", "sarimsoq")],
    "ovqat": [("🍞", "хлеб", "non"), ("🥛", "молоко", "sut"), ("🧀", "сыр", "pishloq"), ("🥚", "яйцо", "tuxum"),
              ("🥩", "мясо", "go'sht"), ("🍚", "рис", "guruch"), ("🍲", "суп", "sho'rva"), ("🍵", "чай", "choy"),
              ("☕", "кофе", "qahva"), ("🎂", "торт", "tort"), ("🍦", "мороженое", "muzqaymoq")],
    "transport": [("🚗", "машина", "mashina"), ("🚌", "автобус", "avtobus"), ("🚆", "поезд", "poyezd"),
                  ("✈️", "самолёт", "samolyot"), ("🚲", "велосипед", "velosiped"), ("🚢", "корабль", "kema"),
                  ("🚕", "такси", "taksi"), ("🚇", "метро", "metro"), ("🚀", "ракета", "raketa")],
    "uy": [("🏠", "дом", "uy"), ("🚪", "дверь", "eshik"), ("🪟", "окно", "deraza"), ("🪑", "стул", "stul"),
           ("🛏", "кровать", "karavot"), ("🔑", "ключ", "kalit"), ("💡", "лампа", "chiroq"), ("📱", "телефон", "telefon"),
           ("⌚", "часы", "soat"), ("📖", "книга", "kitob"), ("☂️", "зонт", "soyabon")],
    "kiyim": [("👕", "рубашка", "ko'ylak"), ("👞", "ботинки", "etik"), ("👗", "платье", "ayollar ko'ylagi"),
              ("👓", "очки", "ko'zoynak"), ("👜", "сумка", "sumka"), ("👒", "шляпа", "shlyapa"),
              ("🧦", "носки", "paypoq"), ("🧤", "перчатки", "qo'lqop"), ("👖", "джинсы", "jinsi")],
    "tana": [("✋", "рука", "qo'l"), ("👁", "глаз", "ko'z"), ("👂", "ухо", "quloq"), ("👃", "нос", "burun"),
             ("👄", "рот", "og'iz"), ("🦵", "нога", "oyoq"), ("❤️", "сердце", "yurak"), ("🦷", "зуб", "tish")],
    "tabiat": [("☀️", "солнце", "quyosh"), ("🌙", "луна", "oy"), ("⭐", "звезда", "yulduz"), ("🌧", "дождь", "yomg'ir"),
               ("❄️", "снег", "qor"), ("🌳", "дерево", "daraxt"), ("🌸", "цветок", "gul"), ("⛰", "гора", "tog'"),
               ("🌊", "море", "dengiz"), ("🔥", "огонь", "olov")],
    "kasb": [("👨‍⚕️", "врач", "shifokor"), ("👨‍🍳", "повар", "oshpaz"), ("👨‍🏫", "учитель", "o'qituvchi"),
             ("👮", "полицейский", "politsiyachi"), ("👨‍🚒", "пожарный", "o't o'chiruvchi"), ("🧑‍🎨", "художник", "rassom")],
    "maktab": [("🖊", "ручка", "ruchka"), ("✏️", "карандаш", "qalam"), ("📓", "тетрадь", "daftar"),
               ("🏫", "школа", "maktab"), ("💻", "компьютер", "kompyuter"), ("⚽", "мяч", "to'p"),
               ("🎸", "гитара", "gitara"), ("🎬", "кино", "kino")],
    "oila": [("👩", "мама", "ona"), ("👨", "папа", "ota"), ("👵", "бабушка", "buvi"), ("👴", "дедушка", "bobo"),
             ("👶", "ребёнок", "bola")],
}
ALLW = [(c, e, r, u) for c, ws in WORDS.items() for e, r, u in ws]

NOADMIN = {}


async def gsend(chat_id, text, **k):
    return await bot.send_message(chat_id, text, parse_mode="HTML", **k)


GAMES = {}  # chat_id -> o'yin holati
GAME_NAMES = {"a": "🎧 Eshit va top", "p": "🖼 Rasm va gap", "d": "🕵️ Detektiv", "w": "🔤 So'zni yig'", "q": "🍎 Bu nima?"}
GAME_INTRO = {"a": "Eshiting, tarjimasini bosing 👇", "p": "Rasmga mos tarjimani bosing 👇", "d": "Mos rasmni toping 👇",
              "w": "So'zni tuzib yozing ✍️", "q": "Rasmga mos so'zni bosing 👇"}
ROUNDS = 7      # 7 ta savol
SECS = 60       # har savol 1 daqiqa: 7 savol = 7 daqiqa, odamlar o'ynab turadi
PTS = [10, 7, 5, 3, 2, 1]  # birinchi topgan 10, keyingilar kamroq: hamma qatnashadi


def _norm(s):
    return re.sub(r"[^а-яa-z]", "", (s or "").lower().replace("ё", "е"))


async def is_chat_admin(chat_id, uid):
    if is_admin(uid):
        return True
    try:
        return (await bot.get_chat_member(chat_id, uid)).status in ("administrator", "creator")
    except Exception:
        return False


def pname(u):
    return html.escape((u.full_name or "O'yinchi")[:20], quote=False)


def wlabel(w):
    return f"{w[1]} · {w[3]}"


def make_round(kind, blk=None):
    """Bitta savol: (matn, tugma matnlari | None, to'g'ri javob, javob izohi, media | None).
    blk: o'yin bitta 100 talik blokdan savol oladi, shunda faqat 1-2 paket yuklanadi (tez, qotmaydi)."""
    if kind in ("a", "p", "d"):
        ps = [x for x in pool() if blk is None or (x[0] - 13) // 100 == blk]
        a = random.choice(ps)
        others = random.sample([x for x in ps if x[0] != a[0]], 3)
        opts = [a] + others
        random.shuffle(opts)
        ans = opts.index(a)
        note = f"{a[1]} = {a[2]}"
        if kind == "a":
            return "🎧", [o[2] for o in opts], ans, note, ("audio", a[0])
        if kind == "p":
            return "🖼", [o[2] for o in opts], ans, note, ("photo", a[0])
        return f"🕵️ Qaysi rasm?\n👉 <b>{html.escape(a[2], quote=False)}</b>", ["1", "2", "3", "4"], ans, note, ("album", [o[0] for o in opts])
    if kind == "q":
        a = random.choice(ALLW)
        opts = [a] + random.sample([x for x in ALLW if x[2] != a[2]], 3)
        random.shuffle(opts)
        return f"👉 {a[1]}", [wlabel(o) for o in opts], opts.index(a), f"{a[1]} {a[2]} = {a[3]}", None
    wpool = [x for x in ALLW if 4 <= len(x[2]) <= 8]
    c, e, ru, uz = random.choice(wpool)
    letters = list(ru)
    for _ in range(10):
        random.shuffle(letters)
        if "".join(letters) != ru:
            break
    return f"{e}\n\n👉 <b>{' '.join(x.upper() for x in letters)}</b>\n🇺🇿 {html.escape(uz, quote=False)}", None, _norm(ru), f"{e} {ru} = {uz}", None


async def bot_kb():
    me = await bot.me()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗣 Suhbatlashish", url=f"https://t.me/{me.username}?start=talk")],
        [InlineKeyboardButton(text="🤖 Botga o'tish va darsni boshlash", url=f"https://t.me/{me.username}?start=oyin")]])


def board(g, limit=10):
    top = sorted(g["sc"].values(), key=lambda x: -x[1])
    medals = ["🥇", "🥈", "🥉"]
    return "\n".join(f"{medals[i] if i < 3 else '▫️'} {n} — {p}" for i, (n, p) in enumerate(top[:limit]))


def winners_line(cur):
    return "  ".join(f"✅ {pname(u)} +{PTS[min(i, len(PTS) - 1)]}" for i, u in enumerate(cur["win"]))


def give(g, cur, u):
    """To'g'ri javob bergan o'yinchiga ball: birinchi 10, keyin 7, 5, 3, 2, 1."""
    if u.id in cur["done"]:
        return None
    cur["done"].add(u.id)
    cur["win"].append(u)
    pts = PTS[min(len(cur["win"]) - 1, len(PTS) - 1)]
    g["sc"].setdefault(u.id, [pname(u), 0])[1] += pts
    if len(cur["win"]) >= 6:
        cur["ev"].set()
    return pts


async def send_round(chat_id, kind, head, opts, media, rid):
    """Savolni yuboradi. Qaytaradi: (xabar, tahrirlash turi). Tugmalar shu xabarda."""
    kb = None
    if opts:
        if kind == "d":
            b_ = [InlineKeyboardButton(text=o, callback_data=f"g{kind}:{rid}:{i}") for i, o in enumerate(opts)]
            kb = InlineKeyboardMarkup(inline_keyboard=[b_])
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=o, callback_data=f"g{kind}:{rid}:{i}")] for i, o in enumerate(opts)])
    if media and media[0] == "audio":
        inp = await asyncio.wait_for(media_in("p", media[1]), 30)
        m = await bot.send_audio(chat_id, inp, caption=head, parse_mode="HTML", reply_markup=kb, title="🎧", performer="Rumiyo")
        media_save("p", media[1], m.audio.file_id if m.audio else "")
        return m, kb, "cap"
    if media and media[0] == "photo":
        inp = await asyncio.wait_for(media_in("g", media[1]), 30)
        m = await bot.send_photo(chat_id, inp, caption=head, parse_mode="HTML", reply_markup=kb)
        media_save("g", media[1], m.photo[-1].file_id if m.photo else "")
        return m, kb, "cap"
    if media and media[0] == "album":
        ins = [await asyncio.wait_for(media_in("g", n), 30) for n in media[1]]
        ms = await bot.send_media_group(chat_id, [InputMediaPhoto(media=x) for x in ins])
        for n, mm in zip(media[1], ms):
            media_save("g", n, mm.photo[-1].file_id if mm.photo else "")
        m = await gsend(chat_id, head, reply_markup=kb)
        return m, kb, "txt"
    m = await gsend(chat_id, head, reply_markup=kb)
    return m, kb, "txt"


async def edit_round(msg, mode, text, kb):
    if mode == "cap":
        await msg.edit_caption(caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.edit_text(parse_mode="HTML", text=text, reply_markup=kb)


async def run_game(chat_id, kind):
    g = GAMES[chat_id]
    skipped = 0
    try:
        for n in range(1, ROUNDS + 1):
            txt, opts, ans, note, media = make_round(kind, g.get("blk"))
            g["rid"] += 1
            rid = g["rid"]
            cur = g["cur"] = {"rid": rid, "ans": ans, "wrong": set(), "done": set(), "win": [], "ev": asyncio.Event()}
            head = f"{GAME_NAMES[kind]} · {n}/{ROUNDS}\n{txt}" if not media or media[0] == "album" else f"{GAME_NAMES[kind]} · {n}/{ROUNDS}"
            try:
                msg, kb, mode = await send_round(chat_id, kind, head, opts, media, rid)
            except Exception as e:  # rasm/ovoz yuklanmadi yoki sekin: savolni tashlab, o'yin davom etadi
                print("savol yuborilmadi:", type(e).__name__, str(e)[:80])
                g["cur"] = None
                skipped += 1
                if skipped >= 3:
                    await gsend(chat_id, "⚠️ Rasm/ovoz hozir yuklanmayapti. Keyinroq urinib ko'ring: /oyin")
                    return
                continue
            end, shown, last = time.time() + SECS, 0, 0.0
            while time.time() < end and not cur["ev"].is_set():
                await asyncio.sleep(1)
                if len(cur["win"]) != shown and time.time() - last >= 5:  # topganlar ro'yxatini ~5 soniyada bir yangilaydi
                    shown, last = len(cur["win"]), time.time()
                    try:
                        await edit_round(msg, mode, head + "\n\n" + winners_line(cur), kb)
                    except Exception:
                        pass
            g["cur"] = None
            res = (winners_line(cur) if cur["win"] else "⏱ Hech kim topmadi") + f"\n👉 <b>{html.escape(note, quote=False)}</b>"
            try:
                await edit_round(msg, mode, f"{head}\n\n{res}", None)
            except Exception:
                pass
            if g["sc"] and n < ROUNDS and n % 2 == 0:
                await gsend(chat_id, f"📊 {n}/{ROUNDS}:\n\n" + board(g, 5))
            await asyncio.sleep(1.5)
        body = board(g) if g["sc"] else "Bu safar hech kim ball olmadi."
        await gsend(chat_id, f"🏁 {GAME_NAMES[kind]} tugadi!\n\n{body}\n\n💬 Endi suhbatlashing! 🤖 Ruscha o'rganish: botga o'ting 👇",
                    reply_markup=await bot_kb())
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print("o'yin xatosi:", type(e).__name__, str(e)[:100])
    finally:
        GAMES.pop(chat_id, None)


async def game_panel(chat_id):
    me = await bot.me()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 O'yin (faqat admin)", callback_data="gm:menu")],
        [InlineKeyboardButton(text="🤖 Botga o'tish", url=f"https://t.me/{me.username}?start=oyin")]])
    await gsend(chat_id, f"🇷🇺 <b>Rumiyo</b> · ruscha o'yin · {BOT_VERSION}", reply_markup=kb)


async def group_message(m: Message):
    chat_id = m.chat.id
    t = (m.text or "").strip()
    if t.startswith("/oyin") and m.from_user:
        me = await bot.me()
        cmd = t.split()[0]
        if "@" in cmd and cmd.split("@")[1].lower() != (me.username or "").lower():
            return
        if cmd.split("@")[0] != "/oyin":
            return
        anon = m.sender_chat is not None and m.sender_chat.id == chat_id  # «anonim admin» sifatida yozilgan
        if not anon and not await is_chat_admin(chat_id, m.from_user.id):
            return  # admin bo'lmaganga bot umuman javob bermaydi
        return await game_panel(chat_id)
    g = GAMES.get(chat_id)
    if g and g["kind"] == "w" and g.get("cur") and t and m.from_user and not m.from_user.is_bot:
        cur = g["cur"]
        if _norm(t) == cur["ans"]:
            give(g, cur, m.from_user)


async def group_callback(c: CallbackQuery):
    chat_id = c.message.chat.id
    d = c.data or ""
    uid = c.from_user.id
    if d.startswith("gm:"):
        if not await is_chat_admin(chat_id, uid):
            return await c.answer("Buni faqat guruh admini bosa oladi", show_alert=True)
        act = d[3:]
        if act == "menu":
            ks = list("apdwq")
            rows = [[InlineKeyboardButton(text=GAME_NAMES[k], callback_data=f"gm:{k}") for k in ks[i:i + 2]] for i in range(0, len(ks), 2)]
            if chat_id in GAMES:
                rows.append([InlineKeyboardButton(text="⏹ To'xtatish", callback_data="gm:stop")])
            await c.answer()
            return await c.message.edit_text(parse_mode="HTML", text="Qaysi o'yin? 👇", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        if act == "stop":
            g = GAMES.get(chat_id)
            if g:
                g["task"].cancel()
                GAMES.pop(chat_id, None)
            await c.answer("To'xtatildi")
            return await c.message.edit_text(parse_mode="HTML", text="⏹ To'xtatildi. Yangisi: /oyin", reply_markup=None)
        if act in GAME_NAMES:
            if chat_id in GAMES:
                return await c.answer("Hozir o'yin ketyapti", show_alert=True)
            if act == "w":  # yozma javoblarni bot faqat guruhda admin bo'lsa ko'radi (Telegram maxfiylik qoidasi)
                try:
                    me = await bot.me()
                    okb = (await bot.get_chat_member(chat_id, me.id)).status in ("administrator", "creator")
                except Exception:
                    okb = False
                if not okb:
                    return await c.answer("«So'zni yig'» uchun botni guruhda admin qiling. Boshqa ikki o'yin admin'siz ham ishlaydi.", show_alert=True)
            blk = None
            if act in ("a", "p", "d"):  # bitta blok tanlanadi va paketi OLDINDAN yuklanadi: o'yin davomida kutish bo'lmaydi
                try:
                    await c.answer("Tayyorlanmoqda...")
                    cnt = {}
                    for x in pool():
                        cnt[(x[0] - 13) // 100] = cnt.get((x[0] - 13) // 100, 0) + 1
                    blk = random.choice([k for k, v in cnt.items() if v >= 12])
                    await asyncio.wait_for(get_pack("p" if act == "a" else "g", blk), 30)
                except Exception as e:
                    print("media paketi yuklanmadi:", type(e).__name__, str(e)[:80])
                    return await c.message.answer("⚠️ Rasm/ovoz hozir yuklanmayapti. Boshqa o'yinni tanlang yoki keyinroq urinib ko'ring.")
            if act not in ("a", "p", "d"):
                await c.answer("Boshlandi!")
            GAMES[chat_id] = {"kind": act, "rid": 0, "sc": {}, "cur": None, "blk": blk}
            GAMES[chat_id]["task"] = asyncio.create_task(run_game(chat_id, act))
            return await c.message.edit_text(parse_mode="HTML", text=f"{GAME_NAMES[act]}\n{GAME_INTRO[act]}", reply_markup=await bot_kb())
    if len(d) > 2 and d[0] == "g" and d[1] in "apdq" and d[2] == ":":
        g = GAMES.get(chat_id)
        try:
            _, rid, i = d.split(":")
            rid, i = int(rid), int(i)
        except ValueError:
            return await c.answer()
        cur = g["cur"] if g else None
        if not cur or cur["rid"] != rid:
            return await c.answer("Bu savol tugagan")
        if uid in cur["done"]:
            return await c.answer("Siz topdingiz ✅ Keyingisini kuting")
        if uid in cur["wrong"]:
            return await c.answer("Xato qildingiz, keyingi savolni kuting")
        if i == cur["ans"]:
            pts = give(g, cur, c.from_user)
            return await c.answer(f"✅ To'g'ri! +{pts}")
        cur["wrong"].add(uid)
        return await c.answer("❌ Xato")
    await c.answer()


# ---------------- hammaga xabar (reklama) ----------------
bc_draft = {}  # admin -> (chat_id, message_id)
bg = set()     # fon vazifalarini xotirada ushlab turadi


def bc_wait(m):  # admin hozir reklama xabarini yuborayotgan bo'lsa (buyruqlar bundan mustasno)
    return bool(m.from_user) and pending.get(m.from_user.id, (None,))[0] == "bc" and not (m.text or "").startswith("/")


@dp.message(bc_wait)
async def bc_input(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return
    pending.pop(uid, None)
    bc_draft[uid] = (m.chat.id, m.message_id)
    n = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    await bot.copy_message(uid, m.chat.id, m.message_id)  # ko'rib chiqish uchun nusxa
    await m.answer(f"👆 Shu xabar {n} ta foydalanuvchiga yuboriladi. Tasdiqlaysizmi?",
                   reply_markup=kb_of([("✅ Yuborish", "bc:go"), ("❌ Bekor", "bc:no")]))


@dp.callback_query(F.data.startswith("bc:"))
async def bc_cb(c: CallbackQuery):
    uid = c.from_user.id
    if not is_admin(uid):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    d = bc_draft.pop(uid, None)
    if c.data == "bc:no" or not d:
        await edit(c, "Bekor qilindi." if c.data == "bc:no" else "Xabar topilmadi, qaytadan /admin dan boshlang.", None)
        return await c.answer()
    await edit(c, "⏳ Yuborilmoqda... tugagach natijani yozaman.", None)
    t = asyncio.create_task(broadcast(uid, *d))
    bg.add(t)
    t.add_done_callback(bg.discard)
    await c.answer("Boshlandi")


async def broadcast(admin_id, chat_id, msg_id):
    ids = [r[0] for r in db.execute("SELECT user_id FROM users")]
    ok = bad = 0
    for uid in ids:
        for _ in range(2):
            try:
                await bot.copy_message(uid, chat_id, msg_id)
                ok += 1
                break
            except TelegramRetryAfter as e:  # Telegram tezlikni cheklasa, kutib qayta urinadi
                await asyncio.sleep(e.retry_after + 1)
            except Exception:  # botni bloklagan va h.k.
                bad += 1
                break
        else:
            bad += 1
        await asyncio.sleep(0.05)  # soniyasiga ~20 ta: Telegram chegarasidan past
    await bot.send_message(admin_id, f"📢 Yuborish tugadi.\n✅ Yetkazildi: {ok}\n❌ Yetmadi: {bad} (botni bloklaganlar va h.k.)")


# ---------------- darslarni yo'qotmaslik: zaxira va linklar ro'yxati ----------------
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")


def make_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, datetime.now(TZ).strftime("zaxira-%Y%m%d-%H%M%S.db"))
    snapshot(path)
    for f in sorted(x for x in os.listdir(BACKUP_DIR) if x.startswith("zaxira-"))[:-14]:  # oxirgi 14 tasi qoladi
        os.remove(os.path.join(BACKUP_DIR, f))
    return path


async def send_backup(admin_ids):
    path = make_backup()
    for aid in admin_ids:
        try:
            await bot.send_document(aid, FSInputFile(path), caption=(
                "💾 Zaxira nusxa: darslar, foydalanuvchilar va to'lovlar shu faylda.\n"
                "Kerak bo'lsa, shu faylni data.db nomi bilan eski joyiga qo'ying, hammasi qaytadi."))
        except Exception as e:
            print("zaxira yuborilmadi:", aid, e)


def links_text():
    out = []
    for lv in LESSON_LEVELS:
        ls = lessons_of(lv)
        out.append(f"=== {lv} ({len(ls)} ta dars) ===")
        for i, (_, u, t) in enumerate(ls, 1):
            out.append(f"{i}. DARS: {u}\n   TEST: {t or 'YO`Q'}")
        out.append("")
    return "\n".join(out)


async def send_links(uid):
    await bot.send_document(uid, BufferedInputFile(links_text().encode("utf-8"), filename="linklar.txt"),
                            caption="📋 Barcha dars va test linklaringiz.")


@dp.message(Command("zaxira"))
async def cmd_backup(m: Message):
    if is_admin(m.from_user.id):
        await send_backup([m.from_user.id])


@dp.message(Command("linklar"))
async def cmd_links(m: Message):
    if is_admin(m.from_user.id):
        await send_links(m.from_user.id)


async def backuper():
    """Har kuni soat 03:00 dan keyin bazaning zaxirasini adminlarga yuboradi."""
    while True:
        try:
            now_l = datetime.now(TZ)
            today = now_l.strftime("%Y-%m-%d")
            last = db.execute("SELECT v FROM meta WHERE k='last_backup'").fetchone()
            if now_l.hour >= 3 and (not last or last[0] != today):
                db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('last_backup', ?)", (today,))
                db.commit()
                await send_backup(ADMIN_IDS)
        except Exception as e:
            print("zaxira xatosi:", e)
        await asyncio.sleep(600)


# ---------------- majburiy obuna: admin paneldan boshqariladi ----------------
def sub_view():
    chs = sub_channels()
    text = "🔔 Majburiy obuna\n\n" + ("\n".join(f"{i}. {sub_name(c)}" for i, c in enumerate(chs, 1)) if chs else
                                       "Hozircha yo'q: botdan hamma erkin foydalanadi.")
    text += "\n\nO'quvchi shu kanal(lar)ga a'zo bo'lmaguncha botdan foydalana olmaydi."
    rows = [[(f"🗑 {sub_name(c)}", f"adm:subd:{i}")] for i, c in enumerate(chs)]
    rows += [[("➕ Kanal qo'shish", "adm:suba")], [("⬅️ Orqaga", "adm:home")]]
    return text, kb_of(*rows)


def suba_wait(m):
    return bool(m.from_user) and pending.get(m.from_user.id, (None,))[0] == "suba" and not (m.text or "").startswith("/")


@dp.message(suba_wait)
async def sub_add(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return
    name = (m.text or "").strip()
    if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,}", name):
        return await m.answer("Kanal nomini @username ko'rinishida yuboring, masalan @mening_kanalim. Yoki /admin.")
    try:
        chat = await bot.get_chat(name)
        me = await bot.me()
        st = (await bot.get_chat_member(chat.id, me.id)).status
    except Exception:
        return await m.answer("Kanal topilmadi yoki bot unda yo'q. Avval botni kanalga admin qilib qo'shing, keyin qayta yuboring.")
    if st not in ("administrator", "creator"):
        return await m.answer("Bot bu kanalda admin emas. Botni kanalga ADMIN qilib qo'shing, keyin qayta yuboring.")
    pending.pop(uid, None)
    chs = sub_channels()
    if name.lower() not in [x.lower() for x in chs]:
        chs.append(name)
        meta_set("sub_channels", json.dumps(chs))
        sub_ok.clear()
    text, kb = sub_view()
    await m.answer("✅ Qo'shildi\n\n" + text, reply_markup=kb)


# ---------------- baza zaxirasi Telegram kanalida ----------------
KANAL_FILE = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "kanal.txt")  # bazaning yonida
BACKUP_EVERY = int(os.getenv("BACKUP_EVERY", "3600"))  # zaxirani kamida shuncha soniyada bir marta yuboradi
cloud_last = [0.0]
cloud_total = [None]  # oxirgi yuborilgan paytdagi o'zgarishlar soni


def id_hint(cid):
    return (f"\n\nKanal ID: {cid}\nBaza butunlay o'chib ketsa ham bot kanalni topishi uchun, .env fayliga shu qatorni yozib qo'ying:\n"
            f"BACKUP_CHANNEL={cid}")


def get_backup_chat():
    v = os.getenv("BACKUP_CHANNEL", "").strip() or meta_get("backup_chat")  # .env dagi qiymat birinchi o'rinda
    if not v:
        try:
            with open(KANAL_FILE, encoding="utf-8") as f:
                v = f.read().strip()
        except OSError:
            v = None
    return int(v) if v and re.fullmatch(r"-?\d+", v) else None


def set_backup_chat(cid):
    meta_set("backup_chat", str(cid))
    try:
        with open(KANAL_FILE, "w", encoding="utf-8") as f:
            f.write(str(cid))
    except OSError:
        pass


def db_is_empty():
    return db.execute("SELECT (SELECT COUNT(*) FROM users WHERE level IS NOT NULL OR goal IS NOT NULL OR paid=1) "
                      "+ (SELECT COUNT(*) FROM lessons)").fetchone()[0] == 0


def snapshot(path):
    dst = sqlite3.connect(path)
    db.backup(dst)
    dst.close()


async def backup_to_channel(force=False):
    """None: yuborilmadi (kanal yo'q / baza bo'sh). True: yuborildi va qadaldi. False: yuborildi, lekin qadalmadi."""
    cid = get_backup_chat()
    if not cid or db_is_empty():
        return None
    if not force and (db.total_changes == cloud_total[0] or time.time() - cloud_last[0] < BACKUP_EVERY):
        return True
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, "kanal-zaxira.db")
    snapshot(path)
    now_l = datetime.now(TZ)
    today = now_l.strftime("%Y-%m-%d")
    nu = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    nl = db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    nv = db.execute("SELECT COUNT(*) FROM users WHERE paid=1 AND (COALESCE(paid_until,0)=0 OR paid_until>strftime('%s','now'))").fetchone()[0]
    msg = await bot.send_document(cid, FSInputFile(path, filename="data.db"), disable_notification=True,
                                  caption=f"💾 Baza zaxirasi · {now_l:%Y-%m-%d %H:%M}\n👥 {nu} foydalanuvchi · 📚 {nl} dars · 💎 {nv} VIP")
    pinned = True
    try:
        await bot.pin_chat_message(cid, msg.message_id, disable_notification=True)
    except Exception as e:
        pinned = False
        print("zaxira qadalmadi (botga 'Xabarlarni qadash' huquqini bering):", e)
    prev, prev_day = meta_get("bk_msg"), meta_get("bk_day")
    if prev and prev_day == today:  # bir kunda bitta zaxira qoladi, kunlik eskilari saqlanadi
        try:
            await bot.delete_message(cid, int(prev))
        except Exception:
            pass
    meta_set("bk_msg", str(msg.message_id))
    meta_set("bk_day", today)
    cloud_total[0] = db.total_changes
    cloud_last[0] = time.time()
    return pinned


async def restore_from_channel():
    """(muvaffaqiyat, xabar). Kanalda qadalgan oxirgi zaxirani olib, bazani almashtiradi."""
    cid = get_backup_chat()
    if not cid:
        return False, "Zaxira kanali ulanmagan."
    try:
        chat = await bot.get_chat(cid)
    except Exception as e:
        return False, f"Kanalga kirib bo'lmadi: {e}"
    doc = getattr(chat.pinned_message, "document", None)
    if not doc:
        return False, "Kanalda qadalgan zaxira topilmadi."
    os.makedirs(BACKUP_DIR, exist_ok=True)
    tmp = os.path.join(BACKUP_DIR, "tiklash.tmp")
    try:
        await bot.download(doc.file_id, destination=tmp)
        src = sqlite3.connect(tmp)
        try:
            src.execute("SELECT COUNT(*) FROM users")
            src.execute("SELECT COUNT(*) FROM lessons")
            if not db_is_empty():
                make_backup()  # hozirgi bazani yo'qotmaslik uchun
            src.backup(db)
        finally:
            src.close()
    except Exception as e:
        return False, f"Zaxira faylini o'qib bo'lmadi: {e}"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    migrate()
    set_backup_chat(cid)
    nu = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    nl = db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    return True, f"Baza tiklandi: {nu} foydalanuvchi, {nl} dars."


async def cloud_view():
    cid = get_backup_chat()
    if cid:
        try:
            name = (await bot.get_chat(cid)).title or str(cid)
        except Exception:
            name = str(cid)
        text = (f"☁️ Zaxira kanali: {name}\n\nBaza o'zgarganda bot nusxasini shu YOPIQ kanalga yuboradi va qadab qo'yadi. "
                f"Baza yo'qolsa yoki bo'sh bo'lsa, shu yerdan o'zi tiklaydi.\nOxirgi zaxira kuni: {meta_get('bk_day') or 'hali yo`q'}")
        rows = [[("🔄 Hozir zaxiralash", "adm:cloudnow")], [("♻️ Kanaldan tiklash", "adm:cloudrestore")],
                [("🔗 Boshqa kanal ulash", "adm:cloudset")], [("⬅️ Orqaga", "adm:home")]]
    else:
        text = ("☁️ Zaxira kanali ulanmagan.\n\n1) Telegramda YOPIQ (private) kanal yarating.\n"
                "2) Botni o'sha kanalga admin qiling («Xabar yozish» va «Xabarlarni qadash» huquqi bilan).\n"
                "Bot kanalni o'zi aniqlaydi. Aniqlamasa, «🔗 Ulash» ni bosib, kanaldan istalgan postni shu yerga forward qiling.")
        rows = [[("🔗 Ulash", "adm:cloudset")], [("⬅️ Orqaga", "adm:home")]]
    return text, kb_of(*rows)


@dp.my_chat_member()
async def on_added(e: ChatMemberUpdated):
    """Admin botni kanal/guruhga ADMIN qilib qo'shsa, u o'sha zahoti majburiy obunaga qo'shiladi. Boshqa hech narsa qilinmaydi."""
    if e.chat.type not in ("channel", "supergroup", "group"):
        return
    if e.new_chat_member.status in ("left", "kicked") or (e.new_chat_member.status == "member" and e.old_chat_member.status == "administrator"):
        # bot chatdan chiqarildi yoki admin huquqi olindi: tekshirib bo'lmaydigan chat obuna ro'yxatida qolib ketmasin
        chs = sub_channels()
        keep = [c for c in chs if not (sub_parts(c)[0] == str(e.chat.id) or (e.chat.username and sub_parts(c)[0].lower() == "@" + e.chat.username.lower()))]
        if len(keep) != len(chs):
            meta_set("sub_channels", json.dumps(keep))
            sub_ok.clear()
            for a in ADMIN_IDS:
                try:
                    await bot.send_message(a, f"ℹ️ «{e.chat.title}» majburiy obunadan olib tashlandi (bot u yerda endi admin emas).")
                except Exception:
                    pass
        return
    if not e.from_user or not is_admin(e.from_user.id) or e.new_chat_member.status != "administrator":
        return
    try:
        link = "" if e.chat.username else await bot.export_chat_invite_link(e.chat.id)
    except Exception:
        return
    entry = ("@" + e.chat.username) if e.chat.username else f"{e.chat.id}|{link}|{(e.chat.title or 'Guruh')[:40]}"
    chs = sub_channels()
    if entry not in chs:
        chs.append(entry)
        meta_set("sub_channels", json.dumps(chs))
        sub_ok.clear()


def kanal_wait(m):
    return bool(m.from_user) and pending.get(m.from_user.id, (None,))[0] == "kanal" and not (m.text or "").startswith("/")


@dp.message(kanal_wait)
async def kanal_input(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return
    chat = None
    fo = getattr(m, "forward_origin", None)
    try:
        if fo is not None and getattr(fo, "chat", None) is not None:
            chat = fo.chat
        elif m.text and re.fullmatch(r"-?\d+", m.text.strip()):
            chat = await bot.get_chat(int(m.text.strip()))
    except Exception:
        chat = None
    if chat is None and m.text and ("t.me/+" in m.text or "joinchat" in m.text):
        return await m.answer("Taklif havolasi (t.me/+...) botga kanalni aniqlab bera olmaydi, Telegram bunga ruxsat bermaydi.\n"
                              "Kanaldan istalgan postni forward qiling yoki kanal ID sini yuboring (-100... bilan boshlanadi). "
                              "Yoki oddiygina botni kanalga admin qilib qo'shing, u o'zi ulanadi.")
    if chat is None:
        return await m.answer("Kanal aniqlanmadi. Kanaldan biror postni forward qiling yoki kanal ID sini yuboring. Bekor qilish: /admin")
    if chat.username:
        return await m.answer("⚠️ Bu ochiq kanal. Bazada foydalanuvchilar ma'lumoti bor, shuning uchun YOPIQ (private) kanal kerak.")
    try:
        me = await bot.me()
        if (await bot.get_chat_member(chat.id, me.id)).status not in ("administrator", "creator"):
            raise ValueError
    except Exception:
        return await m.answer("Bot bu kanalda admin emas. Botni kanalga admin qiling («Xabar yozish» va «Xabarlarni qadash»), keyin qayta yuboring.")
    pending.pop(uid, None)
    set_backup_chat(chat.id)
    if db_is_empty():
        ok, msg = await restore_from_channel()
        return await m.answer(("♻️ " if ok else "⚠️ ") + msg)
    try:
        res = await backup_to_channel(force=True)
    except Exception as e:
        return await m.answer(f"Kanal ulandi, lekin zaxira yuborib bo'lmadi: {e}")
    await m.answer("☁️ Kanal ulandi. " + ("✅ Birinchi zaxira yuborildi." if res else "⚠️ Yuborildi, lekin qadab bo'lmadi: botga «Xabarlarni qadash» huquqini bering.") + id_hint(chat.id))


async def cloud_backuper():
    """Har daqiqada: baza o'zgargan bo'lsa, yangi zaxirani kanalga yuboradi."""
    while True:
        try:
            await backup_to_channel()
        except Exception as e:
            print("kanalga zaxira xatosi:", e)
        await asyncio.sleep(60)


async def startup_restore():
    """Bot yoqilganda baza bo'sh bo'lsa: kanal ma'lum bo'lsa tiklaydi, bo'lmasa adminga bir marta eslatadi."""
    if not db_is_empty():
        return
    if get_backup_chat():
        ok, msg = await restore_from_channel()
        text = ("♻️ Baza bo'sh edi, zaxira kanalidan tiklandi. " if ok else "⚠️ Baza bo'sh, tiklab bo'lmadi. ") + msg
    elif not meta_get("cloud_hint"):
        meta_set("cloud_hint", "1")
        text = ("ℹ️ Baza hozircha bo'sh va zaxira kanali ulanmagan. Eski bazangiz kanalda bo'lsa: /admin → ☁️ Zaxira kanali → 🔗 Ulash. "
                "Yangi bo'lsa: yopiq kanal yaratib, botni unga admin qiling, baza o'sha yerda saqlanadi.")
    else:
        return
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            pass


async def credit_partners(uid, amount):
    """To'lov tasdiqlanganda BITTA odamga ulush yoziladi: promokod bo'lsa uning egasiga, bo'lmasa taklif qilgan do'stga.
    Ulush HOLD_DAYS kundan keyin yechsa bo'ladi."""
    who = sm = None
    kind = ""
    pr = promo_of(uid)
    if pr and pr[3] and pr[2] > 0 and pr[3] != uid:
        who, sm, kind = pr[3], amount * pr[2] // 100, "promokod " + pr[0]
    else:
        r = db.execute("SELECT inviter FROM referrals WHERE invited=? AND ok=1", (uid,)).fetchone()
        if r and REF_PCT > 0 and r[0] != uid and not (pr and pr[3] == r[0]):
            who, sm, kind = r[0], amount * REF_PCT // 100, "do'st"
    if not who or sm <= 0:
        return
    db.execute("INSERT INTO earnings(user_id, from_user, amount, ts, kind) VALUES(?, ?, ?, ?, ?)", (who, uid, sm, int(time.time()), kind))
    db.commit()
    try:
        await bot.send_message(who, f"💰 Ajoyib! Sizning havolangiz orqali kimdir VIP oldi.\n+{money(sm)} hisobingizga yozildi "
                                    f"({HOLD_DAYS} kundan keyin yechsa bo'ladi).\nKo'rish: /pul")
    except Exception:
        pass


chosen_plan = {}


async def start_pro(uid, answer):
    if not PAID_MODE:
        return await answer("Hozircha barcha darslar bepul 🙂")
    if is_lifetime(uid):
        return await answer("Siz allaqachon umrbod VIP dasiz 🙂")
    if db.execute("SELECT 1 FROM payments WHERE user_id=? AND status='pending'", (uid,)).fetchone():
        return await answer("⏳ Chekingiz allaqachon adminga yuborilgan, tekshirilmoqda. Biroz kuting.")
    if not user(uid)[1]:
        return await answer("Avval /start bosib, darajangizni aniqlang.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"{PLANS[k][0]} — {money(price_for(uid, k)[0])}", callback_data=f"plan:{k}")] for k in ("1", "3", "12")])
    await answer("💎 VIP tarifini tanlang (rus + ingliz tili):\n\n" + plans_text(uid) +
                 "\n\n🎟 Promokod bo'lsa: /kod KODINGIZ", reply_markup=kb)


@dp.callback_query(F.data.startswith("plan:"))
async def plan_cb(c: CallbackQuery):
    uid = c.from_user.id
    k = c.data.split(":")[1]
    if k not in PLANS:
        return await c.answer()
    chosen_plan[uid] = k
    awaiting.add(uid)
    db.execute("INSERT OR REPLACE INTO pay_wait(user_id, plan) VALUES(?, ?)", (uid, k))
    db.commit()
    await c.message.answer(pay_info(uid, k) + "\n\n" + await ref_text(uid))
    await c.answer()


@dp.callback_query(F.data == "pro:start")
async def pro_cb(c: CallbackQuery):
    await start_pro(c.from_user.id, c.message.answer)
    await c.answer()


@dp.message(Command("vip", "pulli"))
async def cmd_pro(m: Message):
    await start_pro(m.from_user.id, m.answer)


@dp.message(F.photo | F.document)
async def got_receipt(m: Message):
    uid = m.from_user.id
    if is_lifetime(uid):
        return await m.answer("Siz allaqachon umrbod VIP dasiz 🙂")
    if uid not in awaiting:
        w = db.execute("SELECT plan FROM pay_wait WHERE user_id=?", (uid,)).fetchone()
        if w and w[0] in PLANS:  # bot qayta ishga tushgan: holatni bazadan tiklaymiz
            awaiting.add(uid)
            chosen_plan[uid] = w[0]
    if uid not in awaiting:
        return await m.answer("VIP ga o'tish uchun avval «💎 VIP ga o'tish» tugmasini bosing (/vip).")
    if db.execute("SELECT 1 FROM payments WHERE user_id=? AND status='pending'", (uid,)).fetchone():
        return await m.answer("⏳ Chekingiz allaqachon yuborilgan, tekshirilmoqda. Biroz kuting.")
    kind, fid = ("photo", m.photo[-1].file_id) if m.photo else ("document", m.document.file_id)
    uniq = m.photo[-1].file_unique_id if m.photo else m.document.file_unique_id
    plan = chosen_plan.get(uid, "3")
    amount = price_for(uid, plan)[0]
    pid = db.execute("INSERT INTO payments(user_id, file_id, kind, amount, plan, uniq) VALUES(?, ?, ?, ?, ?, ?)", (uid, fid, kind, amount, plan, uniq)).lastrowid
    flags = []
    if db.execute("SELECT 1 FROM payments WHERE uniq=? AND id<>?", (uniq, pid)).fetchone():
        flags.append("⚠️ Bu chek rasmi oldin ham yuborilgan (takror)!")
    ref = db.execute("SELECT inviter, ts FROM referrals WHERE invited=?", (uid,)).fetchone()
    pr = promo_of(uid)
    if ref and ref[1] and time.time() - ref[1] < 600:
        flags.append("⚠️ Havola bilan kirgan zahoti (10 daqiqa ichida) to'lov yubordi")
    if ref:
        n_inv = db.execute("SELECT COUNT(*) FROM earnings WHERE user_id=?", (ref[0],)).fetchone()[0]
        flags.append(f"👥 Taklif qilgan: ID {ref[0]} (oldin {n_inv} ta sotuv keltirgan)")
    if pr:
        flags.append(f"🏷 Promokod: {pr[0]} (egasi {pr[3] or '—'})")
    db.execute("DELETE FROM pay_wait WHERE user_id=?", (uid,))
    db.commit()
    awaiting.discard(uid)
    kb = kb_of([("✅ Ha, qo'shish", f"pay:ok:{pid}"), ("❌ Yo'q", f"pay:no:{pid}")])
    cap = (f"💳 Chek #{pid}\n👤 {getattr(m.from_user, 'full_name', '')} (ID {uid})\n"
           f"📚 Tugatgan dars: {finished_count(uid)}\n📦 Tarif: {PLANS[plan][0]}\n💰 Kutilgan summa: {money(amount) if amount else PRICE}\n"
           + ("\n".join(flags) + "\n" if flags else "") + "\nBank ilovangizda pul tushganini tekshiring, so'ng tasdiqlang.")
    sent = 0
    for aid in ADMIN_IDS:
        try:
            send = bot.send_photo if kind == "photo" else bot.send_document
            await send(aid, fid, caption=cap, reply_markup=kb)
            sent += 1
        except Exception:
            pass
    if not sent:
        db.execute("UPDATE payments SET status='error' WHERE id=?", (pid,))
        db.commit()
        return await m.answer("Chekni adminga yetkazib bo'lmadi. Keyinroq qayta urinib ko'ring.")
    await m.answer("✅ Chek adminga yuborildi. Tasdiqlansa, sizga xabar keladi.")


@dp.callback_query(F.data.startswith("pay:"))
async def pay_cb(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Ruxsat yo'q", show_alert=True)
    _, act, pid = c.data.split(":")
    row = db.execute("SELECT user_id, status, amount, COALESCE(plan,'L') FROM payments WHERE id=?", (int(pid),)).fetchone()
    if not row or row[1] != "pending":
        return await c.answer("Bu chek allaqachon ko'rib chiqilgan", show_alert=True)
    uid = row[0]
    ok = act == "ok"
    db.execute("UPDATE payments SET status=? WHERE id=?", ("approved" if ok else "rejected", int(pid)))
    until = 0
    if ok:
        until = grant_vip(uid, PLANS.get(row[3], PLANS["L"])[1])
    db.commit()
    if ok and (row[2] or 0) > 0:
        await credit_partners(uid, row[2])
    try:
        await c.message.edit_caption(caption=(c.message.caption or "") + ("\n\n✅ QO'SHILDI" if ok else "\n\n❌ QO'SHILMADI"),
                                     reply_markup=None)
    except Exception:
        pass
    try:
        if ok:
            till = ("\n📅 Amal qilish muddati: " + datetime.fromtimestamp(until, TZ).strftime("%d.%m.%Y")) if until else "\n♾ Umrbod"
            await bot.send_message(uid, "🎉 Tabriklaymiz! Siz VIP ga o'tdingiz!" + till + "\n\nEndi kursning hamma darslari (rus va ingliz) sizga ochiq. Omad! 🚀")
            await push_lesson(uid)
            await bot.send_message(uid, "💸 Endi siz ham pul ishlashingiz mumkin!\n\n" + await ref_text(uid))
        else:
            await bot.send_message(uid, "❌ Admin chekni tasdiqlamadi. Chek aniq ko'rinishi va summa to'g'ri bo'lishi kerak. "
                                        "Qaytadan «💎 VIP ga o'tish» ni bosib, chekni yuboring.")
    except Exception:
        pass
    await c.answer("Bajarildi")


@dp.message(Command("ruxsat"))
async def cmd_grant(m: Message):  # faqat admin: /ruxsat 123456789 — to'lovsiz (qo'lda) to'liq ruxsat berish
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Ishlatish: /ruxsat <foydalanuvchi ID>")
    uid = int(parts[1])
    if not db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
        return await m.answer("Bunday foydalanuvchi topilmadi.")
    grant_vip(uid)
    db.commit()
    await m.answer(f"✅ {uid} ga to'liq ruxsat berildi.")
    try:
        await bot.send_message(uid, "🎉 Tabriklaymiz! Siz VIP ga o'tdingiz!")
        await push_lesson(uid)
    except Exception:
        pass


@dp.message(Command("ochish"))
async def cmd_unlock(m: Message):  # faqat admin: sinash uchun keyingi darsni darhol ochadi
    if not is_admin(m.from_user.id):
        return
    db.execute("UPDATE users SET next_at=0, notified=1 WHERE user_id=?", (m.from_user.id,))
    db.commit()
    await show_lesson(m, m.from_user.id)


@dp.message(Command("vip_royxat", "viplar"))
async def cmd_vip_list(m: Message):  # faqat admin: VIP o'quvchilar (ism, @nik, daraja, tarif, muddat) va tushum
    if not is_admin(m.from_user.id):
        return
    rows = db.execute("SELECT user_id, lang, level, alt_level, COALESCE(paid_until,0) FROM users "
                      "WHERE paid=1 AND (COALESCE(paid_until,0)=0 OR paid_until>?) ORDER BY user_id", (int(time.time()),)).fetchall()
    if not rows:
        return await m.answer("Hozircha VIP o'quvchi yo'q.")
    pays = db.execute("SELECT plan, COUNT(*), COALESCE(SUM(amount),0) FROM payments WHERE status='approved' GROUP BY plan").fetchall()
    total = sum(x[2] for x in pays)
    lastplan = {r[0]: r[1] for r in db.execute("SELECT user_id, plan FROM payments WHERE status='approved' ORDER BY id")}
    out = [f"💎 VIP o'quvchilar: {len(rows)} ta", f"💰 Tasdiqlangan to'lovlar: {sum(x[1] for x in pays)} ta, jami {money(total)}"]
    if pays:
        out.append("📦 " + " · ".join(f"{PLANS.get(x[0] or 'L', PLANS['L'])[0]}: {x[1]}" for x in pays))
    out.append("")
    for uid, lang, lv, alt, until in rows[:60]:
        try:
            ch = await bot.get_chat(uid)
            nm = html.escape(ch.full_name or str(uid), quote=False)
            un = f" @{html.escape(ch.username, quote=False)}" if ch.username else ""
        except Exception:
            nm, un = str(uid), ""
        cur = lang if lang in LANG_NAMES else "ru"
        ru, en = (lv, alt) if cur == "ru" else (alt, lv)
        till = "umrbod" if not until else datetime.fromtimestamp(until, TZ).strftime("%d.%m.%Y")
        pl = lastplan.get(uid)
        plname = PLANS[pl][0] if pl in PLANS else "admin/sovg'a"
        out.append(f'👤 <a href="tg://user?id={uid}">{nm}</a>{un} · ID {uid}\n   🇷🇺 {ru or "—"} · 🇬🇧 {en or "—"} · {finished_count(uid)} dars · {plname} · {till}')
    if len(rows) > 60:
        out.append(f"... va yana {len(rows) - 60} ta")
    out.append("\nDarajani o'zgartirish: /daraja ID A1 [ru|en]")
    chunk = ""
    for ln in out:
        if len(chunk) + len(ln) > 3800:
            await m.answer(chunk, parse_mode="HTML")
            chunk = ""
        chunk += ln + "\n"
    if chunk:
        await m.answer(chunk, parse_mode="HTML")


@dp.message(Command("daraja"))
async def cmd_set_level(m: Message):  # faqat admin: /daraja 123456789 A1 [ru|en] — o'quvchi darajasini qo'lda o'zgartirish
    if not is_admin(m.from_user.id):
        return
    p = (m.text or "").split()
    if len(p) < 3 or not p[1].isdigit() or p[2].upper() not in LEVELS:
        return await m.answer("Ishlatish: /daraja <ID> <daraja> [ru|en]\nMasalan: /daraja 123456789 A1\nDarajalar: " + ", ".join(LEVELS) +
                              "\nTil yozilmasa, o'quvchining hozirgi tili olinadi. Ro'yxat: /vip_royxat")
    uid, lvl = int(p[1]), p[2].upper()
    if not db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
        return await m.answer("Bunday foydalanuvchi topilmadi.")
    cur = ulang(uid)
    lang = p[3].lower() if len(p) > 3 and p[3].lower() in LANG_NAMES else cur
    if lang == cur:
        db.execute("UPDATE users SET level=?, next_at=0, notified=1 WHERE user_id=?", (lvl, uid))
        if lang == "ru" and lvl != "A0":
            meta_set(f"pl{uid}", "11")   # alifbo/sanash savoli qayta so'ralmasin
    else:
        db.execute("UPDATE users SET alt_level=? WHERE user_id=?", (lvl, uid))
    db.commit()
    await m.answer(f"✅ {uid}: {LANG_NAMES[lang]} darajasi {lvl} ga o'zgardi.")
    try:
        await bot.send_message(uid, f"📈 Admin darajangizni {lvl} ga o'zgartirdi ({LANG_NAMES[lang]}).")
        if lang == cur:
            await push_lesson(uid)
    except Exception:
        pass


@dp.message(Command("boshidan"))
async def cmd_restart_me(m: Message):  # faqat admin: o'zini yangi o'quvchidek sinash (darslar va A0 savollari qaytadan)
    uid = m.from_user.id
    if not is_admin(uid):
        return
    db.execute("DELETE FROM progress WHERE user_id=?", (uid,))
    db.execute("DELETE FROM meta WHERE k=?", (f"pl{uid}",))
    arg = (m.text or "").split(maxsplit=1)[1].strip().upper() if len((m.text or "").split(maxsplit=1)) > 1 else "A0"
    lvl = arg if arg in LEVELS else "A0"
    db.execute("INSERT INTO users(user_id, level) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET level=excluded.level, next_at=0, notified=1",
               (uid, lvl))
    db.commit()
    goal, level = user(uid)
    await m.answer(f"♻️ Sinash uchun boshidan boshlandi (daraja: {level}). Darslaringiz tozalandi.\n"
                   "Boshqa daraja: /boshidan B1")
    if level:
        await show_lesson(m, uid)


async def send_safe(coro_fn, tries=3):
    """Telegram 429 (RetryAfter) bo'lsa kutib qayta urinadi. Bloklagan foydalanuvchida False qaytaradi."""
    for _ in range(tries):
        try:
            await coro_fn()
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(min(e.retry_after, 30) + 1)
        except TelegramForbiddenError:
            return False
    return False


async def notifier():
    """Har daqiqada: vaqti kelgan foydalanuvchilarga 'yangi dars ochildi' deb yozadi (sekin-asta, Telegram limitidan oshmasdan)."""
    while True:
        try:
            rows = db.execute("SELECT user_id FROM users WHERE notified=0 AND next_at>0 AND next_at<=?", (now(),)).fetchall()
            for (uid,) in rows:
                try:
                    if paywalled(uid):
                        db.execute("UPDATE users SET notified=1 WHERE user_id=?", (uid,))
                        db.commit()
                        continue
                    ok = await send_safe(lambda: bot.send_message(uid, new_lesson_text(uid)))
                    if ok:  # faqat yuborilgandan keyin belgilaymiz: tarmoq xatosida foydalanuvchi yo'qolmaydi
                        db.execute("UPDATE users SET notified=1 WHERE user_id=?", (uid,))
                        db.commit()
                        await send_safe(lambda: push_lesson(uid))
                    elif not await _can_reach(uid):
                        db.execute("UPDATE users SET notified=1 WHERE user_id=?", (uid,))  # bloklagan: qayta urinmaymiz
                        db.commit()
                except Exception as e:
                    print("notifier (foydalanuvchi):", uid, e)
                await asyncio.sleep(0.1)  # har foydalanuvchiga 2 xabar => ~20 xabar/soniya: Telegram limiti (~30) dan past
        except Exception as e:
            print("notifier xatosi:", e)
        await asyncio.sleep(60)


async def _can_reach(uid):
    """Bloklaganmi? (chat harakatini tekshiradi). Tarmoq xatosida True: keyingi daqiqada qayta urinadi."""
    try:
        await bot.send_chat_action(uid, "typing")
        return True
    except TelegramForbiddenError:
        return False
    except Exception:
        return True


async def housekeeping():
    """Har 10 daqiqada: xotirada to'planib qoladigan narsalarni tozalaydi."""
    while True:
        await asyncio.sleep(600)
        try:
            t = time.time()
            for k in [k for k, v in sub_ok.items() if v < t]:
                sub_ok.pop(k, None)
            for k in [k for k, v in flood.items() if not v or t - v[-1] > 60]:
                flood.pop(k, None)
            try:
                db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error:
                pass
        except Exception as e:
            print("tozalash xatosi:", e)


@dp.errors()
async def on_error(event):
    """Kutilmagan xato bitta foydalanuvchini yiqitsa ham, bot ishlashda davom etadi."""
    import traceback
    print("xato:", repr(event.exception))
    traceback.print_exception(event.exception)   # logda qaysi qatorda yiqilgani ko'rinsin
    return True


# ---------------- Groq kaliti (suhbat saytidagi ustoz uchun) va dars mavzusi ----------------
GROQ = "https://api.groq.com/openai/v1"
KEYS_PATH = os.getenv("KEYS_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(DB_PATH)) if DB_PATH != ":memory:" else os.getcwd(), "keys.json")

def groq_keys():
    try:
        with open(KEYS_PATH, encoding="utf-8") as f:
            v = json.load(f).get("groq")
        v = [v] if isinstance(v, str) else (v or [])
        ks = [k for k in v if isinstance(k, str) and k]
        if ks:
            return ks
    except (OSError, ValueError, AttributeError):
        pass
    k = os.getenv("GROQ_API_KEY", "").strip()
    return [k] if k else []


def groq_key():
    ks = groq_keys()
    return ks[0] if ks else None


def save_groq_keys(ks):
    try:
        with open(KEYS_PATH, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d["groq"] = list(ks)
    with open(KEYS_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f)
    try:
        os.chmod(KEYS_PATH, 0o600)
    except OSError:
        pass


def set_groq_key(k):  # kalit qo'shadi (takrorlanmaydi)
    ks = groq_keys()
    if k not in ks:
        save_groq_keys(ks + [k])


def talk_topic(uid):
    """O'quvchi oxirgi tugatgan darsning mavzusi (admin yozgan), yo'q bo'lsa None."""
    r = db.execute("SELECT l.topic FROM progress p JOIN lessons l ON l.id=p.lesson_id "
                   "WHERE p.user_id=? AND l.topic IS NOT NULL AND l.topic<>'' ORDER BY p.rowid DESC LIMIT 1", (uid,)).fetchone()
    return r[0] if r else None


def talk_blocked(uid):
    """Suhbat hali ochilmagan bo'lsa, foydalanuvchiga aytiladigan xabar; ochiq bo'lsa None."""
    goal, level = user(uid)
    if not level:
        return "Avval /start bosib, darajangizni aniqlang."
    cur = current(uid)[1]
    if need_placement(uid) or (cur and cur[0] in intro_ids()):  # alifbo va sanash darslarida suhbat yo'q
        return ("🗣 Ovozli suhbat alifbo va sanash darslaridan keyin, A0-3 dan ochiladi. "
                "Avval harflar va raqamlarni o'rganib oling 🙂")
    return None


@dp.message(F.text == M_TALK)
async def menu_talk(m: Message):
    uid = m.from_user.id
    why = talk_blocked(uid)
    if why:
        return await m.answer(why)
    if RUMIYO_URL and TALK_SECRET and os.getenv("SUHBAT_ON") != "1":
        # Hozircha «Suhbat» tugmasi Rumiyo (AI ustoz) ni ochadi. Odam bilan suhbatni qaytarish: Environment ga SUHBAT_ON=1 qo'shing.
        if not (is_paid(uid) or is_admin(uid)):
            return await m.answer("🗣 AI ustoz bilan ovozli suhbat — VIP uchun.\n\nVIP bilan: kuniga 10 daqiqa jonli suhbat, istalgancha dars va ikkala til (rus + ingliz).\n"
                                  "Oddiy darslar esa bepul davom etadi 🙂", reply_markup=pro_kb())
        cur = current(uid)[1] or (0, "", None)
        return await m.answer(f"🗣 Suhbat\n\nRumiyo ustoz bilan {'inglizcha' if ulang(uid) == 'en' else 'ruscha'} gaplashing. Ustozni o'zingiz tanlaysiz: 😊 do'stona, 😠 jahldor, 🔥 ehtirosli yoki 🍯 shirin so'z. U bugungi darsni so'raydi. "
                              "Kuniga 10 daqiqa (VIP).",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗣 Rumiyo bilan gaplashish", web_app=WebAppInfo(url=rm_link(uid, cur, 0, 0)))]]))
    if not (TALK_URL and TALK_SECRET):
        return await m.answer("🗣 Suhbat bo'limi tez orada ochiladi.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎙 Suhbatni boshlash", web_app=WebAppInfo(url=talk_link(uid)))]])
    await m.answer("🗣 Suhbat\n\n👥 Boshqa o'quvchi bilan ruscha jonli gaplashing: «Suhbatni boshlash» ni bosing, tasodifiy odam bilan darhol ulanasiz.\n\n"
                   "🎮 Gaplashib turganingizda «O'yin» ni taklif qiling: rol olasiz (masalan, shifokor va bemor), navbat bilan 20 soniya gapirasiz. "
                   "Sun'iy intellekt gapingizni tekshiradi: to'g'ri bo'lsa ⭐, xato bo'lsa ❤️ ketadi (3 ta yurakcha bor).\n\n"
                   "Suhbat va o'yin hamma uchun bepul.", reply_markup=kb)


def topic_wait(m):
    return bool(m.from_user) and pending.get(m.from_user.id, (None,))[0] == "topic" and bool(m.text) and not m.text.startswith("/")


@dp.message(topic_wait)
async def topic_input(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return
    lid = pending.pop(uid)[1]
    row = db.execute("SELECT level FROM lessons WHERE id=?", (lid,)).fetchone()
    if not row:
        return await m.answer("Dars topilmadi.")
    db.execute("UPDATE lessons SET topic=? WHERE id=?", (m.text.strip()[:300], lid))
    db.commit()
    text, kb = level_view(row[0])
    await m.answer("✅ Mavzu saqlandi\n\n" + text, reply_markup=kb)


def groq_wait(m):
    return bool(m.from_user) and pending.get(m.from_user.id, (None,))[0] == "groq" and bool(m.text) and not m.text.startswith("/")


@dp.message(groq_wait)
async def groq_input(m: Message):
    uid = m.from_user.id
    if not is_admin(uid):
        return
    k = m.text.strip()
    try:
        await m.delete()  # kalit chatda qolmasin
    except Exception:
        pass
    if not (k.startswith("gsk_") and 20 < len(k) < 200 and " " not in k):
        return await bot.send_message(uid, "Bu Groq kaliti emas (gsk_ bilan boshlanishi kerak). Qayta yuboring yoki /admin.")
    try:
        async with aiohttp.ClientSession() as http, http.get(GROQ + "/models", headers={"Authorization": "Bearer " + k},
                                                             timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status in (401, 403):
                return await bot.send_message(uid, "❌ Kalit qabul qilinmadi (noto'g'ri yoki bekor qilingan).")
    except Exception as e:
        return await bot.send_message(uid, f"Groq bilan bog'lanib bo'lmadi, kalit saqlanmadi: {type(e).__name__}")
    pending.pop(uid, None)
    set_groq_key(k)
    sent = await push_keys() if (TALK_URL and TALK_SECRET) else None
    note = ("\n🌐 Suhbat saytiga ham yuborildi." if sent else "\n⚠️ Saytga yuborib bo'lmadi, bot o'zi qayta urinadi (5 daqiqada)." if sent is False else "")
    await bot.send_message(uid, f"✅ Groq kaliti saqlandi (…{k[-4:]}). Jami kalitlar: {len(groq_keys())}.\n"
                                "Kalit shu chatdan o'chirildi." + note, reply_markup=admin_home())



# ---------------- odam bilan suhbat: alohida sayt (suhbat-sayt/), bot unga imzolangan havola beradi ----------------
TALK_URL = os.getenv("TALK_URL", "").strip()        # alohida sayt manzili, masalan https://suhbat.example.com/
TALK_SECRET = os.getenv("TALK_SECRET", "").strip()  # bot va sayt uchun BIR XIL maxfiy so'z (uzun, tasodifiy)


async def push_keys():
    """Groq kalitlarini suhbat saytiga yuboradi (sayt boshqa serverda bo'lsa ham). Sayt kalitni faqat xotirasida saqlaydi."""
    if not (TALK_URL and TALK_SECRET):
        return False
    try:
        async with aiohttp.ClientSession() as http, http.post(TALK_URL.rstrip("/") + "/keys", json={"groq": groq_keys()},
                                                              headers={"X-Secret": TALK_SECRET},
                                                              timeout=aiohttp.ClientTimeout(total=100)) as r:  # tekin server uyg'onguncha kutadi
            return r.status == 200
    except Exception as e:
        print("kalitlarni saytga yuborib bo'lmadi:", type(e).__name__)
        return False


async def keys_pusher():
    """Har 1 daqiqada kalitlarni saytga qayta yuboradi (sayt qayta ishga tushsa, kalit yo'qolmasin; tekin server ham uxlamaydi)."""
    while True:
        await push_keys()
        await asyncio.sleep(60)


def hum_vip(uid):
    return is_admin(uid) or is_paid(uid) or not PAID_MODE


def talk_link(uid):
    """Foydalanuvchiga xos, imzolangan, 24 soatlik havola (ID, daraja, VIP holati ichida)."""
    import base64, hashlib, hmac
    body = base64.urlsafe_b64encode(json.dumps({"u": uid, "l": eff(user(uid)[1]), "v": int(hum_vip(uid)), "e": int(time.time()) + 86400,
                                                "g": ulang(uid), "o": GOALS.get(user(uid)[0], "umumiy")[:40], "p": (talk_topic(uid) or "")[:100]},
                                               separators=(",", ":"), ensure_ascii=False).encode()).decode().rstrip("=")
    sig = hmac.new(TALK_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{TALK_URL.rstrip('/')}/?t={body}.{sig}"


# ---------------- RuMio trener: 401 kunlik dars (GitHub Pages) ----------------
TRENER_URL = os.getenv("TRENER_URL", "https://shoxrux56.github.io/trener/").strip()
EN_TRENER_URL = os.getenv("EN_TRENER_URL", "https://shoxrux56.github.io/trening_ingilish/").strip()  # ingliz Trener (251 kun)
TRENER_DAYS = """A1|Salomlashish va tanishuv
A1|Salomlashish va tanishuv · 2-qism
A1|Salomlashish va tanishuv · 3-qism
A1|Salomlashish va tanishuv
A1|Salomlashish va tanishuv · 2-qism
A1|Salomlashish va tanishuv · 3-qism
A1|Salomlashish va tanishuv, Oila
A1|Salomlashish va tanishuv, Oila · 2-qism
A1|Salomlashish va tanishuv, Oila · 3-qism
A1|Oila
A1|Oila · 2-qism
A1|Oila · 3-qism
A1|Oila, Do'kon va bozor
A1|Oila, Do'kon va bozor · 2-qism
A1|Oila, Do'kon va bozor · 3-qism
A1|Do'kon va bozor
A1|Do'kon va bozor · 2-qism
A1|Do'kon va bozor · 3-qism
A1|Do'kon va bozor
A1|Do'kon va bozor · 2-qism
A1|Do'kon va bozor · 3-qism
A1|Do'kon va bozor, Sayohat va transport
A1|Do'kon va bozor, Sayohat va transport · 2-qism
A1|Do'kon va bozor, Sayohat va transport · 3-qism
A1|Sayohat va transport
A1|Sayohat va transport · 2-qism
A1|Sayohat va transport · 3-qism
A1|Sayohat va transport
A1|Sayohat va transport · 2-qism
A1|Sayohat va transport · 3-qism
A1|Sayohat va transport, Ovqat, dam olish va kundalik hayot
A1|Sayohat va transport, Ovqat, dam olish va kundalik hayot · 2-qism
A1|Sayohat va transport, Ovqat, dam olish va kundalik hayot · 3-qism
A1|Ovqat, dam olish va kundalik hayot
A1|Ovqat, dam olish va kundalik hayot · 2-qism
A1|Ovqat, dam olish va kundalik hayot · 3-qism
A1|Ovqat, dam olish va kundalik hayot
A1|Ovqat, dam olish va kundalik hayot · 2-qism
A1|Ovqat, dam olish va kundalik hayot · 3-qism
A1|Ovqat, dam olish va kundalik hayot, Vaqt va sanalar
A1|Ovqat, dam olish va kundalik hayot, Vaqt va sanalar · 2-qism
A1|Ovqat, dam olish va kundalik hayot, Vaqt va sanalar · 3-qism
A1|Vaqt va sanalar
A1|Vaqt va sanalar · 2-qism
A1|Vaqt va sanalar · 3-qism
A2|Vaqt va sanalar, Tabiat va ob-havo
A2|Vaqt va sanalar, Tabiat va ob-havo · 2-qism
A2|Vaqt va sanalar, Tabiat va ob-havo · 3-qism
A2|Tabiat va ob-havo
A2|Tabiat va ob-havo · 2-qism
A2|Tabiat va ob-havo · 3-qism
A2|Tabiat va ob-havo
A2|Tabiat va ob-havo · 2-qism
A2|Tabiat va ob-havo · 3-qism
A2|Tabiat va ob-havo, Uy va shahar
A2|Tabiat va ob-havo, Uy va shahar · 2-qism
A2|Tabiat va ob-havo, Uy va shahar · 3-qism
A2|Uy va shahar
A2|Uy va shahar · 2-qism
A2|Uy va shahar · 3-qism
A2|Uy va shahar, Buyruq, iltimos va majburiyat
A2|Uy va shahar, Buyruq, iltimos va majburiyat · 2-qism
A2|Uy va shahar, Buyruq, iltimos va majburiyat · 3-qism
A2|Buyruq, iltimos va majburiyat
A2|Buyruq, iltimos va majburiyat · 2-qism
A2|Buyruq, iltimos va majburiyat · 3-qism
A2|Buyruq, iltimos va majburiyat
A2|Buyruq, iltimos va majburiyat · 2-qism
A2|Buyruq, iltimos va majburiyat · 3-qism
A2|Buyruq, iltimos va majburiyat, Savollar
A2|Buyruq, iltimos va majburiyat, Savollar · 2-qism
A2|Buyruq, iltimos va majburiyat, Savollar · 3-qism
A2|Savollar
A2|Savollar · 2-qism
A2|Savollar · 3-qism
A2|Savollar, Kiyim va tashqi ko'rinish
A2|Savollar, Kiyim va tashqi ko'rinish · 2-qism
A2|Savollar, Kiyim va tashqi ko'rinish · 3-qism
A2|Kiyim va tashqi ko'rinish, So'zlashuv iboralari
A2|Kiyim va tashqi ko'rinish, So'zlashuv iboralari · 2-qism
A2|Kiyim va tashqi ko'rinish, So'zlashuv iboralari · 3-qism
A2|So'zlashuv iboralari
A2|So'zlashuv iboralari · 2-qism
A2|So'zlashuv iboralari · 3-qism
A2|So'zlashuv iboralari
A2|So'zlashuv iboralari · 2-qism
A2|So'zlashuv iboralari · 3-qism
A2|So'zlashuv iboralari
A2|So'zlashuv iboralari · 2-qism
A2|So'zlashuv iboralari · 3-qism
A2|So'zlashuv iboralari
A2|So'zlashuv iboralari · 2-qism
A2|So'zlashuv iboralari · 3-qism
A2|So'zlashuv iboralari, Raqamlar va o'lchamlar
A2|So'zlashuv iboralari, Raqamlar va o'lchamlar · 2-qism
A2|So'zlashuv iboralari, Raqamlar va o'lchamlar · 3-qism
A2|Raqamlar va o'lchamlar
A2|Raqamlar va o'lchamlar · 2-qism
A2|Raqamlar va o'lchamlar · 3-qism
A2|Raqamlar va o'lchamlar, Favqulodda holatlar va yordam
A2|Raqamlar va o'lchamlar, Favqulodda holatlar va yordam · 2-qism
A2|Raqamlar va o'lchamlar, Favqulodda holatlar va yordam · 3-qism
A2|Favqulodda holatlar va yordam
A2|Favqulodda holatlar va yordam · 2-qism
A2|Favqulodda holatlar va yordam · 3-qism
A2|Favqulodda holatlar va yordam, Yo'l so'rash
A2|Favqulodda holatlar va yordam, Yo'l so'rash · 2-qism
A2|Favqulodda holatlar va yordam, Yo'l so'rash · 3-qism
A2|Yo'l so'rash
A2|Yo'l so'rash · 2-qism
A2|Yo'l so'rash · 3-qism
A2|Yo'l so'rash, Fe'l tuslanishi: men, sen, u, biz, siz, ular
A2|Yo'l so'rash, Fe'l tuslanishi: men, sen, u, biz, siz, ular · 2-qism
A2|Yo'l so'rash, Fe'l tuslanishi: men, sen, u, biz, siz, ular · 3-qism
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular · 2-qism
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular · 3-qism
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular · 2-qism
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular · 3-qism
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular · 2-qism
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular · 3-qism
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular · 2-qism
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular · 3-qism
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular, Bor, yo'q, kerak, mumkin
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular, Bor, yo'q, kerak, mumkin · 2-qism
A2|Fe'l tuslanishi: men, sen, u, biz, siz, ular, Bor, yo'q, kerak, mumkin · 3-qism
A2|Bor, yo'q, kerak, mumkin
A2|Bor, yo'q, kerak, mumkin · 2-qism
A2|Bor, yo'q, kerak, mumkin · 3-qism
A2|Bor, yo'q, kerak, mumkin
A2|Bor, yo'q, kerak, mumkin · 2-qism
A2|Bor, yo'q, kerak, mumkin · 3-qism
B1|Bor, yo'q, kerak, mumkin, Uyda so'zlashuv
B1|Bor, yo'q, kerak, mumkin, Uyda so'zlashuv · 2-qism
B1|Bor, yo'q, kerak, mumkin, Uyda so'zlashuv · 3-qism
B1|Uyda so'zlashuv
B1|Uyda so'zlashuv · 2-qism
B1|Uyda so'zlashuv · 3-qism
B1|Uyda so'zlashuv
B1|Uyda so'zlashuv · 2-qism
B1|Uyda so'zlashuv · 3-qism
B1|Uyda so'zlashuv, Ranglar va buyumlar
B1|Uyda so'zlashuv, Ranglar va buyumlar · 2-qism
B1|Uyda so'zlashuv, Ranglar va buyumlar · 3-qism
B1|Ranglar va buyumlar
B1|Ranglar va buyumlar · 2-qism
B1|Ranglar va buyumlar · 3-qism
B1|Ranglar va buyumlar, Oziq-ovqat
B1|Ranglar va buyumlar, Oziq-ovqat · 2-qism
B1|Ranglar va buyumlar, Oziq-ovqat · 3-qism
B1|Oziq-ovqat
B1|Oziq-ovqat · 2-qism
B1|Oziq-ovqat · 3-qism
B1|Oziq-ovqat
B1|Oziq-ovqat · 2-qism
B1|Oziq-ovqat · 3-qism
B1|Oziq-ovqat, Odob va muloyim iboralar
B1|Oziq-ovqat, Odob va muloyim iboralar · 2-qism
B1|Oziq-ovqat, Odob va muloyim iboralar · 3-qism
B1|Odob va muloyim iboralar
B1|Odob va muloyim iboralar · 2-qism
B1|Odob va muloyim iboralar · 3-qism
B1|Odob va muloyim iboralar, Hafta kunlari va oylar
B1|Odob va muloyim iboralar, Hafta kunlari va oylar · 2-qism
B1|Odob va muloyim iboralar, Hafta kunlari va oylar · 3-qism
B1|Hafta kunlari va oylar
B1|Hafta kunlari va oylar · 2-qism
B1|Hafta kunlari va oylar · 3-qism
B1|Hafta kunlari va oylar, Salomlashish va xayrlashish
B1|Hafta kunlari va oylar, Salomlashish va xayrlashish · 2-qism
B1|Hafta kunlari va oylar, Salomlashish va xayrlashish · 3-qism
B1|Salomlashish va xayrlashish
B1|Salomlashish va xayrlashish · 2-qism
B1|Salomlashish va xayrlashish · 3-qism
B1|Salomlashish va xayrlashish, Narsa va joylar haqida
B1|Salomlashish va xayrlashish, Narsa va joylar haqida · 2-qism
B1|Salomlashish va xayrlashish, Narsa va joylar haqida · 3-qism
B1|Narsa va joylar haqida
B1|Narsa va joylar haqida · 2-qism
B1|Narsa va joylar haqida · 3-qism
B1|Narsa va joylar haqida, So'zlashuv tili
B1|Narsa va joylar haqida, So'zlashuv tili · 2-qism
B1|Narsa va joylar haqida, So'zlashuv tili · 3-qism
B1|So'zlashuv tili
B1|So'zlashuv tili · 2-qism
B1|So'zlashuv tili · 3-qism
B1|So'zlashuv tili
B1|So'zlashuv tili · 2-qism
B1|So'zlashuv tili · 3-qism
B1|So'zlashuv tili
B1|So'zlashuv tili · 2-qism
B1|So'zlashuv tili · 3-qism
B1|So'zlashuv tili, Buyruq shakllari
B1|So'zlashuv tili, Buyruq shakllari · 2-qism
B1|So'zlashuv tili, Buyruq shakllari · 3-qism
B1|Buyruq shakllari
B1|Buyruq shakllari · 2-qism
B1|Buyruq shakllari · 3-qism
B1|Buyruq shakllari
B1|Buyruq shakllari · 2-qism
B1|Buyruq shakllari · 3-qism
B1|Buyruq shakllari
B1|Buyruq shakllari · 2-qism
B1|Buyruq shakllari · 3-qism
B1|Buyruq shakllari
B1|Buyruq shakllari · 2-qism
B1|Buyruq shakllari · 3-qism
B1|Buyruq shakllari
B1|Buyruq shakllari · 2-qism
B1|Buyruq shakllari · 3-qism
B1|Buyruq shakllari, Ish va o'qish
B1|Buyruq shakllari, Ish va o'qish · 2-qism
B1|Buyruq shakllari, Ish va o'qish · 3-qism
B1|Ish va o'qish
B1|Ish va o'qish · 2-qism
B1|Ish va o'qish · 3-qism
B1|Ish va o'qish
B1|Ish va o'qish · 2-qism
B1|Ish va o'qish · 3-qism
B1|Ish va o'qish, Sog'liq
B1|Ish va o'qish, Sog'liq · 2-qism
B1|Ish va o'qish, Sog'liq · 3-qism
B1|Sog'liq
B1|Sog'liq · 2-qism
B1|Sog'liq · 3-qism
B1|Sog'liq
B1|Sog'liq · 2-qism
B1|Sog'liq · 3-qism
B1|Sog'liq, His-tuyg'ular va fikrlar
B1|Sog'liq, His-tuyg'ular va fikrlar · 2-qism
B1|Sog'liq, His-tuyg'ular va fikrlar · 3-qism
B1|His-tuyg'ular va fikrlar
B1|His-tuyg'ular va fikrlar · 2-qism
B1|His-tuyg'ular va fikrlar · 3-qism
B1|His-tuyg'ular va fikrlar, O'tgan va kelasi zamon, rejalar
B1|His-tuyg'ular va fikrlar, O'tgan va kelasi zamon, rejalar · 2-qism
B1|His-tuyg'ular va fikrlar, O'tgan va kelasi zamon, rejalar · 3-qism
B1|O'tgan va kelasi zamon, rejalar
B1|O'tgan va kelasi zamon, rejalar · 2-qism
B1|O'tgan va kelasi zamon, rejalar · 3-qism
B1|O'tgan va kelasi zamon, rejalar
B1|O'tgan va kelasi zamon, rejalar · 2-qism
B1|O'tgan va kelasi zamon, rejalar · 3-qism
B1|O'tgan va kelasi zamon, rejalar, Telefon, texnologiya, bank va pochta
B1|O'tgan va kelasi zamon, rejalar, Telefon, texnologiya, bank va pochta · 2-qism
B1|O'tgan va kelasi zamon, rejalar, Telefon, texnologiya, bank va pochta · 3-qism
B1|Telefon, texnologiya, bank va pochta
B1|Telefon, texnologiya, bank va pochta · 2-qism
B1|Telefon, texnologiya, bank va pochta · 3-qism
B1|Telefon, texnologiya, bank va pochta
B1|Telefon, texnologiya, bank va pochta · 2-qism
B1|Telefon, texnologiya, bank va pochta · 3-qism
B2|Telefon, texnologiya, bank va pochta, Taqqoslash, miqdor va sifat
B2|Telefon, texnologiya, bank va pochta, Taqqoslash, miqdor va sifat · 2-qism
B2|Telefon, texnologiya, bank va pochta, Taqqoslash, miqdor va sifat · 3-qism
B2|Taqqoslash, miqdor va sifat
B2|Taqqoslash, miqdor va sifat · 2-qism
B2|Taqqoslash, miqdor va sifat · 3-qism
B2|Taqqoslash, miqdor va sifat, Sport va qiziqishlar
B2|Taqqoslash, miqdor va sifat, Sport va qiziqishlar · 2-qism
B2|Taqqoslash, miqdor va sifat, Sport va qiziqishlar · 3-qism
B2|Sport va qiziqishlar
B2|Sport va qiziqishlar · 2-qism
B2|Sport va qiziqishlar · 3-qism
B2|Sport va qiziqishlar, Ovqat tayyorlash
B2|Sport va qiziqishlar, Ovqat tayyorlash · 2-qism
B2|Sport va qiziqishlar, Ovqat tayyorlash · 3-qism
B2|Ovqat tayyorlash
B2|Ovqat tayyorlash · 2-qism
B2|Ovqat tayyorlash · 3-qism
B2|Ovqat tayyorlash, Mehmonxona va restoran (dialoglar)
B2|Ovqat tayyorlash, Mehmonxona va restoran (dialoglar) · 2-qism
B2|Ovqat tayyorlash, Mehmonxona va restoran (dialoglar) · 3-qism
B2|Mehmonxona va restoran (dialoglar)
B2|Mehmonxona va restoran (dialoglar) · 2-qism
B2|Mehmonxona va restoran (dialoglar) · 3-qism
B2|Mehmonxona va restoran (dialoglar)
B2|Mehmonxona va restoran (dialoglar) · 2-qism
B2|Mehmonxona va restoran (dialoglar) · 3-qism
B2|Mehmonxona va restoran (dialoglar), Bayramlar va an'analar
B2|Mehmonxona va restoran (dialoglar), Bayramlar va an'analar · 2-qism
B2|Mehmonxona va restoran (dialoglar), Bayramlar va an'analar · 3-qism
B2|Bayramlar va an'analar
B2|Bayramlar va an'analar · 2-qism
B2|Bayramlar va an'analar · 3-qism
B2|Bayramlar va an'analar, Harakat fe'llari
B2|Bayramlar va an'analar, Harakat fe'llari · 2-qism
B2|Bayramlar va an'analar, Harakat fe'llari · 3-qism
B2|Harakat fe'llari
B2|Harakat fe'llari · 2-qism
B2|Harakat fe'llari · 3-qism
B2|Harakat fe'llari
B2|Harakat fe'llari · 2-qism
B2|Harakat fe'llari · 3-qism
B2|Harakat fe'llari, Kundalik tartib
B2|Harakat fe'llari, Kundalik tartib · 2-qism
B2|Harakat fe'llari, Kundalik tartib · 3-qism
B2|Kundalik tartib
B2|Kundalik tartib · 2-qism
B2|Kundalik tartib · 3-qism
B2|Kundalik tartib
B2|Kundalik tartib · 2-qism
B2|Kundalik tartib · 3-qism
B2|Kundalik tartib, Do'konda va bozorda (dialoglar)
B2|Kundalik tartib, Do'konda va bozorda (dialoglar) · 2-qism
B2|Kundalik tartib, Do'konda va bozorda (dialoglar) · 3-qism
B2|Do'konda va bozorda (dialoglar)
B2|Do'konda va bozorda (dialoglar) · 2-qism
B2|Do'konda va bozorda (dialoglar) · 3-qism
B2|Do'konda va bozorda (dialoglar)
B2|Do'konda va bozorda (dialoglar) · 2-qism
B2|Do'konda va bozorda (dialoglar) · 3-qism
B2|Do'konda va bozorda (dialoglar), Sayohat dialoglari
B2|Do'konda va bozorda (dialoglar), Sayohat dialoglari · 2-qism
B2|Do'konda va bozorda (dialoglar), Sayohat dialoglari · 3-qism
B2|Sayohat dialoglari
B2|Sayohat dialoglari · 2-qism
B2|Sayohat dialoglari · 3-qism
B2|Sayohat dialoglari
B2|Sayohat dialoglari · 2-qism
B2|Sayohat dialoglari · 3-qism
B2|Sayohat dialoglari, Kasblar
B2|Sayohat dialoglari, Kasblar · 2-qism
B2|Sayohat dialoglari, Kasblar · 3-qism
B2|Kasblar
B2|Kasblar · 2-qism
B2|Kasblar · 3-qism
B2|Kasblar, Odamlar xarakteri va tavsifi
B2|Kasblar, Odamlar xarakteri va tavsifi · 2-qism
B2|Kasblar, Odamlar xarakteri va tavsifi · 3-qism
B2|Odamlar xarakteri va tavsifi
B2|Odamlar xarakteri va tavsifi · 2-qism
B2|Odamlar xarakteri va tavsifi · 3-qism
B2|Odamlar xarakteri va tavsifi
B2|Odamlar xarakteri va tavsifi · 2-qism
B2|Odamlar xarakteri va tavsifi · 3-qism
B2|Odamlar xarakteri va tavsifi, Hayvonlar va bolalar
B2|Odamlar xarakteri va tavsifi, Hayvonlar va bolalar · 2-qism
B2|Odamlar xarakteri va tavsifi, Hayvonlar va bolalar · 3-qism
B2|Hayvonlar va bolalar
B2|Hayvonlar va bolalar · 2-qism
B2|Hayvonlar va bolalar · 3-qism
B2|Hayvonlar va bolalar, Ta'mir va qurilish
B2|Hayvonlar va bolalar, Ta'mir va qurilish · 2-qism
B2|Hayvonlar va bolalar, Ta'mir va qurilish · 3-qism
B2|Ta'mir va qurilish
B2|Ta'mir va qurilish · 2-qism
B2|Ta'mir va qurilish · 3-qism
B2|Ta'mir va qurilish, Rus tilini o'rganish iboralari
B2|Ta'mir va qurilish, Rus tilini o'rganish iboralari · 2-qism
B2|Ta'mir va qurilish, Rus tilini o'rganish iboralari · 3-qism
B2|Rus tilini o'rganish iboralari
B2|Rus tilini o'rganish iboralari · 2-qism
B2|Rus tilini o'rganish iboralari · 3-qism
B2|Rus tilini o'rganish iboralari
B2|Rus tilini o'rganish iboralari · 2-qism
B2|Rus tilini o'rganish iboralari · 3-qism
B2|Rus tilini o'rganish iboralari, Uchrashuv va telefon
B2|Rus tilini o'rganish iboralari, Uchrashuv va telefon · 2-qism
B2|Rus tilini o'rganish iboralari, Uchrashuv va telefon · 3-qism
B2|Uchrashuv va telefon
B2|Uchrashuv va telefon · 2-qism
B2|Uchrashuv va telefon · 3-qism
B2|Uchrashuv va telefon, Shikoyat va muammolar
B2|Uchrashuv va telefon, Shikoyat va muammolar · 2-qism
B2|Uchrashuv va telefon, Shikoyat va muammolar · 3-qism
B2|Shikoyat va muammolar
B2|Shikoyat va muammolar · 2-qism
B2|Shikoyat va muammolar · 3-qism
B2|Shikoyat va muammolar, Taklif va rad etish
B2|Shikoyat va muammolar, Taklif va rad etish · 2-qism
B2|Shikoyat va muammolar, Taklif va rad etish · 3-qism
B2|Taklif va rad etish
B2|Taklif va rad etish · 2-qism
B2|Taklif va rad etish · 3-qism
B2|Taklif va rad etish, Sifatlar va qarama-qarshi so'zlar
B2|Taklif va rad etish, Sifatlar va qarama-qarshi so'zlar · 2-qism
B2|Taklif va rad etish, Sifatlar va qarama-qarshi so'zlar · 3-qism
B2|Sifatlar va qarama-qarshi so'zlar
B2|Sifatlar va qarama-qarshi so'zlar · 2-qism
B2|Sifatlar va qarama-qarshi so'zlar · 3-qism
B2|Sifatlar va qarama-qarshi so'zlar, O'tgan zamon
B2|Sifatlar va qarama-qarshi so'zlar, O'tgan zamon · 2-qism
B2|Sifatlar va qarama-qarshi so'zlar, O'tgan zamon · 3-qism
B2|O'tgan zamon
B2|O'tgan zamon · 2-qism
B2|O'tgan zamon · 3-qism
B2|O'tgan zamon
B2|O'tgan zamon · 2-qism
B2|O'tgan zamon · 3-qism
B2|O'tgan zamon, Kelasi zamon
B2|O'tgan zamon, Kelasi zamon · 2-qism
B2|O'tgan zamon, Kelasi zamon · 3-qism
B2|Kelasi zamon
B2|Kelasi zamon · 2-qism
B2|Kelasi zamon · 3-qism
B2|Kelasi zamon
B2|Kelasi zamon · 2-qism
B2|Kelasi zamon · 3-qism
B2|Foydali qisqa dialoglar
B2|Foydali qisqa dialoglar · 2-qism
B2|Foydali qisqa dialoglar · 3-qism
B2|Foydali qisqa dialoglar, Qaytma fe'llar
B2|Foydali qisqa dialoglar, Qaytma fe'llar · 2-qism
B2|Foydali qisqa dialoglar, Qaytma fe'llar · 3-qism
B2|Qaytma fe'llar
B2|Qaytma fe'llar · 2-qism
B2|Qaytma fe'llar · 3-qism
B2|Qaytma fe'llar
B2|Qaytma fe'llar · 2-qism
B2|Qaytma fe'llar · 3-qism
B2|Qaytma fe'llar, Maishiy xizmatlar
B2|Qaytma fe'llar, Maishiy xizmatlar · 2-qism
B2|Qaytma fe'llar, Maishiy xizmatlar · 3-qism
B2|Maishiy xizmatlar
B2|Maishiy xizmatlar · 2-qism
B2|Maishiy xizmatlar · 3-qism
B2|Maishiy xizmatlar
B2|Maishiy xizmatlar · 2-qism
B2|Maishiy xizmatlar · 3-qism
B2|Maishiy xizmatlar, Kelishiklar mashqi
B2|Maishiy xizmatlar, Kelishiklar mashqi · 2-qism
B2|Maishiy xizmatlar, Kelishiklar mashqi · 3-qism
B2|Kelishiklar mashqi
B2|Kelishiklar mashqi · 2-qism
B2|Kelishiklar mashqi · 3-qism
B2|Kelishiklar mashqi
B2|Kelishiklar mashqi · 2-qism
B2|Kelishiklar mashqi · 3-qism
B2|Kelishiklar mashqi, Sonlar bilan
B2|Kelishiklar mashqi, Sonlar bilan · 2-qism
B2|Kelishiklar mashqi, Sonlar bilan · 3-qism
C1|Sonlar bilan
C1|Sonlar bilan · 2-qism
C1|Sonlar bilan · 3-qism
C1|Sonlar bilan
C1|Sonlar bilan · 2-qism
C1|Sonlar bilan · 3-qism
C1|Sonlar bilan, Jismoniy holat va his-tuyg'u
C1|Sonlar bilan, Jismoniy holat va his-tuyg'u · 2-qism
C1|Sonlar bilan, Jismoniy holat va his-tuyg'u · 3-qism
C1|Jismoniy holat va his-tuyg'u
C1|Jismoniy holat va his-tuyg'u · 2-qism
C1|Jismoniy holat va his-tuyg'u · 3-qism
C1|Jismoniy holat va his-tuyg'u
C1|Jismoniy holat va his-tuyg'u · 2-qism
C1|Jismoniy holat va his-tuyg'u · 3-qism
C1|Shifokor qabulida
C1|Shifokor qabulida · 2-qism
C1|Shifokor qabulida · 3-qism
C1|Shifokor qabulida
C1|Shifokor qabulida · 2-qism
C1|Shifokor qabulida · 3-qism
C1|Shifokor qabulida, Bankda
C1|Shifokor qabulida, Bankda · 2-qism
C1|Shifokor qabulida, Bankda · 3-qism
C1|Bankda
C1|Bankda · 2-qism
C1|Bankda · 3-qism
C1|Bankda
C1|Bankda · 2-qism
C1|Bankda · 3-qism
C1|Ish suhbati va ofis
C1|Ish suhbati va ofis · 2-qism
C1|Ish suhbati va ofis · 3-qism
C1|Ish suhbati va ofis
C1|Ish suhbati va ofis · 2-qism
C1|Ish suhbati va ofis · 3-qism
C1|Ish suhbati va ofis, Hujjatlar va qonun
C1|Ish suhbati va ofis, Hujjatlar va qonun · 2-qism
C1|Ish suhbati va ofis, Hujjatlar va qonun · 3-qism
C1|Hujjatlar va qonun
C1|Hujjatlar va qonun · 2-qism
C1|Hujjatlar va qonun · 3-qism
C1|Hujjatlar va qonun, Tibbiyot (batafsil)
C1|Hujjatlar va qonun, Tibbiyot (batafsil) · 2-qism
C1|Hujjatlar va qonun, Tibbiyot (batafsil) · 3-qism
C1|Tibbiyot (batafsil)
C1|Tibbiyot (batafsil) · 2-qism
C1|Tibbiyot (batafsil) · 3-qism
C1|Tibbiyot (batafsil)
C1|Tibbiyot (batafsil) · 2-qism
C1|Tibbiyot (batafsil) · 3-qism
C1|Tibbiyot (batafsil), Texnika va internet
C1|Tibbiyot (batafsil), Texnika va internet · 2-qism
C1|Tibbiyot (batafsil), Texnika va internet · 3-qism
C1|Texnika va internet
C1|Texnika va internet · 2-qism
C1|Texnika va internet · 3-qism
C1|Texnika va internet, Maktab va o'rganish
C1|Texnika va internet, Maktab va o'rganish · 2-qism
C1|Texnika va internet, Maktab va o'rganish · 3-qism
C1|Maktab va o'rganish
C1|Maktab va o'rganish · 2-qism
C1|Maktab va o'rganish · 3-qism
C1|Maktab va o'rganish
C1|Maktab va o'rganish · 2-qism
C1|Maktab va o'rganish · 3-qism
C1|Maktab va o'rganish, Sabab, maqsad va qarama-qarshilik
C1|Maktab va o'rganish, Sabab, maqsad va qarama-qarshilik · 2-qism
C1|Maktab va o'rganish, Sabab, maqsad va qarama-qarshilik · 3-qism
C1|Sabab, maqsad va qarama-qarshilik
C1|Sabab, maqsad va qarama-qarshilik · 2-qism
C1|Sabab, maqsad va qarama-qarshilik · 3-qism
C1|Sabab, maqsad va qarama-qarshilik, Jamiyat va yangiliklar
C1|Sabab, maqsad va qarama-qarshilik, Jamiyat va yangiliklar · 2-qism
C1|Sabab, maqsad va qarama-qarshilik, Jamiyat va yangiliklar · 3-qism
C1|Jamiyat va yangiliklar
C1|Jamiyat va yangiliklar · 2-qism
C1|Jamiyat va yangiliklar · 3-qism
C1|Jamiyat va yangiliklar
C1|Jamiyat va yangiliklar · 2-qism
C1|Jamiyat va yangiliklar · 3-qism
C1|Jamiyat va yangiliklar, Oila va munosabatlar
C1|Jamiyat va yangiliklar, Oila va munosabatlar · 2-qism
C1|Jamiyat va yangiliklar, Oila va munosabatlar · 3-qism
C1|Oila va munosabatlar
C1|Oila va munosabatlar · 2-qism
C1|Oila va munosabatlar · 3-qism
C1|Oila va munosabatlar
C1|Oila va munosabatlar · 2-qism
C1|Oila va munosabatlar · 3-qism
C1|Oila va munosabatlar, Pul va moliya
C1|Oila va munosabatlar, Pul va moliya · 2-qism
C1|Oila va munosabatlar, Pul va moliya · 3-qism
C1|Pul va moliya
C1|Pul va moliya · 2-qism
C1|Pul va moliya · 3-qism
C1|Pul va moliya, Aloqa va ijtimoiy tarmoqlar
C1|Pul va moliya, Aloqa va ijtimoiy tarmoqlar · 2-qism
C1|Pul va moliya, Aloqa va ijtimoiy tarmoqlar · 3-qism
C1|Aloqa va ijtimoiy tarmoqlar
C1|Aloqa va ijtimoiy tarmoqlar · 2-qism
C1|Aloqa va ijtimoiy tarmoqlar · 3-qism
C1|Aloqa va ijtimoiy tarmoqlar, Fikr bildirish va suhbat
C1|Aloqa va ijtimoiy tarmoqlar, Fikr bildirish va suhbat · 2-qism
C1|Aloqa va ijtimoiy tarmoqlar, Fikr bildirish va suhbat · 3-qism
C1|Fikr bildirish va suhbat
C1|Fikr bildirish va suhbat · 2-qism
C1|Fikr bildirish va suhbat · 3-qism
C1|Fikr bildirish va suhbat, Fe'l juftliklari (tugallangan va tugallanmagan)
C1|Fikr bildirish va suhbat, Fe'l juftliklari (tugallangan va tugallanmagan) · 2-qism
C1|Fikr bildirish va suhbat, Fe'l juftliklari (tugallangan va tugallanmagan) · 3-qism
C1|Fe'l juftliklari (tugallangan va tugallanmagan)
C1|Fe'l juftliklari (tugallangan va tugallanmagan) · 2-qism
C1|Fe'l juftliklari (tugallangan va tugallanmagan) · 3-qism
C1|Fe'l juftliklari (tugallangan va tugallanmagan)
C1|Fe'l juftliklari (tugallangan va tugallanmagan) · 2-qism
C1|Fe'l juftliklari (tugallangan va tugallanmagan) · 3-qism
C1|Fe'l juftliklari (tugallangan va tugallanmagan), Predloglar bilan
C1|Fe'l juftliklari (tugallangan va tugallanmagan), Predloglar bilan · 2-qism
C1|Fe'l juftliklari (tugallangan va tugallanmagan), Predloglar bilan · 3-qism
C1|Predloglar bilan
C1|Predloglar bilan · 2-qism
C1|Predloglar bilan · 3-qism
C1|Predloglar bilan
C1|Predloglar bilan · 2-qism
C1|Predloglar bilan · 3-qism
C1|Predloglar bilan, Ish va biznes (davomi)
C1|Predloglar bilan, Ish va biznes (davomi) · 2-qism
C1|Predloglar bilan, Ish va biznes (davomi) · 3-qism
C1|Ish va biznes (davomi)
C1|Ish va biznes (davomi) · 2-qism
C1|Ish va biznes (davomi) · 3-qism
C1|Ish va biznes (davomi)
C1|Ish va biznes (davomi) · 2-qism
C1|Ish va biznes (davomi) · 3-qism
C1|Ish va biznes (davomi), Salomatlik maslahatlari
C1|Ish va biznes (davomi), Salomatlik maslahatlari · 2-qism
C1|Ish va biznes (davomi), Salomatlik maslahatlari · 3-qism
C1|Salomatlik maslahatlari
C1|Salomatlik maslahatlari · 2-qism
C1|Salomatlik maslahatlari · 3-qism
C1|Salomatlik maslahatlari
C1|Salomatlik maslahatlari · 2-qism
C1|Salomatlik maslahatlari · 3-qism
C1|Salomatlik maslahatlari, Sayohat va turizm
C1|Salomatlik maslahatlari, Sayohat va turizm · 2-qism
C1|Salomatlik maslahatlari, Sayohat va turizm · 3-qism
C1|Sayohat va turizm
C1|Sayohat va turizm · 2-qism
C1|Sayohat va turizm · 3-qism
C1|Sayohat va turizm
C1|Sayohat va turizm · 2-qism
C1|Sayohat va turizm · 3-qism
C1|Ta'lim va kasb tanlash
C1|Ta'lim va kasb tanlash · 2-qism
C1|Ta'lim va kasb tanlash · 3-qism
C1|Ta'lim va kasb tanlash
C1|Ta'lim va kasb tanlash · 2-qism
C1|Ta'lim va kasb tanlash · 3-qism
C1|Tushuntirish va mulohaza
C1|Tushuntirish va mulohaza · 2-qism
C1|Tushuntirish va mulohaza · 3-qism
C1|Tushuntirish va mulohaza
C1|Tushuntirish va mulohaza · 2-qism
C1|Tushuntirish va mulohaza · 3-qism
C1|Tushuntirish va mulohaza, Shahar va qishloq hayoti
C1|Tushuntirish va mulohaza, Shahar va qishloq hayoti · 2-qism
C1|Tushuntirish va mulohaza, Shahar va qishloq hayoti · 3-qism
C1|Shahar va qishloq hayoti
C1|Shahar va qishloq hayoti · 2-qism
C1|Shahar va qishloq hayoti · 3-qism
C1|Shahar va qishloq hayoti
C1|Shahar va qishloq hayoti · 2-qism
C1|Shahar va qishloq hayoti · 3-qism
C1|Shahar va qishloq hayoti, Zamonaviy hayot va ilovalar
C1|Shahar va qishloq hayoti, Zamonaviy hayot va ilovalar · 2-qism
C1|Shahar va qishloq hayoti, Zamonaviy hayot va ilovalar · 3-qism
C1|Zamonaviy hayot va ilovalar
C1|Zamonaviy hayot va ilovalar · 2-qism
C1|Zamonaviy hayot va ilovalar · 3-qism
C1|Zamonaviy hayot va ilovalar, Kundalik fikrlar
C1|Zamonaviy hayot va ilovalar, Kundalik fikrlar · 2-qism
C1|Zamonaviy hayot va ilovalar, Kundalik fikrlar · 3-qism
C1|Kundalik fikrlar
C1|Kundalik fikrlar · 2-qism
C1|Kundalik fikrlar · 3-qism
C1|Kundalik fikrlar, Majhul va shaxssiz gaplar
C1|Kundalik fikrlar, Majhul va shaxssiz gaplar · 2-qism
C1|Kundalik fikrlar, Majhul va shaxssiz gaplar · 3-qism
C1|Majhul va shaxssiz gaplar
C1|Majhul va shaxssiz gaplar · 2-qism
C1|Majhul va shaxssiz gaplar · 3-qism
C1|Majhul va shaxssiz gaplar
C1|Majhul va shaxssiz gaplar · 2-qism
C1|Majhul va shaxssiz gaplar · 3-qism
C1|Majhul va shaxssiz gaplar, Ishda muloqot
C1|Majhul va shaxssiz gaplar, Ishda muloqot · 2-qism
C1|Majhul va shaxssiz gaplar, Ishda muloqot · 3-qism
C1|Ishda muloqot
C1|Ishda muloqot · 2-qism
C1|Ishda muloqot · 3-qism
C1|Ishda muloqot, Oilaviy tadbirlar
C1|Ishda muloqot, Oilaviy tadbirlar · 2-qism
C1|Ishda muloqot, Oilaviy tadbirlar · 3-qism
C1|Oilaviy tadbirlar
C1|Oilaviy tadbirlar · 2-qism
C1|Oilaviy tadbirlar · 3-qism
C1|Oilaviy tadbirlar, Urf-odat va an'analar
C1|Oilaviy tadbirlar, Urf-odat va an'analar · 2-qism
C1|Oilaviy tadbirlar, Urf-odat va an'analar · 3-qism
C1|Urf-odat va an'analar
C1|Urf-odat va an'analar · 2-qism
C1|Urf-odat va an'analar · 3-qism
C1|Urf-odat va an'analar, Politsiya va rasmiy ishlar
C1|Urf-odat va an'analar, Politsiya va rasmiy ishlar · 2-qism
C1|Urf-odat va an'analar, Politsiya va rasmiy ishlar · 3-qism
C1|Politsiya va rasmiy ishlar, Maslahatlar
C1|Politsiya va rasmiy ishlar, Maslahatlar · 2-qism
C1|Politsiya va rasmiy ishlar, Maslahatlar · 3-qism
C1|Maslahatlar
C1|Maslahatlar · 2-qism
C1|Maslahatlar · 3-qism
C1|Maslahatlar, Texnik muammolar
C1|Maslahatlar, Texnik muammolar · 2-qism
C1|Maslahatlar, Texnik muammolar · 3-qism
C1|Texnik muammolar
C1|Texnik muammolar · 2-qism
C1|Texnik muammolar · 3-qism
C1|Texnik muammolar, Iste'mol va xizmatlar
C1|Texnik muammolar, Iste'mol va xizmatlar · 2-qism
C1|Texnik muammolar, Iste'mol va xizmatlar · 3-qism
C1|Iste'mol va xizmatlar
C1|Iste'mol va xizmatlar · 2-qism
C1|Iste'mol va xizmatlar · 3-qism
C1|Iste'mol va xizmatlar
C1|Iste'mol va xizmatlar · 2-qism
C1|Iste'mol va xizmatlar · 3-qism
C1|Umumiy bilim va faktlar
C1|Umumiy bilim va faktlar · 2-qism
C1|Umumiy bilim va faktlar · 3-qism
C1|Umumiy bilim va faktlar
C1|Umumiy bilim va faktlar · 2-qism
C1|Umumiy bilim va faktlar · 3-qism
C1|Umumiy bilim va faktlar, Sifatdosh va ravishdosh
C1|Umumiy bilim va faktlar, Sifatdosh va ravishdosh · 2-qism
C1|Umumiy bilim va faktlar, Sifatdosh va ravishdosh · 3-qism
C1|Sifatdosh va ravishdosh
C1|Sifatdosh va ravishdosh · 2-qism
C1|Sifatdosh va ravishdosh · 3-qism
C1|Sifatdosh va ravishdosh
C1|Sifatdosh va ravishdosh · 2-qism
C1|Sifatdosh va ravishdosh · 3-qism
C1|Shart va taxmin
C1|Shart va taxmin · 2-qism
C1|Shart va taxmin · 3-qism
C1|Shart va taxmin
C1|Shart va taxmin · 2-qism
C1|Shart va taxmin · 3-qism
C1|Qo'shma gaplar
C1|Qo'shma gaplar · 2-qism
C1|Qo'shma gaplar · 3-qism
C1|Qo'shma gaplar
C1|Qo'shma gaplar · 2-qism
C1|Qo'shma gaplar · 3-qism
C1|Qo'shma gaplar, Madaniyat va san'at
C1|Qo'shma gaplar, Madaniyat va san'at · 2-qism
C1|Qo'shma gaplar, Madaniyat va san'at · 3-qism
C1|Madaniyat va san'at
C1|Madaniyat va san'at · 2-qism
C1|Madaniyat va san'at · 3-qism
C1|Madaniyat va san'at, Aytilgan gap (ko'chirma va o'zlashtirma)
C1|Madaniyat va san'at, Aytilgan gap (ko'chirma va o'zlashtirma) · 2-qism
C1|Madaniyat va san'at, Aytilgan gap (ko'chirma va o'zlashtirma) · 3-qism
C1|Aytilgan gap (ko'chirma va o'zlashtirma)
C1|Aytilgan gap (ko'chirma va o'zlashtirma) · 2-qism
C1|Aytilgan gap (ko'chirma va o'zlashtirma) · 3-qism
C1|Aytilgan gap (ko'chirma va o'zlashtirma), Fikr almashish
C1|Aytilgan gap (ko'chirma va o'zlashtirma), Fikr almashish · 2-qism
C1|Aytilgan gap (ko'chirma va o'zlashtirma), Fikr almashish · 3-qism
C1|Fikr almashish
C1|Fikr almashish · 2-qism
C1|Fikr almashish · 3-qism
C1|Fikr almashish, Tabiat va ekologiya
C1|Fikr almashish, Tabiat va ekologiya · 2-qism
C1|Fikr almashish, Tabiat va ekologiya · 3-qism
C1|Tabiat va ekologiya
C1|Tabiat va ekologiya · 2-qism
C1|Tabiat va ekologiya · 3-qism
C1|Sport va o'yinlar
C1|Sport va o'yinlar · 2-qism
C1|Sport va o'yinlar · 3-qism
C1|Sport va o'yinlar, Kino, kitob va televideniye
C1|Sport va o'yinlar, Kino, kitob va televideniye · 2-qism
C1|Sport va o'yinlar, Kino, kitob va televideniye · 3-qism
C1|Kino, kitob va televideniye
C1|Kino, kitob va televideniye · 2-qism
C1|Kino, kitob va televideniye · 3-qism
C1|Kino, kitob va televideniye
C1|Kino, kitob va televideniye · 2-qism
C1|Kino, kitob va televideniye · 3-qism
C1|Maqollar
C1|Maqollar · 2-qism
C1|Maqollar · 3-qism
C1|Maqollar, Tarix, madaniyat va geografiya
C1|Maqollar, Tarix, madaniyat va geografiya · 2-qism
C1|Maqollar, Tarix, madaniyat va geografiya · 3-qism
C1|Tarix, madaniyat va geografiya
C1|Tarix, madaniyat va geografiya · 2-qism
C1|Tarix, madaniyat va geografiya · 3-qism
C1|Tarix, madaniyat va geografiya
C1|Tarix, madaniyat va geografiya · 2-qism
C1|Tarix, madaniyat va geografiya · 3-qism
C1|Hikoya qilish
C1|Hikoya qilish · 2-qism
C1|Hikoya qilish · 3-qism
C1|Hikoya qilish
C1|Hikoya qilish · 2-qism
C1|Hikoya qilish · 3-qism""".split("\n")  # "daraja|mavzu"; kun raqami = qator tartibi (1..753)


TRENER_DAYS_EN = """A1|Salomlashish va tanishuv
A1|Salomlashish va tanishuv · 2-qism
A1|Salomlashish va tanishuv · 3-qism
A1|Salomlashish va tanishuv
A1|Salomlashish va tanishuv · 2-qism
A1|Salomlashish va tanishuv · 3-qism
A1|Salomlashish va tanishuv, Oila
A1|Salomlashish va tanishuv, Oila · 2-qism
A1|Salomlashish va tanishuv, Oila · 3-qism
A1|Oila
A1|Oila · 2-qism
A1|Oila · 3-qism
A1|Oila, Do'kon va bozor
A1|Oila, Do'kon va bozor · 2-qism
A1|Oila, Do'kon va bozor · 3-qism
A1|Do'kon va bozor
A1|Do'kon va bozor · 2-qism
A1|Do'kon va bozor · 3-qism
A1|Do'kon va bozor
A1|Do'kon va bozor · 2-qism
A1|Do'kon va bozor · 3-qism
A1|Do'kon va bozor, Sayohat va transport
A1|Do'kon va bozor, Sayohat va transport · 2-qism
A1|Do'kon va bozor, Sayohat va transport · 3-qism
A1|Sayohat va transport
A1|Sayohat va transport · 2-qism
A1|Sayohat va transport · 3-qism
A1|Sayohat va transport
A1|Sayohat va transport · 2-qism
A1|Sayohat va transport · 3-qism
A1|Sayohat va transport, Ovqat, dam olish va kundalik hayot
A1|Sayohat va transport, Ovqat, dam olish va kundalik hayot · 2-qism
A1|Sayohat va transport, Ovqat, dam olish va kundalik hayot · 3-qism
A1|Ovqat, dam olish va kundalik hayot
A1|Ovqat, dam olish va kundalik hayot · 2-qism
A1|Ovqat, dam olish va kundalik hayot · 3-qism
A1|Ovqat, dam olish va kundalik hayot
A1|Ovqat, dam olish va kundalik hayot · 2-qism
A1|Ovqat, dam olish va kundalik hayot · 3-qism
A1|Ovqat, dam olish va kundalik hayot, Vaqt va sanalar
A1|Ovqat, dam olish va kundalik hayot, Vaqt va sanalar · 2-qism
A1|Ovqat, dam olish va kundalik hayot, Vaqt va sanalar · 3-qism
A1|Vaqt va sanalar
A1|Vaqt va sanalar · 2-qism
A1|Vaqt va sanalar · 3-qism
A2|Vaqt va sanalar, Tabiat va ob-havo
A2|Vaqt va sanalar, Tabiat va ob-havo · 2-qism
A2|Vaqt va sanalar, Tabiat va ob-havo · 3-qism
A2|Tabiat va ob-havo
A2|Tabiat va ob-havo · 2-qism
A2|Tabiat va ob-havo · 3-qism
A2|Tabiat va ob-havo
A2|Tabiat va ob-havo · 2-qism
A2|Tabiat va ob-havo · 3-qism
A2|Tabiat va ob-havo, Uy va shahar
A2|Tabiat va ob-havo, Uy va shahar · 2-qism
A2|Tabiat va ob-havo, Uy va shahar · 3-qism
A2|Uy va shahar
A2|Uy va shahar · 2-qism
A2|Uy va shahar · 3-qism
A2|Uy va shahar, Buyruq, iltimos va majburiyat
A2|Uy va shahar, Buyruq, iltimos va majburiyat · 2-qism
A2|Uy va shahar, Buyruq, iltimos va majburiyat · 3-qism
A2|Buyruq, iltimos va majburiyat
A2|Buyruq, iltimos va majburiyat · 2-qism
A2|Buyruq, iltimos va majburiyat · 3-qism
A2|Buyruq, iltimos va majburiyat
A2|Buyruq, iltimos va majburiyat · 2-qism
A2|Buyruq, iltimos va majburiyat · 3-qism
A2|Buyruq, iltimos va majburiyat, Savollar
A2|Buyruq, iltimos va majburiyat, Savollar · 2-qism
A2|Buyruq, iltimos va majburiyat, Savollar · 3-qism
A2|Savollar
A2|Savollar · 2-qism
A2|Savollar · 3-qism
A2|Savollar, Kiyim va tashqi ko'rinish
A2|Savollar, Kiyim va tashqi ko'rinish · 2-qism
A2|Savollar, Kiyim va tashqi ko'rinish · 3-qism
A2|Kiyim va tashqi ko'rinish, So'zlashuv iboralari
A2|Kiyim va tashqi ko'rinish, So'zlashuv iboralari · 2-qism
A2|Kiyim va tashqi ko'rinish, So'zlashuv iboralari · 3-qism
A2|So'zlashuv iboralari
A2|So'zlashuv iboralari · 2-qism
A2|So'zlashuv iboralari · 3-qism
A2|So'zlashuv iboralari
A2|So'zlashuv iboralari · 2-qism
A2|So'zlashuv iboralari · 3-qism
A2|So'zlashuv iboralari
A2|So'zlashuv iboralari · 2-qism
A2|So'zlashuv iboralari · 3-qism
A2|So'zlashuv iboralari
A2|So'zlashuv iboralari · 2-qism
A2|So'zlashuv iboralari · 3-qism
A2|So'zlashuv iboralari, Raqamlar va o'lchamlar
A2|So'zlashuv iboralari, Raqamlar va o'lchamlar · 2-qism
A2|So'zlashuv iboralari, Raqamlar va o'lchamlar · 3-qism
A2|Raqamlar va o'lchamlar
A2|Raqamlar va o'lchamlar · 2-qism
A2|Raqamlar va o'lchamlar · 3-qism
A2|Raqamlar va o'lchamlar, Favqulodda holatlar va yordam
A2|Raqamlar va o'lchamlar, Favqulodda holatlar va yordam · 2-qism
A2|Raqamlar va o'lchamlar, Favqulodda holatlar va yordam · 3-qism
A2|Favqulodda holatlar va yordam
A2|Favqulodda holatlar va yordam · 2-qism
A2|Favqulodda holatlar va yordam · 3-qism
A2|Favqulodda holatlar va yordam, Yo'l so'rash
A2|Favqulodda holatlar va yordam, Yo'l so'rash · 2-qism
A2|Favqulodda holatlar va yordam, Yo'l so'rash · 3-qism
A2|Yo'l so'rash
A2|Yo'l so'rash · 2-qism
A2|Yo'l so'rash · 3-qism
A2|Present Simple: he / she / it (-s)
A2|Present Simple: he / she / it (-s) · 2-qism
A2|Present Simple: he / she / it (-s) · 3-qism
A2|Inkor: don't / doesn't
A2|Inkor: don't / doesn't · 2-qism
A2|Inkor: don't / doesn't · 3-qism
A2|Savol: Do / Does
A2|Savol: Do / Does · 2-qism
A2|Savol: Do / Does · 3-qism
A2|Savol so'zlari: What, Where, When, Why
A2|Savol so'zlari: What, Where, When, Why · 2-qism
A2|Savol so'zlari: What, Where, When, Why · 3-qism
A2|Present Continuous: hozir nima qilyapman
A2|Present Continuous: hozir nima qilyapman · 2-qism
A2|Present Continuous: hozir nima qilyapman · 3-qism
A2|like / love / hate / want to
A2|like / love / hate / want to · 2-qism
A2|like / love / hate / want to · 3-qism
A2|Bor, yo'q, kerak, mumkin
A2|Bor, yo'q, kerak, mumkin · 2-qism
A2|Bor, yo'q, kerak, mumkin · 3-qism
A2|Bor, yo'q, kerak, mumkin
A2|Bor, yo'q, kerak, mumkin · 2-qism
A2|Bor, yo'q, kerak, mumkin · 3-qism
B1|Bor, yo'q, kerak, mumkin, Uyda so'zlashuv
B1|Bor, yo'q, kerak, mumkin, Uyda so'zlashuv · 2-qism
B1|Bor, yo'q, kerak, mumkin, Uyda so'zlashuv · 3-qism
B1|Uyda so'zlashuv
B1|Uyda so'zlashuv · 2-qism
B1|Uyda so'zlashuv · 3-qism
B1|Uyda so'zlashuv
B1|Uyda so'zlashuv · 2-qism
B1|Uyda so'zlashuv · 3-qism
B1|Uyda so'zlashuv, Ranglar va buyumlar
B1|Uyda so'zlashuv, Ranglar va buyumlar · 2-qism
B1|Uyda so'zlashuv, Ranglar va buyumlar · 3-qism
B1|Ranglar va buyumlar
B1|Ranglar va buyumlar · 2-qism
B1|Ranglar va buyumlar · 3-qism
B1|Ranglar va buyumlar, Oziq-ovqat
B1|Ranglar va buyumlar, Oziq-ovqat · 2-qism
B1|Ranglar va buyumlar, Oziq-ovqat · 3-qism
B1|Oziq-ovqat
B1|Oziq-ovqat · 2-qism
B1|Oziq-ovqat · 3-qism
B1|Oziq-ovqat
B1|Oziq-ovqat · 2-qism
B1|Oziq-ovqat · 3-qism
B1|Oziq-ovqat, Odob va muloyim iboralar
B1|Oziq-ovqat, Odob va muloyim iboralar · 2-qism
B1|Oziq-ovqat, Odob va muloyim iboralar · 3-qism
B1|Odob va muloyim iboralar
B1|Odob va muloyim iboralar · 2-qism
B1|Odob va muloyim iboralar · 3-qism
B1|Odob va muloyim iboralar, Hafta kunlari va oylar
B1|Odob va muloyim iboralar, Hafta kunlari va oylar · 2-qism
B1|Odob va muloyim iboralar, Hafta kunlari va oylar · 3-qism
B1|Hafta kunlari va oylar
B1|Hafta kunlari va oylar · 2-qism
B1|Hafta kunlari va oylar · 3-qism
B1|Hafta kunlari va oylar, Salomlashish va xayrlashish
B1|Hafta kunlari va oylar, Salomlashish va xayrlashish · 2-qism
B1|Hafta kunlari va oylar, Salomlashish va xayrlashish · 3-qism
B1|Salomlashish va xayrlashish
B1|Salomlashish va xayrlashish · 2-qism
B1|Salomlashish va xayrlashish · 3-qism
B1|Salomlashish va xayrlashish, Narsa va joylar haqida
B1|Salomlashish va xayrlashish, Narsa va joylar haqida · 2-qism
B1|Salomlashish va xayrlashish, Narsa va joylar haqida · 3-qism
B1|Narsa va joylar haqida
B1|Narsa va joylar haqida · 2-qism
B1|Narsa va joylar haqida · 3-qism
B1|Narsa va joylar haqida, So'zlashuv tili
B1|Narsa va joylar haqida, So'zlashuv tili · 2-qism
B1|Narsa va joylar haqida, So'zlashuv tili · 3-qism
B1|So'zlashuv tili
B1|So'zlashuv tili · 2-qism
B1|So'zlashuv tili · 3-qism
B1|So'zlashuv tili
B1|So'zlashuv tili · 2-qism
B1|So'zlashuv tili · 3-qism
B1|So'zlashuv tili
B1|So'zlashuv tili · 2-qism
B1|So'zlashuv tili · 3-qism
B1|So'zlashuv tili, Buyruq shakllari
B1|So'zlashuv tili, Buyruq shakllari · 2-qism
B1|So'zlashuv tili, Buyruq shakllari · 3-qism
B1|Buyruq shakllari
B1|Buyruq shakllari · 2-qism
B1|Buyruq shakllari · 3-qism
B1|Buyruq shakllari
B1|Buyruq shakllari · 2-qism
B1|Buyruq shakllari · 3-qism
B1|Buyruq shakllari
B1|Buyruq shakllari · 2-qism
B1|Buyruq shakllari · 3-qism
B1|Buyruq shakllari
B1|Buyruq shakllari · 2-qism
B1|Buyruq shakllari · 3-qism
B1|Buyruq shakllari
B1|Buyruq shakllari · 2-qism
B1|Buyruq shakllari · 3-qism
B1|Buyruq shakllari, Ish va o'qish
B1|Buyruq shakllari, Ish va o'qish · 2-qism
B1|Buyruq shakllari, Ish va o'qish · 3-qism
B1|Ish va o'qish
B1|Ish va o'qish · 2-qism
B1|Ish va o'qish · 3-qism
B1|Ish va o'qish
B1|Ish va o'qish · 2-qism
B1|Ish va o'qish · 3-qism
B1|Ish va o'qish, Sog'liq
B1|Ish va o'qish, Sog'liq · 2-qism
B1|Ish va o'qish, Sog'liq · 3-qism
B1|Sog'liq
B1|Sog'liq · 2-qism
B1|Sog'liq · 3-qism
B1|Sog'liq
B1|Sog'liq · 2-qism
B1|Sog'liq · 3-qism
B1|Sog'liq, His-tuyg'ular va fikrlar
B1|Sog'liq, His-tuyg'ular va fikrlar · 2-qism
B1|Sog'liq, His-tuyg'ular va fikrlar · 3-qism
B1|His-tuyg'ular va fikrlar
B1|His-tuyg'ular va fikrlar · 2-qism
B1|His-tuyg'ular va fikrlar · 3-qism
B1|His-tuyg'ular va fikrlar, O'tgan va kelasi zamon, rejalar
B1|His-tuyg'ular va fikrlar, O'tgan va kelasi zamon, rejalar · 2-qism
B1|His-tuyg'ular va fikrlar, O'tgan va kelasi zamon, rejalar · 3-qism
B1|O'tgan va kelasi zamon, rejalar
B1|O'tgan va kelasi zamon, rejalar · 2-qism
B1|O'tgan va kelasi zamon, rejalar · 3-qism
B1|O'tgan va kelasi zamon, rejalar
B1|O'tgan va kelasi zamon, rejalar · 2-qism
B1|O'tgan va kelasi zamon, rejalar · 3-qism
B1|O'tgan va kelasi zamon, rejalar, Telefon, texnologiya, bank va pochta
B1|O'tgan va kelasi zamon, rejalar, Telefon, texnologiya, bank va pochta · 2-qism
B1|O'tgan va kelasi zamon, rejalar, Telefon, texnologiya, bank va pochta · 3-qism
B1|Telefon, texnologiya, bank va pochta
B1|Telefon, texnologiya, bank va pochta · 2-qism
B1|Telefon, texnologiya, bank va pochta · 3-qism
B1|Telefon, texnologiya, bank va pochta
B1|Telefon, texnologiya, bank va pochta · 2-qism
B1|Telefon, texnologiya, bank va pochta · 3-qism
B1|Telefon, texnologiya, bank va pochta, Taqqoslash, miqdor va sifat
B1|Telefon, texnologiya, bank va pochta, Taqqoslash, miqdor va sifat · 2-qism
B1|Telefon, texnologiya, bank va pochta, Taqqoslash, miqdor va sifat · 3-qism
B1|Taqqoslash, miqdor va sifat
B1|Taqqoslash, miqdor va sifat · 2-qism
B1|Taqqoslash, miqdor va sifat · 3-qism
B1|Taqqoslash, miqdor va sifat, Sport va qiziqishlar
B1|Taqqoslash, miqdor va sifat, Sport va qiziqishlar · 2-qism
B1|Taqqoslash, miqdor va sifat, Sport va qiziqishlar · 3-qism
B1|Sport va qiziqishlar
B1|Sport va qiziqishlar · 2-qism
B1|Sport va qiziqishlar · 3-qism
B1|Sport va qiziqishlar, Ovqat tayyorlash
B1|Sport va qiziqishlar, Ovqat tayyorlash · 2-qism
B1|Sport va qiziqishlar, Ovqat tayyorlash · 3-qism
B1|Ovqat tayyorlash
B1|Ovqat tayyorlash · 2-qism
B1|Ovqat tayyorlash · 3-qism
B1|Ovqat tayyorlash, Mehmonxona va restoran (dialoglar)
B1|Ovqat tayyorlash, Mehmonxona va restoran (dialoglar) · 2-qism
B1|Ovqat tayyorlash, Mehmonxona va restoran (dialoglar) · 3-qism
B1|Mehmonxona va restoran (dialoglar)
B1|Mehmonxona va restoran (dialoglar) · 2-qism
B1|Mehmonxona va restoran (dialoglar) · 3-qism
B1|Mehmonxona va restoran (dialoglar)
B1|Mehmonxona va restoran (dialoglar) · 2-qism
B1|Mehmonxona va restoran (dialoglar) · 3-qism
B1|Mehmonxona va restoran (dialoglar), Bayramlar va an'analar
B1|Mehmonxona va restoran (dialoglar), Bayramlar va an'analar · 2-qism
B1|Mehmonxona va restoran (dialoglar), Bayramlar va an'analar · 3-qism
B1|Bayramlar va an'analar
B1|Bayramlar va an'analar · 2-qism
B1|Bayramlar va an'analar · 3-qism
B1|Present Perfect (1): have / has + fe'lning 3-shakli
B1|Present Perfect (1): have / has + fe'lning 3-shakli · 2-qism
B1|Present Perfect (1): have / has + fe'lning 3-shakli · 3-qism
B1|Present Perfect (2): ever, never, just, already, yet
B1|Present Perfect (2): ever, never, just, already, yet · 2-qism
B1|Present Perfect (2): ever, never, just, already, yet · 3-qism
B1|Present Perfect yoki Past Simple?
B1|Present Perfect yoki Past Simple? · 2-qism
B1|Present Perfect yoki Past Simple? · 3-qism
B1|for va since
B1|for va since · 2-qism
B1|for va since · 3-qism
B1|Kundalik tartib
B1|Kundalik tartib · 2-qism
B1|Kundalik tartib · 3-qism
B1|Kundalik tartib
B1|Kundalik tartib · 2-qism
B1|Kundalik tartib · 3-qism
B1|Kundalik tartib, Do'konda va bozorda (dialoglar)
B1|Kundalik tartib, Do'konda va bozorda (dialoglar) · 2-qism
B1|Kundalik tartib, Do'konda va bozorda (dialoglar) · 3-qism
B1|Do'konda va bozorda (dialoglar)
B1|Do'konda va bozorda (dialoglar) · 2-qism
B1|Do'konda va bozorda (dialoglar) · 3-qism
B1|Do'konda va bozorda (dialoglar)
B1|Do'konda va bozorda (dialoglar) · 2-qism
B1|Do'konda va bozorda (dialoglar) · 3-qism
B1|Do'konda va bozorda (dialoglar), Sayohat dialoglari
B1|Do'konda va bozorda (dialoglar), Sayohat dialoglari · 2-qism
B1|Do'konda va bozorda (dialoglar), Sayohat dialoglari · 3-qism
B1|Sayohat dialoglari
B1|Sayohat dialoglari · 2-qism
B1|Sayohat dialoglari · 3-qism
B1|Sayohat dialoglari
B1|Sayohat dialoglari · 2-qism
B1|Sayohat dialoglari · 3-qism
B1|Sayohat dialoglari, Kasblar
B1|Sayohat dialoglari, Kasblar · 2-qism
B1|Sayohat dialoglari, Kasblar · 3-qism
B1|Kasblar
B1|Kasblar · 2-qism
B1|Kasblar · 3-qism
B1|Kasblar, Odamlar xarakteri va tavsifi
B1|Kasblar, Odamlar xarakteri va tavsifi · 2-qism
B1|Kasblar, Odamlar xarakteri va tavsifi · 3-qism
B1|Odamlar xarakteri va tavsifi
B1|Odamlar xarakteri va tavsifi · 2-qism
B1|Odamlar xarakteri va tavsifi · 3-qism
B1|Odamlar xarakteri va tavsifi
B1|Odamlar xarakteri va tavsifi · 2-qism
B1|Odamlar xarakteri va tavsifi · 3-qism
B1|Odamlar xarakteri va tavsifi, Hayvonlar va bolalar
B1|Odamlar xarakteri va tavsifi, Hayvonlar va bolalar · 2-qism
B1|Odamlar xarakteri va tavsifi, Hayvonlar va bolalar · 3-qism
B1|Hayvonlar va bolalar
B1|Hayvonlar va bolalar · 2-qism
B1|Hayvonlar va bolalar · 3-qism
B1|Hayvonlar va bolalar, Ta'mir va qurilish
B1|Hayvonlar va bolalar, Ta'mir va qurilish · 2-qism
B1|Hayvonlar va bolalar, Ta'mir va qurilish · 3-qism
B1|Ta'mir va qurilish
B1|Ta'mir va qurilish · 2-qism
B1|Ta'mir va qurilish · 3-qism
B1|Comparative: katta-roq
B1|Comparative: katta-roq · 2-qism
B1|Comparative: katta-roq · 3-qism
B1|Superlative: eng katta
B1|Superlative: eng katta · 2-qism
B1|Superlative: eng katta · 3-qism
B1|as ... as, too, enough
B1|as ... as, too, enough · 2-qism
B1|as ... as, too, enough · 3-qism
B2|some / any, much / many
B2|some / any, much / many · 2-qism
B2|some / any, much / many · 3-qism
B2|Uchrashuv va telefon
B2|Uchrashuv va telefon · 2-qism
B2|Uchrashuv va telefon · 3-qism
B2|Uchrashuv va telefon, Shikoyat va muammolar
B2|Uchrashuv va telefon, Shikoyat va muammolar · 2-qism
B2|Uchrashuv va telefon, Shikoyat va muammolar · 3-qism
B2|Shikoyat va muammolar
B2|Shikoyat va muammolar · 2-qism
B2|Shikoyat va muammolar · 3-qism
B2|Shikoyat va muammolar, Taklif va rad etish
B2|Shikoyat va muammolar, Taklif va rad etish · 2-qism
B2|Shikoyat va muammolar, Taklif va rad etish · 3-qism
B2|Taklif va rad etish
B2|Taklif va rad etish · 2-qism
B2|Taklif va rad etish · 3-qism
B2|Taklif va rad etish, Sifatlar va qarama-qarshi so'zlar
B2|Taklif va rad etish, Sifatlar va qarama-qarshi so'zlar · 2-qism
B2|Taklif va rad etish, Sifatlar va qarama-qarshi so'zlar · 3-qism
B2|Sifatlar va qarama-qarshi so'zlar
B2|Sifatlar va qarama-qarshi so'zlar · 2-qism
B2|Sifatlar va qarama-qarshi so'zlar · 3-qism
B2|Sifatlar va qarama-qarshi so'zlar, O'tgan zamon
B2|Sifatlar va qarama-qarshi so'zlar, O'tgan zamon · 2-qism
B2|Sifatlar va qarama-qarshi so'zlar, O'tgan zamon · 3-qism
B2|O'tgan zamon
B2|O'tgan zamon · 2-qism
B2|O'tgan zamon · 3-qism
B2|O'tgan zamon
B2|O'tgan zamon · 2-qism
B2|O'tgan zamon · 3-qism
B2|O'tgan zamon, Kelasi zamon
B2|O'tgan zamon, Kelasi zamon · 2-qism
B2|O'tgan zamon, Kelasi zamon · 3-qism
B2|Kelasi zamon
B2|Kelasi zamon · 2-qism
B2|Kelasi zamon · 3-qism
B2|Kelasi zamon
B2|Kelasi zamon · 2-qism
B2|Kelasi zamon · 3-qism
B2|Foydali qisqa dialoglar
B2|Foydali qisqa dialoglar · 2-qism
B2|Foydali qisqa dialoglar · 3-qism
B2|must / have to / should
B2|must / have to / should · 2-qism
B2|must / have to / should · 3-qism
B2|used to: oldin shunday edi
B2|used to: oldin shunday edi · 2-qism
B2|used to: oldin shunday edi · 3-qism
B2|Past Continuous: was / were + -ing
B2|Past Continuous: was / were + -ing · 2-qism
B2|Past Continuous: was / were + -ing · 3-qism
B2|First conditional: If ... will
B2|First conditional: If ... will · 2-qism
B2|First conditional: If ... will · 3-qism
B2|Maishiy xizmatlar
B2|Maishiy xizmatlar · 2-qism
B2|Maishiy xizmatlar · 3-qism
B2|Maishiy xizmatlar
B2|Maishiy xizmatlar · 2-qism
B2|Maishiy xizmatlar · 3-qism
B2|will yoki going to?
B2|will yoki going to? · 2-qism
B2|will yoki going to? · 3-qism
B2|Second conditional: If I were ...
B2|Second conditional: If I were ... · 2-qism
B2|Second conditional: If I were ... · 3-qism
B2|me, him, us ... va mine, yours
B2|me, him, us ... va mine, yours · 2-qism
B2|me, him, us ... va mine, yours · 3-qism
B2|Iltimos va taklif: Can / Could / Would / Let's
B2|Iltimos va taklif: Can / Could / Would / Let's · 2-qism
B2|Iltimos va taklif: Can / Could / Would / Let's · 3-qism
B2|-ing yoki to + fe'l
B2|-ing yoki to + fe'l · 2-qism
B2|-ing yoki to + fe'l · 3-qism
B2|who / which / that
B2|who / which / that · 2-qism
B2|who / which / that · 3-qism
B2|Ravishlar: always, usually, quickly
B2|Ravishlar: always, usually, quickly · 2-qism
B2|Ravishlar: always, usually, quickly · 3-qism
B2|Jismoniy holat va his-tuyg'u
B2|Jismoniy holat va his-tuyg'u · 2-qism
B2|Jismoniy holat va his-tuyg'u · 3-qism
B2|Jismoniy holat va his-tuyg'u
B2|Jismoniy holat va his-tuyg'u · 2-qism
B2|Jismoniy holat va his-tuyg'u · 3-qism
B2|Shifokor qabulida
B2|Shifokor qabulida · 2-qism
B2|Shifokor qabulida · 3-qism
B2|Shifokor qabulida
B2|Shifokor qabulida · 2-qism
B2|Shifokor qabulida · 3-qism
B2|Shifokor qabulida, Bankda
B2|Shifokor qabulida, Bankda · 2-qism
B2|Shifokor qabulida, Bankda · 3-qism
B2|Bankda
B2|Bankda · 2-qism
B2|Bankda · 3-qism
B2|Bankda
B2|Bankda · 2-qism
B2|Bankda · 3-qism
B2|Ish suhbati va ofis
B2|Ish suhbati va ofis · 2-qism
B2|Ish suhbati va ofis · 3-qism
B2|Ish suhbati va ofis
B2|Ish suhbati va ofis · 2-qism
B2|Ish suhbati va ofis · 3-qism
B2|Ish suhbati va ofis, Hujjatlar va qonun
B2|Ish suhbati va ofis, Hujjatlar va qonun · 2-qism
B2|Ish suhbati va ofis, Hujjatlar va qonun · 3-qism
B2|Hujjatlar va qonun
B2|Hujjatlar va qonun · 2-qism
B2|Hujjatlar va qonun · 3-qism
B2|Hujjatlar va qonun, Tibbiyot (batafsil)
B2|Hujjatlar va qonun, Tibbiyot (batafsil) · 2-qism
B2|Hujjatlar va qonun, Tibbiyot (batafsil) · 3-qism
B2|Tibbiyot (batafsil)
B2|Tibbiyot (batafsil) · 2-qism
B2|Tibbiyot (batafsil) · 3-qism
B2|Tibbiyot (batafsil)
B2|Tibbiyot (batafsil) · 2-qism
B2|Tibbiyot (batafsil) · 3-qism
B2|Tibbiyot (batafsil), Texnika va internet
B2|Tibbiyot (batafsil), Texnika va internet · 2-qism
B2|Tibbiyot (batafsil), Texnika va internet · 3-qism
B2|Texnika va internet
B2|Texnika va internet · 2-qism
B2|Texnika va internet · 3-qism
B2|Texnika va internet, Maktab va o'rganish
B2|Texnika va internet, Maktab va o'rganish · 2-qism
B2|Texnika va internet, Maktab va o'rganish · 3-qism
B2|Maktab va o'rganish
B2|Maktab va o'rganish · 2-qism
B2|Maktab va o'rganish · 3-qism
B2|Maktab va o'rganish
B2|Maktab va o'rganish · 2-qism
B2|Maktab va o'rganish · 3-qism
B2|Maktab va o'rganish, Sabab, maqsad va qarama-qarshilik
B2|Maktab va o'rganish, Sabab, maqsad va qarama-qarshilik · 2-qism
B2|Maktab va o'rganish, Sabab, maqsad va qarama-qarshilik · 3-qism
B2|Sabab, maqsad va qarama-qarshilik
B2|Sabab, maqsad va qarama-qarshilik · 2-qism
B2|Sabab, maqsad va qarama-qarshilik · 3-qism
B2|Sabab, maqsad va qarama-qarshilik, Jamiyat va yangiliklar
B2|Sabab, maqsad va qarama-qarshilik, Jamiyat va yangiliklar · 2-qism
B2|Sabab, maqsad va qarama-qarshilik, Jamiyat va yangiliklar · 3-qism
B2|Jamiyat va yangiliklar
B2|Jamiyat va yangiliklar · 2-qism
B2|Jamiyat va yangiliklar · 3-qism
B2|Jamiyat va yangiliklar
B2|Jamiyat va yangiliklar · 2-qism
B2|Jamiyat va yangiliklar · 3-qism
B2|Jamiyat va yangiliklar, Oila va munosabatlar
B2|Jamiyat va yangiliklar, Oila va munosabatlar · 2-qism
B2|Jamiyat va yangiliklar, Oila va munosabatlar · 3-qism
B2|Oila va munosabatlar
B2|Oila va munosabatlar · 2-qism
B2|Oila va munosabatlar · 3-qism
B2|Oila va munosabatlar
B2|Oila va munosabatlar · 2-qism
B2|Oila va munosabatlar · 3-qism
B2|Oila va munosabatlar, Pul va moliya
B2|Oila va munosabatlar, Pul va moliya · 2-qism
B2|Oila va munosabatlar, Pul va moliya · 3-qism
B2|Pul va moliya
B2|Pul va moliya · 2-qism
B2|Pul va moliya · 3-qism
B2|Pul va moliya, Aloqa va ijtimoiy tarmoqlar
B2|Pul va moliya, Aloqa va ijtimoiy tarmoqlar · 2-qism
B2|Pul va moliya, Aloqa va ijtimoiy tarmoqlar · 3-qism
B2|Aloqa va ijtimoiy tarmoqlar
B2|Aloqa va ijtimoiy tarmoqlar · 2-qism
B2|Aloqa va ijtimoiy tarmoqlar · 3-qism
B2|Aloqa va ijtimoiy tarmoqlar, Fikr bildirish va suhbat
B2|Aloqa va ijtimoiy tarmoqlar, Fikr bildirish va suhbat · 2-qism
B2|Aloqa va ijtimoiy tarmoqlar, Fikr bildirish va suhbat · 3-qism
B2|Fikr bildirish va suhbat
B2|Fikr bildirish va suhbat · 2-qism
B2|Fikr bildirish va suhbat · 3-qism
B2|Passive (1): is made, is spoken
B2|Passive (1): is made, is spoken · 2-qism
B2|Passive (1): is made, is spoken · 3-qism
B2|Passive (2): was built, was written
B2|Passive (2): was built, was written · 2-qism
B2|Passive (2): was built, was written · 3-qism
B2|Reported speech (1): said that
B2|Reported speech (1): said that · 2-qism
B2|Reported speech (1): said that · 3-qism
B2|Reported speech (2): asked if
B2|Reported speech (2): asked if · 2-qism
B2|Reported speech (2): asked if · 3-qism
B2|Predloglar bilan
B2|Predloglar bilan · 2-qism
B2|Predloglar bilan · 3-qism
B2|Predloglar bilan
B2|Predloglar bilan · 2-qism
B2|Predloglar bilan · 3-qism
B2|Predloglar bilan, Ish va biznes (davomi)
B2|Predloglar bilan, Ish va biznes (davomi) · 2-qism
B2|Predloglar bilan, Ish va biznes (davomi) · 3-qism
B2|Ish va biznes (davomi)
B2|Ish va biznes (davomi) · 2-qism
B2|Ish va biznes (davomi) · 3-qism
B2|Ish va biznes (davomi)
B2|Ish va biznes (davomi) · 2-qism
B2|Ish va biznes (davomi) · 3-qism
B2|Ish va biznes (davomi), Salomatlik maslahatlari
B2|Ish va biznes (davomi), Salomatlik maslahatlari · 2-qism
B2|Ish va biznes (davomi), Salomatlik maslahatlari · 3-qism
B2|Salomatlik maslahatlari
B2|Salomatlik maslahatlari · 2-qism
B2|Salomatlik maslahatlari · 3-qism
B2|Salomatlik maslahatlari
B2|Salomatlik maslahatlari · 2-qism
B2|Salomatlik maslahatlari · 3-qism
B2|Salomatlik maslahatlari, Sayohat va turizm
B2|Salomatlik maslahatlari, Sayohat va turizm · 2-qism
B2|Salomatlik maslahatlari, Sayohat va turizm · 3-qism
B2|Sayohat va turizm
B2|Sayohat va turizm · 2-qism
B2|Sayohat va turizm · 3-qism
B2|Sayohat va turizm
B2|Sayohat va turizm · 2-qism
B2|Sayohat va turizm · 3-qism
B2|Ta'lim va kasb tanlash
B2|Ta'lim va kasb tanlash · 2-qism
B2|Ta'lim va kasb tanlash · 3-qism
B2|Ta'lim va kasb tanlash
B2|Ta'lim va kasb tanlash · 2-qism
B2|Ta'lim va kasb tanlash · 3-qism
B2|Tushuntirish va mulohaza
B2|Tushuntirish va mulohaza · 2-qism
B2|Tushuntirish va mulohaza · 3-qism
B2|Tushuntirish va mulohaza
B2|Tushuntirish va mulohaza · 2-qism
B2|Tushuntirish va mulohaza · 3-qism
C1|Tushuntirish va mulohaza, Shahar va qishloq hayoti
C1|Tushuntirish va mulohaza, Shahar va qishloq hayoti · 2-qism
C1|Tushuntirish va mulohaza, Shahar va qishloq hayoti · 3-qism
C1|Shahar va qishloq hayoti
C1|Shahar va qishloq hayoti · 2-qism
C1|Shahar va qishloq hayoti · 3-qism
C1|Shahar va qishloq hayoti
C1|Shahar va qishloq hayoti · 2-qism
C1|Shahar va qishloq hayoti · 3-qism
C1|Shahar va qishloq hayoti, Zamonaviy hayot va ilovalar
C1|Shahar va qishloq hayoti, Zamonaviy hayot va ilovalar · 2-qism
C1|Shahar va qishloq hayoti, Zamonaviy hayot va ilovalar · 3-qism
C1|Zamonaviy hayot va ilovalar
C1|Zamonaviy hayot va ilovalar · 2-qism
C1|Zamonaviy hayot va ilovalar · 3-qism
C1|Zamonaviy hayot va ilovalar, Kundalik fikrlar
C1|Zamonaviy hayot va ilovalar, Kundalik fikrlar · 2-qism
C1|Zamonaviy hayot va ilovalar, Kundalik fikrlar · 3-qism
C1|Kundalik fikrlar
C1|Kundalik fikrlar · 2-qism
C1|Kundalik fikrlar · 3-qism
C1|Kundalik fikrlar, Majhul va shaxssiz gaplar
C1|Kundalik fikrlar, Majhul va shaxssiz gaplar · 2-qism
C1|Kundalik fikrlar, Majhul va shaxssiz gaplar · 3-qism
C1|Majhul va shaxssiz gaplar
C1|Majhul va shaxssiz gaplar · 2-qism
C1|Majhul va shaxssiz gaplar · 3-qism
C1|Majhul va shaxssiz gaplar
C1|Majhul va shaxssiz gaplar · 2-qism
C1|Majhul va shaxssiz gaplar · 3-qism
C1|Majhul va shaxssiz gaplar, Ishda muloqot
C1|Majhul va shaxssiz gaplar, Ishda muloqot · 2-qism
C1|Majhul va shaxssiz gaplar, Ishda muloqot · 3-qism
C1|Ishda muloqot
C1|Ishda muloqot · 2-qism
C1|Ishda muloqot · 3-qism
C1|Ishda muloqot, Oilaviy tadbirlar
C1|Ishda muloqot, Oilaviy tadbirlar · 2-qism
C1|Ishda muloqot, Oilaviy tadbirlar · 3-qism
C1|Oilaviy tadbirlar
C1|Oilaviy tadbirlar · 2-qism
C1|Oilaviy tadbirlar · 3-qism
C1|Oilaviy tadbirlar, Urf-odat va an'analar
C1|Oilaviy tadbirlar, Urf-odat va an'analar · 2-qism
C1|Oilaviy tadbirlar, Urf-odat va an'analar · 3-qism
C1|Urf-odat va an'analar
C1|Urf-odat va an'analar · 2-qism
C1|Urf-odat va an'analar · 3-qism
C1|Urf-odat va an'analar, Politsiya va rasmiy ishlar
C1|Urf-odat va an'analar, Politsiya va rasmiy ishlar · 2-qism
C1|Urf-odat va an'analar, Politsiya va rasmiy ishlar · 3-qism
C1|Politsiya va rasmiy ishlar, Maslahatlar
C1|Politsiya va rasmiy ishlar, Maslahatlar · 2-qism
C1|Politsiya va rasmiy ishlar, Maslahatlar · 3-qism
C1|Maslahatlar
C1|Maslahatlar · 2-qism
C1|Maslahatlar · 3-qism
C1|Maslahatlar, Texnik muammolar
C1|Maslahatlar, Texnik muammolar · 2-qism
C1|Maslahatlar, Texnik muammolar · 3-qism
C1|Texnik muammolar
C1|Texnik muammolar · 2-qism
C1|Texnik muammolar · 3-qism
C1|Texnik muammolar, Iste'mol va xizmatlar
C1|Texnik muammolar, Iste'mol va xizmatlar · 2-qism
C1|Texnik muammolar, Iste'mol va xizmatlar · 3-qism
C1|Iste'mol va xizmatlar
C1|Iste'mol va xizmatlar · 2-qism
C1|Iste'mol va xizmatlar · 3-qism
C1|Iste'mol va xizmatlar
C1|Iste'mol va xizmatlar · 2-qism
C1|Iste'mol va xizmatlar · 3-qism
C1|Umumiy bilim va faktlar
C1|Umumiy bilim va faktlar · 2-qism
C1|Umumiy bilim va faktlar · 3-qism
C1|Umumiy bilim va faktlar
C1|Umumiy bilim va faktlar · 2-qism
C1|Umumiy bilim va faktlar · 3-qism
C1|Phrasal verbs (1): turn off, get up
C1|Phrasal verbs (1): turn off, get up · 2-qism
C1|Phrasal verbs (1): turn off, get up · 3-qism
C1|Phrasal verbs (2): take off, get along
C1|Phrasal verbs (2): take off, get along · 2-qism
C1|Phrasal verbs (2): take off, get along · 3-qism
C1|wish va Third conditional
C1|wish va Third conditional · 2-qism
C1|wish va Third conditional · 3-qism
C1|Shart va taxmin
C1|Shart va taxmin · 2-qism
C1|Shart va taxmin · 3-qism
C1|Shart va taxmin
C1|Shart va taxmin · 2-qism
C1|Shart va taxmin · 3-qism
C1|Qo'shma gaplar
C1|Qo'shma gaplar · 2-qism
C1|Qo'shma gaplar · 3-qism
C1|Qo'shma gaplar
C1|Qo'shma gaplar · 2-qism
C1|Qo'shma gaplar · 3-qism
C1|Qo'shma gaplar, Madaniyat va san'at
C1|Qo'shma gaplar, Madaniyat va san'at · 2-qism
C1|Qo'shma gaplar, Madaniyat va san'at · 3-qism
C1|Madaniyat va san'at
C1|Madaniyat va san'at · 2-qism
C1|Madaniyat va san'at · 3-qism
C1|Madaniyat va san'at, Aytilgan gap (ko'chirma va o'zlashtirma)
C1|Madaniyat va san'at, Aytilgan gap (ko'chirma va o'zlashtirma) · 2-qism
C1|Madaniyat va san'at, Aytilgan gap (ko'chirma va o'zlashtirma) · 3-qism
C1|Aytilgan gap (ko'chirma va o'zlashtirma)
C1|Aytilgan gap (ko'chirma va o'zlashtirma) · 2-qism
C1|Aytilgan gap (ko'chirma va o'zlashtirma) · 3-qism
C1|Aytilgan gap (ko'chirma va o'zlashtirma), Fikr almashish
C1|Aytilgan gap (ko'chirma va o'zlashtirma), Fikr almashish · 2-qism
C1|Aytilgan gap (ko'chirma va o'zlashtirma), Fikr almashish · 3-qism
C1|Fikr almashish
C1|Fikr almashish · 2-qism
C1|Fikr almashish · 3-qism
C1|Fikr almashish, Tabiat va ekologiya
C1|Fikr almashish, Tabiat va ekologiya · 2-qism
C1|Fikr almashish, Tabiat va ekologiya · 3-qism
C1|Tabiat va ekologiya
C1|Tabiat va ekologiya · 2-qism
C1|Tabiat va ekologiya · 3-qism
C1|Sport va o'yinlar
C1|Sport va o'yinlar · 2-qism
C1|Sport va o'yinlar · 3-qism
C1|Sport va o'yinlar, Kino, kitob va televideniye
C1|Sport va o'yinlar, Kino, kitob va televideniye · 2-qism
C1|Sport va o'yinlar, Kino, kitob va televideniye · 3-qism
C1|Kino, kitob va televideniye
C1|Kino, kitob va televideniye · 2-qism
C1|Kino, kitob va televideniye · 3-qism
C1|Kino, kitob va televideniye
C1|Kino, kitob va televideniye · 2-qism
C1|Kino, kitob va televideniye · 3-qism
C1|Maqollar
C1|Maqollar · 2-qism
C1|Maqollar · 3-qism
C1|Maqollar, Tarix, madaniyat va geografiya
C1|Maqollar, Tarix, madaniyat va geografiya · 2-qism
C1|Maqollar, Tarix, madaniyat va geografiya · 3-qism
C1|Tarix, madaniyat va geografiya
C1|Tarix, madaniyat va geografiya · 2-qism
C1|Tarix, madaniyat va geografiya · 3-qism
C1|Tarix, madaniyat va geografiya
C1|Tarix, madaniyat va geografiya · 2-qism
C1|Tarix, madaniyat va geografiya · 3-qism
C1|Hikoya qilish
C1|Hikoya qilish · 2-qism
C1|Hikoya qilish · 3-qism
C1|Hikoya qilish
C1|Hikoya qilish · 2-qism
C1|Hikoya qilish · 3-qism""".split("\n")  # ingliz kursi: "daraja|mavzu", kun raqami = qator tartibi (1..753)


def trener_import(lang="ru"):
    """Kunlarni (90 kunlik kurs) `lessons` jadvaliga qo'shadi (har kunga dars va test linki). Qayta ishlatsa ham ikkilanmaydi: bor kunning mavzusini yangilaydi."""
    base = (EN_TRENER_URL if lang == "en" else TRENER_URL).rstrip("/") + "/"
    added = upd = 0
    days_list = TRENER_DAYS_EN if lang == "en" else TRENER_DAYS
    for d, line in enumerate(days_list, 1):
        lvl, topic = line.split("|", 1)
        url, test = f"{base}?d={d}", f"{base}?d={d}&m=test"
        r = db.execute("SELECT id FROM lessons WHERE url=?", (url,)).fetchone()
        if r:
            db.execute("UPDATE lessons SET level=?, test_url=?, topic=?, lang=? WHERE id=?", (lvl, test, topic, lang, r[0]))
            upd += 1
        else:
            db.execute("INSERT INTO lessons(level, url, test_url, topic, lang) VALUES(?, ?, ?, ?, ?)", (lvl, url, test, topic, lang))
            added += 1
    N = len(days_list)  # kurs qisqargan bo'lsa, ortiqcha eski kunlar o'chadi
    for lid, url in db.execute("SELECT id, url FROM lessons WHERE url LIKE ?", (base + "?d=%",)).fetchall():
        mm = re.search(r"\?d=(\d+)$", url)
        if mm and int(mm.group(1)) > N:
            db.execute("DELETE FROM lessons WHERE id=?", (lid,))
    db.commit()
    return added, upd


# ---------------- versiyali yangilash ----------------
# Botga dars tuzilishini o'zgartiruvchi tuzatish kiritganda SHU RAQAMNI 1 ga oshiring: bot keyingi ishga tushishda hammaga
# yangi holatni qo'llaydi. O'quvchilarning DARAJASI, VIP/to'lov, til, maqsad, taklif va pul ma'lumotlari saqlanib qoladi;
# trener darslaridagi tugatgan darslar tozalanadi (darslar o'zgargani uchun eski belgilar yangi mazmunga to'g'ri kelmaydi).
DATA_VERSION = 2


def apply_data_version():
    cur = int(meta_get("data_version") or 0)
    if cur >= DATA_VERSION:
        return "yangilash kerak emas (v%d)" % cur
    try:  # xavfsizlik uchun avval baza nusxasi
        if os.path.exists(DB_PATH):
            shutil.copyfile(DB_PATH, f"{DB_PATH}.v{cur}.bak")
    except Exception as e:
        print("baza nusxasi olinmadi:", e)
    n = db.execute("DELETE FROM progress WHERE lesson_id IN (SELECT id FROM lessons WHERE url LIKE '%?d=%')").rowcount
    db.execute("UPDATE users SET next_at=0, alt_next_at=0, notified=1")
    meta_set("data_version", str(DATA_VERSION))
    db.commit()
    return f"v{cur} -> v{DATA_VERSION}: {n} ta tugatilgan dars belgisi tozalandi"


def fix_wrong_a0():
    """Bir martalik avtomatik tuzatish: eski ingliz testi xatosi tufayli A0 olgan odamlar A1 ga o'tadi.
    A0 da QOLADIGANLAR: «Umuman bilmayman» tugmasini bosganlar (sk belgisi) va rus tilida A0 tanlab alifbo/sanash savoliga javob berganlar."""
    if meta_get("mig_a0_v2"):
        return 0
    n = 0
    for uid, lang, lv, alt in db.execute("SELECT user_id, COALESCE(lang,'ru'), level, alt_level FROM users").fetchall():
        if meta_get(f"sk{uid}"):
            continue
        # hozirgi til ingliz bo'lsa level — ingliz darajasi; aks holda alt_level — ingliz darajasi
        if lang == "en" and lv == "A0":
            db.execute("UPDATE users SET level='A1', next_at=0 WHERE user_id=?", (uid,)); n += 1
        elif lang != "en" and alt == "A0":
            db.execute("UPDATE users SET alt_level='A1' WHERE user_id=?", (uid,)); n += 1
        # rus tilida A0 faqat «noldan» tanlaganlarda bo'lgan (rus testi A0 bermaydi): tegmaymiz
    meta_set("mig_a0_v2", "1")
    db.commit()
    return n


@dp.message(Command("kunlar"))
async def cmd_days(m: Message):  # faqat admin: 401 kunlik darslarni bazaga qo'shadi
    if not is_admin(m.from_user.id):
        return
    added, upd = trener_import()
    cnt = {lv: len(lessons_of(lv)) for lv in LEVELS}
    base = TRENER_URL.rstrip("/") + "/"
    other = db.execute("SELECT level, COUNT(*) FROM lessons WHERE url NOT LIKE ? AND COALESCE(lang,'ru')='ru' GROUP BY level", (base + "?d=%",)).fetchall()
    warn = ("\n\n⚠️ Boshqa manzilli ESKI darslar bor: " + ", ".join(f"{a}: {b}" for a, b in other) +
            ". Ular ro'yxatni uzaytiryapti. Admin paneldan o'chiring yoki ayting, tozalash buyrug'ini qo'shaman.") if other else ""
    await m.answer(f"✅ Kunlar qo'shildi: yangi {added} ta, yangilangani {upd} ta.\n"
                   "Bazadagi darslar: " + ", ".join(f"{k}: {v}" for k, v in cnt.items()) +
                   f"\nManzil: {TRENER_URL}" + warn)


EN_A0 = [(1, "Harf yomg'iri: ingliz harflari"), (2, "Ingliz raqamlari"), (3, "Hello! Birinchi so'zlar"),
         (5, "Oila a'zolari"), (6, "Ingliz tili o'yini")]  # a0-4 sahifasi a0-3 bilan bir xil, shuning uchun olinmaydi


def en_a0_import():
    """Ingliz A0 darslari (harflar, raqamlar, birinchi so'zlar): har biri alohida sahifa va testi bilan. Qayta ishlatsa ikkilanmaydi."""
    base = EN_TRENER_URL.rstrip("/") + "/"
    added = 0
    for i, (n, topic) in enumerate(EN_A0, 1):
        url, test = f"{base}a0-{n}/", f"{base}a0-{n}-test/"
        r = db.execute("SELECT id FROM lessons WHERE url=?", (url,)).fetchone()
        if r:
            db.execute("UPDATE lessons SET level='A0', test_url=?, topic=?, lang='en', ord=? WHERE id=?", (test, topic, float(i), r[0]))
        else:
            db.execute("INSERT INTO lessons(level, url, test_url, topic, lang, ord) VALUES('A0', ?, ?, ?, 'en', ?)", (url, test, topic, float(i)))
            added += 1
    # A1: «IN, ON, AT» darsi 10-kundan keyin turadi (A1 kunlariga ham tartib raqami beriladi)
    for d in range(1, 16):
        db.execute("UPDATE lessons SET ord=? WHERE url=? AND lang='en'", (float(d), f"{base}?d={d}"))
    u1, t1 = f"{base}a1-1/", f"{base}a1-1-test/"
    r = db.execute("SELECT id FROM lessons WHERE url=?", (u1,)).fetchone()
    if r:
        db.execute("UPDATE lessons SET level='A1', test_url=?, topic=?, lang='en', ord=10.5 WHERE id=?", (t1, "IN, ON, AT", r[0]))
    else:
        db.execute("INSERT INTO lessons(level, url, test_url, topic, lang, ord) VALUES('A1', ?, ?, ?, 'en', 10.5)", (u1, t1, "IN, ON, AT"))
        added += 1
    db.commit()
    return added


@dp.message(Command("kunlar_en"))
async def cmd_days_en(m: Message):  # faqat admin: ingliz Trener kunlarini (251) bazaga qo'shadi
    if not is_admin(m.from_user.id):
        return
    added, upd = trener_import("en")
    added += en_a0_import()
    n = db.execute("SELECT COUNT(*) FROM lessons WHERE lang='en'").fetchone()[0]
    await m.answer(f"✅ Ingliz kunlari: yangi {added} ta, yangilangani {upd} ta. Jami ingliz darslari: {n}\nManzil: {EN_TRENER_URL}")


@dp.message(Command("joyla"))
async def cmd_move(m: Message):
    """Admin: darsni daraja ichida boshqa o'ringa ko'chiradi. /joyla A1 113 3 = A1 dagi 113-darsni 3-o'ringa qo'yadi
    (raqamlar admin paneldagi ro'yxat bilan bir xil)."""
    if not is_admin(m.from_user.id):
        return
    p = (m.text or "").split()
    if len(p) != 4 or p[1].upper() not in LEVELS or not p[2].isdigit() or not p[3].isdigit():
        return await m.answer("Yozilishi: /joyla A1 113 3\n(A1 darajadagi 113-darsni 3-o'ringa qo'yadi. Raqamlar admin paneldagi ro'yxatdagidek.)")
    lv, a, b = p[1].upper(), int(p[2]), int(p[3])
    ids = [x[0] for x in lessons_of(lv)]
    if not (1 <= a <= len(ids)) or not (1 <= b <= len(ids)):
        return await m.answer(f"{lv} da {len(ids)} ta dars bor. Raqamlar 1 dan {len(ids)} gacha bo'lsin.")
    lid = ids.pop(a - 1)
    ids.insert(b - 1, lid)
    for i, x in enumerate(ids, 1):
        db.execute("UPDATE lessons SET ord=? WHERE id=?", (float(i), x))
    db.commit()
    tp = dict(db.execute("SELECT id, COALESCE(topic, url) FROM lessons WHERE level=?", (lv,)).fetchall())
    near = "\n".join(f"{i}. {str(tp.get(x, ''))[:40]}" for i, x in enumerate(ids, 1) if abs(i - b) <= 2)
    await m.answer(f"✅ {lv}: dars {a}-o'rindan {b}-o'ringa ko'chdi.\n\n{near}")


# ---------------- eslatma: dars ochilgan, lekin o'quvchi kirmagan bo'lsa ----------------
RM_HOURS = [8, 24, 48, 96, 168, 384, 840]  # dars ochilgandan necha SOAT o'tgach eslatiladi (8 soat: bugungi dars tugamagan bo'lsa)
RM_TEXTS = [
    "📚 Bugungi dars hali tugamadi. Kirib davom eting: 5 ta gap, taxminan 15 daqiqa. Bugun tugatsangiz, 🔥 seriyangiz uzilmaydi.",
    "⏰ Kecha darsni tugatmadingiz. Har kun muhim, shunda hech narsa esdan chiqmaydi: bugun kirib, qolgan joyingizdan davom eting.",
    "🔁 2 kun bo'ldi. Esdan chiqmasin: darsga kiring, eski gaplar o'yin ichida o'zi takrorlanadi.",
    "🦊 Sizni sog'indik! Bugun faqat bitta qisqa darsni (5 daqiqa) bajaring, yo'lga qaytasiz.",
    "🌱 Bir hafta bo'ldi. O'rgangan gaplaringiz hali esingizda, qaytib kiring.",
    "💪 Hech qachon kech emas. Bugungi darsga kiring, davom etamiz.",
    "🙂 Dars siz uchun saqlanib turibdi. Xohlagan kuni davom eting.",
]


def rm_due(uid, na, t):
    """Shu foydalanuvchiga hozir qaysi eslatma kerak? None yoki bosqich raqami. Holat meta'da: '<next_at>:<yuborilgan_bosqichlar>'."""
    raw = meta_get(f"rm{uid}") or ""
    a, _, b = raw.partition(":")
    st = int(b) if a == str(na) and b.isdigit() else 0
    idle = (t - na) // 3600
    if st >= len(RM_HOURS) or idle < RM_HOURS[st]:
        return None
    return st


# ---- Rumiyoning ovozli eslatmalari (odamga o'xshab ovozli xabar yuboradi) ----
VOICE_PERSONA = os.getenv("VOICE_PERSONA", "friendly")     # friendly (Do'stona/mehribon), angry (Jahldor), sweet, fiery
VOICE_STAGES = {1: "missed", 3: "comeback", 5: "last"}      # RM_HOURS dagi bosqich raqami -> qaysi ovozli gap (24, 96, 384 soatdan keyin)
VOICE_LINES = {
    "missed": ["{name}, nega darsga kirmadingiz? Men sizni kutib o'tirdim-ku! Bugun kiring, ikki daqiqa bo'lsa ham mayli.",
               "{name}, jonim, kecha sizni kutdim, kelmadingiz. Xafa bo'ldim, lekin bugun kirsangiz kechiraman!",
               "{name}, bugungi darsga kirib qo'ying, iltimos. Gaplar esdan chiqib ketmasin, men yordam beraman."],
    "comeback": ["{name}, sizni juda sog'indim! Eslaysizmi, birga o'rganayotgan edik. Qaytib keling, qayerda to'xtagan bo'lsak, o'sha yerdan davom etamiz.",
                 "{name}, gaplar esdan chiqmasin deb aytgandim-ku! Hali kech emas. Bugun bitta qisqa darsga kiring."],
    "last": ["{name}, men hali ham sizni kutyapman. Hech qachon kech emas, bugun bir qadam tashlang, kiring darsga!"],
    "weekly": ["{name}, bu hafta zo'r o'qidingiz, barakalla! Endi hafta sinoviga kiring, o'rgangan gaplaringiz esingizdami, ko'ramiz."],
}


def voice_lines(kind):
    try:
        extra = json.loads(meta_get("vlines") or "{}").get(kind) or []
    except Exception:
        extra = []
    return list(VOICE_LINES.get(kind, [])) + [x for x in extra if isinstance(x, str) and x.strip()]


async def first_name(uid):
    n = meta_get(f"fn{uid}")
    if n:
        return n
    try:
        n = ((await bot.get_chat(uid)).first_name or "").strip()
    except Exception:
        n = ""
    if n:
        meta_set(f"fn{uid}", n)
    return n


async def rm_audio(text, persona=None, ctx=None):
    """Rumiyo serveridan ovozli xabar (MP3) oladi. ctx bo'lsa, Rumiyoning o'zi vaziyatga qarab gap o'ylab topadi (text = zaxira). Bo'lmasa None."""
    if not (RUMIYO_URL and TALK_SECRET):
        return None
    try:
        async with aiohttp.ClientSession() as http, http.post(RUMIYO_URL.rstrip("/") + "/admin/voice", headers={"X-Secret": TALK_SECRET},
                                                              json={"text": text, "persona": persona or VOICE_PERSONA, "ctx": ctx},
                                                              timeout=aiohttp.ClientTimeout(total=90)) as r:
            return await r.read() if r.status == 200 else None
    except Exception as e:
        print("ovozli xabar olinmadi:", type(e).__name__)
        return None


VOICE_IDLE = {"missed": 30, "comeback": 100, "last": 400}      # soat (taxminan), /ovoz sinovi uchun


async def send_voice_msg(uid, kind, kb=None, idle=None, extra=None):
    """Ovozli xabar yuboradi. Rumiyoning o'zi shu odamga vaziyatga qarab yangi gap o'ylab topadi; model javob bermasa tayyor gaplardan biri.
    Muvaffaqiyatli bo'lsa True; ovoz yaratilmasa False (chaqiruvchi matn yuboradi)."""
    lines = voice_lines(kind)
    if not lines:
        return False
    name = await first_name(uid)
    goal, level = user(uid)
    ctx = {"uid": uid, "name": name, "idle_hours": VOICE_IDLE.get(kind, 30) if idle is None else int(idle), "level": level,
           "goal": re.sub(r"[^\w\s/']", "", GOALS.get(goal, "")).strip() if goal else "", "lang": re.sub(r"[^\w\s]", "", LANG_NAMES.get(ulang(uid), "")).strip(), "done": finished_count(uid), **(extra or {})}
    audio = await rm_audio(random.choice(lines).replace("{name}", name or "do'stim"), ctx=ctx)
    if not audio:
        return False
    return bool(await send_safe(lambda: bot.send_voice(uid, BufferedInputFile(audio, filename="rumiyo.mp3"), reply_markup=kb)))


@dp.message(Command("hafta_ai"))
async def cmd_week_ai(m: Message):
    """Admin: Rumiyo bilan haftalik og'zaki sinovni hozir sinab ko'rish."""
    if not is_admin(m.from_user.id):
        return
    if not (RUMIYO_URL.startswith("https://") and TALK_SECRET):
        return await m.answer("RUMIYO_URL (https://...) va TALK_SECRET kerak.")
    await m.answer("🗣 Haftalik sinov (Rumiyo bilan, ~10 daqiqa):", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🗣 Rumiyo bilan sinov", web_app=WebAppInfo(url=wk_link(m.from_user.id)))]]))


@dp.message(Command("ovoz"))
async def cmd_voice(m: Message):
    """Admin: ovozli eslatmani o'zingizga sinab ko'rish. /ovoz missed | comeback | last"""
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split()
    kind = parts[1] if len(parts) > 1 else "missed"
    if kind not in VOICE_LINES:
        return await m.answer("Turi: missed (1 kun), comeback (4 kun), last (16 kun), weekly (haftalik hisobot). Masalan: /ovoz missed")
    if not await send_voice_msg(m.from_user.id, kind, extra={"kind": "weekly", "week_done": 4} if kind == "weekly" else None):
        await m.answer("⚠️ Ovoz yaratilmadi. Tekshiring: RUMIYO_URL va TALK_SECRET to'g'ri, Rumiyo sayti ishlayaptimi.")


@dp.message(Command("ovoz_qosh"))
async def cmd_voice_add(m: Message):
    """Admin: o'z gapingizni qo'shish. /ovoz_qosh missed {name}, nega kirmading?"""
    if not is_admin(m.from_user.id):
        return
    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 3 or parts[1] not in VOICE_LINES:
        return await m.answer("Yozing: /ovoz_qosh missed Matn... ({name} o'rniga odam ismi qo'yiladi)\nTurlari: missed (1 kun), comeback (4 kun), last (16 kun), weekly (haftalik hisobot)")
    try:
        d = json.loads(meta_get("vlines") or "{}")
    except Exception:
        d = {}
    d.setdefault(parts[1], []).append(parts[2].strip()[:500])
    meta_set("vlines", json.dumps(d, ensure_ascii=False))
    await m.answer(f"✅ Qo'shildi ({parts[1]}). Hozir bu turda {len(voice_lines(parts[1]))} ta gap bor. Sinash: /ovoz {parts[1]}")


async def reminder():
    while True:
        try:
            t = now()
            if 9 <= datetime.fromtimestamp(t, TZ).hour < 21:  # kechasi bezovta qilmaymiz
                rows = db.execute("SELECT user_id, next_at FROM users WHERE next_at>0 AND next_at<=? AND notified=1", (t,)).fetchall()
                for uid, na in rows:
                    try:
                        st = rm_due(uid, na, t)
                        if st is None:
                            continue
                        idle = (t - na) // 3600
                        nxt = next((i for i, d in enumerate(RM_HOURS) if d > idle), len(RM_HOURS))  # o'tib ketganlarini qayta yubormaymiz
                        meta_set(f"rm{uid}", f"{na}:{nxt}")
                        if paywalled(uid):
                            continue
                        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📖 Bugungi darsga kirish", callback_data="rmgo")]])
                        stage = max(nxt - 1, st)
                        ok = bool(stage in VOICE_STAGES and await send_voice_msg(uid, VOICE_STAGES[stage], kb, idle=idle))   # avval Rumiyoning ovozli xabari
                        if not ok:
                            ok = await send_safe(lambda: bot.send_message(uid, RM_TEXTS[stage], reply_markup=kb))
                        if not ok and not await _can_reach(uid):
                            meta_set(f"rm{uid}", f"{na}:{len(RM_HOURS)}")  # bloklagan: boshqa yozmaymiz
                    except Exception as e:
                        print("eslatma (foydalanuvchi):", uid, e)
                    await asyncio.sleep(0.1)
        except Exception as e:
            print("eslatma xatosi:", e)
        await asyncio.sleep(1800)


@dp.callback_query(F.data == "rmgo")
async def reminder_go(c: CallbackQuery):
    await c.answer()
    if user(c.from_user.id)[1]:
        await show_lesson(c.message, c.from_user.id)


@dp.message(F.chat.type == "private", F.text)
async def fallback_text(m: Message):
    """Boshqa hech narsaga to'g'ri kelmagan xabar: jim turmaymiz, odam "bot qotdi" deb o'ylamasin."""
    uid = m.from_user.id
    if user(uid)[1]:
        await m.answer("Pastdagi tugmalardan birini tanlang 👇", reply_markup=main_menu())
    else:
        await m.answer("Boshlash uchun /start ni bosing 👇")


async def set_menu_commands():
    """Chatdagi "Menyu" tugmasi: orqa fonda o'rnatiladi, polling'ni kutdirmaydi."""
    for attempt in range(1, 4):
        try:
            await bot.set_my_commands([BotCommand(command="dars", description="📖 Darsni ochish"),
                                       BotCommand(command="vip", description="💎 VIP ga o'tish"),
                                       BotCommand(command="til", description="🌐 Tilni almashtirish")])
            # guruhda faqat /oyin ko'rinsin (ilgari /dars va /vip chiqib, odamni chalg'itardi)
            await bot.set_my_commands([BotCommand(command="oyin", description="🎮 Ruscha o'yin (faqat admin)")],
                                      scope=BotCommandScopeAllGroupChats())
            return
        except Exception as e:
            print(f"menyu o'rnatilmadi ({attempt}/3):", e)
            if attempt < 3:
                await asyncio.sleep(5)
    print("Telegram bilan aloqa yo'q: internet/VPN ni tekshiring. Bot ishlashda davom etadi, aloqa tiklansa o'zi ulanadi.")


async def main():
    t0 = time.time()
    asyncio.create_task(set_menu_commands())
    try:
        await startup_restore()
    except Exception as e:  # tarmoq xatosi botni to'xtatmasin
        print("tiklash tekshiruvi bajarilmadi:", e)
    try:  # har ishga tushganda darslar ro'yxati kod bilan tenglashadi: admin /kunlar yozishi shart emas, eski foydalanuvchilarga ham yangi darslar o'zi keladi
        print("Rus kunlari:", trener_import("ru"), "· ingliz kunlari:", trener_import("en"))
        print("Versiya:", apply_data_version())
        print("Daraja tuzatish:", fix_wrong_a0())
    except Exception as e:
        print("darslarni yangilab bo'lmadi:", e)
    try:  # ingliz A0 darslari hali yo'q bo'lsa qo'shiladi
        if db.execute("SELECT COUNT(*) FROM lessons WHERE lang='en' AND level='A0'").fetchone()[0] == 0:
            print("Ingliz A0 darslari qo'shildi:", en_a0_import())
    except Exception as e:
        print("ingliz kunlari qo'shilmadi:", e)
    asyncio.create_task(notifier())
    asyncio.create_task(reminder())
    asyncio.create_task(weekly_loop())
    asyncio.create_task(site_results_loop())
    asyncio.create_task(keys_pusher())
    asyncio.create_task(rm_pusher())
    asyncio.create_task(cloud_backuper())
    asyncio.create_task(housekeeping())
    print(f"Bot tayyor: {time.time() - t0:.1f} soniyada polling boshlandi")
    while True:  # Telegram bilan aloqa uzilsa bot yiqilmaydi, o'zi qayta ulanadi
        try:
            await dp.start_polling(bot, close_bot_session=False)
            break
        except (TelegramNetworkError, asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
            print("Telegram bilan aloqa yo'q, 10 soniyadan keyin qayta uriniladi:", e)
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
