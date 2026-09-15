"""
Player-vs-Player Betting Bot for Telegram with Native Dice & Automatic Settlement
=================================================================================

ARCHITECTURE (v2)
-----------------
The bot now behaves DIFFERENTLY in groups vs. in private chat (DM):

  GROUP CHATS
    - This is the only place players can actually play/bet.
    - /bet, /accept, /cancel, /mybets, /leaderboard all work here.
    - Balance shown here is the player's GLOBAL wallet balance (not per-group).

  PRIVATE CHAT (DM with the bot)
    - No betting happens here.
    - Any message (or /start, /wallet) shows the player's WALLET CARD:
        current balance, recent deposit history, recent withdrawal history,
        and an inline "▶️ Play in <Group>" button that opens the group the
        player last played in.
    - /deposit <amount> and /withdraw <amount> create pending requests that
      admins approve/reject from their own DM.

  ADMIN PANEL (DM only, admin user IDs from the ADMIN_IDS env var)
    - /admin            -> menu of admin actions
    - /addbalance       -> credit a user's wallet
    - /removebalance    -> debit a user's wallet
    - /ban / /unban     -> ban/unban a user from playing & deposits/withdrawals
    - /settax <percent> -> set the house tax % taken from bet winnings
    - /deposits         -> list & approve/reject pending deposit requests
    - /withdrawals      -> list & approve/reject pending withdrawal requests

DATABASE (SQLite, see init_db() for full schema)
    users               - one row per Telegram user, GLOBAL wallet balance
    groups              - one row per group the bot has been used in
    bets                - one row per bet (unchanged concept from v1)
    transactions        - full ledger: deposits, withdrawals, bet wins/losses,
                          tax, admin credits/debits (this is the audit trail)
    deposit_requests    - pending/approved/rejected deposit requests
    withdrawal_requests - pending/approved/rejected withdrawal requests
    bans                - banned user ids + reason + who banned them
    settings            - key/value store (currently just tax_percent)

NOTE ON GROUP PRIVACY MODE:
Telegram bots only receive plain text messages in groups if privacy mode is
disabled for the bot, OR the message is a reply to the bot / mentions the bot.
The amount-prompt step below uses ForceReply so the user's answer is always a
reply to the bot, which guarantees delivery even with default privacy mode on.
"""

import logging
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "bet_bot.db")
STARTING_BALANCE = int(os.environ.get("STARTING_BALANCE", "1000"))
DEFAULT_TAX_PERCENT = float(os.environ.get("DEFAULT_TAX_PERCENT", "5"))

# Comma separated Telegram user ids, e.g. "111111111,222222222"
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x
}

# Supported Telegram animated dice emojis, with display labels for buttons
GAME_EMOJI_LABELS = {
    "🎲": "Dice",
    "🎯": "Darts",
    "🎳": "Bowling",
    "🏀": "Basketball",
    "⚽": "Football",
    "🎰": "Slots",
}
VALID_EMOJIS = set(GAME_EMOJI_LABELS.keys())

# Conversation states for the /bet flow
ASK_AMOUNT, ASK_GAME, ASK_PREDICTION = range(3)


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(get_conn()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                last_group_id INTEGER,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                username TEXT,
                invite_link TEXT,
                added_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bets (
                bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                challenger_id INTEGER NOT NULL,
                challenger_name TEXT,
                opponent_id INTEGER,
                opponent_name TEXT,
                amount INTEGER NOT NULL,
                emoji TEXT NOT NULL DEFAULT '🎲',
                prediction TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                winner_id INTEGER,
                tax_amount INTEGER NOT NULL DEFAULT 0,
                challenge_message_id INTEGER,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                tx_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                note TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deposit_requests (
                req_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT,
                handled_at TEXT,
                handled_by INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                req_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT,
                handled_at TEXT,
                handled_by INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bans (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                banned_by INTEGER,
                banned_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.commit()

        # Seed default tax setting if missing
        row = conn.execute("SELECT value FROM settings WHERE key='tax_percent'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('tax_percent', ?)",
                (str(DEFAULT_TAX_PERCENT),),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_tax_percent(conn):
    row = conn.execute("SELECT value FROM settings WHERE key='tax_percent'").fetchone()
    return float(row["value"]) if row else DEFAULT_TAX_PERCENT


def set_tax_percent(conn, percent):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('tax_percent', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(percent),),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# User / wallet helpers  (GLOBAL balance, not per-chat)
# ---------------------------------------------------------------------------

def ensure_user(conn, user_id, username=None, first_name=None):
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users (user_id, username, first_name, balance, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, first_name, STARTING_BALANCE, datetime.utcnow().isoformat()),
        )
        conn.commit()
        log_transaction(conn, user_id, "signup_bonus", STARTING_BALANCE, "Starting balance")
        return STARTING_BALANCE

    updates, params = [], []
    if username and row["username"] != username:
        updates.append("username=?")
        params.append(username)
    if first_name and row["first_name"] != first_name:
        updates.append("first_name=?")
        params.append(first_name)
    if updates:
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id=?", params)
        conn.commit()
    return row["balance"]


def get_balance(conn, user_id):
    row = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row["balance"] if row else None


def adjust_balance(conn, user_id, delta, tx_type, note=None):
    """Adjust a user's global balance and record a ledger entry. Returns new balance."""
    conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))
    conn.commit()
    new_balance = get_balance(conn, user_id)
    log_transaction(conn, user_id, tx_type, delta, note, balance_after=new_balance)
    return new_balance


def log_transaction(conn, user_id, tx_type, amount, note=None, balance_after=None):
    if balance_after is None:
        balance_after = get_balance(conn, user_id) or 0
    conn.execute(
        "INSERT INTO transactions (user_id, type, amount, balance_after, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, tx_type, amount, balance_after, note, datetime.utcnow().isoformat()),
    )
    conn.commit()


def find_user_id_by_username(conn, username):
    username = username.lstrip("@")
    row = conn.execute(
        "SELECT user_id FROM users WHERE username=? COLLATE NOCASE", (username,)
    ).fetchone()
    return row["user_id"] if row else None


def is_banned(conn, user_id):
    row = conn.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,)).fetchone()
    return row is not None


def ban_user(conn, user_id, reason, banned_by):
    conn.execute(
        "INSERT INTO bans (user_id, reason, banned_by, banned_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason, "
        "banned_by=excluded.banned_by, banned_at=excluded.banned_at",
        (user_id, reason, banned_by, datetime.utcnow().isoformat()),
    )
    conn.commit()


def unban_user(conn, user_id):
    conn.execute("DELETE FROM bans WHERE user_id=?", (user_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Group helpers
# ---------------------------------------------------------------------------

def register_group(conn, chat):
    conn.execute(
        "INSERT INTO groups (chat_id, title, username, added_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username",
        (chat.id, chat.title, chat.username, datetime.utcnow().isoformat()),
    )
    conn.commit()


def set_group_invite_link(conn, chat_id, invite_link):
    conn.execute("UPDATE groups SET invite_link=? WHERE chat_id=?", (invite_link, chat_id))
    conn.commit()


def touch_last_group(conn, user_id, chat_id):
    conn.execute("UPDATE users SET last_group_id=? WHERE user_id=?", (chat_id, user_id))
    conn.commit()


async def get_group_play_button(conn, context, chat_id):
    """Best-effort inline button that opens the given group. Returns (button, group_title)."""
    if chat_id is None:
        return None, None
    row = conn.execute("SELECT * FROM groups WHERE chat_id=?", (chat_id,)).fetchone()
    if row is None:
        return None, None

    url = None
    if row["username"]:
        url = f"https://t.me/{row['username']}"
    elif row["invite_link"]:
        url = row["invite_link"]
    else:
        try:
            link = await context.bot.export_chat_invite_link(chat_id)
            set_group_invite_link(conn, chat_id, link)
            url = link
        except Exception:
            url = None

    title = row["title"] or "the group"
    if url:
        return InlineKeyboardButton(f"▶️ Play in {title}", url=url), title
    return None, title


# ---------------------------------------------------------------------------
# Access-control decorators
# ---------------------------------------------------------------------------

def is_admin(user_id):
    return user_id in ADMIN_IDS


def group_only(handler):
    """Wrap a handler so it only runs in group chats; in DM it explains & redirects."""
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type == "private":
            with closing(get_conn()) as conn:
                ensure_user(conn, update.effective_user.id, update.effective_user.username,
                            update.effective_user.first_name)
                last_group = conn.execute(
                    "SELECT last_group_id FROM users WHERE user_id=?",
                    (update.effective_user.id,),
                ).fetchone()["last_group_id"]
                button, title = await get_group_play_button(conn, context, last_group)
            if button:
                await update.message.reply_text(
                    "🎮 Betting only happens in the group chat, not here.",
                    reply_markup=InlineKeyboardMarkup([[button]]),
                )
            else:
                await update.message.reply_text(
                    "🎮 Betting only happens in a group chat. Add me to a group and play there!"
                )
            return
        return await handler(update, context)
    return wrapped


def admin_only(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ This command is for admins only.")
            return
        return await handler(update, context)
    return wrapped


def not_banned(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        with closing(get_conn()) as conn:
            if is_banned(conn, update.effective_user.id):
                await update.message.reply_text("⛔ You are banned from using this bot.")
                return
        return await handler(update, context)
    return wrapped


# ---------------------------------------------------------------------------
# Basic command handlers (behave differently in group vs DM)
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)
        if chat.type == "private":
            await send_wallet_card(update, context, conn)
            return
        register_group(conn, chat)
        touch_last_group(conn, user.id, chat.id)

    await update.message.reply_text(
        f"Welcome! Everyone starts with {STARTING_BALANCE} points.\n"
        "Use /help to see how to challenge other players.\n"
        "DM me anytime to check your wallet, deposits, and withdrawals."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        text = (
            "*Wallet commands (DM only)*\n"
            "/wallet - view balance, deposit & withdrawal history\n"
            "/deposit <amount> - request a deposit (admin approves)\n"
            "/withdraw <amount> - request a withdrawal (admin approves)\n\n"
            "To actually *play*, open the group chat and use /bet there."
        )
        if is_admin(user_id=update.effective_user.id):
            text += (
                "\n\n*Admin panel*\n"
                "/admin - admin menu\n"
                "/addbalance @user <amount>\n"
                "/removebalance @user <amount>\n"
                "/ban @user [reason]\n"
                "/unban @user\n"
                "/settax <percent>\n"
                "/deposits - review pending deposits\n"
                "/withdrawals - review pending withdrawals"
            )
    else:
        text = (
            "*Commands (this group)*\n"
            "/bet @user - or reply to someone's message with /bet - start a challenge\n"
            "  _You'll be asked for the amount, then the game, then even/odd via buttons_\n"
            "  _Fast path: /bet @alice 100 🎲 even_\n"
            "/accept - reply to the bot's challenge message with /accept to take it\n"
            "/accept <bet_id> - or accept by id directly\n"
            "/cancel <bet_id> - cancel your unaccepted bet\n"
            "/mybets - list your open/pending bets\n"
            "/balance - quick balance check\n"
            "/leaderboard - top balances in this chat\n\n"
            "Supported games: 🎲 Dice, 🎯 Darts, 🎳 Bowling, 🏀 Basketball, ⚽ Football, 🎰 Slots\n\n"
            "DM me to see your full wallet & deposit/withdraw."
        )
    await update.message.reply_text(text, parse_mode="Markdown")


# The is_admin() helper collides in name with the keyword usage above; give help_cmd
# its own tiny wrapper to avoid confusion when called positionally.
def _is_admin_kw(user_id):
    return is_admin(user_id)


# ---------------------------------------------------------------------------
# DM: wallet card, deposit/withdraw requests
# ---------------------------------------------------------------------------

async def send_wallet_card(update: Update, context: ContextTypes.DEFAULT_TYPE, conn):
    user = update.effective_user
    balance = ensure_user(conn, user.id, user.username, user.first_name)

    deposits = conn.execute(
        "SELECT amount, status, created_at FROM deposit_requests WHERE user_id=? "
        "ORDER BY req_id DESC LIMIT 5",
        (user.id,),
    ).fetchall()
    withdrawals = conn.execute(
        "SELECT amount, status, created_at FROM withdrawal_requests WHERE user_id=? "
        "ORDER BY req_id DESC LIMIT 5",
        (user.id,),
    ).fetchall()

    lines = [f"💰 *Your Wallet*", f"Balance: *{balance}* points", ""]

    lines.append("📥 *Recent deposits*")
    if deposits:
        for d in deposits:
            lines.append(f"  {d['created_at'][:10]} - {d['amount']} pts [{d['status']}]")
    else:
        lines.append("  none yet")

    lines.append("")
    lines.append("📤 *Recent withdrawals*")
    if withdrawals:
        for w in withdrawals:
            lines.append(f"  {w['created_at'][:10]} - {w['amount']} pts [{w['status']}]")
    else:
        lines.append("  none yet")

    lines.append("")
    lines.append("_Use /deposit <amount> or /withdraw <amount> to request a change._")

    row = conn.execute("SELECT last_group_id FROM users WHERE user_id=?", (user.id,)).fetchone()
    button, _ = await get_group_play_button(conn, context, row["last_group_id"] if row else None)
    keyboard = InlineKeyboardMarkup([[button]]) if button else None

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please DM me and send /wallet to see your wallet 🙂")
        return
    with closing(get_conn()) as conn:
        await send_wallet_card(update, context, conn)


async def dm_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any plain text sent to the bot in DM just shows the wallet card."""
    with closing(get_conn()) as conn:
        await send_wallet_card(update, context, conn)


@not_banned
async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please DM me to request a deposit.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /deposit <amount>")
        return
    try:
        amount = int(context.args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Amount must be a positive whole number.")
        return

    user = update.effective_user
    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)
        conn.execute(
            "INSERT INTO deposit_requests (user_id, amount, created_at) VALUES (?, ?, ?)",
            (user.id, amount, datetime.utcnow().isoformat()),
        )
        conn.commit()
        req_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    await update.message.reply_text(
        f"📥 Deposit request #{req_id} for {amount} points submitted. "
        "An admin will review it shortly."
    )

    tag = f"@{user.username}" if user.username else user.first_name
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"📥 New deposit request #{req_id}\nUser: {tag} ({user.id})\nAmount: {amount} pts",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"dep:approve:{req_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"dep:reject:{req_id}"),
                ]]),
            )
        except Exception:
            logger.warning("Could not notify admin %s", admin_id)


@not_banned
async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please DM me to request a withdrawal.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /withdraw <amount>")
        return
    try:
        amount = int(context.args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Amount must be a positive whole number.")
        return

    user = update.effective_user
    with closing(get_conn()) as conn:
        balance = ensure_user(conn, user.id, user.username, user.first_name)
        if balance < amount:
            await update.message.reply_text(f"You only have {balance} points.")
            return
        conn.execute(
            "INSERT INTO withdrawal_requests (user_id, amount, created_at) VALUES (?, ?, ?)",
            (user.id, amount, datetime.utcnow().isoformat()),
        )
        conn.commit()
        req_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    await update.message.reply_text(
        f"📤 Withdrawal request #{req_id} for {amount} points submitted. "
        "Your balance is unaffected until an admin approves it."
    )

    tag = f"@{user.username}" if user.username else user.first_name
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"📤 New withdrawal request #{req_id}\nUser: {tag} ({user.id})\nAmount: {amount} pts",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"wd:approve:{req_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"wd:reject:{req_id}"),
                ]]),
            )
        except Exception:
            logger.warning("Could not notify admin %s", admin_id)


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

@admin_only
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(get_conn()) as conn:
        tax = get_tax_percent(conn)
        pending_dep = conn.execute(
            "SELECT COUNT(*) AS c FROM deposit_requests WHERE status='pending'"
        ).fetchone()["c"]
        pending_wd = conn.execute(
            "SELECT COUNT(*) AS c FROM withdrawal_requests WHERE status='pending'"
        ).fetchone()["c"]

    text = (
        "🛠 *Admin Panel*\n"
        f"House tax on bet wins: *{tax}%*\n"
        f"Pending deposits: *{pending_dep}*\n"
        f"Pending withdrawals: *{pending_wd}*\n\n"
        "/addbalance @user <amount>\n"
        "/removebalance @user <amount>\n"
        "/ban @user [reason]\n"
        "/unban @user\n"
        "/settax <percent>\n"
        "/deposits\n"
        "/withdrawals"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Pending deposits", callback_data="admin:deposits")],
        [InlineKeyboardButton("📤 Pending withdrawals", callback_data="admin:withdrawals")],
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)


def _parse_user_and_amount(conn, args):
    """Parses '@user <amount>' style admin args. Returns (user_id, tag, amount) or (None, msg, None)."""
    if len(args) < 2:
        return None, "Usage: @user <amount>", None
    target = args[0]
    try:
        amount = int(args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        return None, "Amount must be a positive whole number.", None

    if target.startswith("@"):
        user_id = find_user_id_by_username(conn, target)
        if user_id is None:
            return None, f"I don't know {target} yet - they must /start the bot first.", None
    else:
        try:
            user_id = int(target)
        except ValueError:
            return None, "Give a @username or a numeric user id.", None
    return user_id, target, amount


@admin_only
async def addbalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(get_conn()) as conn:
        user_id, tag, amount = _parse_user_and_amount(conn, context.args)
        if user_id is None:
            await update.message.reply_text(tag)
            return
        ensure_user(conn, user_id)
        new_balance = adjust_balance(conn, user_id, amount, "admin_credit",
                                      note=f"credited by admin {update.effective_user.id}")
    await update.message.reply_text(f"✅ Credited {amount} pts to {tag}. New balance: {new_balance}.")
    try:
        await context.bot.send_message(
            user_id, f"💰 An admin credited your wallet with {amount} points. New balance: {new_balance}."
        )
    except Exception:
        pass


@admin_only
async def removebalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(get_conn()) as conn:
        user_id, tag, amount = _parse_user_and_amount(conn, context.args)
        if user_id is None:
            await update.message.reply_text(tag)
            return
        ensure_user(conn, user_id)
        new_balance = adjust_balance(conn, user_id, -amount, "admin_debit",
                                      note=f"debited by admin {update.effective_user.id}")
    await update.message.reply_text(f"✅ Debited {amount} pts from {tag}. New balance: {new_balance}.")
    try:
        await context.bot.send_message(
            user_id, f"⚠️ An admin debited {amount} points from your wallet. New balance: {new_balance}."
        )
    except Exception:
        pass


@admin_only
async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /ban @user [reason]")
        return
    target = context.args[0]
    reason = " ".join(context.args[1:]) or "No reason given"
    with closing(get_conn()) as conn:
        user_id = find_user_id_by_username(conn, target) if target.startswith("@") else int(target)
        if user_id is None:
            await update.message.reply_text(f"I don't know {target} yet.")
            return
        ban_user(conn, user_id, reason, update.effective_user.id)
    await update.message.reply_text(f"⛔ Banned {target}. Reason: {reason}")
    try:
        await context.bot.send_message(user_id, f"⛔ You have been banned from this bot. Reason: {reason}")
    except Exception:
        pass


@admin_only
async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unban @user")
        return
    target = context.args[0]
    with closing(get_conn()) as conn:
        user_id = find_user_id_by_username(conn, target) if target.startswith("@") else int(target)
        if user_id is None:
            await update.message.reply_text(f"I don't know {target} yet.")
            return
        unban_user(conn, user_id)
    await update.message.reply_text(f"✅ Unbanned {target}.")
    try:
        await context.bot.send_message(user_id, "✅ You have been unbanned and can use the bot again.")
    except Exception:
        pass


@admin_only
async def settax_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /settax <percent>  (e.g. /settax 5)")
        return
    try:
        percent = float(context.args[0])
        if percent < 0 or percent > 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Percent must be a number between 0 and 100.")
        return
    with closing(get_conn()) as conn:
        set_tax_percent(conn, percent)
    await update.message.reply_text(f"✅ House tax on bet winnings set to {percent}%.")


def _format_requests(rows, kind):
    if not rows:
        return f"No pending {kind}."
    lines = [f"*Pending {kind}*"]
    for r in rows:
        lines.append(f"#{r['req_id']} - user {r['user_id']} - {r['amount']} pts")
    return "\n".join(lines)


@admin_only
async def deposits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT * FROM deposit_requests WHERE status='pending' ORDER BY req_id"
        ).fetchall()
    if not rows:
        await update.message.reply_text("No pending deposits. 🎉")
        return
    for r in rows:
        await update.message.reply_text(
            f"📥 Deposit #{r['req_id']} - user {r['user_id']} - {r['amount']} pts",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"dep:approve:{r['req_id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"dep:reject:{r['req_id']}"),
            ]]),
        )


@admin_only
async def withdrawals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT * FROM withdrawal_requests WHERE status='pending' ORDER BY req_id"
        ).fetchall()
    if not rows:
        await update.message.reply_text("No pending withdrawals. 🎉")
        return
    for r in rows:
        await update.message.reply_text(
            f"📤 Withdrawal #{r['req_id']} - user {r['user_id']} - {r['amount']} pts",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"wd:approve:{r['req_id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"wd:reject:{r['req_id']}"),
            ]]),
        )


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the two buttons on /admin (deposits / withdrawals shortcuts)."""
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admins only.", show_alert=True)
        return
    await query.answer()
    if query.data == "admin:deposits":
        await deposits_cmd(update, context)
    else:
        await withdrawals_cmd(update, context)


async def deposit_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admins only.", show_alert=True)
        return
    _, action, req_id_str = query.data.split(":")
    req_id = int(req_id_str)

    with closing(get_conn()) as conn:
        req = conn.execute(
            "SELECT * FROM deposit_requests WHERE req_id=?", (req_id,)
        ).fetchone()
        if req is None or req["status"] != "pending":
            await query.answer("Already handled.", show_alert=True)
            return

        if action == "approve":
            new_balance = adjust_balance(conn, req["user_id"], req["amount"], "deposit",
                                          note=f"deposit request #{req_id}")
            conn.execute(
                "UPDATE deposit_requests SET status='approved', handled_at=?, handled_by=? WHERE req_id=?",
                (datetime.utcnow().isoformat(), query.from_user.id, req_id),
            )
            conn.commit()
            user_msg = f"✅ Your deposit request #{req_id} for {req['amount']} pts was approved. New balance: {new_balance}."
        else:
            conn.execute(
                "UPDATE deposit_requests SET status='rejected', handled_at=?, handled_by=? WHERE req_id=?",
                (datetime.utcnow().isoformat(), query.from_user.id, req_id),
            )
            conn.commit()
            user_msg = f"❌ Your deposit request #{req_id} for {req['amount']} pts was rejected."

    await query.answer("Done.")
    await query.edit_message_text(query.message.text + f"\n\n→ {action.upper()} by admin {query.from_user.id}")
    try:
        await context.bot.send_message(req["user_id"], user_msg)
    except Exception:
        pass


async def withdrawal_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("Admins only.", show_alert=True)
        return
    _, action, req_id_str = query.data.split(":")
    req_id = int(req_id_str)

    with closing(get_conn()) as conn:
        req = conn.execute(
            "SELECT * FROM withdrawal_requests WHERE req_id=?", (req_id,)
        ).fetchone()
        if req is None or req["status"] != "pending":
            await query.answer("Already handled.", show_alert=True)
            return

        if action == "approve":
            balance = get_balance(conn, req["user_id"]) or 0
            if balance < req["amount"]:
                await query.answer("User no longer has enough balance!", show_alert=True)
                return
            new_balance = adjust_balance(conn, req["user_id"], -req["amount"], "withdrawal",
                                          note=f"withdrawal request #{req_id}")
            conn.execute(
                "UPDATE withdrawal_requests SET status='approved', handled_at=?, handled_by=? WHERE req_id=?",
                (datetime.utcnow().isoformat(), query.from_user.id, req_id),
            )
            conn.commit()
            user_msg = f"✅ Your withdrawal request #{req_id} for {req['amount']} pts was approved. New balance: {new_balance}."
        else:
            conn.execute(
                "UPDATE withdrawal_requests SET status='rejected', handled_at=?, handled_by=? WHERE req_id=?",
                (datetime.utcnow().isoformat(), query.from_user.id, req_id),
            )
            conn.commit()
            user_msg = f"❌ Your withdrawal request #{req_id} for {req['amount']} pts was rejected."

    await query.answer("Done.")
    await query.edit_message_text(query.message.text + f"\n\n→ {action.upper()} by admin {query.from_user.id}")
    try:
        await context.bot.send_message(req["user_id"], user_msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Group registration (bot added to / removed from a group)
# ---------------------------------------------------------------------------

async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return
    with closing(get_conn()) as conn:
        register_group(conn, chat)


# ---------------------------------------------------------------------------
# /bet conversation flow (GROUP ONLY)
# ---------------------------------------------------------------------------

@group_only
@not_banned
async def bet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    challenger = update.effective_user

    with closing(get_conn()) as conn:
        register_group(conn, chat)
        ensure_user(conn, challenger.id, challenger.username, challenger.first_name)
        touch_last_group(conn, challenger.id, chat.id)
        if is_banned(conn, challenger.id):
            await update.message.reply_text("⛔ You are banned from betting.")
            return ConversationHandler.END

    args = context.args or []
    opponent_id = None
    opponent_username = None
    opponent_display = None
    remaining_args = args

    if args and args[0].startswith("@"):
        opponent_username = args[0].lstrip("@")
        with closing(get_conn()) as conn:
            opponent_id = find_user_id_by_username(conn, opponent_username)
        opponent_display = f"@{opponent_username}"
        remaining_args = args[1:]
    elif update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied = update.message.reply_to_message.from_user
        if replied.is_bot:
            await update.message.reply_text("You can't challenge a bot.")
            return ConversationHandler.END
        if replied.id == challenger.id:
            await update.message.reply_text("You can't challenge yourself.")
            return ConversationHandler.END
        opponent_id = replied.id
        opponent_username = replied.username
        opponent_display = f"@{replied.username}" if replied.username else replied.first_name
        with closing(get_conn()) as conn:
            ensure_user(conn, replied.id, replied.username, replied.first_name)
        remaining_args = args
    else:
        await update.message.reply_text(
            "Tell me who to challenge:\n"
            "• /bet @username\n"
            "• or reply to their message with /bet"
        )
        return ConversationHandler.END

    with closing(get_conn()) as conn:
        if opponent_id and is_banned(conn, opponent_id):
            await update.message.reply_text("That player is banned and can't play.")
            return ConversationHandler.END

    context.user_data["bet_challenge"] = {
        "opponent_id": opponent_id,
        "opponent_username": opponent_username,
        "opponent_display": opponent_display,
        "challenger_id": challenger.id,
        "chat_id": chat.id,
    }

    # Fast path for power users: /bet @user 100 🎲 even
    if len(remaining_args) >= 2:
        amount = None
        try:
            candidate = int(remaining_args[0])
            if candidate > 0:
                amount = candidate
        except ValueError:
            amount = None

        if amount is not None:
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

    await update.message.reply_text(
        f"Challenging {opponent_display}! How many points do you want to bet?\n"
        f"(reply to this message with a number)",
        reply_markup=ForceReply(selective=True),
    )
    return ASK_AMOUNT


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Please send a positive whole number of points.",
            reply_markup=ForceReply(selective=True),
        )
        return ASK_AMOUNT

    challenger = update.effective_user
    with closing(get_conn()) as conn:
        balance = get_balance(conn, challenger.id)

    if balance is None or balance < amount:
        await update.message.reply_text(
            f"You only have {balance or 0} points — pick a smaller amount.",
            reply_markup=ForceReply(selective=True),
        )
        return ASK_AMOUNT

    context.user_data["bet_challenge"]["amount"] = amount

    keyboard = [
        [InlineKeyboardButton(f"{emoji} {label}", callback_data=f"game:{emoji}")]
        for emoji, label in GAME_EMOJI_LABELS.items()
    ]
    await update.message.reply_text(
        "Pick a game:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ASK_GAME


async def ask_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emoji = query.data.split(":", 1)[1]
    context.user_data.setdefault("bet_challenge", {})["emoji"] = emoji

    keyboard = [
        [
            InlineKeyboardButton("Even", callback_data="pred:even"),
            InlineKeyboardButton("Odd", callback_data="pred:odd"),
        ]
    ]
    await query.edit_message_text(
        f"Game: {emoji} {GAME_EMOJI_LABELS.get(emoji, '')}\nNow pick your prediction:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ASK_PREDICTION


async def ask_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prediction = query.data.split(":", 1)[1]
    data = context.user_data.get("bet_challenge", {})
    amount = data.get("amount")
    emoji = data.get("emoji")

    if amount is None or emoji is None:
        await query.edit_message_text("Something went wrong — please start again with /bet.")
        context.user_data.pop("bet_challenge", None)
        return ConversationHandler.END

    await query.edit_message_text("Creating bet...")
    await _create_bet_and_announce(update, context, amount, emoji, prediction, via_callback=True)
    context.user_data.pop("bet_challenge", None)
    return ConversationHandler.END


async def bet_cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("bet_challenge", None)
    await update.message.reply_text("Bet creation cancelled.")
    return ConversationHandler.END


async def _create_bet_and_announce(update, context, amount, emoji, prediction, via_callback=False):
    """Shared logic to insert the bet row and post the challenge message."""
    chat_id = update.effective_chat.id
    challenger = update.effective_user
    data = context.user_data.get("bet_challenge", {})
    opponent_id = data.get("opponent_id")
    opponent_username = data.get("opponent_username")
    opponent_display = data.get("opponent_display") or "opponent"

    with closing(get_conn()) as conn:
        balance = ensure_user(conn, challenger.id, challenger.username, challenger.first_name)
        if balance < amount:
            msg = f"You only have {balance} points - can't bet {amount}."
            if via_callback:
                await context.bot.send_message(chat_id, msg)
            else:
                await update.message.reply_text(msg)
            return

        conn.execute(
            """
            INSERT INTO bets (chat_id, challenger_id, challenger_name, opponent_id,
                               opponent_name, amount, emoji, prediction, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                chat_id,
                challenger.id,
                challenger.username or challenger.first_name,
                opponent_id,
                opponent_username,
                amount,
                emoji,
                prediction,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        bet_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    opposite_prediction = "odd" if prediction == "even" else "even"
    challenger_tag = f"@{challenger.username}" if challenger.username else challenger.first_name
    text = (
        f"🎲 Bet #{bet_id} created!\n"
        f"{challenger_tag} challenges {opponent_display} for {amount} points.\n"
        f"Game: {emoji}\n"
        f"Prediction: {challenger_tag} picked *{prediction.upper()}* "
        f"(giving {opponent_display} *{opposite_prediction.upper()}*)\n\n"
        f"{opponent_display}, reply to THIS message with /accept to start!"
    )
    sent = await context.bot.send_message(chat_id, text, parse_mode="Markdown")

    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE bets SET challenge_message_id=? WHERE bet_id=?",
            (sent.message_id, bet_id),
        )
        conn.commit()


bet_conv = ConversationHandler(
    entry_points=[CommandHandler("bet", bet_start)],
    states={
        ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount)],
        ASK_GAME: [CallbackQueryHandler(ask_game, pattern="^game:")],
        ASK_PREDICTION: [CallbackQueryHandler(ask_prediction, pattern="^pred:")],
    },
    fallbacks=[CommandHandler("cancel", bet_cancel_conv)],
    name="bet_conversation",
    persistent=False,
)


# ---------------------------------------------------------------------------
# /accept, /cancel, /mybets, /balance, /leaderboard  (GROUP ONLY)
# ---------------------------------------------------------------------------

@group_only
@not_banned
async def accept_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    bet_id = None

    if context.args:
        try:
            bet_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("bet_id must be a number.")
            return
    elif update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied = update.message.reply_to_message
        if replied.from_user.id == context.bot.id:
            with closing(get_conn()) as conn:
                row = conn.execute(
                    """
                    SELECT bet_id FROM bets
                    WHERE chat_id=? AND challenge_message_id=? AND status='pending'
                    """,
                    (chat_id, replied.message_id),
                ).fetchone()
            if row:
                bet_id = row["bet_id"]

    if bet_id is None:
        await update.message.reply_text(
            "Reply to the bet challenge message with /accept, or use /accept <bet_id>."
        )
        return

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)
        touch_last_group(conn, user.id, chat_id)
        if is_banned(conn, user.id):
            await update.message.reply_text("⛔ You are banned from betting.")
            return

        bet = conn.execute(
            "SELECT * FROM bets WHERE bet_id=? AND chat_id=?", (bet_id, chat_id)
        ).fetchone()

        if bet is None:
            await update.message.reply_text("No bet with that ID here.")
            return
        if bet["status"] != "pending":
            await update.message.reply_text(f"Bet #{bet_id} is already '{bet['status']}'.")
            return
        if bet["challenger_id"] == user.id:
            await update.message.reply_text("You can't accept your own bet.")
            return

        expected_tag = (bet["opponent_name"] or "").lstrip("@").lower()
        if expected_tag and (user.username or "").lower() != expected_tag:
            await update.message.reply_text(
                f"This bet was aimed at @{expected_tag}, not you."
            )
            return

        balance = get_balance(conn, user.id)
        if balance < bet["amount"]:
            await update.message.reply_text(
                f"You need {bet['amount']} points to accept, you have {balance}."
            )
            return

        conn.execute(
            "UPDATE bets SET status='accepted', opponent_id=? WHERE bet_id=?",
            (user.id, bet_id),
        )
        conn.commit()

    await update.message.reply_text(
        f"✅ Bet #{bet_id} accepted! Rolling {bet['emoji']}..."
    )

    # Roll the native Telegram animated dice
    dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=bet["emoji"])
    rolled_value = dice_msg.dice.value

    is_even = (rolled_value % 2 == 0)
    outcome = "even" if is_even else "odd"

    challenger_prediction = bet["prediction"]

    if outcome == challenger_prediction:
        winner_id = bet["challenger_id"]
        loser_id = user.id
        winner_name = bet["challenger_name"]
    else:
        winner_id = user.id
        loser_id = bet["challenger_id"]
        winner_name = user.username or user.first_name

    amount = bet["amount"]

    with closing(get_conn()) as conn:
        tax_percent = get_tax_percent(conn)
        tax_amount = round(amount * tax_percent / 100)
        payout = amount - tax_amount

        adjust_balance(conn, winner_id, payout, "bet_win", note=f"bet #{bet_id}")
        adjust_balance(conn, loser_id, -amount, "bet_loss", note=f"bet #{bet_id}")
        if tax_amount:
            log_transaction(conn, winner_id, "tax", -tax_amount, note=f"tax on bet #{bet_id}",
                             balance_after=get_balance(conn, winner_id))
        conn.execute(
            "UPDATE bets SET status='resolved', winner_id=?, tax_amount=? WHERE bet_id=?",
            (winner_id, tax_amount, bet_id),
        )
        conn.commit()

    tax_note = f" (after {tax_percent}% house tax: {tax_amount} pts)" if tax_amount else ""
    await update.message.reply_text(
        f"🎯 Result: Rolled a *{rolled_value}* ({outcome.upper()})!\n"
        f"🏆 @{winner_name} wins Bet #{bet_id} and receives {payout} points!{tax_note}",
        parse_mode="Markdown"
    )


@group_only
async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /cancel <bet_id>")
        return
    try:
        bet_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("bet_id must be a number.")
        return

    with closing(get_conn()) as conn:
        bet = conn.execute(
            "SELECT * FROM bets WHERE bet_id=? AND chat_id=?", (bet_id, chat_id)
        ).fetchone()
        if bet is None:
            await update.message.reply_text("No bet with that ID here.")
            return
        if bet["challenger_id"] != user.id:
            await update.message.reply_text("Only the challenger can cancel this bet.")
            return
        if bet["status"] != "pending":
            await update.message.reply_text("Only a pending (not yet accepted) bet can be cancelled.")
            return

        conn.execute("UPDATE bets SET status='cancelled' WHERE bet_id=?", (bet_id,))
        conn.commit()

    await update.message.reply_text(f"Bet #{bet_id} cancelled.")


@group_only
async def mybets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT * FROM bets
            WHERE chat_id=? AND status IN ('pending','accepted')
              AND (challenger_id=? OR opponent_id=?)
            ORDER BY bet_id DESC
            """,
            (chat_id, user.id, user.id),
        ).fetchall()

    if not rows:
        await update.message.reply_text("You have no open bets.")
        return

    lines = []
    for r in rows:
        lines.append(
            f"#{r['bet_id']} [{r['status']}] {r['challenger_name']} vs "
            f"{r['opponent_name']} - {r['amount']} pts ({r['emoji']} {r['prediction']})"
        )
    await update.message.reply_text("\n".join(lines))


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick balance check - works in both group and DM, but DM prefers /wallet."""
    user = update.effective_user
    with closing(get_conn()) as conn:
        balance = ensure_user(conn, user.id, user.username, user.first_name)
        if update.effective_chat.type != "private":
            touch_last_group(conn, user.id, update.effective_chat.id)
    await update.message.reply_text(f"💰 Your balance: {balance} points")


@group_only
async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Top balances among users who have played in THIS group."""
    chat_id = update.effective_chat.id
    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT u.username, u.balance
            FROM users u
            WHERE u.user_id IN (
                SELECT challenger_id FROM bets WHERE chat_id=?
                UNION
                SELECT opponent_id FROM bets WHERE chat_id=?
            )
            ORDER BY u.balance DESC LIMIT 10
            """,
            (chat_id, chat_id),
        ).fetchall()

    if not rows:
        await update.message.reply_text("No players yet.")
        return

    lines = ["🏅 *Leaderboard*"]
    for i, r in enumerate(rows, start=1):
        name = r["username"] or "unknown"
        lines.append(f"{i}. @{name} - {r['balance']} pts")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# HTTP Health Check Server
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health check server listening on port {port}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set the BOT_TOKEN environment variable to your Telegram bot token.")
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS is empty - nobody will be able to use the admin panel!")

    init_db()
    start_health_server()

    app = Application.builder().token(token).build()

    # Universal
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))

    # Group-only (play)
    app.add_handler(bet_conv)
    app.add_handler(CommandHandler("accept", accept_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("mybets", mybets_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    # DM-only (wallet)
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("deposit", deposit_cmd))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd))

    # Admin panel
    app.add_handler(CommandHandler("admin", admin_menu))
    app.add_handler(CommandHandler("addbalance", addbalance_cmd))
    app.add_handler(CommandHandler("removebalance", removebalance_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("settax", settax_cmd))
    app.add_handler(CommandHandler("deposits", deposits_cmd))
    app.add_handler(CommandHandler("withdrawals", withdrawals_cmd))

    app.add_handler(CallbackQueryHandler(admin_menu_callback, pattern="^admin:"))
    app.add_handler(CallbackQueryHandler(deposit_decision_callback, pattern="^dep:"))
    app.add_handler(CallbackQueryHandler(withdrawal_decision_callback, pattern="^wd:"))

    # Track groups the bot is added to
    app.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Catch-all in DM: any other text just shows the wallet card
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, dm_fallback))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
def init_db():
    with closing(get_conn()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                balance INTEGER NOT NULL DEFAULT 1000,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bets (
                bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
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
        conn.commit()
        # Migration for DBs created before challenge_message_id existed
        try:
            conn.execute("ALTER TABLE bets ADD COLUMN challenge_message_id INTEGER")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists


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
        return STARTING_BALANCE
    if username and row["username"] != username:
        conn.execute(
            "UPDATE users SET username=? WHERE chat_id=? AND user_id=?",
            (username, chat_id, user_id),
        )
        conn.commit()
    return row["balance"]


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


# ---------------------------------------------------------------------------
# Basic command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(get_conn()) as conn:
        ensure_user(conn, update.effective_chat.id, update.effective_user.id, update.effective_user.username)
    await update.message.reply_text(
        "Welcome! Everyone starts with 1000 points.\n"
        "Use /help to see how to challenge other players."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Commands*\n"
        "/bet @user - or reply to someone's message with /bet - start a challenge\n"
        "  _You'll be asked for the amount, then the game, then even/odd via buttons_\n"
        "  _Fast path: /bet @alice 100 🎲 even_\n"
        "/accept - reply to the bot's challenge message with /accept to take it\n"
        "/accept <bet_id> - or accept by id directly\n"
        "/cancel <bet_id> - cancel your unaccepted bet\n"
        "/mybets - list your open/pending bets\n"
        "/balance - check your points\n"
        "/leaderboard - top balances in this chat\n\n"
        "Supported games: 🎲 Dice, 🎯 Darts, 🎳 Bowling, 🏀 Basketball, ⚽ Football, 🎰 Slots",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /bet conversation flow
# ---------------------------------------------------------------------------

async def bet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    challenger = update.effective_user

    with closing(get_conn()) as conn:
        ensure_user(conn, chat_id, challenger.id, challenger.username)

    args = context.args or []
    opponent_id = None
    opponent_username = None
    opponent_display = None
    remaining_args = args

    if args and args[0].startswith("@"):
        opponent_username = args[0].lstrip("@")
        with closing(get_conn()) as conn:
            opponent_id = find_user_id_by_username(conn, chat_id, opponent_username)
        opponent_display = f"@{opponent_username}"
        remaining_args = args[1:]
    elif update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied = update.message.reply_to_message.from_user
        if replied.is_bot:
            await update.message.reply_text("You can't challenge a bot.")
            return ConversationHandler.END
        if replied.id == challenger.id:
            await update.message.reply_text("You can't challenge yourself.")
            return ConversationHandler.END
        opponent_id = replied.id
        opponent_username = replied.username
        opponent_display = f"@{replied.username}" if replied.username else replied.first_name
        with closing(get_conn()) as conn:
            ensure_user(conn, chat_id, replied.id, replied.username)
        remaining_args = args
    else:
        await update.message.reply_text(
            "Tell me who to challenge:\n"
            "• /bet @username\n"
            "• or reply to their message with /bet"
        )
        return ConversationHandler.END

    context.user_data["bet_challenge"] = {
        "opponent_id": opponent_id,
        "opponent_username": opponent_username,
        "opponent_display": opponent_display,
        "challenger_id": challenger.id,
    }

    # Fast path for power users: /bet @user 100 🎲 even
    if len(remaining_args) >= 2:
        amount = None
        try:
            candidate = int(remaining_args[0])
            if candidate > 0:
                amount = candidate
        except ValueError:
            amount = None

        if amount is not None:
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

    await update.message.reply_text(
        f"Challenging {opponent_display}! How many points do you want to bet?\n"
        f"(reply to this message with a number)",
        reply_markup=ForceReply(selective=True),
    )
    return ASK_AMOUNT


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Please send a positive whole number of points.",
            reply_markup=ForceReply(selective=True),
        )
        return ASK_AMOUNT

    chat_id = update.effective_chat.id
    challenger = update.effective_user
    with closing(get_conn()) as conn:
        balance = get_balance(conn, chat_id, challenger.id)

    if balance is None or balance < amount:
        await update.message.reply_text(
            f"You only have {balance or 0} points — pick a smaller amount.",
            reply_markup=ForceReply(selective=True),
        )
        return ASK_AMOUNT

    context.user_data["bet_challenge"]["amount"] = amount

    keyboard = [
        [InlineKeyboardButton(f"{emoji} {label}", callback_data=f"game:{emoji}")]
        for emoji, label in GAME_EMOJI_LABELS.items()
    ]
    await update.message.reply_text(
        "Pick a game:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ASK_GAME


async def ask_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emoji = query.data.split(":", 1)[1]
    context.user_data.setdefault("bet_challenge", {})["emoji"] = emoji

    keyboard = [
        [
            InlineKeyboardButton("Even", callback_data="pred:even"),
            InlineKeyboardButton("Odd", callback_data="pred:odd"),
        ]
    ]
    await query.edit_message_text(
        f"Game: {emoji} {GAME_EMOJI_LABELS.get(emoji, '')}\nNow pick your prediction:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ASK_PREDICTION


async def ask_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prediction = query.data.split(":", 1)[1]
    data = context.user_data.get("bet_challenge", {})
    amount = data.get("amount")
    emoji = data.get("emoji")

    if amount is None or emoji is None:
        await query.edit_message_text("Something went wrong — please start again with /bet.")
        context.user_data.pop("bet_challenge", None)
        return ConversationHandler.END

    await query.edit_message_text("Creating bet...")
    await _create_bet_and_announce(update, context, amount, emoji, prediction, via_callback=True)
    context.user_data.pop("bet_challenge", None)
    return ConversationHandler.END


async def bet_cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("bet_challenge", None)
    await update.message.reply_text("Bet creation cancelled.")
    return ConversationHandler.END


async def _create_bet_and_announce(update, context, amount, emoji, prediction, via_callback=False):
    """Shared logic to insert the bet row and post the challenge message."""
    chat_id = update.effective_chat.id
    challenger = update.effective_user
    data = context.user_data.get("bet_challenge", {})
    opponent_id = data.get("opponent_id")
    opponent_username = data.get("opponent_username")
    opponent_display = data.get("opponent_display") or "opponent"

    with closing(get_conn()) as conn:
        balance = ensure_user(conn, chat_id, challenger.id, challenger.username)
        if balance < amount:
            msg = f"You only have {balance} points - can't bet {amount}."
            if via_callback:
                await context.bot.send_message(chat_id, msg)
            else:
                await update.message.reply_text(msg)
            return

        conn.execute(
            """
            INSERT INTO bets (chat_id, challenger_id, challenger_name, opponent_id,
                               opponent_name, amount, emoji, prediction, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                chat_id,
                challenger.id,
                challenger.username or challenger.first_name,
                opponent_id,
                opponent_username,
                amount,
                emoji,
                prediction,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        bet_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    opposite_prediction = "odd" if prediction == "even" else "even"
    challenger_tag = f"@{challenger.username}" if challenger.username else challenger.first_name
    text = (
        f"🎲 Bet #{bet_id} created!\n"
        f"{challenger_tag} challenges {opponent_display} for {amount} points.\n"
        f"Game: {emoji}\n"
        f"Prediction: {challenger_tag} picked *{prediction.upper()}* "
        f"(giving {opponent_display} *{opposite_prediction.upper()}*)\n\n"
        f"{opponent_display}, reply to THIS message with /accept to start!"
    )
    sent = await context.bot.send_message(chat_id, text, parse_mode="Markdown")

    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE bets SET challenge_message_id=? WHERE bet_id=?",
            (sent.message_id, bet_id),
        )
        conn.commit()


bet_conv = ConversationHandler(
    entry_points=[CommandHandler("bet", bet_start)],
    states={
        ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount)],
        ASK_GAME: [CallbackQueryHandler(ask_game, pattern="^game:")],
        ASK_PREDICTION: [CallbackQueryHandler(ask_prediction, pattern="^pred:")],
    },
    fallbacks=[CommandHandler("cancel", bet_cancel_conv)],
    name="bet_conversation",
    persistent=False,
)


# ---------------------------------------------------------------------------
# /accept, /cancel, /mybets, /balance, /leaderboard
# ---------------------------------------------------------------------------

async def accept_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    bet_id = None

    if context.args:
        try:
            bet_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("bet_id must be a number.")
            return
    elif update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied = update.message.reply_to_message
        if replied.from_user.id == context.bot.id:
            with closing(get_conn()) as conn:
                row = conn.execute(
                    """
                    SELECT bet_id FROM bets
                    WHERE chat_id=? AND challenge_message_id=? AND status='pending'
                    """,
                    (chat_id, replied.message_id),
                ).fetchone()
            if row:
                bet_id = row["bet_id"]

    if bet_id is None:
        await update.message.reply_text(
            "Reply to the bet challenge message with /accept, or use /accept <bet_id>."
        )
        return

    with closing(get_conn()) as conn:
        ensure_user(conn, chat_id, user.id, user.username)
        bet = conn.execute(
            "SELECT * FROM bets WHERE bet_id=? AND chat_id=?", (bet_id, chat_id)
        ).fetchone()

        if bet is None:
            await update.message.reply_text("No bet with that ID here.")
            return
        if bet["status"] != "pending":
            await update.message.reply_text(f"Bet #{bet_id} is already '{bet['status']}'.")
            return
        if bet["challenger_id"] == user.id:
            await update.message.reply_text("You can't accept your own bet.")
            return

        expected_tag = (bet["opponent_name"] or "").lstrip("@").lower()
        if expected_tag and (user.username or "").lower() != expected_tag:
            await update.message.reply_text(
                f"This bet was aimed at @{expected_tag}, not you."
            )
            return

        balance = get_balance(conn, chat_id, user.id)
        if balance < bet["amount"]:
            await update.message.reply_text(
                f"You need {bet['amount']} points to accept, you have {balance}."
            )
            return

        conn.execute(
            "UPDATE bets SET status='accepted', opponent_id=? WHERE bet_id=?",
            (user.id, bet_id),
        )
        conn.commit()

    await update.message.reply_text(
        f"✅ Bet #{bet_id} accepted! Rolling {bet['emoji']}..."
    )

    # Roll the native Telegram animated dice
    dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=bet["emoji"])
    rolled_value = dice_msg.dice.value

    is_even = (rolled_value % 2 == 0)
    outcome = "even" if is_even else "odd"

    challenger_prediction = bet["prediction"]

    if outcome == challenger_prediction:
        winner_id = bet["challenger_id"]
        loser_id = user.id
        winner_name = bet["challenger_name"]
    else:
        winner_id = user.id
        loser_id = bet["challenger_id"]
        winner_name = user.username or user.first_name

    amount = bet["amount"]

    with closing(get_conn()) as conn:
        adjust_balance(conn, chat_id, winner_id, amount)
        adjust_balance(conn, chat_id, loser_id, -amount)
        conn.execute(
            "UPDATE bets SET status='resolved', winner_id=? WHERE bet_id=?",
            (winner_id, bet_id),
        )
        conn.commit()

    await update.message.reply_text(
        f"🎯 Result: Rolled a *{rolled_value}* ({outcome.upper()})!\n"
        f"🏆 @{winner_name} wins Bet #{bet_id} and receives {amount} points!",
        parse_mode="Markdown"
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /cancel <bet_id>")
        return
    try:
        bet_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("bet_id must be a number.")
        return

    with closing(get_conn()) as conn:
        bet = conn.execute(
            "SELECT * FROM bets WHERE bet_id=? AND chat_id=?", (bet_id, chat_id)
        ).fetchone()
        if bet is None:
            await update.message.reply_text("No bet with that ID here.")
            return
        if bet["challenger_id"] != user.id:
            await update.message.reply_text("Only the challenger can cancel this bet.")
            return
        if bet["status"] != "pending":
            await update.message.reply_text("Only a pending (not yet accepted) bet can be cancelled.")
            return

        conn.execute("UPDATE bets SET status='cancelled' WHERE bet_id=?", (bet_id,))
        conn.commit()

    await update.message.reply_text(f"Bet #{bet_id} cancelled.")


async def mybets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT * FROM bets
            WHERE chat_id=? AND status IN ('pending','accepted')
              AND (challenger_id=? OR opponent_id=?)
            ORDER BY bet_id DESC
            """,
            (chat_id, user.id, user.id),
        ).fetchall()

    if not rows:
        await update.message.reply_text("You have no open bets.")
        return

    lines = []
    for r in rows:
        lines.append(
            f"#{r['bet_id']} [{r['status']}] {r['challenger_name']} vs "
            f"{r['opponent_name']} - {r['amount']} pts ({r['emoji']} {r['prediction']})"
        )
    await update.message.reply_text("\n".join(lines))


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    with closing(get_conn()) as conn:
        balance = ensure_user(conn, chat_id, user.id, user.username)
    await update.message.reply_text(f"💰 Your balance: {balance} points")


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT username, balance FROM users WHERE chat_id=? ORDER BY balance DESC LIMIT 10",
            (chat_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("No players yet.")
        return

    lines = ["🏅 *Leaderboard*"]
    for i, r in enumerate(rows, start=1):
        name = r["username"] or "unknown"
        lines.append(f"{i}. @{name} - {r['balance']} pts")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# HTTP Health Check Server
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health check server listening on port {port}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set the BOT_TOKEN environment variable to your Telegram bot token.")

    init_db()
    start_health_server()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(bet_conv)
    app.add_handler(CommandHandler("accept", accept_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("mybets", mybets_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
