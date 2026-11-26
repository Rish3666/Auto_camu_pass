"""Multi-user orchestrator for scheduled Camu gate pass submissions."""

import argparse
import json
import os
import random
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from camu_client import SubmissionResult, current_ist_date, submit_gatepass_for_user
from crypto_helper import decrypt_users
from notifier import notify_all


def load_users() -> list[dict[str, Any]]:
    """Load user roster from USERS_CONFIG env, encrypted users.enc, or local users.json."""
    env_json = os.environ.get("USERS_CONFIG", "").strip()
    if env_json:
        data = json.loads(env_json)
        if not isinstance(data, list):
            raise ValueError("USERS_CONFIG must be a JSON array.")
        return data

    enc_path = Path("users.enc")
    secret_key = os.environ.get("CAMU_SECRET_KEY", "").strip()
    if enc_path.exists() and secret_key:
        return decrypt_users(enc_path.read_bytes(), secret_key)

    local_path = Path("users.json")
    if local_path.exists():
        data = json.loads(local_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("users.json must be a JSON array.")
        return data

    raise RuntimeError(
        "No user roster found. Set USERS_CONFIG secret, provide users.enc + CAMU_SECRET_KEY, "
        "or create a local users.json."
    )


def run_batch(
    *,
    dry_run: bool = False,
    headed: bool = False,
    min_jitter_sec: int = 15,
    max_jitter_sec: int = 45,
    enforce_tuesday_window: bool = False,
) -> list[SubmissionResult]:
    """Execute gate pass submissions sequentially for all enabled users."""
    now_ist = current_ist_date()
    if enforce_tuesday_window:
        # Tuesday is weekday() == 1, window is 09:00 <= hour < 23
        if now_ist.weekday() != 1 or not (9 <= now_ist.hour < 23):
            print(f"Outside Tuesday 09:00–23:00 IST window ({now_ist.isoformat()}). Exiting.")
            return []

    users = load_users()
    results: list[SubmissionResult] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        try:
            active_indices = [i for i, u in enumerate(users) if u.get("enabled", True)]
            for idx, user in enumerate(users):
                name = user.get("name", user.get("username", f"user_{idx}"))
                if not user.get("enabled", True):
                    results.append(SubmissionResult(name=name, status="skipped", message="Disabled in roster."))
                    continue

                print(f"Processing {name}...")
                result = submit_gatepass_for_user(browser, user, dry_run=dry_run)
                results.append(result)

                # Stagger requests between active users to avoid burst rate-limits
                is_last_active = idx == active_indices[-1] if active_indices else True
                if not is_last_active and max_jitter_sec > 0:
                    delay = random.randint(min_jitter_sec, max_jitter_sec)
                    print(f"Sleeping {delay}s before next user...")
                    time.sleep(delay)
        finally:
            browser.close()

    notify_all(results, users, dry_run=dry_run)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run multi-user Camu gate pass automation.")
    parser.add_argument("--dry-run", action="store_true", help="Fill forms and capture screenshots without submitting.")
    parser.add_argument("--headed", action="store_true", help="Show browser window (for local debugging).")
    parser.add_argument("--min-jitter", type=int, default=15, help="Minimum delay (seconds) between users.")
    parser.add_argument("--max-jitter", type=int, default=45, help="Maximum delay (seconds) between users.")
    parser.add_argument(
        "--enforce-window",
        action="store_true",
        help="Abort if current IST time is not Tuesday 09:00–23:00.",
    )
    args = parser.parse_args()

    results = run_batch(
        dry_run=args.dry_run,
        headed=args.headed,
        min_jitter_sec=args.min_jitter,
        max_jitter_sec=args.max_jitter,
        enforce_tuesday_window=args.enforce_window,
    )

    if any(r.status == "failed" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
