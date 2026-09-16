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
        cur.execute('UPDATE users SET balance = balance + %s, updated_at = NOW() WHERE user_id = %s', (delta, user_id))
        if cur.rowcount != 1:
            raise RuntimeError(f'User {user_id} does not exist')


def get_config(key):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM system_config WHERE key = %s', (key,))
            row = cur.fetchone()
            return row['value'] if row else ''


def set_config(key, value):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO system_config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value', (key, value))


def find_user(username):
    username = username.lstrip('@')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT user_id FROM users WHERE username ILIKE %s LIMIT 1', (username,))
            row = cur.fetchone()
            return row['user_id'] if row else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    ensure_user(user.id, user.username, user.first_name)
    if is_banned(user.id):
        if update.message:
            await update.message.reply_text('🚫 You are banned from using this bot.')
        return
    if update.effective_chat.type == 'private':
        await dashboard(update, context)
    else:
        await update.message.reply_text('🎮 Betting Bot is active!\n\nUse /bet @username 100 🎲 even\nor reply to a player message with /bet 100 🎲 even.\n\nUse /wallet in DM to open your wallet.')


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username, user.first_name)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT balance FROM users WHERE user_id = %s', (user.id,))
            balance = int(cur.fetchone()['balance'])
            cur.execute('SELECT tx_type, method, amount, status FROM transactions WHERE user_id = %s ORDER BY tx_id DESC LIMIT 5', (user.id,))
            txs = cur.fetchall()
    history = '\n'.join([f"• {x['tx_type'].title()} ({x['method'].upper()}): {x['amount']} pts [{x['status'].title()}]" for x in txs]) or 'No recent transactions.'
    text = f'👤 *Player Dashboard*\n\n💰 *Balance:* `{balance}` pts\n\n📜 *Recent Transactions:*\n{history}'
    keyboard = [[InlineKeyboardButton('📥 Deposit', callback_data='dm_deposit'), InlineKeyboardButton('📤 Withdraw', callback_data='dm_withdraw')], [InlineKeyboardButton('🎮 Play in Group', url=GROUP_LINK)]]
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=markup)


async def dm_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == 'dm_deposit':
        keyboard = [[InlineKeyboardButton('💳 UPI', callback_data='dep_method:upi')], [InlineKeyboardButton('🪙 USDT BEP-20', callback_data='dep_method:usdt_bep20')]]
        await q.edit_message_text('Select deposit method:', reply_markup=InlineKeyboardMarkup(keyboard))
        return DEP_METHOD
    if q.data == 'dm_withdraw':
        keyboard = [[InlineKeyboardButton('💳 UPI', callback_data='with_method:upi')], [InlineKeyboardButton('🪙 USDT BEP-20', callback_data='with_method:usdt_bep20')]]
        await q.edit_message_text('Select withdrawal method:', reply_markup=InlineKeyboardMarkup(keyboard))
        return WITH_METHOD
    return ConversationHandler.END


async def dep_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    method = q.data.split(':', 1)[1]
    context.user_data['dep_method'] = method
    if method == 'upi':
        address = get_config('upi_id')
        text = f'📥 *UPI Deposit*\n\nSend payment to:\n`{address}`\n\nEnter the amount of points you deposited.'
    else:
        address = get_config('usdt_bep20_address')
        text = f'📥 *USDT BEP-20 Deposit*\n\nSend USDT to:\n`{address}`\n\nEnter the amount of points you deposited.'
    await q.edit_message_text(text, parse_mode='Markdown')
    return DEP_AMOUNT


async def dep_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        amount = int(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await update.message.reply_text('Enter a positive whole number.')
        return DEP_AMOUNT
    method = context.user_data.get('dep_method')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('INSERT INTO transactions (user_id, tx_type, method, amount, status) VALUES (%s, %s, %s, %s, %s)', (user.id, 'deposit', method, amount, 'pending'))
    context.user_data.clear()
    await update.message.reply_text('✅ Deposit request submitted. Admin approval is required.')
    await dashboard(update, context)
    return ConversationHandler.END


async def with_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data['with_method'] = q.data.split(':', 1)[1]
    await q.edit_message_text('Enter the number of points you want to withdraw:')
    return WITH_AMOUNT


async def with_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await update.message.reply_text('Enter a positive whole number.')
        return WITH_AMOUNT
    user = update.effective_user
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT balance FROM users WHERE user_id = %s', (user.id,))
            row = cur.fetchone()
            balance = int(row['balance']) if row else 0
    if balance < amount:
        await update.message.reply_text(f'❌ Insufficient funds. Balance: {balance} pts')
        return ConversationHandler.END
    context.user_data['with_amount'] = amount
    method = context.user_data.get('with_method')
    await update.message.reply_text('Enter your UPI ID:' if method == 'upi' else 'Enter your USDT BEP-20 wallet address:')
    return WITH_ADDRESS


async def with_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    address = update.message.text.strip()
    amount = context.user_data.get('with_amount')
    method = context.user_data.get('with_method')
    if not amount or not method or not address:
        context.user_data.clear()
        await update.message.reply_text('Invalid withdrawal request.')
        return ConversationHandler.END
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT balance FROM users WHERE user_id = %s FOR UPDATE', (user.id,))
            row = cur.fetchone()
            if not row or int(row['balance']) < amount:
                await update.message.reply_text('❌ Insufficient balance.')
                return ConversationHandler.END
            cur.execute('UPDATE users SET balance = balance - %s, updated_at = NOW() WHERE user_id = %s', (amount, user.id))
            cur.execute('INSERT INTO transactions (user_id, tx_type, method, amount, details, status) VALUES (%s, %s, %s, %s, %s, %s)', (user.id, 'withdrawal', method, amount, address, 'pending'))
    context.user_data.clear()
    await update.message.reply_text('✅ Withdrawal request submitted. The amount is reserved until admin approval.')
    await dashboard(update, context)
    return ConversationHandler.END


async def bet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == 'private':
        await update.message.reply_text('⚠️ Betting is only available in group chats.')
        return ConversationHandler.END
    challenger = update.effective_user
    ensure_user(challenger.id, challenger.username, challenger.first_name)
    if is_banned(challenger.id):
        await update.message.reply_text('🚫 You are banned from betting.')
        return ConversationHandler.END
    args = context.args or []
    opponent_id = None
    opponent_username = None
    opponent_display = None
    remaining = args
    if args and args[0].startswith('@'):
        opponent_username = args[0][1:]
        opponent_id = find_user(opponent_username)
        opponent_display = '@' + opponent_username
        if opponent_id is None:
            await update.message.reply_text('❌ That user must use /start first.')
            return ConversationHandler.END
        remaining = args[1:]
    elif update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
        if target.is_bot or target.id == challenger.id:
            await update.message.reply_text('Invalid opponent.')
            return ConversationHandler.END
        opponent_id = target.id
        opponent_username = target.username
        opponent_display = '@' + target.username if target.username else target.first_name
        ensure_user(target.id, target.username, target.first_name)
    else:
        await update.message.reply_text('Use /bet @username 100 🎲 even or reply to a player message with /bet 100 🎲 even.')
        return ConversationHandler.END
    context.user_data['bet_challenge'] = {'opponent_id': opponent_id, 'opponent_username': opponent_username, 'opponent_display': opponent_display, 'challenger_id': challenger.id}
    if len(remaining) >= 2:
        try:
            amount = int(remaining[0])
            if amount > 0:
                emoji = next((x for x in remaining[1:] if x in VALID_EMOJIS), '🎲')
                prediction = next((x.lower() for x in remaining[1:] if x.lower() in ('even', 'odd')), None)
                if prediction:
                    if get_balance(challenger.id) < amount:
                        await update.message.reply_text(f'❌ Insufficient balance. Balance: {get_balance(challenger.id)} pts')
                        context.user_data.clear()
                        return ConversationHandler.END
                    await create_bet(update, context, amount, emoji, prediction)
                    context.user_data.clear()
                    return ConversationHandler.END
        except ValueError:
            pass
    await update.message.reply_text('Enter the point amount for this bet:', reply_markup=ForceReply(selective=True))
    return ASK_AMOUNT


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await update.message.reply_text('Enter a positive whole number.', reply_markup=ForceReply(selective=True))
        return ASK_AMOUNT
    if get_balance(update.effective_user.id) < amount:
        await update.message.reply_text(f'❌ Insufficient balance. Balance: {get_balance(update.effective_user.id)} pts')
        return ASK_AMOUNT
    context.user_data['bet_challenge']['amount'] = amount
    keyboard = [[InlineKeyboardButton(f'{e} {label}', callback_data=f'game:{e}')] for e, label in GAME_EMOJI_LABELS.items()]
    await update.message.reply_text('Select game:', reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_GAME


async def ask_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    emoji = q.data.split(':', 1)[1]
    context.user_data['bet_challenge']['emoji'] = emoji
    keyboard = [[InlineKeyboardButton('Even', callback_data='pred:even'), InlineKeyboardButton('Odd', callback_data='pred:odd')]]
    await q.edit_message_text(f'Game: {emoji}\nChoose your prediction:', reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_PREDICTION


async def ask_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    prediction = q.data.split(':', 1)[1]
    data = context.user_data.get('bet_challenge', {})
    amount = data.get('amount')
    emoji = data.get('emoji')
    if not amount or not emoji:
        await q.edit_message_text('❌ Bet setup expired.')
        context.user_data.clear()
        return ConversationHandler.END
    await q.edit_message_text('Creating bet challenge...')
    await create_bet(update, context, amount, emoji, prediction)
    context.user_data.clear()
    return ConversationHandler.END


async def create_bet(update, context, amount, emoji, prediction):
    chat_id = update.effective_chat.id
    challenger = update.effective_user
    data = context.user_data.get('bet_challenge', {})
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT balance FROM users WHERE user_id = %s FOR UPDATE', (challenger.id,))
            row = cur.fetchone()
            if not row or int(row['balance']) < amount:
                await context.bot.send_message(chat_id, '❌ Insufficient balance.')
                return
            cur.execute('INSERT INTO bets (chat_id, challenger_id, challenger_name, opponent_id, opponent_name, amount, emoji, prediction) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING bet_id', (chat_id, challenger.id, challenger.username or challenger.first_name, data.get('opponent_id'), data.get('opponent_username'), amount, emoji, prediction))
            bet_id = cur.fetchone()['bet_id']
    text = f"🎲 *Bet #{bet_id} Active!*\n\n👤 Challenger: {challenger.first_name}\n🎯 Target: {data.get('opponent_display')}\n💰 Stake: {amount} pts\n🎮 Game: {emoji}\n🎯 Prediction: {prediction.upper()}\n\nAccept with `/accept {bet_id}`."
    sent = await context.bot.send_message(chat_id, text, parse_mode='Markdown')
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('UPDATE bets SET challenge_message_id = %s WHERE bet_id = %s', (sent.message_id, bet_id))


async def accept_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == 'private':
        await update.message.reply_text('⚠️ Accept the bet in the group.')
        return
    user = update.effective_user
    if is_banned(user.id):
        await update.message.reply_text('🚫 You are banned from betting.')
        return
    bet_id = None
    if context.args:
        try:
            bet_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text('Invalid bet ID.')
            return
    elif update.message.reply_to_message:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT bet_id FROM bets WHERE chat_id = %s AND challenge_message_id = %s AND status = %s', (update.effective_chat.id, update.message.reply_to_message.message_id, 'pending'))
                row = cur.fetchone()
                bet_id = row['bet_id'] if row else None
    if not bet_id:
        await update.message.reply_text('Use /accept BET_ID or reply to the bet message.')
        return
    ensure_user(user.id, user.username, user.first_name)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM bets WHERE bet_id = %s AND chat_id = %s FOR UPDATE', (bet_id, update.effective_chat.id))
            bet = cur.fetchone()
            if not bet or bet['status'] != 'pending':
                await update.message.reply_text('❌ Bet is unavailable.')
                return
            if bet['opponent_id'] and bet['opponent_id'] != user.id:
                await update.message.reply_text('❌ You are not the designated opponent.')
                return
            if bet['challenger_id'] == user.id:
                await update.message.reply_text('❌ You cannot accept your own bet.')
                return
            cur.execute('SELECT balance FROM users WHERE user_id = %s FOR UPDATE', (bet['challenger_id'],))
            challenger_row = cur.fetchone()
            cur.execute('SELECT balance FROM users WHERE user_id = %s FOR UPDATE', (user.id,))
            opponent_row = cur.fetchone()
            challenger_balance = int(challenger_row['balance']) if challenger_row else 0
            opponent_balance = int(opponent_row['balance']) if opponent_row else 0
            amount = int(bet['amount'])
            if challenger_balance < amount:
                cur.execute("UPDATE bets SET status = 'cancelled' WHERE bet_id = %s", (bet_id,))
                await update.message.reply_text('❌ Challenger no longer has enough balance. Bet cancelled.')
                return
            if opponent_balance < amount:
                await update.message.reply_text(f'❌ You need {amount} pts. Your balance is {opponent_balance} pts.')
                return
            adjust_balance(conn, bet['challenger_id'], -amount)
            adjust_balance(conn, user.id, -amount)
            tax = int(get_config('tax_percent') or '0')
            cur.execute("UPDATE bets SET status = 'accepted', opponent_id = %s, opponent_name = %s, tax_percent = %s, accepted_at = NOW() WHERE bet_id = %s", (user.id, user.username or user.first_name, tax, bet_id))
    try:
        dice = await context.bot.send_dice(chat_id=update.effective_chat.id, emoji=bet['emoji'])
    except Exception:
        logger.exception('send_dice failed')
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM bets WHERE bet_id = %s FOR UPDATE', (bet_id,))
                current = cur.fetchone()
                if current and current['status'] == 'accepted':
                    adjust_balance(conn, current['challenger_id'], current['amount'])
                    adjust_balance(conn, current['opponent_id'], current['amount'])
                    cur.execute("UPDATE bets SET status = 'cancelled' WHERE bet_id = %s", (bet_id,))
        await update.message.reply_text('❌ Game failed to start. Both stakes were refunded.')
        return
    value = dice.dice.value
    outcome = 'even' if value % 2 == 0 else 'odd'
    winner = bet['challenger_id'] if outcome == bet['prediction'] else user.id
    pot = int(bet['amount']) * 2
    tax_percent = int(bet['tax_percent'] or 0)
    tax_amount = int(pot * tax_percent / 100)
    payout = pot - tax_amount
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT status FROM bets WHERE bet_id = %s FOR UPDATE', (bet_id,))
            state = cur.fetchone()
            if not state or state['status'] != 'accepted':
                await update.message.reply_text('❌ Bet has already been settled.')
                return
            adjust_balance(conn, winner, payout)
            cur.execute("UPDATE bets SET status = 'resolved', winner_id = %s, dice_value = %s, outcome = %s, tax_amount = %s, payout = %s, resolved_at = NOW() WHERE bet_id = %s", (winner, value, outcome, tax_amount, payout, bet_id))
    await update.message.reply_text(f"🎯 *Bet #{bet_id} Result*\n\n🎲 Roll: `{value}`\n📊 Outcome: *{outcome.upper()}*\n🏆 Winner: <a href='tg://user?id={winner}'>Player</a>\n💰 Payout: *{payout} pts*\n🏦 Tax: *{tax_amount} pts* ({tax_percent}%)", parse_mode='HTML')


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text('❌ Unauthorized.')
        return
    keyboard = [[InlineKeyboardButton('⚙️ Tax Rate', callback_data='admin_tax')], [InlineKeyboardButton('🔨 Ban / Unban', callback_data='admin_ban')], [InlineKeyboardButton('💳 Payment Settings', callback_data='admin_gateways')], [InlineKeyboardButton('📑 Financial Queue', callback_data='admin_txs')]]
    await update.message.reply_text('🔧 *Admin Panel*', parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer('Unauthorized', show_alert=True)
        return ConversationHandler.END
    await q.answer()
    if q.data == 'admin_tax':
        await q.message.reply_text('Enter tax percentage from 0 to 100:', reply_markup=ForceReply(selective=True))
        return SET_TAX_STATE
    if q.data == 'admin_ban':
        await q.message.reply_text('Enter Telegram User ID:', reply_markup=ForceReply(selective=True))
        return BAN_USER_STATE
    if q.data == 'admin_gateways':
        keyboard = [[InlineKeyboardButton('Set UPI ID', callback_data='set_gateway_upi')], [InlineKeyboardButton('Set USDT Address', callback_data='set_gateway_usdt')]]
        await q.message.reply_text('Choose payment setting:', reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
    if q.data == 'set_gateway_upi':
        await q.message.reply_text('Enter platform UPI ID:', reply_markup=ForceReply(selective=True))
        return SET_UPI_STATE
    if q.data == 'set_gateway_usdt':
        await q.message.reply_text('Enter platform USDT BEP-20 address:', reply_markup=ForceReply(selective=True))
        return SET_USDT_STATE
    if q.data == 'admin_txs':
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM transactions WHERE status = %s ORDER BY tx_id ASC LIMIT 10', ('pending',))
                txs = cur.fetchall()
        if not txs:
            await q.message.reply_text('✅ Financial queue is empty.')
            return ConversationHandler.END
        for tx in txs:
            details = f"\n📍 Details: `{tx['details']}`" if tx['details'] else ''
            keyboard = [[InlineKeyboardButton('✅ Approve', callback_data=f"tx_app_{tx['tx_id']}"), InlineKeyboardButton('❌ Reject', callback_data=f"tx_rej_{tx['tx_id']}")]]
            await q.message.reply_text(f"🧾 *Transaction #{tx['tx_id']}*\n\nUser: `{tx['user_id']}`\nType: `{tx['tx_type'].upper()}`\nMethod: `{tx['method'].upper()}`\nAmount: *{tx['amount']} pts*{details}", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        return ConversationHandler.END
    return ConversationHandler.END


async def tx_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer('Unauthorized', show_alert=True)
        return
    try:
        _, action, tx_id_text = q.data.split('_')
        tx_id = int(tx_id_text)
    except Exception:
        await q.answer('Invalid transaction', show_alert=True)
        return
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM transactions WHERE tx_id = %s FOR UPDATE', (tx_id,))
            tx = cur.fetchone()
            if not tx or tx['status'] != 'pending':
                await q.answer('Already processed or not found', show_alert=True)
                return
            if action == 'app':
                new_status = 'approved'
                if tx['tx_type'] == 'deposit':
                    adjust_balance(conn, tx['user_id'], tx['amount'])
            else:
                new_status = 'rejected'
                if tx['tx_type'] == 'withdrawal':
                    adjust_balance(conn, tx['user_id'], tx['amount'])
            cur.execute('UPDATE transactions SET status = %s, processed_at = NOW(), processed_by = %s WHERE tx_id = %s', (new_status, q.from_user.id, tx_id))
    await q.answer(f'Transaction {new_status}')
    await q.edit_message_text(f'✅ Transaction #{tx_id}: *{new_status.upper()}*', parse_mode='Markdown')


async def set_tax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        value = int(update.message.text.strip())
        if value < 0 or value > 100:
            raise ValueError
    except (ValueError, AttributeError):
        await update.message.reply_text('Enter a whole number from 0 to 100.')
        return SET_TAX_STATE
    set_config('tax_percent', str(value))
    await update.message.reply_text(f'✅ Tax set to {value}%.')
    return ConversationHandler.END


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = int(update.message.text.strip())
    except (ValueError, AttributeError):
        await update.message.reply_text('Enter a numeric Telegram User ID.')
        return BAN_USER_STATE
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT is_banned FROM users WHERE user_id = %s FOR UPDATE', (user_id,))
            row = cur.fetchone()
            if not row:
                await update.message.reply_text('❌ User not found. They must use /start first.')
                return ConversationHandler.END
            new_state = not bool(row['is_banned'])
            cur.execute('UPDATE users SET is_banned = %s, updated_at = NOW() WHERE user_id = %s', (new_state, user_id))
    await update.message.reply_text(f"✅ User {user_id}: {'BANNED' if new_state else 'UNBANNED'}")
    return ConversationHandler.END


async def set_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text('UPI ID cannot be empty.')
        return SET_UPI_STATE
    set_config('upi_id', value)
    await update.message.reply_text('✅ UPI ID updated.')
    return ConversationHandler.END


async def set_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if not value:
        await update.message.reply_text('USDT address cannot be empty.')
        return SET_USDT_STATE
    set_config('usdt_bep20_address', value)
    await update.message.reply_text('✅ USDT BEP-20 address updated.')
    return ConversationHandler.END


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get('PORT', '10000'))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info('Health server started on port %s', port)


def main():
    require_config()
    init_db()
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()

    dm_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(dm_actions, pattern=r'^dm_')],
        states={
            DEP_METHOD: [CallbackQueryHandler(dep_method, pattern=r'^dep_method:')],
            DEP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, dep_amount)],
            WITH_METHOD: [CallbackQueryHandler(with_method, pattern=r'^with_method:')],
            WITH_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, with_amount)],
            WITH_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, with_address)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    bet_conv = ConversationHandler(
        entry_points=[CommandHandler('bet', bet_start)],
        states={
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount)],
            ASK_GAME: [CallbackQueryHandler(ask_game, pattern=r'^game:')],
            ASK_PREDICTION: [CallbackQueryHandler(ask_prediction, pattern=r'^pred:')],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern=r'^(admin_|set_gateway_)')],
        states={
            SET_TAX_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_tax)],
            BAN_USER_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ban_user)],
            SET_UPI_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_upi)],
            SET_USDT_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_usdt)],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('wallet', start))
    app.add_handler(CommandHandler('admin', admin_panel))
    app.add_handler(CommandHandler('accept', accept_cmd))
    app.add_handler(dm_conv)
    app.add_handler(bet_conv)
    app.add_handler(admin_conv)
    app.add_handler(CallbackQueryHandler(tx_approval, pattern=r'^tx_'))

    logger.info('Bot starting...')
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
# ============================================================================
# CONVERSATION STATES
# ============================================================================

(
    ASK_AMOUNT,
    ASK_GAME,
    ASK_PREDICTION,
    DEP_METHOD,
    DEP_AMOUNT,
    WITH_METHOD,
    WITH_AMOUNT,
    WITH_ADDRESS,
    SET_TAX_STATE,
    BAN_USER_STATE,
    SET_UPI_STATE,
    SET_USDT_STATE,
) = range(12)


# ============================================================================
# DATABASE
# ============================================================================

@contextmanager
def get_conn():
    """
    PostgreSQL database connection.

    Every transaction is committed automatically if successful.
    If an exception occurs, the transaction is rolled back.
    """
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL environment variable is not configured."
        )

    conn = None

    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            cursor_factory=RealDictCursor,
        )

        yield conn

        conn.commit()

    except Exception:
        if conn:
            conn.rollback()
        raise

    finally:
        if conn:
            conn.close()


def init_db():
    """
    Create all required PostgreSQL tables.
    """

    with get_conn() as conn:
        with conn.cursor() as cur:

            # ----------------------------------------------------------------
            # USERS
            # ----------------------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    balance BIGINT NOT NULL DEFAULT 1000,
                    is_banned BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # ----------------------------------------------------------------
            # BETS
            # ----------------------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bets (
                    bet_id BIGSERIAL PRIMARY KEY,

                    chat_id BIGINT NOT NULL,

                    challenger_id BIGINT NOT NULL,
                    challenger_name TEXT,

                    opponent_id BIGINT,
                    opponent_name TEXT,

                    amount BIGINT NOT NULL,

                    emoji TEXT NOT NULL DEFAULT '🎲',

                    prediction TEXT NOT NULL,

                    status TEXT NOT NULL DEFAULT 'pending',

                    winner_id BIGINT,

                    challenge_message_id BIGINT,

                    dice_value INTEGER,

                    outcome TEXT,

                    tax_percent INTEGER DEFAULT 0,

                    tax_amount BIGINT DEFAULT 0,

                    payout BIGINT DEFAULT 0,

                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    accepted_at TIMESTAMPTZ,
                    resolved_at TIMESTAMPTZ
                )
                """
            )

            # ----------------------------------------------------------------
            # TRANSACTIONS
            # ----------------------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    tx_id BIGSERIAL PRIMARY KEY,

                    user_id BIGINT NOT NULL,

                    tx_type TEXT NOT NULL,

                    method TEXT NOT NULL,

                    amount BIGINT NOT NULL,

                    details TEXT,

                    status TEXT NOT NULL DEFAULT 'pending',

                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

                    processed_at TIMESTAMPTZ,

                    processed_by BIGINT
                )
                """
            )

            # ----------------------------------------------------------------
            # SYSTEM CONFIG
            # ----------------------------------------------------------------

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

            # ----------------------------------------------------------------
            # DEFAULT CONFIG
            # ----------------------------------------------------------------

            cur.execute(
                """
                INSERT INTO system_config (key, value)
                VALUES ('tax_percent', '0')
                ON CONFLICT (key) DO NOTHING
                """
            )

            cur.execute(
                """
                INSERT INTO system_config (key, value)
                VALUES ('upi_id', 'not_set@upi')
                ON CONFLICT (key) DO NOTHING
                """
            )

            cur.execute(
                """
                INSERT INTO system_config (key, value)
                VALUES (
                    'usdt_bep20_address',
                    '0x000000000000000000000000000000000000000                chat_id INTEGER NOT NULL,
                challenger_id INTEGER NOT NULL,
                challenger_name TEXT,
                opponent_id INTEGER,
                opponent_name TEXT,
                amount INTEGER NOT NULL,
                emoji TEXT NOT NULL DEFAULT '🎲',
                prediction TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                winner_id INTEGER,
                challenge_message_id INTEGER,
                created_at TEXT
            )
            """
        )
        # Financial Transactions
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                tx_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                tx_type TEXT NOT NULL,
                method TEXT NOT NULL,
                amount INTEGER NOT NULL,
                details TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT
            )
            """
        )
        # Global Platform Configuration
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        # Default Config Data
        conn.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES ('tax_percent', '0')")
        conn.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES ('upi_id', 'not_set@upi')")
        conn.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES ('usdt_bep20_address', '0x0000000000000000000000000000000000000000')")
        conn.commit()


def ensure_user(conn, user_id, username):
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users (user_id, username, balance) VALUES (?, ?, ?)",
            (user_id, username, STARTING_BALANCE),
        )
        conn.commit()
        return STARTING_BALANCE, 0
    if username and row["username"] != username:
        conn.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        conn.commit()
    return row["balance"], row["is_banned"]


def is_user_banned(conn, user_id):
    row = conn.execute("SELECT is_banned FROM users WHERE user_id=? AND is_banned=1", (user_id,)).fetchone()
    return row is not None


def get_balance(conn, user_id):
    row = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row["balance"] if row else 0


def adjust_balance(conn, user_id, delta):
    conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))
    conn.commit()


def find_user_id_by_username(conn, username):
    username = username.lstrip("@")
    row = conn.execute("SELECT user_id FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
    return row["user_id"] if row else None


def get_config_val(conn, key):
    row = conn.execute("SELECT value FROM system_config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


# ---------------------------------------------------------------------------
# Middlewares & Primary Route Dispatchers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    with closing(get_conn()) as conn:
        if is_user_banned(conn, user.id):
            await update.message.reply_text("🚫 You are banned from using this bot.")
            return
        ensure_user(conn, user.id, user.username)

    if chat.type == "private":
        await show_dm_dashboard(update, context)
    else:
        await update.message.reply_text(
            "🎮 *Betting Bot is Active!*\n\n"
            "Use `/bet <@username|reply> <amount>` to place a challenge.\n"
            "To view your wallet, request deposits/withdrawals, or adjust settings, please DM the bot directly.",
            parse_mode="Markdown"
        )


async def show_dm_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with closing(get_conn()) as conn:
        balance = get_balance(conn, user.id)
        txs = conn.execute(
            "SELECT tx_type, method, amount, status FROM transactions WHERE user_id=? ORDER BY tx_id DESC LIMIT 5",
            (user.id,),
        ).fetchall()

    history_text = "\n".join(
        [f"• {t['tx_type'].title()} ({t['method'].upper()}): {t['amount']} pts [{t['status'].title()}]" for t in txs]
    ) if txs else "No recent transactions found."

    text = (
        f"👤 *Player Dashboard*\n\n"
        f"💰 *Wallet Balance:* `{balance}` pts\n\n"
        f"📜 *Recent Transaction History:*\n{history_text}"
    )

    keyboard = [
        [
            InlineKeyboardButton("📥 Deposit", callback_data="dm_deposit"),
            InlineKeyboardButton("📤 Withdraw", callback_data="dm_withdraw"),
        ],
        [InlineKeyboardButton("🎮 Play in Group", url=GROUP_LINK)],
    ]

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ---------------------------------------------------------------------------
# DM Financial Processing Workflows (Deposit & Withdrawal)
# ---------------------------------------------------------------------------

async def handle_dm_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "dm_deposit":
        keyboard = [
            [InlineKeyboardButton("💳 UPI", callback_data="dep_method:upi")],
            [InlineKeyboardButton("🪙 USDT (BEP-20)", callback_data="dep_method:usdt_bep20")],
        ]
        await query.edit_message_text("Select deposit payment method:", reply_markup=InlineKeyboardMarkup(keyboard))
        return DEP_METHOD

    elif query.data == "dm_withdraw":
        keyboard = [
            [InlineKeyboardButton("💳 UPI", callback_data="with_method:upi")],
            [InlineKeyboardButton("🪙 USDT (BEP-20)", callback_data="with_method:usdt_bep20")],
        ]
        await query.edit_message_text("Select withdrawal payment method:", reply_markup=InlineKeyboardMarkup(keyboard))
        return WITH_METHOD


async def process_dep_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = query.data.split(":")[1]
    context.user_data["dep_method"] = method

    with closing(get_conn()) as conn:
        if method == "upi":
            address = get_config_val(conn, "upi_id")
            instructions = f"Send payment via UPI to:\n`{address}`"
        else:
            address = get_config_val(conn, "usdt_bep20_address")
            instructions = f"Send BEP-20 USDT to:\n`{address}`"

    await query.edit_message_text(
        f"📥 *Deposit Instructions*\n\n{instructions}\n\n"
        "After transferring, enter the total point amount deposited:",
        parse_mode="Markdown"
    )
    return DEP_AMOUNT


async def process_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        amount = int(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please provide a valid positive integer.")
        return DEP_AMOUNT

    method = context.user_data.get("dep_method", "unknown")

    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO transactions (user_id, tx_type, method, amount, created_at) VALUES (?, 'deposit', ?, ?, ?)",
            (user.id, method, amount, datetime.utcnow().isoformat()),
        )
        conn.commit()

    await update.message.reply_text("✅ Deposit request submitted to admins for manual confirmation.")
    context.user_data.clear()
    await show_dm_dashboard(update, context)
    return ConversationHandler.END


async def process_with_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = query.data.split(":")[1]
    context.user_data["with_method"] = method

    await query.edit_message_text("Enter the total points you wish to withdraw:")
    return WITH_AMOUNT


async def process_with_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        amount = int(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please provide a valid positive integer.")
        return WITH_AMOUNT

    with closing(get_conn()) as conn:
        balance = get_balance(conn, user.id)
        if balance < amount:
            await update.message.reply_text(f"Insufficient funds. Your current balance is {balance} pts.")
            return ConversationHandler.END

    context.user_data["with_amount"] = amount
    method = context.user_data.get("with_method")
    prompt = "Enter your UPI ID:" if method == "upi" else "Enter your USDT BEP-20 Wallet Address:"
    await update.message.reply_text(prompt)
    return WITH_ADDRESS


async def process_with_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    address = update.message.text.strip()
    amount = context.user_data.get("with_amount")
    method = context.user_data.get("with_method")

    with closing(get_conn()) as conn:
        balance = get_balance(conn, user.id)
        if balance < amount:
            await update.message.reply_text("Insufficient funds balance changed during processing.")
            return ConversationHandler.END

        # Reserve user funds immediately
        adjust_balance(conn, user.id, -amount)
        conn.execute(
            "INSERT INTO transactions (user_id, tx_type, method, amount, details, created_at) VALUES (?, 'withdrawal', ?, ?, ?, ?)",
            (user.id, method, amount, address, datetime.utcnow().isoformat()),
        )
        conn.commit()

    await update.message.reply_text("✅ Withdrawal request logged and queued for administrative approval.")
    context.user_data.clear()
    await show_dm_dashboard(update, context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Group Betting Module
# ---------------------------------------------------------------------------

async def bet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("⚠️ Betting features are reserved exclusively for group chats!")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    challenger = update.effective_user

    with closing(get_conn()) as conn:
        if is_user_banned(conn, challenger.id):
            await update.message.reply_text("🚫 You are banned from participating in games.")
            return ConversationHandler.END
        ensure_user(conn, challenger.id, challenger.username)

    args = context.args or []
    opponent_id, opponent_username, opponent_display = None, None, None
    remaining_args = args

    if args and args[0].startswith("@"):
        opponent_username = args[0].lstrip("@")
        with closing(get_conn()) as conn:
            opponent_id = find_user_id_by_username(conn, opponent_username)
        opponent_display = f"@{opponent_username}"
        remaining_args = args[1:]
    elif update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied = update.message.reply_to_message.from_user
        if replied.is_bot or replied.id == challenger.id:
            await update.message.reply_text("Invalid opponent target.")
            return ConversationHandler.END
        opponent_id = replied.id
        opponent_username = replied.username
        opponent_display = f"@{replied.username}" if replied.username else replied.first_name
        with closing(get_conn()) as conn:
            ensure_user(conn, replied.id, replied.username)
        remaining_args = args
    else:
        await update.message.reply_text("Specify an opponent by tagging them (`/bet @user`) or replying to their message.")
        return ConversationHandler.END

    context.user_data["bet_challenge"] = {
        "opponent_id": opponent_id,
        "opponent_username": opponent_username,
        "opponent_display": opponent_display,
        "challenger_id": challenger.id,
    }

    if len(remaining_args) >= 2:
        try:
            amount = int(remaining_args[0])
            if amount > 0:
                emoji = "🎲"
                prediction = None
                for a in remaining_args[1:]:
                    if a in VALID_EMOJIS:
                        emoji = a
                    elif a.lower() in ("even", "odd"):
                        prediction = a.lower()
                if prediction:
                    await _create_bet_and_announce(update, context, amount, emoji, prediction)
                    context.user_data.pop("bet_challenge", None)
                    return ConversationHandler.END
        except ValueError:
            pass

    await update.message.reply_text("Specify the point amount for this bet:", reply_markup=ForceReply(selective=True))
    return ASK_AMOUNT


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int((update.message.text or "").strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a positive numeric value.", reply_markup=ForceReply(selective=True))
        return ASK_AMOUNT

    challenger = update.effective_user
    with closing(get_conn()) as conn:
        balance = get_balance(conn, challenger.id)

    if balance < amount:
        await update.message.reply_text(f"Insufficient funds. Total points available: {balance}.", reply_markup=ForceReply(selective=True))
        return ASK_AMOUNT

    context.user_data["bet_challenge"]["amount"] = amount
    keyboard = [[InlineKeyboardButton(f"{emoji} {label}", callback_data=f"game:{emoji}")] for emoji, label in GAME_EMOJI_LABELS.items()]
    await update.message.reply_text("Select game mode:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_GAME


async def ask_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emoji = query.data.split(":", 1)[1]
    context.user_data.setdefault("bet_challenge", {})["emoji"] = emoji
    keyboard = [[InlineKeyboardButton("Even", callback_data="pred:even"), InlineKeyboardButton("Odd", callback_data="pred:odd")]]
    await query.edit_message_text(f"Selected Game Mode: {emoji}\nChoose your outcome prediction:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_PREDICTION


async def ask_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prediction = query.data.split(":", 1)[1]
    data = context.user_data.get("bet_challenge", {})
    amount, emoji = data.get("amount"), data.get("emoji")

    if amount is None or emoji is None:
        await query.edit_message_text("Bet setup cancelled due to invalid sequence state.")
        context.user_data.pop("bet_challenge", None)
        return ConversationHandler.END

    await query.edit_message_text("Initializing bet challenge...")
    await _create_bet_and_announce(update, context, amount, emoji, prediction)
    context.user_data.pop("bet_challenge", None)
    return ConversationHandler.END


async def _create_bet_and_announce(update, context, amount, emoji, prediction):
    chat_id = update.effective_chat.id
    challenger = update.effective_user
    data = context.user_data.get("bet_challenge", {})

    with closing(get_conn()) as conn:
        conn.execute(
            """
            INSERT INTO bets (chat_id, challenger_id, challenger_name, opponent_id, opponent_name, amount, emoji, prediction, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                challenger.id,
                challenger.username or challenger.first_name,
                data.get("opponent_id"),
                data.get("opponent_username"),
                amount,
                emoji,
                prediction,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        bet_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    text = (
        f"🎲 *Bet #{bet_id} Active!*\n\n"
        f"👤 *Challenger:* {challenger.first_name}\n"
        f"🎯 *Target:* {data.get('opponent_display')}\n"
        f"💰 *Stake:* {amount} pts\n"
        f"🎮 *Game:* {emoji} | *Prediction:* {prediction.upper()}\n\n"
        f"To accept this bet, target opponent must reply to this message or use `/accept {bet_id}`."
    )
    sent = await context.bot.send_message(chat_id, text, parse_mode="Markdown")

    with closing(get_conn()) as conn:
        conn.execute("UPDATE bets SET challenge_message_id=? WHERE bet_id=?", (sent.message_id, bet_id))
        conn.commit()


async def accept_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("⚠️ Acceptance must occur within the group chat!")
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    bet_id = None

    if context.args:
        try:
            bet_id = int(context.args[0])
        except ValueError:
            return
    elif update.message.reply_to_message:
        with closing(get_conn()) as conn:
            row = conn.execute(
                "SELECT bet_id FROM bets WHERE chat_id=? AND challenge_message_id=? AND status='pending'",
                (chat_id, update.message.reply_to_message.message_id),
            ).fetchone()
            if row:
                bet_id = row["bet_id"]

    if not bet_id:
        return

    with closing(get_conn()) as conn:
        if is_user_banned(conn, user.id):
            await update.message.reply_text("You are banned from participating.")
            return

        bet = conn.execute("SELECT * FROM bets WHERE bet_id=? AND chat_id=?", (bet_id, chat_id)).fetchone()
        if not bet or bet["status"] != "pending":
            await update.message.reply_text("This challenge is no longer available.")
            return

        if bet["opponent_id"] and bet["opponent_id"] != user.id:
            await update.message.reply_text("You are not the designated opponent for this bet.")
            return

        if bet["challenger_id"] == user.id:
            await update.message.reply_text("You cannot accept your own challenge.")
            return

        challenger_bal = get_balance(conn, bet["challenger_id"])
        opponent_bal = get_balance(conn, user.id)

        if challenger_bal < bet["amount"] or opponent_bal < bet["amount"]:
            await update.message.reply_text("One or both users lack sufficient point balances.")
            return

        tax_percent = int(get_config_val(conn, "tax_percent") or "0")
        conn.execute("UPDATE bets SET status='accepted', opponent_id=? WHERE bet_id=?", (user.id, bet_id))
        conn.commit()

    dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=bet["emoji"])
    rolled_value = dice_msg.dice.value
    outcome = "even" if rolled_value % 2 == 0 else "odd"

    winner_id = bet["challenger_id"] if outcome == bet["prediction"] else user.id
    loser_id = user.id if winner_id == bet["challenger_id"] else bet["challenger_id"]

    raw_amount = bet["amount"]
    tax_amount = int(raw_amount * (tax_percent / 100))
    payout = raw_amount - tax_amount

    with closing(get_conn()) as conn:
        adjust_balance(conn, winner_id, payout)
        adjust_balance(conn, loser_id, -raw_amount)
        conn.execute("UPDATE bets SET status='resolved', winner_id=? WHERE bet_id=?", (winner_id, bet_id))
        conn.commit()

    await update.message.reply_text(
        f"🎯 *Outcome Rolled:* {rolled_value} ({outcome.upper()})\n"
        f"🏆 <a href='tg://user?id={winner_id}'>Winner</a> takes {payout} pts (Platform Tax Applied: {tax_percent}%).",
        parse_mode="HTML"
    )


# ---------------------------------------------------------------------------
# Admin Panel & System Control Module
# ---------------------------------------------------------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized access.")
        return

    keyboard = [
        [InlineKeyboardButton("⚙️ Set Global Tax Rate", callback_data="admin_tax")],
        [InlineKeyboardButton("🔨 Ban / Unban User", callback_data="admin_ban")],
        [InlineKeyboardButton("💳 Update Payment Details", callback_data="admin_gateways")],
        [InlineKeyboardButton("📑 Process Financial Queue", callback_data="admin_txs")],
    ]
    await update.message.reply_text("🔧 *Admin Operations Console*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        return

    await query.answer()

    if query.data == "admin_tax":
        await query.message.reply_text("Provide new global platform tax percentage (0 to 100):", reply_markup=ForceReply(selective=True))
        return SET_TAX_STATE

    elif query.data == "admin_ban":
        await query.message.reply_text("Provide target Telegram User ID to flip ban state:", reply_markup=ForceReply(selective=True))
        return BAN_USER_STATE

    elif query.data == "admin_gateways":
        keyboard = [
            [InlineKeyboardButton("Set UPI ID", callback_data="set_gateway_upi")],
            [InlineKeyboardButton("Set USDT Address", callback_data="set_gateway_usdt")],
        ]
        await query.message.reply_text("Select payment gateway configuration parameter to change:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "set_gateway_upi":
        await query.message.reply_text("Enter new default platform UPI ID:", reply_markup=ForceReply(selective=True))
        return SET_UPI_STATE

    elif query.data == "set_gateway_usdt":
        await query.message.reply_text("Enter new default platform USDT BEP-20 receiving address:", reply_markup=ForceReply(selective=True))
        return SET_USDT_STATE

    elif query.data == "admin_txs":
        with closing(get_conn()) as conn:
            txs = conn.execute("SELECT * FROM transactions WHERE status='pending' LIMIT 5").fetchall()

        if not txs:
            await query.message.reply_text("The transaction approval queue is clean.")
            return

        for tx in txs:
            buttons = [
                InlineKeyboardButton("Approve", callback_data=f"tx_app_{tx['tx_id']}"),
                InlineKeyboardButton("Reject", callback_data=f"tx_rej_{tx['tx_id']}"),
            ]
            detail_info = f" | Details: {tx['details']}" if tx['details'] else ""
            await query.message.reply_text(
                f"Tx ID #{tx['tx_id']} | User: {tx['user_id']}\nType: {tx['tx_type'].upper()} ({tx['method'].upper()})\nPoints: {tx['amount']}{detail_info}",
                reply_markup=InlineKeyboardMarkup([buttons]),
            )


async def handle_tx_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        return

    action, tx_id = query.data.split("_")[1:]
    tx_id = int(tx_id)

    with closing(get_conn()) as conn:
        tx = conn.execute("SELECT * FROM transactions WHERE tx_id=?", (tx_id,)).fetchone()
        if not tx or tx["status"] != "pending":
            await query.answer("Transaction state already finalized.")
            return

        if action == "app":
            new_status = "approved"
            if tx["tx_type"] == "deposit":
                adjust_balance(conn, tx["user_id"], tx["amount"])
        else:
            new_status = "rejected"
            if tx["tx_type"] == "withdrawal":
                # Refund reserved balance on rejection
                adjust_balance(conn, tx["user_id"], tx["amount"])

        conn.execute("UPDATE transactions SET status=? WHERE tx_id=?", (new_status, tx_id))
        conn.commit()

    await query.edit_message_text(f"Transaction #{tx_id} updated to status: {new_status.upper()}.")


async def process_tax_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = int(update.message.text.strip())
        if not (0 <= val <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid percentage between 0 and 100.")
        return SET_TAX_STATE

    with closing(get_conn()) as conn:
        conn.execute("UPDATE system_config SET value=? WHERE key='tax_percent'", (str(val),))
        conn.commit()

    await update.message.reply_text(f"Global tax rate adjusted to {val}%.")
    return ConversationHandler.END


async def process_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Please enter a numeric User ID.")
        return BAN_USER_STATE

    with closing(get_conn()) as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
        if row:
            new_state = 0 if row["is_banned"] else 1
            conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (new_state, uid))
            conn.commit()
            status = "UNBANNED" if new_state == 0 else "BANNED"
            await update.message.reply_text(f"User ID {uid} has been set to: {status}.")
        else:
            await update.message.reply_text("Target User ID missing from database record.")

    return ConversationHandler.END


async def process_upi_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    with closing(get_conn()) as conn:
        conn.execute("UPDATE system_config SET value=? WHERE key='upi_id'", (val,))
        conn.commit()
    await update.message.reply_text(f"System UPI address updated to: `{val}`", parse_mode="Markdown")
    return ConversationHandler.END


async def process_usdt_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    with closing(get_conn()) as conn:
        conn.execute("UPDATE system_config SET value=? WHERE key='usdt_bep20_address'", (val,))
        conn.commit()
    await update.message.reply_text(f"System USDT BEP-20 address updated to: `{val}`", parse_mode="Markdown")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Server Runtime Initialization
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def start_health_server():
    server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Fatal Error: BOT_TOKEN environment variable not set.")

    init_db()
    start_health_server()

    app = Application.builder().token(token).build()

    # Conversation Handlers
    dm_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_dm_actions, pattern="^dm_")],
        states={
            DEP_METHOD: [CallbackQueryHandler(process_dep_method, pattern="^dep_method:")],
            DEP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_deposit_amount)],
            WITH_METHOD: [CallbackQueryHandler(process_with_method, pattern="^with_method:")],
            WITH_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_with_amount)],
            WITH_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_with_address)],
        },
        fallbacks=[],
    )

    bet_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("bet", bet_start)],
        states={
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount)],
            ASK_GAME: [CallbackQueryHandler(ask_game, pattern="^game:")],
            ASK_PREDICTION: [CallbackQueryHandler(ask_prediction, pattern="^pred:")],
        },
        fallbacks=[],
    )

    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_callback, pattern="^admin_"),
            CallbackQueryHandler(admin_callback, pattern="^set_gateway_"),
        ],
        states={
            SET_TAX_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_tax_set)],
            BAN_USER_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_ban_user)],
            SET_UPI_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_upi_set)],
            SET_USDT_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_usdt_set)],
        },
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("accept", accept_cmd))
    app.add_handler(CallbackQueryHandler(handle_tx_approval, pattern="^tx_"))

    app.add_handler(dm_conv)
    app.add_handler(bet_conv_handler)
    app.add_handler(admin_conv)

    logger.info("Bot successfully initialized.")
    app.run_polling()


if __name__ == "__main__":
    main()
            """
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO system_config (key, value) VALUES ('tax_percent', '0')"
        )
        conn.commit()


def ensure_user(conn, chat_id, user_id, username):
    row = conn.execute(
        "SELECT * FROM users WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users (chat_id, user_id, username, balance) VALUES (?, ?, ?, ?)",
            (chat_id, user_id, username, STARTING_BALANCE),
        )
        conn.commit()
        return STARTING_BALANCE, 0
    if username and row["username"] != username:
        conn.execute(
            "UPDATE users SET username=? WHERE chat_id=? AND user_id=?",
            (username, chat_id, user_id),
        )
        conn.commit()
    return row["balance"], row["is_banned"]


def is_user_banned(conn, user_id):
    row = conn.execute(
        "SELECT is_banned FROM users WHERE user_id=? AND is_banned=1", (user_id,)
    ).fetchone()
    return row is not None


def get_balance(conn, chat_id, user_id):
    row = conn.execute(
        "SELECT balance FROM users WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    return row["balance"] if row else None


def adjust_balance(conn, chat_id, user_id, delta):
    conn.execute(
        "UPDATE users SET balance = balance + ? WHERE chat_id=? AND user_id=?",
        (delta, chat_id, user_id),
    )
    conn.commit()


def find_user_id_by_username(conn, chat_id, username):
    username = username.lstrip("@")
    row = conn.execute(
        "SELECT user_id FROM users WHERE chat_id=? AND username=? COLLATE NOCASE",
        (chat_id, username),
    ).fetchone()
    return row["user_id"] if row else None


def get_tax_percent(conn):
    row = conn.execute("SELECT value FROM system_config WHERE key='tax_percent'").fetchone()
    return int(row["value"]) if row else 0


# ---------------------------------------------------------------------------
# Middlewares & Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    with closing(get_conn()) as conn:
        if is_user_banned(conn, user.id):
            await update.message.reply_text("🚫 You are banned from using this bot.")
            return
        ensure_user(conn, chat.id, user.id, user.username)

    if chat.type == "private":
        await show_dm_dashboard(update, context)
    else:
        await update.message.reply_text(
            "Welcome! Everyone starts with 1000 points.\n"
            "Use /help to see how to challenge other players."
        )


async def show_dm_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with closing(get_conn()) as conn:
        balance = get_balance(conn, user.id, user.id) or 0
        txs = conn.execute(
            "SELECT tx_type, amount, status FROM transactions WHERE user_id=? ORDER BY tx_id DESC LIMIT 5",
            (user.id,),
        ).fetchall()

    history_text = "\n".join([f"• {t['tx_type'].title()}: {t['amount']} pts [{t['status']}]" for t in txs]) if txs else "No transactions found."

    text = (
        f"👤 *Player Dashboard*\n\n"
        f"💰 *Wallet Balance:* {balance} pts\n\n"
        f"📜 *Recent Transaction History:*\n{history_text}"
    )

    keyboard = [
        [
            InlineKeyboardButton("📥 Deposit", callback_data="dm_deposit"),
            InlineKeyboardButton("📤 Withdraw", callback_data="dm_withdraw"),
        ],
        [InlineKeyboardButton("🎮 Play in Group", url=GROUP_LINK)],
    ]

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )


# ---------------------------------------------------------------------------
# DM Financial Transactions Flow
# ---------------------------------------------------------------------------

async def handle_dm_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "dm_deposit":
        await query.message.reply_text("Enter the amount of points you wish to deposit:", reply_markup=ForceReply(selective=True))
        return DEP_AMOUNT
    elif query.data == "dm_withdraw":
        await query.message.reply_text("Enter the amount of points you wish to withdraw:", reply_markup=ForceReply(selective=True))
        return WITH_AMOUNT


async def process_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        amount = int(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid positive number.")
        return DEP_AMOUNT

    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO transactions (user_id, tx_type, amount, created_at) VALUES (?, 'deposit', ?, ?)",
            (user.id, amount, datetime.utcnow().isoformat()),
        )
        conn.commit()

    await update.message.reply_text("📥 Deposit request submitted for admin review.")
    await show_dm_dashboard(update, context)
    return ConversationHandler.END


async def process_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        amount = int(update.message.text.strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid positive number.")
        return WITH_AMOUNT

    with closing(get_conn()) as conn:
        balance = get_balance(conn, user.id, user.id) or 0
        if balance < amount:
            await update.message.reply_text("Insufficient wallet balance.")
            return ConversationHandler.END

        conn.execute(
            "INSERT INTO transactions (user_id, tx_type, amount, created_at) VALUES (?, 'withdrawal', ?, ?)",
            (user.id, amount, datetime.utcnow().isoformat()),
        )
        conn.commit()

    await update.message.reply_text("📤 Withdrawal request submitted for admin review.")
    await show_dm_dashboard(update, context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Group Betting Logic
# ---------------------------------------------------------------------------

async def bet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("⚠️ Betting is only allowed inside group chats!")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    challenger = update.effective_user

    with closing(get_conn()) as conn:
        if is_user_banned(conn, challenger.id):
            await update.message.reply_text("You are banned from participating in bets.")
            return ConversationHandler.END
        ensure_user(conn, chat_id, challenger.id, challenger.username)

    args = context.args or []
    opponent_id, opponent_username, opponent_display = None, None, None
    remaining_args = args

    if args and args[0].startswith("@"):
        opponent_username = args[0].lstrip("@")
        with closing(get_conn()) as conn:
            opponent_id = find_user_id_by_username(conn, chat_id, opponent_username)
        opponent_display = f"@{opponent_username}"
        remaining_args = args[1:]
    elif update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied = update.message.reply_to_message.from_user
        if replied.is_bot or replied.id == challenger.id:
            await update.message.reply_text("Invalid opponent.")
            return ConversationHandler.END
        opponent_id = replied.id
        opponent_username = replied.username
        opponent_display = f"@{replied.username}" if replied.username else replied.first_name
        with closing(get_conn()) as conn:
            ensure_user(conn, chat_id, replied.id, replied.username)
        remaining_args = args
    else:
        await update.message.reply_text("Tag a valid opponent or reply to their message with /bet")
        return ConversationHandler.END

    context.user_data["bet_challenge"] = {
        "opponent_id": opponent_id,
        "opponent_username": opponent_username,
        "opponent_display": opponent_display,
        "challenger_id": challenger.id,
    }

    if len(remaining_args) >= 2:
        try:
            amount = int(remaining_args[0])
            if amount > 0:
                emoji = "🎲"
                prediction = None
                for a in remaining_args[1:]:
                    if a in VALID_EMOJIS:
                        emoji = a
                    elif a.lower() in ("even", "odd"):
                        prediction = a.lower()
                if prediction:
                    await _create_bet_and_announce(update, context, amount, emoji, prediction)
                    context.user_data.pop("bet_challenge", None)
                    return ConversationHandler.END
        except ValueError:
            pass

    await update.message.reply_text("How many points do you want to bet?", reply_markup=ForceReply(selective=True))
    return ASK_AMOUNT


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int((update.message.text or "").strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a positive number.", reply_markup=ForceReply(selective=True))
        return ASK_AMOUNT

    chat_id = update.effective_chat.id
    challenger = update.effective_user
    with closing(get_conn()) as conn:
        balance = get_balance(conn, chat_id, challenger.id)

    if balance is None or balance < amount:
        await update.message.reply_text(f"You only have {balance or 0} points.", reply_markup=ForceReply(selective=True))
        return ASK_AMOUNT

    context.user_data["bet_challenge"]["amount"] = amount
    keyboard = [[InlineKeyboardButton(f"{emoji} {label}", callback_data=f"game:{emoji}")] for emoji, label in GAME_EMOJI_LABELS.items()]
    await update.message.reply_text("Pick a game:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_GAME


async def ask_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emoji = query.data.split(":", 1)[1]
    context.user_data.setdefault("bet_challenge", {})["emoji"] = emoji
    keyboard = [[InlineKeyboardButton("Even", callback_data="pred:even"), InlineKeyboardButton("Odd", callback_data="pred:odd")]]
    await query.edit_message_text(f"Game: {emoji}\nPick prediction:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_PREDICTION


async def ask_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prediction = query.data.split(":", 1)[1]
    data = context.user_data.get("bet_challenge", {})
    amount, emoji = data.get("amount"), data.get("emoji")

    if amount is None or emoji is None:
        await query.edit_message_text("Cancelled due to missing parameters.")
        context.user_data.pop("bet_challenge", None)
        return ConversationHandler.END

    await query.edit_message_text("Creating bet...")
    await _create_bet_and_announce(update, context, amount, emoji, prediction, via_callback=True)
    context.user_data.pop("bet_challenge", None)
    return ConversationHandler.END


async def _create_bet_and_announce(update, context, amount, emoji, prediction, via_callback=False):
    chat_id = update.effective_chat.id
    challenger = update.effective_user
    data = context.user_data.get("bet_challenge", {})

    with closing(get_conn()) as conn:
        conn.execute(
            """
            INSERT INTO bets (chat_id, challenger_id, challenger_name, opponent_id, opponent_name, amount, emoji, prediction, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, challenger.id, challenger.username or challenger.first_name, data.get("opponent_id"), data.get("opponent_username"), amount, emoji, prediction, datetime.utcnow().isoformat()),
        )
        conn.commit()
        bet_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    text = f"🎲 Bet #{bet_id} created!\n{challenger.first_name} vs {data.get('opponent_display')} for {amount} pts."
    sent = await context.bot.send_message(chat_id, text)

    with closing(get_conn()) as conn:
        conn.execute("UPDATE bets SET challenge_message_id=? WHERE bet_id=?", (sent.message_id, bet_id))
        conn.commit()


async def accept_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("⚠️ Accept bets within groups only!")
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    bet_id = None

    if context.args:
        try:
            bet_id = int(context.args[0])
        except ValueError:
            return
    elif update.message.reply_to_message:
        with closing(get_conn()) as conn:
            row = conn.execute("SELECT bet_id FROM bets WHERE chat_id=? AND challenge_message_id=? AND status='pending'", (chat_id, update.message.reply_to_message.message_id)).fetchone()
            if row:
                bet_id = row["bet_id"]

    if not bet_id:
        return

    with closing(get_conn()) as conn:
        if is_user_banned(conn, user.id):
            await update.message.reply_text("You are banned.")
            return

        bet = conn.execute("SELECT * FROM bets WHERE bet_id=? AND chat_id=?", (bet_id, chat_id)).fetchone()
        if not bet or bet["status"] != "pending" or bet["challenger_id"] == user.id:
            await update.message.reply_text("Invalid bet or operation.")
            return

        tax_percent = get_tax_percent(conn)
        conn.execute("UPDATE bets SET status='accepted', opponent_id=? WHERE bet_id=?", (user.id, bet_id))
        conn.commit()

    dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=bet["emoji"])
    rolled_value = dice_msg.dice.value
    outcome = "even" if rolled_value % 2 == 0 else "odd"

    winner_id = bet["challenger_id"] if outcome == bet["prediction"] else user.id
    loser_id = user.id if winner_id == bet["challenger_id"] else bet["challenger_id"]

    raw_amount = bet["amount"]
    tax_amount = int(raw_amount * (tax_percent / 100))
    payout = raw_amount - tax_amount

    with closing(get_conn()) as conn:
        adjust_balance(conn, chat_id, winner_id, payout)
        adjust_balance(conn, chat_id, loser_id, -raw_amount)
        conn.execute("UPDATE bets SET status='resolved', winner_id=? WHERE bet_id=?", (winner_id, bet_id))
        conn.commit()

    await update.message.reply_text(f"🎯 Result: Rolled {rolled_value} ({outcome.upper()})!\n🏆 Winner receives {payout} pts (Tax applied: {tax_percent}%).")


# ---------------------------------------------------------------------------
# Admin Panel & Management
# ---------------------------------------------------------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized access.")
        return

    keyboard = [
        [InlineKeyboardButton("⚙️ Set Global Tax", callback_data="admin_tax")],
        [InlineKeyboardButton("🔨 Ban/Unban User", callback_data="admin_ban")],
        [InlineKeyboardButton("💳 Approve Transactions", callback_data="admin_txs")],
    ]
    await update.message.reply_text("🔧 *Admin Control Panel*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        return

    await query.answer()

    if query.data == "admin_tax":
        await query.message.reply_text("Send the new global tax percentage (0-100):", reply_markup=ForceReply(selective=True))
        return SET_TAX_STATE
    elif query.data == "admin_ban":
        await query.message.reply_text("Send the User ID to Ban/Unban:", reply_markup=ForceReply(selective=True))
        return BAN_USER_STATE
    elif query.data == "admin_txs":
        with closing(get_conn()) as conn:
            txs = conn.execute("SELECT * FROM transactions WHERE status='pending' LIMIT 5").fetchall()

        if not txs:
            await query.message.reply_text("No pending transactions.")
            return

        for tx in txs:
            buttons = [
                InlineKeyboardButton("Approve", callback_data=f"tx_app_{tx['tx_id']}"),
                InlineKeyboardButton("Reject", callback_data=f"tx_rej_{tx['tx_id']}"),
            ]
            await query.message.reply_text(
                f"Tx #{tx['tx_id']} | User: {tx['user_id']} | Type: {tx['tx_type']} | Amount: {tx['amount']}",
                reply_markup=InlineKeyboardMarkup([buttons]),
            )


async def handle_tx_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        return

    action, tx_id = query.data.split("_")[1:]
    tx_id = int(tx_id)

    with closing(get_conn()) as conn:
        tx = conn.execute("SELECT * FROM transactions WHERE tx_id=?", (tx_id,)).fetchone()
        if not tx or tx["status"] != "pending":
            await query.answer("Transaction already processed.")
            return

        if action == "app":
            new_status = "approved"
            delta = tx["amount"] if tx["tx_type"] == "deposit" else -tx["amount"]
            adjust_balance(conn, tx["user_id"], tx["user_id"], delta)
        else:
            new_status = "rejected"

        conn.execute("UPDATE transactions SET status=? WHERE tx_id=?", (new_status, tx_id))
        conn.commit()

    await query.edit_message_text(f"Transaction #{tx_id} marked as {new_status}.")


async def process_tax_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = int(update.message.text.strip())
        if not (0 <= val <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Provide a valid percentage (0-100).")
        return SET_TAX_STATE

    with closing(get_conn()) as conn:
        conn.execute("UPDATE system_config SET value=? WHERE key='tax_percent'", (str(val),))
        conn.commit()

    await update.message.reply_text(f"Global tax updated to {val}%.")
    return ConversationHandler.END


async def process_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Provide a valid integer User ID.")
        return BAN_USER_STATE

    with closing(get_conn()) as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
        if row:
            new_state = 0 if row["is_banned"] else 1
            conn.execute("UPDATE users SET is_banned=? WHERE user_id=?", (new_state, uid))
            conn.commit()
            status = "unbanned" if new_state == 0 else "banned"
            await update.message.reply_text(f"User {uid} status updated to: {status}.")
        else:
            await update.message.reply_text("User ID not found.")

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Health Server & Main
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")


def start_health_server():
    server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN missing.")

    init_db()
    start_health_server()

    app = Application.builder().token(token).build()

    # DM Flow Setup
    dm_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(handle_dm_actions, pattern="^dm_")],
        states={
            DEP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_deposit)],
            WITH_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_withdrawal)],
        },
        fallbacks=[],
    )

    # Bet Flow Setup
    bet_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("bet", bet_start)],
        states={
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount)],
            ASK_GAME: [CallbackQueryHandler(ask_game, pattern="^game:")],
            ASK_PREDICTION: [CallbackQueryHandler(ask_prediction, pattern="^pred:")],
        },
        fallbacks=[],
    )

    # Admin Flow Setup
    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^admin_")],
        states={
            SET_TAX_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_tax_set)],
            BAN_USER_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_ban_user)],
        },
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("accept", accept_cmd))
    app.add_handler(CallbackQueryHandler(handle_tx_approval, pattern="^tx_"))
    app.add_handler(dm_conv)
    app.add_handler(bet_conv_handler)
    app.add_handler(admin_conv)

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
