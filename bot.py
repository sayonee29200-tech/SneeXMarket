"""
BlockVerse-BOT - Player-vs-Player Telegram Betting Bot (aiogram edition)
=========================================================================

Wallet:
🏦 Your Wallet

💵 Balance: $0
❌ UPI: Not saved yet

Min withdrawal: $2

Inline keyboard:
Row 1: 💰 Deposit | 💸 Withdrawal
Row 2: 🧾 History | 📈 Usage
Row 3: 👥 Official Group

Notes:
- Rebuilt on aiogram 3.x (Bot / Dispatcher / Router / F / FSMContext /
  StatesGroup / InlineKeyboardBuilder / ReplyKeyboardBuilder), matching the
  conventions of the reference bot: HTML parse mode, a `style=` hint
  ("success" / "danger" / "primary") on every button, and a persistent
  Reply-keyboard admin console instead of a single inline admin message.
- Buttons use aiogram `style="success"`, `style="danger"` and `style="primary"` where supported, with emoji labels as a visual fallback on older clients.
- SQLite database is persistent on the same disk. Money is stored as whole
  dollars in this version. All DB access stays synchronous (sqlite3) since
  it's local-disk and single-process; each call is short-lived enough not
  to block the event loop in any meaningful way for this bot's scale.
"""

import asyncio
import logging
import os
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("Fatal Error: BOT_TOKEN environment variable not set.")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_DB_PATH = os.path.join(APP_DIR, "bet_bot.db")

# IMPORTANT: container filesystems are commonly ephemeral after a deploy/restart.
# Keep the SQLite file on a persistent volume when the hosting platform provides
# one. Set DB_PATH explicitly to that mounted location (for example:
# DB_PATH=/data/bet_bot.db). We also use /data automatically when it exists.
def _select_db_path():
    configured = os.environ.get("DB_PATH") or os.environ.get("DATABASE_PATH")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))

    persistent_dir = os.environ.get("BOT_DATA_DIR")
    if persistent_dir:
        return os.path.abspath(os.path.join(os.path.expanduser(persistent_dir), "bet_bot.db"))

    if os.path.isdir("/data"):
        return "/data/bet_bot.db"

    return LEGACY_DB_PATH

DB_PATH = _select_db_path()


def _prepare_database_path():
    db_dir = os.path.dirname(DB_PATH)
    os.makedirs(db_dir, exist_ok=True)

    # If an older deployment stored the DB beside the Python file and the new
    # deployment has a persistent /data volume, migrate it once instead of
    # starting with an empty database. Never overwrite an existing DB.
    if DB_PATH != LEGACY_DB_PATH and not os.path.exists(DB_PATH) and os.path.exists(LEGACY_DB_PATH):
        try:
            # SQLite's backup API is safer than copying only the main file when
            # the old database was using WAL mode.
            source = sqlite3.connect(LEGACY_DB_PATH)
            target = sqlite3.connect(DB_PATH)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            logger.info("Migrated legacy database %s -> %s", LEGACY_DB_PATH, DB_PATH)
        except (OSError, sqlite3.Error) as exc:
            logger.warning("Could not migrate legacy database: %s", exc)

    logger.info("SQLite database path: %s", DB_PATH)
    if DB_PATH.startswith("/data/"):
        logger.warning(
            "SQLite is using /data. This path must be backed by a PERSISTENT volume on your hosting platform, or data will be lost on redeploy."
        )


# New users start at $0 as requested.
STARTING_BALANCE = 0
MIN_WITHDRAWAL = 2

ADMIN_IDS = [
    int(i.strip())
    for i in os.environ.get("ADMIN_IDS", "").split(",")
    if i.strip().isdigit()
]

GROUP_LINK = os.environ.get("GROUP_LINK", "https://t.me/your_group_link")

# Global bot on/off switch (admin-controlled) - mirrors the reference bot's
# BOT_STATUS flag. Admins can always use the bot even while it's OFF.
BOT_STATUS = True
BOT_OFF_MESSAGE = "⚠️ Bot is currently OFF for maintenance. Please wait for an admin to turn it back on."

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

GAME_EMOJI_LABELS = {
    "🎲": "Dice",
    "🎯": "Darts",
    "🎳": "Bowling",
    "🏀": "Basketball",
    "⚽": "Football",
    "🎰": "Slots",
}
VALID_EMOJIS = set(GAME_EMOJI_LABELS)

# Reply-keyboard button labels that belong to the admin console, so plain
# text handlers never mistake them for FSM input (mirrors the reference
# bot's MENU_BUTTONS guard against state bleeding).
ADMIN_MENU_BUTTONS = {
    "⚙️ Set Tax Rate",
    "🚫 Ban User",
    "✅ Unban User",
    "💳 Payment Gateways",
    "🔵 Payment Gateways",
    "📑 Pending Transactions",
    "➕ Add Balance",
    "➖ Cut Balance",
    "🔎 Check Balance",
    "🏆 Top Balances",
    "🔍 Find ID",
    "📢 Broadcast",
    "📊 View Stats",
    "🟢 Bot Status: ON",
    "🔴 Bot Status: OFF",
    "🏠 Main Menu",
}


# ---------------------------------------------------------------------------
# FSM States
# ---------------------------------------------------------------------------

class WalletStates(StatesGroup):
    dep_amount = State()
    dep_proof = State()
    dep_utr = State()
    with_amount = State()
    with_address = State()


class BetStates(StatesGroup):
    ask_amount = State()
    ask_game = State()
    ask_prediction = State()


class AdminStates(StatesGroup):
    set_tax = State()
    ban_user = State()
    unban_user = State()
    set_upi = State()
    set_upi_qr = State()
    set_usdt = State()
    set_usdt_qr = State()
    set_rate = State()
    add_balance_id = State()
    add_balance_amount = State()
    cut_balance_id = State()
    cut_balance_amount = State()
    check_balance = State()
    find_id = State()
    broadcast = State()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def column_exists(conn, table_name, column_name):
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(row["name"] == column_name for row in rows)


def init_db():
    _prepare_database_path()
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
            "INSERT OR IGNORE INTO system_config (key, value) VALUES ('tax_percent', '0')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO system_config (key, value) VALUES ('upi_id', 'not_set@upi')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO system_config (key, value) VALUES ('upi_qr_file_id', '')"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO system_config (key, value)
            VALUES ('usdt_bep20_address', '0x0000000000000000000000000000000000000000')
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO system_config (key, value) VALUES ('usdt_qr_file_id', '')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO system_config (key, value) VALUES ('usd_inr_rate', '0')"
        )

        # Existing DB migration.
        if not column_exists(conn, "bets", "tax_amount"):
            conn.execute("ALTER TABLE bets ADD COLUMN tax_amount INTEGER NOT NULL DEFAULT 0")

        conn.commit()


def ensure_user(conn, user_id, username=None, first_name=None):
    now = datetime.utcnow().isoformat()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO users
            (user_id, username, first_name, balance, is_banned, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            (user_id, username, first_name, STARTING_BALANCE, now, now),
        )
        conn.commit()
        return STARTING_BALANCE, 0

    conn.execute(
        "UPDATE users SET username=?, first_name=?, updated_at=? WHERE user_id=?",
        (username, first_name, now, user_id),
    )
    conn.commit()
    return row["balance"], row["is_banned"]


def is_user_banned(conn, user_id):
    row = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row["is_banned"])


def get_balance(conn, user_id):
    row = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    return int(row["balance"]) if row else 0


def adjust_balance(conn, user_id, delta):
    conn.execute(
        "UPDATE users SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
        (delta, datetime.utcnow().isoformat(), user_id),
    )


def get_user_upi(conn, user_id):
    row = conn.execute("SELECT upi_id FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row["upi_id"] if row and row["upi_id"] else None


def save_user_upi(conn, user_id, upi_id):
    conn.execute(
        "UPDATE users SET upi_id=?, updated_at=? WHERE user_id=?",
        (upi_id, datetime.utcnow().isoformat(), user_id),
    )


def find_user_id_by_username(conn, username):
    username = username.lstrip("@")
    row = conn.execute(
        "SELECT user_id FROM users WHERE username=? COLLATE NOCASE",
        (username,),
    ).fetchone()
    return row["user_id"] if row else None


def get_config_val(conn, key):
    row = conn.execute("SELECT value FROM system_config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def set_config_val(conn, key, value):
    conn.execute("UPDATE system_config SET value=? WHERE key=?", (value, key))


def all_user_ids(conn):
    return [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]


def stats_snapshot(conn):
    total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    banned_users = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_banned=1").fetchone()["c"]
    total_balance = conn.execute("SELECT COALESCE(SUM(balance), 0) AS s FROM users").fetchone()["s"]
    total_bets = conn.execute("SELECT COUNT(*) AS c FROM bets").fetchone()["c"]
    resolved_bets = conn.execute("SELECT COUNT(*) AS c FROM bets WHERE status='resolved'").fetchone()["c"]
    pending_tx = conn.execute("SELECT COUNT(*) AS c FROM transactions WHERE status='pending'").fetchone()["c"]
    total_tax = conn.execute(
        "SELECT COALESCE(SUM(tax_amount), 0) AS s FROM bets WHERE status='resolved'"
    ).fetchone()["s"]
    return {
        "total_users": total_users,
        "banned_users": banned_users,
        "total_balance": total_balance,
        "total_bets": total_bets,
        "resolved_bets": resolved_bets,
        "pending_tx": pending_tx,
        "total_tax": total_tax,
    }


def top_balances(conn, limit=10):
    return conn.execute(
        """
        SELECT user_id, username, first_name, balance
        FROM users
        ORDER BY balance DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Styled button helpers (aiogram InlineKeyboardBuilder / ReplyKeyboardBuilder)
# ---------------------------------------------------------------------------
#
# Each inline button is assigned a semantic Bot API style: `success` for
# positive/confirm actions, `danger` for destructive/reject actions, and
# `primary` for navigation, selection and neutral actions. Telegram clients
# decide the exact rendered colour/theme. Action emojis are kept meaningful
# (money, receipt, QR, address, etc.) rather than using colour-circle emojis.

def add_button(kb, *, text: str, style: str = "primary", callback_data: str = None, url: str = None):
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    try:
        kb.button(**kwargs, style=style)
    except (TypeError, ValueError):
        # Compatibility with older aiogram versions that don't expose style.
        kb.button(**kwargs)


def wallet_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    add_button(kb, text="💰 Deposit", callback_data="wallet:deposit", style="success")
    add_button(kb, text="💸 Withdrawal", callback_data="wallet:withdraw", style="danger")
    add_button(kb, text="🧾 History", callback_data="wallet:history", style="primary")
    add_button(kb, text="📈 Usage", callback_data="wallet:usage", style="primary")
    add_button(kb, text="👥 Official Group", url=GROUP_LINK, style="primary")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def back_to_wallet_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    add_button(kb, text="↩️ Back to Wallet", callback_data="wallet:back", style="primary")
    kb.adjust(1)
    return kb.as_markup()


def deposit_method_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    add_button(kb, text="💳 UPI", callback_data="dep_method:upi", style="primary")
    add_button(kb, text="🪙 USDT BEP-20", callback_data="dep_method:usdt_bep20", style="primary")
    add_button(kb, text="⬅️ Back", callback_data="wallet:back", style="primary")
    kb.adjust(2, 1)
    return kb.as_markup()


def withdraw_method_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    add_button(kb, text="💳 UPI", callback_data="with_method:upi", style="primary")
    add_button(kb, text="🪙 USDT BEP-20", callback_data="with_method:usdt_bep20", style="primary")
    add_button(kb, text="⬅️ Back", callback_data="wallet:back", style="primary")
    kb.adjust(2, 1)
    return kb.as_markup()


def tx_approval_keyboard(tx_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    add_button(kb, text="✅ Approve", callback_data=f"tx:app:{tx_id}", style="success")
    add_button(kb, text="❌ Decline", callback_data=f"tx:rej:{tx_id}", style="danger")
    kb.adjust(2)
    return kb.as_markup()


def game_select_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for emoji, label in GAME_EMOJI_LABELS.items():
        add_button(kb, text=f"{emoji} {label}", callback_data=f"game:{emoji}", style="primary")
    kb.adjust(2)
    return kb.as_markup()


def prediction_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    add_button(kb, text="⬆️ Even", callback_data="pred:even", style="primary")
    add_button(kb, text="⬇️ Odd", callback_data="pred:odd", style="primary")
    kb.adjust(2)
    return kb.as_markup()


def admin_entry_keyboard() -> InlineKeyboardMarkup:
    """Sent as a one-off inline reply to /admin, then swaps the user over
    to the persistent Reply-keyboard console below."""
    kb = InlineKeyboardBuilder()
    add_button(kb, text="🛠️ Open Admin Console", callback_data="admin:open", style="primary")
    kb.adjust(1)
    return kb.as_markup()


def gateway_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    add_button(kb, text="🆔 Set UPI ID", callback_data="gateway:upi", style="primary")
    add_button(kb, text="🖼️ Set UPI QR", callback_data="gateway:upi_qr", style="success")
    add_button(kb, text="📍 Set USDT Address", callback_data="gateway:usdt", style="success")
    add_button(kb, text="🖼️ Set USDT QR", callback_data="gateway:usdt_qr", style="success")
    add_button(kb, text="💱 Set USD/INR Rate", callback_data="gateway:rate", style="primary")
    add_button(kb, text="👁️ View Payment Settings", callback_data="gateway:view", style="primary")
    add_button(kb, text="✖️ Close", callback_data="gateway:close", style="danger")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()

def get_admin_menu_keyboard():
    """Persistent Reply keyboard admin console - mirrors the reference
    bot's `get_admin_menu_keyboard()`, adapted to what this betting bot
    actually needs (tax, bans, gateways, tx queue, balances, broadcast,
    stats, bot status)."""
    kb = ReplyKeyboardBuilder()

    add_button(kb, text="⚙️ Set Tax Rate", style="primary")
    add_button(kb, text="💳 Payment Gateways", style="primary")

    add_button(kb, text="📑 Pending Transactions", style="primary")
    add_button(kb, text="📊 View Stats", style="primary")

    add_button(kb, text="➕ Add Balance", style="success")
    add_button(kb, text="➖ Cut Balance", style="danger")

    add_button(kb, text="🔎 Check Balance", style="primary")
    add_button(kb, text="🏆 Top Balances", style="primary")

    add_button(kb, text="🚫 Ban User", style="danger")
    add_button(kb, text="✅ Unban User", style="success")

    add_button(kb, text="🔍 Find ID", style="primary")
    add_button(kb, text="📢 Broadcast", style="primary")

    status_text = "🟢 Bot Status: ON" if BOT_STATUS else "🔴 Bot Status: OFF"
    add_button(kb, text=status_text, style="success" if BOT_STATUS else "danger")

    add_button(kb, text="🏠 Main Menu", style="primary")

    kb.adjust(2, 2, 2, 2, 2, 2, 1, 1)
    return kb.as_markup(resize_keyboard=True)


# ---------------------------------------------------------------------------
# Gate: bot on/off + ban check (applied per-handler, admins bypass)
# ---------------------------------------------------------------------------

async def is_blocked(message_or_query, user_id: int) -> bool:
    """Returns True (and replies) if this user should not proceed - either
    the bot is globally OFF (admins exempt) or the user is banned."""
    is_admin = user_id in ADMIN_IDS

    if not BOT_STATUS and not is_admin:
        await _reply(message_or_query, BOT_OFF_MESSAGE)
        return True

    with closing(get_conn()) as conn:
        if is_user_banned(conn, user_id):
            await _reply(message_or_query, "🚫 You are banned from using this bot.")
            return True

    return False


async def _reply(message_or_query, text: str, **kwargs):
    if isinstance(message_or_query, CallbackQuery):
        await message_or_query.answer()
        try:
            await message_or_query.message.edit_text(text, **kwargs)
        except TelegramBadRequest:
            await message_or_query.message.answer(text, **kwargs)
    else:
        await message_or_query.answer(text, **kwargs)


# ---------------------------------------------------------------------------
# Real-time notifications: admin (new tx) <-> user (tx result)
# ---------------------------------------------------------------------------

async def notify_admins_new_tx(tx_id, tx_type, user, method, amount, details=None):
    """Push a real-time notification to every admin, including deposit proof."""
    if not ADMIN_IDS:
        return

    label = "📥 <b>New Deposit Request</b>" if tx_type == "deposit" else "📤 <b>New Withdrawal Request</b>"
    username = f"@{user.username}" if user.username else (user.first_name or "Unknown")
    detail_line = f"\nDestination: <code>{details}</code>" if details else ""

    with closing(get_conn()) as conn:
        tx = conn.execute("SELECT * FROM transactions WHERE tx_id=?", (tx_id,)).fetchone()

    text = (
        f"{label}\n\n"
        f"Tx ID: #{tx_id}\n"
        f"User: {username} (<code>{user.id}</code>)\n"
        f"Method: {method.upper()}\n"
        f"Amount: ${amount}"
        f"{detail_line}"
    )
    if tx_type == "deposit" and tx and tx["details"]:
        utr = re.search(r"(?:^|;)utr=([^;]+)", tx["details"])
        if utr:
            text += f"\nUTR/Txn ID: <code>{utr.group(1)}</code>"

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode=ParseMode.HTML, reply_markup=tx_approval_keyboard(tx_id))
        except (TelegramForbiddenError, TelegramBadRequest):
            logger.exception("Failed to notify admin %s of new tx #%s", admin_id, tx_id)

    # Send the actual deposit screenshot as a separate real-time admin message.
    if tx_type == "deposit" and tx and tx["details"]:
        proof = re.search(r"(?:^|;)proof_file_id=([^;]+)", tx["details"])
        if proof:
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_photo(
                        chat_id=admin_id,
                        photo=proof.group(1),
                        caption=f"🧾 Deposit proof — Tx #{tx_id}",
                        reply_markup=tx_approval_keyboard(tx_id),
                    )
                except (TelegramForbiddenError, TelegramBadRequest):
                    logger.exception("Failed to send proof to admin %s for tx #%s", admin_id, tx_id)

async def notify_user_tx_result(tx):
    """Push a real-time notification to the user as soon as an admin
    approves or declines their deposit/withdrawal."""
    tx_type_label = "Deposit" if tx["tx_type"] == "deposit" else "Withdrawal"

    if tx["status"] == "approved":
        body = "Your balance has been credited." if tx["tx_type"] == "deposit" else "Your withdrawal has been processed and sent."
        text = (
            f"✅ <b>{tx_type_label} Approved</b>\n\n"
            f"Tx ID: #{tx['tx_id']}\n"
            f"Amount: ${tx['amount']}\n\n"
            f"{body}"
        )
    else:
        body = (
            "The reserved amount has been refunded to your wallet balance."
            if tx["tx_type"] == "withdrawal"
            else "If you believe this is a mistake, please contact support."
        )
        text = (
            f"❌ <b>{tx_type_label} Declined</b>\n\n"
            f"Tx ID: #{tx['tx_id']}\n"
            f"Amount: ${tx['amount']}\n\n"
            f"{body}"
        )

    try:
        await bot.send_message(chat_id=tx["user_id"], text=text, parse_mode=ParseMode.HTML)
    except (TelegramForbiddenError, TelegramBadRequest):
        logger.exception("Failed to notify user %s of tx #%s result", tx["user_id"], tx["tx_id"])


# ---------------------------------------------------------------------------
# Wallet UI
# ---------------------------------------------------------------------------

def wallet_text(user_id: int) -> str:
    with closing(get_conn()) as conn:
        ensure_user(conn, user_id)
        balance = get_balance(conn, user_id)
        upi = get_user_upi(conn, user_id)

    upi_line = f"💳 UPI: <code>{upi}</code>" if upi else "❌ UPI: Not saved yet"

    return (
        "🏦 <b>Your Wallet</b>\n\n"
        f"💵 <b>Balance:</b> ${balance}\n"
        f"{upi_line}\n\n"
        f"<b>Min withdrawal: ${MIN_WITHDRAWAL}</b>"
    )


async def show_wallet(message_or_query):
    user = message_or_query.from_user
    if not user:
        return

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)

    text = wallet_text(user.id)
    await _reply(message_or_query, text, parse_mode=ParseMode.HTML, reply_markup=wallet_keyboard())


# ---------------------------------------------------------------------------
# /start and /wallet
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    user = message.from_user

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)

    if await is_blocked(message, user.id):
        return

    if message.chat.type == "private":
        text = (
            f"✨ <b>Welcome, {user.first_name}!</b>\n\n"
            "🎮 <b>BlockVerse-BOT</b>\n\n"
            "Use /wallet to open your wallet.\n"
            "You can deposit, withdraw, view history, and read the usage guide."
        )
        kb = InlineKeyboardBuilder()
        add_button(kb, text="👥 Official Group", url=GROUP_LINK, style="primary")
        kb.adjust(1)
        await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb.as_markup())
    else:
        await message.answer(
            "🎮 <b>Betting Bot is Active!</b>\n\n"
            "Use <code>/bet &lt;@username|reply&gt; &lt;amount&gt;</code> to place a challenge.\n"
            "Use <code>/wallet</code> in DM to manage your wallet.",
            parse_mode=ParseMode.HTML,
        )


@router.message(Command("wallet"))
async def wallet_command(message: Message, state: FSMContext):
    await state.clear()

    if message.chat.type != "private":
        await message.answer("🏦 Please open the bot in private chat and use /wallet.")
        return

    if await is_blocked(message, message.from_user.id):
        return

    await show_wallet(message)


# ---------------------------------------------------------------------------
# Wallet callbacks: History / Usage / Back
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "wallet:history")
async def wallet_history_callback(call: CallbackQuery):
    user = call.from_user

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)

    if await is_blocked(call, user.id):
        return

    with closing(get_conn()) as conn:
        bets = conn.execute(
            """
            SELECT bet_id, amount, emoji, prediction, status, winner_id, created_at
            FROM bets
            WHERE challenger_id=? OR opponent_id=?
            ORDER BY bet_id DESC
            LIMIT 30
            """,
            (user.id, user.id),
        ).fetchall()

        txs = conn.execute(
            """
            SELECT tx_id, tx_type, method, amount, details, status, created_at
            FROM transactions
            WHERE user_id=?
            ORDER BY tx_id DESC
            LIMIT 30
            """,
            (user.id,),
        ).fetchall()

    lines = ["📜 <b>Full History</b>", ""]

    if txs:
        lines.append("<b>💳 Transactions</b>")
        for tx in txs:
            details = f" — {tx['details']}" if tx["details"] else ""
            lines.append(
                f"#{tx['tx_id']} • {tx['tx_type'].title()} "
                f"${tx['amount']} • {tx['method'].upper()} "
                f"• {tx['status'].title()}{details}"
            )
    else:
        lines.append("<b>💳 Transactions</b>\nNo transactions yet.")

    lines.append("")

    if bets:
        lines.append("<b>🎮 Betting History</b>")
        for bet in bets:
            if bet["status"] == "resolved":
                result = "WIN" if bet["winner_id"] == user.id else "LOSS"
            else:
                result = bet["status"].upper()
            lines.append(
                f"Bet #{bet['bet_id']} • ${bet['amount']} • "
                f"{bet['emoji']} {bet['prediction'].upper()} • {result}"
            )
    else:
        lines.append("<b>🎮 Betting History</b>\nNo betting history yet.")

    await call.answer()
    await call.message.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=back_to_wallet_keyboard(),
    )


@router.callback_query(F.data == "wallet:usage")
async def wallet_usage_callback(call: CallbackQuery):
    await call.answer()

    text = (
        "📖 <b>How to Use BlockVerse-BOT</b>\n\n"
        "<b>🏦 Wallet</b>\n"
        "Use /wallet to check your balance and manage payments.\n\n"
        "<b>📥 Deposit</b>\n"
        "1. Tap Deposit.\n"
        "2. Select UPI or USDT BEP-20.\n"
        "3. Follow the payment instructions.\n"
        "4. Enter the deposited dollar amount.\n"
        "5. Wait for admin approval.\n\n"
        "<b>📤 Withdrawal</b>\n"
        f"Minimum withdrawal is ${MIN_WITHDRAWAL}.\n"
        "Select UPI or USDT, enter the amount and destination.\n"
        "Withdrawal funds are reserved until an admin approves or rejects it.\n\n"
        "<b>🎮 Betting</b>\n"
        "Betting is available in the official group only.\n"
        "Use <code>/bet @username $amount</code> or reply to a user's message.\n"
        "You can also complete the game selection using the buttons.\n\n"
        "<b>🎯 Results</b>\n"
        "The bot rolls the selected Telegram game emoji and determines "
        "the result as even or odd.\n\n"
        "<b>⚠️ Important</b>\n"
        "Never send payment to an address other than the one displayed by "
        "the bot. Keep your transaction details for verification."
    )

    await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=back_to_wallet_keyboard())


@router.callback_query(F.data == "wallet:back")
async def wallet_back_callback(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await show_wallet(call)


# ---------------------------------------------------------------------------
# Deposit / Withdrawal entry points
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "wallet:deposit")
async def wallet_deposit_entry(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if await is_blocked(call, call.from_user.id):
        return

    await call.message.edit_text(
        "📥 <b>Deposit</b>\n\nSelect your payment method:",
        parse_mode=ParseMode.HTML,
        reply_markup=deposit_method_keyboard(),
    )


@router.callback_query(F.data == "wallet:withdraw")
async def wallet_withdraw_entry(call: CallbackQuery, state: FSMContext):
    await call.answer()
    if await is_blocked(call, call.from_user.id):
        return

    await call.message.edit_text(
        f"📤 <b>Withdrawal</b>\n\nMinimum withdrawal: ${MIN_WITHDRAWAL}\n\n"
        "Select your withdrawal method:",
        parse_mode=ParseMode.HTML,
        reply_markup=withdraw_method_keyboard(),
    )


# ---------------------------------------------------------------------------
# Deposit flow
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("dep_method:"))
async def process_dep_method(call: CallbackQuery, state: FSMContext):
    await call.answer()
    method = call.data.split(":", 1)[1]
    if method not in ("upi", "usdt_bep20"):
        await call.message.edit_text("❌ Invalid deposit method.")
        return
    await state.update_data(dep_method=method)
    await call.message.edit_text(
        f"📥 <b>{'UPI' if method == 'upi' else 'USDT BEP-20'} Deposit</b>\n\n"
        "Enter the <b>USD amount</b> you want to deposit.\n"
        "Example: <code>10</code>",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(WalletStates.dep_amount)


@router.message(WalletStates.dep_amount, ~F.text.in_(ADMIN_MENU_BUTTONS))
async def process_deposit_amount(message: Message, state: FSMContext):
    user = message.from_user
    try:
        amount = int((message.text or "").strip().replace("$", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a valid positive USD amount. Example: <code>10</code>", parse_mode=ParseMode.HTML)
        return

    data = await state.get_data()
    method = data.get("dep_method")
    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)
        upi_id = get_config_val(conn, "upi_id")
        upi_qr = get_config_val(conn, "upi_qr_file_id")
        usdt_address = get_config_val(conn, "usdt_bep20_address")
        usdt_qr = get_config_val(conn, "usdt_qr_file_id")
        rate_text = get_config_val(conn, "usd_inr_rate") or "0"

    try:
        rate = float(rate_text)
    except ValueError:
        rate = 0.0

    if method == "upi":
        if not upi_id or upi_id == "not_set@upi" or rate <= 0:
            await message.answer("❌ UPI payment is not fully configured by admin yet.")
            return
        inr = amount * rate
        text = (
            "💳 <b>UPI Payment Details</b>\n\n"
            f"💵 USD deposit: <b>${amount}</b>\n"
            f"💱 Admin rate: <b>₹{rate:g} / $1</b>\n"
            f"💰 Send exactly: <b>₹{inr:,.2f}</b>\n"
            f"🏦 UPI ID: <code>{upi_id}</code>\n\n"
            "After payment, send a screenshot of the payment here."
        )
        qr = upi_qr
    else:
        if not usdt_address or usdt_address.startswith("0x000000"):
            await message.answer("❌ USDT BEP-20 payment is not configured by admin yet.")
            return
        text = (
            "🪙 <b>USDT BEP-20 Payment Details</b>\n\n"
            f"💵 USD deposit: <b>${amount}</b>\n"
            f"🪙 Send exactly: <b>{amount} USDT</b>\n"
            f"📍 USDT BEP-20 address: <code>{usdt_address}</code>\n\n"
            "After payment, send a screenshot of the payment here."
        )
        qr = usdt_qr

    await state.update_data(dep_amount=amount)
    if qr:
        try:
            await message.answer_photo(photo=qr, caption=text, parse_mode=ParseMode.HTML)
        except (TelegramBadRequest, TelegramForbiddenError):
            await message.answer(text + "\n\n⚠️ QR could not be displayed; use the address above.", parse_mode=ParseMode.HTML)
    else:
        await message.answer(text + "\n\n⚠️ QR code is not configured by admin.", parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.dep_proof)


@router.message(WalletStates.dep_proof, F.photo)
async def process_deposit_proof(message: Message, state: FSMContext):
    await state.update_data(dep_proof=message.photo[-1].file_id)
    await message.answer("🧾 Screenshot received. Now enter your <b>UTR / Transaction ID</b>.", parse_mode=ParseMode.HTML)
    await state.set_state(WalletStates.dep_utr)


@router.message(WalletStates.dep_proof)
async def process_deposit_proof_invalid(message: Message):
    await message.answer("❌ Please send the payment screenshot as a photo.")


@router.message(WalletStates.dep_utr, ~F.text.in_(ADMIN_MENU_BUTTONS))
async def process_deposit_utr(message: Message, state: FSMContext):
    user = message.from_user
    utr = (message.text or "").strip()
    if not 4 <= len(utr) <= 128:
        await message.answer("❌ Enter a valid UTR / Transaction ID.")
        return

    data = await state.get_data()
    amount = data.get("dep_amount")
    method = data.get("dep_method")
    proof = data.get("dep_proof")
    if not amount or method not in ("upi", "usdt_bep20") or not proof:
        await state.clear()
        await message.answer("❌ Deposit session expired. Please start again from /wallet.")
        return

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)
        details = f"utr={utr};proof_file_id={proof}"
        cur = conn.execute(
            "INSERT INTO transactions (user_id, tx_type, method, amount, details, status, created_at) VALUES (?, 'deposit', ?, ?, ?, 'pending', ?)",
            (user.id, method, amount, details, datetime.utcnow().isoformat()),
        )
        conn.commit()
        tx_id = cur.lastrowid

    await state.clear()
    await message.answer(
        f"✅ <b>Deposit request submitted.</b>\n\nTx ID: #{tx_id}\nAmount: ${amount}\nUTR/Txn ID: <code>{utr}</code>\n\n"
        "Your screenshot and transaction ID were sent to the admin for verification.",
        parse_mode=ParseMode.HTML,
    )
    await show_wallet(message)
    await notify_admins_new_tx(tx_id, "deposit", user, method, amount)


# ---------------------------------------------------------------------------
# Withdrawal flow
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("with_method:"))
async def process_with_method(call: CallbackQuery, state: FSMContext):
    await call.answer()
    method = call.data.split(":", 1)[1]
    await state.update_data(with_method=method)

    await call.message.edit_text(
        f"📤 <b>Withdrawal — {method.upper()}</b>\n\n"
        f"Minimum withdrawal: ${MIN_WITHDRAWAL}\n\n"
        "Enter the dollar amount you wish to withdraw.\n"
        "Example: <code>5</code>",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(WalletStates.with_amount)


@router.message(WalletStates.with_amount, ~F.text.in_(ADMIN_MENU_BUTTONS))
async def process_with_amount(message: Message, state: FSMContext):
    user = message.from_user

    try:
        raw = (message.text or "").strip().replace("$", "")
        amount = int(raw)
        if amount < MIN_WITHDRAWAL:
            raise ValueError
    except ValueError:
        await message.answer(f"❌ Minimum withdrawal is ${MIN_WITHDRAWAL}.\nEnter a valid dollar amount.")
        return

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)

        if is_user_banned(conn, user.id):
            await message.answer("🚫 You are banned from using this bot.")
            await state.clear()
            return

        balance = get_balance(conn, user.id)
        if balance < amount:
            await message.answer(f"❌ Insufficient funds.\nCurrent balance: ${balance}")
            await state.clear()
            return

    await state.update_data(with_amount=amount)
    data = await state.get_data()
    method = data.get("with_method")

    prompt = (
        "💳 Enter your UPI ID.\n\nYour UPI ID will be saved for future withdrawals."
        if method == "upi"
        else "🪙 Enter your USDT BEP-20 wallet address."
    )

    await message.answer(prompt)
    await state.set_state(WalletStates.with_address)


@router.message(WalletStates.with_address, ~F.text.in_(ADMIN_MENU_BUTTONS))
async def process_with_address(message: Message, state: FSMContext):
    user = message.from_user
    address = (message.text or "").strip()

    data = await state.get_data()
    amount = data.get("with_amount")
    method = data.get("with_method")

    if not address or amount is None or method is None:
        await message.answer("❌ Withdrawal session expired. Please use /wallet again.")
        await state.clear()
        return

    if method == "upi" and (" " in address or len(address) < 3):
        await message.answer("❌ Please enter a valid UPI ID.")
        return

    if method == "usdt_bep20" and len(address) < 20:
        await message.answer("❌ Please enter a valid BEP-20 wallet address.")
        return

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)
        balance = get_balance(conn, user.id)

        if balance < amount:
            await message.answer("❌ Insufficient balance. Your balance changed during processing.")
            await state.clear()
            return

        # Reserve funds immediately.
        adjust_balance(conn, user.id, -amount)

        if method == "upi":
            save_user_upi(conn, user.id, address)

        cursor = conn.execute(
            """
            INSERT INTO transactions (user_id, tx_type, method, amount, details, status, created_at)
            VALUES (?, 'withdrawal', ?, ?, ?, 'pending', ?)
            """,
            (user.id, method, amount, address, datetime.utcnow().isoformat()),
        )
        conn.commit()
        tx_id = cursor.lastrowid

    await state.clear()

    await message.answer(
        f"✅ <b>Withdrawal request submitted.</b>\n\n"
        f"Amount: ${amount}\n"
        f"Method: {method.upper()}\n"
        "Your funds are reserved until an admin approves or rejects the request.",
        parse_mode=ParseMode.HTML,
    )
    await show_wallet(message)
    await notify_admins_new_tx(tx_id, "withdrawal", user, method, amount, details=address)


# ---------------------------------------------------------------------------
# Group Betting
# ---------------------------------------------------------------------------

async def _create_bet_and_announce(chat_id: int, challenger, challenge_data: dict, amount: int, emoji: str, prediction: str):
    with closing(get_conn()) as conn:
        ensure_user(conn, challenger.id, challenger.username, challenger.first_name)

        if get_balance(conn, challenger.id) < amount:
            await bot.send_message(chat_id, f"❌ {challenger.first_name} has insufficient balance.")
            return

        cursor = conn.execute(
            """
            INSERT INTO bets
            (chat_id, challenger_id, challenger_name, opponent_id, opponent_name,
             amount, emoji, prediction, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                chat_id,
                challenger.id,
                challenger.username or challenger.first_name,
                challenge_data.get("opponent_id"),
                challenge_data.get("opponent_username"),
                amount,
                emoji,
                prediction,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        bet_id = cursor.lastrowid

    text = (
        f"🎲 <b>Bet #{bet_id} Active!</b>\n\n"
        f"👤 <b>Challenger:</b> {challenger.first_name}\n"
        f"🎯 <b>Target:</b> {challenge_data.get('opponent_display')}\n"
        f"💵 <b>Stake:</b> ${amount}\n"
        f"🎮 <b>Game:</b> {emoji}\n"
        f"🎯 <b>Prediction:</b> {prediction.upper()}\n\n"
        f"To accept, the target opponent can reply to this message "
        f"or use <code>/accept {bet_id}</code>."
    )

    sent = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE bets SET challenge_message_id=? WHERE bet_id=?",
            (sent.message_id, bet_id),
        )
        conn.commit()


@router.message(Command("bet"))
async def bet_start(message: Message, command: CommandObject, state: FSMContext):
    if message.chat.type == "private":
        await message.answer("⚠️ Betting features are available only inside the official group.")
        return

    challenger = message.from_user
    chat_id = message.chat.id

    with closing(get_conn()) as conn:
        ensure_user(conn, challenger.id, challenger.username, challenger.first_name)
        if is_user_banned(conn, challenger.id):
            await message.answer("🚫 You are banned from participating in games.")
            return

    args = (command.args or "").split() if command.args else []
    opponent_id = None
    opponent_username = None
    opponent_display = None
    remaining_args = args

    if args and args[0].startswith("@"):
        opponent_username = args[0].lstrip("@")

        with closing(get_conn()) as conn:
            opponent_id = find_user_id_by_username(conn, opponent_username)

        if opponent_id is None:
            await message.answer("❌ I could not find that user in the database. Ask them to use /start first.")
            return

        opponent_display = f"@{opponent_username}"
        remaining_args = args[1:]

    elif message.reply_to_message and message.reply_to_message.from_user:
        replied = message.reply_to_message.from_user

        if replied.is_bot or replied.id == challenger.id:
            await message.answer("❌ Invalid opponent target.")
            return

        opponent_id = replied.id
        opponent_username = replied.username
        opponent_display = f"@{replied.username}" if replied.username else replied.first_name

        with closing(get_conn()) as conn:
            ensure_user(conn, replied.id, replied.username, replied.first_name)

        remaining_args = args

    else:
        await message.answer(
            "Specify an opponent by tagging them:\n"
            "<code>/bet @username 2</code>\n\n"
            "Or reply to their message:\n"
            "<code>/bet 2</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    challenge_data = {
        "opponent_id": opponent_id,
        "opponent_username": opponent_username,
        "opponent_display": opponent_display,
        "challenger_id": challenger.id,
    }

    if len(remaining_args) >= 2:
        try:
            amount = int(remaining_args[0].replace("$", ""))

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
                        bal = get_balance(conn, challenger.id)
                    if bal < amount:
                        await message.answer(f"❌ Insufficient balance. Available: ${bal}")
                        return

                    await _create_bet_and_announce(chat_id, challenger, challenge_data, amount, emoji, prediction)
                    return
        except ValueError:
            pass

    await state.update_data(bet_challenge=challenge_data)
    await message.answer("💵 Enter the dollar amount for this bet:")
    await state.set_state(BetStates.ask_amount)


@router.message(BetStates.ask_amount, ~F.text.in_(ADMIN_MENU_BUTTONS))
async def ask_amount(message: Message, state: FSMContext):
    try:
        amount = int((message.text or "").strip().replace("$", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a positive dollar amount.")
        return

    challenger = message.from_user

    with closing(get_conn()) as conn:
        balance = get_balance(conn, challenger.id)

    if balance < amount:
        await message.answer(
            f"❌ Insufficient funds.\nAvailable balance: ${balance}",
        )
        return

    data = await state.get_data()
    challenge_data = data.get("bet_challenge", {})
    challenge_data["amount"] = amount
    await state.update_data(bet_challenge=challenge_data)

    await message.answer(
        "🎮 <b>Select game mode:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=game_select_keyboard(),
    )
    await state.set_state(BetStates.ask_game)


@router.callback_query(BetStates.ask_game, F.data.startswith("game:"))
async def ask_game(call: CallbackQuery, state: FSMContext):
    await call.answer()
    emoji = call.data.split(":", 1)[1]

    data = await state.get_data()
    challenge_data = data.get("bet_challenge", {})
    challenge_data["emoji"] = emoji
    await state.update_data(bet_challenge=challenge_data)

    await call.message.edit_text(
        f"Selected Game: {emoji}\n\nChoose your outcome prediction:",
        reply_markup=prediction_keyboard(),
    )
    await state.set_state(BetStates.ask_prediction)


@router.callback_query(BetStates.ask_prediction, F.data.startswith("pred:"))
async def ask_prediction(call: CallbackQuery, state: FSMContext):
    await call.answer()
    prediction = call.data.split(":", 1)[1]

    data = await state.get_data()
    challenge_data = data.get("bet_challenge", {})
    amount = challenge_data.get("amount")
    emoji = challenge_data.get("emoji")

    if amount is None or emoji is None:
        await call.message.edit_text("❌ Bet setup expired. Please start again.")
        await state.clear()
        return

    await call.message.edit_text("🎮 Initializing bet challenge...")

    await _create_bet_and_announce(call.message.chat.id, call.from_user, challenge_data, amount, emoji, prediction)
    await state.clear()


@router.message(Command("accept"))
async def accept_cmd(message: Message, command: CommandObject):
    if message.chat.type == "private":
        await message.answer("⚠️ Bet acceptance must occur inside the official group.")
        return

    chat_id = message.chat.id
    user = message.from_user
    bet_id = None

    if command.args:
        try:
            bet_id = int(command.args.strip().split()[0])
        except (ValueError, IndexError):
            await message.answer("❌ Invalid bet ID.")
            return

    elif message.reply_to_message:
        with closing(get_conn()) as conn:
            row = conn.execute(
                """
                SELECT bet_id FROM bets
                WHERE chat_id=? AND challenge_message_id=? AND status='pending'
                """,
                (chat_id, message.reply_to_message.message_id),
            ).fetchone()
            if row:
                bet_id = row["bet_id"]

    if not bet_id:
        await message.answer(
            "Reply to a bet challenge or use <code>/accept &lt;bet_id&gt;</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    with closing(get_conn()) as conn:
        ensure_user(conn, user.id, user.username, user.first_name)

        if is_user_banned(conn, user.id):
            await message.answer("🚫 You are banned from participating.")
            return

        bet = conn.execute("SELECT * FROM bets WHERE bet_id=? AND chat_id=?", (bet_id, chat_id)).fetchone()

        if not bet or bet["status"] != "pending":
            await message.answer("❌ This challenge is no longer available.")
            return

        if bet["opponent_id"] and bet["opponent_id"] != user.id:
            await message.answer("❌ You are not the designated opponent for this bet.")
            return

        if bet["challenger_id"] == user.id:
            await message.answer("❌ You cannot accept your own challenge.")
            return

        challenger_bal = get_balance(conn, bet["challenger_id"])
        opponent_bal = get_balance(conn, user.id)

        if challenger_bal < bet["amount"] or opponent_bal < bet["amount"]:
            await message.answer("❌ One or both players do not have enough balance.")
            return

        tax_percent = int(get_config_val(conn, "tax_percent") or "0")

        conn.execute(
            "UPDATE bets SET status='accepted', opponent_id=?, opponent_name=? WHERE bet_id=?",
            (user.id, user.username or user.first_name, bet_id),
        )
        conn.commit()

    dice_msg = await bot.send_dice(chat_id=chat_id, emoji=bet["emoji"])

    rolled_value = dice_msg.dice.value
    outcome = "even" if rolled_value % 2 == 0 else "odd"

    winner_id = bet["challenger_id"] if outcome == bet["prediction"] else user.id
    loser_id = user.id if winner_id == bet["challenger_id"] else bet["challenger_id"]

    raw_amount = int(bet["amount"])
    tax_amount = int((raw_amount * 2) * tax_percent / 100)
    payout = (raw_amount * 2) - tax_amount

    with closing(get_conn()) as conn:
        adjust_balance(conn, loser_id, -raw_amount)
        adjust_balance(conn, winner_id, payout)
        conn.execute(
            "UPDATE bets SET status='resolved', winner_id=?, tax_amount=? WHERE bet_id=?",
            (winner_id, tax_amount, bet_id),
        )
        conn.commit()

    await message.answer(
        f"🎯 <b>Outcome:</b> {rolled_value} ({outcome.upper()})\n\n"
        f"🏆 Winner: <a href='tg://user?id={winner_id}'>Player</a>\n"
        f"💵 Payout: ${payout}\n"
        f"📊 Platform tax: {tax_percent}%",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Admin Panel
# ---------------------------------------------------------------------------

def admin_only(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        await message.answer("Unauthorized access.")
        return

    await state.clear()
    await message.answer(
        "🔧 <b>Admin Operations Console</b>\n\nUse the buttons below to manage the bot.",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard(),
    )


@router.message(F.text == "🏠 Main Menu")
async def admin_main_menu(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    await state.clear()
    await message.answer("🏠 Back to normal mode.", reply_markup=ReplyKeyboardRemove())


@router.message(F.text.in_({"🟢 Bot Status: ON", "🔴 Bot Status: OFF"}))
async def admin_toggle_bot_status(message: Message):
    global BOT_STATUS
    if not admin_only(message.from_user.id):
        return
    BOT_STATUS = not BOT_STATUS
    await message.answer(
        f"Bot status is now: {'🟢 ON' if BOT_STATUS else '🔴 OFF'}",
        reply_markup=get_admin_menu_keyboard(),
    )


@router.message(F.text == "⚙️ Set Tax Rate")
async def admin_ask_tax(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    await message.answer(
        "Provide new global platform tax percentage (0 to 100):",
    )
    await state.set_state(AdminStates.set_tax)


@router.message(AdminStates.set_tax)
async def process_tax_set(message: Message, state: FSMContext):
    try:
        val = int((message.text or "").strip())
        if not 0 <= val <= 100:
            raise ValueError
    except ValueError:
        await message.answer("Please enter a percentage between 0 and 100.")
        return

    with closing(get_conn()) as conn:
        set_config_val(conn, "tax_percent", str(val))
        conn.commit()

    await state.clear()
    await message.answer(f"Global tax rate adjusted to {val}%.", reply_markup=get_admin_menu_keyboard())


@router.message(F.text == "🚫 Ban User")
async def admin_ask_ban(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    await message.answer("Provide target Telegram User ID to ban:")
    await state.set_state(AdminStates.ban_user)


@router.message(F.text == "✅ Unban User")
async def admin_ask_unban(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    await message.answer("Provide target Telegram User ID to unban:")
    await state.set_state(AdminStates.unban_user)


async def _set_ban_state(message: Message, state: FSMContext, banned: int):
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Please enter a numeric Telegram User ID.")
        return

    with closing(get_conn()) as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            await message.answer("Target User ID is not in the database.", reply_markup=get_admin_menu_keyboard())
            await state.clear()
            return

        conn.execute(
            "UPDATE users SET is_banned=?, updated_at=? WHERE user_id=?",
            (banned, datetime.utcnow().isoformat(), uid),
        )
        conn.commit()

    await state.clear()
    status = "BANNED" if banned else "UNBANNED"
    await message.answer(f"User ID {uid} has been set to: {status}.", reply_markup=get_admin_menu_keyboard())


@router.message(AdminStates.ban_user)
async def process_ban_user(message: Message, state: FSMContext):
    await _set_ban_state(message, state, 1)


@router.message(AdminStates.unban_user)
async def process_unban_user(message: Message, state: FSMContext):
    await _set_ban_state(message, state, 0)


@router.message(F.text.in_({"💳 Payment Gateways", "🔵 Payment Gateways"}))
async def admin_gateways(message: Message):
    if not admin_only(message.from_user.id):
        return
    await message.answer(
        "💳 <b>Payment Gateway Settings</b>\n\nConfigure UPI/USDT details, QR codes and the USD → INR rate.",
        parse_mode=ParseMode.HTML,
        reply_markup=gateway_keyboard(),
    )


@router.callback_query(F.data == "gateway:upi")
async def admin_set_upi_prompt(call: CallbackQuery, state: FSMContext):
    if not admin_only(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True); return
    await call.answer()
    await call.message.answer("Enter the platform UPI ID:")
    await state.set_state(AdminStates.set_upi)


@router.callback_query(F.data == "gateway:upi_qr")
async def admin_set_upi_qr_prompt(call: CallbackQuery, state: FSMContext):
    if not admin_only(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True); return
    await call.answer()
    await call.message.answer("Send the platform UPI QR code as a photo.")
    await state.set_state(AdminStates.set_upi_qr)


@router.callback_query(F.data == "gateway:usdt")
async def admin_set_usdt_prompt(call: CallbackQuery, state: FSMContext):
    if not admin_only(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True); return
    await call.answer()
    await call.message.answer("Enter the platform USDT BEP-20 receiving address:")
    await state.set_state(AdminStates.set_usdt)


@router.callback_query(F.data == "gateway:usdt_qr")
async def admin_set_usdt_qr_prompt(call: CallbackQuery, state: FSMContext):
    if not admin_only(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True); return
    await call.answer()
    await call.message.answer("Send the platform USDT BEP-20 QR code as a photo.")
    await state.set_state(AdminStates.set_usdt_qr)


@router.callback_query(F.data == "gateway:rate")
async def admin_set_rate_prompt(call: CallbackQuery, state: FSMContext):
    if not admin_only(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True); return
    await call.answer()
    await call.message.answer(
        "Enter the USD → INR rate used for UPI deposits.\nExample: <code>88.50</code> means $1 = ₹88.50.",
        parse_mode=ParseMode.HTML,
    )
    await state.set_state(AdminStates.set_rate)


@router.callback_query(F.data == "gateway:view")
async def admin_view_gateway_settings(call: CallbackQuery):
    if not admin_only(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True); return
    await call.answer()
    with closing(get_conn()) as conn:
        upi = get_config_val(conn, "upi_id")
        upi_qr = get_config_val(conn, "upi_qr_file_id")
        usdt = get_config_val(conn, "usdt_bep20_address")
        usdt_qr = get_config_val(conn, "usdt_qr_file_id")
        rate = get_config_val(conn, "usd_inr_rate") or "0"
    await call.message.answer(
        "💳 <b>Current Payment Settings</b>\n\n"
        f"💳 UPI ID: <code>{upi}</code>\n"
        f"🟢 UPI QR: {'Configured' if upi_qr else 'Not configured'}\n"
        f"🟢 USDT address: <code>{usdt}</code>\n"
        f"🟢 USDT QR: {'Configured' if usdt_qr else 'Not configured'}\n"
        f"💱 USD/INR rate: <b>₹{rate}</b>",
        parse_mode=ParseMode.HTML, reply_markup=gateway_keyboard(),
    )


@router.callback_query(F.data == "gateway:close")
async def admin_gateway_close(call: CallbackQuery):
    if not admin_only(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True); return
    await call.answer()
    await call.message.edit_text("💳 Payment gateway settings closed.")


@router.message(AdminStates.set_upi)
async def process_upi_set(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("UPI ID cannot be empty."); return
    with closing(get_conn()) as conn:
        set_config_val(conn, "upi_id", val); conn.commit()
    await state.clear()
    await message.answer(f"✅ UPI ID updated to: <code>{val}</code>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())


@router.message(AdminStates.set_upi_qr, F.photo)
async def process_upi_qr_set(message: Message, state: FSMContext):
    with closing(get_conn()) as conn:
        set_config_val(conn, "upi_qr_file_id", message.photo[-1].file_id); conn.commit()
    await state.clear()
    await message.answer("✅ UPI QR code updated.", reply_markup=get_admin_menu_keyboard())


@router.message(AdminStates.set_upi_qr)
async def process_upi_qr_invalid(message: Message):
    await message.answer("❌ Send the UPI QR code as a photo.")


@router.message(AdminStates.set_usdt)
async def process_usdt_set(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val:
        await message.answer("USDT address cannot be empty."); return
    with closing(get_conn()) as conn:
        set_config_val(conn, "usdt_bep20_address", val); conn.commit()
    await state.clear()
    await message.answer(f"✅ USDT BEP-20 address updated to: <code>{val}</code>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())


@router.message(AdminStates.set_usdt_qr, F.photo)
async def process_usdt_qr_set(message: Message, state: FSMContext):
    with closing(get_conn()) as conn:
        set_config_val(conn, "usdt_qr_file_id", message.photo[-1].file_id); conn.commit()
    await state.clear()
    await message.answer("✅ USDT BEP-20 QR code updated.", reply_markup=get_admin_menu_keyboard())


@router.message(AdminStates.set_usdt_qr)
async def process_usdt_qr_invalid(message: Message):
    await message.answer("❌ Send the USDT BEP-20 QR code as a photo.")


@router.message(AdminStates.set_rate)
async def process_rate_set(message: Message, state: FSMContext):
    try:
        val = float((message.text or "").strip().replace("₹", ""))
        if val <= 0 or val > 100000:
            raise ValueError
    except ValueError:
        await message.answer("❌ Enter a valid positive rate, e.g. <code>88.50</code>.", parse_mode=ParseMode.HTML); return
    with closing(get_conn()) as conn:
        set_config_val(conn, "usd_inr_rate", f"{val:.4f}".rstrip("0").rstrip(".")); conn.commit()
    await state.clear()
    await message.answer(f"✅ USD/INR rate updated: <b>₹{val:g} per $1</b>", parse_mode=ParseMode.HTML, reply_markup=get_admin_menu_keyboard())


@router.message(F.text == "📑 Pending Transactions")
async def admin_pending_txs(message: Message):
    if not admin_only(message.from_user.id):
        return

    with closing(get_conn()) as conn:
        txs = conn.execute(
            "SELECT * FROM transactions WHERE status='pending' ORDER BY tx_id ASC LIMIT 20"
        ).fetchall()

    if not txs:
        await message.answer("✅ The transaction approval queue is clean.")
        return

    for tx in txs:
        detail_info = f"\nDetails: {tx['details']}" if tx["details"] else ""
        await message.answer(
            f"Tx ID #{tx['tx_id']}\n"
            f"User: {tx['user_id']}\n"
            f"Type: {tx['tx_type'].upper()}\n"
            f"Method: {tx['method'].upper()}\n"
            f"Amount: ${tx['amount']}"
            f"{detail_info}",
            reply_markup=tx_approval_keyboard(tx["tx_id"]),
        )


@router.callback_query(F.data.startswith("tx:"))
async def handle_tx_approval(call: CallbackQuery):
    if not admin_only(call.from_user.id):
        await call.answer("Unauthorized.", show_alert=True)
        return

    await call.answer()

    try:
        _, action, tx_id_text = call.data.split(":")
        tx_id = int(tx_id_text)
    except (ValueError, TypeError):
        await call.message.edit_text("Invalid transaction action.")
        return

    with closing(get_conn()) as conn:
        tx = conn.execute("SELECT * FROM transactions WHERE tx_id=?", (tx_id,)).fetchone()

        if not tx or tx["status"] != "pending":
            await call.message.edit_text("Transaction state already finalized.")
            return

        if action == "app":
            new_status = "approved"
            if tx["tx_type"] == "deposit":
                adjust_balance(conn, tx["user_id"], tx["amount"])
        elif action == "rej":
            new_status = "rejected"
            if tx["tx_type"] == "withdrawal":
                adjust_balance(conn, tx["user_id"], tx["amount"])
        else:
            await call.message.edit_text("Invalid transaction action.")
            return

        conn.execute("UPDATE transactions SET status=? WHERE tx_id=?", (new_status, tx_id))
        conn.commit()

    await call.message.edit_text(f"Transaction #{tx_id} updated to: {new_status.upper()}.")

    tx_result = dict(tx)
    tx_result["status"] = new_status
    await notify_user_tx_result(tx_result)


# ---------------------------------------------------------------------------
# Admin: Balances / Find ID / Broadcast / Stats
# ---------------------------------------------------------------------------

@router.message(F.text == "➕ Add Balance")
async def admin_add_balance_ask_id(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    await message.answer("Enter target Telegram User ID to credit:")
    await state.set_state(AdminStates.add_balance_id)


@router.message(F.text == "➖ Cut Balance")
async def admin_cut_balance_ask_id(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    await message.answer("Enter target Telegram User ID to debit:")
    await state.set_state(AdminStates.cut_balance_id)


@router.message(AdminStates.add_balance_id)
async def admin_add_balance_ask_amount(message: Message, state: FSMContext):
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Please enter a numeric Telegram User ID.")
        return

    with closing(get_conn()) as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        await message.answer("Target User ID is not in the database.", reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    await state.update_data(target_id=uid)
    await message.answer(f"Enter dollar amount to ADD to user {uid}'s balance:")
    await state.set_state(AdminStates.add_balance_amount)


@router.message(AdminStates.cut_balance_id)
async def admin_cut_balance_ask_amount(message: Message, state: FSMContext):
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Please enter a numeric Telegram User ID.")
        return

    with closing(get_conn()) as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        await message.answer("Target User ID is not in the database.", reply_markup=get_admin_menu_keyboard())
        await state.clear()
        return

    await state.update_data(target_id=uid)
    await message.answer(f"Enter dollar amount to CUT from user {uid}'s balance:")
    await state.set_state(AdminStates.cut_balance_amount)


@router.message(AdminStates.add_balance_amount)
async def admin_add_balance_commit(message: Message, state: FSMContext):
    try:
        amount = int((message.text or "").strip().replace("$", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Please enter a positive dollar amount.")
        return

    data = await state.get_data()
    uid = data.get("target_id")

    with closing(get_conn()) as conn:
        adjust_balance(conn, uid, amount)
        conn.execute(
            "INSERT INTO transactions (user_id, tx_type, method, amount, details, status, created_at) "
            "VALUES (?, 'admin_credit', 'manual', ?, 'Admin balance credit', 'approved', ?)",
            (uid, amount, datetime.utcnow().isoformat()),
        )
        conn.commit()
        new_balance = get_balance(conn, uid)

    await state.clear()
    await message.answer(
        f"✅ Added ${amount} to user {uid}. New balance: ${new_balance}.",
        reply_markup=get_admin_menu_keyboard(),
    )
    try:
        await bot.send_message(uid, f"💰 An admin credited ${amount} to your wallet.")
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


@router.message(AdminStates.cut_balance_amount)
async def admin_cut_balance_commit(message: Message, state: FSMContext):
    try:
        amount = int((message.text or "").strip().replace("$", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Please enter a positive dollar amount.")
        return

    data = await state.get_data()
    uid = data.get("target_id")

    with closing(get_conn()) as conn:
        adjust_balance(conn, uid, -amount)
        conn.execute(
            "INSERT INTO transactions (user_id, tx_type, method, amount, details, status, created_at) "
            "VALUES (?, 'admin_debit', 'manual', ?, 'Admin balance deduction', 'approved', ?)",
            (uid, amount, datetime.utcnow().isoformat()),
        )
        conn.commit()
        new_balance = get_balance(conn, uid)

    await state.clear()
    await message.answer(
        f"✅ Cut ${amount} from user {uid}. New balance: ${new_balance}.",
        reply_markup=get_admin_menu_keyboard(),
    )
    try:
        await bot.send_message(uid, f"⚠️ An admin deducted ${amount} from your wallet.")
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


@router.message(F.text == "🔎 Check Balance")
async def admin_check_balance_ask(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    await message.answer("Enter Telegram User ID to check:")
    await state.set_state(AdminStates.check_balance)


@router.message(AdminStates.check_balance)
async def admin_check_balance_commit(message: Message, state: FSMContext):
    try:
        uid = int((message.text or "").strip())
    except ValueError:
        await message.answer("Please enter a numeric Telegram User ID.")
        return

    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT username, first_name, balance, is_banned FROM users WHERE user_id=?", (uid,)
        ).fetchone()

    await state.clear()

    if not row:
        await message.answer("Target User ID is not in the database.", reply_markup=get_admin_menu_keyboard())
        return

    username = f"@{row['username']}" if row["username"] else (row["first_name"] or "Unknown")
    await message.answer(
        f"👤 {username} (<code>{uid}</code>)\n"
        f"💵 Balance: ${row['balance']}\n"
        f"🚫 Banned: {'Yes' if row['is_banned'] else 'No'}",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard(),
    )


@router.message(F.text == "🏆 Top Balances")
async def admin_top_balances(message: Message):
    if not admin_only(message.from_user.id):
        return

    with closing(get_conn()) as conn:
        rows = top_balances(conn, limit=10)

    if not rows:
        await message.answer("No users yet.")
        return

    lines = ["🏆 <b>Top Balances</b>", ""]
    for i, row in enumerate(rows, start=1):
        username = f"@{row['username']}" if row["username"] else (row["first_name"] or "Unknown")
        lines.append(f"{i}. {username} (<code>{row['user_id']}</code>) — ${row['balance']}")

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(F.text == "🔍 Find ID")
async def admin_find_id_ask(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    await message.answer("Enter the @username to look up:")
    await state.set_state(AdminStates.find_id)


@router.message(AdminStates.find_id)
async def admin_find_id_commit(message: Message, state: FSMContext):
    username = (message.text or "").strip().lstrip("@")

    with closing(get_conn()) as conn:
        uid = find_user_id_by_username(conn, username)

    await state.clear()

    if uid is None:
        await message.answer(f"❌ No user found with username @{username}.", reply_markup=get_admin_menu_keyboard())
        return

    await message.answer(
        f"✅ @{username} → <code>{uid}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=get_admin_menu_keyboard(),
    )


@router.message(F.text == "📢 Broadcast")
async def admin_broadcast_ask(message: Message, state: FSMContext):
    if not admin_only(message.from_user.id):
        return
    await message.answer(
        "Send the message you want broadcast to all known users:",
    )
    await state.set_state(AdminStates.broadcast)


@router.message(AdminStates.broadcast)
async def admin_broadcast_commit(message: Message, state: FSMContext):
    await state.clear()

    with closing(get_conn()) as conn:
        user_ids = all_user_ids(conn)

    status_msg = await message.answer(f"📢 Broadcasting to {len(user_ids)} users...")

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await message.copy_to(chat_id=uid)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        await asyncio.sleep(0.05)  # gentle pacing to stay under flood limits

    await status_msg.edit_text(
        f"📢 Broadcast complete.\n✅ Delivered: {sent}\n❌ Failed: {failed}"
    )


@router.message(F.text == "📊 View Stats")
async def admin_view_stats(message: Message):
    if not admin_only(message.from_user.id):
        return

    with closing(get_conn()) as conn:
        s = stats_snapshot(conn)

    await message.answer(
        "📊 <b>Bot Stats</b>\n\n"
        f"👥 Total users: {s['total_users']}\n"
        f"🚫 Banned users: {s['banned_users']}\n"
        f"💰 Combined balance: ${s['total_balance']}\n"
        f"🎮 Total bets placed: {s['total_bets']}\n"
        f"✅ Bets resolved: {s['resolved_bets']}\n"
        f"💸 Total tax collected: ${s['total_tax']}\n"
        f"📑 Pending transactions: {s['pending_tx']}",
        parse_mode=ParseMode.HTML,
    )


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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server started on port %s", port)


# ---------------------------------------------------------------------------
# Fallback: catch admin-only buttons tapped by non-admins silently
# ---------------------------------------------------------------------------

@router.message(F.text.in_(ADMIN_MENU_BUTTONS))
async def admin_button_guard(message: Message):
    # Reachable only if none of the admin_only-gated handlers above matched
    # first (i.e. a non-admin somehow has the admin keyboard open).
    if not admin_only(message.from_user.id):
        await message.answer("Unauthorized access.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    init_db()
    start_health_server()
    logger.info("BlockVerse-BOT successfully initialized.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
