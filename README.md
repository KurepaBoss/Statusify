<div align="center">
  <img src="https://raw.githubusercontent.com/KurepaBoss/Statusify/main/statusify.ico" width="128" />
  <h1>Statusify v1.1.2</h1>
  <p><strong>The ultimate Discord Rich Presence & Spotify Lyrics bridge.</strong></p>

  ![Statusify v1.1.2](https://img.shields.io/badge/Statusify-v1.1.2-brightgreen?style=for-the-badge)
  ![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge)
  ![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)
</div>

---

## 🌟 Overview
**Statusify** is a lightweight, high-performance bridge that connects your Spotify listening experience directly to Discord and your desktop. It offers a beautiful, High-DPI aware GUI to track your session history, view synced lyrics, and manage multiple Discord profiles with a single click.

---

## ✨ Key Features
- 🚀 **Zero-Config Startup:** Automated setup wizard and self-installing dependencies.
- 🎤 **Synced Lyrics:** Direct integration with Spicetify for live, real-time lyrics on your Discord status.
- 📂 **Session History:** A searchable database of everything you've listened to—filter by song, artist, or even lyric content.
- 🎭 **Multi-Profile Support:** Manage multiple Discord Application IDs and switch between them instantly.
- 🔧 **Advanced Customization:** 
  - Toggle "Paused" status indicators.
  - Custom instrumental text.
  - Global hotkeys for skipping songs and toggling visibility.
- 🖼️ **Retina-Ready UI:** Native High-DPI support for crystal-clear text on any Windows scaling mode.

---

## 🚀 Easy Setup (Tutorial)

Setting up Statusify is simpler than ever. Follow these **3 steps** to get started:

### 1. Install Requirements
- Ensure you have [Python 3.10 or higher](https://www.python.org/downloads/) installed. 
- **Note:** Statusify will automatically install all necessary Python libraries for you when you launch it for the first time.

### 2. Prepare Spicetify (For Lyrics)
To see lyrics on your Discord status, you need [Spicetify](https://spicetify.app/):
1. **Open your Spicetify Marketplace** in the Spotify app.
2. Go to the **Extensions** tab and install the **Lyrics** extension.
3. **That's it!** Statusify will automatically move the required bridge files into your Spicetify folder when you launch it.

### 3. Launch & Connect
1. Download this repository and run:
   ```bash
   python main.py
   ```
2. **Setup Wizard:** On the first run, Statusify will ask for your **Discord Application ID**. Follow the link provided in the popup to create one in 30 seconds.
3. **Enjoy!** Your Spotify status and lyrics will now sync beautifully to Discord.

---

## ⚙️ Configuration
The app saves all your preferences in `statusify.cfg`. You can customize:
- **Hotkeys:** Default `Ctrl+Alt+S` to toggle Discord RPC.
- **Theme:** Smooth Dark and Light modes with custom accent colors.
- **History:** Toggle session recording on/off in the Settings menu.

---

## 🤝 Contributing
Found a bug or have a suggestion? Open an [Issue](https://github.com/KurepaBoss/Statusify/issues) or submit a Pull Request.

**Made with ❤️ by [KurepaBoss](https://github.com/KurepaBoss)**
