import logging
import os
import threading
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg2
from psycopg2.extras import RealDictCursor
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
# Logging / configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("betbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
GROUP_LINK = os.getenv("GROUP_LINK", "https://t.me/your_group_link").strip()

# All wallet/bet/transaction balances are stored as US dollars in cents.
# Example: $2.50 is stored as 250.
STARTING_BALANCE_CENTS = 0
MIN_WITHDRAWAL_CENTS = 200

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

GAMES = {
    "🎲": "Dice",
    "🎯": "Darts",
    "🎳": "Bowling",
    "🏀": "Basketball",
    "⚽": "Football",
    "🎰": "Slots",
}
VALID_EMOJIS = set(GAMES)

(
    ASK_AMOUNT,
    ASK_GAME,
    ASK_PREDICTION,
    DEP_METHOD,
    DEP_AMOUNT,
    WITH_METHOD,
    WITH_AMOUNT,
    WITH_ADDRESS,
    SET_TAX,
    BAN_USER,
    SET_UPI,
    SET_USDT,
) = range(12)


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------

def money_to_cents(value):
    """Convert a user-entered dollar amount to integer cents."""
    try:
        text = str(value).strip().replace("$", "")
        if not text:
            raise InvalidOperation
        amount = Decimal(text)
        if amount <= 0:
            raise InvalidOperation
        if amount.as_tuple().exponent < -2:
            raise InvalidOperation
        cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_DOWN))
        if cents <= 0:
            raise InvalidOperation
        return cents
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Invalid dollar amount")


def money(cents):
    """Format integer cents as $X or $X.XX."""
    cents = int(cents or 0)
    return f"${cents / 100:.2f}" if cents % 100 else f"${cents // 100}"


def clean_text(value, max_len=200):
    value = str(value or "").strip()
    return value[:max_len]


# ---------------------------------------------------------------------------
# PostgreSQL database layer
# ---------------------------------------------------------------------------

def conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    with closing(conn()) as db:
        with db.cursor() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    balance BIGINT NOT NULL DEFAULT 0,
                    is_banned BOOLEAN NOT NULL DEFAULT FALSE,
                    upi_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

            # Safe upgrade for databases created by the previous version.
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS upi_id TEXT")
            c.execute("ALTER TABLE users ALTER COLUMN balance SET DEFAULT 0")

            c.execute(
                """
                CREATE TABLE IF NOT EXISTS bets (
                    bet_id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    challenger_id BIGINT NOT NULL,
                    challenger_name TEXT,
                    opponent_id BIGINT,
                    opponent_name TEXT,
                    amount BIGINT NOT NULL,
                    emoji TEXT NOT NULL,
                    prediction TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    winner_id BIGINT,
                    challenge_message_id BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    accepted_at TIMESTAMPTZ,
                    resolved_at TIMESTAMPTZ
                )
                """
            )

            c.execute(
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
                    processed_at TIMESTAMPTZ
                )
                """
            )

            c.execute(
                """
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            c.execute(
                "INSERT INTO system_config(key,value) VALUES('tax_percent','0') "
                "ON CONFLICT(key) DO NOTHING"
            )
            c.execute(
                "INSERT INTO system_config(key,value) VALUES('upi_id','not_set@upi') "
                "ON CONFLICT(key) DO NOTHING"
            )
            c.execute(
                "INSERT INTO system_config(key,value) VALUES('usdt_bep20_address','not_set') "
                "ON CONFLICT(key) DO NOTHING"
            )

            # Helpful indexes for history/queue lookups.
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_bets_challenger ON bets(challenger_id, bet_id DESC)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_bets_opponent ON bets(opponent_id, bet_id DESC)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id, tx_id DESC)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status, tx_id)"
            )
        db.commit()


def ensure_user(db, user):
    with db.cursor() as c:
        c.execute(
            """
            INSERT INTO users(user_id,username,first_name,balance)
            VALUES(%s,%s,%s,%s)
            ON CONFLICT(user_id) DO UPDATE SET
                username=COALESCE(EXCLUDED.username,users.username),
                first_name=COALESCE(EXCLUDED.first_name,users.first_name),
                updated_at=NOW()
            RETURNING balance,is_banned,upi_id
            """,
            (user.id, user.username, user.first_name, STARTING_BALANCE_CENTS),
        )
        return c.fetchone()


def banned(db, uid):
    with db.cursor() as c:
        c.execute("SELECT is_banned FROM users WHERE user_id=%s", (uid,))
        row = c.fetchone()
        return bool(row and row["is_banned"])


def balance(db, uid):
    with db.cursor() as c:
        c.execute("SELECT balance FROM users WHERE user_id=%s", (uid,))
        row = c.fetchone()
        return int(row["balance"]) if row else 0


def config(db, key):
    with db.cursor() as c:
        c.execute("SELECT value FROM system_config WHERE key=%s", (key,))
        row = c.fetchone()
        return row["value"] if row else ""


def set_config(db, key, value):
    with db.cursor() as c:
        c.execute(
            """
            INSERT INTO system_config(key,value) VALUES(%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
            """,
            (key, value),
        )


def get_saved_upi(db, uid):
    with db.cursor() as c:
        c.execute("SELECT upi_id FROM users WHERE user_id=%s", (uid,))
        row = c.fetchone()
        return (row["upi_id"] or "").strip() if row else ""


# ---------------------------------------------------------------------------
# Wallet dashboard
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    with closing(conn()) as db:
        ensure_user(db, user)
        if banned(db, user.id):
            await update.effective_message.reply_text(
                "🚫 You are banned from using this bot."
            )
            return
        db.commit()

    if update.effective_chat.type == "private":
        await dashboard(update, context)
    else:
        await update.effective_message.reply_text(
            "🎮 Betting Bot is active!\n\n"
            "Use /bet @username $2 🎲 even\n"
            "or reply to a player's message with /bet $2."
        )


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    with closing(conn()) as db:
        ensure_user(db, user)
        bal = balance(db, user.id)
        upi = get_saved_upi(db, user.id)
        db.commit()

    upi_line = f"💳 UPI: `{upi}`" if upi else "❌ UPI: Not saved yet"

    text = (
        "🏦 *Your Wallet*\n\n"
        f"💵 Balance: *{money(bal)}*\n"
        f"{upi_line}\n\n"
        "Min withdrawal: *$2*"
    )

    # Telegram clients control the visual color of inline buttons.
    # The labels use 🟢/🔵 to make the requested green/blue distinction clear.
    keyboard = [
        [InlineKeyboardButton("🟢 Deposit", callback_data="wallet:deposit")],
        [InlineKeyboardButton("🔵 Withdrawal", callback_data="wallet:withdraw")],
        [InlineKeyboardButton("🔵 History", callback_data="wallet:history")],
        [InlineKeyboardButton("🔵 Usage", callback_data="wallet:usage")],
        [InlineKeyboardButton("Official Group", url=GROUP_LINK)],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=markup
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="Markdown", reply_markup=markup
        )


# ---------------------------------------------------------------------------
# Wallet history / usage
# ---------------------------------------------------------------------------

async def wallet_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user

    with closing(conn()) as db:
        ensure_user(db, user)
        with db.cursor() as c:
            c.execute(
                """
                SELECT bet_id, challenger_id, opponent_id, amount, emoji,
                       prediction, status, winner_id, created_at, resolved_at
                FROM bets
                WHERE challenger_id=%s OR opponent_id=%s
                ORDER BY bet_id DESC
                """,
                (user.id, user.id),
            )
            bets = c.fetchall()

            c.execute(
                """
                SELECT tx_id, tx_type, method, amount, details, status,
                       created_at, processed_at
                FROM transactions
                WHERE user_id=%s
                ORDER BY tx_id DESC
                """,
                (user.id,),
            )
            txs = c.fetchall()
        db.commit()

    lines = ["📜 *Full History*", ""]

    if bets:
        lines.append("🎮 *Betting History*")
        for b in bets:
            role = "Challenger" if b["challenger_id"] == user.id else "Opponent"
            result = "Pending"
            if b["status"] == "resolved":
                result = "🏆 Won" if b["winner_id"] == user.id else "❌ Lost"
            elif b["status"] == "cancelled":
                result = "Cancelled"
            lines.append(
                f"#{b['bet_id']} • {money(b['amount'])} • {b['emoji']} {GAMES.get(b['emoji'], 'Game')}\n"
                f"   {role} • {b['prediction'].upper()} • {result}"
            )
    else:
        lines.append("🎮 *Betting History*\nNo bets yet.")

    lines.append("")
    if txs:
        lines.append("💳 *Deposits & Withdrawals*")
        for t in txs:
            direction = t["tx_type"].title()
            details = f" • {t['details']}" if t["details"] else ""
            lines.append(
                f"Tx #{t['tx_id']} • {direction} • {money(t['amount'])} • "
                f"{t['method'].upper()} • {t['status'].title()}{details}"
            )
    else:
        lines.append("💳 *Deposits & Withdrawals*\nNo transactions yet.")

    text = "\n".join(lines)

    # Telegram messages have a size limit. Split history into safe chunks.
    chunks = []
    while len(text) > 3800:
        cut = text.rfind("\n", 0, 3800)
        if cut <= 0:
            cut = 3800
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)

    await q.edit_message_text(chunks[0], parse_mode="Markdown")
    for chunk in chunks[1:]:
        await q.message.chat.send_message(chunk, parse_mode="Markdown")

    await q.message.chat.send_message(
        "Use the button below to return to your wallet.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏦 Back to Wallet", callback_data="wallet:back")]]
        ),
    )


async def wallet_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    text = (
        "📘 *Usage Instructions*\n\n"
        "🏦 *Wallet*\n"
        "Use /wallet in DM to view your balance, saved UPI, history, deposits and withdrawals.\n\n"
        "📥 *Deposit*\n"
        "1. Tap Deposit.\n"
        "2. Select UPI or USDT BEP-20.\n"
        "3. Send the payment to the displayed platform address.\n"
        "4. Enter the dollar amount deposited.\n"
        "5. An admin must approve the request before funds are credited.\n\n"
        "📤 *Withdrawal*\n"
        "1. Tap Withdrawal.\n"
        "2. Minimum withdrawal is $2.\n"
        "3. Select UPI or USDT BEP-20.\n"
        "4. Enter the amount and payout address/details.\n"
        "5. Withdrawal funds are reserved until admin approval.\n\n"
        "🎮 *How to Bet*\n"
        "Betting is available in the official group only.\n"
        "Use `/bet @username $2 🎲 even` or reply to a player's message with `/bet $2`.\n"
        "Choose a game and predict EVEN or ODD. The Telegram game roll determines the result.\n\n"
        "💡 *Important*\n"
        "Only bet amounts you can afford to lose. Check your balance and transaction history regularly."
    )

    await q.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏦 Back to Wallet", callback_data="wallet:back")]]
        ),
    )


# ---------------------------------------------------------------------------
# Deposit / withdrawal workflows
# ---------------------------------------------------------------------------

async def wallet_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "wallet:deposit":
        keyboard = [
            [InlineKeyboardButton("UPI", callback_data="dep:upi")],
            [InlineKeyboardButton("USDT BEP-20", callback_data="dep:usdt")],
        ]
        await q.edit_message_text(
            "📥 *Deposit*\n\nSelect your deposit method:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return DEP_METHOD

    if q.data == "wallet:withdraw":
        keyboard = [
            [InlineKeyboardButton("UPI", callback_data="with:upi")],
            [InlineKeyboardButton("USDT BEP-20", callback_data="with:usdt")],
        ]
        await q.edit_message_text(
            "📤 *Withdrawal*\n\nMinimum withdrawal: *$2*\n\nSelect your withdrawal method:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return WITH_METHOD

    if q.data == "wallet:history":
        await wallet_history(update, context)
        return ConversationHandler.END

    if q.data == "wallet:usage":
        await wallet_usage(update, context)
        return ConversationHandler.END

    if q.data == "wallet:back":
        await dashboard(update, context)
        return ConversationHandler.END

    return ConversationHandler.END


async def dep_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    method = q.data.split(":", 1)[1]
    context.user_data["dep_method"] = method

    with closing(conn()) as db:
        address = config(db, "upi_id") if method == "upi" else config(db, "usdt_bep20_address")

    method_name = "UPI" if method == "upi" else "USDT BEP-20"
    await q.edit_message_text(
        f"📥 *{method_name} Deposit*\n\n"
        f"Send payment to:\n`{address}`\n\n"
        "After payment, enter the amount in USD (example: `5` or `5.50`).",
        parse_mode="Markdown",
    )
    return DEP_AMOUNT


async def dep_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = money_to_cents(update.effective_message.text)
    except ValueError:
        await update.effective_message.reply_text(
            "Enter a valid dollar amount, for example $5 or $5.50."
        )
        return DEP_AMOUNT

    method = context.user_data.get("dep_method")
    if method not in ("upi", "usdt"):
        await update.effective_message.reply_text("Deposit session expired. Start again with /wallet.")
        return ConversationHandler.END

    user = update.effective_user
    with closing(conn()) as db:
        ensure_user(db, user)
        with db.cursor() as c:
            c.execute(
                """
                INSERT INTO transactions(user_id,tx_type,method,amount,status)
                VALUES(%s,'deposit',%s,%s,'pending')
                """,
                (user.id, method, amount),
            )
        db.commit()

    context.user_data.clear()
    await update.effective_message.reply_text(
        f"✅ Deposit request for {money(amount)} submitted for admin approval."
    )
    await dashboard(update, context)
    return ConversationHandler.END


async def with_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    method = q.data.split(":", 1)[1]
    context.user_data["with_method"] = method

    await q.edit_message_text(
        "📤 *Withdrawal*\n\n"
        "Enter the amount you want to withdraw.\n"
        "Minimum withdrawal: *$2*\n\n"
        "Example: `2` or `10.50`",
        parse_mode="Markdown",
    )
    return WITH_AMOUNT


async def with_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = money_to_cents(update.effective_message.text)
    except ValueError:
        await update.effective_message.reply_text(
            "Enter a valid dollar amount, for example $2 or $10.50."
        )
        return WITH_AMOUNT

    if amount < MIN_WITHDRAWAL_CENTS:
        await update.effective_message.reply_text(
            "❌ Minimum withdrawal is $2."
        )
        return WITH_AMOUNT

    user = update.effective_user
    with closing(conn()) as db:
        ensure_user(db, user)
        bal = balance(db, user.id)
        db.commit()

    if bal < amount:
        await update.effective_message.reply_text(
            f"❌ Insufficient balance. Current balance: {money(bal)}"
        )
        return WITH_AMOUNT

    context.user_data["with_amount"] = amount
    method = context.user_data.get("with_method")

    if method == "upi":
        with closing(conn()) as db:
            saved = get_saved_upi(db, user.id)
        if saved:
            await update.effective_message.reply_text(
                f"Your saved UPI ID is `{saved}`.\n\n"
                "Send the UPI ID to use for this withdrawal, or enter the same ID to keep it saved.",
                parse_mode="Markdown",
            )
        else:
            await update.effective_message.reply_text("Enter your UPI ID:")
    else:
        await update.effective_message.reply_text("Enter your USDT BEP-20 wallet address:")
    return WITH_ADDRESS


async def with_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    address = clean_text(update.effective_message.text, 200)
    amount = context.user_data.get("with_amount")
    method = context.user_data.get("with_method")

    if not address or not amount or method not in ("upi", "usdt"):
        await update.effective_message.reply_text(
            "Invalid withdrawal session. Start again with /wallet."
        )
        return ConversationHandler.END

    with closing(conn()) as db:
        ensure_user(db, user)
        with db.cursor() as c:
            # Lock the wallet row so two withdrawals cannot spend the same balance.
            c.execute(
                "SELECT balance FROM users WHERE user_id=%s FOR UPDATE",
                (user.id,),
            )
            row = c.fetchone()
            if not row or int(row["balance"]) < int(amount):
                db.rollback()
                await update.effective_message.reply_text("❌ Insufficient funds.")
                return ConversationHandler.END

            c.execute(
                "UPDATE users SET balance=balance-%s,updated_at=NOW() WHERE user_id=%s",
                (amount, user.id),
            )

            c.execute(
                """
                INSERT INTO transactions(user_id,tx_type,method,amount,details,status)
                VALUES(%s,'withdrawal',%s,%s,%s,'pending')
                """,
                (user.id, method, amount, address),
            )

            # Save UPI for the wallet display and future withdrawals.
            if method == "upi":
                c.execute(
                    "UPDATE users SET upi_id=%s,updated_at=NOW() WHERE user_id=%s",
                    (address, user.id),
                )
        db.commit()

    context.user_data.clear()
    await update.effective_message.reply_text(
        f"✅ Withdrawal request for {money(amount)} submitted.\n"
        "Your funds are reserved until an admin approves or rejects it."
    )
    await dashboard(update, context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Group betting
# ---------------------------------------------------------------------------

async def bet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(
            "Betting is available only in the official group."
        )
        return ConversationHandler.END

    user = update.effective_user
    with closing(conn()) as db:
        ensure_user(db, user)
        if banned(db, user.id):
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
                c.execute(
                    "SELECT user_id FROM users WHERE username ILIKE %s LIMIT 1",
                    (opponent_username,),
                )
                row = c.fetchone()
        if not row:
            await update.effective_message.reply_text(
                "That player is not registered. Ask them to /start the bot first."
            )
            return ConversationHandler.END
        opponent_id = row["user_id"]
        opponent_display = "@" + opponent_username
        rest = args[1:]

    elif update.effective_message.reply_to_message and update.effective_message.reply_to_message.from_user:
        player = update.effective_message.reply_to_message.from_user
        if player.is_bot or player.id == user.id:
            await update.effective_message.reply_text("Invalid opponent.")
            return ConversationHandler.END
        opponent_id = player.id
        opponent_username = player.username
        opponent_display = "@" + player.username if player.username else player.first_name
        with closing(conn()) as db:
            ensure_user(db, player)
            db.commit()
    else:
        await update.effective_message.reply_text(
            "Use /bet @username $2, or reply to a player's message with /bet $2."
        )
        return ConversationHandler.END

    context.user_data["challenge"] = {
        "opponent_id": opponent_id,
        "opponent_username": opponent_username,
        "opponent_display": opponent_display,
    }

    if rest:
        try:
            amount = money_to_cents(rest[0])
            emoji = "🎲"
            prediction = None
            for item in rest[1:]:
                if item in VALID_EMOJIS:
                    emoji = item
                elif item.lower() in ("even", "odd"):
                    prediction = item.lower()

            if prediction:
                with closing(conn()) as db:
                    if balance(db, user.id) < amount:
                        await update.effective_message.reply_text("❌ Insufficient balance.")
                        return ConversationHandler.END
                await create_bet(update, context, amount, emoji, prediction)
                context.user_data.pop("challenge", None)
                return ConversationHandler.END
        except ValueError:
            pass

    await update.effective_message.reply_text(
        "Enter stake amount in USD, for example $2 or $2.50:",
        reply_markup=ForceReply(selective=True),
    )
    return ASK_AMOUNT


async def ask_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = money_to_cents(update.effective_message.text)
    except ValueError:
        await update.effective_message.reply_text(
            "Enter a valid dollar amount, for example $2 or $5.50."
        )
        return ASK_AMOUNT

    with closing(conn()) as db:
        if balance(db, update.effective_user.id) < amount:
            await update.effective_message.reply_text("❌ Insufficient balance.")
            return ASK_AMOUNT

    context.user_data["challenge"]["amount"] = amount
    keyboard = [
        [InlineKeyboardButton(f"{emoji} {name}", callback_data=f"game:{emoji}")]
        for emoji, name in GAMES.items()
    ]
    await update.effective_message.reply_text(
        "Select game:", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ASK_GAME


async def ask_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    emoji = q.data.split(":", 1)[1]
    context.user_data["challenge"]["emoji"] = emoji
    await q.edit_message_text(
        "Choose prediction:",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("EVEN", callback_data="pred:even"),
                InlineKeyboardButton("ODD", callback_data="pred:odd"),
            ]]
        ),
    )
    return ASK_PREDICTION


async def ask_prediction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    prediction = q.data.split(":", 1)[1]
    data = context.user_data.get("challenge", {})

    if not data.get("amount") or not data.get("emoji"):
        await q.edit_message_text("Bet session expired. Start again.")
        context.user_data.pop("challenge", None)
        return ConversationHandler.END

    await q.edit_message_text("Creating challenge...")
    await create_bet(update, context, data["amount"], data["emoji"], prediction)
    context.user_data.pop("challenge", None)
    return ConversationHandler.END


async def create_bet(update, context, amount, emoji, prediction):
    user = update.effective_user
    data = context.user_data["challenge"]

    with closing(conn()) as db:
        ensure_user(db, user)
        with db.cursor() as c:
            c.execute(
                "SELECT balance FROM users WHERE user_id=%s FOR UPDATE",
                (user.id,),
            )
            row = c.fetchone()
            if not row or int(row["balance"]) < amount:
                db.rollback()
                await update.effective_message.reply_text("❌ Insufficient balance.")
                return

            c.execute(
                """
                INSERT INTO bets(
                    chat_id,challenger_id,challenger_name,opponent_id,
                    opponent_name,amount,emoji,prediction
                )
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING bet_id
                """,
                (
                    update.effective_chat.id,
                    user.id,
                    user.username or user.first_name,
                    data.get("opponent_id"),
                    data.get("opponent_username"),
                    amount,
                    emoji,
                    prediction,
                ),
            )
            bet_id = c.fetchone()["bet_id"]
        db.commit()

    message = await context.bot.send_message(
        update.effective_chat.id,
        "🎲 *BET #{bet_id}*\n\n"
        "👤 Challenger: {name}\n"
        "🎯 Target: {target}\n"
        "💵 Stake: {amount}\n"
        "🎮 Game: {emoji} {game}\n"
        "🔮 Prediction: {prediction}\n\n"
        "Reply to this message with /accept or use /accept {bet_id}".format(
            bet_id=bet_id,
            name=user.first_name,
            target=data["opponent_display"],
            amount=money(amount),
            emoji=emoji,
            game=GAMES[emoji],
            prediction=prediction.upper(),
        ),
        parse_mode="Markdown",
    )

    with closing(conn()) as db:
        with db.cursor() as c:
            c.execute(
                "UPDATE bets SET challenge_message_id=%s WHERE bet_id=%s",
                (message.message_id, bet_id),
            )
        db.commit()


async def accept_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(
            "Acceptance must happen in the group."
        )
        return

    user = update.effective_user
    chat_id = update.effective_chat.id
    bet_id = None

    with closing(conn()) as db:
        ensure_user(db, user)
        if banned(db, user.id):
            await update.effective_message.reply_text("🚫 You are banned from betting.")
            db.rollback()
            return

        if context.args:
            try:
                bet_id = int(context.args[0])
            except ValueError:
                pass
        elif update.effective_message.reply_to_message:
            with db.cursor() as c:
                c.execute(
                    """
                    SELECT bet_id FROM bets
                    WHERE chat_id=%s AND challenge_message_id=%s AND status='pending'
                    """,
                    (chat_id, update.effective_message.reply_to_message.message_id),
                )
                row = c.fetchone()
                bet_id = row["bet_id"] if row else None

        if not bet_id:
            await update.effective_message.reply_text(
                "Use /accept BET_ID or reply to the bet message with /accept."
            )
            db.rollback()
            return

        with db.cursor() as c:
            c.execute(
                "SELECT * FROM bets WHERE bet_id=%s AND chat_id=%s FOR UPDATE",
                (bet_id, chat_id),
            )
            bet = c.fetchone()

            if not bet or bet["status"] != "pending":
                db.rollback()
                await update.effective_message.reply_text("Bet is no longer available.")
                return

            if bet["opponent_id"] and bet["opponent_id"] != user.id:
                db.rollback()
                await update.effective_message.reply_text("You are not the designated opponent.")
                return

            if bet["challenger_id"] == user.id:
                db.rollback()
                await update.effective_message.reply_text("You cannot accept your own bet.")
                return

            c.execute(
                "SELECT balance FROM users WHERE user_id=%s FOR UPDATE",
                (bet["challenger_id"],),
            )
            challenger_row = c.fetchone()
            c.execute(
                "SELECT balance FROM users WHERE user_id=%s FOR UPDATE",
                (user.id,),
            )
            opponent_row = c.fetchone()

            amount = int(bet["amount"])
            if (
                not challenger_row
                or not opponent_row
                or int(challenger_row["balance"]) < amount
                or int(opponent_row["balance"]) < amount
            ):
                db.rollback()
                await update.effective_message.reply_text(
                    "❌ One or both players lack sufficient balance."
                )
                return

            c.execute(
                """
                UPDATE bets
                SET status='accepted', opponent_id=%s, opponent_name=%s, accepted_at=NOW()
                WHERE bet_id=%s
                """,
                (user.id, user.username or user.first_name, bet_id),
            )
        db.commit()

    # Roll outside the DB transaction so the database is not held open while Telegram responds.
    try:
        dice = await context.bot.send_dice(chat_id=chat_id, emoji=bet["emoji"])
        value = dice.dice.value
    except Exception:
        with closing(conn()) as db:
            with db.cursor() as c:
                c.execute(
                    """
                    UPDATE bets
                    SET status='pending',opponent_id=%s,opponent_name=%s,accepted_at=NULL
                    WHERE bet_id=%s AND status='accepted'
                    """,
                    (bet["opponent_id"], bet["opponent_name"], bet_id),
                )
            db.commit()
        await update.effective_message.reply_text(
            "Telegram roll failed; the bet was returned to pending."
        )
        return

    outcome = "even" if value % 2 == 0 else "odd"
    winner = bet["challenger_id"] if outcome == bet["prediction"] else user.id
    loser = user.id if winner == bet["challenger_id"] else bet["challenger_id"]
    amount = int(bet["amount"])

    with closing(conn()) as db:
        with db.cursor() as c:
            c.execute("SELECT value FROM system_config WHERE key='tax_percent'")
            tax_row = c.fetchone()
            try:
                tax = max(0, min(100, int(tax_row["value"])))
            except (ValueError, TypeError):
                tax = 0

            c.execute(
                "SELECT user_id,balance FROM users WHERE user_id IN (%s,%s) FOR UPDATE",
                (bet["challenger_id"], user.id),
            )
            rows = c.fetchall()
            balances = {r["user_id"]: int(r["balance"]) for r in rows}

            if (
                balances.get(bet["challenger_id"], 0) < amount
                or balances.get(user.id, 0) < amount
            ):
                c.execute(
                    "UPDATE bets SET status='cancelled',resolved_at=NOW() WHERE bet_id=%s",
                    (bet_id,),
                )
                db.commit()
                await update.effective_message.reply_text(
                    "Bet cancelled because a player no longer has enough balance."
                )
                return

            c.execute(
                "UPDATE users SET balance=balance-%s,updated_at=NOW() WHERE user_id=%s",
                (amount, bet["challenger_id"]),
            )
            c.execute(
                "UPDATE users SET balance=balance-%s,updated_at=NOW() WHERE user_id=%s",
                (amount, user.id),
            )

            pot = amount * 2
            tax_amount = pot * tax // 100
            payout = pot - tax_amount

            c.execute(
                "UPDATE users SET balance=balance+%s,updated_at=NOW() WHERE user_id=%s",
                (payout, winner),
            )
            c.execute(
                """
                UPDATE bets
                SET status='resolved',winner_id=%s,resolved_at=NOW()
                WHERE bet_id=%s
                """,
                (winner, bet_id),
            )
        db.commit()

    await update.effective_message.reply_text(
        f"🎯 Result: {value} ({outcome.upper()})\n"
        f"🏆 Winner: {winner}\n"
        f"💵 Pot: {money(pot)}\n"
        f"🧾 Tax: {money(tax_amount)} ({tax}%)\n"
        f"🎁 Payout: {money(payout)}"
    )


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("Unauthorized access.")
        return

    keyboard = [
        [InlineKeyboardButton("⚙️ Tax Rate", callback_data="admin:tax")],
        [InlineKeyboardButton("🔨 Ban / Unban", callback_data="admin:ban")],
        [InlineKeyboardButton("💳 Payment Details", callback_data="admin:gateway")],
        [InlineKeyboardButton("📑 Transactions", callback_data="admin:txs")],
    ]
    await update.effective_message.reply_text(
        "🔧 *Admin Panel*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Unauthorized", show_alert=True)
        return ConversationHandler.END

    await q.answer()

    if q.data == "admin:tax":
        await q.message.reply_text(
            "Enter tax percentage 0-100:",
            reply_markup=ForceReply(selective=True),
        )
        return SET_TAX

    if q.data == "admin:ban":
        await q.message.reply_text(
            "Enter Telegram User ID:",
            reply_markup=ForceReply(selective=True),
        )
        return BAN_USER

    if q.data == "admin:gateway":
        await q.message.reply_text(
            "Choose setting:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Set UPI ID", callback_data="gateway:upi")],
                    [InlineKeyboardButton("Set USDT BEP-20", callback_data="gateway:usdt")],
                ]
            ),
        )
        return ConversationHandler.END

    if q.data == "gateway:upi":
        await q.message.reply_text(
            "Enter platform UPI ID:",
            reply_markup=ForceReply(selective=True),
        )
        return SET_UPI

    if q.data == "gateway:usdt":
        await q.message.reply_text(
            "Enter platform USDT BEP-20 address:",
            reply_markup=ForceReply(selective=True),
        )
        return SET_USDT

    if q.data == "admin:txs":
        with closing(conn()) as db:
            with db.cursor() as c:
                c.execute(
                    """
                    SELECT * FROM transactions
                    WHERE status='pending'
                    ORDER BY tx_id
                    LIMIT 20
                    """
                )
                rows = c.fetchall()

        if not rows:
            await q.message.reply_text("No pending transactions.")
            return ConversationHandler.END

        for tx in rows:
            buttons = [
                InlineKeyboardButton("✅ Approve", callback_data=f"tx:app:{tx['tx_id']}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"tx:rej:{tx['tx_id']}"),
            ]
            await q.message.reply_text(
                f"Tx #{tx['tx_id']}\n"
                f"User: {tx['user_id']}\n"
                f"Type: {tx['tx_type']}\n"
                f"Method: {tx['method']}\n"
                f"Amount: {money(tx['amount'])}\n"
                f"Details: {tx['details'] or '-'}",
                reply_markup=InlineKeyboardMarkup([buttons]),
            )
        return ConversationHandler.END

    return ConversationHandler.END


async def tx_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Unauthorized", show_alert=True)
        return

    try:
        _, action, tx_id = q.data.split(":")
        tx_id = int(tx_id)
    except (ValueError, TypeError):
        await q.answer("Invalid transaction", show_alert=True)
        return

    with closing(conn()) as db:
        with db.cursor() as c:
            c.execute(
                "SELECT * FROM transactions WHERE tx_id=%s FOR UPDATE",
                (tx_id,),
            )
            tx = c.fetchone()

            if not tx or tx["status"] != "pending":
                db.rollback()
                await q.answer("Already processed", show_alert=True)
                return

            if action == "app":
                if tx["tx_type"] == "deposit":
                    c.execute(
                        "UPDATE users SET balance=balance+%s,updated_at=NOW() WHERE user_id=%s",
                        (tx["amount"], tx["user_id"]),
                    )
                status = "approved"

            elif action == "rej":
                if tx["tx_type"] == "withdrawal":
                    # Refund the reserved withdrawal amount.
                    c.execute(
                        "UPDATE users SET balance=balance+%s,updated_at=NOW() WHERE user_id=%s",
                        (tx["amount"], tx["user_id"]),
                    )
                status = "rejected"
            else:
                db.rollback()
                await q.answer("Invalid action", show_alert=True)
                return

            c.execute(
                "UPDATE transactions SET status=%s,processed_at=NOW() WHERE tx_id=%s",
                (status, tx_id),
            )
        db.commit()

    await q.answer("Updated")
    await q.edit_message_text(
        f"Transaction #{tx_id}: {status.upper()} • {money(tx['amount'])}"
    )


async def set_tax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    try:
        value = int(update.effective_message.text.strip())
        if not 0 <= value <= 100:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Enter a whole number from 0 to 100.")
        return SET_TAX

    with closing(conn()) as db:
        set_config(db, "tax_percent", str(value))
        db.commit()

    await update.effective_message.reply_text(f"Tax set to {value}%.")
    return ConversationHandler.END


async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    try:
        uid = int(update.effective_message.text.strip())
    except ValueError:
        await update.effective_message.reply_text("Enter a numeric User ID.")
        return BAN_USER

    with closing(conn()) as db:
        with db.cursor() as c:
            c.execute(
                "SELECT is_banned FROM users WHERE user_id=%s FOR UPDATE",
                (uid,),
            )
            row = c.fetchone()
            if not row:
                db.rollback()
                await update.effective_message.reply_text("User not found.")
                return ConversationHandler.END

            state = not row["is_banned"]
            c.execute(
                "UPDATE users SET is_banned=%s,updated_at=NOW() WHERE user_id=%s",
                (state, uid),
            )
        db.commit()

    await update.effective_message.reply_text(
        f"User {uid}: {'BANNED' if state else 'UNBANNED'}"
    )
    return ConversationHandler.END


async def set_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    value = clean_text(update.effective_message.text, 200)
    with closing(conn()) as db:
        set_config(db, "upi_id", value)
        db.commit()

    await update.effective_message.reply_text("Platform UPI ID updated.")
    return ConversationHandler.END


async def set_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END

    value = clean_text(update.effective_message.text, 200)
    with closing(conn()) as db:
        set_config(db, "usdt_bep20_address", value)
        db.commit()

    await update.effective_message.reply_text("Platform USDT BEP-20 address updated.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Render health server
# ---------------------------------------------------------------------------

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, fmt, *args):
        return


def health_server():
    port = int(os.getenv("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


# ---------------------------------------------------------------------------
# Application startup
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN environment variable is missing")
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL environment variable is missing")

    init_db()
    threading.Thread(target=health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    wallet = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                wallet_entry,
                pattern=r"^wallet:(deposit|withdraw|history|usage|back)$",
            )
        ],
        states={
            DEP_METHOD: [
                CallbackQueryHandler(dep_method, pattern=r"^dep:(upi|usdt)$")
            ],
            DEP_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, dep_amount)
            ],
            WITH_METHOD: [
                CallbackQueryHandler(with_method, pattern=r"^with:(upi|usdt)$")
            ],
            WITH_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, with_amount)
            ],
            WITH_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, with_address)
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    bets = ConversationHandler(
        entry_points=[CommandHandler("bet", bet_start)],
        states={
            ASK_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount)
            ],
            ASK_GAME: [
                CallbackQueryHandler(ask_game, pattern=r"^game:")
            ],
            ASK_PREDICTION: [
                CallbackQueryHandler(
                    ask_prediction, pattern=r"^pred:(even|odd)$"
                )
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    admin = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                admin_cb,
                pattern=r"^(admin:(tax|ban|gateway|txs)|gateway:(upi|usdt))$",
            )
        ],
        states={
            SET_TAX: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_tax)
            ],
            BAN_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ban_user)
            ],
            SET_UPI: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_upi)
            ],
            SET_USDT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_usdt)
            ],
        },
        fallbacks=[],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("accept", accept_cmd))
    app.add_handler(CallbackQueryHandler(tx_cb, pattern=r"^tx:(app|rej):\d+$"))
    app.add_handler(wallet)
    app.add_handler(bets)
    app.add_handler(admin)

    log.info("Bot started successfully")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
