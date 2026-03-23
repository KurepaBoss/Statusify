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
- **Python 3.8+** → https://www.python.org/downloads/  
- **Spotify** desktop app  
- **Spicetify** → https://spicetify.app  
- **Discord application ID** → https://discord.com/developers/applications  
- **Spicetify extension “Spicy Lyrics”** (install from Spicetify Marketplace → Extensions tab → Search for "Spicy Lyrics")

---

## Setup

### 1. Clone

```bash
git clone https://github.com/KurepaBoss/statusify.git
cd statusify
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Edit `.env`

Edit a file called `.env` in the folder:

```
DISCORD_APP_ID=your_id_here
```

Get your ID by creating a new application at:  
https://discord.com/developers/applications  

The application name is what shows as "Listening to ..." on your profile.  
"Spotify" is a commonly used name (tested), but you can name it anything you like.

---

### 4. Install the Spicetify extension

Copy `lyrics-bridge.js` to:

```
%appdata%\spicetify\Extensions\
```

Then apply:

```bash
spicetify apply
```

Make sure you also install the **“Spicy Lyrics” extension** from the Spicetify Marketplace (Extensions tab → Cart page), as it is required for synced lyrics to work properly.

---

### 5. Build & Run

Right-click `build_launcher.ps1` and select **Run with PowerShell**  
(or run it manually in PowerShell):

```powershell
.\build_launcher.ps1
```

After the build completes, the `.exe` will be created in the **same folder** as the script.

Run it:

```powershell
.\Statusify.exe
```

> If PowerShell blocks the script, run:
> ```
> Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
> ```

---

## Hotkeys

| Action | Default combo |
|--------|--------------|
| Skip track | `Ctrl+Alt+N` |
| Toggle RPC on/off | `Ctrl+Alt+S` |
| Skip instrumental | `Ctrl+Alt+I` |

All combos are configurable in **Settings → Hotkeys**.

---

## How it works

```
Spotify → Spicetify extension → WebSocket → Statusify → Discord RPC
```

1. The Spicetify extension (`lyrics-bridge.js`) hooks into Spotify's player and fetches synced lyrics from SpicyLyrics (falls back to Spotify's API)  
2. It streams track info, position ticks, and lyrics to Statusify over a local WebSocket  
3. Statusify determines the current lyric line and updates Discord Rich Presence  
4. Discord rate limits (5 updates / 20 seconds) are respected  

---

## Troubleshooting

**No lyrics showing**  
→ Run `spicetify apply` after copying `lyrics-bridge.js`

**RPC not connecting**  
→ Check your `DISCORD_APP_ID` and make sure Discord is running

**Wrong timing**  
→ Adjust lyric delay using the controls in the app  

---

## License

MIT
