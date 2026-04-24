# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Language

All code, comments, docstrings, and UI strings must be in **English**.

## Commands

```bash
# Run the main application
.venv/Scripts/python.exe main.py

# Test the chat reader standalone (no UI, streams chat to console)
.venv/Scripts/python.exe samp_chat.py

# Install runtime dependencies
.venv/Scripts/pip install -r requirements.txt
```

## Architecture

**SA-MP 0.3.DL-R1** — reads the in-game chat via process memory and renders a transparent tkinter overlay.

### Execution flow

```
main.py  →  ControlPanel (tk.Tk root, always-on-top menu)
         →  SelectorDialog  (pick the GTA window)
         →  ChatOverlay     (Toplevel, transparent, click-through)
         →  Thread: SampChatReader._reader_loop()
               └─ find_chat_array()  — signature scan at runtime
               └─ poll() every 200 ms → _raw_queue
         →  Thread: _translation_loop()
               └─ argostranslate → _display_queue
         →  _drain_queue() via after() → ChatOverlay.add_message()
```

### Modules

**`main.py`** — `ControlPanel` (tk.Tk): main window with status indicator and action buttons. Hosts all dialog classes (ChatStyleDialog, FiltersDialog, TranslationDialog, ShortcutsDialog, PresetsDialog, …). Uses two `queue.Queue` objects for thread-safe communication between reader, translator, and UI.

**`samp_chat.py`** — `SampChatReader` + `find_chat_array()`: reads `gta_sa.exe` memory via `pymem`. The chat buffer is heap-allocated (address changes every session), so `find_chat_array` locates it at runtime using a raw-byte regex signature.

**`window_overlay.py`** — `SelectorDialog` + `ChatOverlay`: Win32 window enumeration dialog and transparent overlay. Click-through is achieved by subclassing the window procedure to return `HTTRANSPARENT` for `WM_NCHITTEST`.

**`config.py`** — `ConfigManager`: loads/saves named configuration presets as JSON in `%APPDATA%/SAMP-Translate/config.json`.

### CChatLine struct — SA-MP 0.3.DL-R1

Determined by memory analysis on 2026-04-19. Not officially documented.

```
CChatLine  (252 bytes = 0xFC per entry, 100 entries total)
  +0x00  DWORD  always 0 (reserved)
  +0x04  DWORD  message type (1–8; 0 = empty slot)
  +0x08  DWORD  text color ARGB (alpha byte always 0xFF)
  +0x0C  DWORD  always 0
  +0x10  ...    internal fields (timestamps, pointers)
  +0x30  char[] message text (null-terminated, Windows-1252, up to ~204 bytes)
```

Signature used to locate the array:
`[\x01-\x0a]\x00\x00\x00...[\x00-\xff]{3}\xff\x00\x00\x00\x00`
(regex over raw bytes starting at `struct+0x04`), requiring 8+ consecutive entries spaced 0xFC bytes apart.

### Key design decisions

- **`SelectorDialog`** uses `Toplevel` + `grab_set` + `wait_window` when called with `master=` — avoids a second `tk.Tk()`.
- **`ChatOverlay`** also uses `Toplevel(master)` when integrated into `ControlPanel`, and `stop()` calls `destroy()` (not `quit()`, which would kill the shared mainloop).
- The Tcl/Tk path fix (`_fix_tcl_paths`) runs at the top of `window_overlay.py` and `main.py` because the venv does not inherit TCL/TK paths from the base Python installation.
- VS Code uses `.vscode/settings.json` to point to the `.venv` interpreter (required for Pylance to resolve `win32api`, `win32gui`, etc. via `pywin32-stubs`).
- Translation uses a **producer-consumer** pattern across two queues (`_raw_queue`, `_display_queue`) so memory reading, translation, and UI updates never block each other.
- argostranslate translators are cached in `_argos_cache[(src, tgt)]` and pre-warmed in a background thread to avoid first-message latency.
- Indirect translation (src→en→tgt) is attempted automatically when no direct package exists.

#### Keyboard hooks

- **`_setup_chat_hook()`** uses `suppress=True` (intercepts the key before it reaches SA-MP) with an `_inject_count` counter: when GTA is not in the foreground the key is re-injected via `win32api.keybd_event` so it still reaches other apps. Always guard against empty `chat_key` / `toggle_key` before calling `keyboard.on_press_key()` — an empty string raises an exception that silently breaks `config.apply()`.
- **`_start_input_hook()`** (overlay chat input) tracks Shift/Ctrl/Alt state manually via `KEY_DOWN`/`KEY_UP` events instead of using `GetAsyncKeyState`. With `suppress=True` active, Windows never updates its async key-state table for consumed keys, so `GetAsyncKeyState(VK_SHIFT)` always returns 0.
- **`_resolve_char()`** uses `ToUnicodeEx` with the keyboard layout handle of the GTA window thread (`GetKeyboardLayout(GetWindowThreadProcessId(hwnd))`) to correctly translate scan codes under any Windows input language. Do **not** call `ToUnicodeEx` for modifier key events — it corrupts the internal dead-key state and breaks subsequent character resolution.

#### Translation startup

- `_apply_startup_config()` calls `_on_translation_toggle()` explicitly **after** `config.apply()` returns. The `trace_add("write", …)` callback on `_translation["enabled"]` fires during `apply()` before `source`/`target` are set, so the prewarm inside the callback receives `_PLACEHOLDER` values and does nothing. The explicit post-apply call runs with all settings populated and actually warms the cache.

#### Presets

- New presets are always created with `_default_preset()` (blank factory defaults), never a snapshot of the current UI state. `PresetsDialog._new_preset()` calls `config.create()` then `config.apply()` — not `save_current()`.
- Default preset ships with no shortcuts configured (`chat_key: ""`, `toggle_key: ""`) so the app starts without capturing any keys by default.

### Reverse engineering tools

The `find_chat_offsets*.py` files (if present) are standalone RE tools used to discover the SA-MP memory layout. They are not part of the application and can be ignored or deleted.
