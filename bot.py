"""
BlockVerse-BOT - Player-vs-Player Telegram Betting Bot
=======================================================

Wallet:
🏦 Your Wallet

💵 Balance: $0
❌ UPI: Not saved yet

Min withdrawal: $2

Inline keyboard:
Row 1: 🟢 Deposit | 🔵 Withdrawal
Row 2: 🔵 History | 🔵 Usage
Row 3: Official Group

Notes:
- Telegram Bot API does not allow bots to set inline-button background colours.
  Button colours are controlled by the Telegram client/theme. The requested
  visual distinction is represented with 🟢/🔵 labels.
- SQLite database is persistent on the same disk.
- Money is stored as whole dollars in this version.
"""

import logging
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bet_bot.db")

# New users start at $0 as requested.
STARTING_BALANCE = 0
MIN_WITHDRAWAL = 2

ADMIN_IDS = [
    int(i.strip())
    for i in os.environ.get("ADMIN_IDS", "").split(",")
    if i.strip().isdigit()
]

GROUP_LINK = os.environ.get(
    "GROUP_LINK",
    "https://t.me/your_group_link",
)

# ---------------------------------------------------------------------------
# Inline button colour helpers
# ---------------------------------------------------------------------------
#
# Note on "button colours": the Telegram Bot API — used identically by both
# aiogram and python-telegram-bot — gives a bot no way to set an inline
# button's background colour; that is controlled entirely by the Telegram
# client/theme. Neither library has a ButtonStyle.success / .danger /
# .primary concept the way some other chat platforms do. To still express
# that "success / danger / primary" language clearly, every button below is
# consistently prefixed with a colour-coded circle emoji so the four key
# action buttons (Deposit, Withdrawal, Approve, Decline) are easy to tell
# apart at a glance.


def btn_success(text: str, callback_data: str) -> InlineKeyboardButton:
    """Green 'success' style button (e.g. Approve)."""
    return InlineKeyboardButton(f"🟢 {text}", callback_data=callback_data)


def btn_danger(text: str, callback_data: str) -> InlineKeyboardButton:
    """Red 'danger' style button (e.g. Decline)."""
    return InlineKeyboardButton(f"🔴 {text}", callback_data=callback_data)


def btn_primary(text: str, callback_data: str) -> InlineKeyboardButton:
    """Blue 'primary' style button (e.g. main navigation actions)."""
    return InlineKeyboardButton(f"🔵 {text}", callback_data=callback_data)


GAME_EMOJI_LABELS = {
    "🎲": "Dice",
    "🎯": "Darts",
    "🎳": "Bowling",
    "🏀": "Basketball",
    "⚽": "Football",
    "🎰": "Slots",
}
VALID_EMOJIS = set(GAME_EMOJI_LABELS)

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


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn, table_name, column_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def init_db():
    with closing(get_conn()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER NOT NULL DEFAULT 0,
                upi_id TEXT,
                is_banned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )

        # Migration for databases created by older versions.
        if not column_exists(conn, "users", "first_name"):
            conn.execute("ALTER TABLE users ADD COLUMN first_name TEXT")

        if not column_exists(conn, "users", "upi_id"):
            conn.execute("ALTER TABLE users ADD COLUMN upi_id TEXT")

        if not column_exists(conn, "users", "created_at"):
            conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT")

        if not column_exists(conn, "users", "updated_at"):
            conn.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")

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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO system_config (key, value)
            VALUES ('tax_percent', '0')
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO system_config (key, value)
            VALUES ('upi_id', 'not_set@upi')
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO system_config (key, value)
            VALUES (
                'usdt_bep20_address',
                '0x0000000000000000000000000000000000000000'
            )
            """
        )

        conn.commit()


def ensure_user(conn, user_id, username=None, first_name=None):
    now = datetime.utcnow().isoformat()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO users
            (user_id, username, first_name, balance, is_banned, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            (
                user_id,
                username,
                first_name,
                STARTING_BALANCE,
                now,
                now,
            ),
        )
        conn.commit()
        return STARTING_BALANCE, 0

    conn.execute(
        """
        UPDATE users
        SET username=?,
            first_name=?,
            updated_at=?
        WHERE user_id=?
        """,
        (username, first_name, now, user_id),
    )
    conn.commit()

    return row["balance"], row["is_banned"]


def is_user_banned(conn, user_id):
    row = conn.execute(
        "SELECT is_banned FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return bool(row and row["is_banned"])


def get_balance(conn, user_id):
    row = conn.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return int(row["balance"]) if row else 0


def adjust_balance(conn, user_id, delta):
    # Transaction is intentionally committed by the caller's connection.
    conn.execute(
        """
        UPDATE users
        SET balance = balance + ?,
            updated_at = ?
        WHERE user_id = ?
        """,
        (delta, datetime.utcnow().isoformat(), user_id),
    )


def get_user_upi(conn, user_id):
    row = conn.execute(
        "SELECT upi_id FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return row["upi_id"] if row and row["upi_id"] else None


def save_user_upi(conn, user_id, upi_id):
    conn.execute(
        """
        UPDATE users
        SET upi_id=?, updated_at=?
        WHERE user_id=?
        """,
        (upi_id, datetime.utcnow().isoformat(), user_id),
    )


def find_user_id_by_username(conn, username):
    username = username.lstrip("@")
    row = conn.execute(
        """
        SELECT user_id
        FROM users
        WHERE username=? COLLATE NOCASE
        """,
        (username,),
    ).fetchone()
    return row["user_id"] if row else None


def get_config_val(conn, key):
    row = conn.execute(
        "SELECT value FROM system_config WHERE key=?",
        (key,),
    ).fetchone()
    return row["value"] if row else ""


# ---------------------------------------------------------------------------
# Wallet UI
# ---------------------------------------------------------------------------

def wallet_keyboard():
    # Telegram itself controls the actual inline-button background colour;
    # see the "Inline button colour helpers" note above. Two buttons per
    # row, as requested.
    return InlineKeyboardMarkup(
        [
            [
                btn_primary("Deposit", "wallet_deposit"),
                btn_primary("Withdrawal", "wallet_withdraw"),
            ],
            [
                InlineKeyboardButton("🔵 History", callback_data="wallet_history"),
                InlineKeyboardButton("🔵 Usage", callback_data="wallet_usage"),
            ],
            [
                InlineKeyboardButton("Official Group", url=GROUP_LINK),
            ],
        ]
    )


def wallet_text(user_id):
    with closing(get_conn()) as conn:
        ensure_user(conn, user_id)
        balance = get_balance(conn, user_id)
        upi = get_user_upi(conn, user_id)

    upi_line = f"💳 UPI: `{upi}`" if upi else "❌ UPI: Not saved yet"

    return (
        "🏦 *Your Wallet*\n\n"
        f"💵 *Balance:* ${balance}\n"
        f"{upi_line}\n\n"
        f"*Min withdrawal: ${MIN_WITHDRAWAL}*"
    )


async def show_wallet(update, context):
    user = update.effective_user
    if not user:
        return

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)
        if is_user_banned(conn, user.id):
            text = "🚫 You are banned from using this bot."
            if update.callback_query:
                await update.callback_query.edit_message_text(text)
            else:
                await update.message.reply_text(text)
            return

    text = wallet_text(user.id)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=wallet_keyboard(),
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=wallet_keyboard(),
        )


# ---------------------------------------------------------------------------
# Real-time notifications: admin (new tx) <-> user (tx result)
# ---------------------------------------------------------------------------

async def notify_admins_new_tx(context, tx_id, tx_type, user, method, amount, details=None):
    """Push a real-time notification to every admin as soon as a deposit or
    withdrawal request is created, with inline Approve/Decline buttons so
    it can be actioned immediately without opening /admin."""
    if not ADMIN_IDS:
        return

    label = "📥 *New Deposit Request*" if tx_type == "deposit" else "📤 *New Withdrawal Request*"
    username = f"@{user.username}" if user.username else (user.first_name or "Unknown")
    detail_line = f"\nAddress/UPI: `{details}`" if details else ""

    text = (
        f"{label}\n\n"
        f"Tx ID: #{tx_id}\n"
        f"User: {username} (`{user.id}`)\n"
        f"Method: {method.upper()}\n"
        f"Amount: ${amount}"
        f"{detail_line}"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                btn_success("Approve", f"tx_app_{tx_id}"),
                btn_danger("Decline", f"tx_rej_{tx_id}"),
            ]
        ]
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception(
                "Failed to notify admin %s of new tx #%s", admin_id, tx_id
            )


async def notify_user_tx_result(context, tx):
    """Push a real-time notification to the user as soon as an admin
    approves or declines their deposit/withdrawal."""
    tx_type_label = "Deposit" if tx["tx_type"] == "deposit" else "Withdrawal"

    if tx["status"] == "approved":
        if tx["tx_type"] == "deposit":
            body = "Your balance has been credited."
        else:
            body = "Your withdrawal has been processed and sent."
        text = (
            f"✅ *{tx_type_label} Approved*\n\n"
            f"Tx ID: #{tx['tx_id']}\n"
            f"Amount: ${tx['amount']}\n\n"
            f"{body}"
        )
    else:
        if tx["tx_type"] == "withdrawal":
            body = "The reserved amount has been refunded to your wallet balance."
        else:
            body = "If you believe this is a mistake, please contact support."
        text = (
            f"❌ *{tx_type_label} Declined*\n\n"
            f"Tx ID: #{tx['tx_id']}\n"
            f"Amount: ${tx['amount']}\n\n"
            f"{body}"
        )

    try:
        await context.bot.send_message(
            chat_id=tx["user_id"],
            text=text,
            parse_mode="Markdown",
        )
    except Exception:
        logger.exception(
            "Failed to notify user %s of tx #%s result", tx["user_id"], tx["tx_id"]
        )


# ---------------------------------------------------------------------------
# /start and /wallet
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)

        if is_user_banned(conn, user.id):
            await update.message.reply_text(
                "🚫 You are banned from using this bot."
            )
            return

    if chat.type == "private":
        text = (
            f"✨ *Welcome, {user.first_name}!*\n\n"
            "🎮 *BlockVerse-BOT*\n\n"
            "Use /wallet to open your wallet.\n"
            "You can deposit, withdraw, view history, and read the usage guide."
        )
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Official Group",
                            url=GROUP_LINK,
                        )
                    ]
                ]
            ),
        )
    else:
        await update.message.reply_text(
            "🎮 *Betting Bot is Active!*\n\n"
            "Use `/bet <@username|reply> <amount>` to place a challenge.\n"
            "Use `/wallet` in DM to manage your wallet.",
            parse_mode="Markdown",
        )


async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "🏦 Please open the bot in private chat and use /wallet."
        )
        return
    await show_wallet(update, context)


# ---------------------------------------------------------------------------
# Wallet callbacks: History / Usage
# ---------------------------------------------------------------------------

async def wallet_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)

        if is_user_banned(conn, user.id):
            await query.edit_message_text(
                "🚫 You are banned from using this bot."
            )
            return

        if query.data == "wallet_history":
            bets = conn.execute(
                """
                SELECT bet_id, amount, emoji, prediction, status,
                       winner_id, created_at
                FROM bets
                WHERE challenger_id=? OR opponent_id=?
                ORDER BY bet_id DESC
                LIMIT 30
                """,
                (user.id, user.id),
            ).fetchall()

            txs = conn.execute(
                """
                SELECT tx_id, tx_type, method, amount, details,
                       status, created_at
                FROM transactions
                WHERE user_id=?
                ORDER BY tx_id DESC
                LIMIT 30
                """,
                (user.id,),
            ).fetchall()

    lines = ["📜 *Full History*", ""]

    if txs:
        lines.append("*💳 Transactions*")
        for tx in txs:
            details = f" — {tx['details']}" if tx["details"] else ""
            lines.append(
                f"#{tx['tx_id']} • {tx['tx_type'].title()} "
                f"${tx['amount']} • {tx['method'].upper()} "
                f"• {tx['status'].title()}{details}"
            )
    else:
        lines.append("*💳 Transactions*\nNo transactions yet.")

    lines.append("")

    if bets:
        lines.append("*🎮 Betting History*")
        for bet in bets:
            if bet["status"] == "resolved":
                if bet["winner_id"] == user.id:
                    result = "WIN"
                else:
                    result = "LOSS"
            else:
                result = bet["status"].upper()

            lines.append(
                f"Bet #{bet['bet_id']} • ${bet['amount']} • "
                f"{bet['emoji']} {bet['prediction'].upper()} • {result}"
            )
    else:
        lines.append("*🎮 Betting History*\nNo betting history yet.")

    text = "\n".join(lines)

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="wallet_back")]]
    )

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def wallet_usage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    text = (
        "📖 *How to Use BlockVerse-BOT*\n\n"
        "*🏦 Wallet*\n"
        "Use /wallet to check your balance and manage payments.\n\n"
        "*📥 Deposit*\n"
        "1. Tap Deposit.\n"
        "2. Select UPI or USDT BEP-20.\n"
        "3. Follow the payment instructions.\n"
        "4. Enter the deposited dollar amount.\n"
        "5. Wait for admin approval.\n\n"
        "*📤 Withdrawal*\n"
        f"Minimum withdrawal is ${MIN_WITHDRAWAL}.\n"
        "Select UPI or USDT, enter the amount and destination.\n"
        "Withdrawal funds are reserved until an admin approves or rejects it.\n\n"
        "*🎮 Betting*\n"
        "Betting is available in the official group only.\n"
        "Use `/bet @username $amount` or reply to a user's message.\n"
        "You can also complete the game selection using the buttons.\n\n"
        "*🎯 Results*\n"
        "The bot rolls the selected Telegram game emoji and determines "
        "the result as even or odd.\n\n"
        "*⚠️ Important*\n"
        "Never send payment to an address other than the one displayed by "
        "the bot. Keep your transaction details for verification."
    )

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Wallet", callback_data="wallet_back")]]
    )

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def wallet_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await show_wallet(update, context)


# ---------------------------------------------------------------------------
# Deposit / Withdrawal callbacks
# ---------------------------------------------------------------------------

async def wallet_financial_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "wallet_deposit":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💳 UPI",
                        callback_data="dep_method:upi",
                    ),
                    InlineKeyboardButton(
                        "🪙 USDT BEP-20",
                        callback_data="dep_method:usdt_bep20",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="wallet_back",
                    )
                ],
            ]
        )
        await query.edit_message_text(
            "📥 *Deposit*\n\nSelect your payment method:",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return DEP_METHOD

    if query.data == "wallet_withdraw":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💳 UPI",
                        callback_data="with_method:upi",
                    ),
                    InlineKeyboardButton(
                        "🪙 USDT BEP-20",
                        callback_data="with_method:usdt_bep20",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="wallet_back",
                    )
                ],
            ]
        )
        await query.edit_message_text(
            f"📤 *Withdrawal*\n\nMinimum withdrawal: ${MIN_WITHDRAWAL}\n\n"
            "Select your withdrawal method:",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return WITH_METHOD

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Deposit
# ---------------------------------------------------------------------------

async def process_dep_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    method = query.data.split(":", 1)[1]
    context.user_data["dep_method"] = method

    with closing(get_conn()) as conn:
        if method == "upi":
            address = get_config_val(conn, "upi_id")
            instructions = (
                "💳 Send payment via UPI to:\n"
                f"`{address}`"
            )
        else:
            address = get_config_val(conn, "usdt_bep20_address")
            instructions = (
                "🪙 Send BEP-20 USDT to:\n"
                f"`{address}`"
            )

    await query.edit_message_text(
        "📥 *Deposit Instructions*\n\n"
        f"{instructions}\n\n"
        "After completing the transfer, enter the total dollar amount "
        "you deposited.\n\n"
        "Example: `10`",
        parse_mode="Markdown",
    )
    return DEP_AMOUNT


async def process_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    try:
        raw = (update.message.text or "").strip().replace("$", "")
        amount = int(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Enter a valid positive dollar amount.\nExample: 10"
        )
        return DEP_AMOUNT

    method = context.user_data.get("dep_method", "unknown")

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)

        cursor = conn.execute(
            """
            INSERT INTO transactions
            (user_id, tx_type, method, amount, status, created_at)
            VALUES (?, 'deposit', ?, ?, 'pending', ?)
            """,
            (
                user.id,
                method,
                amount,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        tx_id = cursor.lastrowid

    context.user_data.clear()

    await update.message.reply_text(
        "✅ *Deposit request submitted.*\n\n"
        "An admin must verify and approve the payment before "
        "the balance is credited.",
        parse_mode="Markdown",
    )
    await show_wallet(update, context)

    # Real-time admin alert with inline Approve/Decline buttons.
    await notify_admins_new_tx(context, tx_id, "deposit", user, method, amount)

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Withdrawal
# ---------------------------------------------------------------------------

async def process_with_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    method = query.data.split(":", 1)[1]
    context.user_data["with_method"] = method

    await query.edit_message_text(
        f"📤 *Withdrawal — {method.upper()}*\n\n"
        f"Minimum withdrawal: ${MIN_WITHDRAWAL}\n\n"
        "Enter the dollar amount you wish to withdraw.\n"
        "Example: `5`",
        parse_mode="Markdown",
    )
    return WITH_AMOUNT


async def process_with_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    try:
        raw = (update.message.text or "").strip().replace("$", "")
        amount = int(raw)
        if amount < MIN_WITHDRAWAL:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            f"❌ Minimum withdrawal is ${MIN_WITHDRAWAL}.\n"
            "Enter a valid dollar amount."
        )
        return WITH_AMOUNT

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)

        if is_user_banned(conn, user.id):
            await update.message.reply_text(
                "🚫 You are banned from using this bot."
            )
            return ConversationHandler.END

        balance = get_balance(conn, user.id)

        if balance < amount:
            await update.message.reply_text(
                f"❌ Insufficient funds.\n"
                f"Current balance: ${balance}"
            )
            return ConversationHandler.END

    context.user_data["with_amount"] = amount
    method = context.user_data.get("with_method")

    if method == "upi":
        prompt = (
            "💳 Enter your UPI ID.\n\n"
            "Your UPI ID will be saved for future withdrawals."
        )
    else:
        prompt = (
            "🪙 Enter your USDT BEP-20 wallet address."
        )

    await update.message.reply_text(prompt)
    return WITH_ADDRESS


async def process_with_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    address = (update.message.text or "").strip()

    amount = context.user_data.get("with_amount")
    method = context.user_data.get("with_method")

    if not address or amount is None or method is None:
        await update.message.reply_text(
            "❌ Withdrawal session expired. Please use /wallet again."
        )
        context.user_data.clear()
        return ConversationHandler.END

    if method == "upi" and (" " in address or len(address) < 3):
        await update.message.reply_text(
            "❌ Please enter a valid UPI ID."
        )
        return WITH_ADDRESS

    if method == "usdt_bep20" and len(address) < 20:
        await update.message.reply_text(
            "❌ Please enter a valid BEP-20 wallet address."
        )
        return WITH_ADDRESS

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)

        balance = get_balance(conn, user.id)

        if balance < amount:
            await update.message.reply_text(
                "❌ Insufficient balance. Your balance changed during processing."
            )
            return ConversationHandler.END

        # Reserve funds immediately.
        adjust_balance(conn, user.id, -amount)

        # Save UPI automatically for future use.
        if method == "upi":
            save_user_upi(conn, user.id, address)

        cursor = conn.execute(
            """
            INSERT INTO transactions
            (user_id, tx_type, method, amount, details, status, created_at)
            VALUES (?, 'withdrawal', ?, ?, ?, 'pending', ?)
            """,
            (
                user.id,
                method,
                amount,
                address,
                datetime.utcnow().isoformat(),
            ),
        )

        conn.commit()
        tx_id = cursor.lastrowid

    context.user_data.clear()

    await update.message.reply_text(
        f"✅ *Withdrawal request submitted.*\n\n"
        f"Amount: ${amount}\n"
        f"Method: {method.upper()}\n"
        "Your funds are reserved until an admin approves or rejects the request.",
        parse_mode="Markdown",
    )

    await show_wallet(update, context)

    # Real-time admin alert with inline Approve/Decline buttons.
    await notify_admins_new_tx(
        context, tx_id, "withdrawal", user, method, amount, details=address
    )

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Group Betting
# ---------------------------------------------------------------------------

async def bet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "⚠️ Betting features are available only inside the official group."
        )
        return ConversationHandler.END

    challenger = update.effective_user
    chat_id = update.effective_chat.id

    with closing(get_conn()) as conn:
        ensure_user(conn, challenger.id, challenger.username, challenger.first_name)

        if is_user_banned(conn, challenger.id):
            await update.message.reply_text(
                "🚫 You are banned from participating in games."
            )
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

        if opponent_id is None:
            await update.message.reply_text(
                "❌ I could not find that user in the database. "
                "Ask them to use /start first."
            )
            return ConversationHandler.END

        opponent_display = f"@{opponent_username}"
        remaining_args = args[1:]

    elif update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied = update.message.reply_to_message.from_user

        if replied.is_bot or replied.id == challenger.id:
            await update.message.reply_text("❌ Invalid opponent target.")
            return ConversationHandler.END

        opponent_id = replied.id
        opponent_username = replied.username
        opponent_display = (
            f"@{replied.username}"
            if replied.username
            else replied.first_name
        )

        with closing(get_conn()) as conn:
            ensure_user(
                conn,
                replied.id,
                replied.username,
                replied.first_name,
            )

        remaining_args = args

    else:
        await update.message.reply_text(
            "Specify an opponent by tagging them:\n"
            "`/bet @username 2`\n\n"
            "Or reply to their message:\n"
            "`/bet 2`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    context.user_data["bet_challenge"] = {
        "opponent_id": opponent_id,
        "opponent_username": opponent_username,
        "opponent_display": opponent_display,
        "challenger_id": challenger.id,
    }

    if len(remaining_args) >= 2:
        try:
            amount_text = remaining_args[0].replace("$", "")
            amount = int(amount_text)

            if amount > 0:
                emoji = "🎲"
                prediction = None

                for arg in remaining_args[1:]:
                    if arg in VALID_EMOJIS:
                        emoji = arg
                    elif arg.lower() in ("even", "odd"):
                        prediction = arg.lower()

                if prediction:
                    with closing(get_conn()) as conn:
                        if get_balance(conn, challenger.id) < amount:
                            await update.message.reply_text(
                                f"❌ Insufficient balance. "
                                f"Available: ${get_balance(conn, challenger.id)}"
                            )
                            return ConversationHandler.END

                    await _create_bet_and_announce(
                        update,
                        context,
                        amount,
                        emoji,
                        prediction,
                    )
                    context.user_data.pop("bet_challenge", None)
                    return ConversationHandler.END

        except ValueError:
            pass

    await update.message.reply_text(
        "💵 Enter the dollar amount for this bet:",
        reply_markup=ForceReply(selective=True),
    )
    return ASK_AMOUNT


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw = (update.message.text or "").strip().replace("$", "")
        amount = int(raw)

        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Enter a positive dollar amount.",
            reply_markup=ForceReply(selective=True),
        )
        return ASK_AMOUNT

    challenger = update.effective_user

    with closing(get_conn()) as conn:
        balance = get_balance(conn, challenger.id)

    if balance < amount:
        await update.message.reply_text(
            f"❌ Insufficient funds.\nAvailable balance: ${balance}",
            reply_markup=ForceReply(selective=True),
        )
        return ASK_AMOUNT

    context.user_data.setdefault("bet_challenge", {})["amount"] = amount

    keyboard = [
        [
            InlineKeyboardButton(
                f"{emoji} {label}",
                callback_data=f"game:{emoji}",
            )
            for emoji, label in list(GAME_EMOJI_LABELS.items())[i:i + 2]
        ]
        for i in range(0, len(GAME_EMOJI_LABELS), 2)
    ]

    await update.message.reply_text(
        "🎮 *Select game mode:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ASK_GAME


async def ask_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    emoji = query.data.split(":", 1)[1]
    context.user_data.setdefault("bet_challenge", {})["emoji"] = emoji

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Even", callback_data="pred:even"),
                InlineKeyboardButton("Odd", callback_data="pred:odd"),
            ]
        ]
    )

    await query.edit_message_text(
        f"Selected Game: {emoji}\n\nChoose your outcome prediction:",
        reply_markup=keyboard,
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
        await query.edit_message_text(
            "❌ Bet setup expired. Please start again."
        )
        context.user_data.pop("bet_challenge", None)
        return ConversationHandler.END

    await query.edit_message_text("🎮 Initializing bet challenge...")

    await _create_bet_and_announce(
        update,
        context,
        amount,
        emoji,
        prediction,
    )

    context.user_data.pop("bet_challenge", None)
    return ConversationHandler.END


async def _create_bet_and_announce(update, context, amount, emoji, prediction):
    chat_id = update.effective_chat.id
    challenger = update.effective_user
    data = context.user_data.get("bet_challenge", {})

    with closing(get_conn()) as conn:
        ensure_user(
            conn,
            challenger.id,
            challenger.username,
            challenger.first_name,
        )

        if get_balance(conn, challenger.id) < amount:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ {challenger.first_name} has insufficient balance.",
            )
            return

        conn.execute(
            """
            INSERT INTO bets
            (
                chat_id,
                challenger_id,
                challenger_name,
                opponent_id,
                opponent_name,
                amount,
                emoji,
                prediction,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
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

        bet_id = conn.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]

    text = (
        f"🎲 *Bet #{bet_id} Active!*\n\n"
        f"👤 *Challenger:* {challenger.first_name}\n"
        f"🎯 *Target:* {data.get('opponent_display')}\n"
        f"💵 *Stake:* ${amount}\n"
        f"🎮 *Game:* {emoji}\n"
        f"🎯 *Prediction:* {prediction.upper()}\n\n"
        f"To accept, the target opponent can reply to this message "
        f"or use `/accept {bet_id}`."
    )

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
    )

    with closing(get_conn()) as conn:
        conn.execute(
            """
            UPDATE bets
            SET challenge_message_id=?
            WHERE bet_id=?
            """,
            (sent.message_id, bet_id),
        )
        conn.commit()


async def accept_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "⚠️ Bet acceptance must occur inside the official group."
        )
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    bet_id = None

    if context.args:
        try:
            bet_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Invalid bet ID.")
            return

    elif update.message.reply_to_message:
        with closing(get_conn()) as conn:
            row = conn.execute(
                """
                SELECT bet_id
                FROM bets
                WHERE chat_id=?
                  AND challenge_message_id=?
                  AND status='pending'
                """,
                (
                    chat_id,
                    update.message.reply_to_message.message_id,
                ),
            ).fetchone()

            if row:
                bet_id = row["bet_id"]

    if not bet_id:
        await update.message.reply_text(
            "Reply to a bet challenge or use `/accept <bet_id>`.",
            parse_mode="Markdown",
        )
        return

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)

        if is_user_banned(conn, user.id):
            await update.message.reply_text(
                "🚫 You are banned from participating."
            )
            return

        bet = conn.execute(
            """
            SELECT *
            FROM bets
            WHERE bet_id=? AND chat_id=?
            """,
            (bet_id, chat_id),
        ).fetchone()

        if not bet or bet["status"] != "pending":
            await update.message.reply_text(
                "❌ This challenge is no longer available."
            )
            return

        if bet["opponent_id"] and bet["opponent_id"] != user.id:
            await update.message.reply_text(
                "❌ You are not the designated opponent for this bet."
            )
            return

        if bet["challenger_id"] == user.id:
            await update.message.reply_text(
                "❌ You cannot accept your own challenge."
            )
            return

        challenger_bal = get_balance(conn, bet["challenger_id"])
        opponent_bal = get_balance(conn, user.id)

        if challenger_bal < bet["amount"] or opponent_bal < bet["amount"]:
            await update.message.reply_text(
                "❌ One or both players do not have enough balance."
            )
            return

        tax_percent = int(get_config_val(conn, "tax_percent") or "0")

        conn.execute(
            """
            UPDATE bets
            SET status='accepted',
                opponent_id=?,
                opponent_name=?
            WHERE bet_id=?
            """,
            (
                user.id,
                user.username or user.first_name,
                bet_id,
            ),
        )
        conn.commit()

    dice_msg = await context.bot.send_dice(
        chat_id=chat_id,
        emoji=bet["emoji"],
    )

    rolled_value = dice_msg.dice.value
    outcome = "even" if rolled_value % 2 == 0 else "odd"

    winner_id = (
        bet["challenger_id"]
        if outcome == bet["prediction"]
        else user.id
    )
    loser_id = (
        user.id
        if winner_id == bet["challenger_id"]
        else bet["challenger_id"]
    )

    raw_amount = int(bet["amount"])
    tax_amount = int(raw_amount * tax_percent / 100)
    payout = raw_amount - tax_amount

    with closing(get_conn()) as conn:
        # The challenger and opponent each stake the bet amount.
        # Winner receives the opponent stake minus platform tax.
        adjust_balance(conn, loser_id, -raw_amount)
        adjust_balance(conn, winner_id, payout)

        conn.execute(
            """
            UPDATE bets
            SET status='resolved',
                winner_id=?
            WHERE bet_id=?
            """,
            (winner_id, bet_id),
        )

        conn.commit()

    await update.message.reply_text(
        f"🎯 *Outcome:* {rolled_value} ({outcome.upper()})\n\n"
        f"🏆 Winner: <a href='tg://user?id={winner_id}'>Player</a>\n"
        f"💵 Payout: ${payout}\n"
        f"📊 Platform tax: {tax_percent}%",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Admin Panel
# ---------------------------------------------------------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized access.")
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⚙️ Set Global Tax Rate",
                    callback_data="admin_tax",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔨 Ban / Unban User",
                    callback_data="admin_ban",
                )
            ],
            [
                InlineKeyboardButton(
                    "💳 Update Payment Details",
                    callback_data="admin_gateways",
                )
            ],
            [
                InlineKeyboardButton(
                    "📑 Process Financial Queue",
                    callback_data="admin_txs",
                )
            ],
        ]
    )

    await update.message.reply_text(
        "🔧 *Admin Operations Console*",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Unauthorized.", show_alert=True)
        return ConversationHandler.END

    await query.answer()

    if query.data == "admin_tax":
        await query.message.reply_text(
            "Provide new global platform tax percentage (0 to 100):",
            reply_markup=ForceReply(selective=True),
        )
        return SET_TAX_STATE

    if query.data == "admin_ban":
        await query.message.reply_text(
            "Provide target Telegram User ID to flip ban state:",
            reply_markup=ForceReply(selective=True),
        )
        return BAN_USER_STATE

    if query.data == "admin_gateways":
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Set UPI ID",
                        callback_data="set_gateway_upi",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Set USDT Address",
                        callback_data="set_gateway_usdt",
                    )
                ],
            ]
        )
        await query.message.reply_text(
            "Select payment gateway configuration:",
            reply_markup=keyboard,
        )
        return ConversationHandler.END

    if query.data == "set_gateway_upi":
        await query.message.reply_text(
            "Enter new default platform UPI ID:",
            reply_markup=ForceReply(selective=True),
        )
        return SET_UPI_STATE

    if query.data == "set_gateway_usdt":
        await query.message.reply_text(
            "Enter new default platform USDT BEP-20 receiving address:",
            reply_markup=ForceReply(selective=True),
        )
        return SET_USDT_STATE

    if query.data == "admin_txs":
        with closing(get_conn()) as conn:
            txs = conn.execute(
                """
                SELECT *
                FROM transactions
                WHERE status='pending'
                ORDER BY tx_id ASC
                LIMIT 20
                """
            ).fetchall()

        if not txs:
            await query.message.reply_text(
                "✅ The transaction approval queue is clean."
            )
            return ConversationHandler.END

        for tx in txs:
            buttons = [
                btn_success("Approve", f"tx_app_{tx['tx_id']}"),
                btn_danger("Reject", f"tx_rej_{tx['tx_id']}"),
            ]

            detail_info = (
                f"\nDetails: {tx['details']}"
                if tx["details"]
                else ""
            )

            await query.message.reply_text(
                f"Tx ID #{tx['tx_id']}\n"
                f"User: {tx['user_id']}\n"
                f"Type: {tx['tx_type'].upper()}\n"
                f"Method: {tx['method'].upper()}\n"
                f"Amount: ${tx['amount']}"
                f"{detail_info}",
                reply_markup=InlineKeyboardMarkup([buttons]),
            )

        return ConversationHandler.END

    return ConversationHandler.END


async def handle_tx_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Unauthorized.", show_alert=True)
        return

    await query.answer()

    try:
        _, action, tx_id_text = query.data.split("_")
        tx_id = int(tx_id_text)
    except (ValueError, TypeError):
        await query.edit_message_text("Invalid transaction action.")
        return

    with closing(get_conn()) as conn:
        tx = conn.execute(
            "SELECT * FROM transactions WHERE tx_id=?",
            (tx_id,),
        ).fetchone()

        if not tx or tx["status"] != "pending":
            await query.edit_message_text(
                "Transaction state already finalized."
            )
            return

        if action == "app":
            new_status = "approved"

            if tx["tx_type"] == "deposit":
                adjust_balance(
                    conn,
                    tx["user_id"],
                    tx["amount"],
                )

        elif action == "rej":
            new_status = "rejected"

            if tx["tx_type"] == "withdrawal":
                # Refund the reserved withdrawal amount.
                adjust_balance(
                    conn,
                    tx["user_id"],
                    tx["amount"],
                )
        else:
            await query.edit_message_text("Invalid transaction action.")
            return

        conn.execute(
            """
            UPDATE transactions
            SET status=?
            WHERE tx_id=?
            """,
            (new_status, tx_id),
        )
        conn.commit()

    await query.edit_message_text(
        f"Transaction #{tx_id} updated to: {new_status.upper()}."
    )

    # Real-time notification to the user about the outcome.
    tx_result = dict(tx)
    tx_result["status"] = new_status
    await notify_user_tx_result(context, tx_result)


async def process_tax_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = int((update.message.text or "").strip())
        if not 0 <= val <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Please enter a percentage between 0 and 100."
        )
        return SET_TAX_STATE

    with closing(get_conn()) as conn:
        conn.execute(
            """
            UPDATE system_config
            SET value=?
            WHERE key='tax_percent'
            """,
            (str(val),),
        )
        conn.commit()

    await update.message.reply_text(
        f"Global tax rate adjusted to {val}%."
    )
    return ConversationHandler.END


async def process_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int((update.message.text or "").strip())
    except ValueError:
        await update.message.reply_text(
            "Please enter a numeric Telegram User ID."
        )
        return BAN_USER_STATE

    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT is_banned FROM users WHERE user_id=?",
            (uid,),
        ).fetchone()

        if not row:
            await update.message.reply_text(
                "Target User ID is not in the database."
            )
            return ConversationHandler.END

        new_state = 0 if row["is_banned"] else 1

        conn.execute(
            """
            UPDATE users
            SET is_banned=?, updated_at=?
            WHERE user_id=?
            """,
            (
                new_state,
                datetime.utcnow().isoformat(),
                uid,
            ),
        )
        conn.commit()

    status = "UNBANNED" if new_state == 0 else "BANNED"

    await update.message.reply_text(
        f"User ID {uid} has been set to: {status}."
    )
    return ConversationHandler.END


async def process_upi_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = (update.message.text or "").strip()

    if not val:
        await update.message.reply_text("UPI ID cannot be empty.")
        return SET_UPI_STATE

    with closing(get_conn()) as conn:
        conn.execute(
            """
            UPDATE system_config
            SET value=?
            WHERE key='upi_id'
            """,
            (val,),
        )
        conn.commit()

    await update.message.reply_text(
        f"System UPI address updated to: `{val}`",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def process_usdt_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = (update.message.text or "").strip()

    if not val:
        await update.message.reply_text(
            "USDT address cannot be empty."
        )
        return SET_USDT_STATE

    with closing(get_conn()) as conn:
        conn.execute(
            """
            UPDATE system_config
            SET value=?
            WHERE key='usdt_bep20_address'
            """,
            (val,),
        )
        conn.commit()

    await update.message.reply_text(
        f"System USDT BEP-20 address updated to: `{val}`",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Render Health Server
# ---------------------------------------------------------------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)

    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    thread.start()

    logger.info("Health server started on port %s", port)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    token = os.environ.get("BOT_TOKEN")

    if not token:
        raise SystemExit(
            "Fatal Error: BOT_TOKEN environment variable not set."
        )

    init_db()
    start_health_server()

    app = Application.builder().token(token).build()

    # -----------------------------------------------------------------------
    # Wallet financial ConversationHandler
    # -----------------------------------------------------------------------
    dm_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                wallet_financial_entry,
                pattern=r"^wallet_(deposit|withdraw)$",
            )
        ],
        states={
            DEP_METHOD: [
                CallbackQueryHandler(
                    process_dep_method,
                    pattern=r"^dep_method:",
                )
            ],
            DEP_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    process_deposit_amount,
                )
            ],
            WITH_METHOD: [
                CallbackQueryHandler(
                    process_with_method,
                    pattern=r"^with_method:",
                )
            ],
            WITH_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    process_with_amount,
                )
            ],
            WITH_ADDRESS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    process_with_address,
                )
            ],
        },
        fallbacks=[
            CommandHandler("wallet", wallet_command),
        ],
        allow_reentry=True,
    )

    # -----------------------------------------------------------------------
    # Betting ConversationHandler
    # -----------------------------------------------------------------------
    bet_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("bet", bet_start),
        ],
        states={
            ASK_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    ask_amount,
                )
            ],
            ASK_GAME: [
                CallbackQueryHandler(
                    ask_game,
                    pattern=r"^game:",
                )
            ],
            ASK_PREDICTION: [
                CallbackQueryHandler(
                    ask_prediction,
                    pattern=r"^pred:",
                )
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    # -----------------------------------------------------------------------
    # Admin ConversationHandler
    # -----------------------------------------------------------------------
    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                admin_callback,
                pattern=r"^(admin_|set_gateway_)",
            )
        ],
        states={
            SET_TAX_STATE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    process_tax_set,
                )
            ],
            BAN_USER_STATE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    process_ban_user,
                )
            ],
            SET_UPI_STATE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    process_upi_set,
                )
            ],
            SET_USDT_STATE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    process_usdt_set,
                )
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    # -----------------------------------------------------------------------
    # Commands
    # -----------------------------------------------------------------------
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", wallet_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("accept", accept_cmd))

    # -----------------------------------------------------------------------
    # Wallet informational callbacks
    # These are outside the financial ConversationHandler.
    # -----------------------------------------------------------------------
    app.add_handler(
        CallbackQueryHandler(
            wallet_info_callback,
            pattern=r"^wallet_(history|usage)$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            wallet_back_callback,
            pattern=r"^wallet_back$",
        )
    )

    # Transaction approval callbacks.
    app.add_handler(
        CallbackQueryHandler(
            handle_tx_approval,
            pattern=r"^tx_(app|rej)_",
        )
    )

    # Conversation handlers.
    app.add_handler(dm_conv)
    app.add_handler(bet_conv_handler)
    app.add_handler(admin_conv)

    logger.info("BlockVerse-BOT successfully initialized.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
