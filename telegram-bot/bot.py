"""Telegram bot bridge — forwards messages to the nanobot gateway with rich rendering."""

import json
import logging
import os

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
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
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = text.rfind(" ", 0, limit)
        if split_at == -1:
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
    """Forward user message to gateway and reply with rich response."""
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

    blocks = data.get("blocks")
    response_text = data.get("response", "No response received.")

    if blocks:
        await _send_rich_blocks(update, blocks, response_text)
    else:
        for chunk in split_message(response_text):
            await update.message.reply_text(chunk)


async def _send_rich_blocks(update: Update, blocks: list[dict], fallback_text: str) -> None:
    """Render and send structured blocks as Telegram messages."""
    sent_any = False

    for block in blocks:
        btype = block.get("type")

        if btype == "text":
            html = block.get("html", block.get("content", ""))
            if html:
                for chunk in split_message(html):
                    await update.message.reply_text(chunk, parse_mode="HTML")
                sent_any = True

        elif btype == "inline_keyboard":
            buttons = block.get("buttons", [])
            keyboard = [[InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"])] for btn in buttons]
            markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("Actions:", reply_markup=markup)
            sent_any = True

        elif btype == "confirmation":
            html = block.get("html", "")
            buttons = block.get("buttons", [])
            keyboard = [[InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"])] for btn in buttons]
            markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(html, parse_mode="HTML", reply_markup=markup)
            sent_any = True

        elif btype == "image":
            url = block.get("url", "")
            alt = block.get("alt", "")
            if url:
                try:
                    await update.message.reply_photo(photo=url, caption=alt)
                    sent_any = True
                except Exception as exc:
                    logger.warning("Failed to send image %s: %s", url, exc)
                    await update.message.reply_text(f"[Image: {alt}]\n{url}")
                    sent_any = True

        elif btype == "rendered_image":
            import base64
            from io import BytesIO
            img_bytes = base64.b64decode(block["image_base64"])
            bio = BytesIO(img_bytes)
            bio.name = f"{block.get('original_type', 'image')}.png"
            await update.message.reply_photo(photo=bio, caption=block.get("title", ""))
            sent_any = True

        elif btype == "chart":
            title = block.get("title", "Chart")
            await update.message.reply_text(f"<b>{title}</b>\n(Chart data available in webchat)", parse_mode="HTML")
            sent_any = True

        elif btype == "waveform":
            title = block.get("title", "Waveform")
            await update.message.reply_text(f"<b>{title}</b>\n(Waveform data available in webchat)", parse_mode="HTML")
            sent_any = True

    # Always send the text summary
    if fallback_text:
        for chunk in split_message(fallback_text):
            await update.message.reply_text(chunk, parse_mode="HTML")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()

    try:
        data = json.loads(query.data)
    except (json.JSONDecodeError, TypeError):
        await query.edit_message_text("Invalid button data.")
        return

    action = data.get("action", "")
    params = data.get("params", {})

    chat_id = update.effective_chat.id
    user_id = str(update.effective_user.id)
    session_id = f"tg-{chat_id}"

    if action == "confirm":
        # Confirmation button → POST to /confirm/{id}
        cid = params.get("confirmation_id", "")
        if not cid:
            await query.edit_message_text("Missing confirmation ID.")
            return
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(f"{GATEWAY_URL}/confirm/{cid}")
                resp.raise_for_status()
                result = resp.json()
            result_text = json.dumps(result.get("result", result), indent=2)
            await query.edit_message_text(f"Confirmed.\n<pre>{result_text[:3000]}</pre>", parse_mode="HTML")
        except Exception as exc:
            logger.error("Confirm error: %s", exc)
            await query.edit_message_text(f"Confirmation failed: {exc}")
        return

    # Action buttons → synthetic /chat message
    label = data.get("label", action.replace("_", " ").title())
    param_str = " ".join(f"{v}" for v in params.values())
    synthetic_message = f"{label} {param_str}".strip()

    payload = {
        "message": synthetic_message,
        "user_id": user_id,
        "channel": "telegram",
        "tenant_id": TENANT_ID,
        "session_id": session_id,
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{GATEWAY_URL}/chat", json=payload)
            resp.raise_for_status()
            resp_data = resp.json()
    except Exception as exc:
        logger.error("Callback chat error: %s", exc)
        await query.edit_message_text(f"Error: {exc}")
        return

    blocks = resp_data.get("blocks")
    response_text = resp_data.get("response", "No response.")

    # For callback responses, edit original and send new messages
    await query.edit_message_text(f"Loading {label}...")

    msg = query.message
    if blocks:
        # Send blocks as new messages
        for block in blocks:
            btype = block.get("type")
            if btype == "text":
                html = block.get("html", block.get("content", ""))
                if html:
                    await msg.reply_text(html, parse_mode="HTML")
            elif btype == "inline_keyboard":
                buttons = block.get("buttons", [])
                keyboard = [[InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"])] for btn in buttons]
                await msg.reply_text("Actions:", reply_markup=InlineKeyboardMarkup(keyboard))
            elif btype == "rendered_image":
                import base64
                from io import BytesIO
                img_bytes = base64.b64decode(block["image_base64"])
                bio = BytesIO(img_bytes)
                bio.name = f"{block.get('original_type', 'image')}.png"
                await msg.reply_photo(photo=bio, caption=block.get("title", ""))
            elif btype == "image":
                url = block.get("url", "")
                alt = block.get("alt", "")
                if url:
                    try:
                        await msg.reply_photo(photo=url, caption=alt)
                    except Exception:
                        await msg.reply_text(f"[Image: {alt}]\n{url}")

    if response_text:
        for chunk in split_message(response_text):
            await msg.reply_text(chunk, parse_mode="HTML")


def main() -> None:
    logger.info("Starting Telegram bot (gateway=%s, tenant=%s)", GATEWAY_URL, TENANT_ID)
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling()


if __name__ == "__main__":
    main()
