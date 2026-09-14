"""
Player-vs-Player Betting Bot for Telegram
==========================================

Lets group members challenge each other to bets using virtual points
(no real money involved). Flow:

    /bet @opponent 100 who wins the match tonight?
        -> creates a pending challenge from you to @opponent for 100 points

    /accept <bet_id>
        -> opponent accepts, points are locked from both balances

    /resolve <bet_id> @winner
        -> either participant (or a group admin) declares the winner,
           points move from loser to winner

    /cancel <bet_id>
        -> challenger can cancel a bet that hasn't been accepted yet

    /balance
        -> check your point balance

    /leaderboard
        -> top 10 balances in this chat

    /help
        -> list commands

Setup:
    1. pip install -r requirements.txt
    2. Get a bot token from @BotFather on Telegram
    3. export BOT_TOKEN="your-token-here"
    4. python bot.py

Data is stored in bet_bot.db (SQLite) in the same folder, so balances and
bets persist across restarts.
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
                description TEXT,
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
    # keep username fresh
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
        "/bet @user amount description - challenge someone\n"
        "/accept <bet_id> - accept a challenge made to you\n"
        "/resolve <bet_id> @winner - declare the winner\n"
        "/cancel <bet_id> - cancel your own unaccepted bet\n"
        "/mybets - list your open/pending bets\n"
        "/balance - check your points\n"
        "/leaderboard - top balances in this chat\n\n"
        "All points are virtual - nothing here is real money.",
        parse_mode="Markdown",
    )


async def bet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    challenger = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /bet @opponent amount description\n"
            "Example: /bet @alice 50 who wins the chess match"
        )
        return

    opponent_tag = context.args[0]
    if not opponent_tag.startswith("@"):
        await update.message.reply_text("Tag your opponent with @username, e.g. /bet @alice 50 ...")
        return

    try:
        amount = int(context.args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Amount must be a positive whole number of points.")
        return

    description = " ".join(context.args[2:]) or "unspecified"

    with closing(get_conn()) as conn:
        ensure_user(conn, chat_id, challenger.id, challenger.username)
        balance = get_balance(conn, chat_id, challenger.id)
        if balance < amount:
            await update.message.reply_text(
                f"You only have {balance} points - can't bet {amount}."
            )
            return

        opponent_id = find_user_id_by_username(conn, chat_id, opponent_tag)
        # opponent may not have interacted with the bot yet - that's fine,
        # we store the tag and resolve the id when they /accept.

        conn.execute(
            """
            INSERT INTO bets (chat_id, challenger_id, challenger_name, opponent_id,
                               opponent_name, amount, description, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                chat_id,
                challenger.id,
                challenger.username or challenger.first_name,
                opponent_id,
                opponent_tag.lstrip("@"),
                amount,
                description,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        bet_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    await update.message.reply_text(
        f"🎲 Bet #{bet_id} created!\n"
        f"{challenger.first_name} challenges {opponent_tag} for {amount} points.\n"
        f"Bet: {description}\n\n"
        f"{opponent_tag}, reply with /accept {bet_id} to accept."
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
        f"✅ Bet #{bet_id} accepted! {bet['amount']} points are on the line.\n"
        f"Either player (or an admin) can resolve it with /resolve {bet_id} @winner"
    )


async def resolve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /resolve <bet_id> @winner")
        return
    try:
        bet_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("bet_id must be a number.")
        return
    winner_tag = context.args[1].lstrip("@")

    with closing(get_conn()) as conn:
        bet = conn.execute(
            "SELECT * FROM bets WHERE bet_id=? AND chat_id=?", (bet_id, chat_id)
        ).fetchone()
        if bet is None:
            await update.message.reply_text("No bet with that ID here.")
            return
        if bet["status"] != "accepted":
            await update.message.reply_text(f"Bet #{bet_id} isn't in an accepted state.")
            return

        participants = {bet["challenger_id"], bet["opponent_id"]}
        is_admin = False
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            is_admin = True

        if user.id not in participants and not is_admin:
            await update.message.reply_text("Only the two players or a group admin can resolve this bet.")
            return

        # figure out which participant matches winner_tag
        challenger_row = conn.execute(
            "SELECT username FROM users WHERE chat_id=? AND user_id=?",
            (chat_id, bet["challenger_id"]),
        ).fetchone()
        opponent_row = conn.execute(
            "SELECT username FROM users WHERE chat_id=? AND user_id=?",
            (chat_id, bet["opponent_id"]),
        ).fetchone()

        winner_id = None
        loser_id = None
        if challenger_row and (challenger_row["username"] or "").lower() == winner_tag.lower():
            winner_id, loser_id = bet["challenger_id"], bet["opponent_id"]
        elif opponent_row and (opponent_row["username"] or "").lower() == winner_tag.lower():
            winner_id, loser_id = bet["opponent_id"], bet["challenger_id"]
        else:
            await update.message.reply_text(
                "That username doesn't match either player in this bet."
            )
            return

        amount = bet["amount"]
        adjust_balance(conn, chat_id, winner_id, amount)
        adjust_balance(conn, chat_id, loser_id, -amount)
        conn.execute(
            "UPDATE bets SET status='resolved', winner_id=? WHERE bet_id=?",
            (winner_id, bet_id),
        )
        conn.commit()

    await update.message.reply_text(
        f"🏆 Bet #{bet_id} resolved! @{winner_tag} wins {amount} points."
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
            f"{r['opponent_name']} - {r['amount']} pts - {r['description']}"
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
# Tiny HTTP server so Render's Web Service health check has a port to hit.
# The bot itself only talks to Telegram via polling; this thread just
# answers "OK" to keep the platform happy.
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # silence per-request logging


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
    app.add_handler(CommandHandler("resolve", resolve_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("mybets", mybets_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
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
    # keep username fresh
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
        "/bet @user amount description - challenge someone\n"
        "/accept <bet_id> - accept a challenge made to you\n"
        "/resolve <bet_id> @winner - declare the winner\n"
        "/cancel <bet_id> - cancel your own unaccepted bet\n"
        "/mybets - list your open/pending bets\n"
        "/balance - check your points\n"
        "/leaderboard - top balances in this chat\n\n"
        "All points are virtual - nothing here is real money.",
        parse_mode="Markdown",
    )


async def bet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    challenger = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /bet @opponent amount description\n"
            "Example: /bet @alice 50 who wins the chess match"
        )
        return

    opponent_tag = context.args[0]
    if not opponent_tag.startswith("@"):
        await update.message.reply_text("Tag your opponent with @username, e.g. /bet @alice 50 ...")
        return

    try:
        amount = int(context.args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Amount must be a positive whole number of points.")
        return

    description = " ".join(context.args[2:]) or "unspecified"

    with closing(get_conn()) as conn:
        ensure_user(conn, chat_id, challenger.id, challenger.username)
        balance = get_balance(conn, chat_id, challenger.id)
        if balance < amount:
            await update.message.reply_text(
                f"You only have {balance} points - can't bet {amount}."
            )
            return

        opponent_id = find_user_id_by_username(conn, chat_id, opponent_tag)
        # opponent may not have interacted with the bot yet - that's fine,
        # we store the tag and resolve the id when they /accept.

        conn.execute(
            """
            INSERT INTO bets (chat_id, challenger_id, challenger_name, opponent_id,
                               opponent_name, amount, description, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                chat_id,
                challenger.id,
                challenger.username or challenger.first_name,
                opponent_id,
                opponent_tag.lstrip("@"),
                amount,
                description,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()
        bet_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    await update.message.reply_text(
        f"🎲 Bet #{bet_id} created!\n"
        f"{challenger.first_name} challenges {opponent_tag} for {amount} points.\n"
        f"Bet: {description}\n\n"
        f"{opponent_tag}, reply with /accept {bet_id} to accept."
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
        f"✅ Bet #{bet_id} accepted! {bet['amount']} points are on the line.\n"
        f"Either player (or an admin) can resolve it with /resolve {bet_id} @winner"
    )


async def resolve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /resolve <bet_id> @winner")
        return
    try:
        bet_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("bet_id must be a number.")
        return
    winner_tag = context.args[1].lstrip("@")

    with closing(get_conn()) as conn:
        bet = conn.execute(
            "SELECT * FROM bets WHERE bet_id=? AND chat_id=?", (bet_id, chat_id)
        ).fetchone()
        if bet is None:
            await update.message.reply_text("No bet with that ID here.")
            return
        if bet["status"] != "accepted":
            await update.message.reply_text(f"Bet #{bet_id} isn't in an accepted state.")
            return

        participants = {bet["challenger_id"], bet["opponent_id"]}
        is_admin = False
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            is_admin = True

        if user.id not in participants and not is_admin:
            await update.message.reply_text("Only the two players or a group admin can resolve this bet.")
            return

        # figure out which participant matches winner_tag
        challenger_row = conn.execute(
            "SELECT username FROM users WHERE chat_id=? AND user_id=?",
            (chat_id, bet["challenger_id"]),
        ).fetchone()
        opponent_row = conn.execute(
            "SELECT username FROM users WHERE chat_id=? AND user_id=?",
            (chat_id, bet["opponent_id"]),
        ).fetchone()

        winner_id = None
        loser_id = None
        if challenger_row and (challenger_row["username"] or "").lower() == winner_tag.lower():
            winner_id, loser_id = bet["challenger_id"], bet["opponent_id"]
        elif opponent_row and (opponent_row["username"] or "").lower() == winner_tag.lower():
            winner_id, loser_id = bet["opponent_id"], bet["challenger_id"]
        else:
            await update.message.reply_text(
                "That username doesn't match either player in this bet."
            )
            return

        amount = bet["amount"]
        adjust_balance(conn, chat_id, winner_id, amount)
        adjust_balance(conn, chat_id, loser_id, -amount)
        conn.execute(
            "UPDATE bets SET status='resolved', winner_id=? WHERE bet_id=?",
            (winner_id, bet_id),
        )
        conn.commit()

    await update.message.reply_text(
        f"🏆 Bet #{bet_id} resolved! @{winner_tag} wins {amount} points."
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
            f"{r['opponent_name']} - {r['amount']} pts - {r['description']}"
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
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set the BOT_TOKEN environment variable to your Telegram bot token.")

    init_db()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("bet", bet_cmd))
    app.add_handler(CommandHandler("accept", accept_cmd))
    app.add_handler(CommandHandler("resolve", resolve_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("mybets", mybets_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
