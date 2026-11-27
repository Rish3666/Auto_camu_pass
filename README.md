# Auto Camu Pass

Automated weekly gate pass submitter for `https://student.camu.in/v2/profile` that runs every **Thursday and Friday between 09:00 and 23:00**.

Includes **three ways** to run it—choose what fits best:

1. **Chrome Extension (`extension/`) — Best for Friends (Zero Server, Self-Service)**
2. **Self-Service Telegram Bot (`telegram_bot.py`) — Runs 24/7 even when laptops are off**
3. **GitHub Actions Batch Runner (`runner.py`) — Cloud cron for a managed roster**

---

## 1. Chrome Extension (Recommended for Friends)

Each friend installs the extension in Chrome/Brave/Edge, saves their own credentials locally (`chrome.storage.local`), and never shares passwords with anyone.

### How Friends Install & Use It
1. Download the latest **Beta** `.zip` (e.g. `auto-camu-pass-extension-v0.1.0.zip`) from the **[Releases](../../releases)** page and unzip it.
2. Open Chrome and go to `chrome://extensions`.
3. Enable **Developer mode** (top-right toggle).
4. Click **Load unpacked** and select the unzipped folder.
5. Click the **Auto Camu Pass** extension icon:
   - Toggle **Auto-submit on Tuesdays** ON.
   - Enter Camu username, password, pass type, reason, and out/in times.
   - Click **Save Settings**, then **Dry-Run Test** to verify the form fills correctly.
6. Every Tuesday between **09:00 and 23:00**, as soon as Chrome is open, the extension checks every 15 minutes, submits once for that Tuesday, and shows a browser notification.

*(Note: A Chrome extension only runs while Chrome is open on Tuesday. If your laptop stays off all Tuesday, use the Telegram Bot or GitHub Actions option instead.)*

---

## 2. Self-Service Telegram Bot (24/7 Cloud)

Friends manage and submit their Camu gate passes directly from Telegram (using Camu's JSON REST API `crtLv` with encrypted SQLite credentials):

- `/register` — Guided setup with clickable quick-pick buttons for pass type (`Week End Gate Pass`), reason (`Weekend outing`, `Festival outing`, `Emergency outing`), and out/in time (`06:00 – 20:00`)
- `/update` — Update any individual setting (name, Camu login, pass type, reason, or time) without re-registering
- `/fill` — Fill & submit a **same-day outing for today** (defaults to **10 minutes from now → 8:00 PM**, or custom `HH:MM HH:MM` times)
- `/submit` — Immediately submit your **weekend gate pass** (`Friday → Sunday`) using your saved defaults
- `/status` — View your profile, last run status, and inline quick-action buttons
- `/test` — Dry-run verification against Camu's JSON API (no submission)
- `/pause`, `/resume`, `/delete`, `/cancel` — Manage or remove your account

```bash
cp .env.example .env
export TELEGRAM_BOT_TOKEN="your-botfather-token"
export CAMU_SECRET_KEY="$(python crypto_helper.py keygen)"
python telegram_bot.py
```

Or run 24/7 with Docker:
```bash
docker build -t auto-camu-bot .
docker run -d --name auto-camu-bot --env-file .env -v $(pwd)/data:/app/data auto-camu-bot
```

### What is `CAMU_SECRET_KEY` and how do I get it?
- `CAMU_SECRET_KEY` is **not** from Camu — it is a private Fernet (AES) encryption key you generate yourself to encrypt/decrypt stored Camu passwords in `data/users.db` (Telegram bot) or `users.enc` (GitHub Actions).
- Generate one locally by running:
  ```bash
  python crypto_helper.py keygen
  ```
- Paste the generated key into your `.env` file (`CAMU_SECRET_KEY=...`) or your hosting/GitHub Actions secrets. **Never commit `.env` or this key to Git**, and keep a backup (changing the key invalidates previously encrypted passwords in `data/users.db`).

---

## 3. GitHub Actions Batch Runner

See `.github/workflows/weekly_batch.yml` and `users.example.json`. Add `USERS_CONFIG` (or `users.enc` + `CAMU_SECRET_KEY`) in **GitHub Settings → Secrets and variables → Actions**.

---

## Running Unit Tests
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -v
```
