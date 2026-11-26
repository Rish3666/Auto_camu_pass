"""Playwright-based Camu gate pass submission worker with isolated browser contexts."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError

IST = timezone(timedelta(hours=5, minutes=30))
CAMU_PROFILE_URL = "https://student.camu.in/v2/profile"

# Centralized selectors for student.camu.in.
# Update these if your institution's Camu portal uses different element attributes or labels.
SELECTORS = {
    "username_input": 'input[type="text"], input[name="Email"], input[name="username"], input[placeholder*="User" i], input[placeholder*="Email" i]',
    "password_input": 'input[type="password"]',
    "login_button": 'button[type="submit"], button:has-text("Login"), button:has-text("Sign In")',
    "gatepass_nav": 'a:has-text("Gate Pass"), a:has-text("Outpass"), button:has-text("Gate Pass"), [routerlink*="gatepass" i], [href*="gatepass" i]',
    "apply_button": 'button:has-text("Apply"), button:has-text("New"), button:has-text("Add"), button:has-text("Request")',
    "pass_type_select": 'select[name*="type" i], [formcontrolname*="type" i], mat-select',
    "reason_input": 'textarea, input[name*="reason" i], input[placeholder*="Reason" i]',
    "out_time_input": 'input[name*="out" i], input[placeholder*="Out" i]',
    "in_time_input": 'input[name*="in" i], input[placeholder*="In" i]',
    "submit_button": 'button[type="submit"], button:has-text("Submit"), button:has-text("Save")',
}


@dataclass
class SubmissionResult:
    """Outcome of a single user's gate pass submission attempt."""

    name: str
    status: str  # "submitted" | "dry_run" | "skipped" | "failed"
    message: str
    screenshot_path: str | None = None


def current_ist_date() -> datetime:
    """Return the current datetime in IST (UTC+05:30)."""
    return datetime.now(IST)


def _save_screenshot(page: Page, user_name: str, tag: str, output_dir: Path) -> str:
    """Save a sanitized screenshot for debugging or audit verification."""
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_name.lower())
    path = output_dir / f"{safe_name}_{tag}.png"
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def _fill_first_visible(page: Page, selector: str, value: str, timeout_ms: int = 8000) -> bool:
    """Fill the first visible matching element; return True if found and filled."""
    locator = page.locator(selector).first
    try:
        locator.wait_for(state="visible", timeout=timeout_ms)
        locator.fill(value)
        return True
    except PlaywrightTimeoutError:
        return False


def submit_gatepass_for_user(
    browser: Browser,
    user: dict[str, Any],
    *,
    dry_run: bool = False,
    screenshot_dir: Path = Path("screenshots"),
    timeout_ms: int = 15000,
) -> SubmissionResult:
    """Run an isolated Playwright context to submit a gate pass for one user."""
    name = user.get("name", user.get("username", "unknown"))
    username = user.get("username", "")
    password = user.get("password", "")

    if not username or not password:
        return SubmissionResult(name=name, status="failed", message="Missing username or password.")

    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.set_default_timeout(timeout_ms)

    try:
        # 1. Open Camu portal
        page.goto(CAMU_PROFILE_URL, wait_until="domcontentloaded")

        # 2. Authenticate
        if not _fill_first_visible(page, SELECTORS["username_input"], username, timeout_ms=timeout_ms):
            shot = _save_screenshot(page, name, "login_field_missing", screenshot_dir)
            return SubmissionResult(
                name=name,
                status="failed",
                message="Could not locate username input on login page.",
                screenshot_path=shot,
            )

        if not _fill_first_visible(page, SELECTORS["password_input"], password, timeout_ms=timeout_ms):
            shot = _save_screenshot(page, name, "password_field_missing", screenshot_dir)
            return SubmissionResult(
                name=name,
                status="failed",
                message="Could not locate password input on login page.",
                screenshot_path=shot,
            )

        page.locator(SELECTORS["login_button"]).first.click()
        page.wait_for_load_state("networkidle", timeout=timeout_ms)

        # Detect if still on login page (bad credentials / OTP / CAPTCHA)
        if page.locator(SELECTORS["password_input"]).count() > 0 and page.locator(SELECTORS["password_input"]).first.is_visible():
            shot = _save_screenshot(page, name, "login_failed", screenshot_dir)
            return SubmissionResult(
                name=name,
                status="failed",
                message="Login did not complete (invalid credentials, OTP, or CAPTCHA).",
                screenshot_path=shot,
            )

        # 3. Navigate to Gate Pass module
        nav = page.locator(SELECTORS["gatepass_nav"]).first
        nav.wait_for(state="visible", timeout=timeout_ms)
        nav.click()
        page.wait_for_load_state("networkidle", timeout=timeout_ms)

        # Open Apply modal/form if present
        apply_btn = page.locator(SELECTORS["apply_button"]).first
        if apply_btn.count() > 0 and apply_btn.is_visible():
            apply_btn.click()

        # 4. Fill Gate Pass fields for today (Tuesday in IST)
        today_str = current_ist_date().strftime("%Y-%m-%d")
        out_time = user.get("out_time", "10:00")
        in_time = user.get("in_time", "20:00")
        reason = user.get("reason", "Personal work")
        pass_type = user.get("pass_type", "Day Outing")

        # Attempt to select pass type if dropdown is a native <select>
        select_loc = page.locator(SELECTORS["pass_type_select"]).first
        if select_loc.count() > 0 and select_loc.is_visible():
            tag_name = select_loc.evaluate("el => el.tagName.toLowerCase()")
            if tag_name == "select":
                select_loc.select_option(label=pass_type)
            else:
                select_loc.click()
                page.locator(f'text="{pass_type}"').first.click()

        _fill_first_visible(page, SELECTORS["reason_input"], reason, timeout_ms=5000)
        _fill_first_visible(page, SELECTORS["out_time_input"], f"{today_str} {out_time}", timeout_ms=5000)
        _fill_first_visible(page, SELECTORS["in_time_input"], f"{today_str} {in_time}", timeout_ms=5000)

        if dry_run:
            shot = _save_screenshot(page, name, "dry_run", screenshot_dir)
            return SubmissionResult(
                name=name,
                status="dry_run",
                message=f"Dry-run completed for {today_str} ({out_time}–{in_time}); submit skipped.",
                screenshot_path=shot,
            )

        # 5. Final Submit
        page.locator(SELECTORS["submit_button"]).first.click()
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        shot = _save_screenshot(page, name, "submitted", screenshot_dir)
        return SubmissionResult(
            name=name,
            status="submitted",
            message=f"Submitted for {today_str} ({out_time}–{in_time}).",
            screenshot_path=shot,
        )

    except Exception as exc:
        try:
            shot = _save_screenshot(page, name, "error", screenshot_dir)
        except Exception:
            shot = None
        return SubmissionResult(
            name=name,
            status="failed",
            message=f"{type(exc).__name__}: {exc}",
            screenshot_path=shot,
        )
    finally:
        context.close()
