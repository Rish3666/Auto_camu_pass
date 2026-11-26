# AGENTS.md — Auto Camu Pass Engineering & Handoff Guide

> **Living Document:** Update this file whenever architecture, selectors, release versions, or open tasks change. Any new contributor or AI agent should read this file first before making edits.

---

## 1. Project Overview
**Goal:** Automate weekly Gate Pass submissions on `https://student.camu.in/v2/profile` every **Thursday and Friday between 09:00 and 23:00 (local/IST)** so students don't have to submit manually.

The repository supports **three independent delivery modes** sharing the same core goal:
1. **Chrome Extension (`extension/`)** — *Primary self-service mode for friends.* Runs locally in each user's browser via Manifest V3 (`chrome.alarms` + `chrome.storage.local`). Zero server cost, zero shared passwords.
2. **Self-Service Telegram Bot (`telegram_bot.py` + `db.py`)** — 24/7 bot with encrypted SQLite (`CAMU_SECRET_KEY`) for users whose laptops are off all Tuesday.
3. **GitHub Actions Batch Runner (`runner.py` + `.github/workflows/weekly_batch.yml`)** — Scheduled Tuesday cloud cron for a managed `users.json` / `users.enc` roster.

---

## 2. Repository Map

| Path | Purpose |
| :--- | :--- |
| `extension/manifest.json` | Manifest V3 definition (`alarms`, `storage`, `tabs`, `scripting`, `notifications`, `https://student.camu.in/*`). |
| `extension/background.js` | Service worker: 15-min alarm, Tuesday 09:00–23:00 check, `lastSubmittedDate` dedup, background tab runner. |
| `extension/content.js` | Injected DOM automation: login, Gate Pass navigation, form fill, submit / dry-run. |
| `extension/popup/` | Extension popup UI (`popup.html`, `popup.css`, `popup.js`) for local settings & manual triggers. |
| `camu_client.py` | Playwright automation worker (`submit_gatepass_for_user`) with isolated `browser.new_context()` per user. |
| `runner.py` | Multi-user batch orchestrator with 15–45s staggered jitter and fault isolation. |
| `crypto_helper.py` | AES/Fernet keygen & encryption CLI for `users.json` → `users.enc`. |
| `db.py` | Encrypted SQLite CRUD (`upsert_user`, `get_user`, `set_enabled`, `delete_user`, `list_active_users`). |
| `telegram_bot.py` | `python-telegram-bot` v21+ self-service bot (`/register`, `/test`, `/submit`, `/pause`, `/resume`, `/delete`) + Tuesday `job_queue`. |
| `notifier.py` | Discord webhook & Telegram batch summary reporter. |
| `.github/workflows/weekly_batch.yml` | Tuesday 04:30 UTC (10:00 AM IST) batch runner + `workflow_dispatch` dry-run. |
| `.github/workflows/release_extension.yml` | Packages `extension/` into a `.zip` and attaches it to GitHub Releases on `v*` tags. |
| `tests/` | `pytest` unit tests (`test_core.py`, `test_db.py`). |

---

## 3. Development & Testing Rules

### Environment Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Mandatory Checks Before Pushing
1. **Run unit tests:**
   ```bash
   pytest -v
   ```
2. **Validate Chrome Extension manifest:**
   ```bash
   python3 -m json.tool extension/manifest.json > /dev/null
   ```
3. **Security / Secrets hygiene:**
   - Never commit `users.json`, `.env`, `*.key`, `data/`, or `screenshots/`.
   - In `extension/`, never use `eval()`, inline `<script>`, or `.then()` chains (always `async/await` per Manifest V3 rules).
   - Never store mutable state in `extension/background.js` global variables—persist everything in `chrome.storage.local`.

---

## 4. Release Process & Versioning (Chrome Extension)
Friends download the extension directly from **GitHub Releases** (no `git clone` required).

- **Versioning Scheme:**
  - `0.0.x` — Alpha releases (pre-release)
  - `0.1.x` — Beta releases (pre-release)
  - `1.0.0+` — Stable releases

To publish a new extension release:
1. Bump `"version"` in `extension/manifest.json`.
2. Update the **Changelog** section at the bottom of this file.
3. Commit and push to `main`, then create and push a version tag (e.g. `v0.1.1`):
   ```bash
   git tag v0.1.1
   git push origin v0.1.1
   ```
   `.github/workflows/release_extension.yml` will automatically zip `extension/` and publish the GitHub Release (marking `v0.*` as pre-release).

---

## 5. Current Status & Open Tasks (Next Agent Priorities)

- [ ] **Verify Live Camu DOM Selectors (`HIGH PRIORITY`):**
  - Both `extension/content.js` (`SELECTORS`) and `camu_client.py` (`SELECTORS`) use generic/best-guess selectors for `student.camu.in/v2/profile` because live credentials were not available during initial scaffolding.
  - **Next step:** Run `Dry-Run Test` in the extension (or `python runner.py --dry-run --headed`), inspect the actual Camu DOM / Network tab, and lock in exact selectors (or switch `content.js` to direct `fetch()` against Camu's internal gate pass API if cleaner).
- [ ] **Add Extension Icons:**
  - `extension/manifest.json` currently omits `"icons"` so Chrome uses its default icon cleanly. Generate `16x16`, `48x48`, and `128x128` PNG icons in `extension/icons/` and wire them up.
- [ ] **OTP / CAPTCHA Handling (if applicable):**
  - Confirm whether the target college's Camu login uses OTP or CAPTCHA. If yes, adjust `extension/content.js` to reuse an existing logged-in Camu session rather than re-authenticating.

---

## 6. Changelog (Keep Updated)

- **2026-11-27 (`v0.1.1` — Beta 1.1):**
  - Added `isCancelledOrRejected()` in `extension/background.js` so cancelled/rejected/inactive (`StFl !== 'A'`, `isCncl`, `Cancel`/`Reject`) passes in `getAllleave/` are ignored by duplicate checks.
  - Updated `checkCamuSubmittedToday()` and `extension/popup/popup.js` to always verify live against Camu and clear stale `lastSubmittedDate` when a user cancels their pass.
- **2026-11-27 (`v0.1.0` — Beta 1):**
  - Promoted the extension from Alpha (`v0.0.x`) to **Beta 1 (`v0.1.0`)**.
  - Updated versioning scheme (`0.0.x` = Alpha, `0.1.x` = Beta, `1.0.0+` = Stable) in `AGENTS.md`, `manifest.json`, and `.github/workflows/release_extension.yml`.
- **2026-11-27 (`v0.0.15`):**
  - Updated `extension/background.js` (`getWeekendDates()`) and `extension/popup/popup.js` so the Weekend Gate Pass always targets **Friday (Out date) → Sunday (In date)** whether triggered on Thursday or Friday, and deduplicates against that weekend's Friday date.
  - Cleaned up the Dry-run status message (removed temporary `sample=...` debug JSON).
- **2026-11-27 (`v1.0.0-alpha.14`):**
  - Locked pass resolution to prefer `WEEK_END_PASS` (`Week End Gate Pass`) over normal Leave passes and migrated any older stored `Day Outing` value in `extension/popup/popup.js`.
- **2026-11-27 (`v1.0.0-alpha.13`):**
  - Filtered duplicate checks in `extension/background.js` with `isGatePassEntry()` so regular Leaves don't block Weekend Gate Pass submissions.
  - Prioritized `passCd` (`WEEK_END_PASS`) from `/api/gatepasses/fetch-gatepasses-types`, used string `'true'` for `GateOutPass` (matching `HstleReq`), and updated default `passType` to `Week End Gate Pass`.
- **2026-11-27 (`v1.0.0-alpha.12`):**
  - Updated `resolvePassType()` in `extension/background.js` to prioritize `passCd` (e.g. `WEEK_END_PASS`) as the `passTy` ID and `code` (e.g. `Week End Gate Pass`) as the display label from `/api/gatepasses/fetch-gatepasses-types`.
- **2026-11-27 (`v1.0.0-alpha.11`):**
  - Updated `resolvePassType()` in `extension/background.js` to check past `GateOutPass` entries in `getAllleave/` for a known working `passTy`, prioritize `passTy`/`Code` fields over `_id` in `fetch-gatepasses-types`, and print the raw sample in Dry-run.
- **2026-11-27 (`v1.0.0-alpha.10`):**
  - Fixed flex text overflow in the popup profile card (`extension/popup/popup.html`, `extension/popup/popup.css`) by adding `flex: 1; min-width: 0; overflow: hidden;` on `.profile-info` and `.profile-text` so long names/emails truncate cleanly with ellipsis instead of overlapping the `Re-sync` button.
- **2026-11-27 (`v1.0.0-alpha.9`):**
  - Added live Camu check (`checkCamuSubmittedToday()` via `POST /api/Leave/getAllleave/` + `CHECK_TODAY_STATUS`) so manual gate pass submissions on `student.camu.in` also update the popup **`Submitted`** pin and green `OK` toolbar badge.
- **2026-11-27 (`v1.0.0-alpha.8`):**
  - Added **Submitted / Not submitted** status pin (`#submitted-pin`) in the popup header and a green `OK` toolbar badge (`chrome.action.setBadgeText`) in `extension/background.js` so users can immediately see whether today's gate pass has been submitted.
- **2026-11-27 (`v1.0.0-alpha.7`):**
  - Fixed `PASSTY_NOT_FOUND` by recursively searching `/api/gatepasses/fetch-gatepasses-types` (`findFirstArrayOfObjects` + `resolvePassType`) and surfacing resolved `passTy` + available types in Dry-run.
  - Enforced duplicate gate pass guard on both scheduled and manual `Submit now` runs via `getAllleave/`.
- **2026-11-27 (`v1.0.0-alpha.6`):**
  - Switched schedule from Tuesday to **Thursday & Friday (09:00–23:00)** across `extension/background.js` (`day === 4 || day === 5`), `extension/popup/*`, `runner.py`, `.github/workflows/weekly_batch.yml` (`30 4 * * 4,5`), and `telegram_bot.py` (`days=(4, 5)`).
- **2026-11-27 (`v1.0.0-alpha.5`):**
  - Added instant **Script ON / OFF** toggle (`TOGGLE_SCRIPT`) in `extension/popup/*` and `extension/background.js` that immediately persists state, ensures the 15-minute `chrome.alarms` schedule, and checks on browser launch (`onStartup`) + every 15m on Tuesdays (09:00–23:00).
- **2026-11-27 (`v1.0.0-alpha.4`):**
  - Switched Chrome Extension (`extension/background.js`) to **100% silent `fetch()` mode** using the captured Camu endpoints (`POST /api/Leave/getAllleave/`, `POST /api/gatepasses/fetch-gatepasses-types`, `POST /api/Leave/crtLv`)—no tabs opened on Dry-Run or Tuesday auto-submit.
  - Updated `extension/content.js` to extract `StuID`/`PerId`, `InId`, `loggedInId`, and `PerNm` from the logged-in Camu session.
  - Removed temporary `webRequest` API recorder from `manifest.json` and `popup.html`.
- **2026-11-26 (`v1.0.0-alpha.3`):**
  - Added built-in `chrome.webRequest` API Recorder in `extension/background.js` and `extension/popup/*` (`Start recording`, `Copy API log`, `Clear log`) to capture Camu's XHR/fetch endpoints & payloads with DevTools closed (passwords/tokens auto-redacted). Once captured once, endpoints will be converted to silent `fetch()` mode for all users.
- **2026-11-26 (`v1.0.0-alpha.2`):**
  - Registered `extension/content.js` as a persistent `content_scripts` entry on `https://student.camu.in/*` so opening/logging into Camu automatically syncs student details (`PROFILE_SYNCED`) without requiring a manual test run first.
  - Added "Open Camu" / "Re-sync" action (`OPEN_CAMU_AND_SYNC`) in `extension/background.js` and recursive storage + DOM profile extraction in `extension/content.js`.
  - Redesigned the extension popup (`extension/popup/*`) in **shadcn/ui** Zinc dark style (cards, switch toggle, outline/primary buttons, synced profile card, collapsible optional credentials).
- **2026-11-26 (`v1.0.0-alpha.1`):**
  - Marked extension release as **Alpha** (`prerelease: true` for `alpha`/`beta` tags in `.github/workflows/release_extension.yml`, `version_name: "1.0.0-alpha.1"` in `manifest.json`).
  - Added automatic student profile extraction from the logged-in Camu session (`extractStudentDetailsFromSession()` in `extension/content.js`) to auto-fill student name, ID/roll number, phone, and parent contact into Gate Pass forms and display the fetched profile in the popup.
  - Initialized Playwright multi-user runner (`runner.py`, `camu_client.py`, `crypto_helper.py`, `notifier.py`), self-service encrypted Telegram bot (`telegram_bot.py`, `db.py`), Manifest V3 Chrome Extension (`extension/`), and `AGENTS.md`.
