# SAMP Translate

A real-time, offline-capable translation overlay for **SA-MP 0.3.DL-R1** (San Andreas Multiplayer).

SAMP Translate reads the in-game chat directly from process memory and renders translated text on a transparent, click-through overlay — no game files are modified.

---

## Features

- **Transparent overlay** — translated chat appears right below the native SA-MP chat, fully click-through
- **Offline translation** — powered by [argostranslate](https://github.com/argosopentech/argostranslate); no internet required after packages are downloaded
- **Two-way translation** — translate incoming server chat *and* outgoing messages you type
- **Chat filters** — WhiteList (show only matching) and BlackList (hide matching) with per-filter colors
- **Keyboard shortcuts** — configurable hotkeys for text-input, toggle, clear, and filter toggle
- **Configuration presets** — save, load, rename, export, and import full settings snapshots
- **Status indicator** — always-on-top "SAMP AUTO TRANSLATE ON/OFF" badge
- **Ignore Myself** — suppress messages that start with your own player name

---

## Requirements

| Requirement | Details |
|---|---|
| OS | Windows 10 / 11 (64-bit) |
| Python | 3.11 or newer |
| SA-MP | 0.3.DL-R1 |
| GTA SA | Steam or retail (same executable SA-MP supports) |

> **Administrator rights** may be required for `keyboard` global hooks and process memory access via `pymem`.

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/SpaceKboy/SAMP-Translate.git
cd SAMP-Translate

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Usage

```bash
# Start the application
.venv\Scripts\python.exe main.py
```

1. Click **Select GTA Window** and choose your `GTA:SA` window from the list.
2. Open **Translation** to pick source and target languages, then download the offline packages.
3. Use **Shortcuts** to configure your hotkeys (default: `Y` = text chat, `Z` = toggle).
4. Play — translated chat appears automatically in the overlay.

### Default hotkeys (while GTA is focused)

| Key | Action |
|---|---|
| `Y` | Open text-input field |
| `Z` | Toggle translation on/off |
| *(unset)* | Clear chat |
| *(unset)* | Toggle all filters |

---

## Architecture

```
main.py          ControlPanel (tkinter Tk root)
                 ├── SelectorDialog    window picker
                 ├── ChatOverlay       transparent Toplevel
                 └── background threads:
                     ├── samp-chat-reader   → _raw_queue
                     └── samp-translator    → _display_queue
                         └── _drain_queue() via after()

samp_chat.py     SampChatReader + find_chat_array()
                 Reads gta_sa.exe memory via pymem
                 Locates CChatLine[100] by signature scan

window_overlay.py  SelectorDialog + ChatOverlay
                   Transparent Win32 overlay (WM_NCHITTEST → HTTRANSPARENT)

config.py        ConfigManager — JSON presets in %APPDATA%\SAMP-Translate\
```

### CChatLine struct (SA-MP 0.3.DL-R1)

Determined by memory analysis — not officially documented.

```
Offset   Size   Field
+0x00    4      reserved (always 0)
+0x04    4      message type  (1–8; 0 = empty slot)
+0x08    4      text color    (ARGB; alpha byte always 0xFF)
+0x0C    4      reserved (always 0)
+0x10    ...    internal fields
+0x30    ~204   message text  (null-terminated, Windows-1252)
```

Total entry size: **252 bytes (0xFC)**. The array holds **100 entries** as a ring buffer.

---

## Project Structure

```
SAMP-Translate/
├── main.py            Application entry point and UI dialogs
├── samp_chat.py       SA-MP memory reader
├── window_overlay.py  Transparent overlay and window selector
├── config.py          Preset persistence (JSON)
├── requirements.txt   Python dependencies
└── .vscode/
    └── settings.json  VS Code interpreter path (points to .venv)
```

---

## Contributing

Pull requests are welcome! Please:

1. Fork the repository and create a feature branch.
2. Keep all code and comments in **English**.
3. Follow the existing code style (no unnecessary abstractions, minimal comments).
4. Test with a live SA-MP server before submitting.

---

## License

This project is open source. See [LICENSE](LICENSE) for details.
