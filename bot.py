import logging
import os
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger("betbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
GROUP_LINK = os.getenv("GROUP_LINK", "https://t.me/your_group_link").strip()
STARTING_BALANCE = int(os.getenv("STARTING_BALANCE", "1000"))
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

GAMES = {"🎲": "Dice", "🎯": "Darts", "🎳": "Bowling", "🏀": "Basketball", "⚽": "Football", "🎰": "Slots"}
VALID_EMOJIS = set(GAMES)
ASK_AMOUNT, ASK_GAME, ASK_PREDICTION, DEP_METHOD, DEP_AMOUNT, WITH_METHOD, WITH_AMOUNT, WITH_ADDRESS, SET_TAX, BAN_USER, SET_UPI, SET_USDT = range(12)


def conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    with closing(conn()) as db:
        with db.cursor() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
                balance BIGINT NOT NULL DEFAULT 1000, is_banned BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            c.execute("""CREATE TABLE IF NOT EXISTS bets (
                bet_id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, challenger_id BIGINT NOT NULL,
                challenger_name TEXT, opponent_id BIGINT, opponent_name TEXT, amount BIGINT NOT NULL,
                emoji TEXT NOT NULL, prediction TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                winner_id BIGINT, challenge_message_id BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                accepted_at TIMESTAMPTZ, resolved_at TIMESTAMPTZ)""")
            c.execute("""CREATE TABLE IF NOT EXISTS transactions (
                tx_id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, tx_type TEXT NOT NULL,
                method TEXT NOT NULL, amount BIGINT NOT NULL, details TEXT,
                status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMPTZ)""")
            c.execute("CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.execute("INSERT INTO system_config(key,value) VALUES('tax_percent','0') ON CONFLICT(key) DO NOTHING")
            c.execute("INSERT INTO system_config(key,value) VALUES('upi_id','not_set@upi') ON CONFLICT(key) DO NOTHING")
            c.execute("INSERT INTO system_config(key,value) VALUES('usdt_bep20_address','not_set') ON CONFLICT(key) DO NOTHING")
        db.commit()


def ensure_user(db, user):
    with db.cursor() as c:
        c.execute("""INSERT INTO users(user_id,username,first_name,balance) VALUES(%s,%s,%s,%s)
            ON CONFLICT(user_id) DO UPDATE SET username=COALESCE(EXCLUDED.username,users.username),
            first_name=COALESCE(EXCLUDED.first_name,users.first_name),updated_at=NOW()
            RETURNING balance,is_banned""", (user.id, user.username, user.first_name, STARTING_BALANCE))
        return c.fetchone()


def banned(db, uid):
    with db.cursor() as c:
        c.execute("SELECT is_banned FROM users WHERE user_id=%s", (uid,))
        r = c.fetchone()
        return bool(r and r["is_banned"])


def balance(db, uid):
    with db.cursor() as c:
        c.execute("SELECT balance FROM users WHERE user_id=%s", (uid,))
        r = c.fetchone()
        return int(r["balance"]) if r else 0


def config(db, key):
    with db.cursor() as c:
        c.execute("SELECT value FROM system_config WHERE key=%s", (key,))
        r = c.fetchone()
        return r["value"] if r else ""


def set_config(db, key, value):
    with db.cursor() as c:
        c.execute("""INSERT INTO system_config(key,value) VALUES(%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""", (key, value))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    with closing(conn()) as db:
        ensure_user(db, u)
        if banned(db, u.id):
            await update.effective_message.reply_text("🚫 You are banned from using this bot.")
            return
        db.commit()
    if update.effective_chat.type == "private":
        await dashboard(update, context)
    else:
        await update.effective_message.reply_text("🎮 Betting Bot is active!\n\nUse /bet @username amount 🎲 even\nor reply to a player's message with /bet amount.")


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    with closing(conn()) as db:
        ensure_user(db, u)
        bal = balance(db, u.id)
        with db.cursor() as c:
            c.execute("SELECT tx_type,method,amount,status FROM transactions WHERE user_id=%s ORDER BY tx_id DESC LIMIT 5", (u.id,))
            rows = c.fetchall()
        db.commit()
    hist = "\n".join(f"• {r['tx_type'].title()} / {r['method'].upper()}: {r['amount']} pts [{r['status'].title()}]" for r in rows) or "No recent transactions."
    text = f"👤 Player Dashboard\n\n💰 Balance: {bal} pts\n\n📜 Recent Transactions:\n{hist}"
    kb = [[InlineKeyboardButton("📥 Deposit", callback_data="dm_deposit"), InlineKeyboardButton("📤 Withdraw", callback_data="dm_withdraw")], [InlineKeyboardButton("🎮 Play in Group", url=GROUP_LINK)]]
    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def dm_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "dm_deposit":
        await q.edit_message_text("Select deposit method:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 UPI", callback_data="dep:upi")], [InlineKeyboardButton("🪙 USDT BEP-20", callback_data="dep:usdt")]]))
        return DEP_METHOD
    await q.edit_message_text("Select withdrawal method:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 UPI", callback_data="with:upi")], [InlineKeyboardButton("🪙 USDT BEP-20", callback_data="with:usdt")]]))
    return WITH_METHOD


async def dep_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    method = q.data.split(":", 1)[1]
    context.user_data["dep_method"] = method
    with closing(conn()) as db:
        address = config(db, "upi_id") if method == "upi" else config(db, "usdt_bep20_address")
    await q.edit_message_text(f"📥 Deposit instructions\n\nSend payment to:\n{address}\n\nThen enter the number of points requested.")
    return DEP_AMOUNT


async def dep_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.effective_message.text.strip())
        if amount <= 0: raise ValueError
    except (ValueError, TypeError):
        await update.effective_message.reply_text("Enter a positive whole number.")
        return DEP_AMOUNT
    method = context.user_data.get("dep_method")
    if method not in ("upi", "usdt"):
        return ConversationHandler.END
    with closing(conn()) as db:
        ensure_user(db, update.effective_user)
        with db.cursor() as c:
            c.execute("INSERT INTO transactions(user_id,tx_type,method,amount,status) VALUES(%s,'deposit',%s,%s,'pending')", (update.effective_user.id, method, amount))
        db.commit()
    context.user_data.clear()
    await update.effective_message.reply_text("✅ Deposit request submitted for admin approval.")
    await dashboard(update, context)
    return ConversationHandler.END


async def with_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["with_method"] = q.data.split(":", 1)[1]
    await q.edit_message_text("Enter the number of points to withdraw:")
    return WITH_AMOUNT


async def with_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.effective_message.text.strip())
        if amount <= 0: raise ValueError
    except (ValueError, TypeError):
        await update.effective_message.reply_text("Enter a positive whole number.")
        return WITH_AMOUNT
    u = update.effective_user
    with closing(conn()) as db:
        ensure_user(db, u)
        bal = balance(db, u.id)
        db.commit()
    if bal < amount:
        await update.effective_message.reply_text(f"Insufficient balance. Current balance: {bal} pts.")
        return WITH_AMOUNT
    context.user_data["with_amount"] = amount
    await update.effective_message.reply_text("Enter your UPI ID:" if context.user_data["with_method"] == "upi" else "Enter your USDT BEP-20 wallet address:")
    return WITH_ADDRESS


async def with_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    address = update.effective_message.text.strip()
    amount = context.user_data.get("with_amount")
    method = context.user_data.get("with_method")
    if not address or not amount or method not in ("upi", "usdt"):
        await update.effective_message.reply_text("Invalid withdrawal session. Start again.")
        return ConversationHandler.END
    with closing(conn()) as db:
        ensure_user(db, u)
        with db.cursor() as c:
            c.execute("SELECT balance FROM users WHERE user_id=%s FOR UPDATE", (u.id,))
            r = c.fetchone()
            if not r or r["balance"] < amount:
                db.rollback()
                await update.effective_message.reply_text("Insufficient funds.")
                return ConversationHandler.END
            c.execute("UPDATE users SET balance=balance-%s,updated_at=NOW() WHERE user_id=%s", (amount, u.id))
            c.execute("INSERT INTO transactions(user_id,tx_type,method,amount,details,status) VALUES(%s,'withdrawal',%s,%s,%s,'pending')", (u.id, method, amount, address))
        db.commit()
    context.user_data.clear()
    await update.effective_message.reply_text("✅ Withdrawal submitted. Points are reserved until admin approval.")
    await dashboard(update, context)
    return ConversationHandler.END


async def bet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text("Betting is available only in the group.")
        return ConversationHandler.END
    u = update.effective_user
    with closing(conn()) as db:
        ensure_user(db, u)
        if banned(db, u.id):
            await update.effective_message.reply_text("🚫 You are banned from betting.")
            return ConversationHandler.END
        db.commit()
    args = context.args or []
    opponent_id = opponent_username = opponent_display = None
    rest = args
    if args and args[0].startswith("@"):
        opponent_username = args[0][1:]
        with closing(conn()) as db:
            with db.cursor() as c:
                c.execute("SELECT user_id FROM users WHERE username ILIKE %s LIMIT 1", (opponent_username,))
                r = c.fetchone()
        if not r:
            await update.effective_message.reply_text("That player is not registered. Ask them to /start the bot first.")
            return ConversationHandler.END
        opponent_id = r["user_id"]
        opponent_display = "@" + opponent_username
        rest = args[1:]
    elif update.effective_message.reply_to_message and update.effective_message.reply_to_message.from_user:
        p = update.effective_message.reply_to_message.from_user
        if p.is_bot or p.id == u.id:
            await update.effective_message.reply_text("Invalid opponent.")
            return ConversationHandler.END
        opponent_id, opponent_username = p.id, p.username
        opponent_display = "@" + p.username if p.username else p.first_name
        with closing(conn()) as db:
            ensure_user(db, p)
            db.commit()
    else:
        await update.effective_message.reply_text("Use /bet @username amount, or reply to a player with /bet amount.")
        return ConversationHandler.END
    context.user_data["challenge"] = {"opponent_id": opponent_id, "opponent_username": opponent_username, "opponent_display": opponent_display}
    if rest:
        try:
            amount = int(rest[0])
            if amount <= 0: raise ValueError
            emoji, prediction = "🎲", None
            for x in rest[1:]:
                if x in VALID_EMOJIS: emoji = x
                if x.lower() in ("even", "odd"): prediction = x.lower()
            if prediction:
                with closing(conn()) as db:
                    if balance(db, u.id) < amount:
                        await update.effective_message.reply_text("Insufficient balance.")
                        return ConversationHandler.END
                await create_bet(update, context, amount, emoji, prediction)
                context.user_data.pop("challenge", None)
                return ConversationHandler.END
        except ValueError:
            pass
    await update.effective_message.reply_text("Enter stake amount:", reply_markup=ForceReply(selective=True))
    return ASK_AMOUNT


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.effective_message.text.strip())
        if amount <= 0: raise ValueError
    except (ValueError, TypeError):
        await update.effective_message.reply_text("Enter a positive whole number.")
        return ASK_AMOUNT
    with closing(conn()) as db:
        if balance(db, update.effective_user.id) < amount:
            await update.effective_message.reply_text("Insufficient balance.")
            return ASK_AMOUNT
    context.user_data["challenge"]["amount"] = amount
    kb = [[InlineKeyboardButton(f"{e} {n}", callback_data=f"game:{e}")] for e,n in GAMES.items()]
    await update.effective_message.reply_text("Select game:", reply_markup=InlineKeyboardMarkup(kb))
    return ASK_GAME


async def ask_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    emoji = q.data.split(":",1)[1]
    context.user_data["challenge"]["emoji"] = emoji
    await q.edit_message_text("Choose prediction:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("EVEN",callback_data="pred:even"),InlineKeyboardButton("ODD",callback_data="pred:odd")]]))
    return ASK_PREDICTION


async def ask_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    prediction = q.data.split(":",1)[1]
    d = context.user_data.get("challenge", {})
    if not d.get("amount") or not d.get("emoji"):
        await q.edit_message_text("Bet session expired. Start again.")
        return ConversationHandler.END
    await q.edit_message_text("Creating challenge...")
    await create_bet(update, context, d["amount"], d["emoji"], prediction)
    context.user_data.pop("challenge", None)
    return ConversationHandler.END


async def create_bet(update, context, amount, emoji, prediction):
    u = update.effective_user
    d = context.user_data["challenge"]
    with closing(conn()) as db:
        ensure_user(db, u)
        with db.cursor() as c:
            c.execute("SELECT balance FROM users WHERE user_id=%s FOR UPDATE", (u.id,))
            r = c.fetchone()
            if not r or r["balance"] < amount:
                db.rollback()
                await update.effective_message.reply_text("Insufficient balance.")
                return
            c.execute("""INSERT INTO bets(chat_id,challenger_id,challenger_name,opponent_id,opponent_name,amount,emoji,prediction)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING bet_id""", (update.effective_chat.id,u.id,u.username or u.first_name,d.get("opponent_id"),d.get("opponent_username"),amount,emoji,prediction))
            bet_id = c.fetchone()["bet_id"]
        db.commit()
    msg = await context.bot.send_message(update.effective_chat.id, f"🎲 BET #{bet_id}\n\n👤 Challenger: {u.first_name}\n🎯 Target: {d['opponent_display']}\n💰 Stake: {amount} pts\n🎮 Game: {emoji} {GAMES[emoji]}\n🔮 Prediction: {prediction.upper()}\n\nReply to this message with /accept or use /accept {bet_id}")
    with closing(conn()) as db:
        with db.cursor() as c:
            c.execute("UPDATE bets SET challenge_message_id=%s WHERE bet_id=%s", (msg.message_id, bet_id))
        db.commit()


async def accept_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text("Acceptance must happen in the group.")
        return
    u, chat_id = update.effective_user, update.effective_chat.id
    bet_id = None
    with closing(conn()) as db:
        ensure_user(db, u)
        if context.args:
            try: bet_id = int(context.args[0])
            except ValueError: pass
        elif update.effective_message.reply_to_message:
            with db.cursor() as c:
                c.execute("SELECT bet_id FROM bets WHERE chat_id=%s AND challenge_message_id=%s AND status='pending'", (chat_id,update.effective_message.reply_to_message.message_id))
                r=c.fetchone(); bet_id=r["bet_id"] if r else None
        if not bet_id:
            await update.effective_message.reply_text("Use /accept BET_ID or reply to the bet message with /accept.")
            return
        with db.cursor() as c:
            c.execute("SELECT * FROM bets WHERE bet_id=%s AND chat_id=%s FOR UPDATE", (bet_id,chat_id)); bet=c.fetchone()
            if not bet or bet["status"] != "pending":
                db.rollback(); await update.effective_message.reply_text("Bet is no longer available."); return
            if bet["opponent_id"] and bet["opponent_id"] != u.id:
                db.rollback(); await update.effective_message.reply_text("You are not the designated opponent."); return
            if bet["challenger_id"] == u.id:
                db.rollback(); await update.effective_message.reply_text("You cannot accept your own bet."); return
            c.execute("SELECT balance FROM users WHERE user_id=%s FOR UPDATE",(bet["challenger_id"],)); cr=c.fetchone()
            c.execute("SELECT balance FROM users WHERE user_id=%s FOR UPDATE",(u.id,)); orow=c.fetchone()
            if not cr or not orow or cr["balance"] < bet["amount"] or orow["balance"] < bet["amount"]:
                db.rollback(); await update.effective_message.reply_text("One or both players lack sufficient balance."); return
            c.execute("UPDATE bets SET status='accepted',opponent_id=%s,opponent_name=%s,accepted_at=NOW() WHERE bet_id=%s",(u.id,u.username or u.first_name,bet_id))
        db.commit()
    try:
        dice = await context.bot.send_dice(chat_id=chat_id, emoji=bet["emoji"])
        value = dice.dice.value
    except Exception:
        with closing(conn()) as db:
            with db.cursor() as c: c.execute("UPDATE bets SET status='pending',opponent_id=%s,opponent_name=%s,accepted_at=NULL WHERE bet_id=%s",(bet["opponent_id"],bet["opponent_name"],bet_id))
            db.commit()
        await update.effective_message.reply_text("Telegram roll failed; bet returned to pending."); return
    outcome = "even" if value % 2 == 0 else "odd"
    winner = bet["challenger_id"] if outcome == bet["prediction"] else u.id
    loser = u.id if winner == bet["challenger_id"] else bet["challenger_id"]
    amount = int(bet["amount"])
    with closing(conn()) as db:
        with db.cursor() as c:
            c.execute("SELECT value FROM system_config WHERE key='tax_percent'"); tr=c.fetchone()
            try: tax=max(0,min(100,int(tr["value"])))
            except (ValueError,TypeError): tax=0
            c.execute("SELECT user_id,balance FROM users WHERE user_id IN (%s,%s) FOR UPDATE",(bet["challenger_id"],u.id)); rows=c.fetchall(); bs={r["user_id"]:r["balance"] for r in rows}
            if bs.get(bet["challenger_id"],0)<amount or bs.get(u.id,0)<amount:
                c.execute("UPDATE bets SET status='cancelled',resolved_at=NOW() WHERE bet_id=%s",(bet_id,)); db.commit(); await update.effective_message.reply_text("Bet cancelled because a player no longer has enough points."); return
            c.execute("UPDATE users SET balance=balance-%s,updated_at=NOW() WHERE user_id=%s",(amount,bet["challenger_id"]))
            c.execute("UPDATE users SET balance=balance-%s,updated_at=NOW() WHERE user_id=%s",(amount,u.id))
            pot=amount*2; tax_amount=pot*tax//100; payout=pot-tax_amount
            c.execute("UPDATE users SET balance=balance+%s,updated_at=NOW() WHERE user_id=%s",(payout,winner))
            c.execute("UPDATE bets SET status='resolved',winner_id=%s,resolved_at=NOW() WHERE bet_id=%s",(winner,bet_id))
        db.commit()
    await update.effective_message.reply_text(f"🎯 Result: {value} ({outcome.upper()})\n🏆 Winner: {winner}\n💰 Pot: {amount*2} pts\n🧾 Tax: {tax_amount} pts ({tax}%)\n🎁 Payout: {payout} pts")


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Unauthorized access."); return
    kb=[[InlineKeyboardButton("⚙️ Tax Rate",callback_data="admin:tax")],[InlineKeyboardButton("🔨 Ban / Unban",callback_data="admin:ban")],[InlineKeyboardButton("💳 Payment Details",callback_data="admin:gateway")],[InlineKeyboardButton("📑 Transactions",callback_data="admin:txs")]]
    await update.effective_message.reply_text("🔧 Admin Panel",reply_markup=InlineKeyboardMarkup(kb))


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Unauthorized",show_alert=True); return ConversationHandler.END
    await q.answer()
    if q.data=="admin:tax":
        await q.message.reply_text("Enter tax percentage 0-100:",reply_markup=ForceReply(selective=True)); return SET_TAX
    if q.data=="admin:ban":
        await q.message.reply_text("Enter Telegram User ID:",reply_markup=ForceReply(selective=True)); return BAN_USER
    if q.data=="admin:gateway":
        await q.message.reply_text("Choose setting:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Set UPI ID",callback_data="gateway:upi")],[InlineKeyboardButton("Set USDT BEP-20",callback_data="gateway:usdt")]])); return ConversationHandler.END
    if q.data=="gateway:upi":
        await q.message.reply_text("Enter platform UPI ID:",reply_markup=ForceReply(selective=True)); return SET_UPI
    if q.data=="gateway:usdt":
        await q.message.reply_text("Enter platform USDT BEP-20 address:",reply_markup=ForceReply(selective=True)); return SET_USDT
    if q.data=="admin:txs":
        with closing(conn()) as db:
            with db.cursor() as c: c.execute("SELECT * FROM transactions WHERE status='pending' ORDER BY tx_id LIMIT 10"); rows=c.fetchall()
        if not rows: await q.message.reply_text("No pending transactions."); return ConversationHandler.END
        for t in rows:
            kb=[[InlineKeyboardButton("✅ Approve",callback_data=f"tx:app:{t['tx_id']}"),InlineKeyboardButton("❌ Reject",callback_data=f"tx:rej:{t['tx_id']}")]]
            await q.message.reply_text(f"Tx #{t['tx_id']}\nUser: {t['user_id']}\nType: {t['tx_type']}\nMethod: {t['method']}\nAmount: {t['amount']} pts\nDetails: {t['details'] or '-'}",reply_markup=InlineKeyboardMarkup(kb))
        return ConversationHandler.END
    return ConversationHandler.END


async def tx_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Unauthorized",show_alert=True); return
    try: _,action,tid=q.data.split(":"); tid=int(tid)
    except ValueError: await q.answer("Invalid transaction",show_alert=True); return
    with closing(conn()) as db:
        with db.cursor() as c:
            c.execute("SELECT * FROM transactions WHERE tx_id=%s FOR UPDATE",(tid,)); t=c.fetchone()
            if not t or t["status"]!="pending": db.rollback(); await q.answer("Already processed",show_alert=True); return
            if action=="app":
                if t["tx_type"]=="deposit": c.execute("UPDATE users SET balance=balance+%s,updated_at=NOW() WHERE user_id=%s",(t["amount"],t["user_id"]))
                status="approved"
            elif action=="rej":
                if t["tx_type"]=="withdrawal": c.execute("UPDATE users SET balance=balance+%s,updated_at=NOW() WHERE user_id=%s",(t["amount"],t["user_id"]))
                status="rejected"
            else: db.rollback(); return
            c.execute("UPDATE transactions SET status=%s,processed_at=NOW() WHERE tx_id=%s",(status,tid))
        db.commit()
    await q.answer("Updated")
    await q.edit_message_text(f"Transaction #{tid}: {status.upper()}")


async def set_tax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return ConversationHandler.END
    try: v=int(update.effective_message.text.strip()); assert 0<=v<=100
    except (ValueError,AssertionError): await update.effective_message.reply_text("Enter 0-100."); return SET_TAX
    with closing(conn()) as db: set_config(db,"tax_percent",str(v)); db.commit()
    await update.effective_message.reply_text(f"Tax set to {v}%."); return ConversationHandler.END


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return ConversationHandler.END
    try: uid=int(update.effective_message.text.strip())
    except ValueError: await update.effective_message.reply_text("Enter a numeric User ID."); return BAN_USER
    with closing(conn()) as db:
        with db.cursor() as c:
            c.execute("SELECT is_banned FROM users WHERE user_id=%s FOR UPDATE",(uid,)); r=c.fetchone()
            if not r: db.rollback(); await update.effective_message.reply_text("User not found."); return ConversationHandler.END
            state=not r["is_banned"]; c.execute("UPDATE users SET is_banned=%s,updated_at=NOW() WHERE user_id=%s",(state,uid))
        db.commit()
    await update.effective_message.reply_text(f"User {uid}: {'BANNED' if state else 'UNBANNED'}"); return ConversationHandler.END


async def set_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return ConversationHandler.END
    v=update.effective_message.text.strip()
    with closing(conn()) as db: set_config(db,"upi_id",v); db.commit()
    await update.effective_message.reply_text("UPI ID updated."); return ConversationHandler.END


async def set_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return ConversationHandler.END
    v=update.effective_message.text.strip()
    with closing(conn()) as db: set_config(db,"usdt_bep20_address",v); db.commit()
    await update.effective_message.reply_text("USDT BEP-20 address updated."); return ConversationHandler.END


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, fmt, *args): return


def health_server():
    port=int(os.getenv("PORT","10000")); HTTPServer(("0.0.0.0",port),Health).serve_forever()


def main():
    if not BOT_TOKEN: raise SystemExit("BOT_TOKEN environment variable is missing")
    if not DATABASE_URL: raise SystemExit("DATABASE_URL environment variable is missing")
    init_db()
    threading.Thread(target=health_server,daemon=True).start()
    app=Application.builder().token(BOT_TOKEN).build()
    dm=ConversationHandler(entry_points=[CallbackQueryHandler(dm_action,pattern=r"^dm_(deposit|withdraw)$")],states={DEP_METHOD:[CallbackQueryHandler(dep_method,pattern=r"^dep:(upi|usdt)$")],DEP_AMOUNT:[MessageHandler(filters.TEXT&~filters.COMMAND,dep_amount)],WITH_METHOD:[CallbackQueryHandler(with_method,pattern=r"^with:(upi|usdt)$")],WITH_AMOUNT:[MessageHandler(filters.TEXT&~filters.COMMAND,with_amount)],WITH_ADDRESS:[MessageHandler(filters.TEXT&~filters.COMMAND,with_address)]},fallbacks=[],allow_reentry=True)
    bets=ConversationHandler(entry_points=[CommandHandler("bet",bet_start)],states={ASK_AMOUNT:[MessageHandler(filters.TEXT&~filters.COMMAND,ask_amount)],ASK_GAME:[CallbackQueryHandler(ask_game,pattern=r"^game:")],ASK_PREDICTION:[CallbackQueryHandler(ask_prediction,pattern=r"^pred:(even|odd)$")]},fallbacks=[],allow_reentry=True)
    admin=ConversationHandler(entry_points=[CallbackQueryHandler(admin_cb,pattern=r"^(admin:(tax|ban|gateway|txs)|gateway:(upi|usdt))$")],states={SET_TAX:[MessageHandler(filters.TEXT&~filters.COMMAND,set_tax)],BAN_USER:[MessageHandler(filters.TEXT&~filters.COMMAND,ban_user)],SET_UPI:[MessageHandler(filters.TEXT&~filters.COMMAND,set_upi)],SET_USDT:[MessageHandler(filters.TEXT&~filters.COMMAND,set_usdt)]},fallbacks=[],allow_reentry=True)
    app.add_handler(CommandHandler("start",start)); app.add_handler(CommandHandler("wallet",start)); app.add_handler(CommandHandler("admin",admin_panel)); app.add_handler(CommandHandler("accept",accept_cmd)); app.add_handler(CallbackQueryHandler(tx_cb,pattern=r"^tx:(app|rej):\d+$")); app.add_handler(dm); app.add_handler(bets); app.add_handler(admin)
    log.info("Bot started successfully")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
