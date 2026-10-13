"""
24/7 VC Userbot - Telegram bot panel (photo + buttons + OTP login + AUTO-JOIN)
Env: API_ID, API_HASH, BOT_TOKEN, OWNER_ID, PORT (host deta hai)
Optional: SESSION_STRING, TARGET_ID, AUTO_START (default 1)
"""
import asyncio, json, logging, os, re, sys, time, wave
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)      # Pyrogram isi loop par chalega (important)

from aiohttp import web
from pyrogram import Client, filters
from pyrogram.errors import (SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
                             PasswordHashInvalid, PhoneNumberInvalid, FloodWait)
from pyrogram.types import InlineKeyboardMarkup as KB, InlineKeyboardButton as B
from pytgcalls import PyTgCalls
from pytgcalls import filters as tgf
from pytgcalls.types import MediaStream

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
PORT = int(os.getenv("PORT", "8080"))
DB, SIL, BG = "data.json", "silence.wav", "bg.jpg"

_missing = [k for k, v in {"API_ID": API_ID, "API_HASH": API_HASH,
            "BOT_TOKEN": BOT_TOKEN, "OWNER_ID": OWNER_ID}.items() if not v]
if _missing:
    print("❌ Ye env vars missing hain:", ", ".join(_missing)); sys.exit(1)

data = json.load(open(DB)) if os.path.exists(DB) else {"session": None, "target": None, "running": False}
def save(): json.dump(data, open(DB, "w"))

if os.getenv("SESSION_STRING"): data["session"] = os.getenv("SESSION_STRING").strip()
if os.getenv("TARGET_ID"):      data["target"] = os.getenv("TARGET_ID").strip()
if os.getenv("SESSION_STRING") and os.getenv("TARGET_ID") and os.getenv("AUTO_START", "1") != "0":
    data["running"] = True

bot = Client("controller", API_ID, API_HASH, bot_token=BOT_TOKEN, in_memory=True)
user = call = None
flags = {"joined": False}                # abhi VC me hai ya nahi
st = {"mode": None, "panel": None}       # mode: phone / code / pass / target
lg = {"client": None, "phone": None, "hash": None}

# ---------------- VC ----------------
def make_silence():
    if not os.path.exists(SIL):
        with wave.open(SIL, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000 * 60)

def is_no_vc(e):
    s = (type(e).__name__ + " " + str(e)).lower()
    return any(k in s for k in ("noactivegroupcall", "no active group", "group call", "groupcall",
                                "voice chat", "videochat", "not started"))

async def ensure_client():
    global user, call
    if call is not None: return
    make_silence()
    u = Client("vc_user", API_ID, API_HASH, session_string=data["session"], in_memory=True)
    c = PyTgCalls(u)

    @c.on_update(tgf.stream_end())
    async def _loop(_, upd):
        await c.play(upd.chat_id, MediaStream(SIL, video_flags=MediaStream.Flags.IGNORE))

    try:
        await c.start()
    except Exception:
        try: await u.stop()
        except Exception: pass
        raise
    user, call = u, c

async def try_join():
    """True = VC me hai, False = VC abhi on nahi hai. Baaki errors raise hote hain."""
    if not data["session"]: raise Exception("Pehle account login karo")
    if not data["target"]:  raise Exception("Pehle target ID set karo")
    await ensure_client()
    tid = int(data["target"])
    try: await user.get_chat(tid)
    except Exception as e: logging.warning("get_chat fail: %r", e)
    try:
        await call.play(tid, MediaStream(SIL, video_flags=MediaStream.Flags.IGNORE))
    except Exception as e:
        if is_no_vc(e):
            flags["joined"] = False
            return False
        flags["joined"] = False
        raise
    flags["joined"] = True
    return True

async def leave_chat(tid):
    if call and tid:
        try: await call.leave_call(int(tid))
        except Exception: pass
    flags["joined"] = False

async def full_logout():
    global user, call
    data["running"] = False; save()
    await leave_chat(data["target"])
    if user:
        try: await user.stop()
        except Exception: pass
    user = call = None
    data.update(session=None, running=False); save()

async def watcher():
    """Har 10s check: VC start hote hi auto-join. Join ke baad 2 min me ek baar health check."""
    last = 0
    while True:
        await asyncio.sleep(10)
        try:
            if not (data["running"] and data["session"] and data["target"]): continue
            gap = 120 if flags["joined"] else 10
            if time.time() - last < gap: continue
            last = time.time()
            before = flags["joined"]
            await try_join()
            if flags["joined"] != before and st["mode"] is None:
                await setcap(caption(), home_kb())
        except Exception as e:
            logging.warning("watcher: %r", e)

# ---------------- UI ----------------
def caption(extra=""):
    acc = "✅ Connected" if data["session"] else "❌ Not logged in"
    if data["running"] and flags["joined"]: run = "🟢 VC me hai"
    elif data["running"]:                   run = "🟡 VC start hone ka wait (auto-join ON)"
    else:                                   run = "🔴 Stopped"
    t = (f"🎙 **24/7 VC Panel**\n\n👤 Account: {acc}\n"
         f"🎯 Target: `{data['target'] or 'Not set'}`\n📡 Status: {run}")
    return t + (f"\n\n{extra}" if extra else "")

def home_kb():
    rows = [[B("🚪 Logout Account", callback_data="logout") if data["session"]
             else B("🔑 Login Account", callback_data="login")],
            [B("🎯 Set Target", callback_data="target")],
            [B("🟢 Start VC", callback_data="start"), B("🔴 Stop VC", callback_data="stop")]]
    if data["session"]: rows.append([B("📋 Session String", callback_data="sess")])
    rows.append([B("🔄 Refresh", callback_data="home")])
    return KB(rows)

back_kb = KB([[B("⬅️ Back", callback_data="home")]])

async def setcap(text, kb):
    if not st["panel"]: return
    cid, mid = st["panel"]
    try: await bot.edit_message_caption(cid, mid, text, reply_markup=kb)
    except Exception: pass

async def home(extra=""):
    st["mode"] = None
    await setcap(caption(extra), home_kb())

async def cancel_login():
    if lg["client"]:
        try: await lg["client"].disconnect()
        except Exception: pass
    lg.update(client=None, phone=None, hash=None)

owner = filters.user(OWNER_ID)

@bot.on_message(filters.command("start") & filters.private & owner)
async def start_cmd(_, m):
    sent = await m.reply_photo(BG, caption=caption(), reply_markup=home_kb())
    st["panel"] = (sent.chat.id, sent.id); st["mode"] = None

@bot.on_message(filters.command("start") & filters.private & ~owner)
async def deny(_, m):
    await m.reply(f"⛔ Ye private bot hai.\nTumhari ID: `{m.from_user.id}`")

@bot.on_callback_query(owner)
async def on_cb(_, q):
    d = q.data
    st["panel"] = (q.message.chat.id, q.message.id)
    if d == "home":
        await cancel_login(); await home(); await q.answer()
    elif d == "login":
        st["mode"] = "phone"
        await setcap(caption("📱 Apna phone number bhejo\n(country code ke saath, jaise `+919999999999`)"), back_kb)
        await q.answer()
    elif d == "target":
        st["mode"] = "target"
        await setcap(caption("🎯 Group/Channel ID bhejo\n(jaise `-1001234567890`)"), back_kb)
        await q.answer()
    elif d == "start":
        if not data["session"] or not data["target"]:
            await q.answer("Pehle login karo aur target set karo", show_alert=True); return
        data["running"] = True; save()
        try:
            ok = await try_join()
            msg = ("VC join ho gaya ✅" if ok else
                   "VC abhi off hai ⏳\nAuto-join ON hai: VC start hote hi account khud join ho jayega.")
            await q.answer(msg, show_alert=True)
        except Exception as e:
            logging.exception("join fail")
            await q.answer(f"{type(e).__name__}: {e}"[:190], show_alert=True)
        await home()
    elif d == "stop":
        data["running"] = False; save()
        await leave_chat(data["target"])
        await q.answer("VC chhod diya 🔴 (auto-join band)", show_alert=True); await home()
    elif d == "logout":
        await full_logout(); await q.answer("Logout ho gaya", show_alert=True); await home()
    elif d == "sess":
        await bot.send_message(q.message.chat.id,
            f"🔐 Session String (kisi ko mat dena, Render ke `SESSION_STRING` me daalo, phir ye message delete kar do):\n\n`{data['session']}`")
        await q.answer()

async def finish_login():
    data["session"] = await lg["client"].export_session_string()
    await cancel_login(); save()

async def join_later():
    try: await try_join()
    except Exception as e: logging.warning("join_later: %r", e)
    if st["mode"] is None: await setcap(caption(), home_kb())

@bot.on_message(filters.private & owner & filters.text & ~filters.command(["start"]))
async def on_text(_, m):
    mode, txt = st["mode"], m.text.strip()
    if not mode: return
    try: await m.delete()          # phone/password chat me na dikhe
    except Exception: pass
    try:
        if mode == "phone":
            await cancel_login()
            c = Client("login", API_ID, API_HASH, in_memory=True)
            await c.connect()
            sent = await c.send_code(txt)
            lg.update(client=c, phone=txt, hash=sent.phone_code_hash); st["mode"] = "code"
            await setcap(caption("📨 Telegram app me OTP aaya hoga.\n\n⚠️ OTP **spaces ke saath** bhejo, jaise `1 2 3 4 5` "
                                 "(warna Telegram code block kar deta hai)."), back_kb)
        elif mode == "code":
            code = re.sub(r"\D", "", txt)
            try:
                await lg["client"].sign_in(lg["phone"], lg["hash"], code)
            except SessionPasswordNeeded:
                st["mode"] = "pass"
                await setcap(caption("🔐 2-step verification password bhejo."), back_kb); return
            await finish_login(); await home("✅ Login ho gaya! Ab 🎯 target set karo.")
        elif mode == "pass":
            await lg["client"].check_password(txt)
            await finish_login(); await home("✅ Login ho gaya! Ab 🎯 target set karo.")
        elif mode == "target":
            int(txt)                 # valid number check
            old = data["target"]
            if old and old != txt: await leave_chat(old)
            data["target"] = txt
            if data["session"]: data["running"] = True     # auto-join ON
            save(); flags["joined"] = False
            await home("✅ Target save. Auto-join ON: VC start hote hi account khud join ho jayega.")
            if data["session"]: asyncio.create_task(join_later())
    except PhoneNumberInvalid:
        await setcap(caption("❌ Number galat hai. Dobara bhejo (+91...)."), back_kb)
    except (PhoneCodeInvalid, PhoneCodeExpired):
        await setcap(caption("❌ OTP galat/expire. Back dabake dobara login karo, OTP spaces ke saath bhejo."), back_kb)
        await cancel_login(); st["mode"] = None
    except PasswordHashInvalid:
        await setcap(caption("❌ Password galat. Dobara bhejo."), back_kb)
    except ValueError:
        await setcap(caption("❌ ID number honi chahiye, jaise `-1001234567890`."), back_kb)
    except FloodWait as e:
        await setcap(caption(f"⏳ Telegram ne {e.value}s rukne ko kaha. Baad me try karo."), back_kb)
    except Exception as e:
        await setcap(caption(f"❌ Error: {e}"[:900]), back_kb)

# ---------------- main ----------------
async def main():
    app = web.Application()
    app.add_routes([web.get("/", lambda r: web.Response(text="VC bot running")),
                    web.get("/health", lambda r: web.Response(text="ok"))])
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    try:
        await bot.start()
    except Exception as e:
        print("❌ Bot start fail (BOT_TOKEN/API_ID/API_HASH check karo):", repr(e)); raise
    me = await bot.get_me()
    print(f"✅ Bot online: @{me.username}")
    try:
        sent = await bot.send_photo(OWNER_ID, BG, caption=caption(), reply_markup=home_kb())
        st["panel"] = (sent.chat.id, sent.id)
    except Exception as e:
        print("Owner ko panel nahi bheja (bot me pehle /start dabao, OWNER_ID check karo):", repr(e))
    asyncio.create_task(watcher())       # auto-join watcher
    print("Bot ready")
    await asyncio.Event().wait()

loop.run_until_complete(main())
