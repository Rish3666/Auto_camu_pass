"""Self-service Telegram Bot for Camu Gate Pass automation."""

import asyncio
import os
import random
from datetime import time as dt_time
from pathlib import Path

from playwright.sync_api import sync_playwright
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from camu_client import IST, SubmissionResult, submit_gatepass_for_user
from db import delete_user, get_user, init_db, list_active_users, set_enabled, upsert_user

DB_PATH = Path(os.environ.get("DB_PATH", "data/users.db"))

# Conversation states for /register
ASK_NAME, ASK_USERNAME, ASK_PASSWORD, ASK_PASS_TYPE, ASK_REASON, ASK_TIMES = range(6)


def _run_single_user_sync(user: dict, *, dry_run: bool) -> SubmissionResult:
    """Execute Playwright submission synchronously for one user (called via asyncio.to_thread)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            return submit_gatepass_for_user(browser, user, dry_run=dry_run)
        finally:
            browser.close()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Auto Camu Gate Pass Bot*\n\n"
        "Set up once and your gate pass will be submitted automatically every *Tuesday between 09:00–23:00 IST*.\n\n"
        "*Commands:*\n"
        "/register — Add or update your Camu credentials & pass defaults\n"
        "/status — View your current settings\n"
        "/test — Run a dry-run (fills form & sends screenshot without submitting)\n"
        "/submit — Submit your gate pass right now\n"
        "/pause — Pause weekly auto-submit\n"
        "/resume — Resume weekly auto-submit\n"
        "/delete — Remove your account and credentials permanently",
        parse_mode="Markdown",
    )


async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Let's set up your profile.\n\n1️⃣ What is your *display name*?", parse_mode="Markdown")
    return ASK_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("2️⃣ Enter your *Camu username / email*:", parse_mode="Markdown")
    return ASK_USERNAME


async def register_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["camu_username"] = update.message.text.strip()
    await update.message.reply_text(
        "3️⃣ Enter your *Camu password*:\n_(I will delete your message immediately after saving it encrypted.)_",
        parse_mode="Markdown",
    )
    return ASK_PASSWORD


async def register_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["camu_password"] = update.message.text
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.message.reply_text(
        "4️⃣ Enter your *Gate Pass type* (or send `skip` for `Day Outing`):",
        parse_mode="Markdown",
    )
    return ASK_PASS_TYPE


async def register_pass_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    context.user_data["pass_type"] = "Day Outing" if val.lower() == "skip" else val
    await update.message.reply_text(
        "5️⃣ Enter your default *reason* (or send `skip` for `Personal work`):",
        parse_mode="Markdown",
    )
    return ASK_REASON


async def register_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    context.user_data["reason"] = "Personal work" if val.lower() == "skip" else val
    await update.message.reply_text(
        "6️⃣ Enter *Out and In time* in 24h format separated by space (e.g. `10:00 20:00`, or `skip` for default):",
        parse_mode="Markdown",
    )
    return ASK_TIMES


async def register_times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = update.message.text.strip()
    out_time, in_time = "10:00", "20:00"
    if val.lower() != "skip":
        parts = val.split()
        if len(parts) != 2:
            await update.message.reply_text("Please send two times like `10:00 20:00` or `skip`.", parse_mode="Markdown")
            return ASK_TIMES
        out_time, in_time = parts[0], parts[1]

    upsert_user(
        telegram_user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        name=context.user_data["name"],
        camu_username=context.user_data["camu_username"],
        camu_password=context.user_data["camu_password"],
        pass_type=context.user_data["pass_type"],
        reason=context.user_data["reason"],
        out_time=out_time,
        in_time=in_time,
        db_path=DB_PATH,
    )
    context.user_data.clear()
    await update.message.reply_text(
        "✅ *Registered!* Your credentials are encrypted and saved.\n"
        "Run /test to verify your login & form with a dry-run screenshot.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def register_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Registration cancelled.")
    return ConversationHandler.END


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user(update.effective_user.id, db_path=DB_PATH)
    if not user:
        await update.message.reply_text("You are not registered yet. Send /register to set up.")
        return

    state = "Active ✅" if user["enabled"] else "Paused ⏸️"
    await update.message.reply_text(
        f"*Your Profile ({state})*\n"
        f"• Name: `{user['name']}`\n"
        f"• Camu User: `{user['username']}`\n"
        f"• Pass Type: `{user['pass_type']}`\n"
        f"• Reason: `{user['reason']}`\n"
        f"• Time: `{user['out_time']} – {user['in_time']}`",
        parse_mode="Markdown",
    )


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if set_enabled(update.effective_user.id, False, db_path=DB_PATH):
        await update.message.reply_text("⏸️ Weekly auto-submit paused. Send /resume anytime to re-enable.")
    else:
        await update.message.reply_text("You are not registered yet. Send /register first.")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if set_enabled(update.effective_user.id, True, db_path=DB_PATH):
        await update.message.reply_text("▶️ Weekly auto-submit resumed for Tuesdays!")
    else:
        await update.message.reply_text("You are not registered yet. Send /register first.")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if delete_user(update.effective_user.id, db_path=DB_PATH):
        await update.message.reply_text("🗑️ Your account and encrypted credentials have been deleted.")
    else:
        await update.message.reply_text("No stored account found.")


async def _handle_manual_run(update: Update, *, dry_run: bool) -> None:
    user = get_user(update.effective_user.id, include_password=True, db_path=DB_PATH)
    if not user:
        await update.message.reply_text("You are not registered yet. Send /register first.")
        return

    label = "dry-run" if dry_run else "live submission"
    await update.message.reply_text(f"⏳ Running {label} on Camu for `{user['name']}`...", parse_mode="Markdown")

    result = await asyncio.to_thread(_run_single_user_sync, user, dry_run=dry_run)
    emoji = "✅" if result.status in ("submitted", "dry_run") else "❌"
    await update.message.reply_text(f"{emoji} *{result.status}*: {result.message}", parse_mode="Markdown")

    if result.screenshot_path and Path(result.screenshot_path).exists():
        with open(result.screenshot_path, "rb") as photo:
            await update.message.reply_photo(photo=photo)


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_manual_run(update, dry_run=True)


async def submit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_manual_run(update, dry_run=False)


async def weekly_tuesday_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled Tuesday job: processes all active users with staggered delays."""
    users = list_active_users(db_path=DB_PATH)
    for idx, user in enumerate(users):
        result = await asyncio.to_thread(_run_single_user_sync, user, dry_run=False)
        emoji = "✅" if result.status == "submitted" else "❌"
        msg = f"{emoji} *Tuesday Gate Pass ({result.status})*\n{result.message}"
        try:
            await context.bot.send_message(chat_id=user["chat_id"], text=msg, parse_mode="Markdown")
            if result.screenshot_path and Path(result.screenshot_path).exists():
                with open(result.screenshot_path, "rb") as photo:
                    await context.bot.send_photo(chat_id=user["chat_id"], photo=photo)
        except Exception as exc:
            print(f"Failed to notify {user['name']}: {exc}")

        if idx < len(users) - 1:
            await asyncio.sleep(random.randint(15, 45))


def build_application() -> Application:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required.")

    init_db(DB_PATH)
    app = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            ASK_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_username)],
            ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_password)],
            ASK_PASS_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_pass_type)],
            ASK_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_reason)],
            ASK_TIMES: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_times)],
        },
        fallbacks=[CommandHandler("cancel", register_cancel)],
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("test", test_cmd))
    app.add_handler(CommandHandler("submit", submit_cmd))

    # Schedule every Tuesday (day 2 in python-telegram-bot: 0=Sun, 1=Mon, 2=Tue)
    hour = int(os.environ.get("SCHEDULE_HOUR", "10"))
    minute = int(os.environ.get("SCHEDULE_MINUTE", "0"))
    app.job_queue.run_daily(
        weekly_tuesday_job,
        time=dt_time(hour=hour, minute=minute, tzinfo=IST),
        days=(2,),
        name="tuesday_gatepass_batch",
    )
    return app


def main() -> None:
    app = build_application()
    print("Telegram bot polling started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
