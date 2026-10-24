import asyncio, os, re, sys, gc, time, shutil, sqlite3, logging, html, wave, tempfile
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
                           KeyboardButton, ReplyKeyboardMarkup)
from pyrogram import Client, raw
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
SILENCE = os.path.join(tempfile.gettempdir(), "silence.wav")

def norm_url(u: str) -> str:
    u = u.strip()
    if u.startswith("@"): return "https://t.me/" + u[1:]
    if u.startswith(("http://", "https://", "tg://")): return u
    if u.startswith(("t.me/", "telegram.me/")): return "https://" + u
    return "https://t.me/" + u.lstrip("/")

OWNER_URL = norm_url(os.getenv("OWNER_URL") or f"tg://user?id={OWNER_ID}")
HELP_URL = norm_url(os.getenv("HELP_URL") or OWNER_URL)
UPDATES_URL = norm_url(os.getenv("UPDATES_URL") or OWNER_URL)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
esc = lambda x: html.escape(str(x))

# ---------------- font (ꜱᴍᴀʟʟ ᴄᴀᴘꜱ) ----------------
_SC = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
_SC.update({k.upper(): v for k, v in list(_SC.items())})
_SKIP = re.compile(r"(<code>.*?</code>|<[^>]+>|&#?\w+;)", re.S)

def fx(t: str) -> str:
    return "".join(p if _SKIP.fullmatch(p) else "".join(_SC.get(ch, ch) for ch in p)
                   for p in _SKIP.split(t))

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

def is_paused() -> bool:
    return get_set("paused") == "1"

# ---------------- UI ----------------
def ib(text, cb=None, url=None, style=None):
    kw = {"text": fx(text)}
    if cb: kw["callback_data"] = cb
    if url: kw["url"] = url
    if style: kw["style"] = style          # primary / success / danger (colored)
    return InlineKeyboardButton(**kw)

def kb(rows): return InlineKeyboardMarkup(inline_keyboard=rows)
def back_kb(to="home"): return kb([[ib("⬅️ BACK", to, style="danger")]])

def home_kb():
    return kb([
        [ib("📗 ABOUT", "about"), ib("💌 OWNER", url=OWNER_URL)],
        [ib("💡 GUIDE", "guide", style="success")],
        [ib("⌜ ACCOUNTS ⌟", "accounts", style="primary"), ib("🎯 TARGETS", "targets", style="success")],
        [ib("📊 STATUS", "status"), ib("♻️ REFRESH", "refresh", style="danger")],
        [ib("⌜ HELP ⌟", url=HELP_URL), ib("⌜ UPDATES ⌟", url=UPDATES_URL, style="success")],
    ])

B_VID = fx("🎬 SET VIDEO")
def menu_kb():                               # keyboard me sirf SET VIDEO
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=B_VID, style="success")]],
                               resize_keyboard=True, is_persistent=True,
                               input_field_placeholder=fx("Select an option…"))

HOME_TXT = ("<b>🎙 VC KEEPER</b>\n<i>Your account, always live in voice chat.</i>\n\n"
            "• Multi-account login\n• Auto rejoin when VC restarts\n• Live status")

# ---- single-message UI (edit in place) ----
UI: dict = {}

def set_ui(m: Message):
    UI.update(chat=m.chat.id, id=m.message_id,
              kind="caption" if (m.video or m.animation or m.photo or m.document) else "text")

async def del_msg(m):
    try: await m.delete()
    except Exception: pass

def _old(key):
    v = get_set(key)
    if v:
        try:
            c, i = v.split(":"); return int(c), int(i)
        except Exception: return None

async def _kill(ids):
    for t in ids:
        if t:
            try: await bot.delete_message(*t)
            except Exception: pass

async def send_home(chat_id):
    old = [_old("home_msg"), _old("panel_msg")]; UI.clear()
    # keyboard wala msg delete nahi karna, warna SET VIDEO button gayab ho jata hai
    panel = await bot.send_message(chat_id, fx("🎛 Control panel · SET VIDEO below"), reply_markup=menu_kb())
    vid = get_set("video") or START_VIDEO
    home = None
    if vid:
        try:
            home = await bot.send_video(chat_id, vid, caption=fx(HOME_TXT), has_spoiler=True, reply_markup=home_kb())
        except Exception as e:
            logging.warning("video send fail: %s", e)
    if not home:
        home = await bot.send_message(chat_id, fx(HOME_TXT), reply_markup=home_kb())
    set_ui(home)
    put_set("home_msg", f"{chat_id}:{home.message_id}"); put_set("panel_msg", f"{chat_id}:{panel.message_id}")
    await _kill(old)

async def show(text, markup=None):
    if not UI: return
    text = fx(text)
    try:
        if UI["kind"] == "caption":
            await bot.edit_message_caption(chat_id=UI["chat"], message_id=UI["id"], caption=text, reply_markup=markup)
        else:
            await bot.edit_message_text(text, chat_id=UI["chat"], message_id=UI["id"], reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e): return
        logging.warning("edit fail: %s", e)
        await send_home(UI["chat"])

# ---------------- media (ffmpeg + silent wav) ----------------
MEDIA_READY = asyncio.Event()

def _make_wav():
    if os.path.exists(SILENCE): return
    with wave.open(SILENCE, "wb") as w:          # 10 min silent wav, 8kHz mono
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        sec = b"\x00\x00" * 8000
        for _ in range(600): w.writeframes(sec)

async def prep_media():
    try:
        _make_wav()
        if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            try:
                import static_ffmpeg
                await asyncio.to_thread(static_ffmpeg.add_paths)     # ffmpeg + ffprobe
            except Exception as e:
                logging.warning("static-ffmpeg: %s", e)
        if not shutil.which("ffmpeg"):
            import imageio_ffmpeg
            d = os.path.join(tempfile.gettempdir(), "ffbin"); os.makedirs(d, exist_ok=True)
            link = os.path.join(d, "ffmpeg")
            if not os.path.exists(link): os.symlink(imageio_ffmpeg.get_ffmpeg_exe(), link)
            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
    except Exception as e:
        logging.warning("media prep: %s", e)
    finally:
        logging.info("ffmpeg=%s ffprobe=%s", shutil.which("ffmpeg"), shutil.which("ffprobe"))
        MEDIA_READY.set()

# ---------------- VC workers ----------------
class Worker:
    def __init__(self, acc_id, session):
        self.id = acc_id
        self.app = Client(f"acc{acc_id}", api_id=API_ID, api_hash=API_HASH,
                          session_string=session, in_memory=True,
                          workers=2, max_concurrent_transmissions=1)
        self.call = PyTgCalls(self.app)
        self.targets: set[int] = set()
        self.state: dict[int, str] = {}
        self.err: dict[int, str] = {}
        self.muted: dict[int, bool] = {}
        self.mute_err: dict[int, str] = {}
        self.task = None

    async def start(self):
        await self.app.start()
        try:
            async for _ in self.app.get_dialogs(limit=100): pass
        except Exception as e:
            logging.warning("dialogs cache: %s", e)
        await self.call.start()
        self.task = asyncio.create_task(self.keeper())

    async def resolve(self, chat: str) -> int:
        chat = chat.strip().replace("https://t.me/", "").replace("http://t.me/", "").lstrip("@")
        key = int(chat) if chat.lstrip("-").isdigit() else chat
        return (await self.app.get_chat(key)).id

    async def join(self, chat_id):
        await asyncio.wait_for(MEDIA_READY.wait(), 180)
        try:
            ms = MediaStream(SILENCE, AudioQuality.LOW, video_flags=MediaStream.Flags.IGNORE,
                             audio_flags=MediaStream.Flags.REQUIRED)
        except TypeError:
            ms = MediaStream(SILENCE, AudioQuality.LOW, video_flags=MediaStream.Flags.IGNORE)
        await self.call.play(chat_id, ms)
        self.muted[chat_id] = False
        await asyncio.sleep(2)
        await self.mute(chat_id)

    async def force_mute(self, chat_id):
        """Telegram server par seedha self-mute (mic icon off)."""
        peer = await self.app.resolve_peer(chat_id)
        if isinstance(peer, raw.types.InputPeerChannel):
            full = await self.app.invoke(raw.functions.channels.GetFullChannel(
                channel=raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)))
        else:
            full = await self.app.invoke(raw.functions.messages.GetFullChat(chat_id=peer.chat_id))
        call = full.full_chat.call
        if not call: raise RuntimeError("no active call")
        me = await self.app.resolve_peer("me")
        await self.app.invoke(raw.functions.phone.EditGroupCallParticipant(call=call, participant=me, muted=True))

    async def mute(self, chat_id):
        ok, errs = False, []
        try:
            await self.call.mute_stream(chat_id); ok = True
        except Exception as e:
            errs.append(f"mute_stream {type(e).__name__}")
        try:
            await self.force_mute(chat_id); ok = True
        except Exception as e:
            errs.append(f"raw {type(e).__name__}: {e}")
        self.muted[chat_id] = ok; self.mute_err[chat_id] = " | ".join(errs) if not ok else ""
        if errs: logging.warning("[acc %s] mute %s: %s", self.id, chat_id, errs)

    async def tick(self, chat_id):
        try:
            await self.call.played_time(chat_id)
            self.state[chat_id] = "in"; self.err.pop(chat_id, None)
            if not self.muted.get(chat_id): await self.mute(chat_id)
            return
        except Exception:
            pass
        try:
            await self.join(chat_id)
            self.state[chat_id] = "in"; self.err.pop(chat_id, None)
            logging.info("[acc %s] joined VC %s", self.id, chat_id)
        except Exception as e:
            self.state[chat_id] = "wait"; self.err[chat_id] = f"{type(e).__name__}: {e}"
            logging.warning("[acc %s] VC %s join fail: %r", self.id, chat_id, e)

    async def keeper(self):
        while True:
            if not is_paused():
                for cid in list(self.targets): await self.tick(cid)
            await asyncio.sleep(30)

    async def stop(self):
        if self.task: self.task.cancel()
        try: await self.app.stop()
        except Exception: pass

workers: dict[int, Worker] = {}
errors: dict[int, str] = {}

async def start_worker(acc_id, session):
    old = workers.pop(acc_id, None)
    if old: await old.stop()
    w = None
    try:
        w = Worker(acc_id, session)
        await w.start()
    except BaseException as e:
        logging.exception("worker start failed")
        errors[acc_id] = f"{type(e).__name__}: {e}"
        if w: await w.stop()
        return None
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

# ---------------- refresh helpers ----------------
def rss_mb() -> int:
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"): return int(line.split()[1]) // 1024
    except Exception: pass
    return 0

RAM_LIMIT = int(os.getenv("RAM_LIMIT", "400"))   # MB, isse upar auto refresh
BOOT_TIME = time.time()

def _rss(pid) -> int:
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"): return int(line.split()[1]) // 1024
    except Exception: pass
    return 0

def total_mb() -> int:                    # bot + uske child processes (ffmpeg) ki RAM
    me = os.getpid(); t = _rss(me)
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                with open(f"/proc/{p}/stat") as f: ppid = int(f.read().rsplit(")", 1)[1].split()[1])
                if ppid == me: t += _rss(p)
            except Exception: pass
    return t

def kill_children():                      # leftover ffmpeg wagairah band karo
    me = os.getpid()
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                with open(f"/proc/{p}/stat") as f: ppid = int(f.read().rsplit(")", 1)[1].split()[1])
                if ppid == me: os.kill(int(p), 9)
            except Exception: pass

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

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear(); await cancel_login(m.from_user.id); await del_msg(m)
    await send_home(m.chat.id)

@router.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    set_ui(c.message); await state.clear(); await cancel_login(c.from_user.id); await c.answer()
    await show(HOME_TXT, home_kb())

@router.callback_query(F.data == "about")
async def about(c: CallbackQuery):
    await c.answer(fx("🎙 VC Keeper aapke Telegram account ko target group/channel ke voice chat me 24/7 rakhta hai. "
                      "VC off→on hote hi auto rejoin."), show_alert=True)

@router.callback_query(F.data == "guide")
async def guide(c: CallbackQuery):
    await c.answer(fx("1) ACCOUNTS: add account (number + OTP)\n2) TARGETS: group/channel add karo\n"
                      "3) Account us chat ka member ho\nVC start hote hi account join ho jayega."), show_alert=True)

# ---- ACCOUNTS (add / list / remove — sab ek jagah) ----
async def screen_accounts(note=""):
    rows = db.execute("SELECT id,name,phone FROM accounts").fetchall()
    txt = "👤 <b>Accounts</b>\n\n" + (note + "\n\n" if note else "")
    txt += ("\n".join(f"{'🟢' if i in workers else '🔴'} <b>#{i}</b> {esc(n)} · <code>{esc(p)}</code>" for i, n, p in rows)
            if rows else "Koi account nahi hai. ➕ ADD ACCOUNT dabao.")
    k = [[ib("➕ ADD ACCOUNT", "login", style="primary")]]
    k += [[ib(f"🚪 REMOVE #{i} {n}"[:40], f"lo:{i}", style="danger")] for i, n, _ in rows]
    await show(txt, kb(k + [[ib("⬅️ BACK", "home", style="danger")]]))

@router.callback_query(F.data == "accounts")
async def accounts(c: CallbackQuery, state: FSMContext):
    set_ui(c.message); await state.clear(); await cancel_login(c.from_user.id)
    await c.answer(); await screen_accounts()

@router.callback_query(F.data.startswith("lo:"))
async def logout(c: CallbackQuery):
    set_ui(c.message); aid = int(c.data.split(":")[1]); w = workers.pop(aid, None)
    if w: await w.stop()
    db.execute("DELETE FROM accounts WHERE id=?", (aid,)); db.execute("DELETE FROM targets WHERE account_id=?", (aid,)); db.commit()
    await c.answer(fx("✅ Account hata diya")); await screen_accounts()

# ---- ADD ACCOUNT (login flow) ----
@router.callback_query(F.data == "login")
async def login(c: CallbackQuery, state: FSMContext):
    set_ui(c.message); await c.answer(); await state.set_state(Login.phone)
    await show("📱 <b>Add account</b>\n\nPhone number bhejo (country code ke saath)\n<code>+919999999999</code>", back_kb("accounts"))

@router.message(Login.phone)
async def l_phone(m: Message, state: FSMContext):
    await del_msg(m)
    if not m.text: return
    phone = m.text.strip().replace(" ", "")
    await show("⏳ <b>OTP bheja ja raha hai…</b>")
    cl = Client(f"tmp{m.from_user.id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    try:
        await cl.connect(); sent = await cl.send_code(phone)
    except Exception as e:
        try: await cl.disconnect()
        except Exception: pass
        return await show(f"❌ <code>{esc(e)}</code>\n\nDobara number bhejo.", back_kb("accounts"))
    temp[m.from_user.id] = {"cl": cl, "phone": phone, "hash": sent.phone_code_hash}
    await state.set_state(Login.code)
    await show("🔑 <b>OTP</b>\n\nCode <b>spaces ke saath</b> bhejo\n<code>1 2 3 4 5</code>", back_kb("accounts"))

@router.message(Login.code)
async def l_code(m: Message, state: FSMContext):
    await del_msg(m); d = temp.get(m.from_user.id)
    if not d or not m.text: return
    try:
        await d["cl"].sign_in(d["phone"], d["hash"], m.text.replace(" ", ""))
    except SessionPasswordNeeded:
        await state.set_state(Login.pw)
        return await show("🔐 <b>2-Step password</b>\n\nPassword bhejo.", back_kb("accounts"))
    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        return await show(f"❌ <code>{esc(e)}</code>\n\nDobara OTP bhejo.", back_kb("accounts"))
    except Exception as e:
        return await show(f"❌ <code>{esc(e)}</code>", back_kb("accounts"))
    await finish_login(m, state)

@router.message(Login.pw)
async def l_pw(m: Message, state: FSMContext):
    await del_msg(m); d = temp.get(m.from_user.id)
    if not d or not m.text: return
    try: await d["cl"].check_password(m.text)
    except PasswordHashInvalid: return await show("❌ Password galat hai, dobara bhejo.", back_kb("accounts"))
    except Exception as e: return await show(f"❌ <code>{esc(e)}</code>", back_kb("accounts"))
    await finish_login(m, state)

async def finish_login(m: Message, state: FSMContext):
    d = temp.pop(m.from_user.id)
    await show("⏳ <b>Account start ho raha hai…</b>")
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
    db.commit(); await state.clear()
    w = await start_worker(acc_id, session)
    if w: await screen_accounts(f"✅ <b>{esc(me.first_name)}</b> login ho gaya.")
    else: await screen_accounts(f"⚠️ Login hua par worker start nahi hua:\n<code>{esc(errors.get(acc_id))[:200]}</code>")

# ---- TARGETS ----
async def screen_targets(note=""):
    txt = "🎯 <b>Targets</b>\n\n" + (note + "\n\n" if note else ""); rows = []
    for aid, _ in db.execute("SELECT id,name FROM accounts").fetchall():
        w = workers.get(aid)
        for (chat,) in db.execute("SELECT chat FROM targets WHERE account_id=?", (aid,)).fetchall():
            title = chat
            if w:
                try: title = (await w.app.get_chat(int(chat))).title or chat
                except Exception: pass
            rows.append([ib(f"❌ {title}"[:40], f"rt:{aid}:{chat}", style="danger")])
    if is_paused(): txt += "⏸ <b>Paused</b> · account VC se bahar hain, targets saved.\n\n"
    txt += "Remove karne ke liye ❌ dabao." if rows else "Koi target set nahi hai."
    if rows: rows.append([ib("🎧 JOIN ALL", "joinall", style="success"), ib("🚪 LEAVE ALL", "leaveall", style="danger")])
    rows += [[ib("➕ ADD TARGET", "addt", style="primary")], [ib("⬅️ BACK", "home", style="danger")]]
    await show(txt, kb(rows))

@router.callback_query(F.data == "targets")
async def targets(c: CallbackQuery, state: FSMContext):
    set_ui(c.message); await state.clear(); await c.answer(); await screen_targets()

@router.callback_query(F.data.startswith("rt:"))
async def rm_target(c: CallbackQuery):
    set_ui(c.message); _, aid, chat = c.data.split(":"); aid = int(aid); cid = int(chat)
    w = workers.get(aid)
    if w:
        w.targets.discard(cid); w.state.pop(cid, None); w.err.pop(cid, None)
        try: await w.call.leave_call(cid)
        except Exception: pass
    db.execute("DELETE FROM targets WHERE account_id=? AND chat=?", (aid, chat)); db.commit()
    await c.answer(fx("✅ Target hata diya")); await screen_targets()

async def ask_chat(state: FSMContext, acc):
    await state.set_state(Tgt.chat); await state.update_data(acc=acc)
    await show("🎯 <b>Add target</b>\n\nGroup/Channel ka <b>ID</b> (-100…) ya <b>@username</b> / link bhejo.", back_kb("targets"))

@router.callback_query(F.data == "addt")
async def add_target(c: CallbackQuery, state: FSMContext):
    set_ui(c.message)
    accs = db.execute("SELECT id,name FROM accounts").fetchall()
    if not accs: return await c.answer(fx("⚠️ Pehle ACCOUNTS me account add karo."), show_alert=True)
    await c.answer()
    if len(accs) == 1: return await ask_chat(state, accs[0][0])
    rows = [[ib(f"#{i} {n}"[:40], f"st:{i}", style="primary")] for i, n in accs] + [[ib("⬅️ BACK", "targets", style="danger")]]
    await show("🎯 <b>Kaunsa account?</b>", kb(rows))

@router.callback_query(F.data.startswith("st:"))
async def st_pick(c: CallbackQuery, state: FSMContext):
    set_ui(c.message); await c.answer(); await ask_chat(state, int(c.data.split(":")[1]))

@router.message(Tgt.chat)
async def t_chat(m: Message, state: FSMContext):
    await del_msg(m)
    if not m.text: return
    acc = (await state.get_data())["acc"]
    await show("⏳ <b>Please wait…</b>")
    w = await get_worker(acc)
    if not w:
        return await show(f"❌ Account start nahi hua:\n<code>{esc(errors.get(acc, 'unknown'))[:300]}</code>", back_kb("targets"))
    try: cid = await w.resolve(m.text)
    except Exception as e:
        return await show(f"❌ Chat nahi mila. Account us chat ka member hona chahiye.\n<code>{esc(e)}</code>\n\nDobara ID bhejo.", back_kb("targets"))
    db.execute("INSERT OR REPLACE INTO targets VALUES(?,?)", (acc, str(cid))); db.commit()
    w.targets.add(cid); await state.clear()
    if is_paused():
        return await screen_targets(f"✅ Target saved: <code>{cid}</code>\n⏸ Paused hai · JOIN ALL dabao.")
    await show("⏳ <b>VC join try ho raha hai…</b>")
    await w.tick(cid)
    if w.state.get(cid) == "in": await screen_targets(f"✅ Target set · 🎧 VC me join ho gaya: <code>{cid}</code>")
    else: await screen_targets(f"✅ Target set: <code>{cid}</code>\n⏳ Abhi join nahi hua:\n<code>{esc(w.err.get(cid, ''))[:200]}</code>")

# ---- JOIN ALL / LEAVE ALL ----
@router.callback_query(F.data == "leaveall")
async def leave_all(c: CallbackQuery):
    set_ui(c.message); await c.answer(fx("🚪 Leaving…"))
    put_set("paused", "1")                                   # keeper dobara join na kare
    await show("🚪 <b>Sabhi VC se nikal raha hoon…</b>")
    n = 0
    for w in list(workers.values()):
        for cid in list(w.targets):
            try: await asyncio.wait_for(w.call.leave_call(cid), 8); n += 1
            except Exception: pass
            w.state[cid] = "paused"; w.muted[cid] = False; w.err.pop(cid, None)
    await screen_targets(f"✅ {n} VC se nikal gaye. Targets saved hain.")

@router.callback_query(F.data == "joinall")
async def join_all(c: CallbackQuery):
    set_ui(c.message); await c.answer(fx("🎧 Joining…")); del_set("paused")
    await show("🎧 <b>Sabhi VC me join ho raha hoon…</b>")
    tot = ok = 0
    for w in list(workers.values()):
        for cid in list(w.targets):
            tot += 1; await w.tick(cid); ok += 1 if w.state.get(cid) == "in" else 0
    await screen_targets(f"🎧 {ok}/{tot} VC me join ho gaya." + ("" if ok == tot else "\n⏳ Baaki me VC chalu nahi hai, chalu hote hi join honge."))

# ---- STATUS ----
async def screen_status():
    rows = db.execute("SELECT id,name FROM accounts").fetchall()
    txt = "📊 <b>Live status</b>\n\n" + ("" if rows else "Koi account nahi hai.")
    for i, n in rows:
        w = workers.get(i)
        txt += f"👤 <b>{esc(n)}</b> · {'🟢 online' if w else '🔴 offline'}\n"
        if not w and errors.get(i): txt += f"<code>{esc(errors[i])[:150]}</code>\n"
        for cid in (w.targets if w else []):
            ok = w.state.get(cid) == "in"
            label = "⏸ Paused" if is_paused() else ("🎧 In VC" if ok else "⏳ Waiting for VC")
            txt += f"   • <code>{cid}</code> — {label}\n"
            if not ok and not is_paused() and w.err.get(cid): txt += f"     <code>{esc(w.err[cid])[:120]}</code>\n"
            if ok and not is_paused(): txt += f"     {'🔇 mic off' if w.muted.get(cid) else '🎙 mic on'}\n"
            if ok and w.mute_err.get(cid): txt += f"     <code>{esc(w.mute_err[cid])[:150]}</code>\n"
        txt += "\n"
    await show(txt, kb([[ib("🎧 JOIN NOW", "joinnow", style="success"), ib("🔄 REFRESH", "status", style="primary")],
                        [ib("⬅️ BACK", "home", style="danger")]]))

@router.callback_query(F.data == "status")
async def status(c: CallbackQuery, state: FSMContext):
    set_ui(c.message); await state.clear(); await c.answer(); await screen_status()

@router.callback_query(F.data == "joinnow")
async def joinnow(c: CallbackQuery):
    set_ui(c.message); await c.answer(fx("⏳ Joining…")); del_set("paused")
    await show("⏳ <b>VC join try ho raha hai…</b>")
    for w in list(workers.values()):
        for cid in list(w.targets): await w.tick(cid)
    await screen_status()

# ---- VIDEO (bottom keyboard button) ----
VID_KB = lambda: kb([[ib("🚫 REMOVE VIDEO", "rmvid", style="danger")], [ib("⬅️ BACK", "home", style="danger")]])

@router.message(F.text == B_VID)
async def vid_btn(m: Message, state: FSMContext):
    await del_msg(m); await state.clear(); await cancel_login(m.from_user.id)
    if not UI: await send_home(m.chat.id)
    await state.set_state(Vid.wait)
    await show("🎬 <b>Set video</b>\n\nEk video bhejo. Start par ye <b>hidden (spoiler)</b> dikhega.", VID_KB())

@router.message(Vid.wait, F.video)
async def got_video(m: Message, state: FSMContext):
    put_set("video", m.video.file_id); await del_msg(m); await state.clear(); await send_home(m.chat.id)

@router.message(Vid.wait)
async def not_video(m: Message):
    await del_msg(m); await show("⚠️ <b>Sirf video bhejo.</b>", VID_KB())

@router.callback_query(F.data == "rmvid")
async def rm_video(c: CallbackQuery, state: FSMContext):
    set_ui(c.message); del_set("video"); await state.clear(); await c.answer(fx("🚫 Video hata diya"))
    await send_home(c.message.chat.id)

# ---- REFRESH (RAM clear, accounts safe) ----
async def restart_bot(chat_id: int, auto: bool = False):
    put_set("refreshing", f"{chat_id}:{total_mb()}:{1 if auto else 0}")
    for w in list(workers.values()):
        for cid in list(w.targets):
            try: await asyncio.wait_for(w.call.leave_call(cid), 5)
            except Exception: pass
        try: await asyncio.wait_for(w.stop(), 10)
        except Exception: pass
    db.commit(); db.close()
    kill_children(); gc.collect()
    os.execv(sys.executable, [sys.executable] + sys.argv)     # fresh process = fresh RAM

@router.callback_query(F.data == "refresh")
async def refresh(c: CallbackQuery, state: FSMContext):
    set_ui(c.message); await state.clear(); await cancel_login(c.from_user.id)
    await c.answer(fx("♻️ Refreshing…"))
    await show("♻️ <b>Refreshing…</b>\n\nRAM clear ho rahi hai. Accounts aur targets safe hain, kuch seconds me bot wapas aayega.")
    await restart_bot(c.message.chat.id)

async def ram_watchdog():
    while True:
        await asyncio.sleep(60)
        if time.time() - BOOT_TIME < 180: continue          # boot ke turant baad nahi
        used = total_mb()
        if used >= RAM_LIMIT:
            logging.warning("RAM %s MB >= %s MB, auto refresh", used, RAM_LIMIT)
            try: await bot.send_message(OWNER_ID, fx(f"♻️ RAM {used} MB ho gayi · auto refresh ho raha hai…"))
            except Exception: pass
            await restart_bot(OWNER_ID, auto=True)

async def after_refresh(chat_id: int, before: str, auto: bool):
    if not auto: await send_home(chat_id)
    await asyncio.sleep(45)                                    # workers start hone do
    m = await bot.send_message(chat_id, fx(f"✅ {'Auto ' if auto else ''}Refresh done · RAM {before} MB → {total_mb()} MB\nAccounts safe hain."))
    await asyncio.sleep(10)
    try: await m.delete()
    except Exception: pass

# ---- non-owner ----
pub = Router()
@pub.message()
async def private(m: Message):
    await m.answer(fx("🔒 This is a private bot."))

async def main():
    async def boot():
        await prep_media()
        for acc_id, session in db.execute("SELECT id,session FROM accounts").fetchall():
            asyncio.create_task(start_worker(acc_id, session))
    asyncio.create_task(boot())
    r = get_set("refreshing")
    if r:
        del_set("refreshing")
        try:
            chat, before, auto = r.split(":"); asyncio.create_task(after_refresh(int(chat), before, auto == "1"))
        except Exception: pass
    asyncio.create_task(ram_watchdog())
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router); dp.include_router(pub)
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
