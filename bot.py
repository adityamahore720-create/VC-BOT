import asyncio, os, sqlite3, logging, html
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
                           KeyboardButton, ReplyKeyboardMarkup)
from pyrogram import Client
from pyrogram.errors import (SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
                             PasswordHashInvalid)
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
OWNER_ID = int(os.environ["OWNER_ID"])
START_VIDEO = os.getenv("START_VIDEO", "")
OWNER_URL = os.getenv("OWNER_URL") or f"tg://user?id={OWNER_ID}"
HELP_URL = os.getenv("HELP_URL") or OWNER_URL
UPDATES_URL = os.getenv("UPDATES_URL") or OWNER_URL
SILENCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "silence.mp3")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
esc = lambda x: html.escape(str(x))

# ---------------- DB ----------------
db = sqlite3.connect(os.getenv("DB_PATH", "data.db"), check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, name TEXT, session TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS targets(account_id INTEGER, chat TEXT, PRIMARY KEY(account_id, chat))")
db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
db.commit()

def get_set(k):
    r = db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return r[0] if r else None

def put_set(k, v):
    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v)); db.commit()

def del_set(k):
    db.execute("DELETE FROM settings WHERE key=?", (k,)); db.commit()

# ---------------- UI ----------------
def ib(text, cb=None, url=None, style=None):
    kw = {"text": text}
    if cb: kw["callback_data"] = cb
    if url: kw["url"] = url
    if style: kw["style"] = style            # primary / success / danger
    return InlineKeyboardButton(**kw)

def home_kb():   # message ke neeche wale inline buttons (screenshot jaisa)
    return InlineKeyboardMarkup(inline_keyboard=[
        [ib("📗 ABOUT", "about"), ib("💌 OWNER", url=OWNER_URL)],
        [ib("💡 GUIDE", "guide", style="success")],
        [ib("⌜ HELP ⌟", url=HELP_URL), ib("⌜ UPDATES ⌟", url=UPDATES_URL, style="success")],
    ])

B_LOGIN, B_ACC = "🔐 LOGIN", "👤 ACCOUNTS"
B_TGT, B_RM = "🎯 SET TARGET", "🗑 REMOVE"
B_VID, B_RMVID = "🎬 SET VIDEO", "🚫 REMOVE VIDEO"
B_STAT = "📊 STATUS"

def menu_kb():   # keyboard ki jagah wale colored buttons
    def b(t, s=None):
        d = {"text": t}
        if s: d["style"] = s
        return KeyboardButton(**d)
    return ReplyKeyboardMarkup(keyboard=[
        [b(B_LOGIN, "primary"), b(B_ACC, "success")],
        [b(B_TGT, "primary"), b(B_RM, "danger")],
        [b(B_VID, "success"), b(B_RMVID, "danger")],
        [b(B_STAT, "primary")],
    ], resize_keyboard=True, is_persistent=True, input_field_placeholder="Select an option…")

HOME_TXT = ("<b>🎙 VC KEEPER</b>\n<i>Your account, always live in voice chat.</i>\n\n"
            "• Multi-account login\n• Auto rejoin when VC restarts\n• Live status\n\n"
            "Use the menu below ⬇️")

# ---------------- chat cleanup helpers ----------------
trash: list[tuple[int, int]] = []
last: dict[str, tuple[int, int]] = {}

def keep(m: Message):
    trash.append((m.chat.id, m.message_id))

async def say(chat_id, text, **kw):
    m = await bot.send_message(chat_id, text, **kw)
    trash.append((chat_id, m.message_id)); return m

async def sweep():
    items = trash[:]; trash.clear()
    for cid, mid in items:
        try: await bot.delete_message(cid, mid)
        except Exception: pass

async def flash(chat_id, text, secs=8):
    m = await bot.send_message(chat_id, text)
    async def _d():
        await asyncio.sleep(secs)
        try: await m.delete()
        except Exception: pass
    asyncio.create_task(_d())

async def send_home(chat_id):
    old = [last.pop("panel", None), last.pop("home", None)]
    panel = await bot.send_message(chat_id, "🎛 <b>Control panel ready</b>", reply_markup=menu_kb())
    vid = get_set("video") or START_VIDEO
    home = None
    if vid:
        try:
            home = await bot.send_video(chat_id, vid, caption=HOME_TXT, has_spoiler=True, reply_markup=home_kb())
        except Exception as e:
            logging.warning("video send fail: %s", e)
    if not home:
        home = await bot.send_message(chat_id, HOME_TXT, reply_markup=home_kb())
    last["panel"] = (chat_id, panel.message_id); last["home"] = (chat_id, home.message_id)
    for o in old:
        if o:
            try: await bot.delete_message(*o)
            except Exception: pass

# ---------------- VC workers ----------------
class Worker:
    def __init__(self, acc_id, session):
        self.id = acc_id
        self.app = Client(f"acc{acc_id}", api_id=API_ID, api_hash=API_HASH,
                          session_string=session, in_memory=True)
        self.call = PyTgCalls(self.app)
        self.targets: set[int] = set()
        self.state: dict[int, str] = {}
        self.task = None

    async def start(self):
        await self.app.start()
        try:                                   # peers cache (-100 ID resolve ke liye)
            async for _ in self.app.get_dialogs(limit=300): pass
        except Exception as e:
            logging.warning("dialogs cache: %s", e)
        await self.call.start()
        self.task = asyncio.create_task(self.keeper())

    async def resolve(self, chat: str) -> int:
        chat = chat.strip().replace("https://t.me/", "").replace("http://t.me/", "").lstrip("@")
        key = int(chat) if chat.lstrip("-").isdigit() else chat
        return (await self.app.get_chat(key)).id

    async def join(self, chat_id):
        await self.call.play(chat_id, MediaStream(
            SILENCE, AudioQuality.LOW, video_flags=MediaStream.Flags.IGNORE,
            ffmpeg_parameters="-stream_loop -1"))

    async def tick(self, chat_id):
        try:
            await self.call.played_time(chat_id)
            self.state[chat_id] = "in"
        except Exception:
            try:
                await self.join(chat_id); self.state[chat_id] = "in"
                logging.info("[acc %s] joined VC %s", self.id, chat_id)
            except Exception as e:
                self.state[chat_id] = "wait"
                logging.info("[acc %s] VC %s off: %s", self.id, chat_id, e)

    async def keeper(self):
        while True:
            for cid in list(self.targets):
                await self.tick(cid)
            await asyncio.sleep(20)

    async def stop(self):
        if self.task: self.task.cancel()
        try: await self.app.stop()
        except Exception: pass

workers: dict[int, Worker] = {}
errors: dict[int, str] = {}

async def start_worker(acc_id, session):
    old = workers.pop(acc_id, None)
    if old: await old.stop()
    w = Worker(acc_id, session)
    try:
        await w.start()
    except Exception as e:
        logging.exception("worker start failed")
        errors[acc_id] = f"{type(e).__name__}: {e}"
        await w.stop(); return None
    errors.pop(acc_id, None); workers[acc_id] = w
    for (chat,) in db.execute("SELECT chat FROM targets WHERE account_id=?", (acc_id,)).fetchall():
        try: w.targets.add(await w.resolve(chat))
        except Exception as e: logging.warning("target %s: %s", chat, e)
    return w

async def get_worker(acc_id):
    w = workers.get(acc_id)
    if w: return w
    r = db.execute("SELECT session FROM accounts WHERE id=?", (acc_id,)).fetchone()
    return await start_worker(acc_id, r[0]) if r else None

# ---------------- handlers ----------------
router = Router()
router.message.filter(F.from_user.id == OWNER_ID)
router.callback_query.filter(F.from_user.id == OWNER_ID)

class Login(StatesGroup): phone = State(); code = State(); pw = State()
class Tgt(StatesGroup): chat = State()
class Vid(StatesGroup): wait = State()

temp: dict[int, dict] = {}

async def cancel_login(uid):
    d = temp.pop(uid, None)
    if d:
        try: await d["cl"].disconnect()
        except Exception: pass

async def begin(m: Message, state: FSMContext):
    await state.clear(); await cancel_login(m.from_user.id)
    await sweep(); keep(m)

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear(); await cancel_login(m.from_user.id); await sweep()
    try: await m.delete()
    except Exception: pass
    await send_home(m.chat.id)

@router.callback_query(F.data == "about")
async def about(c: CallbackQuery):
    await c.answer("🎙 VC Keeper aapke Telegram account ko target group/channel ke voice chat me 24/7 rakhta hai. "
                   "VC off→on hote hi auto rejoin.", show_alert=True)

@router.callback_query(F.data == "guide")
async def guide(c: CallbackQuery):
    await c.answer("1) LOGIN: number + OTP\n2) SET TARGET: group/channel ID\n"
                   "3) Account us chat ka member ho\nVC start hote hi account join ho jayega.", show_alert=True)

# ---- LOGIN ----
@router.message(F.text == B_LOGIN)
async def login(m: Message, state: FSMContext):
    await begin(m, state); await state.set_state(Login.phone)
    await say(m.chat.id, "📱 <b>Login</b>\nPhone number bhejo (country code ke saath)\n<code>+919999999999</code>")

@router.message(Login.phone)
async def l_phone(m: Message, state: FSMContext):
    keep(m); phone = m.text.strip().replace(" ", "")
    cl = Client(f"tmp{m.from_user.id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    try:
        await cl.connect(); sent = await cl.send_code(phone)
    except Exception as e:
        try: await cl.disconnect()
        except Exception: pass
        return await say(m.chat.id, f"❌ {esc(e)}\nDobara number bhejo.")
    temp[m.from_user.id] = {"cl": cl, "phone": phone, "hash": sent.phone_code_hash}
    await state.set_state(Login.code)
    await say(m.chat.id, "🔑 OTP bhejo <b>spaces ke saath</b>\nExample: <code>1 2 3 4 5</code>")

@router.message(Login.code)
async def l_code(m: Message, state: FSMContext):
    try: await m.delete()
    except Exception: pass
    d = temp.get(m.from_user.id)
    if not d: return
    try:
        await d["cl"].sign_in(d["phone"], d["hash"], m.text.replace(" ", ""))
    except SessionPasswordNeeded:
        await state.set_state(Login.pw)
        return await say(m.chat.id, "🔐 2-step password bhejo")
    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        return await say(m.chat.id, f"❌ {esc(e)}\nDobara OTP bhejo.")
    except Exception as e:
        return await say(m.chat.id, f"❌ {esc(e)}")
    await finish_login(m, state)

@router.message(Login.pw)
async def l_pw(m: Message, state: FSMContext):
    try: await m.delete()
    except Exception: pass
    d = temp.get(m.from_user.id)
    if not d: return
    try: await d["cl"].check_password(m.text)
    except PasswordHashInvalid: return await say(m.chat.id, "❌ Password galat hai, dobara bhejo.")
    except Exception as e: return await say(m.chat.id, f"❌ {esc(e)}")
    await finish_login(m, state)

async def finish_login(m: Message, state: FSMContext):
    d = temp.pop(m.from_user.id)
    me = await d["cl"].get_me()
    session = await d["cl"].export_session_string()
    await d["cl"].disconnect()
    row = db.execute("SELECT id FROM accounts WHERE phone=?", (d["phone"],)).fetchone()
    if row:
        acc_id = row[0]
        db.execute("UPDATE accounts SET session=?, name=? WHERE id=?", (session, me.first_name, acc_id))
    else:
        acc_id = db.execute("INSERT INTO accounts(phone,name,session) VALUES(?,?,?)",
                            (d["phone"], me.first_name, session)).lastrowid
    db.commit()
    await state.clear(); await sweep()
    w = await start_worker(acc_id, session)
    if w: await flash(m.chat.id, f"✅ <b>{esc(me.first_name)}</b> login ho gaya.")
    else: await flash(m.chat.id, f"⚠️ Login hua par worker start nahi hua:\n<code>{esc(errors.get(acc_id))}</code>", 20)

# ---- ACCOUNTS / STATUS ----
@router.message(F.text == B_ACC)
async def accounts(m: Message, state: FSMContext):
    await begin(m, state)
    rows = db.execute("SELECT id,name,phone FROM accounts").fetchall()
    if not rows: return await say(m.chat.id, "👤 Koi account login nahi hai.")
    txt = "👤 <b>Logged-in accounts</b>\n\n"
    for i, n, p in rows:
        t = db.execute("SELECT COUNT(*) FROM targets WHERE account_id=?", (i,)).fetchone()[0]
        txt += f"<b>#{i}</b> {esc(n)} · <code>{esc(p)}</code>\n🎯 Targets: {t}\n\n"
    await say(m.chat.id, txt)

@router.message(F.text == B_STAT)
async def status(m: Message, state: FSMContext):
    await begin(m, state)
    rows = db.execute("SELECT id,name FROM accounts").fetchall()
    if not rows: return await say(m.chat.id, "📊 Koi account nahi hai.")
    txt = "📊 <b>Live status</b>\n\n"
    for i, n in rows:
        w = workers.get(i)
        txt += f"👤 <b>{esc(n)}</b> · {'🟢 online' if w else '🔴 offline'}\n"
        if not w and errors.get(i): txt += f"<code>{esc(errors[i])}</code>\n"
        for cid in (w.targets if w else []):
            s = "🎧 In VC" if w.state.get(cid) == "in" else "⏳ Waiting for VC"
            txt += f"   • <code>{cid}</code> — {s}\n"
        txt += "\n"
    await say(m.chat.id, txt)

# ---- SET TARGET ----
async def ask_chat(chat_id, state: FSMContext, acc):
    await state.set_state(Tgt.chat); await state.update_data(acc=acc)
    await say(chat_id, "🎯 Group/Channel ka <b>ID</b> (-100…) ya <b>@username</b> / link bhejo")

@router.message(F.text == B_TGT)
async def set_target(m: Message, state: FSMContext):
    await begin(m, state)
    accs = db.execute("SELECT id,name FROM accounts").fetchall()
    if not accs: return await flash(m.chat.id, "⚠️ Pehle LOGIN karo.")
    if len(accs) == 1: return await ask_chat(m.chat.id, state, accs[0][0])
    kb = InlineKeyboardMarkup(inline_keyboard=[[ib(f"#{i} {n}"[:40], f"st:{i}", style="primary")] for i, n in accs])
    await say(m.chat.id, "🎯 Kaunsa account?", reply_markup=kb)

@router.callback_query(F.data.startswith("st:"))
async def st_pick(c: CallbackQuery, state: FSMContext):
    await c.answer()
    try: await c.message.delete()
    except Exception: pass
    await ask_chat(c.message.chat.id, state, int(c.data.split(":")[1]))

@router.message(Tgt.chat)
async def t_chat(m: Message, state: FSMContext):
    keep(m); acc = (await state.get_data())["acc"]
    w = await get_worker(acc)
    if not w:
        return await say(m.chat.id, f"❌ Account start nahi hua:\n<code>{esc(errors.get(acc, 'unknown'))}</code>")
    try: cid = await w.resolve(m.text)
    except Exception as e:
        return await say(m.chat.id, f"❌ Chat nahi mila. Account us chat ka member hona chahiye.\n<code>{esc(e)}</code>")
    db.execute("INSERT OR REPLACE INTO targets VALUES(?,?)", (acc, str(cid))); db.commit()
    w.targets.add(cid); await state.clear(); await sweep()
    asyncio.create_task(w.tick(cid))
    await flash(m.chat.id, f"✅ Target set: <code>{cid}</code>\nVC chalu hote hi account join karega.")

# ---- REMOVE ----
@router.message(F.text == B_RM)
async def remove(m: Message, state: FSMContext):
    await begin(m, state); rows = []
    for aid, name in db.execute("SELECT id,name FROM accounts").fetchall():
        w = workers.get(aid)
        for (chat,) in db.execute("SELECT chat FROM targets WHERE account_id=?", (aid,)).fetchall():
            title = chat
            if w:
                try: title = (await w.app.get_chat(int(chat))).title or chat
                except Exception: pass
            rows.append([ib(f"❌ {title}"[:40], f"rt:{aid}:{chat}", style="danger")])
        rows.append([ib(f"🚪 Logout #{aid} {name}"[:40], f"lo:{aid}", style="danger")])
    if not rows: return await flash(m.chat.id, "Remove karne ke liye kuch nahi hai.")
    await say(m.chat.id, "🗑 <b>Kya remove karna hai?</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@router.callback_query(F.data.startswith("rt:"))
async def rm_target(c: CallbackQuery):
    _, aid, chat = c.data.split(":"); aid = int(aid); cid = int(chat)
    w = workers.get(aid)
    if w:
        w.targets.discard(cid); w.state.pop(cid, None)
        try: await w.call.leave_call(cid)
        except Exception: pass
    db.execute("DELETE FROM targets WHERE account_id=? AND chat=?", (aid, chat)); db.commit()
    await c.answer("✅ Target hata diya"); await sweep()

@router.callback_query(F.data.startswith("lo:"))
async def logout(c: CallbackQuery):
    aid = int(c.data.split(":")[1]); w = workers.pop(aid, None)
    if w: await w.stop()
    db.execute("DELETE FROM accounts WHERE id=?", (aid,)); db.execute("DELETE FROM targets WHERE account_id=?", (aid,)); db.commit()
    await c.answer("✅ Account hata diya"); await sweep()

# ---- VIDEO ----
@router.message(F.text == B_VID)
async def set_video(m: Message, state: FSMContext):
    await begin(m, state); await state.set_state(Vid.wait)
    await say(m.chat.id, "🎬 Video bhejo. Start message me ye <b>hidden (spoiler)</b> dikhega.")

@router.message(Vid.wait, F.video)
async def got_video(m: Message, state: FSMContext):
    put_set("video", m.video.file_id); keep(m); await state.clear(); await sweep()
    await flash(m.chat.id, "✅ Video set ho gaya."); await send_home(m.chat.id)

@router.message(Vid.wait)
async def not_video(m: Message):
    keep(m); await say(m.chat.id, "⚠️ Sirf video bhejo.")

@router.message(F.text == B_RMVID)
async def rm_video(m: Message, state: FSMContext):
    await begin(m, state); await sweep(); del_set("video")
    await flash(m.chat.id, "🚫 Video hata diya."); await send_home(m.chat.id)

# ---- non-owner ----
pub = Router()
@pub.message()
async def private(m: Message):
    await m.answer("🔒 This is a private bot.")

async def main():
    for acc_id, session in db.execute("SELECT id,session FROM accounts").fetchall():
        asyncio.create_task(start_worker(acc_id, session))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router); dp.include_router(pub)
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
