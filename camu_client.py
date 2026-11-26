"""Playwright-authenticated Camu JSON API gate pass worker (no form DOM clicking)."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError

IST = timezone(timedelta(hours=5, minutes=30))
CAMU_BASE = "https://student.camu.in"
CAMU_PROFILE_URL = f"{CAMU_BASE}/v2/profile"

# Login-only selectors (after login, all Gate Pass operations use Camu's JSON REST APIs).
LOGIN_SELECTORS = {
    "username_input": 'input[type="text"], input[name="Email"], input[name="username"], input[placeholder*="User" i], input[placeholder*="Email" i]',
    "password_input": 'input[type="password"]',
    "login_button": 'button[type="submit"], button:has-text("Login"), button:has-text("Sign In")',
}

# Backwards-compatible alias
SELECTORS = LOGIN_SELECTORS

_EXTRACT_PROFILE_JS = """
() => {
  const details = { name: '', studentId: '', loggedInId: '', institutionId: '' };
  const walk = (root, visit, depth = 0, seen = new Set()) => {
    if (!root || typeof root !== 'object' || depth > 6 || seen.has(root)) return;
    seen.add(root);
    if (Array.isArray(root)) {
      for (const item of root) walk(item, visit, depth + 1, seen);
      return;
    }
    visit(root);
    for (const val of Object.values(root)) {
      if (val && typeof val === 'object') walk(val, visit, depth + 1, seen);
    }
  };
  const parseStorage = (storage) => {
    const out = [];
    for (let i = 0; i < storage.length; i++) {
      const raw = storage.getItem(storage.key(i));
      if (!raw || (!raw.startsWith('{') && !raw.startsWith('['))) continue;
      try { out.push(JSON.parse(raw)); } catch (_) {}
    }
    return out;
  };
  const pick = (obj, keys) => {
    for (const k of keys) {
      const v = obj[k];
      if (typeof v === 'string' && v.trim()) return v.trim();
      if (typeof v === 'number') return String(v);
    }
    return '';
  };
  const roots = [...parseStorage(window.localStorage), ...parseStorage(window.sessionStorage)];
  for (const root of roots) {
    walk(root, (obj) => {
      details.name ||= pick(obj, ['PerNm', 'FNa', 'StuNa', 'studentName', 'fullName', 'Name', 'name']);
      details.studentId ||= pick(obj, ['StuID', 'stuId', 'PerId', 'studentId']);
      details.loggedInId ||= pick(obj, ['loggedInId', 'userId', 'usrId']);
      details.institutionId ||= pick(obj, ['InId', 'inId', 'institutionId']);
    });
  }
  if (!details.loggedInId) {
    for (const root of roots) {
      walk(root, (obj) => {
        if (!details.loggedInId && typeof obj._id === 'string' && obj._id !== details.studentId && obj._id.length === 24) {
          details.loggedInId = obj._id;
        }
      });
    }
  }
  return details;
}
"""

_CAMU_POST_JS = """
async ([path, body]) => {
  const init = {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
  };
  if (body !== null && body !== undefined) {
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(`https://student.camu.in${path}`, init);
  const text = await resp.text();
  let json = null;
  try { json = JSON.parse(text); } catch (_) {}
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status} on ${path}: ${text.slice(0, 140)}`);
  }
  return json ?? text;
}
"""


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


def get_weekend_dates(now: datetime | None = None) -> tuple[str, str]:
    """Return (friday_date_str, sunday_date_str) for the current/upcoming weekend in IST."""
    ref = now or current_ist_date()
    days_until_friday = 0 if ref.weekday() == 4 else (4 - ref.weekday() + 7) % 7
    friday = ref + timedelta(days=days_until_friday)
    sunday = friday + timedelta(days=2)
    return friday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def build_utc_iso_from_ist(date_str: str, time_str: str) -> str:
    """Convert an IST date ('YYYY-MM-DD') and 24h time ('HH:MM') to UTC ISO-8601 string."""
    try:
        dt_ist = datetime.strptime(f"{date_str} {time_str or '10:00'}", "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        return dt_ist.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except ValueError:
        return f"{date_str}T04:30:00.000Z"


def find_first_array_of_objects(root: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Recursively locate the first non-empty list of dicts inside a Camu API response."""
    if root is None or depth > 5:
        return []
    if isinstance(root, list):
        return [x for x in root if isinstance(x, dict)] if (root and isinstance(root[0], dict)) else []
    if isinstance(root, dict):
        for val in root.values():
            found = find_first_array_of_objects(val, depth + 1)
            if found:
                return found
    return []


def is_gate_pass_entry(leave_obj: dict[str, Any] | None) -> bool:
    if not leave_obj:
        return False
    if leave_obj.get("GateOutPass") in (False, "false"):
        return False
    return bool(
        leave_obj.get("GateOutPass") in (True, "true")
        or leave_obj.get("passTy")
        or leave_obj.get("PassTy")
        or leave_obj.get("passCd")
    )


def is_leave_for_date(leave_obj: dict[str, Any] | None, target_date_str: str) -> bool:
    if not leave_obj:
        return False
    for key in ("FrDt", "frdt"):
        raw = leave_obj.get(key)
        if not raw:
            continue
        if str(raw).startswith(target_date_str):
            return True
    return False


def resolve_pass_type(
    types_list: list[dict[str, Any]],
    desired_label: str = "Week End Gate Pass",
    existing_leaves: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve passTy code (preferring WEEK_END_PASS) from fetch-gatepasses-types."""
    leaves = existing_leaves or []
    past_pass = next((lv for lv in leaves if is_gate_pass_entry(lv) and (lv.get("passTy") or lv.get("PassTy"))), None)
    past_pass_ty = str(past_pass.get("passTy") or past_pass.get("PassTy") or "").strip() if past_pass else ""

    if not types_list:
        return {
            "id": past_pass_ty,
            "label": past_pass_ty,
            "gate_out_pass_val": past_pass.get("GateOutPass", "true") if past_pass else "true",
            "lv_ty_val": past_pass.get("LvTy", "Leave") if past_pass else "Leave",
        }

    def get_label(item: dict[str, Any]) -> str:
        for k in ("Nm", "Name", "name", "code", "Code", "PassNm", "passNm", "Desc", "description", "label", "Title", "title", "PassTy", "passTy", "type"):
            val = item.get(k)
            if val is not None and str(val).strip():
                return str(val).strip()
        return ""

    def get_id(item: dict[str, Any]) -> str:
        for k in ("passCd", "PassCd", "passTy", "PassTy", "Cd", "_id", "id", "code", "Code", "value"):
            val = item.get(k)
            if val is not None and str(val).strip():
                return str(val).strip()
        return get_label(item)

    target = (desired_label or "Week End Gate Pass").strip().lower()
    weekend_match = next(
        (
            item
            for item in types_list
            if str(item.get("passCd") or "").upper() == "WEEK_END_PASS"
            or "week end" in get_label(item).lower()
        ),
        None,
    )
    match = next(
        (
            item
            for item in types_list
            if target
            and (
                (get_label(item) and (get_label(item).lower() == target or target in get_label(item).lower() or get_label(item).lower() in target))
                or get_id(item).lower() == target
            )
        ),
        None,
    ) or weekend_match
    chosen = match or types_list[0]
    return {
        "id": get_id(chosen) or past_pass_ty,
        "label": get_label(chosen) or past_pass_ty,
        "gate_out_pass_val": past_pass.get("GateOutPass", "true") if past_pass else "true",
        "lv_ty_val": past_pass.get("LvTy", "Leave") if past_pass else "Leave",
    }


def build_crtlv_payload(
    *,
    profile: dict[str, str],
    resolved_type: dict[str, Any],
    friday_date_str: str,
    sunday_date_str: str,
    out_time: str = "06:00",
    in_time: str = "20:00",
    reason: str = "Weekend outing",
) -> dict[str, Any]:
    """Construct the exact JSON payload expected by POST /api/Leave/crtLv."""
    student_id = profile.get("studentId", "")
    return {
        "Attachments": [],
        "FrDt": f"{friday_date_str}T00:00:00.000Z",
        "GateOutPass": resolved_type.get("gate_out_pass_val", "true"),
        "HstleReq": "true",
        "InId": profile.get("institutionId", ""),
        "LvTy": resolved_type.get("lv_ty_val", "Leave"),
        "PerId": student_id,
        "PerNm": profile.get("name", ""),
        "PerTy": "Student",
        "PtAb": "",
        "Rson": "",
        "Sson": "",
        "ToDt": f"{sunday_date_str}T00:00:00.000Z",
        "atcmnt": [],
        "frdt": build_utc_iso_from_ist(friday_date_str, out_time),
        "loggedInId": profile.get("loggedInId") or student_id,
        "passTy": resolved_type.get("id", ""),
        "periods": [],
        "rson": reason or "Weekend outing",
        "timezoneOffSet": 330,
        "todt": build_utc_iso_from_ist(sunday_date_str, in_time),
    }


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
    """Authenticate in an isolated Playwright context, then submit via Camu's JSON REST APIs."""
    name = user.get("name", user.get("username", "unknown"))
    username = user.get("username", "")
    password = user.get("password", "")

    if not username or not password:
        return SubmissionResult(name=name, status="failed", message="Missing username or password.")

    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.set_default_timeout(timeout_ms)

    try:
        # 1. Open Camu portal & authenticate to obtain session cookies + storage profile
        page.goto(CAMU_PROFILE_URL, wait_until="domcontentloaded")

        if not _fill_first_visible(page, LOGIN_SELECTORS["username_input"], username, timeout_ms=timeout_ms):
            shot = _save_screenshot(page, name, "login_field_missing", screenshot_dir)
            return SubmissionResult(
                name=name,
                status="failed",
                message="Could not locate username input on login page.",
                screenshot_path=shot,
            )

        if not _fill_first_visible(page, LOGIN_SELECTORS["password_input"], password, timeout_ms=timeout_ms):
            shot = _save_screenshot(page, name, "password_field_missing", screenshot_dir)
            return SubmissionResult(
                name=name,
                status="failed",
                message="Could not locate password input on login page.",
                screenshot_path=shot,
            )

        page.locator(LOGIN_SELECTORS["login_button"]).first.click()
        page.wait_for_load_state("networkidle", timeout=timeout_ms)

        if page.locator(LOGIN_SELECTORS["password_input"]).count() > 0 and page.locator(LOGIN_SELECTORS["password_input"]).first.is_visible():
            shot = _save_screenshot(page, name, "login_failed", screenshot_dir)
            return SubmissionResult(
                name=name,
                status="failed",
                message="Login did not complete (invalid credentials, OTP, or CAPTCHA).",
                screenshot_path=shot,
            )

        # 2. Extract StuID / InId / loggedInId / PerNm from browser storage
        profile: dict[str, str] = {}
        for _ in range(5):
            profile = page.evaluate(_EXTRACT_PROFILE_JS)
            if profile.get("studentId"):
                break
            page.wait_for_timeout(1000)

        if not profile.get("studentId"):
            shot = _save_screenshot(page, name, "profile_missing", screenshot_dir)
            return SubmissionResult(
                name=name,
                status="failed",
                message="Logged in, but could not extract StuID from Camu browser storage.",
                screenshot_path=shot,
            )

        friday_str, sunday_str = get_weekend_dates()
        out_time = user.get("out_time", "06:00")
        in_time = user.get("in_time", "20:00")
        reason = user.get("reason", "Weekend outing")
        pass_type = user.get("pass_type", "Week End Gate Pass")

        # 3. Direct JSON POST /api/Leave/getAllleave/ (duplicate gate pass guard)
        leaves_res = page.evaluate(
            _CAMU_POST_JS,
            ["/api/Leave/getAllleave/", {"StuID": profile["studentId"], "isFrmReact": True}],
        )
        existing_leaves = find_first_array_of_objects(leaves_res)
        already_exists = any(
            is_gate_pass_entry(lv) and is_leave_for_date(lv, friday_str) for lv in existing_leaves
        )
        if already_exists and not dry_run:
            return SubmissionResult(
                name=name,
                status="skipped",
                message=f"Weekend gate pass already exists for Fri {friday_str} → Sun {sunday_str}.",
            )

        # 4. Direct JSON POST /api/gatepasses/fetch-gatepasses-types
        types_res = page.evaluate(_CAMU_POST_JS, ["/api/gatepasses/fetch-gatepasses-types", None])
        types_list = find_first_array_of_objects(types_res)
        resolved_type = resolve_pass_type(types_list, pass_type, existing_leaves)

        if not resolved_type.get("id"):
            return SubmissionResult(
                name=name,
                status="failed",
                message=f"PASSTY_NOT_FOUND in fetch-gatepasses-types: {str(types_res)[:140]}",
            )

        payload = build_crtlv_payload(
            profile=profile,
            resolved_type=resolved_type,
            friday_date_str=friday_str,
            sunday_date_str=sunday_str,
            out_time=out_time,
            in_time=in_time,
            reason=reason,
        )

        if dry_run:
            return SubmissionResult(
                name=name,
                status="dry_run",
                message=(
                    f"Dry-run OK (JSON API): {resolved_type['label'] or resolved_type['id']} ({resolved_type['id']}) "
                    f"for Fri {friday_str} ({out_time}) → Sun {sunday_str} ({in_time})."
                ),
            )

        # 5. Direct JSON POST /api/Leave/crtLv (no DOM clicking)
        page.evaluate(_CAMU_POST_JS, ["/api/Leave/crtLv", payload])
        return SubmissionResult(
            name=name,
            status="submitted",
            message=f"Submitted via JSON API: Fri {friday_str} ({out_time}) → Sun {sunday_str} ({in_time}).",
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
