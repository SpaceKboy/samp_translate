"""
SAMP Translate — main entry point.

Execution flow:
    1. Open ControlPanel (compact external menu, always-on-top)
    2. User clicks "Select GTA Window" → SelectorDialog
    3. Start ChatOverlay on top of GTA (transparent Toplevel)
    4. Background thread reads SA-MP chat → queue.Queue
    5. Tkinter drain loop processes the queue → ChatOverlay.add_message()
"""

import os
import sys
import queue
import threading
import time
import dataclasses


def _fix_tcl_paths() -> None:
    """
    Ensure TCL_LIBRARY / TK_LIBRARY point to the base Python installation.

    A venv does not copy the Tcl/Tk runtime, so tkinter would fail to import
    without this path fix. Must run before any tkinter import.
    """
    base = getattr(sys, "base_prefix", sys.prefix)
    tcl = os.path.join(base, "tcl", "tcl8.6")
    tk_ = os.path.join(base, "tcl", "tk8.6")
    if os.path.isdir(tcl):
        os.environ.setdefault("TCL_LIBRARY", tcl)
        os.environ.setdefault("TK_LIBRARY", tk_)


_fix_tcl_paths()

import tkinter as tk
import win32gui
import win32api
import win32con

from window_overlay import SelectorDialog, ChatOverlay, get_window_rect
from samp_chat import SampChatReader, ChatMessage
from config import ConfigManager

# ── Color palette ─────────────────────────────────────────────────────────────

BG       = "#1a1a2e"   # background (dark navy)
BG_PANEL = "#16213e"   # panel / card background
FG       = "#e0e0e0"   # primary text
FG_DIM   = "#888888"   # secondary / disabled text
ACCENT   = "#00FF00"   # highlight / success green
FONT_UI  = ("Segoe UI", 9)

POLL_INTERVAL_MS = 200  # queue drain interval in milliseconds

# ── Supported languages ───────────────────────────────────────────────────────
# Maps human-readable language names to ISO 639-1 codes used by argostranslate.
# Argostranslate supports only a subset of these — presence in this dict does
# NOT guarantee an offline package is available for every pair.

ALL_LANGUAGES: dict[str, str] = {
    "Afrikaans":           "af",
    "Albanian":            "sq",
    "Arabic":              "ar",
    "Azerbaijani":         "az",
    "Bengali":             "bn",
    "Bosnian":             "bs",
    "Bulgarian":           "bg",
    "Catalan":             "ca",
    "Chinese Simplified":  "zh",
    "Chinese Traditional": "zt",
    "Croatian":            "hr",
    "Czech":               "cs",
    "Danish":              "da",
    "Dutch":               "nl",
    "English":             "en",
    "Esperanto":           "eo",
    "Estonian":            "et",
    "Finnish":             "fi",
    "French":              "fr",
    "Galician":            "gl",
    "German":              "de",
    "Greek":               "el",
    "Hebrew":              "he",
    "Hindi":               "hi",
    "Hungarian":           "hu",
    "Indonesian":          "id",
    "Irish":               "ga",
    "Italian":             "it",
    "Japanese":            "ja",
    "Korean":              "ko",
    "Latvian":             "lv",
    "Lithuanian":          "lt",
    "Macedonian":          "mk",
    "Malay":               "ms",
    "Norwegian":           "nb",
    "Persian":             "fa",
    "Polish":              "pl",
    "Portuguese":          "pt",
    "Romanian":            "ro",
    "Russian":             "ru",
    "Serbian":             "sr",
    "Slovak":              "sk",
    "Slovenian":           "sl",
    "Spanish":             "es",
    "Swedish":             "sv",
    "Tagalog":             "tl",
    "Thai":                "th",
    "Turkish":             "tr",
    "Ukrainian":           "uk",
    "Urdu":                "ur",
    "Vietnamese":          "vi",
}

# Reverse map: ISO code → display name (e.g. "pt" → "Portuguese")
CODE_TO_LANG: dict[str, str] = {v: k for k, v in ALL_LANGUAGES.items()}

# Sentinel value shown in language dropdowns when no language is selected
LANG_PLACEHOLDER = "─ Select ─"


# ── Chat position dialog ──────────────────────────────────────────────────────

class ChatPositionDialog:
    """Dialog for adjusting the X/Y position of the translated chat overlay."""

    STEP = 5  # pixels moved per arrow-button click

    def __init__(self, master: tk.Misc, overlay: ChatOverlay, hwnd: int):
        self._overlay = overlay
        self._hwnd    = hwnd

        self.root = tk.Toplevel(master)
        self.root.title("Chat Position")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        # Initialise spinboxes with the current overlay position
        rect = get_window_rect(hwnd)
        h    = rect[3] if rect else 600
        x, y = overlay.get_position(h)

        self._x = tk.IntVar(value=x)
        self._y = tk.IntVar(value=y)

        self._build_ui()
        self.root.grab_set()

    def _build_ui(self) -> None:
        # Coordinate spinboxes
        coords = tk.Frame(self.root, bg=BG, padx=14, pady=10)
        coords.pack(fill="x")

        tk.Label(coords, text="X:", bg=BG, fg=FG, font=FONT_UI).grid(row=0, column=0, sticky="w")
        tk.Spinbox(
            coords, from_=0, to=9999, textvariable=self._x, width=6,
            command=self._apply, bg=BG_PANEL, fg=FG, relief="flat",
            buttonbackground=BG_PANEL,
        ).grid(row=0, column=1, padx=(4, 20))

        tk.Label(coords, text="Y:", bg=BG, fg=FG, font=FONT_UI).grid(row=0, column=2, sticky="w")
        tk.Spinbox(
            coords, from_=0, to=9999, textvariable=self._y, width=6,
            command=self._apply, bg=BG_PANEL, fg=FG, relief="flat",
            buttonbackground=BG_PANEL,
        ).grid(row=0, column=3, padx=(4, 0))

        # D-pad for fine-tuning
        dpad = tk.Frame(self.root, bg=BG, pady=6)
        dpad.pack()

        _btn = dict(
            bg=BG_PANEL, fg=FG, relief="flat", width=3,
            font=("Segoe UI", 13), cursor="hand2",
            activebackground=ACCENT, activeforeground="#000",
        )
        tk.Button(dpad, text="↑", command=self._move_up,    **_btn).grid(row=0, column=1, padx=3, pady=3)
        tk.Button(dpad, text="←", command=self._move_left,  **_btn).grid(row=1, column=0, padx=3, pady=3)
        tk.Label( dpad, text="·", bg=BG, fg=FG_DIM, font=("Segoe UI", 13), width=3, anchor="center").grid(row=1, column=1)
        tk.Button(dpad, text="→", command=self._move_right, **_btn).grid(row=1, column=2, padx=3, pady=3)
        tk.Button(dpad, text="↓", command=self._move_down,  **_btn).grid(row=2, column=1, padx=3, pady=3)

        tk.Button(
            self.root, text="Close", command=self.root.destroy,
            bg=BG_PANEL, fg=FG, relief="flat", padx=14, pady=4, cursor="hand2",
        ).pack(pady=(2, 10))

    def _apply(self) -> None:
        self._overlay.set_position(self._x.get(), self._y.get())

    def _move_up(self)    -> None: self._y.set(max(0, self._y.get() - self.STEP)); self._apply()
    def _move_down(self)  -> None: self._y.set(self._y.get() + self.STEP);         self._apply()
    def _move_left(self)  -> None: self._x.set(max(0, self._x.get() - self.STEP)); self._apply()
    def _move_right(self) -> None: self._x.set(self._x.get() + self.STEP);         self._apply()


# ── Input chat position dialog ────────────────────────────────────────────────

class ChatInputPositionDialog:
    """Dialog for adjusting the position of the text-input field in the overlay."""

    STEP = 5

    def __init__(self, master: tk.Misc, overlay: ChatOverlay, hwnd: int):
        self._overlay = overlay
        self._hwnd    = hwnd

        self.root = tk.Toplevel(master)
        self.root.title("Input Chat Position")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        rect = get_window_rect(hwnd)
        h    = rect[3] if rect else 600
        x, y = overlay.get_input_position(h)

        self._x = tk.IntVar(value=x)
        self._y = tk.IntVar(value=y)

        self._build_ui()
        self.root.grab_set()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Show a live preview of the pill so the user sees where it will land
        self._overlay.show_input_preview()

    def _build_ui(self) -> None:
        coords = tk.Frame(self.root, bg=BG, padx=14, pady=10)
        coords.pack(fill="x")

        tk.Label(coords, text="X:", bg=BG, fg=FG, font=FONT_UI).grid(row=0, column=0, sticky="w")
        tk.Spinbox(
            coords, from_=0, to=9999, textvariable=self._x, width=6,
            command=self._apply, bg=BG_PANEL, fg=FG, relief="flat",
            buttonbackground=BG_PANEL,
        ).grid(row=0, column=1, padx=(4, 20))

        tk.Label(coords, text="Y:", bg=BG, fg=FG, font=FONT_UI).grid(row=0, column=2, sticky="w")
        tk.Spinbox(
            coords, from_=0, to=9999, textvariable=self._y, width=6,
            command=self._apply, bg=BG_PANEL, fg=FG, relief="flat",
            buttonbackground=BG_PANEL,
        ).grid(row=0, column=3, padx=(4, 0))

        dpad = tk.Frame(self.root, bg=BG, pady=6)
        dpad.pack()

        _btn = dict(
            bg=BG_PANEL, fg=FG, relief="flat", width=3,
            font=("Segoe UI", 13), cursor="hand2",
            activebackground=ACCENT, activeforeground="#000",
        )
        tk.Button(dpad, text="↑", command=self._move_up,    **_btn).grid(row=0, column=1, padx=3, pady=3)
        tk.Button(dpad, text="←", command=self._move_left,  **_btn).grid(row=1, column=0, padx=3, pady=3)
        tk.Label( dpad, text="·", bg=BG, fg=FG_DIM, font=("Segoe UI", 13), width=3, anchor="center").grid(row=1, column=1)
        tk.Button(dpad, text="→", command=self._move_right, **_btn).grid(row=1, column=2, padx=3, pady=3)
        tk.Button(dpad, text="↓", command=self._move_down,  **_btn).grid(row=2, column=1, padx=3, pady=3)

        tk.Button(
            self.root, text="Close", command=self._on_close,
            bg=BG_PANEL, fg=FG, relief="flat", padx=14, pady=4, cursor="hand2",
        ).pack(pady=(2, 10))

    def _apply(self) -> None:
        x, y = self._x.get(), self._y.get()
        self._overlay.set_input_position(x, y)
        self._overlay.move_input_preview(x, y)

    def _on_close(self) -> None:
        self._overlay._close_input()
        self.root.destroy()

    def _move_up(self)    -> None: self._y.set(max(0, self._y.get() - self.STEP)); self._apply()
    def _move_down(self)  -> None: self._y.set(self._y.get() + self.STEP);         self._apply()
    def _move_left(self)  -> None: self._x.set(max(0, self._x.get() - self.STEP)); self._apply()
    def _move_right(self) -> None: self._x.set(self._x.get() + self.STEP);         self._apply()


# ── Chat style dialog ─────────────────────────────────────────────────────────

FONTS_AVAILABLE = [
    "Arial", "Consolas", "Courier New", "Impact", "Segoe UI",
    "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana",
]


class ChatStyleDialog:
    """Dialog for customising the font, size, color, and line count of chat text."""

    def __init__(self, master: tk.Misc, overlay: ChatOverlay):
        self._overlay = overlay

        self.root = tk.Toplevel(master)
        self.root.title("Edit Chat")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        family, size, color = overlay.get_style()
        self._font_var  = tk.StringVar(value=family)
        self._size_var  = tk.IntVar(value=size)
        self._color_var = tk.StringVar(value=color)
        self._lines_var = tk.IntVar(value=overlay.get_max_messages())

        self._build_ui()
        self.root.grab_set()

    def _build_ui(self) -> None:
        pad = dict(padx=14, pady=6)

        # Font family
        row_font = tk.Frame(self.root, bg=BG, **pad)
        row_font.pack(fill="x")
        tk.Label(row_font, text="Font:", bg=BG, fg=FG, font=FONT_UI, width=8, anchor="w").pack(side="left")
        font_menu = tk.OptionMenu(row_font, self._font_var, *FONTS_AVAILABLE)
        font_menu.config(bg=BG_PANEL, fg=FG, relief="flat", activebackground=ACCENT,
                         activeforeground="#000", highlightthickness=0)
        font_menu["menu"].config(bg=BG_PANEL, fg=FG)
        font_menu.pack(side="left", fill="x", expand=True)

        # Font size
        row_size = tk.Frame(self.root, bg=BG, **pad)
        row_size.pack(fill="x")
        tk.Label(row_size, text="Size:", bg=BG, fg=FG, font=FONT_UI, width=8, anchor="w").pack(side="left")
        tk.Spinbox(
            row_size, from_=6, to=48, textvariable=self._size_var, width=5,
            bg=BG_PANEL, fg=FG, relief="flat", buttonbackground=BG_PANEL,
        ).pack(side="left")

        # Max visible lines
        row_lines = tk.Frame(self.root, bg=BG, **pad)
        row_lines.pack(fill="x")
        tk.Label(row_lines, text="Lines:", bg=BG, fg=FG, font=FONT_UI, width=8, anchor="w").pack(side="left")
        tk.Spinbox(
            row_lines, from_=1, to=30, textvariable=self._lines_var, width=5,
            bg=BG_PANEL, fg=FG, relief="flat", buttonbackground=BG_PANEL,
        ).pack(side="left")

        # Text color
        row_color = tk.Frame(self.root, bg=BG, **pad)
        row_color.pack(fill="x")
        tk.Label(row_color, text="Color:", bg=BG, fg=FG, font=FONT_UI, width=8, anchor="w").pack(side="left")
        self._color_swatch = tk.Label(
            row_color, text="  ██  ", bg=BG, fg=self._color_var.get(),
            font=("Segoe UI", 11), cursor="hand2",
        )
        self._color_swatch.pack(side="left")
        tk.Button(
            row_color, text="Choose",
            command=self._pick_color,
            bg=BG_PANEL, fg=FG, relief="flat", padx=8, pady=2,
            activebackground=ACCENT, activeforeground="#000", cursor="hand2",
        ).pack(side="left", padx=(6, 0))

        # Action buttons
        btn_row = tk.Frame(self.root, bg=BG, padx=14, pady=8)
        btn_row.pack(fill="x")
        tk.Button(
            btn_row, text="Apply", command=self._apply,
            bg=ACCENT, fg="#000", relief="flat", padx=12, pady=4, cursor="hand2",
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            btn_row, text="Close", command=self.root.destroy,
            bg=BG_PANEL, fg=FG, relief="flat", padx=12, pady=4, cursor="hand2",
        ).pack(side="left")

    def _pick_color(self) -> None:
        from tkinter import colorchooser
        result = colorchooser.askcolor(
            color=self._color_var.get(), title="Chat Color", parent=self.root,
        )
        if result and result[1]:
            self._color_var.set(result[1])
            self._color_swatch.config(fg=result[1])

    def _apply(self) -> None:
        self._overlay.set_style(self._font_var.get(), self._size_var.get(), self._color_var.get())
        self._overlay.set_max_messages(self._lines_var.get())


# ── Filter dialogs ────────────────────────────────────────────────────────────

FILTER_COLORS = {"whitelist": ACCENT, "blacklist": "#ff6666"}
FILTER_LABELS = {"whitelist": "WhiteList", "blacklist": "BlackList"}


class AddFilterDialog:
    """
    Small dialog for creating a single chat filter (whitelist or blacklist).

    Whitelist — only messages containing the keyword are shown.
    Blacklist — messages containing the keyword are hidden.
    Whitelist filters also support a custom font color for matched messages.
    """

    def __init__(self, master: tk.Misc, filter_type: str, on_confirm):
        self._filter_type = filter_type  # "whitelist" or "blacklist"
        self._on_confirm  = on_confirm

        self.root = tk.Toplevel(master)
        label = FILTER_LABELS[filter_type]
        self.root.title(f"Add {label}")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        self._build_ui()
        self.root.grab_set()

    def _build_ui(self) -> None:
        pad   = dict(padx=14, pady=6)
        color = FILTER_COLORS[self._filter_type]
        label = FILTER_LABELS[self._filter_type]

        tk.Label(self.root, text=f"Add {label}", bg=BG, fg=color,
                 font=("Segoe UI", 11, "bold"), padx=14, pady=10).pack(anchor="w")

        row_name = tk.Frame(self.root, bg=BG, **pad)
        row_name.pack(fill="x")
        tk.Label(row_name, text="Name:", bg=BG, fg=FG, font=FONT_UI, width=12, anchor="w").pack(side="left")
        self._name_var = tk.StringVar()
        tk.Entry(row_name, textvariable=self._name_var, width=22,
                 bg=BG_PANEL, fg=FG, relief="flat", insertbackground=FG).pack(side="left")

        row_kw = tk.Frame(self.root, bg=BG, **pad)
        row_kw.pack(fill="x")
        tk.Label(row_kw, text="Keyword:", bg=BG, fg=FG, font=FONT_UI, width=12, anchor="w").pack(side="left")
        self._kw_var = tk.StringVar()
        tk.Entry(row_kw, textvariable=self._kw_var, width=22,
                 bg=BG_PANEL, fg=FG, relief="flat", insertbackground=FG).pack(side="left")

        # Optional font color — only available for whitelist filters
        self._font_color: str = ""
        if self._filter_type == "whitelist":
            row_color = tk.Frame(self.root, bg=BG, **pad)
            row_color.pack(fill="x")
            tk.Label(row_color, text="Font Color:", bg=BG, fg=FG,
                     font=FONT_UI, width=12, anchor="w").pack(side="left")
            self._color_swatch = tk.Label(
                row_color, text="  ██  ", bg=BG, fg="#FFFFFF",
                font=("Segoe UI", 11), cursor="hand2",
            )
            self._color_swatch.pack(side="left")
            tk.Button(
                row_color, text="Choose",
                command=self._pick_color,
                bg=BG_PANEL, fg=FG, relief="flat", padx=8, pady=2,
                activebackground=ACCENT, activeforeground="#000", cursor="hand2",
            ).pack(side="left", padx=(6, 0))
            tk.Label(row_color, text="(default if empty)", bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))

        btn_row = tk.Frame(self.root, bg=BG, padx=14, pady=8)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Add", command=self._confirm,
                  bg=color, fg="#000", relief="flat", padx=12, pady=4,
                  cursor="hand2").pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Cancel", command=self.root.destroy,
                  bg=BG_PANEL, fg=FG, relief="flat", padx=12, pady=4,
                  cursor="hand2").pack(side="left")

    def _pick_color(self) -> None:
        from tkinter import colorchooser
        initial = self._font_color if self._font_color else "#FFFFFF"
        result  = colorchooser.askcolor(color=initial, title="Font Color", parent=self.root)
        if result and result[1]:
            self._font_color = result[1]
            self._color_swatch.config(fg=result[1])

    def _confirm(self) -> None:
        name    = self._name_var.get().strip()
        keyword = self._kw_var.get().strip()
        if name and keyword:
            self._on_confirm(name, keyword, self._filter_type, self._font_color)
            self.root.destroy()


class FiltersDialog:
    """
    Dialog for managing chat filters and the 'Ignore Myself' option.

    WhiteList: only messages containing the keyword are shown.
    BlackList: messages containing the keyword are hidden.
    'Ignore Myself': hides messages that start with the player's own name.
    """

    def __init__(self, master: tk.Misc, filters: list[dict], overlay: ChatOverlay | None,
                 ignore_self: dict, no_translate_commands: tk.BooleanVar, on_filter_change):
        self._filters               = filters
        self._overlay               = overlay
        self._ignore_self           = ignore_self
        self._no_translate_commands = no_translate_commands
        self._on_filter_change      = on_filter_change

        self.root = tk.Toplevel(master)
        self.root.title("Filters")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        self._build_ui()

    def _build_ui(self) -> None:
        tk.Label(
            self.root, text="Chat Filters", bg=BG, fg=ACCENT,
            font=("Segoe UI", 11, "bold"), padx=14, pady=10,
        ).pack(anchor="w")

        tk.Label(
            self.root,
            text="WhiteList: show only messages containing the keyword.  "
                 "BlackList: hide messages containing the keyword.",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 8), padx=14,
        ).pack(anchor="w")

        tk.Frame(self.root, bg=BG_PANEL, height=1).pack(fill="x", padx=14, pady=6)

        # ── Ignore Myself ──
        ignore_frame = tk.Frame(self.root, bg=BG_PANEL, padx=10, pady=8)
        ignore_frame.pack(fill="x", padx=14, pady=(0, 6))

        tk.Checkbutton(
            ignore_frame, text="Ignore Myself",
            variable=self._ignore_self["var"],
            command=self._on_ignore_toggle,
            bg=BG_PANEL, fg=FG, selectcolor=BG,
            activebackground=BG_PANEL, activeforeground=ACCENT,
            font=FONT_UI, cursor="hand2",
        ).pack(side="left")

        self._lbl_ignore_name = tk.Label(
            ignore_frame,
            text=self._ignore_self["name"] or "no name defined",
            bg=BG_PANEL,
            fg=ACCENT if self._ignore_self["name"] else FG_DIM,
            font=("Segoe UI", 8),
        )
        self._lbl_ignore_name.pack(side="right")

        # ── Don't translate commands ──
        cmd_frame = tk.Frame(self.root, bg=BG_PANEL, padx=10, pady=8)
        cmd_frame.pack(fill="x", padx=14, pady=(0, 6))

        tk.Checkbutton(
            cmd_frame, text="Don't translate commands  (/command)",
            variable=self._no_translate_commands,
            bg=BG_PANEL, fg=FG, selectcolor=BG,
            activebackground=BG_PANEL, activeforeground=ACCENT,
            font=FONT_UI, cursor="hand2",
        ).pack(side="left")

        tk.Frame(self.root, bg=BG_PANEL, height=1).pack(fill="x", padx=14, pady=6)

        self._list_frame = tk.Frame(self.root, bg=BG, padx=14, pady=4)
        self._list_frame.pack(fill="x")
        self._render_filters()

        tk.Frame(self.root, bg=BG_PANEL, height=1).pack(fill="x", padx=14, pady=6)

        bottom = tk.Frame(self.root, bg=BG, padx=14, pady=4)
        bottom.pack(fill="x")

        tk.Button(
            bottom, text="+ WhiteList",
            command=lambda: self._open_add_filter("whitelist"),
            bg=BG_PANEL, fg=ACCENT, relief="flat", padx=10, pady=4, cursor="hand2",
            activebackground=ACCENT, activeforeground="#000",
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            bottom, text="+ BlackList",
            command=lambda: self._open_add_filter("blacklist"),
            bg=BG_PANEL, fg="#ff6666", relief="flat", padx=10, pady=4, cursor="hand2",
            activebackground="#ff6666", activeforeground="#000",
        ).pack(side="left")

        tk.Button(
            bottom, text="Close", command=self.root.destroy,
            bg=BG_PANEL, fg=FG, relief="flat", padx=14, pady=4, cursor="hand2",
        ).pack(side="right")

        self.root.update_idletasks()

    def _on_ignore_toggle(self) -> None:
        if self._ignore_self["var"].get():
            PlayerNameDialog(
                master=self.root,
                current_name=self._ignore_self["name"],
                on_confirm=self._set_ignore_name,
                on_cancel=self._cancel_ignore,
            )
        else:
            self._on_filter_change()

    def _set_ignore_name(self, name: str) -> None:
        self._ignore_self["name"] = name
        self._lbl_ignore_name.config(text=name, fg=ACCENT)
        self._on_filter_change()

    def _cancel_ignore(self) -> None:
        self._ignore_self["var"].set(False)

    def _render_filters(self) -> None:
        for w in self._list_frame.winfo_children():
            w.destroy()

        if not self._filters:
            tk.Label(self._list_frame, text="No filters created yet.",
                     bg=BG, fg=FG_DIM, font=FONT_UI).pack(pady=6)
            return

        for f in self._filters:
            ftype  = f.get("type", "whitelist")
            fcolor = FILTER_COLORS[ftype]
            flabel = FILTER_LABELS[ftype]

            row = tk.Frame(self._list_frame, bg=BG_PANEL, padx=10, pady=8)
            row.pack(fill="x", pady=(0, 6))

            f["var"].trace_add("write", lambda *_: self._on_toggle())

            tk.Label(
                row, text=f"[{flabel}]",
                bg=BG_PANEL, fg=fcolor, font=("Segoe UI", 7, "bold"),
            ).pack(side="left", padx=(0, 6))

            tk.Checkbutton(
                row, text=f["name"],
                variable=f["var"],
                bg=BG_PANEL, fg=FG, selectcolor=BG,
                activebackground=BG_PANEL, activeforeground=fcolor,
                font=FONT_UI, cursor="hand2",
            ).pack(side="left")

            # Clickable color swatch — only for whitelist filters
            if ftype == "whitelist":
                swatch_color = f.get("color") or "#FFFFFF"
                swatch = tk.Label(
                    row, text="■", bg=BG_PANEL, fg=swatch_color,
                    font=("Segoe UI", 14), cursor="hand2",
                )
                swatch.pack(side="right", padx=(4, 0))
                swatch.bind("<Button-1>", lambda e, fil=f, sw=swatch: self._change_filter_color(fil, sw))

            tk.Label(
                row, text=f'"{f["keyword"]}"',
                bg=BG_PANEL, fg=FG_DIM, font=("Segoe UI", 8),
            ).pack(side="right")

    def _on_toggle(self) -> None:
        if self._overlay:
            self._overlay.clear_messages()

    def _open_add_filter(self, filter_type: str) -> None:
        AddFilterDialog(master=self.root, filter_type=filter_type, on_confirm=self._add_filter)

    def _add_filter(self, name: str, keyword: str, filter_type: str, color: str = "") -> None:
        self._filters.append({
            "name":    name,
            "keyword": keyword,
            "type":    filter_type,
            "color":   color,
            "var":     tk.BooleanVar(value=False),
        })
        self._render_filters()
        if self._overlay:
            self._overlay.clear_messages()

    def _change_filter_color(self, f: dict, swatch: tk.Label) -> None:
        from tkinter import colorchooser
        initial = f.get("color") or "#FFFFFF"
        result  = colorchooser.askcolor(color=initial, title="Font Color", parent=self.root)
        if result and result[1]:
            f["color"] = result[1]
            swatch.config(fg=result[1])


# ── Player name dialog ────────────────────────────────────────────────────────

class PlayerNameDialog:
    """Small dialog for entering the player's in-game name (used by Ignore Myself)."""

    def __init__(self, master: tk.Misc, current_name: str, on_confirm, on_cancel):
        self._on_confirm = on_confirm
        self._on_cancel  = on_cancel

        self.root = tk.Toplevel(master)
        self.root.title("Add your in-game name")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)

        self._name_var = tk.StringVar(value=current_name)
        self._build_ui()
        self.root.grab_set()

    def _build_ui(self) -> None:
        tk.Label(self.root, text="Add your in-game name", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold"), padx=14, pady=10).pack(anchor="w")

        tk.Label(self.root,
                 text="Messages starting with this name will be ignored.",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8), padx=14).pack(anchor="w")

        row = tk.Frame(self.root, bg=BG, padx=14, pady=10)
        row.pack(fill="x")
        tk.Label(row, text="Name:", bg=BG, fg=FG, font=FONT_UI).pack(side="left", padx=(0, 6))
        tk.Entry(row, textvariable=self._name_var, width=26,
                 bg=BG_PANEL, fg=FG, relief="flat", insertbackground=FG).pack(side="left")

        btn_row = tk.Frame(self.root, bg=BG, padx=14, pady=6)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Confirm", command=self._confirm,
                  bg=ACCENT, fg="#000", relief="flat", padx=12, pady=4,
                  cursor="hand2").pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Cancel", command=self._cancel,
                  bg=BG_PANEL, fg=FG, relief="flat", padx=12, pady=4,
                  cursor="hand2").pack(side="left")

    def _confirm(self) -> None:
        name = self._name_var.get().strip()
        if name:
            self._on_confirm(name)
            self.root.destroy()

    def _cancel(self) -> None:
        self._on_cancel()
        self.root.destroy()


# ── Status overlay dialog ─────────────────────────────────────────────────────

class StatusOverlayDialog:
    """Adjust position, font size, and visibility of the 'SAMP AUTO TRANSLATE' indicator."""

    STEP = 5

    def __init__(self, master: tk.Misc, overlay: ChatOverlay, hwnd: int):
        self._overlay = overlay
        self._hwnd    = hwnd

        self.root = tk.Toplevel(master)
        self.root.title("Status Overlay")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        rect = get_window_rect(hwnd)
        w    = rect[2] if rect else 800
        x, y = overlay.get_status_position(w)

        self._visible_var = tk.BooleanVar(value=overlay.get_status_visible())
        self._x    = tk.IntVar(value=x)
        self._y    = tk.IntVar(value=y)
        self._size = tk.IntVar(value=overlay.get_status_font_size())

        self._build_ui()
        self.root.grab_set()

    def _build_ui(self) -> None:
        tk.Label(self.root, text="Status Overlay", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold"), padx=14, pady=10).pack(anchor="w")

        # Visibility toggle
        vis_frame = tk.Frame(self.root, bg=BG_PANEL, padx=12, pady=8)
        vis_frame.pack(fill="x", padx=14, pady=(0, 8))
        tk.Checkbutton(
            vis_frame, text="Show status overlay",
            variable=self._visible_var, command=self._apply_visible,
            bg=BG_PANEL, fg=FG, selectcolor=BG,
            activebackground=BG_PANEL, activeforeground=ACCENT,
            font=FONT_UI, cursor="hand2",
        ).pack(anchor="w")

        # Font size
        size_frame = tk.Frame(self.root, bg=BG_PANEL, padx=12, pady=8)
        size_frame.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(size_frame, text="Size:", bg=BG_PANEL, fg=FG,
                 font=FONT_UI, width=10, anchor="w").pack(side="left")
        tk.Spinbox(
            size_frame, from_=6, to=28, textvariable=self._size, width=5,
            command=self._apply_size,
            bg=BG, fg=FG, relief="flat", buttonbackground=BG_PANEL,
        ).pack(side="left")

        # Position controls
        pos_frame = tk.Frame(self.root, bg=BG_PANEL, padx=12, pady=8)
        pos_frame.pack(fill="x", padx=14, pady=(0, 8))

        tk.Label(pos_frame, text="Position", bg=BG_PANEL, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Frame(pos_frame, bg="#2a2a4a", height=1).pack(fill="x", pady=(4, 8))

        coords = tk.Frame(pos_frame, bg=BG_PANEL)
        coords.pack(fill="x")
        tk.Label(coords, text="X:", bg=BG_PANEL, fg=FG, font=FONT_UI).grid(row=0, column=0, sticky="w")
        tk.Spinbox(coords, from_=0, to=9999, textvariable=self._x, width=6,
                   command=self._apply_position, bg=BG, fg=FG, relief="flat",
                   buttonbackground=BG_PANEL).grid(row=0, column=1, padx=(4, 20))
        tk.Label(coords, text="Y:", bg=BG_PANEL, fg=FG, font=FONT_UI).grid(row=0, column=2, sticky="w")
        tk.Spinbox(coords, from_=0, to=9999, textvariable=self._y, width=6,
                   command=self._apply_position, bg=BG, fg=FG, relief="flat",
                   buttonbackground=BG_PANEL).grid(row=0, column=3, padx=(4, 0))

        dpad = tk.Frame(pos_frame, bg=BG_PANEL, pady=6)
        dpad.pack()
        _btn = dict(bg=BG, fg=FG, relief="flat", width=3,
                    font=("Segoe UI", 13), cursor="hand2",
                    activebackground=ACCENT, activeforeground="#000")
        tk.Button(dpad, text="↑", command=self._move_up,    **_btn).grid(row=0, column=1, padx=3, pady=3)
        tk.Button(dpad, text="←", command=self._move_left,  **_btn).grid(row=1, column=0, padx=3, pady=3)
        tk.Label( dpad, text="·", bg=BG_PANEL, fg=FG_DIM, font=("Segoe UI", 13), width=3).grid(row=1, column=1)
        tk.Button(dpad, text="→", command=self._move_right, **_btn).grid(row=1, column=2, padx=3, pady=3)
        tk.Button(dpad, text="↓", command=self._move_down,  **_btn).grid(row=2, column=1, padx=3, pady=3)

        tk.Button(self.root, text="Close", command=self.root.destroy,
                  bg=BG_PANEL, fg=FG, relief="flat", padx=14, pady=4, cursor="hand2",
                  ).pack(pady=(2, 10))

    def _apply_visible(self)   -> None: self._overlay.set_status_visible(self._visible_var.get())
    def _apply_size(self)      -> None: self._overlay.set_status_font_size(self._size.get())
    def _apply_position(self)  -> None: self._overlay.set_status_position(self._x.get(), self._y.get())

    def _move_up(self)    -> None: self._y.set(max(0, self._y.get() - self.STEP)); self._apply_position()
    def _move_down(self)  -> None: self._y.set(self._y.get() + self.STEP);         self._apply_position()
    def _move_left(self)  -> None: self._x.set(max(0, self._x.get() - self.STEP)); self._apply_position()
    def _move_right(self) -> None: self._x.set(self._x.get() + self.STEP);         self._apply_position()


# ── Chat menu dialog ──────────────────────────────────────────────────────────

class ChatMenuDialog:
    """Launcher menu for all customisation sub-dialogs (position, style, status overlay)."""

    def __init__(self, master: tk.Misc, overlay: ChatOverlay, hwnd: int):
        self._overlay = overlay
        self._hwnd    = hwnd

        self.root = tk.Toplevel(master)
        self.root.title("Customize")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        self._build_ui()

    def _build_ui(self) -> None:
        tk.Label(self.root, text="Customize", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold"), padx=14, pady=10).pack(anchor="w")

        btn_frame = tk.Frame(self.root, bg=BG, padx=14, pady=4)
        btn_frame.pack(fill="x")

        _btn = dict(relief="flat", padx=8, pady=6, cursor="hand2",
                    activebackground=ACCENT, activeforeground="#000")

        tk.Button(btn_frame, text="Chat Position",
                  command=self._open_position,       bg=BG_PANEL, fg=FG, **_btn).pack(fill="x")
        tk.Button(btn_frame, text="Input Chat Position",
                  command=self._open_input_position, bg=BG_PANEL, fg=FG, **_btn).pack(fill="x", pady=(6, 0))
        tk.Button(btn_frame, text="Edit Chat",
                  command=self._open_style,          bg=BG_PANEL, fg=FG, **_btn).pack(fill="x", pady=(6, 0))
        tk.Button(btn_frame, text="Status Overlay",
                  command=self._open_status_overlay, bg=BG_PANEL, fg=FG, **_btn).pack(fill="x", pady=(6, 12))

    def _open_position(self)        -> None: ChatPositionDialog(master=self.root, overlay=self._overlay, hwnd=self._hwnd)
    def _open_input_position(self)  -> None: ChatInputPositionDialog(master=self.root, overlay=self._overlay, hwnd=self._hwnd)
    def _open_style(self)           -> None: ChatStyleDialog(master=self.root, overlay=self._overlay)
    def _open_status_overlay(self)  -> None: StatusOverlayDialog(master=self.root, overlay=self._overlay, hwnd=self._hwnd)


# ── Translation dialog ────────────────────────────────────────────────────────

class TranslationDialog:
    """
    Dialog for configuring offline translation.

    Two independent translation channels:
      Server Chat — translates incoming SA-MP messages before they appear in the overlay.
      User Chat   — translates text you type before sending it to the server.

    Both channels require offline argostranslate packages to be installed.
    """

    def __init__(self, master: tk.Misc, translation: dict,
                 check_fn, download_fn, get_installed_fn):
        self._translation      = translation
        self._check_fn         = check_fn
        self._download_fn      = download_fn
        self._get_installed_fn = get_installed_fn

        self.root = tk.Toplevel(master)
        self.root.title("Translation")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        self._build_ui()
        self._refresh_status()
        self._refresh_user_status()

    def _build_ui(self) -> None:
        tk.Label(self.root, text="Translation", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold"), padx=14, pady=10).pack(anchor="w")

        # Server chat translation section
        self._build_section(
            title="Server Chat",
            enabled_var=self._translation["enabled"],
            source_var=self._translation["source"],
            target_var=self._translation["target"],
            on_change=self._refresh_status,
        )

        # User (outgoing) chat translation section
        self._build_section(
            title="User Chat",
            enabled_var=self._translation["user_enabled"],
            source_var=self._translation["user_source"],
            target_var=self._translation["user_target"],
            on_change=self._refresh_user_status,
        )

        # Offline package management section
        pkg_frame = tk.Frame(self.root, bg=BG_PANEL, padx=12, pady=10)
        pkg_frame.pack(fill="x", padx=14, pady=(0, 10))

        tk.Label(pkg_frame, text="Offline Packages", bg=BG_PANEL, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Frame(pkg_frame, bg="#2a2a4a", height=1).pack(fill="x", pady=(4, 8))

        # Dropdown listing already-installed language pairs
        self._installed_pkg_frame = tk.Frame(pkg_frame, bg=BG_PANEL)
        self._installed_pkg_frame.pack(fill="x", pady=(0, 8))
        self._refresh_installed_dropdown()

        tk.Frame(pkg_frame, bg="#2a2a4a", height=1).pack(fill="x", pady=(0, 8))

        # Server chat download section
        tk.Label(pkg_frame, text="Server Chat:", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self._lbl_pkg_status = tk.Label(
            pkg_frame, text="Checking...", bg=BG_PANEL, fg=FG_DIM, font=FONT_UI,
        )
        self._lbl_pkg_status.pack(anchor="w")
        self._btn_download = tk.Button(
            pkg_frame, text="Download packages",
            command=self._download_packages,
            bg=BG_PANEL, fg=ACCENT, relief="flat", padx=10, pady=4,
            activebackground=ACCENT, activeforeground="#000", cursor="hand2",
        )
        self._btn_download.pack(anchor="w", pady=(4, 10))

        # User chat download section
        tk.Label(pkg_frame, text="User Chat:", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self._lbl_user_pkg_status = tk.Label(
            pkg_frame, text="Checking...", bg=BG_PANEL, fg=FG_DIM, font=FONT_UI,
        )
        self._lbl_user_pkg_status.pack(anchor="w")
        self._btn_user_download = tk.Button(
            pkg_frame, text="Download packages",
            command=self._download_user_packages,
            bg=BG_PANEL, fg=ACCENT, relief="flat", padx=10, pady=4,
            activebackground=ACCENT, activeforeground="#000", cursor="hand2",
        )
        self._btn_user_download.pack(anchor="w", pady=(4, 0))

        tk.Button(self.root, text="Close", command=self.root.destroy,
                  bg=BG_PANEL, fg=FG, relief="flat", padx=14, pady=4,
                  cursor="hand2").pack(pady=(6, 10))

    def _refresh_installed_dropdown(self) -> None:
        """Rebuild the dropdown that lists currently installed language packages."""
        for w in self._installed_pkg_frame.winfo_children():
            w.destroy()

        tk.Label(
            self._installed_pkg_frame,
            text="Installed packages:", bg=BG_PANEL, fg=FG_DIM,
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w")

        pairs = self._get_installed_fn()
        if not pairs:
            tk.Label(
                self._installed_pkg_frame,
                text="No packages installed",
                bg=BG_PANEL, fg=FG_DIM, font=FONT_UI,
            ).pack(anchor="w", pady=(2, 0))
            return

        pair_names = [
            f"{CODE_TO_LANG.get(src, src)} → {CODE_TO_LANG.get(tgt, tgt)}"
            for src, tgt in pairs
        ]
        self._installed_pkg_var = tk.StringVar(value=pair_names[0])
        om = tk.OptionMenu(self._installed_pkg_frame, self._installed_pkg_var, *pair_names)
        om.config(bg=BG, fg=ACCENT, relief="flat", activebackground=ACCENT,
                  activeforeground="#000", highlightthickness=0, width=26)
        om["menu"].config(bg=BG, fg=ACCENT)
        om.pack(anchor="w", pady=(2, 0))

    def _make_lang_dropdown(self, parent, var, on_change) -> tk.OptionMenu:
        """
        Build a language OptionMenu that places installed languages at the top
        and colours them green so they stand out from uninstalled ones.
        """
        installed_pairs  = self._get_installed_fn()
        installed_codes: set[str] = set()
        for src, tgt in installed_pairs:
            installed_codes.add(src)
            installed_codes.add(tgt)

        installed_names = sorted(n for n, c in ALL_LANGUAGES.items() if c in installed_codes)
        other_names     = sorted(n for n, c in ALL_LANGUAGES.items() if c not in installed_codes)
        all_options     = [LANG_PLACEHOLDER] + installed_names + other_names

        om = tk.OptionMenu(parent, var, *all_options, command=lambda _: on_change())
        om.config(bg=BG, fg=FG, relief="flat", activebackground=ACCENT,
                  activeforeground="#000", highlightthickness=0, width=16)
        menu = om["menu"]
        menu.config(bg=BG, fg=FG)

        # Highlight installed languages in green (+1 to skip the placeholder entry)
        for i, name in enumerate(installed_names):
            menu.entryconfig(i + 1, foreground="#00FF00")

        return om

    def _build_section(self, title: str, enabled_var, source_var, target_var,
                       on_change) -> None:
        section = tk.Frame(self.root, bg=BG_PANEL, padx=12, pady=10)
        section.pack(fill="x", padx=14, pady=(0, 8))

        header = tk.Frame(section, bg=BG_PANEL)
        header.pack(fill="x")
        tk.Label(header, text=title, bg=BG_PANEL, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Checkbutton(
            header, text="Enable",
            variable=enabled_var,
            bg=BG_PANEL, fg=FG, selectcolor=BG,
            activebackground=BG_PANEL, activeforeground=ACCENT,
            font=FONT_UI, cursor="hand2",
        ).pack(side="right")

        tk.Frame(section, bg="#2a2a4a", height=1).pack(fill="x", pady=(6, 8))

        row_in = tk.Frame(section, bg=BG_PANEL)
        row_in.pack(fill="x", pady=(0, 6))
        tk.Label(row_in, text="Input:", bg=BG_PANEL, fg=FG_DIM,
                 font=FONT_UI, width=8, anchor="w").pack(side="left")
        self._make_lang_dropdown(row_in, source_var, on_change).pack(side="left")

        row_out = tk.Frame(section, bg=BG_PANEL)
        row_out.pack(fill="x")
        tk.Label(row_out, text="Output:", bg=BG_PANEL, fg=FG_DIM,
                 font=FONT_UI, width=8, anchor="w").pack(side="left")
        self._make_lang_dropdown(row_out, target_var, on_change).pack(side="left")

    def _current_codes(self) -> tuple[str | None, str | None]:
        return (ALL_LANGUAGES.get(self._translation["source"].get()),
                ALL_LANGUAGES.get(self._translation["target"].get()))

    def _current_user_codes(self) -> tuple[str | None, str | None]:
        return (ALL_LANGUAGES.get(self._translation["user_source"].get()),
                ALL_LANGUAGES.get(self._translation["user_target"].get()))

    def _refresh_status(self) -> None:
        src, tgt = self._current_codes()
        if not src or not tgt:
            self._lbl_pkg_status.config(text="Select languages above", fg=FG_DIM)
            self._btn_download.config(state="disabled", fg=FG_DIM)
            return
        if self._check_fn(src, tgt):
            self._lbl_pkg_status.config(text="✓ Installed — offline translation ready", fg=ACCENT)
            self._btn_download.config(state="disabled", fg=FG_DIM)
        else:
            self._lbl_pkg_status.config(text="✗ Packages not installed", fg="#ff6666")
            self._btn_download.config(state="normal", fg=ACCENT)

    def _refresh_user_status(self) -> None:
        src, tgt = self._current_user_codes()
        if not src or not tgt:
            self._lbl_user_pkg_status.config(text="Select languages above", fg=FG_DIM)
            self._btn_user_download.config(state="disabled", fg=FG_DIM)
            return
        if self._check_fn(src, tgt):
            self._lbl_user_pkg_status.config(text="✓ Installed — offline translation ready", fg=ACCENT)
            self._btn_user_download.config(state="disabled", fg=FG_DIM)
        else:
            self._lbl_user_pkg_status.config(text="✗ Packages not installed", fg="#ff6666")
            self._btn_user_download.config(state="normal", fg=ACCENT)

    def _download_packages(self) -> None:
        src, tgt = self._current_codes()
        if not src or not tgt:
            return
        self._btn_download.config(state="disabled", text="Downloading...", fg=FG_DIM)
        self._lbl_pkg_status.config(text="Downloading packages (internet required)...", fg=FG_DIM)

        def on_done(ok: bool):
            if ok:
                self._lbl_pkg_status.config(text="✓ Installed successfully!", fg=ACCENT)
                self._btn_download.config(text="Download packages", state="disabled", fg=FG_DIM)
                self._refresh_installed_dropdown()
            else:
                self._lbl_pkg_status.config(text="✗ Download failed.", fg="#ff6666")
                self._btn_download.config(text="Download packages", state="normal", fg=ACCENT)

        self._download_fn(src, tgt, on_done)

    def _download_user_packages(self) -> None:
        src, tgt = self._current_user_codes()
        if not src or not tgt:
            return
        self._btn_user_download.config(state="disabled", text="Downloading...", fg=FG_DIM)
        self._lbl_user_pkg_status.config(text="Downloading packages (internet required)...", fg=FG_DIM)

        def on_done(ok: bool):
            if ok:
                self._lbl_user_pkg_status.config(text="✓ Installed successfully!", fg=ACCENT)
                self._btn_user_download.config(text="Download packages", state="disabled", fg=FG_DIM)
                self._refresh_installed_dropdown()
            else:
                self._lbl_user_pkg_status.config(text="✗ Download failed.", fg="#ff6666")
                self._btn_user_download.config(text="Download packages", state="normal", fg=ACCENT)

        self._download_fn(src, tgt, on_done)


# ── Shortcuts dialog ──────────────────────────────────────────────────────────

class ShortcutsDialog:
    """
    Dialog for assigning global hotkeys to SAMP Translate functions.

    Click 'Change' to enter listening mode, then press the desired key.
    Press ESC during listening to clear (remove) the shortcut.
    Modifier keys (Shift, Ctrl, Alt, etc.) are ignored as sole hotkeys.
    """

    _IGNORE_KEYS = frozenset({
        "shift", "left shift", "right shift",
        "ctrl",  "left ctrl",  "right ctrl",
        "alt",   "left alt",   "right alt",
        "caps lock", "tab", "win", "left win", "right win",
        "unknown",
    })

    def __init__(self, master: tk.Misc, shortcuts: dict, on_change):
        self._shortcuts        = shortcuts
        self._on_change        = on_change   # called with key_name after each change
        self._hook             = None
        self._listening_target = None   # dict: {key_name, label, btn}

        self.root = tk.Toplevel(master)
        self.root.title("Shortcuts")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self.root.grab_set()

    def _build_ui(self) -> None:
        tk.Label(self.root, text="Shortcuts", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold"), padx=14, pady=10).pack(anchor="w")
        tk.Label(
            self.root,
            text="Click 'Change' and press the new key.  ESC removes the shortcut.",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 8), padx=14,
        ).pack(anchor="w", pady=(0, 6))

        self._build_shortcut_row(
            key_name="chat_key",
            title="Text Chat",
            description="Opens the text-input field while GTA is focused.",
        )
        self._build_shortcut_row(
            key_name="toggle_key",
            title="Enable / Disable Translation",
            description="Toggles SAMP Auto Translate on and off while GTA is focused.",
        )
        self._build_shortcut_row(
            key_name="clear_key",
            title="Clear Chat",
            description="Clears all messages from the chat overlay.",
        )
        self._build_shortcut_row(
            key_name="filters_key",
            title="Enable / Disable Filters",
            description="Toggles all chat filters on and off at once.",
        )

        tk.Button(
            self.root, text="Close", command=self._on_close,
            bg=BG_PANEL, fg=FG, relief="flat", padx=14, pady=4, cursor="hand2",
        ).pack(pady=(4, 10))

    def _key_display(self, key_name: str) -> tuple[str, str]:
        """Return (display_text, color) for the key label."""
        val = self._shortcuts.get(key_name, "")
        return (val.upper(), ACCENT) if val else ("─", FG_DIM)

    def _restore_label(self, target: dict) -> None:
        key_name = target["key_name"]
        text, color = self._key_display(key_name)
        target["label"].config(text=text, fg=color)
        target["btn"].config(
            text="Change", fg=FG, activebackground=ACCENT,
            command=lambda kn=key_name, kl=target["label"], b=target["btn"]:
                self._start_listening(kn, kl, b),
        )

    def _build_shortcut_row(self, key_name: str, title: str, description: str) -> None:
        section = tk.Frame(self.root, bg=BG_PANEL, padx=12, pady=10)
        section.pack(fill="x", padx=14, pady=(0, 8))

        tk.Label(section, text=title, bg=BG_PANEL, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(section, text=description, bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(2, 6))
        tk.Frame(section, bg="#2a2a4a", height=1).pack(fill="x", pady=(0, 8))

        row = tk.Frame(section, bg=BG_PANEL)
        row.pack(fill="x")

        tk.Label(row, text="Key:", bg=BG_PANEL, fg=FG_DIM,
                 font=FONT_UI, width=8, anchor="w").pack(side="left")

        text, color = self._key_display(key_name)
        key_label = tk.Label(
            row, text=text, bg=BG, fg=color,
            font=("Consolas", 12, "bold"), width=6, relief="flat", padx=6, pady=3,
        )
        key_label.pack(side="left", padx=(0, 10))

        btn = tk.Button(row, text="Change", bg=BG_PANEL, fg=FG,
                        relief="flat", padx=10, pady=4,
                        activebackground=ACCENT, activeforeground="#000", cursor="hand2")
        btn.config(command=lambda kn=key_name, kl=key_label, b=btn: self._start_listening(kn, kl, b))
        btn.pack(side="left")

    def _start_listening(self, key_name: str, label: tk.Label, btn: tk.Button) -> None:
        if self._listening_target is not None:
            return  # already listening for another shortcut
        self._listening_target = {"key_name": key_name, "label": label, "btn": btn}
        label.config(text="...", fg=FG_DIM)
        btn.config(text="Cancel", fg="#ff6666", activebackground="#ff6666",
                   command=self._cancel_listening)
        try:
            import keyboard as kb

            def _handler(event):
                if event.event_type != kb.KEY_DOWN:
                    return
                name = event.name.lower()
                if name in ("escape", "esc"):
                    self.root.after(0, self._clear_key)
                    return
                if name in self._IGNORE_KEYS:
                    return
                self.root.after(0, lambda n=name: self._apply_key(n))

            self._hook = kb.hook(_handler, suppress=False)
        except ImportError:
            self._cancel_listening()

    def _apply_key(self, name: str) -> None:
        if self._listening_target is None:
            return
        target = self._listening_target
        self._stop_hook()
        self._listening_target = None
        self._shortcuts[target["key_name"]] = name
        self._restore_label(target)
        self._on_change(target["key_name"])

    def _clear_key(self) -> None:
        """Remove the shortcut assignment (triggered when ESC is pressed during listening)."""
        if self._listening_target is None:
            return
        target = self._listening_target
        self._stop_hook()
        self._listening_target = None
        self._shortcuts[target["key_name"]] = ""
        self._restore_label(target)
        self._on_change(target["key_name"])

    def _cancel_listening(self) -> None:
        if self._listening_target is None:
            return
        target = self._listening_target
        self._stop_hook()
        self._listening_target = None
        self._restore_label(target)

    def _stop_hook(self) -> None:
        if self._hook is not None:
            try:
                import keyboard as kb
                kb.unhook(self._hook)
            except Exception:
                pass
            self._hook = None

    def _on_close(self) -> None:
        self._stop_hook()
        self.root.destroy()


# ── Presets dialog ────────────────────────────────────────────────────────────

class PresetsDialog:
    """
    Dialog for managing named configuration presets.

    Presets are complete snapshots of all settings (shortcuts, translation,
    filters, chat style, overlay positions). They can be saved, loaded, renamed,
    deleted, and exported/imported as standalone JSON files.
    """

    def __init__(self, master: tk.Misc, config: ConfigManager, panel, overlay):
        self._config  = config
        self._panel   = panel
        self._overlay = overlay

        self.root = tk.Toplevel(master)
        self.root.title("Configuration Presets")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        self._build_ui()
        self.root.grab_set()

    def _build_ui(self) -> None:
        tk.Label(self.root, text="Presets", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold"), padx=14, pady=10).pack(anchor="w")

        tk.Label(
            self.root,
            text="Select a preset from the list and use the buttons below to manage it.",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 8), padx=14,
        ).pack(anchor="w", pady=(0, 6))

        # Preset list
        list_frame = tk.Frame(self.root, bg=BG_PANEL, padx=12, pady=10)
        list_frame.pack(fill="x", padx=14, pady=(0, 8))

        tk.Label(list_frame, text="Saved presets:", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")

        self._listbox = tk.Listbox(
            list_frame, bg=BG, fg=FG,
            selectbackground=ACCENT, selectforeground="#000",
            font=FONT_UI, relief="flat", height=7, activestyle="none",
            highlightthickness=0,
        )
        self._listbox.pack(fill="x", pady=(4, 0))
        self._refresh_list()

        # Action buttons
        btn_frame = tk.Frame(self.root, bg=BG, padx=14, pady=4)
        btn_frame.pack(fill="x")

        _btn = dict(relief="flat", padx=8, pady=5, cursor="hand2",
                    activebackground=ACCENT, activeforeground="#000")

        row1 = tk.Frame(btn_frame, bg=BG)
        row1.pack(fill="x", pady=(0, 4))
        tk.Button(row1, text="Save current", command=self._save_current,
                  bg=ACCENT, fg="#000", **_btn).pack(side="left", padx=(0, 4))
        tk.Button(row1, text="Load", command=self._load_selected,
                  bg=BG_PANEL, fg=FG, **_btn).pack(side="left", padx=(0, 4))
        tk.Button(row1, text="New preset", command=self._new_preset,
                  bg=BG_PANEL, fg=FG, **_btn).pack(side="left")

        row2 = tk.Frame(btn_frame, bg=BG)
        row2.pack(fill="x", pady=(0, 4))
        tk.Button(row2, text="Rename", command=self._rename_preset,
                  bg=BG_PANEL, fg=FG, **_btn).pack(side="left", padx=(0, 4))
        tk.Button(row2, text="Delete", command=self._delete_preset,
                  bg=BG_PANEL, fg="#ff6666",
                  activebackground="#ff6666", activeforeground="#000",
                  **{k: v for k, v in _btn.items() if k not in ("activebackground", "activeforeground")},
                  ).pack(side="left")

        row3 = tk.Frame(btn_frame, bg=BG)
        row3.pack(fill="x", pady=(0, 8))
        tk.Button(row3, text="Export", command=self._export_preset,
                  bg=BG_PANEL, fg=FG, **_btn).pack(side="left", padx=(0, 4))
        tk.Button(row3, text="Import", command=self._import_preset,
                  bg=BG_PANEL, fg=FG, **_btn).pack(side="left")

        tk.Button(self.root, text="Close", command=self.root.destroy,
                  bg=BG_PANEL, fg=FG, relief="flat", padx=14, pady=4, cursor="hand2",
                  ).pack(pady=(0, 10))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _refresh_list(self) -> None:
        self._listbox.delete(0, tk.END)
        active = self._config.active_preset
        for i, name in enumerate(self._config.preset_names):
            self._listbox.insert(tk.END, f"● {name}" if name == active else f"  {name}")
            if name == active:
                self._listbox.selection_set(i)

    def _selected_name(self) -> str | None:
        sel = self._listbox.curselection()
        if not sel:
            return None
        return self._listbox.get(sel[0])[2:]  # strip the "● " or "  " prefix

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        """Show a small inline dialog that asks for a text value and returns it."""
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.configure(bg=BG)
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)
        dialog.grab_set()

        var    = tk.StringVar(value=initial)
        result: list[str | None] = [None]

        tk.Label(dialog, text=title, bg=BG, fg=ACCENT,
                 font=("Segoe UI", 10, "bold"), padx=14, pady=10).pack(anchor="w")

        row = tk.Frame(dialog, bg=BG, padx=14, pady=6)
        row.pack(fill="x")
        entry = tk.Entry(row, textvariable=var, width=28,
                         bg=BG_PANEL, fg=FG, relief="flat", insertbackground=FG)
        entry.pack(fill="x")
        entry.select_range(0, tk.END)
        entry.focus_set()

        def _confirm():
            v = var.get().strip()
            if v:
                result[0] = v
                dialog.destroy()

        entry.bind("<Return>", lambda _: _confirm())
        entry.bind("<Escape>", lambda _: dialog.destroy())

        btn_row = tk.Frame(dialog, bg=BG, padx=14, pady=8)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Confirm", command=_confirm,
                  bg=ACCENT, fg="#000", relief="flat", padx=10, pady=4,
                  cursor="hand2").pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Cancel", command=dialog.destroy,
                  bg=BG_PANEL, fg=FG, relief="flat", padx=10, pady=4,
                  cursor="hand2").pack(side="left")

        dialog.wait_window()
        return result[0]

    def _warn(self, msg: str) -> None:
        from tkinter import messagebox
        messagebox.showwarning("Warning", msg, parent=self.root)

    def _ask_yes_no(self, msg: str) -> bool:
        from tkinter import messagebox
        return messagebox.askyesno("Confirm", msg, parent=self.root)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _save_current(self) -> None:
        name = self._selected_name()
        if not name:
            self._warn("Select a preset from the list to save.")
            return
        self._config.save_current(name, self._panel, self._overlay)
        self._config.save()
        self._refresh_list()

    def _load_selected(self) -> None:
        name = self._selected_name()
        if not name:
            self._warn("Select a preset from the list to load.")
            return
        self._config.apply(name, self._panel, self._overlay)
        self._config.save()
        self._refresh_list()

    def _new_preset(self) -> None:
        name = self._ask_name("New preset name")
        if not name:
            return
        if name in self._config.preset_names:
            self._warn(f'A preset named "{name}" already exists.')
            return
        self._config.create(name)
        self._config.apply(name, self._panel, self._overlay)
        self._config.save()
        self._refresh_list()

    def _rename_preset(self) -> None:
        old = self._selected_name()
        if not old:
            self._warn("Select a preset to rename.")
            return
        new = self._ask_name("Rename preset", initial=old)
        if not new or new == old:
            return
        if new in self._config.preset_names:
            self._warn(f'A preset named "{new}" already exists.')
            return
        self._config.rename(old, new)
        self._config.save()
        self._refresh_list()

    def _delete_preset(self) -> None:
        name = self._selected_name()
        if not name:
            self._warn("Select a preset to delete.")
            return
        if len(self._config.preset_names) <= 1:
            self._warn("At least one preset must exist.")
            return
        if not self._ask_yes_no(f'Delete preset "{name}"?'):
            return
        self._config.delete(name)
        self._config.save()
        self._refresh_list()

    def _export_preset(self) -> None:
        name = self._selected_name()
        if not name:
            self._warn("Select a preset to export.")
            return
        from tkinter import filedialog, messagebox
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export Preset",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialfile=f"{name}.json",
        )
        if not path:
            return
        try:
            self._config.export_preset(name, path)
            messagebox.showinfo("Exported", f'Preset "{name}" exported successfully!',
                                parent=self.root)
        except Exception as e:
            messagebox.showerror("Error", f"Export failed: {e}", parent=self.root)

    def _import_preset(self) -> None:
        from tkinter import filedialog, messagebox
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Import Preset",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            name = self._config.import_preset(path)
            self._config.save()
            self._refresh_list()
            messagebox.showinfo("Imported", f'Preset "{name}" imported successfully!',
                                parent=self.root)
        except Exception as e:
            messagebox.showerror("Error", f"Import failed: {e}", parent=self.root)


# ── Control panel (external menu) ─────────────────────────────────────────────

class ControlPanel:
    """
    Main application controller — the compact always-on-top control window.

    Manages the SA-MP chat reader thread, the translation worker thread, the
    transparent chat overlay, keyboard hooks, and the configuration preset system.

    Threading model:
        _reader_thread      — calls SampChatReader.poll() → _raw_queue
        _translator_thread  — consumes _raw_queue, translates → _display_queue
        Tkinter mainloop    — _drain_queue() via after() → ChatOverlay
    """

    def __init__(self):
        self._hwnd: int = 0
        self._raw_queue:     queue.Queue = queue.Queue()  # reader → translator
        self._display_queue: queue.Queue = queue.Queue()  # translator → UI
        self._reader = SampChatReader()
        self._reader_thread:     threading.Thread | None = None
        self._translator_thread: threading.Thread | None = None
        self._overlay: ChatOverlay | None = None

        self.root = tk.Tk()
        self.root.title("SAMP Translate")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Filters — each entry: name, keyword, type ("whitelist"|"blacklist"), var (BooleanVar)
        self._filters: list[dict] = []
        self._ignore_self: dict = {"var": tk.BooleanVar(value=False), "name": ""}
        self._no_translate_commands = tk.BooleanVar(value=False)

        self._translation: dict = {
            "enabled":      tk.BooleanVar(value=False),
            "source":       tk.StringVar(value=LANG_PLACEHOLDER),
            "target":       tk.StringVar(value=LANG_PLACEHOLDER),
            "user_enabled": tk.BooleanVar(value=False),
            "user_source":  tk.StringVar(value=LANG_PLACEHOLDER),
            "user_target":  tk.StringVar(value=LANG_PLACEHOLDER),
        }

        # Cache of (src_code, tgt_code) → argostranslate.Translator to avoid
        # re-loading the translation model on every message
        self._argos_cache: dict = {}

        self._translation["enabled"].trace_add("write", self._on_translation_toggle)
        self._translation["user_enabled"].trace_add("write", self._on_translation_toggle)

        # Global keyboard hook handles
        self._kb_hook          = None
        self._toggle_kb_hook   = None
        self._clear_kb_hook    = None
        self._filters_kb_hook  = None

        self._translate_active: bool = True   # controlled by the toggle hotkey
        self._filters_enabled:  bool = True   # controlled by the filters hotkey

        self._shortcuts: dict = {
            "chat_key":    "y",
            "toggle_key":  "z",
            "clear_key":   "",
            "filters_key": "",
        }

        self._config = ConfigManager()
        self._full_ui_built = False
        self._build_ui()
        self._apply_startup_config()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._build_launcher_ui()

    def _build_launcher_ui(self) -> None:
        """Initial screen shown before a GTA window has been selected."""
        for w in self.root.winfo_children():
            w.destroy()

        header = tk.Frame(self.root, bg=BG, padx=16, pady=16)
        header.pack(fill="x")
        tk.Label(header, text="SAMP Translate", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 14, "bold")).pack()
        tk.Label(header, text="Translation overlay for SA-MP", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(pady=(2, 0))

        tk.Frame(self.root, bg=BG_PANEL, height=1).pack(fill="x", padx=10)

        body = tk.Frame(self.root, bg=BG, padx=16, pady=18)
        body.pack(fill="x")
        tk.Label(
            body,
            text="Select the GTA SA window\nto get started.",
            bg=BG, fg=FG_DIM, font=FONT_UI, justify="center",
        ).pack(pady=(0, 14))
        tk.Button(
            body, text="Select GTA Window",
            command=self._select_window,
            bg=ACCENT, fg="#000", relief="flat", padx=8, pady=10,
            font=("Segoe UI", 9, "bold"),
            activebackground="#00cc00", activeforeground="#000",
            cursor="hand2",
        ).pack(fill="x")

    def _build_full_ui(self, window_title: str) -> None:
        """Full control panel shown after a GTA window has been selected."""
        for w in self.root.winfo_children():
            w.destroy()

        # Header
        header = tk.Frame(self.root, bg=BG, padx=12, pady=8)
        header.pack(fill="x")
        tk.Label(
            header, text="SAMP Translate",
            bg=BG, fg=ACCENT, font=("Segoe UI", 12, "bold"),
        ).pack(side="left")

        # Connection status panel
        status = tk.Frame(self.root, bg=BG_PANEL, padx=12, pady=8)
        status.pack(fill="x", padx=10, pady=(0, 6))

        self._dot = tk.Label(status, text="●", bg=BG_PANEL, fg="orange", font=("Segoe UI", 10))
        self._dot.grid(row=0, column=0, padx=(0, 5))

        self._lbl_status = tk.Label(status, text="Connecting...", bg=BG_PANEL, fg=FG, font=FONT_UI)
        self._lbl_status.grid(row=0, column=1, sticky="w")

        self._lbl_window = tk.Label(
            status, text=window_title, bg=BG_PANEL, fg=FG_DIM, font=FONT_UI,
        )
        self._lbl_window.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Action buttons
        btn_frame = tk.Frame(self.root, bg=BG, padx=10, pady=8)
        btn_frame.pack(fill="x")

        _btn = dict(relief="flat", padx=8, pady=4, cursor="hand2",
                    activebackground=ACCENT, activeforeground="#000")

        for label, cmd in [
            ("Customize",    self._open_chat_menu),
            ("Translation",  self._open_translation_dialog),
            ("Filters",      self._open_filters_dialog),
            ("Shortcuts",    self._open_shortcuts_dialog),
            ("Presets",      self._open_presets_dialog),
        ]:
            tk.Button(btn_frame, text=label, command=cmd,
                      bg=BG_PANEL, fg=FG_DIM, **_btn).pack(fill="x", pady=(4, 0))

        tk.Button(
            btn_frame, text="Clear Chat", command=self._clear_chat,
            bg=BG_PANEL, fg=FG_DIM, relief="flat", padx=8, pady=4, cursor="hand2",
            activebackground="#ff4444", activeforeground="#fff",
        ).pack(fill="x", pady=(4, 0))

        self._full_ui_built = True

    # ── Window selection ──────────────────────────────────────────────────────

    def _select_window(self) -> None:
        dialog = SelectorDialog(master=self.root)
        hwnd   = dialog.selected_hwnd
        if hwnd is None:
            return

        self._hwnd = hwnd
        title = win32gui.GetWindowText(hwnd) or f"HWND={hwnd}"

        if not self._full_ui_built:
            self._build_full_ui(title)
        else:
            self._lbl_window.config(text=title)

        self._start_overlay(hwnd)
        self._start_reader()

    # ── Overlay ───────────────────────────────────────────────────────────────

    def _start_overlay(self, hwnd: int) -> None:
        if self._overlay is not None:
            self._overlay.stop()
        self._overlay = ChatOverlay(hwnd, master=self.root)
        self._overlay.set_translate_active(self._translate_active)
        self._config.apply_overlay(self._config.active_preset, self._overlay)
        self._setup_chat_hook()
        self._setup_toggle_hook()
        self._setup_clear_hook()
        self._setup_filters_hook()

    # ── Reader thread ─────────────────────────────────────────────────────────

    def _start_reader(self) -> None:
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="samp-chat-reader",
        )
        self._translator_thread = threading.Thread(
            target=self._translation_loop, daemon=True, name="samp-translator",
        )
        self._reader_thread.start()
        self._translator_thread.start()
        self.root.after(POLL_INTERVAL_MS, self._drain_queue)

    def _reader_loop(self) -> None:
        """
        Continuously attach to gta_sa.exe and feed raw ChatMessages into _raw_queue.

        Retries attach() every 2 seconds until successful, then polls until the
        process exits.
        """
        while True:
            try:
                self._reader.attach()
                self.root.after(0, self._set_connected)
                break
            except Exception:
                time.sleep(2.0)

        while True:
            try:
                if not self._reader.is_attached():
                    self.root.after(0, self._set_disconnected)
                    break
                for msg in self._reader.poll():
                    self._raw_queue.put(msg)
                time.sleep(POLL_INTERVAL_MS / 1000)
            except Exception:
                time.sleep(1.0)

    def _translation_loop(self) -> None:
        """
        Dedicated translation worker — runs in its own thread so heavy translation
        work never blocks the reader or the UI.
        """
        while True:
            try:
                msg        = self._raw_queue.get()
                translated = self._try_translate(msg)
                self._display_queue.put((msg, translated))  # (original, translated)
            except Exception:
                pass

    def _try_translate(self, msg):
        """
        Attempt to translate a message using the cached argostranslate translator.

        Falls back to the original message on any error (missing package, etc.).
        Supports direct translation (src→tgt) and indirect routing (src→en→tgt).
        """
        if not self._translation["enabled"].get():
            return msg
        if self._no_translate_commands.get() and msg.text.lstrip().startswith("/"):
            return msg
        src = ALL_LANGUAGES.get(self._translation["source"].get())
        tgt = ALL_LANGUAGES.get(self._translation["target"].get())
        if not src or not tgt or src == tgt:
            return msg
        try:
            # Direct translation (uses cached translator object)
            t = self._argos_cache.get((src, tgt))
            if t:
                result = t.translate(msg.text)
                return dataclasses.replace(msg, text=result or msg.text)

            # Indirect route via English: src→en→tgt
            if src != "en" and tgt != "en":
                t1 = self._argos_cache.get((src, "en"))
                t2 = self._argos_cache.get(("en", tgt))
                if t1 and t2:
                    result = t2.translate(t1.translate(msg.text))
                    return dataclasses.replace(msg, text=result or msg.text)
        except Exception:
            pass
        return msg

    def _prewarm_translator(self, src: str, tgt: str) -> None:
        """
        Load the argostranslate Translator object in a background thread.

        Called when translation is enabled or when a language pair changes,
        so the first message is not delayed by model loading.
        """
        def _worker():
            try:
                import argostranslate.translate as at
                lang_map  = {l.code: l for l in at.get_installed_languages()}
                from_lang = lang_map.get(src)
                to_lang   = lang_map.get(tgt)
                if not from_lang or not to_lang:
                    return

                # Try direct route first
                t = from_lang.get_translation(to_lang)
                if t:
                    self._argos_cache[(src, tgt)] = t
                    return

                # Fall back to indirect route via English
                if src != "en" and tgt != "en":
                    en_lang = lang_map.get("en")
                    if en_lang:
                        t1 = from_lang.get_translation(en_lang)
                        t2 = en_lang.get_translation(to_lang)
                        if t1:
                            self._argos_cache[(src, "en")] = t1
                        if t2:
                            self._argos_cache[("en", tgt)] = t2
            except Exception:
                pass

        if (src, tgt) not in self._argos_cache:
            threading.Thread(target=_worker, daemon=True, name="argos-prewarm").start()

    def _on_translation_toggle(self, *_) -> None:
        """Called when the translation enabled checkbox changes state."""
        if self._translation["enabled"].get():
            # Clear the raw queue so old messages are not translated with the new settings
            while not self._raw_queue.empty():
                try:
                    self._raw_queue.get_nowait()
                except queue.Empty:
                    break
            src = ALL_LANGUAGES.get(self._translation["source"].get())
            tgt = ALL_LANGUAGES.get(self._translation["target"].get())
            if src and tgt:
                self._prewarm_translator(src, tgt)
        if self._translation["user_enabled"].get():
            u_src = ALL_LANGUAGES.get(self._translation["user_source"].get())
            u_tgt = ALL_LANGUAGES.get(self._translation["user_target"].get())
            if u_src and u_tgt:
                self._prewarm_translator(u_src, u_tgt)

    def _check_argos_installed(self, src: str, tgt: str) -> bool:
        """Return True if argostranslate has a working translation route for src→tgt."""
        try:
            import argostranslate.translate as at
            lang_map  = {l.code: l for l in at.get_installed_languages()}
            from_lang = lang_map.get(src)
            to_lang   = lang_map.get(tgt)
            if not from_lang or not to_lang:
                return False
            if from_lang.get_translation(to_lang) is not None:
                return True
            # Check indirect route via English
            if src != "en" and tgt != "en":
                en = lang_map.get("en")
                if en:
                    return (from_lang.get_translation(en) is not None and
                            en.get_translation(to_lang) is not None)
            return False
        except Exception:
            return False

    def _download_argos_packages(self, src: str, tgt: str, on_done) -> None:
        """Download and install argostranslate packages for src→tgt in a background thread."""
        def _worker():
            try:
                import argostranslate.package as ap
                ap.update_package_index()
                avail_map = {(p.from_code, p.to_code): p for p in ap.get_available_packages()}

                if (src, tgt) in avail_map:
                    pkgs = [avail_map[(src, tgt)]]
                elif src != "en" and tgt != "en" and (src, "en") in avail_map and ("en", tgt) in avail_map:
                    # Install both legs of the indirect route
                    pkgs = [avail_map[(src, "en")], avail_map[("en", tgt)]]
                else:
                    self.root.after(0, lambda: on_done(False))
                    return

                for pkg in pkgs:
                    ap.install_from_path(pkg.download())

                self._argos_cache.pop((src, tgt), None)
                self._prewarm_translator(src, tgt)
                self.root.after(0, lambda: on_done(True))
            except Exception as e:
                print(f"[argos-download] {e}")
                self.root.after(0, lambda: on_done(False))

        threading.Thread(target=_worker, daemon=True, name="argos-download").start()

    def _get_installed_pairs(self) -> list[tuple[str, str]]:
        """Return a list of (src_code, tgt_code) for all installed argostranslate packages."""
        try:
            import argostranslate.translate as at
            pairs = []
            for lang in at.get_installed_languages():
                for t in lang.translations_from:
                    pairs.append((lang.code, t.to_lang.code))
            return pairs
        except Exception:
            return []

    def _open_translation_dialog(self) -> None:
        TranslationDialog(
            master=self.root,
            translation=self._translation,
            check_fn=self._check_argos_installed,
            download_fn=self._download_argos_packages,
            get_installed_fn=self._get_installed_pairs,
        )

    # ── Connection status ─────────────────────────────────────────────────────

    def _set_connected(self) -> None:
        self._dot.config(fg=ACCENT)
        self._lbl_status.config(text="Connected", fg=ACCENT)

    def _set_disconnected(self) -> None:
        self._dot.config(fg="red")
        self._lbl_status.config(text="Disconnected", fg="red")

    def _clear_chat(self) -> None:
        if self._overlay is not None:
            self._overlay.clear_messages()

    # ── Queue drain → overlay ─────────────────────────────────────────────────

    def _drain_queue(self) -> None:
        """
        Pull translated messages from the display queue and pass them to the overlay.

        Applies whitelist/blacklist filters and the 'Ignore Myself' rule before
        adding a message. Scheduled via after() to run on the Tkinter main thread.
        """
        if self._filters_enabled:
            whitelist = [f for f in self._filters if f["var"].get() and f.get("type") == "whitelist"]
            blacklist = [f for f in self._filters if f["var"].get() and f.get("type") == "blacklist"]
        else:
            whitelist = []
            blacklist = []
        ignore_name = self._ignore_self["name"].lower() if self._ignore_self["var"].get() else ""

        while not self._display_queue.empty():
            original, translated = self._display_queue.get_nowait()
            if not self._translate_active:
                continue  # discard messages while translation is toggled off
            text_low = original.text.lower()

            # WhiteList: find the first matching filter (also captures its custom color)
            matched_color: str | None = None
            if whitelist:
                matched = next((f for f in whitelist if f["keyword"].lower() in text_low), None)
                if matched is None:
                    continue
                matched_color = matched.get("color") or None

            # BlackList: discard if any active keyword is found
            if any(f["keyword"].lower() in text_low for f in blacklist):
                continue

            # Ignore Myself: discard messages that start with the player's own name
            if ignore_name and text_low.startswith(ignore_name):
                continue

            if self._overlay:
                self._overlay.add_message(translated.text, color=matched_color)

        self.root.after(POLL_INTERVAL_MS, self._drain_queue)

    # ── Customize menu ────────────────────────────────────────────────────────

    def _open_chat_menu(self) -> None:
        if self._overlay is None:
            return
        ChatMenuDialog(master=self.root, overlay=self._overlay, hwnd=self._hwnd)

    # ── Filters ───────────────────────────────────────────────────────────────

    def _open_filters_dialog(self) -> None:
        FiltersDialog(
            master=self.root,
            filters=self._filters,
            overlay=self._overlay,
            ignore_self=self._ignore_self,
            no_translate_commands=self._no_translate_commands,
            on_filter_change=self._clear_chat,
        )

    # ── Shortcuts ─────────────────────────────────────────────────────────────

    def _open_shortcuts_dialog(self) -> None:
        ShortcutsDialog(master=self.root, shortcuts=self._shortcuts,
                        on_change=self._on_shortcut_change)

    def _on_shortcut_change(self, key_name: str) -> None:
        """Re-register only the hook whose key just changed."""
        if not self._hwnd:
            return
        if key_name == "chat_key":
            self._setup_chat_hook()
        elif key_name == "toggle_key":
            self._setup_toggle_hook()
        elif key_name == "clear_key":
            self._setup_clear_hook()
        elif key_name == "filters_key":
            self._setup_filters_hook()

    # ── Keyboard hooks ────────────────────────────────────────────────────────

    def _setup_chat_hook(self) -> None:
        """
        Register a global key hook that opens the text-input field when the
        configured chat key is pressed while GTA is the foreground window.
        """
        try:
            import keyboard

            if self._kb_hook is not None:
                try:
                    keyboard.unhook(self._kb_hook)
                except Exception:
                    pass

            key = self._shortcuts["chat_key"]
            if not key:
                self._kb_hook = None
                return

            # _inject_count tracks re-injected key events so they are not
            # suppressed a second time. Increment before injecting, decrement
            # when the injected event comes back through the hook.
            _inject_count = [0]

            def _handler(event):
                try:
                    if _inject_count[0] > 0:
                        _inject_count[0] -= 1
                        return  # let this re-injected event reach other apps
                    if win32gui.GetForegroundWindow() == self._hwnd:
                        # GTA is focused: suppress the key so SA-MP never opens
                        # its native chat, then open our overlay instead.
                        self.root.after(0, self._show_send_input)
                    elif len(key) == 1:
                        # Another app is focused: re-inject so it still receives
                        # the key (suppress=True would otherwise eat it globally).
                        _inject_count[0] += 1
                        win32api.keybd_event(ord(key.upper()), 0, 0, 0)
                except Exception:
                    pass

            self._kb_hook = keyboard.on_press_key(key, _handler, suppress=True)
        except ImportError:
            pass

    def _setup_toggle_hook(self) -> None:
        """
        Register a global key hook that toggles translation on/off when the
        configured toggle key is pressed while GTA is the foreground window.
        """
        try:
            import keyboard

            if self._toggle_kb_hook is not None:
                try:
                    keyboard.unhook(self._toggle_kb_hook)
                except Exception:
                    pass

            key = self._shortcuts["toggle_key"]
            if not key:
                self._toggle_kb_hook = None
                return

            def _handler(event):
                try:
                    if win32gui.GetForegroundWindow() == self._hwnd:
                        self.root.after(0, self._toggle_translate)
                except Exception:
                    pass

            self._toggle_kb_hook = keyboard.on_press_key(key, _handler, suppress=False)
        except ImportError:
            pass

    def _setup_clear_hook(self) -> None:
        """
        Register a global key hook that clears the chat overlay when the
        configured clear key is pressed while GTA is the foreground window.
        """
        try:
            import keyboard

            if self._clear_kb_hook is not None:
                try:
                    keyboard.unhook(self._clear_kb_hook)
                except Exception:
                    pass

            key = self._shortcuts.get("clear_key", "")
            if not key:
                self._clear_kb_hook = None
                return

            def _handler(event):
                try:
                    if win32gui.GetForegroundWindow() == self._hwnd:
                        self.root.after(0, self._clear_chat_hotkey)
                except Exception:
                    pass

            self._clear_kb_hook = keyboard.on_press_key(key, _handler, suppress=False)
        except ImportError:
            pass

    def _setup_filters_hook(self) -> None:
        """
        Register a global key hook that toggles all filters on/off when the
        configured filters key is pressed while GTA is the foreground window.
        """
        try:
            import keyboard

            if self._filters_kb_hook is not None:
                try:
                    keyboard.unhook(self._filters_kb_hook)
                except Exception:
                    pass

            key = self._shortcuts.get("filters_key", "")
            if not key:
                self._filters_kb_hook = None
                return

            def _handler(event):
                try:
                    if win32gui.GetForegroundWindow() == self._hwnd:
                        self.root.after(0, self._toggle_filters_hotkey)
                except Exception:
                    pass

            self._filters_kb_hook = keyboard.on_press_key(key, _handler, suppress=False)
        except ImportError:
            pass

    def _clear_chat_hotkey(self) -> None:
        if not self._translate_active:
            return
        self._clear_chat()
        if self._overlay:
            self._overlay.show_notification("CHAT", "CLEARED", active=True)

    def _toggle_filters_hotkey(self) -> None:
        if not self._translate_active:
            return
        self._filters_enabled = not self._filters_enabled
        if self._overlay:
            self._overlay.clear_messages()
            if self._filters_enabled:
                self._overlay.show_notification("FILTERS", "ON",  active=True)
            else:
                self._overlay.show_notification("FILTERS", "OFF", active=False)

    def _toggle_translate(self) -> None:
        self._translate_active = not self._translate_active
        if self._overlay:
            self._overlay.set_translate_active(self._translate_active)
            if not self._translate_active:
                self._overlay.clear_messages()

    # ── Message sending ───────────────────────────────────────────────────────

    def _show_send_input(self) -> None:
        if self._overlay is None or not self._translate_active:
            return
        self._overlay.show_input(on_submit=self._on_send_submit)

    def _on_send_submit(self, text: str) -> None:
        """
        Optionally translate the typed text (using the User Chat settings)
        then inject it into the SA-MP chat via clipboard + keybd_event.
        """
        def _worker():
            result = text
            if self._translation["user_enabled"].get():
                src = ALL_LANGUAGES.get(self._translation["user_source"].get())
                tgt = ALL_LANGUAGES.get(self._translation["user_target"].get())
                if src and tgt and src != tgt:
                    translator = self._argos_cache.get((src, tgt))
                    if translator:
                        try:
                            result = translator.translate(text) or text
                        except Exception:
                            pass
            self._send_to_samp(result)

        threading.Thread(target=_worker, daemon=True, name="samp-send").start()

    def _send_to_samp(self, text: str) -> None:
        """
        Inject text into the SA-MP chat by:
          1. Copying it to the clipboard as Unicode.
          2. Pressing T to open the SA-MP chat input.
          3. Pressing Ctrl+V to paste.
          4. Pressing Enter to send.

        Timing delays are required so the game's input system has time to
        register each keystroke before the next one arrives.
        """
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()

            time.sleep(0.05)
            win32gui.SetForegroundWindow(self._hwnd)
            time.sleep(0.15)

            win32api.keybd_event(ord('T'), 0, 0, 0)
            time.sleep(0.03)
            win32api.keybd_event(ord('T'), 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.08)

            win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
            win32api.keybd_event(ord('V'), 0, 0, 0)
            time.sleep(0.02)
            win32api.keybd_event(ord('V'), 0, win32con.KEYEVENTF_KEYUP, 0)
            win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
            time.sleep(0.08)

            win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
            time.sleep(0.02)
            win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
        except Exception as e:
            print(f"[send_to_samp] {e}")

    # ── Presets ───────────────────────────────────────────────────────────────

    def _apply_startup_config(self) -> None:
        """Restore the active preset from the last session on startup."""
        self._config.apply(self._config.active_preset, self, None)
        # The trace_add on 'enabled'/'user_enabled' fires during apply() before
        # source/target are set, so _prewarm_translator gets _PLACEHOLDER values
        # and does nothing. Explicitly call it now that all settings are loaded.
        self._on_translation_toggle()

    def _open_presets_dialog(self) -> None:
        PresetsDialog(master=self.root, config=self._config, panel=self, overlay=self._overlay)

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        """Save the current state, unhook all keyboard hooks, and close the overlay."""
        try:
            self._config.save_current(self._config.active_preset, self, self._overlay)
            self._config.save()
        except Exception:
            pass
        for hook in (self._kb_hook, self._toggle_kb_hook, self._clear_kb_hook, self._filters_kb_hook):
            if hook is not None:
                try:
                    import keyboard
                    keyboard.unhook(hook)
                except Exception:
                    pass
        if self._overlay:
            self._overlay.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    app = ControlPanel()
    app.run()


if __name__ == "__main__":
    main()
