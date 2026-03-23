<div align="center">

<img src="statusify.ico" width="80" alt="Statusify logo">

# Statusify

**Show real-time Spotify lyrics as your Discord Rich Presence**

![Windows](https://img.shields.io/badge/Windows-10%2F11-0078D4?logo=windows)
![Python](https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white)
![Discord](https://img.shields.io/badge/Discord_RPC-5865F2?logo=discord&logoColor=white)
![Spicetify](https://img.shields.io/badge/Spicetify-1DB954?logo=spotify&logoColor=white)

</div>

---

Statusify connects to Spotify via a Spicetify extension, fetches synced lyrics, and updates your Discord status line by line in real time.

## Features

- 🎵 Line-by-line synced lyrics on your Discord profile
- 🎸 Automatic instrumental detection — shows `🎵 ─ ─ ─ ─ ─ ─ ─ ─ ─ 🎵` during gaps
- ⏱️ Adjustable lyric delay to sync to your ears
- ⏭️ Global hotkeys — skip track, toggle RPC, skip instrumental
- 📜 Song history with full lyrics viewer
- 🌓 Dark/light theme with accent colour picker
- 🚀 Optional Windows startup launch

---

## Requirements

- **Windows 10 or 11**
- **Python 3.8+** → [python.org](https://www.python.org/downloads/)
- **Spotify** desktop app
- **Spicetify** → [spicetify.app](https://spicetify.app)
- **Discord application ID** → [discord.com/developers](https://discord.com/developers/applications)

---

## Setup

### 1. Clone

```bash
git clone https://github.com/YOUR_USERNAME/statusify.git
cd statusify
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create `.env`

Create a file called `.env` in the folder:

```
DISCORD_APP_ID=your_id_here
```

Get your ID by making a new application at the [Discord Developer Portal](https://discord.com/developers/applications). The application name is what shows as "Listening to ..." on your profile.

### 4. Install the Spicetify extension

Copy `lyrics-bridge.js` to your Spicetify extensions folder:

```
%appdata%\spicetify\Extensions\
```

Then apply:

```bash
spicetify apply
```

### 5. Run

Double-click **`run.vbs`** — launches silently with no terminal window.

Use **`run.bat`** if you want to see the console output for debugging.

---

## Optional: Statusify.exe launcher

To show **Statusify** with the correct icon in Task Manager's startup tab (instead of "Python"):

1. Right-click `build_launcher.ps1` → **Run with PowerShell**
2. It compiles `Statusify.exe` using .NET's built-in C# compiler — no extra tools needed
3. Toggle **Launch at Windows startup** off and on in Settings to update the entry

---

## Hotkeys

| Action | Default combo |
|---|---|
| Skip track | `Ctrl+Alt+N` |
| Toggle RPC on/off | `Ctrl+Alt+S` |
| Skip instrumental | `Ctrl+Alt+I` |

All combos are configurable in **Settings → Hotkeys**.

---

## How it works

```
Spotify → Spicetify extension → WebSocket → Statusify → Discord RPC
```

1. The Spicetify extension (`lyrics-bridge.js`) hooks into Spotify's player and fetches synced lyrics from SpicyLyrics (falls back to Spotify's own lyrics API)
2. It streams track info, position ticks, and lyrics to Statusify over a local WebSocket
3. Statusify calculates which lyric line is current, groups short lines together, and pushes updates to Discord via the local RPC pipe
4. Discord's rate limit (5 updates per 20 seconds) is respected with a sliding window counter

---

## Troubleshooting

**No lyrics showing** — make sure `spicetify apply` was run after copying `lyrics-bridge.js`

**RPC not connecting** — check your `DISCORD_APP_ID` in `.env` and make sure Discord is running

**Wrong timing** — use the `±` buttons in the Now Playing tab to adjust lyric delay

---

## License

MIT
