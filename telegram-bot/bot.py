"""Telegram bot bridge — forwards messages to the nanobot gateway."""

import logging
import os

import httpx
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("telegram-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://nanobot-gateway:3000")
TENANT_ID = os.environ.get("TENANT_ID", "default")

TG_MAX_LENGTH = 4096


def split_message(text: str, limit: int = TG_MAX_LENGTH) -> list[str]:
    """Split text into chunks that fit within the Telegram message limit."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to split at last newline within limit
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            # No newline — split at last space
            split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
            # No space — hard split
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "Hello! I'm Hobot, your clinical assistant. "
        "Send me a message and I'll query the hospital systems for you."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forward user message to gateway and reply with the response."""
    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    text = update.message.text

    if not text:
        return

    session_id = f"tg-{chat_id}"

    payload = {
        "message": text,
        "user_id": user_id,
        "channel": "telegram",
        "tenant_id": TENANT_ID,
        "session_id": session_id,
    }

    logger.info("chat_id=%s user=%s message=%r", chat_id, user_id, text[:80])

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{GATEWAY_URL}/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("Gateway HTTP error: %s", exc)
        await update.message.reply_text(
            "Sorry, something went wrong while processing your request. "
            "Please try again later."
        )
        return
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.error("Gateway unreachable: %s", exc)
        await update.message.reply_text(
            "Sorry, the clinical gateway is currently unreachable. "
            "Please try again in a moment."
        )
        return

    response_text = data.get("response", "No response received.")

    for chunk in split_message(response_text):
        await update.message.reply_text(chunk)


def main() -> None:
    logger.info("Starting Telegram bot (gateway=%s, tenant=%s)", GATEWAY_URL, TENANT_ID)
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
