<div align="center">
  <img src="https://raw.githubusercontent.com/KurepaBoss/Statusify/main/statusify_icon_preview.png" width="128" />
  <h1>Statusify v1.2.0</h1>
  <p><strong>The ultimate Discord Rich Presence & Spotify Lyrics bridge.</strong></p>

  ![Statusify v1.2.0](https://img.shields.io/badge/Statusify-v1.2.0-brightgreen?style=for-the-badge)
  ![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge)
  ![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078d6?style=for-the-badge)
  ![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)
</div>

---

## 🌟 Overview
**Statusify** is a lightweight, high-performance bridge that connects your Spotify listening experience directly to Discord and your desktop. It offers a beautiful, High-DPI aware GUI to track your session history, view synced lyrics, and manage multiple Discord profiles with a single click.

Lyrics come straight from Spicetify over a local WebSocket — no API keys, no polling a web service, no rate limits. The bridge extension pushes the current track and its exact playback position every 500 ms, and Statusify maps that position to the right lyric line before it reaches Discord.

---

## 🆕 What's New in v1.2.0

**A fully redesigned interface.** The GUI now runs on a real animation engine — eased fades and transitions, smooth scrolling, hover and focus states on every control, and rounded album art. Accent colours are blended and contrast-checked at runtime, so text stays readable against whatever accent you pick, in both light and dark themes.

**No more zombie processes.** Closing Statusify could leave `pythonw.exe` running with no window, still holding the single-instance lock and port 8765 — so the next launch refused to start and Task Manager was the only way out. Shutdown now closes the Discord pipe first to unblock stuck reader threads, then exits without waiting on them.

**Launching a second copy now shows the first one.** Previously it just exited silently and looked like nothing had happened. It now brings the running window to the front, and tells you plainly if the running copy is wedged instead of leaving you guessing.

**Stale-bridge detection.** Statusify compares the bridge extension in your Spicetify folder against the copy actually injected into Spotify and warns you when they differ — the situation that used to look like "lyrics randomly stopped working." See [step 2 of the setup](#2-prepare-spicetify-for-lyrics) for why restarting Spotify never fixed it.

**Better diagnostics.** Every log line, including everything from the Spicetify bridge, now lands in a timestamped `statusify.log` next to `main.py` — so "presence stopped working an hour ago" is answerable after the fact. The file is size-capped, and crashes are captured too, even under `pythonw.exe`, which has no console for stderr to go to.

---

## ✨ Key Features

**Presence & lyrics**
- 🎤 **Synced lyrics** on your Discord status, driven by Spicetify's exact playback position.
- 🎸 **Instrumental handling** — detects instrumental gaps and shows your own custom text instead of a blank line.
- ⏸️ **Paused indicator** — optionally keep a "Paused" status instead of clearing your presence.
- 🖼️ **Album art** on the presence, with a local disk cache so the same track never re-downloads.
- ⏱️ **Lyric timing offset** — a global delay slider, plus a per-track offset that is remembered for songs whose lyrics are permanently early or late.

**The app itself**
- 🚀 **Zero-config startup** — a setup wizard on first run and self-installing dependencies.
- 📂 **Session history** — a searchable database of everything you've listened to; filter by song, artist, or even lyric content, and export any track's lyrics as a timestamped `.lrc` or plain `.txt`.
- 🎭 **Multi-profile support** — manage multiple Discord Application IDs and switch between them instantly.
- 🪟 **Mini mode** — collapse to a compact, always-visible bar.
- 🔔 **System tray** — close-to-tray, so Statusify keeps running out of the way.
- 🎨 **Themes** — smooth dark and light modes with custom accent colours.
- ⌨️ **Global hotkeys** for toggling RPC, skipping the current track, and skipping instrumentals.
- 🚫 **Blacklist** — case-insensitive terms matched against artist and title, so anything you'd rather not broadcast never reaches Discord.
- 📊 **Session stats** — songs played and total listening time.
- ⚙️ **Start with Windows**, optionally minimised.
- 🔄 **Update checker** — reads the repo's releases in the background and shows you the changelog for anything newer.
- 🖥️ **Retina-ready UI** — native High-DPI support for crystal-clear text on any Windows scaling mode.

---

## ⬇️ Download

<div align="center">

### **[Download Statusify.exe](https://github.com/KurepaBoss/Statusify/releases/latest)**

</div>

One file, no Python required — Python and every dependency are compiled in. Put it in a folder you intend to keep (it stores your settings, history and logs beside itself), then double-click it.

> **Windows will warn you the first time.** The exe is unsigned, so SmartScreen shows "Windows protected your PC" → **More info** → **Run anyway**. Each release publishes a `Statusify.exe.sha256` you can check against `Get-FileHash Statusify.exe -Algorithm SHA256` if you want to confirm the download is byte-for-byte what the build workflow produced. Some antivirus engines also flag it, because Statusify registers a global keyboard hook (for the hotkeys) and opens a local socket (for the Spicetify bridge) — both visible in the source above.

Prefer running from source? That's the next section, and it's still the better option if you want to modify anything.

---

## 🚀 Easy Setup (Tutorial)

Setting up Statusify is simpler than ever. Follow these **3 steps** to get started:

### 1. Install Requirements
- **Using `Statusify.exe`?** Skip this step — there is nothing to install.
- From source, ensure you have [Python 3.10 or higher](https://www.python.org/downloads/) installed.
- **Note:** Statusify will automatically install all necessary Python libraries for you when you launch it for the first time. If you'd rather do it yourself: `pip install -r requirements.txt`.

### 2. Prepare Spicetify (For Lyrics)
To see lyrics on your Discord status, you need [Spicetify](https://spicetify.app/):
1. **Open your Spicetify Marketplace** in the Spotify app.
2. Go to the **Extensions** tab and install **Spicy Lyrics**.
3. Statusify automatically copies its bridge extension into your Spicetify folder when you launch it.
4. **Run `spicetify apply`.** This is the step that matters, and restarting Spotify is *not* a substitute for it.

> **Why `spicetify apply` and not just a restart?**
> Spicetify keeps two copies of every extension: the source in
> `%APPDATA%\spicetify\Extensions`, and an injected copy inside Spotify's own
> `xpui` bundle. Spotify only ever runs the injected one, and only
> `spicetify apply` updates it. Restarting Spotify re-runs whatever was
> injected last time — so an outdated bridge survives any number of restarts.
> Statusify compares the two on every launch and warns you when they differ.

> If Spicy Lyrics has no lyrics for a track, Statusify falls back to Spotify's own lyrics and says so in the log, rather than silently going quiet. If lyrics are unavailable from both, it still publishes the track title, artist and album art to Discord — a lyric problem never means a blank Rich Presence.

### 3. Launch & Connect
1. Double-click **`Statusify.exe`**, or from source:
   ```bash
   python main.py
   ```
   From source on Windows you can also use `run.bat`, or `run.vbs` for a fully windowless start.
2. **Setup Wizard:** On the first run, Statusify will ask for your **Discord Application ID**. Follow the link provided in the popup to create one in 30 seconds.
3. **Enjoy!** Your Spotify status and lyrics will now sync beautifully to Discord.

> Prefer to configure it up front? Copy `.env.example` to `.env` and put your Application ID there — the wizard is skipped entirely.

---

## ⚙️ Configuration

Your preferences live in `statusify.cfg`, written next to `main.py` — or next to `Statusify.exe` if you're using the release build — and never committed. Everything in it is editable from the Settings tab:

| Setting | Notes |
| --- | --- |
| **Theme** | Dark / light, plus a custom accent colour. |
| **Hotkeys** | Defaults: `Ctrl+Alt+S` toggle RPC, `Ctrl+Alt+N` skip track, `Ctrl+Alt+I` skip instrumental. |
| **Lyric delay** | Global offset in ms, with a per-track override for stubborn songs. |
| **Behaviour** | Close-to-tray, always-on-top, start minimised. |
| **Startup** | Launch Statusify with Windows. |
| **Discord RPC** | Paused indicator, custom instrumental text, profile switching. |
| **History** | Toggle session recording on/off. |

**Files Statusify creates at runtime**, in its own folder (all gitignored):

| File | What it is |
| --- | --- |
| `statusify.cfg` | Your preferences and window geometry. |
| `.env` | Your Discord Application ID. |
| `history.json` | Session history, including stored lyrics. |
| `statusify.log` | Timestamped diagnostic log; rotates to `.old` past 512 KB. |
| `.artcache/` | Cached album art, keyed by URL hash. |
| `exports/` | Lyrics you export as `.lrc` / `.txt`. |

---

## 🧩 How the bridge works

```
Spotify + Spicetify ──[ lyrics-bridge.js ]──► ws://127.0.0.1:8765 ──► main.py ──► Discord RPC
      track + position, every 500 ms                                  the lyric line for this instant
```

`lyrics-bridge.js` is installed into Spicetify for you on launch. See [INSTALL.md](INSTALL.md) if you want to install or debug it by hand.

---

## 🩺 Troubleshooting

**Lyrics or presence stopped working.** Check `statusify.log` first — it sits next to `main.py` and includes every message from the Spicetify bridge, prefixed `[Bridge]`.

**Statusify warns that the bridge is out of date.** Run `spicetify apply`. Restarting Spotify will not help; see the note above.

**My settings and history keep disappearing (exe build).** `Statusify.exe` stores its data in the folder it runs from. Running it straight out of a temp folder, an unzipped-on-the-fly archive, or anywhere Windows cleans up will take your config with it — move the exe somewhere permanent. A read-only location will fail to save for the same reason; `statusify.log` records the write error.

**"Statusify is already running" but there's no window.** Launch it again — the running instance will bring its window to the front. If it tells you the running copy isn't responding, quit it from the system-tray icon and start it again.

**No lyrics on one specific track.** Confirm Spicy Lyrics itself has lyrics for it in Spotify. Statusify falls back to Spotify's own lyrics and logs the fallback; if neither source has them, presence still shows title, artist and album art.

**Verify the bridge is connected.** Open Spotify DevTools (`Ctrl+Shift+I`) and look for `[LyricsBridge] Connected to Python.` in the Console tab.

---

## 🛠️ Development

```bash
pip install -r requirements.txt
pip install pytest
pytest
```

The suite is headless — it stubs out tkinter, Discord and Spotify, so it runs anywhere without a display. `tests/legacy/` holds standalone diagnostic scripts from earlier debugging sessions; they are excluded from collection on purpose (see `pytest.ini`) and are run directly, e.g. `python tests/legacy/test_freeze_fix.py`.

### Building the release executable

```powershell
.\build_exe.ps1        # -> dist\Statusify.exe
```

That creates its own `.buildenv` venv, installs PyInstaller, and runs `Statusify.spec`. Pushing a `v*` tag does the same thing on a clean Windows runner and attaches the exe plus its SHA256 to the GitHub release — see `.github/workflows/release.yml`.

Two notes for anyone touching the packaging:

- **`_APP_DIR` vs `_RES_DIR`.** User data (config, history, logs, caches) resolves from `_APP_DIR`, which points at the folder holding the exe when frozen. Bundled read-only files (`lyrics-bridge.js`, `statusify.ico`) resolve from `_RES_DIR`, which is `sys._MEIPASS`. Mixing them up means either losing every setting on exit or never being able to install the bridge.
- **`upx=False` in the spec is deliberate.** UPX packing is a strong antivirus heuristic trigger, and a global keyboard hook plus a listening socket is already an awkward combination for scanners.

`build_launcher.ps1` is a different, smaller thing: it compiles a 9 KB shim that launches `main.py` with your local `pythonw.exe`, for running from source without a console window.

---

## 🤝 Contributing
Found a bug or have a suggestion? Open an [Issue](https://github.com/KurepaBoss/Statusify/issues) or submit a Pull Request.

## 📄 License
Released under the [MIT License](LICENSE).

**Made with ❤️ by [KurepaBoss](https://github.com/KurepaBoss)**
