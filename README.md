<div align="center">
  <img src="https://raw.githubusercontent.com/KurepaBoss/Statusify/main/statusify.ico" width="100" />
  <h1>Statusify v1.1.0</h1>
  <p><strong>A sleek, lightweight dynamic overlay and backend bringing Spotify lyrics and rich presence fully under your control.</strong></p>
</div>

<br>

Statusify bridges the gap between Spotify and Discord, giving you an interactive, highly-customizable Discord RPC and a beautiful minimal GUI. Track your listening stats, view synced lyrics live from Spicetify, search through your song history, and fine-tune your exact Discord status in real-time.

![Statusify UI](https://img.shields.io/badge/Statusify-v1.1.0-brightgreen) ![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue) ![License: MIT](https://img.shields.io/badge/License-MIT-yellow)

---

## 🌟 What's New in v1.1.0 (The Mega Update)

Statusify v1.1.0 brings **8 massive additions** designed to make your experience smoother and highly tailored:

- **1️⃣ First-Run Setup Wizard:** No more editing hidden `.env` files. Statusify launches a clean setup dialog on your first load so you can quickly paste your Discord App ID.
- **2️⃣ Interactive Auto-Update Checker:** Never miss a feature. Statusify checks GitHub directly on launch and presents an interactive prompt containing all newest release notes.
- **3️⃣ Multi-Profile Discord Manager:** Have multiple Discord bots or alternate accounts? Save multiple App IDs directly inside settings and switch between them dynamically in 1 click.
- **4️⃣ Unified Session History & Lyric Search:** Find any song you've played! Your session history is now totally searchable—filter tracks by title, artist, or even **individual lines sung in the lyrics**.
- **5️⃣ In-Overlay Lyric Highlights:** Looking at the lyrics for a song? The new internal search highlights and jumps to the exact line you're searching for.
- **6️⃣ Custom Instrumental Mode:** Don't just settle for `🎵 ─ ─ ─ ─ ─ ─ ─ ─ ─ 🎵`. You can now inject custom instrumental or blank-text indicators straight from the Settings menu.
- **7️⃣ Paused-State RPC:** Toggle `"Show Paused on Discord"` to keep your RPC active with a `⏸ Paused` indicator when Spotify stops, instead of Discord completely wiping your status.
- **8️⃣ Crisper Typography (HiDPI Aware):** Massive UI engine bump fixing pixelated Tkinter fonts on Windows 10/11 laptops with scaling enabled. Crystal-clear text anywhere.

---

## 🚀 Quick Setup

1. **Install Python 3.10+**
2. **Install Spicetify** running the Spicetify Lyrics extension (provides lyric websocket).
3. **Install Requirements:**
   ```bash
   pip install pypresence websockets pillow python-dotenv
   # For hotkeys:
   pip install keyboard 
   ```
4. **Run Statusify!**
   ```bash
   python main.py
   ```
   *The Setup Wizard will walk you through creating and pasting your Discord Developer App ID.*

---

## 🎨 Features & Configuration

### Discord RPC Behaviour
The `statusify.cfg` configuration file remembers everything for you automatically when you use the UI:
- **Global Hotkeys:** Bind keyboard shortcuts (`Ctrl+Alt+S`) to globally hide/show your RPC, skip songs, or skip instrumental breaks.
- **Dark & Light Mode:** Toggleable dynamic themes with custom primary Accent colours.
- **Remember Session History:** Toggles whether Statusify keeps your track session alive in memory for later searches.

---

## 🤝 Contribution
Found a bug or want to request a feature? Feel free to open an issue or PR to help make Statusify even better.

**Made by [KurepaBoss](https://github.com/KurepaBoss)**
