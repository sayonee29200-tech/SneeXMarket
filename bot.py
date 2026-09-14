"""
Player-vs-Player Betting Bot for Telegram with Native Dice & Automatic Settlement
=================================================================================

Lets group members challenge each other to bets using virtual points.

Flow:

    /bet @opponent
        -> or reply to someone's message with /bet
        -> bot asks for the amount (send it as a reply to the bot's prompt)
        -> bot shows inline buttons to pick the game (dice, darts, bowling, ...)
        -> bot shows inline buttons to pick even/odd
        -> challenge is posted

    /bet @opponent 100 🎲 even
        -> fast path: skips the conversation and creates the bet directly

    /accept
        -> reply to the bot's challenge message with /accept (no bet id needed)
    /accept <bet_id>
        -> or accept by id directly

    /cancel <bet_id>
        -> challenger can cancel a bet that hasn't been accepted yet

    /mybets
        -> list your open/pending bets

    /balance
        -> check your point balance

    /leaderboard
        -> top 10 balances in this chat

    /help
        -> list commands

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
