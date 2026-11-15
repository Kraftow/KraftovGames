import os
import hmac
import json
import time
import random
import asyncio
import secrets
import sqlite3
import hashlib
import threading
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse

# читаем настройки из файла .env (работает и на Windows)
_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env):
    for _line in open(_env, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])          # твой Telegram ID — без лимита на кручение
BASE_URL = (os.environ.get("BASE_URL") or os.environ["RENDER_EXTERNAL_URL"]).rstrip("/")    # https://твой-домен
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me-please")
DB_PATH = os.environ.get("DB_PATH", "bot.db")

COOLDOWN = 12 * 60 * 60  # 12 часов
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FEEDBACK_BTN = "✉️ Обратная связь"

# ---- мини-игра «Пельмень или Тапок» (вместо орла и решки) ----
COIN_SIDES = {"pelmen": ("Пельмень", "🥟"), "tapok": ("Тапок", "🩴")}
MAX_BET = 1_000_000_000
CURRENCY = "🪙"

# (название, эмодзи, вес). Сумма весов = 140, поэтому шансы нормируются:
# Фартануло ≈21.4%, Туз ≈42.9%, 100$ ≈10.7%, Не повезло 25%
PRIZES = [
    ("Фартануло", "🍀", 30, "/static/clover.svg"),
    ("Порванный туз червовый", "🃏", 60, "/static/ace.svg"),
    ("Сто долларов", "💵", 15, "/static/money.svg"),
    ("Повезёт в другой раз", "😢", 35, "/static/sad.svg"),
]

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db_lock = threading.Lock()
with db_lock:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS spins(user_id INTEGER, ts REAL, prize TEXT);
        CREATE TABLE IF NOT EXISTS fb_wait(user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS fb_map(owner_msg_id INTEGER PRIMARY KEY, user_id INTEGER);
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, last_seen REAL, blocked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS balances(user_id INTEGER PRIMARY KEY, coins INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tx(user_id INTEGER, ts REAL, delta INTEGER, reason TEXT);
        """
    )
    db.commit()


async def tg(method: str, **data):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{API}/{method}", json=data)
        return r.json()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg(
        "setWebhook",
        url=f"{BASE_URL}/webhook",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message"],
    )
    await tg(
        "setChatMenuButton",
        menu_button={"type": "web_app", "text": "Открыть", "web_app": {"url": BASE_URL}},
    )
    yield


app = FastAPI(lifespan=lifespan)


# ---------- проверка initData от Telegram ----------
def check_init_data(init_data: str) -> dict:
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = pairs.pop("hash", None)
    if not their_hash:
        raise HTTPException(401, "no hash")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    mine = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mine, their_hash):
        raise HTTPException(401, "bad hash")
    if time.time() - int(pairs.get("auth_date", 0)) > 86400:
        raise HTTPException(401, "expired")
    return json.loads(pairs["user"])


def seconds_left(uid: int) -> int:
    if uid == OWNER_ID:
        return 0
    with db_lock:
        row = db.execute("SELECT MAX(ts) FROM spins WHERE user_id=?", (uid,)).fetchone()
    last = row[0]
    if not last:
        return 0
    return max(0, int(COOLDOWN - (time.time() - last)))



# ---------- пользователи и баланс ----------
def touch_user(u: dict):
    with db_lock:
        db.execute(
            "INSERT INTO users(user_id, first_name, username, last_seen, blocked) VALUES(?,?,?,?,0) "
            "ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, "
            "username=excluded.username, last_seen=excluded.last_seen, blocked=0",
            (u["id"], u.get("first_name", ""), (u.get("username") or "").lower(), time.time()),
        )
        db.commit()


def get_balance(uid: int) -> int:
    row = db.execute("SELECT coins FROM balances WHERE user_id=?", (uid,)).fetchone()
    return row[0] if row else 0


def add_balance(uid: int, delta: int, reason: str) -> int:
    """Вызывать только под db_lock. Возвращает новый баланс."""
    db.execute("INSERT OR IGNORE INTO balances(user_id, coins) VALUES(?,0)", (uid,))
    db.execute("UPDATE balances SET coins = coins + ? WHERE user_id=?", (delta, uid))
    db.execute("INSERT INTO tx VALUES(?,?,?,?)", (uid, time.time(), delta, reason))
    db.commit()
    return get_balance(uid)


async def body_user(req: Request):
    body = await req.json()
    user = check_init_data(body["initData"])
    return body, user


async def body_admin(req: Request):
    body, user = await body_user(req)
    if user["id"] != OWNER_ID:  # проверка на сервере, а не в интерфейсе
        raise HTTPException(403, "admin only")
    return body, user

# ---------- API для мини-приложения ----------
@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))


@app.post("/api/status")
async def status(req: Request):
    user = check_init_data((await req.json())["initData"])
    touch_user(user)
    with db_lock:
        bal = get_balance(user["id"])
    return {
        "balance": bal,
        "is_admin": user["id"] == OWNER_ID,
        "left": seconds_left(user["id"]),
        "prizes": [{"name": n, "emoji": e, "img": i} for n, e, _, i in PRIZES],
    }


@app.post("/api/history")
async def history(req: Request):
    user = check_init_data((await req.json())["initData"])
    with db_lock:
        rows = db.execute(
            "SELECT prize, ts FROM spins WHERE user_id=? ORDER BY ts DESC LIMIT 50", (user["id"],)
        ).fetchall()
    return {"items": [{"prize": p, "ts": t} for p, t in rows]}


@app.post("/api/spin")
async def spin(req: Request):
    user = check_init_data((await req.json())["initData"])
    uid = user["id"]
    with db_lock:  # защита от двойного нажатия
        left = seconds_left_nolock(uid)
        if left > 0:
            return JSONResponse({"error": "cooldown", "left": left}, status_code=429)
        idx = random.choices(range(len(PRIZES)), weights=[p[2] for p in PRIZES])[0]
        db.execute("INSERT INTO spins VALUES(?,?,?)", (uid, time.time(), PRIZES[idx][0]))
        db.commit()
    name = PRIZES[idx][0]
    await tg("sendMessage", chat_id=uid, text=f"🎉 Вы выиграли: {name}")
    return {"index": idx, "name": name, "left": 0 if uid == OWNER_ID else COOLDOWN}


def seconds_left_nolock(uid: int) -> int:
    if uid == OWNER_ID:
        return 0
    last = db.execute("SELECT MAX(ts) FROM spins WHERE user_id=?", (uid,)).fetchone()[0]
    return max(0, int(COOLDOWN - (time.time() - last))) if last else 0



# ---------- мини-игры ----------
@app.post("/api/coin")
async def coin(req: Request):
    body, user = await body_user(req)
    uid = user["id"]
    side = body.get("side")
    try:
        bet = int(body.get("bet"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    if side not in COIN_SIDES or bet < 1 or bet > MAX_BET:
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    with db_lock:  # атомарно: проверка баланса + списание/начисление
        bal = get_balance(uid)
        if bet > bal:
            return JSONResponse({"error": "no_money", "balance": bal}, status_code=400)
        result = secrets.choice(list(COIN_SIDES))
        won = result == side
        bal = add_balance(uid, bet if won else -bet, f"coin {'win' if won else 'lose'}")
    return {"result": result, "won": won, "balance": bal}


# ---------- админка (только OWNER_ID) ----------
@app.post("/api/admin/stats")
async def admin_stats(req: Request):
    await body_admin(req)
    with db_lock:
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        blocked = db.execute("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]
        total = db.execute("SELECT COALESCE(SUM(coins),0) FROM balances").fetchone()[0]
        top = db.execute(
            "SELECT b.user_id, u.first_name, u.username, b.coins FROM balances b "
            "LEFT JOIN users u ON u.user_id=b.user_id WHERE b.coins<>0 ORDER BY b.coins DESC LIMIT 10"
        ).fetchall()
    return {
        "users": users, "blocked": blocked, "total": total,
        "top": [{"id": i, "name": n or "", "username": un or "", "coins": c} for i, n, un, c in top],
    }


@app.post("/api/admin/give")
async def admin_give(req: Request):
    body, _ = await body_admin(req)
    target = str(body.get("target", "")).strip()
    try:
        amount = int(body.get("amount"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Некорректная сумма"}, status_code=400)
    if amount == 0 or abs(amount) > MAX_BET:
        return JSONResponse({"error": "Некорректная сумма"}, status_code=400)
    with db_lock:
        if target.lstrip("@").lower() and not target.lstrip("-").isdigit():
            row = db.execute(
                "SELECT user_id, first_name FROM users WHERE username=?", (target.lstrip("@").lower(),)
            ).fetchone()
        elif target.lstrip("-").isdigit():
            row = db.execute(
                "SELECT user_id, first_name FROM users WHERE user_id=?", (int(target),)
            ).fetchone()
        else:
            row = None
        if not row:
            return JSONResponse(
                {"error": "Пользователь не найден (он должен хотя бы раз открыть бота)"}, status_code=404
            )
        uid, name = row
        if amount < 0 and get_balance(uid) + amount < 0:
            amount = -get_balance(uid)  # не уходим в минус
            if amount == 0:
                return JSONResponse({"error": "У пользователя уже 0"}, status_code=400)
        new_bal = add_balance(uid, amount, "admin")
    if amount > 0:
        await tg("sendMessage", chat_id=uid, text=f"💰 Вам начислено {amount} {CURRENCY}\nБаланс: {new_bal} {CURRENCY}")
    return {"ok": True, "user_id": uid, "name": name, "balance": new_bal, "applied": amount}


_broadcast_running = False


async def run_broadcast(text: str):
    global _broadcast_running
    ok = fail = 0
    try:
        with db_lock:
            ids = [r[0] for r in db.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()]
        async with httpx.AsyncClient(timeout=15) as c:
            for uid in ids:
                try:
                    r = (await c.post(f"{API}/sendMessage", json={"chat_id": uid, "text": text})).json()
                except Exception:
                    r = {"ok": False}
                if r.get("ok"):
                    ok += 1
                else:
                    fail += 1
                    if r.get("error_code") == 403:  # пользователь заблокировал бота
                        with db_lock:
                            db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (uid,))
                            db.commit()
                    elif r.get("error_code") == 429:
                        await asyncio.sleep(r.get("parameters", {}).get("retry_after", 3))
                await asyncio.sleep(0.05)  # ~20 сообщений/сек, в рамках лимитов Telegram
        await tg("sendMessage", chat_id=OWNER_ID, text=f"📣 Рассылка завершена\n✅ Доставлено: {ok}\n❌ Не доставлено: {fail}")
    finally:
        _broadcast_running = False


@app.post("/api/admin/broadcast")
async def admin_broadcast(req: Request):
    global _broadcast_running
    body, _ = await body_admin(req)
    text = str(body.get("text", "")).strip()
    if not text or len(text) > 4000:
        return JSONResponse({"error": "Текст пустой или длиннее 4000 символов"}, status_code=400)
    if _broadcast_running:
        return JSONResponse({"error": "Предыдущая рассылка ещё идёт"}, status_code=409)
    with db_lock:
        n = db.execute("SELECT COUNT(*) FROM users WHERE blocked=0").fetchone()[0]
    _broadcast_running = True
    asyncio.create_task(run_broadcast(text))
    return {"ok": True, "recipients": n}


_IMAGES = {"clover.svg", "ace.svg", "money.svg", "sad.svg"}


@app.get("/static/{name}")
async def static_img(name: str):
    if name not in _IMAGES:
        raise HTTPException(404)
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), media_type="image/svg+xml")


# ---------- вебхук бота ----------
@app.post("/webhook")
async def webhook(req: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403)
    msg = (await req.json()).get("message")
    if msg:
        await handle_message(msg)
    return {"ok": True}


async def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "")
    if msg["chat"].get("type") == "private":
        touch_user(msg["from"])

    # владелец отвечает на пересланную обратную связь -> уходит пользователю
    if uid == OWNER_ID and msg.get("reply_to_message"):
        with db_lock:
            row = db.execute(
                "SELECT user_id FROM fb_map WHERE owner_msg_id=?",
                (msg["reply_to_message"]["message_id"],),
            ).fetchone()
        if row:
            await tg("sendMessage", chat_id=row[0], text=f"💬 Ответ:\n{text}")
            return

    if text.startswith("/start"):
        await tg(
            "sendMessage",
            chat_id=chat_id,
            text="Привет! Жми «Открыть» и крути рулетку 🎰",
            reply_markup={
                "inline_keyboard": [[{"text": "🎰 Открыть", "web_app": {"url": BASE_URL}}]]
            },
        )
        await tg(
            "sendMessage",
            chat_id=chat_id,
            text="Если что — пиши нам через кнопку ниже 👇",
            reply_markup={"keyboard": [[{"text": FEEDBACK_BTN}]], "resize_keyboard": True},
        )
        return

    if text == FEEDBACK_BTN:
        with db_lock:
            db.execute("INSERT OR IGNORE INTO fb_wait VALUES(?)", (uid,))
            db.commit()
        await tg("sendMessage", chat_id=chat_id, text="Напишите ваше сообщение одним текстом, я передам его 👌")
        return

    with db_lock:
        waiting = db.execute("SELECT 1 FROM fb_wait WHERE user_id=?", (uid,)).fetchone()
    if waiting:
        u = msg["from"]
        who = f"{u.get('first_name', '')} @{u.get('username', '—')} (id {uid})"
        sent = await tg("sendMessage", chat_id=OWNER_ID, text=f"✉️ Обратная связь от {who}:\n\n{text}")
        with db_lock:
            db.execute("DELETE FROM fb_wait WHERE user_id=?", (uid,))
            if sent.get("ok"):
                db.execute(
                    "INSERT OR REPLACE INTO fb_map VALUES(?,?)",
                    (sent["result"]["message_id"], uid),
                )
            db.commit()
        await tg("sendMessage", chat_id=chat_id, text="Спасибо! Сообщение отправлено ✅")
