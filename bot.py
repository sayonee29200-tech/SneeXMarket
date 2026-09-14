"""
Player-vs-Player Betting Bot for Telegram with Native Dice & Automatic Settlement
=================================================================================

Lets group members challenge each other to bets using virtual points.
Flow:

    /bet @opponent 100 🎲 even
        -> creates a pending challenge picking 'even' for a dice roll

    /accept <bet_id>
        -> opponent accepts, Telegram rolls the dice animation, determines if the result
           is even or odd, and automatically transfers points to the winner.

    /cancel <bet_id>
        -> challenger can cancel a bet that hasn't been accepted yet

    /balance
        -> check your point balance

    /leaderboard
        -> top 10 balances in this chat

    /help
        -> list commands
"""

import logging
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "bet_bot.db")
STARTING_BALANCE = 1000

# Supported Telegram animated dice emojis
VALID_EMOJIS = {"🎲", "🎯", "🎳", "🏀", "⚽", "🎰"}


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
                created_at TEXT
            )
            """
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
# Command handlers
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
        "/bet @user amount [emoji] <even|odd> - challenge someone\n"
        "  _Example: /bet @alice 50 🎲 even_\n"
        "/accept <bet_id> - accept challenge & auto-roll\n"
        "/cancel <bet_id> - cancel your unaccepted bet\n"
        "/mybets - list your open/pending bets\n"
        "/balance - check your points\n"
        "/leaderboard - top balances in this chat\n\n"
        "Supported Telegram animated dice: 🎲 🎯 🎳 🏀 ⚽ 🎰",
        parse_mode="Markdown",
    )


async def bet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    challenger = update.effective_user

    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /bet @opponent amount [emoji] <even|odd>\n"
            "Examples:\n"
            "  /bet @alice 50 even\n"
            "  /bet @bob 100 🎲 odd"
        )
        return

    opponent_tag = context.args[0]
    if not opponent_tag.startswith("@"):
        await update.message.reply_text("Tag your opponent with @username, e.g. /bet @alice 50 even")
        return

    try:
        amount = int(context.args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Amount must be a positive whole number of points.")
        return

    # Parse optional emoji and prediction choice
    args_tail = context.args[2:]
    target_emoji = "🎲"
    prediction = None

    for arg in args_tail:
        if arg in VALID_EMOJIS:
            target_emoji = arg
        elif arg.lower() in ("even", "odd"):
            prediction = arg.lower()

    if not prediction:
        await update.message.reply_text("You must choose 'even' or 'odd'. Example: /bet @alice 50 🎲 even")
        return

    with closing(get_conn()) as conn:
        ensure_user(conn, chat_id, challenger.id, challenger.username)
        balance = get_balance(conn, chat_id, challenger.id)
        if balance < amount:
            await update.message.reply_text(
                f"You only have {balance} points - can't bet {amount}."
            )
            return

        opponent_id = find_user_id_by_username(conn, chat_id, opponent_tag)

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
                opponent_tag.lstrip("@"),
                amount,
                target_emoji,
                prediction,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        bet_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    opposite_prediction = "odd" if prediction == "even" else "even"
    await update.message.reply_text(
        f"🎲 Bet #{bet_id} created!\n"
        f"{challenger.first_name} challenges {opponent_tag} for {amount} points.\n"
        f"Game: {target_emoji} roll\n"
        f"Prediction: @{challenger.username or challenger.first_name} picked *{prediction.upper()}* "
        f"(giving {opponent_tag} *{opposite_prediction.upper()}*)\n\n"
        f"{opponent_tag}, reply with /accept {bet_id} to start!",
        parse_mode="Markdown"
    )


async def accept_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /accept <bet_id>")
        return
    try:
        bet_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("bet_id must be a number.")
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

    # Determine Even or Odd outcome
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
    app.add_handler(CommandHandler("bet", bet_cmd))
    app.add_handler(CommandHandler("accept", accept_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("mybets", mybets_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
