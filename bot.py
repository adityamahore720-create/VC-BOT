import asyncio, os, sqlite3, logging
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, FSInputFile)
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality, ChatUpdate, Update
from pytgcalls import filters as tg_filters

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
OWNER_ID = int(os.environ["OWNER_ID"])          # sirf ye banda bot use kar sakta hai
START_VIDEO = os.getenv("START_VIDEO", "")        # file_id ya local path (mp4)
SILENCE = "silence.mp3"                           # ffmpeg se banega (README dekho)

# ---------------- DB ----------------
db = sqlite3.connect(os.getenv("DB_PATH", "data.db"), check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, name TEXT, session TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS targets(account_id INTEGER, chat TEXT, PRIMARY KEY(account_id, chat))")
db.commit()

# ---------------- UI (colored buttons) ----------------
# Bot API 9.4+: style = "primary" (blue) | "success" (green) | "danger" (red)
def btn(text, cb=None, url=None, style=None):
    kw = {"text": text}
    if cb: kw["callback_data"] = cb
    if url: kw["url"] = url
    if style: kw["style"] = style
    return InlineKeyboardButton(**kw)

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("📗 ABOUT", "about"), btn("💌 OWNER", url="https://t.me/your_username")],
        [btn("💡 GUIDE", "guide", style="success")],
        [btn("⌜ LOGIN ⌟", "login", style="primary"), btn("⌜ ACCOUNTS ⌟", "accounts", style="success")],
        [btn("🎯 SET TARGET", "settarget", style="primary"), btn("🗑 REMOVE", "remove", style="danger")],
        [btn("⌜ HELP ⌟", url="https://t.me/your_support"), btn("⌜ UPDATES ⌟", url="https://t.me/your_channel", style="success")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ BACK", "home", style="danger")]])

# ---------------- VC Manager ----------------
class Worker:
    def __init__(self, acc_id, session):
        self.id = acc_id
        self.app = Client(f"acc{acc_id}", api_id=API_ID, api_hash=API_HASH,
                          session_string=session, in_memory=True)
        self.call = PyTgCalls(self.app)
        self.targets: set[int] = set()
        self.task = None

    async def start(self):
        await self.app.start()
        await self.call.start()

        @self.call.on_update(tg_filters.chat_update(
            ChatUpdate.Status.CLOSED_VOICE_CHAT | ChatUpdate.Status.KICKED |
            ChatUpdate.Status.LEFT_CALL))
        async def _closed(_, u: Update):
            logging.info("VC closed/left in %s -> keeper rejoin karega", u.chat_id)

        self.task = asyncio.create_task(self.keeper())

    async def resolve(self, chat: str) -> int:
        chat = chat.strip()
        c = await self.app.get_chat(int(chat) if chat.lstrip("-").isdigit() else chat)
        return c.id

    async def join(self, chat_id: int):
        await self.call.play(chat_id, MediaStream(
            SILENCE, AudioQuality.LOW, video_flags=MediaStream.Flags.IGNORE,
            ffmpeg_parameters="-stream_loop -1"))

    async def keeper(self):
        # har 20 sec check: VC off ho gaya ya hum nikal gaye to dubara join
        while True:
            for chat_id in list(self.targets):
                try:
                    await self.call.played_time(chat_id)       # connected hai to OK
                except Exception:
                    try:
                        await self.join(chat_id)
                        logging.info("[acc %s] joined VC %s", self.id, chat_id)
                    except Exception as e:                      # VC band hai etc.
                        logging.info("[acc %s] VC %s abhi off: %s", self.id, chat_id, e)
            await asyncio.sleep(20)

    async def stop(self):
        if self.task: self.task.cancel()
        try: await self.app.stop()
        except Exception: pass

workers: dict[int, Worker] = {}

async def boot_worker(acc_id, session):
    w = Worker(acc_id, session)
    await w.start()
    workers[acc_id] = w
    for (chat,) in db.execute("SELECT chat FROM targets WHERE account_id=?", (acc_id,)):
        try: w.targets.add(await w.resolve(chat))
        except Exception as e: logging.warning("target %s resolve fail: %s", chat, e)

# ---------------- Bot ----------------
router = Router()
router.message.filter(F.from_user.id == OWNER_ID)
router.callback_query.filter(F.from_user.id == OWNER_ID)

class Login(StatesGroup):
    phone = State(); code = State(); pw = State()

class Tgt(StatesGroup):
    pick = State(); chat = State()

temp: dict[int, dict] = {}

async def send_home(m: Message):
    cap = "<b>🎙 VC KEEPER BOT</b>\n\nApna account login karo, target group/channel set karo — account hamesha VC me rahega."
    if START_VIDEO:
        v = START_VIDEO if not os.path.exists(START_VIDEO) else FSInputFile(START_VIDEO)
        await m.answer_video(v, caption=cap, has_spoiler=True,    # has_spoiler = video hide/blur
                             parse_mode="HTML", reply_markup=main_kb())
    else:
        await m.answer(cap, parse_mode="HTML", reply_markup=main_kb())

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear(); await send_home(m)

@router.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear(); await c.message.answer("🏠 Menu", reply_markup=main_kb()); await c.answer()

@router.callback_query(F.data == "about")
async def about(c: CallbackQuery):
    await c.message.answer("📗 Ye bot aapke account ko target VC me 24/7 rakhta hai.\nVC off→on hone par auto-rejoin.", reply_markup=back_kb()); await c.answer()

@router.callback_query(F.data == "guide")
async def guide(c: CallbackQuery):
    await c.message.answer(
        "💡 <b>Guide</b>\n1) LOGIN → number, OTP, (2FA password)\n2) SET TARGET → account chuno, chat ID / @username do\n"
        "3) Account us group/channel ka member hona chahiye\n4) Bas! VC aate hi account join ho jayega.",
        parse_mode="HTML", reply_markup=back_kb()); await c.answer()

# ---- Login ----
@router.callback_query(F.data == "login")
async def login(c: CallbackQuery, state: FSMContext):
    await state.set_state(Login.phone)
    await c.message.answer("📱 Phone number bhejo (country code ke saath), e.g. +919999999999"); await c.answer()

@router.message(Login.phone)
async def l_phone(m: Message, state: FSMContext):
    phone = m.text.strip().replace(" ", "")
    cl = Client(f"tmp{m.from_user.id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    await cl.connect()
    try:
        sent = await cl.send_code(phone)
    except Exception as e:
        await cl.disconnect(); return await m.answer(f"❌ {e}")
    temp[m.from_user.id] = {"cl": cl, "phone": phone, "hash": sent.phone_code_hash}
    await state.set_state(Login.code)
    await m.answer("🔑 OTP bhejo <b>spaces ke saath</b> (e.g. 1 2 3 4 5) warna Telegram code block kar deta hai.", parse_mode="HTML")

@router.message(Login.code)
async def l_code(m: Message, state: FSMContext):
    d = temp[m.from_user.id]; code = m.text.replace(" ", "")
    try:
        await d["cl"].sign_in(d["phone"], d["hash"], code)
    except SessionPasswordNeeded:
        await state.set_state(Login.pw); return await m.answer("🔐 2-step password bhejo")
    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        return await m.answer(f"❌ {e}")
    await finish_login(m, state)

@router.message(Login.pw)
async def l_pw(m: Message, state: FSMContext):
    d = temp[m.from_user.id]
    try: await d["cl"].check_password(m.text)
    except Exception as e: return await m.answer(f"❌ {e}")
    try: await m.delete()
    except Exception: pass
    await finish_login(m, state)

async def finish_login(m: Message, state: FSMContext):
    d = temp.pop(m.from_user.id)
    me = await d["cl"].get_me()
    session = await d["cl"].export_session_string()
    await d["cl"].disconnect()
    cur = db.execute("INSERT INTO accounts(phone,name,session) VALUES(?,?,?)", (d["phone"], me.first_name, session))
    db.commit()
    await boot_worker(cur.lastrowid, session)
    await state.clear()
    await m.answer(f"✅ Login ho gaya: <b>{me.first_name}</b> ({d['phone']})", parse_mode="HTML", reply_markup=main_kb())

# ---- Accounts list ----
@router.callback_query(F.data == "accounts")
async def accounts(c: CallbackQuery):
    rows = db.execute("SELECT id,phone,name FROM accounts").fetchall()
    if not rows: txt = "Koi account login nahi hai."
    else:
        txt = "👤 <b>Logged-in accounts</b>\n\n"
        for i, p, n in rows:
            t = db.execute("SELECT chat FROM targets WHERE account_id=?", (i,)).fetchall()
            txt += f"#{i} • {n} • {p}\n🎯 " + (", ".join(x[0] for x in t) or "no target") + "\n\n"
    await c.message.answer(txt, parse_mode="HTML", reply_markup=back_kb()); await c.answer()

# ---- Targets ----
def acc_kb(prefix):
    rows = db.execute("SELECT id,name FROM accounts").fetchall()
    return InlineKeyboardMarkup(inline_keyboard=[[btn(f"#{i} {n}", f"{prefix}:{i}", style="primary")] for i, n in rows]
                                + [[btn("⬅️ BACK", "home", style="danger")]])

@router.callback_query(F.data == "settarget")
async def settarget(c: CallbackQuery):
    await c.message.answer("Kaunsa account?", reply_markup=acc_kb("st")); await c.answer()

@router.callback_query(F.data.startswith("st:"))
async def st_pick(c: CallbackQuery, state: FSMContext):
    await state.set_state(Tgt.chat); await state.update_data(acc=int(c.data.split(":")[1]))
    await c.message.answer("Group/Channel ID (-100...) ya @username bhejo"); await c.answer()

@router.message(Tgt.chat)
async def st_chat(m: Message, state: FSMContext):
    acc = (await state.get_data())["acc"]; w = workers.get(acc)
    try: cid = await w.resolve(m.text)
    except Exception as e: return await m.answer(f"❌ Chat nahi mila (account member hai?): {e}")
    db.execute("INSERT OR REPLACE INTO targets VALUES(?,?)", (acc, str(cid))); db.commit()
    w.targets.add(cid); await state.clear()
    await m.answer(f"🎯 Target set: <code>{cid}</code>\nVC chalu hote hi account join ho jayega.", parse_mode="HTML", reply_markup=main_kb())

@router.callback_query(F.data == "remove")
async def remove(c: CallbackQuery):
    await c.message.answer("Kis account ka target hataun?", reply_markup=acc_kb("rm")); await c.answer()

@router.callback_query(F.data.startswith("rm:"))
async def rm(c: CallbackQuery):
    acc = int(c.data.split(":")[1]); w = workers.get(acc)
    if w:
        for t in list(w.targets):
            try: await w.call.leave_call(t)
            except Exception: pass
        w.targets.clear()
    db.execute("DELETE FROM targets WHERE account_id=?", (acc,)); db.commit()
    await c.message.answer("🗑 Targets hata diye.", reply_markup=main_kb()); await c.answer()

@router.message(Command("fileid"))
async def fileid(m: Message):
    if m.video: await m.answer(m.video.file_id)

@router.message(F.video)
async def vid(m: Message):
    await m.answer(f"START_VIDEO file_id:\n<code>{m.video.file_id}</code>", parse_mode="HTML")

async def main():
    for acc_id, session in db.execute("SELECT id,session FROM accounts").fetchall():
        await boot_worker(acc_id, session)
    bot = Bot(BOT_TOKEN); dp = Dispatcher(storage=MemoryStorage()); dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
