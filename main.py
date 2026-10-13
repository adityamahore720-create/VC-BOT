"""
24/7 VC Panel - web UI (OTP + 2FA password login) + VC control
Env vars: API_ID, API_HASH, PANEL_PASS, PORT (host deta hai)
Optional (restart ke baad bhi login bacha rahe):
  SESSION_STRING, TARGET_ID, AUTO_START (default 1; band karna ho to 0)
"""
import asyncio, json, os, wave
from aiohttp import web
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired
from pytgcalls import PyTgCalls
from pytgcalls import filters as tgf
from pytgcalls.types import MediaStream

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PANEL_PASS = os.getenv("PANEL_PASS", "admin123")
PORT = int(os.getenv("PORT", "8080"))
DB, SIL = "data.json", "silence.wav"

data = json.load(open(DB)) if os.path.exists(DB) else {"session": None, "target": None, "running": False}
save = lambda: json.dump(data, open(DB, "w"))

# Env vars se settings (Render free par file mit jati hai, isliye)
if os.getenv("SESSION_STRING"): data["session"] = os.getenv("SESSION_STRING").strip()
if os.getenv("TARGET_ID"):      data["target"] = os.getenv("TARGET_ID").strip()
if os.getenv("SESSION_STRING") and os.getenv("TARGET_ID") and os.getenv("AUTO_START", "1") != "0":
    data["running"] = True

login = {"client": None, "phone": None, "hash": None}
user = call = None

def make_silence():
    if not os.path.exists(SIL):
        with wave.open(SIL, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000 * 60)   # 60 sec silence (loop hota hai)

async def start_user():
    global user, call
    if user: return
    user = Client("vc_user", API_ID, API_HASH, session_string=data["session"], in_memory=True)
    call = PyTgCalls(user)

    @call.on_update(tgf.stream_end())
    async def _loop(_, u):
        await call.play(u.chat_id, MediaStream(SIL, video_flags=MediaStream.Flags.IGNORE))

    await call.start()

async def join():
    if not (data["session"] and data["target"]):
        raise web.HTTPBadRequest(text="Pehle login karo aur target set karo")
    make_silence()
    await start_user()
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

# ---------- web ----------
def auth(request):
    if request.headers.get("X-Pass") != PANEL_PASS:
        raise web.HTTPUnauthorized(text="Wrong panel password")

def ok(**kw): return web.json_response({"ok": True, **kw})

async def index(_):  return web.FileResponse("index.html")
async def bg(_):     return web.FileResponse("bg.jpg")

async def status(r):
    auth(r)
    return ok(logged_in=bool(data["session"]), target=data["target"], running=data["running"])

async def send_code(r):
    auth(r)
    phone = (await r.json())["phone"].strip()
    c = Client("login", API_ID, API_HASH, in_memory=True)
    await c.connect()
    sent = await c.send_code(phone)
    login.update(client=c, phone=phone, hash=sent.phone_code_hash)
    return ok(msg="OTP bhej diya (Telegram app check karo)")

async def verify(r):
    auth(r)
    code = (await r.json())["code"].strip().replace(" ", "")
    c = login["client"]
    if not c: raise web.HTTPBadRequest(text="Pehle OTP mangao")
    try:
        await c.sign_in(login["phone"], login["hash"], code)
    except SessionPasswordNeeded:
        return ok(need_password=True)
    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        raise web.HTTPBadRequest(text="OTP galat ya expire ho gaya")
    await finish_login()
    return ok(need_password=False)

async def password(r):
    auth(r)
    pw = (await r.json())["password"]
    try: await login["client"].check_password(pw)
    except Exception: raise web.HTTPBadRequest(text="Password galat hai")
    await finish_login()
    return ok()

async def finish_login():
    data["session"] = await login["client"].export_session_string()
    await login["client"].disconnect()
    login.update(client=None, phone=None, hash=None)
    save()

async def set_target(r):
    auth(r)
    data["target"] = str((await r.json())["target"]).strip(); save()
    return ok()

async def get_session(r):
    auth(r)
    if not data["session"]: raise web.HTTPBadRequest(text="Login nahi hai")
    return ok(session=data["session"])

async def health(_): return web.Response(text="ok")

async def start(r):  auth(r); await join();  return ok()
async def stop(r):   auth(r); await leave(); return ok()

async def logout(r):
    global user, call
    auth(r); await leave()
    if user:
        try: await user.stop()
        except Exception: pass
    user = call = None
    data.update(session=None, running=False); save()
    return ok()

async def on_start(app):
    if data["session"] and data["target"] and data["running"]:
        try: await join()
        except Exception as e: print("auto-join fail:", e)

app = web.Application()
app.on_startup.append(on_start)
app.add_routes([
    web.get("/", index), web.get("/bg.jpg", bg),
    web.get("/health", health), web.get("/api/session", get_session),
    web.get("/api/status", status),
    web.post("/api/send_code", send_code), web.post("/api/verify", verify),
    web.post("/api/password", password), web.post("/api/target", set_target),
    web.post("/api/start", start), web.post("/api/stop", stop), web.post("/api/logout", logout),
])
web.run_app(app, host="0.0.0.0", port=PORT)
