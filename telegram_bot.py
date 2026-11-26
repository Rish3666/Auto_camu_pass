"""Self-service Telegram Bot for Camu Gate Pass automation."""

import asyncio
import html
import logging
import os
import random
import re
from datetime import time as dt_time, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from camu_client import IST, SubmissionResult, current_ist_date, submit_gatepass_for_user
from db import (
    delete_user,
    get_user,
    init_db,
    list_active_users,
    record_submission_status,
    set_enabled,
    upsert_user,
)

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DB_PATH", "data/users.db"))
DEFAULT_PASS_TYPE = "Week End Gate Pass"
DEFAULT_REASON = "Weekend outing"
DEFAULT_OUT_TIME = "06:00"
DEFAULT_IN_TIME = "20:00"

# Conversation states for /register
ASK_NAME, ASK_USERNAME, ASK_PASSWORD, ASK_PASS_TYPE, ASK_REASON, ASK_TIMES = range(6)

# Per-user in-flight lock set to prevent concurrent Playwright launches for the same account
_IN_FLIGHT_USERS: set[int] = set()

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _esc(value: object) -> str:
    return html.escape(str(value or ""))


def get_weekend_dates(now=None) -> tuple[str, str]:
    """Return (friday_date_str, sunday_date_str) for the upcoming/current weekend in IST."""
    ref = now or current_ist_date()
    # Python weekday(): Mon=0 .. Thu=3, Fri=4, Sat=5, Sun=6
    days_until_friday = 0 if ref.weekday() == 4 else (4 - ref.weekday() + 7) % 7
    friday = ref + timedelta(days=days_until_friday)
    sunday = friday + timedelta(days=2)
    return friday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def validate_time_pair(out_str: str, in_str: str) -> tuple[bool, str]:
    """Validate 24h HH:MM out/in times and ensure out_time is earlier than in_time."""
    m_out = _TIME_RE.match(out_str.strip())
    m_in = _TIME_RE.match(in_str.strip())
    if not m_out or not m_in:
        return False, "Use 24-hour HH:MM format (e.g. 06:00 20:00)."
    out_mins = int(m_out.group(1)) * 60 + int(m_out.group(2))
    in_mins = int(m_in.group(1)) * 60 + int(m_in.group(2))
    if out_mins >= in_mins:
        return False, "In time must be later than Out time."
    return True, ""


def _cleanup_screenshot(path_str: str | None) -> None:
    if not path_str:
        return
    try:
        Path(path_str).unlink(missing_ok=True)
    except OSError:
        pass


def _load_dotenv(env_path: Path = Path(".env")) -> None:
    """Load key=value pairs from .env if present (without overriding existing env vars)."""
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _run_single_user_sync(user: dict, *, dry_run: bool) -> SubmissionResult:
    """Execute Playwright submission synchronously for one user (called via asyncio.to_thread)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            return submit_gatepass_for_user(browser, user, dry_run=dry_run)
        finally:
            browser.close()


def _build_status_markup(user: dict) -> InlineKeyboardMarkup:
    toggle_label = "⏸️ Pause" if user.get("enabled") else "▶️ Resume"
    toggle_cb = "action:pause" if user.get("enabled") else "action:resume"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🧪 Dry-Run Test", callback_data="action:test"),
                InlineKeyboardButton("🚀 Submit Now", callback_data="action:submit"),
            ],
            [InlineKeyboardButton(toggle_label, callback_data=toggle_cb)],
        ]
    )


def _format_status_text(user: dict) -> str:
    state = "Active ✅" if user["enabled"] else "Paused ⏸️"
    lines = [
        f"<b>Your Profile ({state})</b>",
        f"• Name: <code>{_esc(user['name'])}</code>",
        f"• Camu User: <code>{_esc(user['username'])}</code>",
        f"• Pass Type: <code>{_esc(user['pass_type'])}</code>",
        f"• Reason: <code>{_esc(user['reason'])}</code>",
        f"• Time: <code>{_esc(user['out_time'])} – {_esc(user['in_time'])}</code>",
    ]
    if user.get("last_submitted_date"):
        lines.append(f"• Last Submitted: <code>{_esc(user['last_submitted_date'])}</code>")
    if user.get("last_status"):
        lines.append(f"• Last Status: <code>{_esc(user['last_status'])}</code>")
    return "\n".join(lines)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>Auto Camu Gate Pass Bot</b>\n\n"
        "Set up once and your gate pass will be submitted automatically every "
        "<b>Thursday &amp; Friday (09:00–23:00 IST)</b>.\n\n"
        "<b>Commands:</b>\n"
        "/register — Add or update your Camu credentials &amp; pass defaults\n"
        "/status — View your settings &amp; quick action buttons\n"
        "/test — Run a dry-run (fills form &amp; sends screenshot without submitting)\n"
        "/submit — Submit your gate pass right now\n"
        "/pause — Pause weekly auto-submit\n"
        "/resume — Resume weekly auto-submit\n"
        "/delete — Remove your account and credentials permanently\n"
        "/cancel — Cancel an active registration",
        parse_mode=ParseMode.HTML,
    )


async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Let's set up your profile.\n\n1️⃣ What is your <b>display name</b>?",
        parse_mode=ParseMode.HTML,
    )
    return ASK_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Please enter a non-empty display name.")
        return ASK_NAME
    context.user_data["name"] = name
    await update.message.reply_text(
        "2️⃣ Enter your <b>Camu username / email</b>:",
        parse_mode=ParseMode.HTML,
    )
    return ASK_USERNAME


async def register_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = (update.message.text or "").strip()
    if not username:
        await update.message.reply_text("Please enter a valid Camu username / email.")
        return ASK_USERNAME
    context.user_data["camu_username"] = username
    await update.message.reply_text(
        "3️⃣ Enter your <b>Camu password</b>:\n"
        "<i>(I will delete your message immediately after saving it encrypted.)</i>",
        parse_mode=ParseMode.HTML,
    )
    return ASK_PASSWORD


async def register_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["camu_password"] = update.message.text or ""
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.message.reply_text(
        f"4️⃣ Enter your <b>Gate Pass type</b> (or send <code>skip</code> for <code>{_esc(DEFAULT_PASS_TYPE)}</code>):",
        parse_mode=ParseMode.HTML,
    )
    return ASK_PASS_TYPE


async def register_pass_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = (update.message.text or "").strip()
    context.user_data["pass_type"] = DEFAULT_PASS_TYPE if not val or val.lower() == "skip" else val
    await update.message.reply_text(
        f"5️⃣ Enter your default <b>reason</b> (or send <code>skip</code> for <code>{_esc(DEFAULT_REASON)}</code>):",
        parse_mode=ParseMode.HTML,
    )
    return ASK_REASON


async def register_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = (update.message.text or "").strip()
    context.user_data["reason"] = DEFAULT_REASON if not val or val.lower() == "skip" else val
    await update.message.reply_text(
        "6️⃣ Enter <b>Out and In time</b> in 24h format separated by space "
        f"(e.g. <code>{DEFAULT_OUT_TIME} {DEFAULT_IN_TIME}</code>, or <code>skip</code> for default):",
        parse_mode=ParseMode.HTML,
    )
    return ASK_TIMES


async def register_times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = (update.message.text or "").strip()
    out_time, in_time = DEFAULT_OUT_TIME, DEFAULT_IN_TIME
    if val and val.lower() != "skip":
        parts = val.split()
        if len(parts) != 2:
            await update.message.reply_text(
                f"Please send two times like <code>{DEFAULT_OUT_TIME} {DEFAULT_IN_TIME}</code> or <code>skip</code>.",
                parse_mode=ParseMode.HTML,
            )
            return ASK_TIMES
        ok, err_msg = validate_time_pair(parts[0], parts[1])
        if not ok:
            await update.message.reply_text(
                f"{_esc(err_msg)} Try again (e.g. <code>{DEFAULT_OUT_TIME} {DEFAULT_IN_TIME}</code>) or send <code>skip</code>.",
                parse_mode=ParseMode.HTML,
            )
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
        "✅ <b>Registered!</b> Your credentials are encrypted and saved.\n"
        "Run /test to verify your login &amp; form with a dry-run screenshot.",
        parse_mode=ParseMode.HTML,
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

    await update.message.reply_text(
        _format_status_text(user),
        parse_mode=ParseMode.HTML,
        reply_markup=_build_status_markup(user),
    )


async def callback_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button taps from /status."""
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    data = query.data or ""

    if data == "action:pause":
        set_enabled(uid, False, db_path=DB_PATH)
        user = get_user(uid, db_path=DB_PATH)
        if user:
            await query.edit_message_text(
                _format_status_text(user),
                parse_mode=ParseMode.HTML,
                reply_markup=_build_status_markup(user),
            )
    elif data == "action:resume":
        set_enabled(uid, True, db_path=DB_PATH)
        user = get_user(uid, db_path=DB_PATH)
        if user:
            await query.edit_message_text(
                _format_status_text(user),
                parse_mode=ParseMode.HTML,
                reply_markup=_build_status_markup(user),
            )
    elif data == "action:test":
        await _handle_manual_run(update, dry_run=True)
    elif data == "action:submit":
        await _handle_manual_run(update, dry_run=False)


async def pause_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if set_enabled(update.effective_user.id, False, db_path=DB_PATH):
        await update.message.reply_text("⏸️ Weekly auto-submit paused. Send /resume anytime to re-enable.")
    else:
        await update.message.reply_text("You are not registered yet. Send /register first.")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if set_enabled(update.effective_user.id, True, db_path=DB_PATH):
        await update.message.reply_text("▶️ Weekly auto-submit resumed for Thursdays & Fridays!")
    else:
        await update.message.reply_text("You are not registered yet. Send /register first.")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if delete_user(update.effective_user.id, db_path=DB_PATH):
        await update.message.reply_text("🗑️ Your account and encrypted credentials have been deleted.")
    else:
        await update.message.reply_text("No stored account found.")


async def _handle_manual_run(update: Update, *, dry_run: bool) -> None:
    msg_target = update.effective_message if hasattr(update, "effective_message") and update.effective_message else update.message
    uid = update.effective_user.id
    if uid in _IN_FLIGHT_USERS:
        await msg_target.reply_text("⏳ A run is already in progress for your account. Please wait for it to finish.")
        return

    user = get_user(uid, include_password=True, db_path=DB_PATH)
    if not user:
        await msg_target.reply_text("You are not registered yet. Send /register first.")
        return

    _IN_FLIGHT_USERS.add(uid)
    result: SubmissionResult | None = None
    try:
        label = "dry-run" if dry_run else "live submission"
        await msg_target.reply_text(
            f"⏳ Running {label} on Camu for <code>{_esc(user['name'])}</code>...",
            parse_mode=ParseMode.HTML,
        )

        result = await asyncio.to_thread(_run_single_user_sync, user, dry_run=dry_run)
        friday_str, _ = get_weekend_dates()
        record_submission_status(
            uid,
            status=f"{result.status}: {result.message}",
            submitted_date=friday_str if (not dry_run and result.status == "submitted") else None,
            db_path=DB_PATH,
        )

        emoji = "✅" if result.status in ("submitted", "dry_run") else "❌"
        await msg_target.reply_text(
            f"{emoji} <b>{_esc(result.status)}</b>: {_esc(result.message)}",
            parse_mode=ParseMode.HTML,
        )

        if result.screenshot_path and Path(result.screenshot_path).exists():
            with open(result.screenshot_path, "rb") as photo:
                await msg_target.reply_photo(photo=photo)
    finally:
        _IN_FLIGHT_USERS.discard(uid)
        if result:
            _cleanup_screenshot(result.screenshot_path)


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_manual_run(update, dry_run=True)


async def submit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_manual_run(update, dry_run=False)


async def weekly_tuesday_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled Thursday/Friday job: deduplicates per weekend (Friday date) with staggered delays."""
    friday_str, sunday_str = get_weekend_dates()
    users = list_active_users(db_path=DB_PATH)
    for idx, user in enumerate(users):
        uid = user["telegram_user_id"]
        if user.get("last_submitted_date") == friday_str:
            logger.info("Skipping %s: already submitted for weekend %s -> %s", user["name"], friday_str, sunday_str)
            continue
        if uid in _IN_FLIGHT_USERS:
            logger.info("Skipping %s: manual run already in flight", user["name"])
            continue

        _IN_FLIGHT_USERS.add(uid)
        result: SubmissionResult | None = None
        try:
            result = await asyncio.to_thread(_run_single_user_sync, user, dry_run=False)
            record_submission_status(
                uid,
                status=f"{result.status}: {result.message}",
                submitted_date=friday_str if result.status == "submitted" else None,
                db_path=DB_PATH,
            )
            emoji = "✅" if result.status == "submitted" else "❌"
            msg = f"{emoji} <b>Weekly Gate Pass ({_esc(result.status)})</b>\n{_esc(result.message)}"
            try:
                await context.bot.send_message(chat_id=user["chat_id"], text=msg, parse_mode=ParseMode.HTML)
                if result.screenshot_path and Path(result.screenshot_path).exists():
                    with open(result.screenshot_path, "rb") as photo:
                        await context.bot.send_photo(chat_id=user["chat_id"], photo=photo)
            except Exception as exc:
                logger.warning("Failed to notify %s: %s", user["name"], exc)
        except Exception as exc:
            logger.exception("Scheduled run crashed for %s: %s", user["name"], exc)
            record_submission_status(uid, status=f"failed: {exc}", db_path=DB_PATH)
        finally:
            _IN_FLIGHT_USERS.discard(uid)
            if result:
                _cleanup_screenshot(result.screenshot_path)

        if idx < len(users) - 1:
            await asyncio.sleep(random.randint(15, 45))


async def _post_init(app: Application) -> None:
    """Register slash command descriptions in Telegram's command menu."""
    await app.bot.set_my_commands(
        [
            BotCommand("register", "Add or update Camu credentials & defaults"),
            BotCommand("status", "View your saved settings & action buttons"),
            BotCommand("test", "Run a dry-run with screenshot (no submission)"),
            BotCommand("submit", "Submit your gate pass right now"),
            BotCommand("pause", "Pause weekly auto-submit"),
            BotCommand("resume", "Resume weekly auto-submit"),
            BotCommand("delete", "Delete your account and credentials"),
            BotCommand("cancel", "Cancel current registration"),
        ]
    )


def build_application() -> Application:
    _load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required.")
    if not os.environ.get("CAMU_SECRET_KEY", "").strip():
        raise RuntimeError("CAMU_SECRET_KEY environment variable is required.")

    init_db(DB_PATH)
    app = Application.builder().token(token).post_init(_post_init).build()

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

    app.add_handler(CommandHandler(["start", "help"], start_cmd))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("pause", pause_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("test", test_cmd))
    app.add_handler(CommandHandler("submit", submit_cmd))
    app.add_handler(CallbackQueryHandler(callback_action, pattern=r"^action:"))

    if app.job_queue is None:
        raise RuntimeError(
            'JobQueue is not available. Install python-telegram-bot with job-queue: pip install "python-telegram-bot[job-queue]"'
        )

    # Schedule every Thursday & Friday (days 4, 5 in python-telegram-bot: 0=Sun..6=Sat)
    hour = int(os.environ.get("SCHEDULE_HOUR", "10"))
    minute = int(os.environ.get("SCHEDULE_MINUTE", "0"))
    app.job_queue.run_daily(
        weekly_tuesday_job,
        time=dt_time(hour=hour, minute=minute, tzinfo=IST),
        days=(4, 5),
        name="thu_fri_gatepass_batch",
    )
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    app = build_application()
    logger.info("Telegram bot polling started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
