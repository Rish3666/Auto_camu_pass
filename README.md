# Auto Camu Pass

Automated weekly gate pass submitter for `https://student.camu.in/v2/profile` that runs every **Tuesday between 09:00 and 23:00**.

Includes **three ways** to run it—choose what fits best:

1. **Chrome Extension (`extension/`) — Best for Friends (Zero Server, Self-Service)**
2. **Self-Service Telegram Bot (`telegram_bot.py`) — Runs 24/7 even when laptops are off**
3. **GitHub Actions Batch Runner (`runner.py`) — Cloud cron for a managed roster**

---

## 1. Chrome Extension (Recommended for Friends)

Each friend installs the extension in Chrome/Brave/Edge, saves their own credentials locally (`chrome.storage.local`), and never shares passwords with anyone.

### How Friends Install & Use It
1. Download `auto-camu-pass-extension-v1.0.0.zip` from the **[Releases](../../releases)** page and unzip it.
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

Friends message your bot with `/register`, `/test`, `/pause`, `/resume`, `/delete`.

```bash
export TELEGRAM_BOT_TOKEN="your-botfather-token"
export CAMU_SECRET_KEY="$(python crypto_helper.py keygen)"
python telegram_bot.py
```

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
