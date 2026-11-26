"""Batch summary and per-user notification helpers (Discord & Telegram)."""

import os
from typing import Iterable

import requests

from camu_client import SubmissionResult

STATUS_EMOJI = {
    "submitted": "✅",
    "dry_run": "🧪",
    "skipped": "⏭️",
    "failed": "❌",
}


def format_batch_summary(results: Iterable[SubmissionResult], *, dry_run: bool = False) -> str:
    """Build a human-readable summary report for all processed friends."""
    results_list = list(results)
    header = "🧪 **Camu Gate Pass Dry-Run Report**" if dry_run else "📋 **Camu Gate Pass Weekly Report**"
    lines = [header, ""]

    for res in results_list:
        emoji = STATUS_EMOJI.get(res.status, "ℹ️")
        lines.append(f"{emoji} **{res.name}** (`{res.status}`): {res.message}")

    submitted = sum(1 for r in results_list if r.status == "submitted")
    failed = sum(1 for r in results_list if r.status == "failed")
    lines.append("")
    lines.append(f"Total: {len(results_list)} | Submitted: {submitted} | Failed: {failed}")
    return "\n".join(lines)


def send_discord_webhook(message: str, webhook_url: str | None = None) -> bool:
    """Post a summary message to a Discord channel webhook."""
    url = webhook_url or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return False
    resp = requests.post(url, json={"content": message}, timeout=10)
    resp.raise_for_status()
    return True


def send_telegram_message(
    message: str,
    chat_id: str | None = None,
    bot_token: str | None = None,
) -> bool:
    """Send a message via Telegram Bot API."""
    token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not target_chat:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": target_chat, "text": message, "parse_mode": "Markdown"},
        timeout=10,
    )
    resp.raise_for_status()
    return True


def notify_all(results: list[SubmissionResult], users: list[dict], *, dry_run: bool = False) -> None:
    """Send batch summary to group channels and optional per-user Telegram DMs."""
    summary = format_batch_summary(results, dry_run=dry_run)
    print(summary)

    try:
        send_discord_webhook(summary)
    except Exception as exc:
        print(f"Warning: Discord notification failed: {exc}")

    try:
        send_telegram_message(summary)
    except Exception as exc:
        print(f"Warning: Telegram group notification failed: {exc}")

    # Optional individual Telegram DMs
    user_chat_map = {u.get("name", u.get("username")): u.get("telegram_chat_id", "") for u in users}
    for res in results:
        chat_id = user_chat_map.get(res.name, "").strip()
        if chat_id and res.status != "skipped":
            emoji = STATUS_EMOJI.get(res.status, "ℹ️")
            dm = f"{emoji} Hi {res.name}, your Camu gate pass status: *{res.status}*\n{res.message}"
            try:
                send_telegram_message(dm, chat_id=chat_id)
            except Exception as exc:
                print(f"Warning: Telegram DM to {res.name} failed: {exc}")
