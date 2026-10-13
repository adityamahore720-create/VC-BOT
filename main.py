"""
24/7 VC Userbot - Telegram bot panel (photo + buttons + OTP login)
Env: API_ID, API_HASH, BOT_TOKEN, OWNER_ID, PORT (host deta hai)
Optional: SESSION_STRING, TARGET_ID, AUTO_START (default 1)
"""
import asyncio, json, logging, os, re, sys, wave
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)      # Pyrogram isi loop par chalega (important fix)
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
st = {"mode": None, "panel": None}      # mode: phone / code / pass / target
lg = {"client": None, "phone": None, "hash": None}

# ---------------- VC ----------------
def make_silence():
    if not os.path.exists(SIL):
        with wave.open(SIL, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000 * 60)

async def join():
    global user, call
    if not data["session"]: raise Exception("Pehle account login karo")
    if not data["target"]:  raise Exception("Pehle target ID set karo")
    make_silence()
    if user is None:
        user = Client("vc_user", API_ID, API_HASH, session_string=data["session"], in_memory=True)
        call = PyTgCalls(user)

        @call.on_update(tgf.stream_end())
        async def _loop(_, u):
            await call.play(u.chat_id, MediaStream(SIL, video_flags=MediaStream.Flags.IGNORE))

        await call.start()
    tid = int(data["target"])
    try: await user.get_chat(tid)
    except Exception: pass
    await call.play(tid, MediaStream(SIL, video_flags=MediaStream.Flags.IGNORE))
    data["running"] = True; save()

async def leave():
    if call and data["target"]:
        try: await call.leave_call(int(data["target"]))
        except Exception: pass
    data["running"] = False; save()

async def full_logout():
    global user, call
    await leave()
    if user:
        try: await user.stop()
        except Exception: pass
    user = call = None
    data.update(session=None, running=False); save()

# ---------------- UI ----------------
def caption(extra=""):
    acc = "✅ Connected" if data["session"] else "❌ Not logged in"
    run = "🟢 Running" if data["running"] else "🔴 Stopped"
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
        try: await join(); await q.answer("VC join ho gaya ✅", show_alert=True)
        except Exception as e: await q.answer(f"Error: {e}"[:190], show_alert=True)
        await home()
    elif d == "stop":
        await leave(); await q.answer("VC chhod diya 🔴", show_alert=True); await home()
    elif d == "logout":
        await full_logout(); await q.answer("Logout ho gaya", show_alert=True); await home()
    elif d == "sess":
        s = await bot.send_message(q.message.chat.id,
            f"🔐 Session String (kisi ko mat dena, Render ke `SESSION_STRING` me daalo, phir ye message delete kar do):\n\n`{data['session']}`")
        await q.answer()

async def finish_login():
    data["session"] = await lg["client"].export_session_string()
    await cancel_login(); save()

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
            int(txt)                 # check valid number
            data["target"] = txt; save()
            await home("✅ Target save ho gaya.")
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
    if data["session"] and data["target"] and data["running"]:
        try: await join()
        except Exception as e: print("auto-join fail:", e)
    print("Bot ready")
    await asyncio.Event().wait()

loop.run_until_complete(main())
