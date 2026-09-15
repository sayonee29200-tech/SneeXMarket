"""
Player-vs-Player Betting Bot for Telegram
=========================================

Features:
1. Group = gameplay
2. DM = wallet/dashboard
3. Persistent PostgreSQL database
4. Deposits: UPI / USDT BEP-20
5. Withdrawals: UPI / USDT BEP-20
6. Admin panel
7. Tax configuration
8. Ban / unban users
9. Transaction approval
10. Persistent bets and balances
11. Render-compatible health server

IMPORTANT:
Set these environment variables on Render:

BOT_TOKEN
DATABASE_URL
ADMIN_IDS
GROUP_LINK
"""

import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

DATABASE_URL = os.environ.get("DATABASE_URL")

STARTING_BALANCE = 1000

ADMIN_IDS = [
    int(i.strip())
    for i in os.environ.get("ADMIN_IDS", "").split(",")
    if i.strip().isdigit()
]

GROUP_LINK = os.environ.get(
    "GROUP_LINK",
    "https://t.me/your_group_link",
)


# ============================================================================
# GAME CONFIGURATION
# ============================================================================

GAME_EMOJI_LABELS = {
    "🎲": "Dice",
    "🎯": "Darts",
    "🎳": "Bowling",
    "🏀": "Basketball",
    "⚽": "Football",
    "🎰": "Slots",
}

VALID_EMOJIS = set(GAME_EMOJI_LABELS.keys())


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
