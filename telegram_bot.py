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
    update_user_fields,
    upsert_user,
)

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DB_PATH", "data/users.db"))
DEFAULT_PASS_TYPE = "Week End Gate Pass"
DEFAULT_REASON = "Weekend outing"
DEFAULT_OUT_TIME = "06:00"
DEFAULT_IN_TIME = "20:00"

# Conversation states for /register, /update, and /fill
(
    ASK_NAME,
    ASK_USERNAME,
    ASK_PASSWORD,
    ASK_PASS_TYPE,
    ASK_REASON,
    ASK_TIMES,
    UPD_CHOOSE,
    UPD_VALUE,
    UPD_PASSWORD,
    FILL_REASON,
    FILL_TIMES,
) = range(11)

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


def get_instant_fill_defaults(now=None) -> tuple[str, str, str]:
    """Return (today_date_str, start_time_plus_10m, end_time_8pm) in IST."""
    ref = now or current_ist_date()
    start_dt = ref + timedelta(minutes=10)
    date_str = start_dt.strftime("%Y-%m-%d")
    start_str = start_dt.strftime("%H:%M")
    end_str = "20:00"
    return date_str, start_str, end_str


def _run_single_user_sync(
    user: dict,
    *,
    dry_run: bool,
    out_date: str | None = None,
    in_date: str | None = None,
) -> SubmissionResult:
    """Execute Playwright JSON API submission synchronously for one user (called via asyncio.to_thread)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            return submit_gatepass_for_user(browser, user, dry_run=dry_run, out_date=out_date, in_date=in_date)
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
        "/register — Set up your Camu credentials &amp; pass defaults\n"
        "/update — Update your name, credentials, pass type, reason, or times\n"
        "/fill — Fill &amp; submit an outing for today (starts 10 min from now → 8:00 PM, or custom)\n"
        "/status — View your settings &amp; quick action buttons\n"
        "/test — Run a dry-run (fills form &amp; sends screenshot without submitting)\n"
        "/submit — Submit your gate pass right now\n"
        "/pause — Pause weekly auto-submit\n"
        "/resume — Resume weekly auto-submit\n"
        "/delete — Remove your account and credentials permanently\n"
        "/cancel — Cancel an active registration, update, or fill",
        parse_mode=ParseMode.HTML,
    )


def _pass_type_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(DEFAULT_PASS_TYPE, callback_data=f"reg:pass:{DEFAULT_PASS_TYPE}"),
                InlineKeyboardButton("Skip (Default)", callback_data="reg:pass:skip"),
            ]
        ]
    )


def _reason_markup(*, include_skip: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Weekend outing", callback_data="reg:reason:Weekend outing"),
            InlineKeyboardButton("Festival outing", callback_data="reg:reason:Festival outing"),
        ],
        [
            InlineKeyboardButton("Emergency outing", callback_data="reg:reason:Emergency outing"),
        ],
    ]
    if include_skip:
        rows[1].append(InlineKeyboardButton("Skip (Default)", callback_data="reg:reason:skip"))
    return InlineKeyboardMarkup(rows)


def _times_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"{DEFAULT_OUT_TIME} – {DEFAULT_IN_TIME}", callback_data=f"reg:times:{DEFAULT_OUT_TIME} {DEFAULT_IN_TIME}"),
                InlineKeyboardButton("Skip (Default)", callback_data="reg:times:skip"),
            ]
        ]
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
        f"4️⃣ Choose your <b>Gate Pass type</b> below, or type a custom one:",
        parse_mode=ParseMode.HTML,
        reply_markup=_pass_type_markup(),
    )
    return ASK_PASS_TYPE


async def _apply_pass_type(target_msg, context: ContextTypes.DEFAULT_TYPE, val: str) -> int:
    context.user_data["pass_type"] = DEFAULT_PASS_TYPE if not val or val.lower() == "skip" else val
    await target_msg.reply_text(
        f"5️⃣ Choose your default <b>reason</b> below, or type a custom one:",
        parse_mode=ParseMode.HTML,
        reply_markup=_reason_markup(),
    )
    return ASK_REASON


async def register_pass_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = (update.message.text or "").strip()
    return await _apply_pass_type(update.message, context, val)


async def register_pass_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    val = (query.data or "").removeprefix("reg:pass:")
    chosen = DEFAULT_PASS_TYPE if val.lower() == "skip" else val
    await query.edit_message_text(f"4️⃣ Gate Pass type: <code>{_esc(chosen)}</code>", parse_mode=ParseMode.HTML)
    return await _apply_pass_type(query.message, context, chosen)


async def _apply_reason(target_msg, context: ContextTypes.DEFAULT_TYPE, val: str) -> int:
    context.user_data["reason"] = DEFAULT_REASON if not val or val.lower() == "skip" else val
    await target_msg.reply_text(
        "6️⃣ Choose <b>Out and In time</b> below, or type 24h format separated by space "
        f"(e.g. <code>{DEFAULT_OUT_TIME} {DEFAULT_IN_TIME}</code>):",
        parse_mode=ParseMode.HTML,
        reply_markup=_times_markup(),
    )
    return ASK_TIMES


async def register_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = (update.message.text or "").strip()
    return await _apply_reason(update.message, context, val)


async def register_reason_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    val = (query.data or "").removeprefix("reg:reason:")
    chosen = DEFAULT_REASON if val.lower() == "skip" else val
    await query.edit_message_text(f"5️⃣ Reason: <code>{_esc(chosen)}</code>", parse_mode=ParseMode.HTML)
    return await _apply_reason(query.message, context, chosen)


async def _finish_register_times(update: Update, target_msg, context: ContextTypes.DEFAULT_TYPE, val: str) -> int:
    out_time, in_time = DEFAULT_OUT_TIME, DEFAULT_IN_TIME
    if val and val.lower() != "skip":
        parts = val.split()
        if len(parts) != 2:
            await target_msg.reply_text(
                f"Please send two times like <code>{DEFAULT_OUT_TIME} {DEFAULT_IN_TIME}</code> or tap a button below.",
                parse_mode=ParseMode.HTML,
                reply_markup=_times_markup(),
            )
            return ASK_TIMES
        ok, err_msg = validate_time_pair(parts[0], parts[1])
        if not ok:
            await target_msg.reply_text(
                f"{_esc(err_msg)} Try again (e.g. <code>{DEFAULT_OUT_TIME} {DEFAULT_IN_TIME}</code>) or tap a button below.",
                parse_mode=ParseMode.HTML,
                reply_markup=_times_markup(),
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
    await target_msg.reply_text(
        "✅ <b>Registered!</b> Your credentials are encrypted and saved.\n"
        "Run /test to verify your login &amp; JSON API dry-run.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def register_times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = (update.message.text or "").strip()
    return await _finish_register_times(update, update.message, context, val)


async def register_times_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    val = (query.data or "").removeprefix("reg:times:")
    display = f"{DEFAULT_OUT_TIME} – {DEFAULT_IN_TIME}" if val.lower() == "skip" else val.replace(" ", " – ")
    await query.edit_message_text(f"6️⃣ Time: <code>{_esc(display)}</code>", parse_mode=ParseMode.HTML)
    return await _finish_register_times(update, query.message, context, val)


async def register_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    msg = update.effective_message if hasattr(update, "effective_message") and update.effective_message else update.message
    await msg.reply_text("Cancelled.")
    return ConversationHandler.END


async def update_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = get_user(update.effective_user.id, db_path=DB_PATH)
    if not user:
        await update.message.reply_text("You are not registered yet. Send /register first.")
        return ConversationHandler.END

    context.user_data.clear()
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👤 Display Name", callback_data="upd:name"),
                InlineKeyboardButton("🔐 Camu Login", callback_data="upd:creds"),
            ],
            [
                InlineKeyboardButton("🎫 Pass Type", callback_data="upd:pass_type"),
                InlineKeyboardButton("📝 Reason", callback_data="upd:reason"),
            ],
            [
                InlineKeyboardButton("⏰ Out / In Time", callback_data="upd:times"),
                InlineKeyboardButton("❌ Cancel", callback_data="upd:cancel"),
            ],
        ]
    )
    await update.message.reply_text(
        "What would you like to <b>update</b>?",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return UPD_CHOOSE


async def update_choose_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    field = (query.data or "").removeprefix("upd:")

    if field == "cancel":
        await query.edit_message_text("Update cancelled.")
        return ConversationHandler.END

    context.user_data["update_field"] = field
    if field == "name":
        await query.edit_message_text("Enter your new <b>display name</b>:", parse_mode=ParseMode.HTML)
    elif field == "creds":
        await query.edit_message_text("Enter your new <b>Camu username / email</b>:", parse_mode=ParseMode.HTML)
    elif field == "pass_type":
        await query.edit_message_text(
            "Choose a new <b>Gate Pass type</b> below, or type a custom one:",
            parse_mode=ParseMode.HTML,
            reply_markup=_pass_type_markup(),
        )
    elif field == "reason":
        await query.edit_message_text(
            "Choose a new default <b>reason</b> below, or type a custom one:",
            parse_mode=ParseMode.HTML,
            reply_markup=_reason_markup(),
        )
    elif field == "times":
        await query.edit_message_text(
            f"Choose new <b>Out and In time</b> below, or type 24h format (e.g. <code>{DEFAULT_OUT_TIME} {DEFAULT_IN_TIME}</code>):",
            parse_mode=ParseMode.HTML,
            reply_markup=_times_markup(),
        )
    return UPD_VALUE


async def _apply_update_value(update: Update, target_msg, context: ContextTypes.DEFAULT_TYPE, val: str) -> int:
    field = context.user_data.get("update_field", "")
    uid = update.effective_user.id

    if field == "name":
        if not val:
            await target_msg.reply_text("Please enter a non-empty display name.")
            return UPD_VALUE
        update_user_fields(uid, name=val, db_path=DB_PATH)
        summary = f"Display name updated to <code>{_esc(val)}</code>."
    elif field == "creds":
        if not val:
            await target_msg.reply_text("Please enter a valid Camu username / email.")
            return UPD_VALUE
        context.user_data["new_username"] = val
        await target_msg.reply_text(
            "Now enter your new <b>Camu password</b>:\n<i>(Your message will be deleted immediately.)</i>",
            parse_mode=ParseMode.HTML,
        )
        return UPD_PASSWORD
    elif field == "pass_type":
        chosen = DEFAULT_PASS_TYPE if not val or val.lower() == "skip" else val
        update_user_fields(uid, pass_type=chosen, db_path=DB_PATH)
        summary = f"Pass type updated to <code>{_esc(chosen)}</code>."
    elif field == "reason":
        chosen = DEFAULT_REASON if not val or val.lower() == "skip" else val
        update_user_fields(uid, reason=chosen, db_path=DB_PATH)
        summary = f"Reason updated to <code>{_esc(chosen)}</code>."
    elif field == "times":
        out_t, in_t = DEFAULT_OUT_TIME, DEFAULT_IN_TIME
        if val and val.lower() != "skip":
            parts = val.split()
            if len(parts) != 2:
                await target_msg.reply_text(
                    f"Please send two times like <code>{DEFAULT_OUT_TIME} {DEFAULT_IN_TIME}</code> or tap a button.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=_times_markup(),
                )
                return UPD_VALUE
            ok, err_msg = validate_time_pair(parts[0], parts[1])
            if not ok:
                await target_msg.reply_text(
                    f"{_esc(err_msg)} Try again or tap a button.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=_times_markup(),
                )
                return UPD_VALUE
            out_t, in_t = parts[0], parts[1]
        update_user_fields(uid, out_time=out_t, in_time=in_t, db_path=DB_PATH)
        summary = f"Time updated to <code>{_esc(out_t)} – {_esc(in_t)}</code>."
    else:
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data.clear()
    await target_msg.reply_text(f"✅ {summary}", parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def update_value_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = (update.message.text or "").strip()
    return await _apply_update_value(update, update.message, context, val)


async def update_value_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    val = data.split(":", 2)[-1] if ":" in data else data
    await query.edit_message_text("Updating…")
    return await _apply_update_value(update, query.message, context, val)


async def update_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pw = update.message.text or ""
    try:
        await update.message.delete()
    except Exception:
        pass
    update_user_fields(
        update.effective_user.id,
        camu_username=context.user_data["new_username"],
        camu_password=pw,
        db_path=DB_PATH,
    )
    context.user_data.clear()
    await update.message.reply_text("✅ <b>Camu credentials updated!</b> Run /test to verify.", parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def fill_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = get_user(update.effective_user.id, db_path=DB_PATH)
    if not user:
        await update.message.reply_text("You are not registered yet. Send /register first.")
        return ConversationHandler.END

    date_str, start_str, end_str = get_instant_fill_defaults()
    context.user_data.clear()
    context.user_data["fill_date"] = date_str
    context.user_data["fill_default_start"] = start_str
    context.user_data["fill_default_end"] = end_str

    await update.message.reply_text(
        f"📅 <b>Fill Outing for {_esc(date_str)}</b>\n\n"
        "1️⃣ Choose your <b>reason</b> below, or type a custom one:",
        parse_mode=ParseMode.HTML,
        reply_markup=_reason_markup(include_skip=False),
    )
    return FILL_REASON


async def _prompt_fill_times(target_msg, context: ContextTypes.DEFAULT_TYPE) -> int:
    date_str = context.user_data["fill_date"]
    start_str = context.user_data["fill_default_start"]
    end_str = context.user_data["fill_default_end"]
    ok_default, _ = validate_time_pair(start_str, end_str)

    rows = []
    if ok_default:
        rows.append(
            [InlineKeyboardButton(f"✅ Use {start_str} – {end_str} (8:00 PM)", callback_data=f"fill:times:{start_str} {end_str}")]
        )
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="fill:cancel")])

    hint = (
        f"Default time is <code>{start_str} – {end_str}</code> (10 min from now until 8:00 PM).\n"
        if ok_default
        else f"Current time + 10 min (<code>{start_str}</code>) is already past <code>20:00</code>.\n"
    )
    await target_msg.reply_text(
        f"2️⃣ <b>Time for {_esc(date_str)}</b>\n"
        f"{hint}"
        "Tap the button to submit, or type custom 24h times (e.g. <code>14:30 20:00</code>):",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return FILL_TIMES


async def fill_reason_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = (update.message.text or "").strip()
    context.user_data["fill_reason"] = val or DEFAULT_REASON
    return await _prompt_fill_times(update.message, context)


async def fill_reason_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    val = (query.data or "").removeprefix("reg:reason:")
    chosen = DEFAULT_REASON if not val or val.lower() == "skip" else val
    context.user_data["fill_reason"] = chosen
    await query.edit_message_text(f"1️⃣ Reason: <code>{_esc(chosen)}</code>", parse_mode=ParseMode.HTML)
    return await _prompt_fill_times(query.message, context)


async def _execute_fill_submission(update: Update, target_msg, context: ContextTypes.DEFAULT_TYPE, out_time: str, in_time: str) -> int:
    uid = update.effective_user.id
    if uid in _IN_FLIGHT_USERS:
        await target_msg.reply_text("⏳ A run is already in progress for your account. Please wait for it to finish.")
        context.user_data.clear()
        return ConversationHandler.END

    user = get_user(uid, include_password=True, db_path=DB_PATH)
    if not user:
        await target_msg.reply_text("You are not registered yet. Send /register first.")
        context.user_data.clear()
        return ConversationHandler.END

    date_str = context.user_data["fill_date"]
    reason = context.user_data.get("fill_reason") or user["reason"]
    override_user = {
        **user,
        "reason": reason,
        "out_time": out_time,
        "in_time": in_time,
    }
    context.user_data.clear()

    _IN_FLIGHT_USERS.add(uid)
    result: SubmissionResult | None = None
    try:
        await target_msg.reply_text(
            f"⏳ Submitting outing for <code>{_esc(date_str)}</code> "
            f"(<code>{_esc(out_time)} – {_esc(in_time)}</code>, <code>{_esc(reason)}</code>)...",
            parse_mode=ParseMode.HTML,
        )
        result = await asyncio.to_thread(
            _run_single_user_sync,
            override_user,
            dry_run=False,
            out_date=date_str,
            in_date=date_str,
        )
        record_submission_status(
            uid,
            status=f"{result.status}: {result.message}",
            submitted_date=date_str if result.status == "submitted" else None,
            db_path=DB_PATH,
        )
        emoji = "✅" if result.status in ("submitted", "skipped") else "❌"
        await target_msg.reply_text(
            f"{emoji} <b>{_esc(result.status)}</b>: {_esc(result.message)}",
            parse_mode=ParseMode.HTML,
        )
        if result.screenshot_path and Path(result.screenshot_path).exists():
            with open(result.screenshot_path, "rb") as photo:
                await target_msg.reply_photo(photo=photo)
    finally:
        _IN_FLIGHT_USERS.discard(uid)
        if result:
            _cleanup_screenshot(result.screenshot_path)
    return ConversationHandler.END


async def fill_times_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = (update.message.text or "").strip()
    parts = val.split()
    if len(parts) != 2:
        await update.message.reply_text(
            "Please send two 24h times like <code>14:30 20:00</code> or tap the button above.",
            parse_mode=ParseMode.HTML,
        )
        return FILL_TIMES
    ok, err_msg = validate_time_pair(parts[0], parts[1])
    if not ok:
        await update.message.reply_text(f"{_esc(err_msg)} Try again (e.g. <code>14:30 20:00</code>).", parse_mode=ParseMode.HTML)
        return FILL_TIMES
    return await _execute_fill_submission(update, update.message, context, parts[0], parts[1])


async def fill_times_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "fill:cancel":
        context.user_data.clear()
        await query.edit_message_text("Outing fill cancelled.")
        return ConversationHandler.END
    val = data.removeprefix("fill:times:")
    parts = val.split()
    if len(parts) != 2:
        context.user_data.clear()
        await query.edit_message_text("Invalid time selection.")
        return ConversationHandler.END
    await query.edit_message_text(f"2️⃣ Time: <code>{_esc(parts[0])} – {_esc(parts[1])}</code>", parse_mode=ParseMode.HTML)
    return await _execute_fill_submission(update, query.message, context, parts[0], parts[1])


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
            BotCommand("register", "Set up your Camu credentials & defaults"),
            BotCommand("update", "Update a saved setting (name, login, pass, reason, time)"),
            BotCommand("fill", "Fill & submit an outing for today (now+10m to 8:00 PM)"),
            BotCommand("status", "View your saved settings & action buttons"),
            BotCommand("test", "Run a dry-run (no submission)"),
            BotCommand("submit", "Submit your gate pass right now"),
            BotCommand("pause", "Pause weekly auto-submit"),
            BotCommand("resume", "Resume weekly auto-submit"),
            BotCommand("delete", "Delete your account and credentials"),
            BotCommand("cancel", "Cancel current registration or update"),
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
            ASK_PASS_TYPE: [
                CallbackQueryHandler(register_pass_type_cb, pattern=r"^reg:pass:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_pass_type),
            ],
            ASK_REASON: [
                CallbackQueryHandler(register_reason_cb, pattern=r"^reg:reason:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_reason),
            ],
            ASK_TIMES: [
                CallbackQueryHandler(register_times_cb, pattern=r"^reg:times:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, register_times),
            ],
        },
        fallbacks=[CommandHandler("cancel", register_cancel)],
    )

    update_handler = ConversationHandler(
        entry_points=[CommandHandler("update", update_start)],
        states={
            UPD_CHOOSE: [CallbackQueryHandler(update_choose_cb, pattern=r"^upd:")],
            UPD_VALUE: [
                CallbackQueryHandler(update_value_cb, pattern=r"^reg:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, update_value_text),
            ],
            UPD_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_password)],
        },
        fallbacks=[CommandHandler("cancel", register_cancel)],
    )

    fill_handler = ConversationHandler(
        entry_points=[CommandHandler("fill", fill_start)],
        states={
            FILL_REASON: [
                CallbackQueryHandler(fill_reason_cb, pattern=r"^reg:reason:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fill_reason_text),
            ],
            FILL_TIMES: [
                CallbackQueryHandler(fill_times_cb, pattern=r"^fill:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fill_times_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", register_cancel)],
    )

    app.add_handler(CommandHandler(["start", "help"], start_cmd))
    app.add_handler(conv_handler)
    app.add_handler(update_handler)
    app.add_handler(fill_handler)
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
