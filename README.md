<div align="center">
  <img src="https://raw.githubusercontent.com/KurepaBoss/Statusify/main/statusify_icon_preview.png" width="128" />
  <h1>Statusify v2.0.1</h1>
  <p><strong>The ultimate Discord Rich Presence & Spotify Lyrics bridge.</strong></p>

  ![Statusify v2.0.1](https://img.shields.io/badge/Statusify-v2.0.1-brightgreen?style=for-the-badge)
  ![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge)
  ![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078d6?style=for-the-badge)
  ![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)
  [![Tests](https://github.com/KurepaBoss/Statusify/actions/workflows/tests.yml/badge.svg)](https://github.com/KurepaBoss/Statusify/actions/workflows/tests.yml)

  <img src="docs/preview.png" alt="Statusify 2.0: the lyric sheet over the album's flowing colours with playback controls, and the Settings page with listening stats" width="760" />
  <br><sub>The lyric sheet takes its colours from the cover. The cover, song and lyrics in this screenshot are made up.</sub>
</div>

---

## 🌟 Overview
**Statusify** is a lightweight, high-performance bridge that connects your Spotify listening experience directly to Discord and your desktop. It offers a beautiful, High-DPI aware GUI to track your session history, view synced lyrics, and manage multiple Discord profiles with a single click.

Lyrics come straight from Spicetify over a local WebSocket — no API keys, no polling a web service, no rate limits. The bridge extension pushes the current track and its exact playback position every 500 ms, and Statusify maps that position to the right lyric line before it reaches Discord.

---

## 🆕 What's New in v2.0.1

**Settings could flip a setting you didn't touch.** In 2.0.0, when the Settings page re-laid itself out (for example as the listening stats finished loading), clicking an ordinary row could toggle a switch that had been in that spot before. Every click target is now unique to the current layout. If a setting changed without you asking in 2.0.0 (*Start minimised to the tray* is the likely one), set it back once and it will stay.

Everything new in Statusify 2.0 is listed under v2.0.0 below.

---

## 🆕 What's New in v2.0.0

Statusify 2.0 is a new app on the outside. Everything since v1.4.0 is in this release.

**A lyric sheet that moves with the music.** The Lyrics page is rebuilt from scratch. The background is the album cover's own colours drifting slowly like thick water, heavily blurred, and it flows into the next cover's colours when the track changes. When a new line starts, the sheet glides up: the new line sharpens and brightens, and lines further away soften and dim. Colours are kept dark (or light) enough that the words stay readable on any cover. Japanese, Chinese, Korean and Thai lyrics are drawn with the right fonts.

**Control Spotify from Statusify.** Previous, play/pause and next buttons sit under the progress bar. The bar is a seek bar: hover it to see the time under the pointer, then click or drag. Scroll the lyrics with the mouse wheel to read ahead or back, hover a line to highlight it, and click it to jump there. The sheet drifts back to the line being sung a few seconds after you stop. Keyboard: `Space` play/pause, `←`/`→` seek 5 s, `Ctrl+←`/`Ctrl+→` previous/next.

**A redesigned Settings page.** Grouped cards, one clear row per setting with a short explanation, proper switches and buttons, and scrolling that no longer tears or leaves rows behind. The Listening section now shows big, readable numbers for this session, the last 7 days and all time, plus your top five artists for each.

**Faster on slower PCs.** The lyric page measures how long it takes to draw and eases off on its own (fewer frames, then a still background) when a PC is struggling. You can also pick *Auto*, *Smooth* or *Fast* under *Appearance → Performance*. Unchanged frames are skipped, lyric lines are drawn only when they come on screen, lines land exactly on time even at a low frame rate, and the stats queries moved off the UI thread. Pausing no longer makes Statusify rewrite its database twice a second.

**History that actually persists.** History used to be written only when you clicked Quit, so logging off, a crash or an update threw the session away. It now lives in a small SQLite database (`history.db`) and every play is saved the moment it starts. Your old `history.json` is imported automatically. The History tab shows restored history again, and search covers everything you've played: title, artist and lyrics.

**Lyrics more often, and faster.** Lyrics for a track you've heard before load instantly from a local cache. When Spicy Lyrics and Spotify both come up empty, Statusify tries [LRCLIB](https://lrclib.net) and accepts a result only if it matches the track's length.

**Your friends see the song, not "Spotify".** The member list reads *Listening to &lt;song&gt;*, the title and album art link to the track, and the progress bar is millisecond-accurate. Each can be switched off in Settings.

**A real Windows window.** The native frame is back, with Snap Layouts, the drop shadow and resizing from any edge. Its title bar takes the album's colour.

**Updating from 1.x:** the Spotify bridge gained the playback commands, so it has to be re-applied once. The installer does this for you. If you run from source, click the *Lyrics bridge out of date* message under the status dots after updating.

---

## 🆕 What's New in v1.4.0

**One-click updates.** When a new version is out, *Install update* downloads the new installer, checks it against the SHA-256 published with the release (and refuses to run it if they differ), installs it silently and restarts Statusify. Installs from `Statusify-Setup.exe` only; portable and source copies still get the download link.

**Repair lyrics from the app.** When a Spotify update knocks out the lyrics bridge, the warning under the status dots is now a button: click it and Statusify re-applies Spicetify for you, then clears the warning once Spotify is running the current bridge.

**Lyrics right after Spotify starts.** Songs that fall back to Spotify's own lyrics used to get none if played in the first ~20 seconds after Spotify opened: Spotify's internal request router isn't ready yet and the bridge gave up. It now waits for it.

**No more admin.** Hotkeys don't need Statusify to run elevated any more (since v1.3.0), so nothing about Statusify asks for administrator rights.

**Under the hood.** Tests run on every push, the Discord connection code lives in its own module, and releases are ready for code signing (see [SIGNING.md](SIGNING.md)).

---

## 🆕 What's New in v1.3.0

**One-click installer.** Download `Statusify-Setup-1.3.0.exe` from the [Releases page](https://github.com/KurepaBoss/Statusify/releases) and it handles everything: installs Statusify, installs [Spicetify](https://spicetify.app/) if you don't have it, wires the lyrics bridge into Spotify and applies it. No terminal, no copying files. It also adds a **Repair Spicetify bridge** shortcut to the Start Menu for when a Spotify update wipes Spicetify.

**Hotkeys work in games, without admin.** Hotkeys now use Windows' own `RegisterHotKey` instead of a keyboard hook. The old hook went deaf whenever a game had focus unless Statusify ran as administrator, and fired on every key-repeat, so holding `Ctrl+Alt+N` a moment too long skipped a whole run of tracks. Now one press is one action, and a combo already taken by another program is reported instead of silently doing nothing.

**Lyrics no longer leak between songs.** Skipping while lyrics were still loading could put the *previous* song's lyrics on your status. Lyrics are now matched to their exact track. A lyric-less track also no longer inherits the previous song's instrumental breaks, and the bridge no longer fetches every track twice on connect.

**Smoother lyric timing.** Lines are grouped by how much song they cover, so each Discord update lasts long enough to stay inside Discord's rate limit (5 updates per 20 s). Playback position is interpolated between the bridge's position pings, so lines advance on time instead of in jumps.

**Shows alongside games.** Your presence is now a *Listening* activity, the same slot Spotify uses, so a running game no longer hides your music.

**Spicy Lyrics 6.x support**, a faster and tidier Settings page (collapsible sections, auto-save, no more SAVE buttons), and the scroll-restore crash is fixed.

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
- 📚 **Three lyric sources** — Spicy Lyrics, then Spotify's own, then [LRCLIB](https://lrclib.net); lyrics you've heard before load instantly from a local cache.
- 👥 **Shows the song in the member list** — *Listening to &lt;song&gt;*, with the title linking to the track on Spotify.
- ⏱️ **Lyric timing offset** — a global delay slider, plus a per-track offset that is remembered for songs whose lyrics are permanently early or late.

**The app itself**
- 🌊 **A living lyric sheet** — album colours flowing behind the words, lines that glide, sharpen and fade.
- ⏯️ **Playback controls** — previous, play/pause, next, a seek bar, and click-a-lyric-line to jump there.
- 🚀 **Zero-config startup** — a setup wizard on first run and self-installing dependencies.
- 📂 **Listening history** — every play saved as it happens, with its date; search everything you've played by song, artist, or even lyric content, and export any track's lyrics as a timestamped `.lrc` or plain `.txt`.
- 🎭 **Multi-profile support** — manage multiple Discord Application IDs and switch between them instantly.
- 🪟 **Mini mode** — collapse to a compact, always-visible bar.
- 🔔 **System tray** — close-to-tray, so Statusify keeps running out of the way.
- 🎨 **Themes** — smooth dark and light modes with custom accent colours.
- ⌨️ **Global hotkeys** for toggling RPC, skipping the current track, and skipping instrumentals.
- 🚫 **Blacklist** — case-insensitive terms matched against artist and title, so anything you'd rather not broadcast never reaches Discord.
- 📊 **Listening stats** — this session, the last 7 days and all time: plays, listening time and top artists.
- ⚙️ **Start with Windows**, optionally minimised.
- 🔄 **Update checker** — reads the repo's releases in the background and shows you the changelog for anything newer.
- 🖥️ **Retina-ready UI** — native High-DPI support for crystal-clear text on any Windows scaling mode.

---

## ⬇️ Download

<div align="center">

### **[Download Statusify-Setup.exe](https://github.com/KurepaBoss/Statusify/releases/latest)**

</div>

The installer is the recommended download: it installs Statusify, sets up Spicetify and the lyrics bridge, and enables one-click updates. See [Easy Setup](#-easy-setup) below.

A portable `Statusify.exe` is on the same release page if you'd rather not install anything — one file with Python and every dependency compiled in. Put it in a folder you intend to keep (it stores your settings, history and logs beside itself), then double-click it. You'll need to set up Spicetify yourself (see [INSTALL.md](INSTALL.md)).

> **Windows will warn you the first time.** The exe is unsigned, so SmartScreen shows "Windows protected your PC" → **More info** → **Run anyway**. Each release publishes a `.sha256` file for every exe you can check against `Get-FileHash <file> -Algorithm SHA256` if you want to confirm the download is byte-for-byte what the build workflow produced. Some antivirus engines also flag it, because Statusify registers global hotkeys and opens a local socket (for the Spicetify bridge) — both visible in the source above.

Prefer running from source? That's the next section, and it's still the better option if you want to modify anything.

---

## 🚀 Easy Setup

You need the **regular desktop Spotify** from [spotify.com](https://www.spotify.com/download/windows/). The Microsoft Store version can't be modified by Spicetify, so lyrics won't work with it. Open Spotify once and log in before installing.

### 1. Run the installer
Download **`Statusify-Setup-<version>.exe`** from the [latest release](https://github.com/KurepaBoss/Statusify/releases/latest) and run it. No administrator rights needed; it installs just for you.

> Windows SmartScreen may say *"Windows protected your PC"* because the installer isn't code-signed. Click **More info → Run anyway**. Each release lists a SHA-256 checksum if you want to verify the download.

Keep **"Install Spicetify and the lyrics bridge"** ticked. A console window shows progress while it sets up Spicetify, and Spotify restarts once at the end.

### 2. Enter your Discord Application ID
The installer asks for it (you can also skip and Statusify asks on first launch):
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Name it what you want your status to show, e.g. **Spotify**.
3. Copy the **Application ID** from *General Information* and paste it in.

### 3. Play something
Launch Statusify, play a song, and your status shows the track and synced lyrics.

### If lyrics stop after a Spotify update
Spotify updates remove Spicetify's changes. Run **Start Menu → Statusify → Repair Spicetify bridge**. Statusify also warns you when the bridge inside Spotify is out of date.

<details>
<summary><b>Running from source instead</b></summary>

1. Install [Python 3.10+](https://www.python.org/downloads/) and [Spicetify](https://spicetify.app/docs/getting-started).
2. `pip install -r requirements.txt`
3. Set up the bridge: `powershell -ExecutionPolicy Bypass -File installer\setup-spicetify.ps1 -Bridge lyrics-bridge.js`
4. `python main.py` (or `run.vbs` for a windowless start). Copy `.env.example` to `.env` to set the Application ID up front.

</details>

> **Why `spicetify apply` and not just a restart?**
> Spicetify keeps two copies of every extension: the source in
> `%APPDATA%\spicetify\Extensions`, and an injected copy inside Spotify's own
> `xpui` bundle. Spotify only ever runs the injected one, and only
> `spicetify apply` updates it. The installer and the Repair shortcut both do
> this for you and verify the result.

> If Spicy Lyrics has no lyrics for a track, Statusify falls back to Spotify's own lyrics, then to LRCLIB, and says which in the log. If none of them has lyrics, it still publishes the title, artist and album art, so a lyric problem never means a blank Rich Presence.

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
| **Discord RPC** | Paused indicator, custom instrumental text, profile switching, song vs app name in the member list, Spotify link, LRCLIB fallback. |
| **History** | Toggle history on/off. Turning it off deletes what's recorded when you quit. |

**Files Statusify creates at runtime**, in its own folder (all gitignored):

| File | What it is |
| --- | --- |
| `statusify.cfg` | Your preferences and window geometry. |
| `.env` | Your Discord Application ID. |
| `history.db` | Listening history and the lyrics cache (SQLite). A legacy `history.json` is imported once and renamed `history.json.migrated`. |
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

**No lyrics on one specific track.** Confirm Spicy Lyrics itself has lyrics for it in Spotify. Statusify then tries Spotify's own lyrics and LRCLIB, and logs each fallback; if no source has them, presence still shows title, artist and album art.

**Verify the bridge is connected.** Open Spotify DevTools (`Ctrl+Shift+I`) and look for `[LyricsBridge] Connected to Python.` in the Console tab.

---

## 🛠️ Development

```bash
pip install -r requirements.txt
pip install pytest
pytest
```

The suite stubs out Discord, Spotify and the network, so it needs neither running, but it does need Windows (named pipes, the registry and live hotkey registration). `tests/test_gui_smoke.py` builds the real window and every page, so it also needs a desktop session. Tests never touch your real data: `tests/conftest.py` points `STATUSIFY_DATA_DIR` at a temp folder, and the same variable works for running a throwaway copy of the app. CI runs the suite on every push.

### Building the release executable

```powershell
.\build_exe.ps1        # -> dist\Statusify.exe
```

That creates its own `.buildenv` venv, installs PyInstaller, and runs `Statusify.spec`. Pushing a `v*` tag does the same thing on a clean Windows runner and attaches the exe plus its SHA256 to the GitHub release — see `.github/workflows/release.yml`.

Two notes for anyone touching the packaging:

- **`_APP_DIR` vs `_RES_DIR`.** User data (config, history, logs, caches) resolves from `_APP_DIR`, which points at the folder holding the exe when frozen. Bundled read-only files (`lyrics-bridge.js`, `statusify.ico`) resolve from `_RES_DIR`, which is `sys._MEIPASS`. Mixing them up means either losing every setting on exit or never being able to install the bridge.
- **`upx=False` in the spec is deliberate.** UPX packing is a strong antivirus heuristic trigger, and global hotkeys plus a listening socket is already an awkward combination for scanners.

`build_launcher.ps1` is a different, smaller thing: it compiles a 9 KB shim that launches `main.py` with your local `pythonw.exe`, for running from source without a console window.

---

## 🤝 Contributing
Found a bug or have a suggestion? Open an [Issue](https://github.com/KurepaBoss/Statusify/issues) or submit a Pull Request.

## 📄 License
Released under the [MIT License](LICENSE).

**Made with ❤️ by [KurepaBoss](https://github.com/KurepaBoss)**
