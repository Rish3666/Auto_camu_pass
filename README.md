# Auto Camu Pass (Multi-User)

Automated weekly gate pass submitter for `https://student.camu.in/v2/profile` built with **Python + Playwright + GitHub Actions**. Runs every **Tuesday at 10:00 AM IST** (inside the 09:00–23:00 window) for you and all your friends with isolated browser sessions, staggered delays, and Discord/Telegram notifications.

---

## Setup Guide

### 1. Choose How to Store Friends' Credentials
Never commit plaintext passwords (`users.json` is in `.gitignore`). Pick one of the two options below:

#### Option A: Store JSON Directly in GitHub Secrets (Simplest)
1. Copy `users.example.json` and fill in your friends' details.
2. In your GitHub repo, go to **Settings → Secrets and variables → Actions → New repository secret**:
   - Name: `USERS_CONFIG`
   - Value: Paste the entire JSON array.

#### Option B: Encrypted `users.enc` File in Repo
1. Generate a secret key:
   ```bash
   python crypto_helper.py keygen
   ```
2. Encrypt your `users.json`:
   ```bash
   python crypto_helper.py encrypt --input users.json --output users.enc --key "<YOUR_KEY>"
   ```
3. Commit `users.enc` (safe—it is AES/Fernet encrypted) and add `CAMU_SECRET_KEY` to **GitHub Actions Secrets**.

---

### 2. Optional: Notifications (Discord / Telegram)
Add any of these to **GitHub Actions Secrets**:
- `DISCORD_WEBHOOK_URL`: Posts a weekly batch summary to your group Discord channel.
- `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`: Posts the summary to a Telegram group (plus individual DMs if `telegram_chat_id` is set per friend in the roster).

---

### 3. Test Safely with Dry-Run
Before Tuesday:
1. Go to your repo's **Actions** tab → **Weekly Camu Gate Pass Batch** → **Run workflow**.
2. Check **`dry_run = true`** and click **Run workflow**.
3. Download the `camu-screenshots-*` artifact from the completed run to inspect the filled form before live submission.

---

## Local Testing
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Run unit tests
pytest

# Run a local headed dry-run (create users.json locally first)
python runner.py --dry-run --headed
```
