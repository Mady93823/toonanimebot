<div align="center">
  <h1>🍿 ToonWorld Anime Bot 🤖</h1>
  <p><i>A hyper-fast Telegram bot for downloading episodes from ToonWorld4All with Multi-Audio & Resolution Support!</i></p>
  
  <p>
    <img src="https://img.shields.io/badge/Python-3.9+-blue.svg" alt="Python Version">
    <img src="https://img.shields.io/badge/Telegram-Pyrogram-blue" alt="Pyrogram">
    <img src="https://img.shields.io/badge/Tool-yt--dlp-red" alt="YT-DLP">
  </p>
</div>

---

## ✨ Features

- **Interactive UI:** Interactive Telegram inline keyboards to select the exact video resolution (`1080p`, `720p`).
- **Multi-Audio Support:** Download individual audio tracks (Tamil, Hindi, Telugu, English, Japanese) or merge them all into a massive Multi-Audio MKV file!
- **Series Page Scraping:** Send a main show URL (`toonworld4all.me/show-name/`) and the bot will instantly scrape the page, giving you an interactive keyboard menu to pick which exact episode you want to download!
- **Playwright Interception:** Automates a headless Chromium browser with stealth modes to bypass Cloudflare protection and fetch hidden HLS streams.
- **2GB Upload Limits:** Powered by Pyrogram and Telegram's MTProto API to effortlessly upload files larger than the standard 50MB bot limit.
- **Format Toggle:** Choose to upload the final file to the chat as a streaming **Video** or a raw **Document**.
- **Admin & Auth System:** Highly secure access control. Master Admins (defined via `.env`) can dynamically authorize or revoke secondary users directly via Telegram commands.

---

## 🛡️ Admin Authentication System

The bot employs a dual-layer security model to ensure nobody can abuse your server resources to download episodes.

1. **Master Admins:** Any Telegram user ID placed inside your `.env` file's `ALLOWED_ADMIN_IDS` variable is considered a Master Admin.
2. **Dynamic Users:** Master Admins can securely authorize their friends or other channels to use the bot by sending commands directly to the bot chat:
   - `/auth 12345678` — Grants the specified User ID full access to the bot.
   - `/del 12345678` — Revokes their access immediately. 
   *(All authorized user IDs are securely backed up in a local `authorized_users.json` file inside your bot directory).*

---

## 🚀 Quick Setup (Local or VPS)

**1. Clone the repository**
```bash
git clone https://github.com/Mady93823/toonanimebot.git
cd toonanimebot
```

**2. Set up the Environment**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps
```

**3. Configure your API Tokens**
Copy the template file:
```bash
cp .env.example .env
```
Edit `.env` and fill in:
- `TELEGRAM_BOT_TOKEN` *(from @BotFather)*
- `TELEGRAM_API_ID` & `TELEGRAM_API_HASH` *(from my.telegram.org)*
- `ALLOWED_ADMIN_IDS` *(Your personal Telegram user ID)*

**4. Run the Bot**
```bash
python telegram_bot.py
```

---

## 🛠️ Deploying as a Background Service (Ubuntu/Systemd)

To keep your bot running 24/7 on a VPS, run it as a `systemd` service!

1. **Create the service file:**
```bash
sudo nano /etc/systemd/system/toonbot.service
```
2. **Paste this configuration** *(Make sure your paths match!)*:
```ini
[Unit]
Description=ToonWorld Telegram Anime Bot
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=/home/toonanimebot

# Points to your virtual environment's Python
ExecStart=/home/toonanimebot/venv/bin/python telegram_bot.py

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
3. **Start the service:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable toonbot
sudo systemctl start toonbot
```
Check if it's running via `sudo systemctl status toonbot`!

---
*Created for automated anime extraction from archive.toonworld4all.me*
