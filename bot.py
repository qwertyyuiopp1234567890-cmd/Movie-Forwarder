"""
Telegram Movie Bot
------------------
A simple, well-structured Telegram bot that delivers movies stored in a
private/public Telegram channel. Users send a numeric movie code (e.g. 101)
and the bot forwards the matching message from the channel to them.

Built with python-telegram-bot v21+ (async).

Run:
    1. pip install "python-telegram-bot>=21"
    2. Set the environment variables below (or edit the constants at the top).
    3. python bot.py
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
import json
import logging
import os
from pathlib import Path
from typing import Dict

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Get the bot token from BotFather and put it in the BOT_TOKEN env variable
# (or replace the fallback string below).
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "PUT-YOUR-BOT-TOKEN-HERE")

# Telegram channel where movies are stored.
# - For a public channel use the username string, e.g. "@my_movies_channel"
# - For a private channel use the numeric chat id, e.g. -1001234567890
#   (you can get it by forwarding a message from the channel to @userinfobot)
CHANNEL_ID_RAW: str = os.getenv("CHANNEL_ID", "@your_channel_username")

# Normalize CHANNEL_ID:
#   - Numeric ids (e.g. "-1001234567890") become int
#   - Public channel usernames are kept as str and the leading "@" is added
#     automatically if the user forgot it (e.g. "muviesman" -> "@muviesman")
def _normalize_channel_id(raw: str):
    raw = raw.strip()
    try:
        return int(raw)
    except ValueError:
        if not raw.startswith("@"):
            raw = "@" + raw
        return raw

CHANNEL_ID = _normalize_channel_id(CHANNEL_ID_RAW)

# Comma-separated list of admin user ids that may add new movies.
# Example: ADMIN_IDS="123456789,987654321"
ADMIN_IDS = {
    int(uid.strip())
    for uid in os.getenv("ADMIN_IDS", "").split(",")
    if uid.strip().isdigit()
}

# Persistent storage for the {code: message_id} mapping.
DATA_FILE = Path(__file__).parent / "movies.json"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Quiet down the very chatty httpx logger
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Movie storage helpers
# ---------------------------------------------------------------------------
def load_movies() -> Dict[str, int]:
    """Load the {code: message_id} mapping from disk.

    Returns an empty dict if the file doesn't exist yet. Codes are stored
    as strings so that JSON round-tripping is lossless.
    """
    if not DATA_FILE.exists():
        return {}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Could not read %s: %s", DATA_FILE, exc)
        return {}


def save_movies(movies: Dict[str, int]) -> None:
    """Persist the {code: message_id} mapping to disk."""
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(movies, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.error("Could not write %s: %s", DATA_FILE, exc)


# In-memory cache. Loaded once at startup, updated whenever an admin adds
# a new entry. For larger collections swap this for a real database
# (SQLite, PostgreSQL, Redis, ...).
MOVIES: Dict[str, int] = load_movies()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — greet the user and explain how the bot works."""
    user = update.effective_user
    welcome = (
        f"Hi <b>{user.first_name}</b>! 🎬\n\n"
        "Send me a <b>movie code</b> (for example <code>101</code>) "
        "and I'll forward the movie to you.\n\n"
        "Available commands:\n"
        "• /start — show this message\n"
        "• /help  — usage instructions\n"
        "• /list  — show all available movie codes"
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help — short usage instructions."""
    text = (
        "How to use this bot:\n\n"
        "1. Find a movie code in our list / channel.\n"
        "2. Send the code as a plain message (e.g. <code>101</code>).\n"
        "3. The movie will be forwarded straight to this chat."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /list — show all known movie codes."""
    if not MOVIES:
        await update.message.reply_text("No movies have been added yet.")
        return

    codes = ", ".join(sorted(MOVIES.keys(), key=lambda c: (len(c), c)))
    await update.message.reply_text(f"Available movie codes:\n{codes}")


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /add <code> <message_id> — admin-only command to register a movie.

    Usage:
        /add 101 42
            -> map code "101" to message_id 42 in the channel
    """
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ You are not allowed to use this command.")
        return

    # Validate arguments.
    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: /add <code> <message_id>\nExample: /add 101 42"
        )
        return

    code, message_id_str = context.args
    if not message_id_str.lstrip("-").isdigit():
        await update.message.reply_text("The message_id must be an integer.")
        return

    # Save in memory and on disk.
    MOVIES[code] = int(message_id_str)
    save_movies(MOVIES)

    await update.message.reply_text(
        f"✅ Added movie code <code>{code}</code> → message id "
        f"<code>{message_id_str}</code>.",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Text (movie code) handler
# ---------------------------------------------------------------------------
async def handle_movie_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any non-command text message as a potential movie code."""
    code = update.message.text.strip()

    # Look up the code in the in-memory store.
    message_id = MOVIES.get(code)
    if message_id is None:
        await update.message.reply_text("❌ Movie not found. Please check the code.")
        return

    # Forward the original channel message to the user.
    try:
        await context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=CHANNEL_ID,
            message_id=message_id,
        )
    except Forbidden:
        logger.error("Bot is not a member/admin of channel %s", CHANNEL_ID)
        await update.message.reply_text(
            "⚠️ I can't access the movies channel. Please contact the admin."
        )
    except BadRequest as exc:
        logger.error("BadRequest while forwarding code %s: %s", code, exc)
        await update.message.reply_text(
            "⚠️ I couldn't fetch that movie. The message may have been deleted."
        )
    except TelegramError as exc:
        logger.exception("Telegram error while forwarding code %s: %s", code, exc)
        await update.message.reply_text("⚠️ Something went wrong. Please try again.")


# ---------------------------------------------------------------------------
# Global error handler
# ---------------------------------------------------------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any uncaught exception raised inside a handler."""
    logger.exception("Unhandled exception while processing an update", exc_info=context.error)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def main() -> None:
    """Build the Application, register handlers and start polling."""
    if BOT_TOKEN == "PUT-YOUR-BOT-TOKEN-HERE":
        raise RuntimeError(
            "Please set the BOT_TOKEN environment variable (or edit bot.py)."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("add", add_command))

    # Any non-command text is treated as a movie code lookup.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_movie_code))

    # Global error handler
    app.add_error_handler(error_handler)

    logger.info("Bot is starting up. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
