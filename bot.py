"""
Player-vs-Player Betting Bot for Telegram with Native Dice & Automatic Settlement
=================================================================================

GROUP CHATS: playing only.
    /bet @opponent            - or reply to someone's message with /bet
    /bet @opponent 100 🎲 even - fast path, skips the button flow
    /accept                   - reply to the bot's challenge message
    /accept <bet_id>          - or accept by id
    /cancel <bet_id>          - cancel your own unaccepted bet
    /mybets                   - your open/pending bets in this chat
    /leaderboard              - top balances among players active in this chat

DM WITH THE BOT: account management only.
    /balance                  - wallet balance, recent deposit/withdrawal/win/loss
                                history, and buttons to jump back into your groups

ADMIN PANEL (any chat, admin-only):
    /ban <@user|id>           - stop a player from betting/accepting
    /unban <@user|id>
    /deposit <@user|id> <amt> - credit a player's wallet
    /withdraw <@user|id> <amt>- debit a player's wallet
    /settax <percent>         - house cut taken from every payout (0-100)
    /tax                      - show current tax rate (anyone can check)
    /housebalance             - total tax collected so far
    /addadmin <user_id>       - grant admin rights to another user

IMPORTANT SCOPE NOTE:
Deposit/withdraw here are admin-controlled ledger adjustments only. This bot
does NOT integrate any real payment processor (UPI, bank, card, crypto). If
real money is meant to back these points, that exchange has to happen outside
the bot, with an admin then reflecting it via /deposit or /withdraw. Wiring
real money movement directly into a peer-to-peer wagering bot would make this
an unlicensed gambling service in most jurisdictions, which is not something
this code should do.

Initial admin(s) are bootstrapped from the ADMIN_USER_IDS environment
variable (comma-separated Telegram numeric user IDs) on first run.
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
from telegram.constants import ChatType
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
STARTING_BALANCE = 1000
DEFAULT_TAX_PERCENT = 0.0
HOUSE_ACCOUNT_ID = 0  # pseudo user_id used only in the transactions ledger

GAME_EMOJI_LABELS = {
    "🎲": "Dice",
    "🎯": "Darts",
    "🎳": "Bowling",
    "🏀": "Basketball",
    "⚽": "Football",
    "🎰": "Slots",
}
VALID_EMOJIS = set(GAME_EMOJI_LABELS.keys())

ASK_AMOUNT, ASK_GAME, ASK_PREDICTION = range(3)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_conn()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER NOT NULL DEFAULT 1000,
                banned INTEGER NOT NULL DEFAULT 0
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
                admin_id INTEGER,
                note TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS group_players (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                chat_title TEXT,
                chat_username TEXT,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)"
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
                tax_amount INTEGER DEFAULT 0,
                challenge_message_id INTEGER,
                created_at TEXT
            )
            """
        )
        conn.commit()

        # Migrations for DBs created by earlier versions of this bot
        for stmt in (
            "ALTER TABLE bets ADD COLUMN challenge_message_id INTEGER",
            "ALTER TABLE bets ADD COLUMN tax_amount INTEGER DEFAULT 0",
        ):
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists


def bootstrap_admins():
    raw = os.environ.get("ADMIN_USER_IDS", "")
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    if not ids:
        logger.warning("ADMIN_USER_IDS not set - no admins configured yet.")
        return
    with closing(get_conn()) as conn:
        for item in ids:
            try:
                uid = int(item)
            except ValueError:
                continue
            conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (uid,))
        conn.commit()


# ---- Wallet / ledger -------------------------------------------------------

def ensure_wallet(conn, user_id, username):
    row = conn.execute("SELECT * FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO wallets (user_id, username, balance, banned) VALUES (?, ?, ?, 0)",
            (user_id, username, STARTING_BALANCE),
        )
        conn.commit()
        _log_tx(conn, user_id, "signup_bonus", STARTING_BALANCE, note="Starting balance")
        return conn.execute("SELECT * FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    if username and row["username"] != username:
        conn.execute("UPDATE wallets SET username=? WHERE user_id=?", (username, user_id))
        conn.commit()
        row = conn.execute("SELECT * FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    return row


def get_wallet(conn, user_id):
    return conn.execute("SELECT * FROM wallets WHERE user_id=?", (user_id,)).fetchone()


def is_banned(conn, user_id):
    row = get_wallet(conn, user_id)
    return bool(row and row["banned"])


def _log_tx(conn, user_id, tx_type, amount, admin_id=None, note=None):
    conn.execute(
        "INSERT INTO transactions (user_id, type, amount, admin_id, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, tx_type, amount, admin_id, note, datetime.utcnow().isoformat()),
    )
    conn.commit()


def adjust_wallet(conn, user_id, delta, tx_type, admin_id=None, note=None):
    conn.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (delta, user_id))
    conn.commit()
    _log_tx(conn, user_id, tx_type, delta, admin_id=admin_id, note=note)


def register_group_player(conn, chat_id, chat_title, chat_username, user_id, username):
    conn.execute(
        """
        INSERT INTO group_players (chat_id, user_id, username, chat_title, chat_username)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            username=excluded.username,
            chat_title=excluded.chat_title,
            chat_username=excluded.chat_username
        """,
        (chat_id, user_id, username, chat_title, chat_username),
    )
    conn.commit()


def resolve_user_ref(conn, ref):
    """Resolve '@username' or a numeric id to a user_id, or None if unknown."""
    ref = ref.strip()
    if ref.startswith("@"):
        uname = ref[1:].lower()
        row = conn.execute(
            "SELECT user_id FROM wallets WHERE lower(username)=?", (uname,)
        ).fetchone()
        return row["user_id"] if row else None
    try:
        return int(ref)
    except ValueError:
        return None


def is_admin(conn, user_id):
    return conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone() is not None


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_tax_percent(conn):
    return float(get_setting(conn, "tax_percent", DEFAULT_TAX_PERCENT))


# ---------------------------------------------------------------------------
# Chat-type helpers
# ---------------------------------------------------------------------------

def is_group_chat(update: Update) -> bool:
    return update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def is_private_chat(update: Update) -> bool:
    return update.effective_chat.type == ChatType.PRIVATE


async def _require_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_group_chat(update):
        return True
    await update.effective_message.reply_text(
        "🎮 Betting only happens in group chats — add me to a group and play there.\n"
        "Use /balance right here to check your wallet."
    )
    return False


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    with closing(get_conn()) as conn:
        ok = is_admin(conn, update.effective_user.id)
    if not ok:
        await update.effective_message.reply_text("⛔ Admins only.")
    return ok


async def _notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    try:
        await context.bot.send_message(user_id, text)
    except Forbidden:
        pass  # user has never started a DM with the bot
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Wallet display (DM)
# ---------------------------------------------------------------------------

async def _send_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with closing(get_conn()) as conn:
        wallet = ensure_wallet(conn, user.id, user.username)
        txs = conn.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY tx_id DESC LIMIT 10",
            (user.id,),
        ).fetchall()
        groups = conn.execute(
            "SELECT chat_id, chat_title, chat_username FROM group_players WHERE user_id=?",
            (user.id,),
        ).fetchall()

    lines = ["💼 *Your Wallet*", f"Balance: {wallet['balance']} pts"]
    if wallet["banned"]:
        lines.append("⚠️ Your account is currently *banned* from playing.")

    lines.append("")
    lines.append("📜 *Recent activity*")
    if txs:
        for t in txs:
            sign = "+" if t["amount"] >= 0 else ""
            label = t["type"].replace("_", " ")
            when = (t["created_at"] or "").split("T")[0]
            lines.append(f"{sign}{t['amount']} pts — {label} ({when})")
    else:
        lines.append("No activity yet.")

    linkable = [g for g in groups if g["chat_username"]]
    unlinkable = [g for g in groups if not g["chat_username"]]

    if unlinkable:
        lines.append("")
        lines.append("Groups you play in (open them directly):")
        for g in unlinkable:
            lines.append(f"• {g['chat_title'] or 'Unnamed group'}")

    if not groups:
        lines.append("")
        lines.append("You haven't played in a group yet — get added to one to start!")

    keyboard = [
        [InlineKeyboardButton(f"🎮 Play in {g['chat_title'] or g['chat_username']}",
                               url=f"https://t.me/{g['chat_username']}")]
        for g in linkable
    ]
    markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="Markdown", reply_markup=markup
    )


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_private_chat(update):
        with closing(get_conn()) as conn:
            ensure_wallet(conn, update.effective_user.id, update.effective_user.username)
        if context.args and context.args[0] == "wallet":
            await _send_wallet(update, context)
            return
        await update.message.reply_text(
            "Welcome! I run point-based betting games in group chats.\n"
            "Add me to a group to challenge friends there. Use /balance here anytime "
            "for your wallet and history."
        )
        return

    chat = update.effective_chat
    user = update.effective_user
    with closing(get_conn()) as conn:
        ensure_wallet(conn, user.id, user.username)
        register_group_player(conn, chat.id, chat.title, chat.username, user.id, user.username)
    await update.message.reply_text(
        "Welcome! Everyone starts with 1000 points.\n"
        "Use /help to see how to challenge other players."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(get_conn()) as conn:
        admin = is_admin(conn, update.effective_user.id)

    if is_private_chat(update):
        text = (
            "*Wallet (DM only)*\n"
            "/balance - your balance, deposit/withdrawal history, and quick links "
            "back into your groups\n\n"
            "Betting itself happens in group chats — add me to a group and use /bet there."
        )
    else:
        text = (
            "*Commands*\n"
            "/bet @user - or reply to someone's message with /bet - start a challenge\n"
            "  _Fast path: /bet @alice 100 🎲 even_\n"
            "/accept - reply to the bot's challenge message with /accept\n"
            "/accept <bet_id> - or accept by id directly\n"
            "/cancel <bet_id> - cancel your unaccepted bet\n"
            "/mybets - list your open/pending bets\n"
            "/leaderboard - top balances in this chat\n\n"
            "DM me /balance to see your wallet & history.\n\n"
            "Supported games: 🎲 Dice, 🎯 Darts, 🎳 Bowling, 🏀 Basketball, ⚽ Football, 🎰 Slots"
        )

    if admin:
        text += (
            "\n\n*Admin panel*\n"
            "/ban <@user|id> - ban a player\n"
            "/unban <@user|id> - unban a player\n"
            "/deposit <@user|id> <amount> - credit a wallet\n"
            "/withdraw <@user|id> <amount> - debit a wallet\n"
            "/settax <percent> - set house tax on payouts (0-100)\n"
            "/tax - show current tax rate\n"
            "/housebalance - total tax collected\n"
            "/stats - full economy overview (players, volume, tax, etc.)\n"
            "/addadmin <user_id> - grant admin rights"
        )

    await update.effective_message.reply_text(text, parse_mode="Markdown")


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group_chat(update):
        bot_username = context.bot.username
        markup = None
        if bot_username:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(
                    "💬 Check wallet in DM",
                    url=f"https://t.me/{bot_username}?start=wallet",
                )]]
            )
        await update.message.reply_text(
            "Your wallet and transaction history are private — check them in our DM.",
            reply_markup=markup,
        )
        return
    await _send_wallet(update, context)


# ---------------------------------------------------------------------------
# /bet conversation flow (group only)
# ---------------------------------------------------------------------------

async def bet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update, context):
        return ConversationHandler.END

    chat = update.effective_chat
    chat_id = chat.id
    challenger = update.effective_user

    with closing(get_conn()) as conn:
        ensure_wallet(conn, challenger.id, challenger.username)
        register_group_player(conn, chat_id, chat.title, chat.username, challenger.id, challenger.username)
        if is_banned(conn, challenger.id):
            await update.message.reply_text("⛔ You are banned from playing.")
            return ConversationHandler.END

    args = context.args or []
    opponent_id = None
    opponent_username = None
    opponent_display = None
    remaining_args = args

    if args and args[0].startswith("@"):
        opponent_username = args[0].lstrip("@")
        with closing(get_conn()) as conn:
            opponent_id = resolve_user_ref(conn, args[0])
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
            ensure_wallet(conn, replied.id, replied.username)
            register_group_player(conn, chat_id, chat.title, chat.username, replied.id, replied.username)
        remaining_args = args
    else:
        await update.message.reply_text(
            "Tell me who to challenge:\n"
            "• /bet @username\n"
            "• or reply to their message with /bet"
        )
        return ConversationHandler.END

    if opponent_id is not None:
        with closing(get_conn()) as conn:
            if is_banned(conn, opponent_id):
                await update.message.reply_text("⛔ That player is banned from playing.")
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

    challenger = update.effective_user
    with closing(get_conn()) as conn:
        wallet = ensure_wallet(conn, challenger.id, challenger.username)

    if wallet["balance"] < amount:
        await update.message.reply_text(
            f"You only have {wallet['balance']} points — pick a smaller amount.",
            reply_markup=ForceReply(selective=True),
        )
        return ASK_AMOUNT

    context.user_data["bet_challenge"]["amount"] = amount

    keyboard = [
        [InlineKeyboardButton(f"{emoji} {label}", callback_data=f"game:{emoji}")]
        for emoji, label in GAME_EMOJI_LABELS.items()
    ]
    await update.message.reply_text("Pick a game:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ASK_GAME


async def ask_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    emoji = query.data.split(":", 1)[1]
    context.user_data.setdefault("bet_challenge", {})["emoji"] = emoji

    keyboard = [[
        InlineKeyboardButton("🟢 Even", callback_data="pred:even"),
        InlineKeyboardButton("🔴 Odd", callback_data="pred:odd"),
    ]]
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
    chat_id = update.effective_chat.id
    challenger = update.effective_user
    data = context.user_data.get("bet_challenge", {})
    opponent_id = data.get("opponent_id")
    opponent_username = data.get("opponent_username")
    opponent_display = data.get("opponent_display") or "opponent"

    with closing(get_conn()) as conn:
        wallet = ensure_wallet(conn, challenger.id, challenger.username)
        if wallet["balance"] < amount:
            msg = f"You only have {wallet['balance']} points - can't bet {amount}."
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
            "UPDATE bets SET challenge_message_id=? WHERE bet_id=?", (sent.message_id, bet_id)
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
# /accept, /cancel, /mybets, /leaderboard (group only)
# ---------------------------------------------------------------------------

async def accept_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update, context):
        return

    chat = update.effective_chat
    chat_id = chat.id
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
        ensure_wallet(conn, user.id, user.username)
        register_group_player(conn, chat_id, chat.title, chat.username, user.id, user.username)

        if is_banned(conn, user.id):
            await update.message.reply_text("⛔ You are banned from playing.")
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
            await update.message.reply_text(f"This bet was aimed at @{expected_tag}, not you.")
            return

        wallet = get_wallet(conn, user.id)
        if wallet["balance"] < bet["amount"]:
            await update.message.reply_text(
                f"You need {bet['amount']} points to accept, you have {wallet['balance']}."
            )
            return

        conn.execute(
            "UPDATE bets SET status='accepted', opponent_id=? WHERE bet_id=?", (user.id, bet_id)
        )
        conn.commit()

    await update.message.reply_text(f"✅ Bet #{bet_id} accepted! Rolling {bet['emoji']}...")

    dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=bet["emoji"])
    rolled_value = dice_msg.dice.value
    outcome = "even" if rolled_value % 2 == 0 else "odd"

    if outcome == bet["prediction"]:
        winner_id, loser_id = bet["challenger_id"], user.id
        winner_name = bet["challenger_name"]
    else:
        winner_id, loser_id = user.id, bet["challenger_id"]
        winner_name = user.username or user.first_name

    amount = bet["amount"]

    with closing(get_conn()) as conn:
        tax_percent = get_tax_percent(conn)
        # round-half-up so a 1-point bet at low tax % doesn't always truncate to 0,
        # and totals reconcile correctly against amount * tax_percent / 100 on average
        tax_amount = int((amount * tax_percent / 100) + 0.5)
        tax_amount = min(tax_amount, amount)  # tax can never exceed the wagered amount
        winner_gain = amount - tax_amount

        adjust_wallet(conn, winner_id, winner_gain, "bet_win", note=f"Bet #{bet_id}")
        adjust_wallet(conn, loser_id, -amount, "bet_loss", note=f"Bet #{bet_id}")
        if tax_amount > 0:
            # The ledger (transactions table) is the single source of truth for tax
            # collected — stats are computed by summing it directly, so there's
            # nothing here that can drift out of sync.
            _log_tx(conn, HOUSE_ACCOUNT_ID, "tax", tax_amount, note=f"Bet #{bet_id}")

        conn.execute(
            "UPDATE bets SET status='resolved', winner_id=?, tax_amount=? WHERE bet_id=?",
            (winner_id, tax_amount, bet_id),
        )
        conn.commit()

    result_text = (
        f"🎯 Result: Rolled a *{rolled_value}* ({outcome.upper()})!\n"
        f"🏆 @{winner_name} wins Bet #{bet_id} and receives {winner_gain} points!"
    )
    if tax_amount > 0:
        result_text += f"\n🏛 House tax: {tax_amount} pts ({tax_percent:g}%)"
    await update.message.reply_text(result_text, parse_mode="Markdown")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update, context):
        return

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
    if not await _require_group(update, context):
        return

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

    lines = [
        f"#{r['bet_id']} [{r['status']}] {r['challenger_name']} vs "
        f"{r['opponent_name']} - {r['amount']} pts ({r['emoji']} {r['prediction']})"
        for r in rows
    ]
    await update.message.reply_text("\n".join(lines))


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_group(update, context):
        return

    chat_id = update.effective_chat.id
    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT w.username, w.balance
            FROM group_players gp
            JOIN wallets w ON w.user_id = gp.user_id
            WHERE gp.chat_id=?
            ORDER BY w.balance DESC LIMIT 10
            """,
            (chat_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("No players yet.")
        return

    lines = ["🏅 *Leaderboard*"]
    for i, r in enumerate(rows, start=1):
        lines.append(f"{i}. @{r['username'] or 'unknown'} - {r['balance']} pts")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <@username|user_id>")
        return
    with closing(get_conn()) as conn:
        target_id = resolve_user_ref(conn, context.args[0])
        if target_id is None:
            await update.message.reply_text("Couldn't find that player (they may not have used the bot yet).")
            return
        conn.execute("UPDATE wallets SET banned=1 WHERE user_id=?", (target_id,))
        conn.commit()
    await update.message.reply_text(f"⛔ User {target_id} banned.")
    await _notify_user(context, target_id, "⛔ You have been banned from playing by an admin.")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <@username|user_id>")
        return
    with closing(get_conn()) as conn:
        target_id = resolve_user_ref(conn, context.args[0])
        if target_id is None:
            await update.message.reply_text("Couldn't find that player.")
            return
        conn.execute("UPDATE wallets SET banned=0 WHERE user_id=?", (target_id,))
        conn.commit()
    await update.message.reply_text(f"✅ User {target_id} unbanned.")
    await _notify_user(context, target_id, "✅ You have been unbanned and can play again.")


async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, context):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /deposit <@username|user_id> <amount>")
        return
    with closing(get_conn()) as conn:
        target_id = resolve_user_ref(conn, context.args[0])
        if target_id is None:
            await update.message.reply_text("Couldn't find that player.")
            return
        try:
            amount = int(context.args[1])
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Amount must be a positive whole number.")
            return
        ensure_wallet(conn, target_id, None)
        adjust_wallet(conn, target_id, amount, "deposit", admin_id=update.effective_user.id)
        new_balance = get_wallet(conn, target_id)["balance"]
    await update.message.reply_text(f"💰 Credited {amount} pts to {target_id}. New balance: {new_balance}.")
    await _notify_user(context, target_id, f"💰 An admin credited {amount} pts to your wallet.")


async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, context):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /withdraw <@username|user_id> <amount>")
        return
    with closing(get_conn()) as conn:
        target_id = resolve_user_ref(conn, context.args[0])
        if target_id is None:
            await update.message.reply_text("Couldn't find that player.")
            return
        try:
            amount = int(context.args[1])
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Amount must be a positive whole number.")
            return
        wallet = ensure_wallet(conn, target_id, None)
        if wallet["balance"] < amount:
            await update.message.reply_text(
                f"That player only has {wallet['balance']} pts — can't withdraw {amount}."
            )
            return
        adjust_wallet(conn, target_id, -amount, "withdrawal", admin_id=update.effective_user.id)
        new_balance = get_wallet(conn, target_id)["balance"]
    await update.message.reply_text(f"🏧 Debited {amount} pts from {target_id}. New balance: {new_balance}.")
    await _notify_user(context, target_id, f"🏧 An admin debited {amount} pts from your wallet.")


async def settax_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /settax <percent 0-100>")
        return
    try:
        percent = float(context.args[0])
        if not (0 <= percent <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Percent must be a number between 0 and 100.")
        return
    with closing(get_conn()) as conn:
        set_setting(conn, "tax_percent", percent)
    await update.message.reply_text(f"🏛 House tax set to {percent:g}% of every payout.")


async def tax_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(get_conn()) as conn:
        percent = get_tax_percent(conn)
    await update.message.reply_text(f"🏛 Current house tax: {percent:g}% of every winning payout.")


def _total_tax_collected(conn) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM transactions WHERE type='tax'"
    ).fetchone()
    return int(row["total"])


async def housebalance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, context):
        return
    with closing(get_conn()) as conn:
        total = _total_tax_collected(conn)
    await update.message.reply_text(f"🏛 Total tax collected: {total} pts.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, context):
        return
    with closing(get_conn()) as conn:
        tax_percent = get_tax_percent(conn)
        total_tax = _total_tax_collected(conn)

        players = conn.execute("SELECT COUNT(*) AS c FROM wallets").fetchone()["c"]
        banned = conn.execute(
            "SELECT COUNT(*) AS c FROM wallets WHERE banned=1"
        ).fetchone()["c"]
        circulating = conn.execute(
            "SELECT COALESCE(SUM(balance), 0) AS s FROM wallets"
        ).fetchone()["s"]

        total_bets = conn.execute("SELECT COUNT(*) AS c FROM bets").fetchone()["c"]
        resolved_bets = conn.execute(
            "SELECT COUNT(*) AS c FROM bets WHERE status='resolved'"
        ).fetchone()["c"]
        pending_bets = conn.execute(
            "SELECT COUNT(*) AS c FROM bets WHERE status IN ('pending','accepted')"
        ).fetchone()["c"]
        volume = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM bets WHERE status='resolved'"
        ).fetchone()["s"]

        active_groups = conn.execute(
            "SELECT COUNT(DISTINCT chat_id) AS c FROM group_players"
        ).fetchone()["c"]

    text = (
        "📊 *Bot Stats*\n\n"
        f"👥 Players: {players} ({banned} banned)\n"
        f"💰 Points in circulation: {circulating}\n"
        f"🏟 Active groups: {active_groups}\n\n"
        f"🎲 Bets total: {total_bets} "
        f"({resolved_bets} resolved, {pending_bets} open)\n"
        f"📈 Total volume wagered (resolved): {volume} pts\n\n"
        f"🏛 Tax rate: {tax_percent:g}%\n"
        f"🏛 Total tax collected: {total_tax} pts"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Provide a numeric Telegram user id.")
        return
    with closing(get_conn()) as conn:
        conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_admin_id,))
        conn.commit()
    await update.message.reply_text(f"✅ {new_admin_id} is now an admin.")


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
    bootstrap_admins()
    start_health_server()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))

    app.add_handler(bet_conv)
    app.add_handler(CommandHandler("accept", accept_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("mybets", mybets_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("deposit", deposit_cmd))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd))
    app.add_handler(CommandHandler("settax", settax_cmd))
    app.add_handler(CommandHandler("tax", tax_cmd))
    app.add_handler(CommandHandler("housebalance", housebalance_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
