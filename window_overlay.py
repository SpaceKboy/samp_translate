"""
Window selection dialog and transparent chat overlay for SAMP Translate.

Key design decisions:
  - ChatOverlay uses Toplevel(master) so the Tk mainloop stays in ControlPanel.
  - The overlay window uses -transparentcolor black: pure black pixels become
    transparent, letting the game render beneath them.
  - Click-through is achieved by subclassing the Win32 window procedure and
    returning HTTRANSPARENT for WM_NCHITTEST, making all mouse events fall
    through to the window behind.
  - stop() calls destroy() (not quit()) to avoid killing the shared mainloop.
"""

import os
import sys


def _fix_tcl_paths() -> None:
    """
    Ensure TCL_LIBRARY and TK_LIBRARY point to the base Python installation.

    When running from a venv the Tcl/Tk DLLs live in the base interpreter's
    directory, not in the venv; without this fix tkinter raises an import error.
    """
    base = getattr(sys, "base_prefix", sys.prefix)
    tcl = os.path.join(base, "tcl", "tcl8.6")
    tk_ = os.path.join(base, "tcl", "tk8.6")
    if os.path.isdir(tcl):
        os.environ.setdefault("TCL_LIBRARY", tcl)
        os.environ.setdefault("TK_LIBRARY", tk_)


_fix_tcl_paths()

import time
import tkinter as tk
from tkinter import ttk, messagebox
import win32gui
import win32process
import win32api
import win32con

# How often the overlay refreshes its position to follow the game window
UPDATE_INTERVAL_MS = 100


def _draw_rounded_rect(canvas, x1, y1, x2, y2, r, fill="#111111", outline="white", lw=1):
    """
    Draw a rounded rectangle on a tk.Canvas.

    Implemented as two overlapping rectangles (horizontal + vertical strips)
    plus four arc corners, then an outline drawn the same way.
    """
    # Filled body
    canvas.create_rectangle(x1 + r, y1,     x2 - r, y2,     fill=fill, outline="")
    canvas.create_rectangle(x1,     y1 + r, x2,     y2 - r, fill=fill, outline="")
    for ax, ay, start in [
        (x1,         y1,         90),
        (x2 - 2 * r, y1,          0),
        (x1,         y2 - 2 * r, 180),
        (x2 - 2 * r, y2 - 2 * r, 270),
    ]:
        canvas.create_arc(ax, ay, ax + 2 * r, ay + 2 * r,
                          start=start, extent=90, fill=fill, outline="")
    # Outline
    canvas.create_line(x1 + r, y1, x2 - r, y1, fill=outline, width=lw)
    canvas.create_line(x1 + r, y2, x2 - r, y2, fill=outline, width=lw)
    canvas.create_line(x1, y1 + r, x1, y2 - r, fill=outline, width=lw)
    canvas.create_line(x2, y1 + r, x2, y2 - r, fill=outline, width=lw)
    for ax, ay, start in [
        (x1,         y1,         90),
        (x2 - 2 * r, y1,          0),
        (x1,         y2 - 2 * r, 180),
        (x2 - 2 * r, y2 - 2 * r, 270),
    ]:
        canvas.create_arc(ax, ay, ax + 2 * r, ay + 2 * r,
                          start=start, extent=90, style="arc", outline=outline, width=lw)


def list_visible_windows() -> list[tuple[int, str, str]]:
    """
    Return a list of (hwnd, title, process_name) for all visible, titled windows.

    Results are sorted alphabetically by process name to make GTA easy to find.
    """
    windows = []

    def _enum(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            h = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                False, pid,
            )
            path = win32process.GetModuleFileNameEx(h, 0)
            win32api.CloseHandle(h)
            proc_name = path.split("\\")[-1]
        except Exception:
            proc_name = "?"
        windows.append((hwnd, title, proc_name))

    win32gui.EnumWindows(_enum, None)
    windows.sort(key=lambda w: w[2].lower())
    return windows


def get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Return (x, y, width, height) for the given window, or None on error."""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return left, top, right - left, bottom - top
    except Exception:
        return None


class SelectorDialog:
    """
    Modal dialog that lets the user pick a target window from a list.

    When called with master= it opens as a Toplevel and blocks via grab_set +
    wait_window. Without master it creates its own Tk root and enters mainloop.
    The chosen HWND is stored in self.selected_hwnd (None if cancelled).
    """

    def __init__(self, master: tk.Misc | None = None):
        self.selected_hwnd: int | None = None
        self._master = master

        if master is not None:
            self.root = tk.Toplevel(master)
        else:
            self.root = tk.Tk()

        self.root.title("Select Window")
        self.root.resizable(True, True)
        self.root.minsize(520, 360)
        self._build_ui()

        if master is not None:
            self.root.grab_set()
            master.wait_window(self.root)
        else:
            self.root.mainloop()

    def _build_ui(self):
        top = tk.Frame(self.root, padx=8, pady=6)
        top.pack(fill="x")

        tk.Label(top, text="Filter:").pack(side="left")
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._refresh_list())
        tk.Entry(top, textvariable=self._filter_var, width=30).pack(side="left", padx=4)

        tk.Button(top, text="Refresh", command=self._refresh_list).pack(side="right")

        cols = ("proc", "title", "hwnd")
        frame = tk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=8)

        self._tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        self._tree.heading("proc",  text="Process")
        self._tree.heading("title", text="Window Title")
        self._tree.heading("hwnd",  text="HWND")
        self._tree.column("proc",  width=140, anchor="w")
        self._tree.column("title", width=280, anchor="w")
        self._tree.column("hwnd",  width=80,  anchor="center")

        sb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._tree.bind("<Double-1>", lambda _: self._confirm())

        btn_frame = tk.Frame(self.root, pady=6)
        btn_frame.pack()
        tk.Button(btn_frame, text="OK",     width=12, command=self._confirm).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Cancel", width=12, command=self.root.destroy).pack(side="left", padx=6)

        self._all_windows: list[tuple[int, str, str]] = []
        self._refresh_list()

    def _refresh_list(self):
        self._all_windows = list_visible_windows()
        self._apply_filter()

    def _apply_filter(self):
        term = self._filter_var.get().lower()
        self._tree.delete(*self._tree.get_children())
        for hwnd, title, proc in self._all_windows:
            if term in title.lower() or term in proc.lower():
                self._tree.insert("", "end", iid=str(hwnd), values=(proc, title, hwnd))

    def _confirm(self):
        sel = self._tree.selection()
        if not sel:
            messagebox.showwarning("Warning", "Please select a window first.")
            return
        self.selected_hwnd = int(sel[0])
        self.root.destroy()


class ChatOverlay:
    """
    Transparent, always-on-top overlay that renders translated chat text over GTA.

    The overlay window is sized and positioned to exactly match the game window
    every UPDATE_INTERVAL_MS milliseconds. Pure black pixels are transparent
    (via -transparentcolor black), and WM_NCHITTEST returns HTTRANSPARENT so all
    mouse and keyboard input passes through to the game unimpeded.
    """

    MAX_MESSAGES = 9
    # SA-MP's native chat occupies roughly 50–72 % of the window height;
    # we place translated text just below that band.
    CHAT_Y_RATIO  = 0.73
    CHAT_X_OFFSET = 8     # pixels from the left edge
    LINE_HEIGHT   = 19    # vertical spacing between chat lines
    FONT          = ("Arial", 10, "bold")
    TEXT_COLOR    = "#FFFFFF"
    # Shadow is near-black, not pure black — pure black is the transparent key colour
    SHADOW_COLOR  = "#111111"

    def __init__(self, hwnd: int, master: tk.Misc | None = None):
        self._hwnd   = hwnd
        self._running = True
        self._messages: list[tuple[str, str | None]] = []

        # Default chat position (pixels from top-left of game window)
        self._pos_x: int | None = 48
        self._pos_y: int | None = 330

        # Default text-input field position
        self._input_pos_x: int | None = 48
        self._input_pos_y: int | None = 267

        # Translation toggle state (reflected in the status indicator)
        self._translate_active: bool = True

        # Status indicator ("SAMP AUTO TRANSLATE ON/OFF") settings
        self._status_visible:   bool = True
        self._status_font_size: int  = 9
        self._status_pos_x: int | None = 1395  # None = auto-right
        self._status_pos_y: int         = 855

        # Notification banner settings
        self._notif_font_size: int     = 9
        self._notif_pos_x:     int | None = None  # None = auto top-right
        self._notif_pos_y:     int        = 10

        self.root = tk.Toplevel(master) if master is not None else tk.Tk()
        self._configure_window()
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._create_status_widget()
        self._create_notif_widget()
        self._update()

    def _configure_window(self):
        """Set up the transparent, borderless, always-on-top overlay window."""
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", "black")
        self.root.attributes("-alpha", 1.0)
        self.root.geometry("1x1+0+0")
        self.root.configure(bg="black")
        self.root.after(0, self._set_click_through)

    def _set_click_through(self) -> None:
        """
        Subclass the Win32 window procedure to return HTTRANSPARENT on WM_NCHITTEST.

        This makes the overlay 100 % click-through: the user can interact with the
        game normally even when the chat text is rendered on top of it.
        """
        try:
            import ctypes
            from ctypes import wintypes

            hwnd          = self.root.winfo_id()
            WM_NCHITTEST  = 0x0084
            HTTRANSPARENT = -1
            GWLP_WNDPROC  = -4

            WNDPROC = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            )

            def _proc(h, msg, wp, lp):
                if msg == WM_NCHITTEST:
                    return HTTRANSPARENT
                return ctypes.windll.user32.CallWindowProcW(
                    self._overlay_old_wndproc, h, msg, wp, lp,
                )

            # Keep a reference to the callback so it is not garbage-collected
            self._overlay_wndproc     = WNDPROC(_proc)
            self._overlay_old_wndproc = ctypes.windll.user32.SetWindowLongW(
                hwnd, GWLP_WNDPROC, self._overlay_wndproc,
            )
        except Exception:
            pass

    def _create_status_widget(self) -> None:
        """Build the 'SAMP AUTO TRANSLATE ON/OFF' indicator widget."""
        font  = ("Segoe UI", self._status_font_size, "bold")
        frame = tk.Frame(self.root, bg="#111111", padx=8, pady=4)
        self._status_dot = tk.Label(
            frame, text="●", bg="#111111", fg="#FF4444", font=font,
        )
        self._status_dot.pack(side="left")
        self._status_title = tk.Label(
            frame, text=" SAMP AUTO TRANSLATE  ", bg="#111111", fg="#FFFFFF", font=font,
        )
        self._status_title.pack(side="left")
        self._status_state = tk.Label(
            frame, text="OFF", bg="#111111", fg="#FF4444", font=font,
        )
        self._status_state.pack(side="left")
        self._status_frame = frame
        # Position is applied on the first _update_status_position() call

    def _create_notif_widget(self) -> None:
        """Build the temporary notification banner (hidden by default)."""
        font  = ("Segoe UI", self._notif_font_size, "bold")
        frame = tk.Frame(self.root, bg="#111111", padx=8, pady=4)
        self._notif_dot   = tk.Label(frame, text="●", bg="#111111", fg="#00FF00", font=font)
        self._notif_title = tk.Label(frame, text="",  bg="#111111", fg="#FFFFFF", font=font)
        self._notif_state = tk.Label(frame, text="",  bg="#111111", fg="#00FF00", font=font)
        self._notif_dot.pack(side="left")
        self._notif_title.pack(side="left")
        self._notif_state.pack(side="left")
        self._notif_frame    = frame
        self._notif_hide_job: str | None = None

    def show_notification(
        self, title: str, state_text: str, active: bool = True, duration_ms: int = 2500
    ) -> None:
        """
        Flash a temporary banner (e.g. "FILTERS ON") and hide it after duration_ms.

        active=True uses green; active=False uses red.
        """
        color = "#00FF00" if active else "#FF4444"
        self._notif_dot.config(fg=color)
        self._notif_title.config(text=f" {title}  ")
        self._notif_state.config(text=state_text, fg=color)
        if self._notif_hide_job is not None:
            try:
                self.root.after_cancel(self._notif_hide_job)
            except Exception:
                pass
        # Initial placement; final x is calculated in _update_notif_position()
        self._notif_frame.place(x=0, y=10)
        self._notif_hide_job = self.root.after(duration_ms, self._hide_notification)

    def _hide_notification(self) -> None:
        self._notif_frame.place_forget()
        self._notif_hide_job = None

    def get_notif_position(self, window_w: int) -> tuple[int, int]:
        if self._notif_pos_x is None:
            fw = self._notif_frame.winfo_reqwidth()
            x  = window_w - fw - 10 if fw > 0 else window_w - 200
        else:
            x = self._notif_pos_x
        return x, self._notif_pos_y

    def set_notif_position(self, x: int, y: int) -> None:
        self._notif_pos_x = x
        self._notif_pos_y = y

    def get_notif_font_size(self) -> int:
        return self._notif_font_size

    def set_notif_font_size(self, size: int) -> None:
        self._notif_font_size = max(6, min(28, size))
        font = ("Segoe UI", self._notif_font_size, "bold")
        self._notif_dot.config(font=font)
        self._notif_title.config(font=font)
        self._notif_state.config(font=font)

    def _update_notif_position(self, w: int) -> None:
        """Reposition the notification banner. Called every update cycle."""
        if not self._notif_frame.winfo_ismapped():
            return
        if self._notif_pos_x is None:
            fw = self._notif_frame.winfo_reqwidth()
            sx = w - fw - 10 if fw > 0 else w - 200
        else:
            sx = self._notif_pos_x
        self._notif_frame.place(x=sx, y=self._notif_pos_y)

    def _update_status_position(self, w: int) -> None:
        """Reposition the status indicator. Called every update cycle."""
        if not self._status_visible:
            self._status_frame.place_forget()
            return
        if self._status_pos_x is None:
            fw = self._status_frame.winfo_reqwidth()
            sx = w - fw - 10 if fw > 0 else w - 220
        else:
            sx = self._status_pos_x
        self._status_frame.place(x=sx, y=self._status_pos_y)

    # ── Status overlay getters / setters ──────────────────────────────────────

    def get_status_position(self, window_w: int) -> tuple[int, int]:
        if self._status_pos_x is None:
            fw = self._status_frame.winfo_reqwidth()
            x  = window_w - fw - 10 if fw > 0 else window_w - 220
        else:
            x = self._status_pos_x
        return x, self._status_pos_y

    def set_status_position(self, x: int, y: int) -> None:
        self._status_pos_x = x
        self._status_pos_y = y

    def get_status_font_size(self) -> int:
        return self._status_font_size

    def set_status_font_size(self, size: int) -> None:
        self._status_font_size = max(6, min(28, size))
        font = ("Segoe UI", self._status_font_size, "bold")
        for w in self._status_frame.winfo_children():
            w.config(font=font)
        self._status_dot.config(font=font)
        self._status_title.config(font=font)
        self._status_state.config(font=font)
        if self._status_pos_x is None:
            # Force position recalculation after font size changes widget width
            self._status_frame.place_forget()

    def get_status_visible(self) -> bool:
        return self._status_visible

    def set_status_visible(self, visible: bool) -> None:
        self._status_visible = visible
        if not visible:
            self._status_frame.place_forget()

    def set_translate_active(self, active: bool) -> None:
        """Update the status indicator dot and text to reflect the active/inactive state."""
        self._translate_active = active
        color = "#00FF00" if active else "#FF4444"
        text  = "ON"      if active else "OFF"
        self._status_dot.config(fg=color)
        self._status_state.config(text=text, fg=color)

    # ── Chat message management ────────────────────────────────────────────────

    def add_message(self, text: str, color: str | None = None) -> None:
        """Append a message and evict the oldest if over MAX_MESSAGES."""
        self._messages.append((text, color))
        if len(self._messages) > self.MAX_MESSAGES:
            self._messages.pop(0)

    def clear_messages(self) -> None:
        """Remove all displayed messages."""
        self._messages.clear()

    def set_style(self, font_family: str, font_size: int, color: str) -> None:
        self.FONT       = (font_family, font_size, "bold")
        self.TEXT_COLOR = color

    def get_style(self) -> tuple[str, int, str]:
        return self.FONT[0], self.FONT[1], self.TEXT_COLOR

    def get_max_messages(self) -> int:
        return self.MAX_MESSAGES

    def set_max_messages(self, n: int) -> None:
        self.MAX_MESSAGES = max(1, n)
        while len(self._messages) > self.MAX_MESSAGES:
            self._messages.pop(0)

    def get_position(self, window_h: int) -> tuple[int, int]:
        x = self._pos_x if self._pos_x is not None else self.CHAT_X_OFFSET
        y = self._pos_y if self._pos_y is not None else int(window_h * self.CHAT_Y_RATIO)
        return x, y

    def set_position(self, x: int, y: int) -> None:
        self._pos_x = x
        self._pos_y = y

    def get_input_position(self, window_h: int) -> tuple[int, int]:
        if self._input_pos_x is not None and self._input_pos_y is not None:
            return self._input_pos_x, self._input_pos_y
        cx, cy = self.get_position(window_h)
        return cx, cy + self.MAX_MESSAGES * self.LINE_HEIGHT + 6

    def set_input_position(self, x: int, y: int) -> None:
        self._input_pos_x = x
        self._input_pos_y = y

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _redraw(self, w: int, h: int):
        """Repaint all chat lines onto the canvas with a 1-px drop shadow."""
        self.canvas.delete("all")
        chat_x, chat_y = self.get_position(h)

        for i, (msg, msg_color) in enumerate(self._messages):
            y = chat_y + i * self.LINE_HEIGHT
            if y + self.LINE_HEIGHT > h:
                break
            text_color = msg_color if msg_color else self.TEXT_COLOR
            # Shadow (offset by 1 px)
            self.canvas.create_text(
                chat_x + 1, y + 1,
                text=msg, anchor="nw",
                fill=self.SHADOW_COLOR,
                font=self.FONT,
            )
            # Main text
            self.canvas.create_text(
                chat_x, y,
                text=msg, anchor="nw",
                fill=text_color,
                font=self.FONT,
            )

    def _update(self):
        """
        Main update loop — runs every UPDATE_INTERVAL_MS via tkinter's after().

        Repositions the overlay to match the game window and triggers a redraw.
        Hides the overlay if the game window is invalid or minimized.
        """
        if not self._running:
            return

        if not win32gui.IsWindow(self._hwnd):
            self.root.withdraw()
            self.root.after(UPDATE_INTERVAL_MS, self._update)
            return

        rect = get_window_rect(self._hwnd)
        if rect and rect[2] > 0 and rect[3] > 0:
            x, y, w, h = rect
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            self.canvas.config(width=w, height=h)
            self._redraw(w, h)
            self._update_status_position(w)
            self._update_notif_position(w)
            self.root.deiconify()
        else:
            self.root.withdraw()

        self.root.after(UPDATE_INTERVAL_MS, self._update)

    # ── Text input field ──────────────────────────────────────────────────────

    # Shift-key character map for a US QWERTY layout
    _SHIFT_MAP = {
        '1': '!', '2': '@', '3': '#', '4': '$', '5': '%',
        '6': '^', '7': '&', '8': '*', '9': '(', '0': ')',
        '`': '~', '-': '_', '=': '+', '[': '{', ']': '}',
        '\\': '|', ';': ':', '\'': '"', ',': '<', '.': '>', '/': '?',
    }

    # Pill-shaped input box dimensions
    _INPUT_W        = 340
    _INPUT_H        = 28
    _INPUT_R        = 14   # radius = H // 2 → pill shape
    _INPUT_PAD_LEFT = 14   # internal left padding
    _INPUT_TEXT_X   = 22   # text starts after the cursor line

    def _input_position(self) -> tuple[int, int]:
        if self._input_pos_x is not None and self._input_pos_y is not None:
            return self._input_pos_x, self._input_pos_y
        x = self._pos_x if self._pos_x is not None else self.CHAT_X_OFFSET
        y = (self._pos_y if self._pos_y is not None else 330) + self.MAX_MESSAGES * self.LINE_HEIGHT + 6
        return x, y

    def _make_input_canvas(self, x: int, y: int) -> tk.Canvas:
        c = tk.Canvas(
            self.root,
            width=self._INPUT_W, height=self._INPUT_H,
            bg="black", highlightthickness=0, bd=0,
        )
        c.place(x=x, y=y)
        return c

    def _draw_input_canvas(self, text: str = "", cursor: bool = False) -> None:
        """Repaint the input pill: background, optional cursor line, and typed text."""
        c = self._input_frame
        if c is None:
            return
        try:
            if not c.winfo_exists():
                return
        except Exception:
            return
        W, H, R = self._INPUT_W, self._INPUT_H, self._INPUT_R
        c.delete("all")
        _draw_rounded_rect(c, 0, 0, W - 1, H - 1, R, fill="#111111", outline="white", lw=1)
        if cursor:
            px = self._INPUT_PAD_LEFT
            c.create_line(px, 5, px, H - 5, fill="white", width=2)
        if text:
            c.create_text(self._INPUT_TEXT_X, H // 2, text=text,
                          anchor="w", fill="white", font=("Arial", 10))

    def _blink_input_cursor(self) -> None:
        if getattr(self, "_input_frame", None) is None:
            return
        try:
            if not self._input_frame.winfo_exists():
                return
        except Exception:
            return
        self._cursor_visible = not getattr(self, "_cursor_visible", True)
        self._draw_input_canvas(
            text=getattr(self, "_input_var", tk.StringVar()).get(),
            cursor=self._cursor_visible,
        )
        self._cursor_job = self.root.after(530, self._blink_input_cursor)

    def show_input_preview(self) -> None:
        """
        Show the text-input pill without activating the keyboard hook.

        Used by ChatInputPositionDialog so the user can drag the field to
        their preferred position before it goes live.
        """
        if getattr(self, "_input_frame", None) is not None:
            try:
                if self._input_frame.winfo_exists():
                    return
            except Exception:
                pass
        x, y = self._input_position()
        self._input_frame    = self._make_input_canvas(x, y)
        self._input_kb_hook  = None
        self._draw_input_canvas(text="Text Chat", cursor=False)

    def move_input_preview(self, x: int, y: int) -> None:
        """Move the preview pill in real time while the position dialog is open."""
        if getattr(self, "_input_frame", None) is not None:
            try:
                if self._input_frame.winfo_exists():
                    self._input_frame.place(x=x, y=y)
            except Exception:
                pass

    def show_input(self, on_submit, on_cancel=None) -> None:
        """
        Display the interactive text-input field over the overlay.

        A global keyboard hook captures all key events without stealing focus
        from GTA. Pressing Enter calls on_submit(text); Escape calls on_cancel().
        """
        if getattr(self, "_input_frame", None) is not None:
            try:
                if self._input_frame.winfo_exists():
                    return
            except Exception:
                pass

        x, y = self._input_position()
        self._input_frame    = self._make_input_canvas(x, y)
        self._input_var      = tk.StringVar()
        self._cursor_visible = True
        self._cursor_job     = None
        self._draw_input_canvas(text="", cursor=True)
        self._blink_input_cursor()

        def _submit():
            text = self._input_var.get().strip()
            self._close_input()
            if text and on_submit:
                on_submit(text)

        def _cancel():
            self._close_input()
            if on_cancel:
                on_cancel()

        self._start_input_hook(_submit, _cancel)

    def _start_input_hook(self, on_submit, on_cancel) -> None:
        """
        Install a global keyboard hook that routes all key events to the input field.

        The hook uses suppress=True so keypresses do not also reach the game while
        the input field is open.
        """
        try:
            import keyboard as kb

            def _refresh(text):
                self._draw_input_canvas(text=text, cursor=getattr(self, "_cursor_visible", True))

            def _handler(event):
                if event.event_type != kb.KEY_DOWN:
                    return
                name = event.name
                if name == 'enter':
                    self.root.after(0, on_submit)
                elif name in ('escape', 'esc'):
                    self.root.after(0, on_cancel)
                elif name == 'backspace':
                    def _back():
                        t = self._input_var.get()[:-1]
                        self._input_var.set(t)
                        _refresh(t)
                    self.root.after(0, _back)
                else:
                    char = self._resolve_char(name)
                    if char is not None:
                        def _append(c=char):
                            t = self._input_var.get() + c
                            self._input_var.set(t)
                            _refresh(t)
                        self.root.after(0, _append)

            self._input_kb_hook = kb.hook(_handler, suppress=True)
        except ImportError:
            self._input_kb_hook = None

    def _resolve_char(self, name: str) -> str | None:
        """
        Map a key name to the character it produces, respecting Shift and Caps Lock.

        Handles only printable ASCII characters on a US QWERTY layout.
        Returns None for non-printable keys (arrows, function keys, etc.).
        """
        try:
            import keyboard as kb
            shift = kb.is_pressed('shift')
            caps  = kb.is_pressed('caps lock')
        except Exception:
            shift = caps = False

        if len(name) == 1:
            if name.isalpha():
                return name.upper() if (shift ^ caps) else name
            if shift and name in self._SHIFT_MAP:
                return self._SHIFT_MAP[name]
            return name
        if name == 'space':
            return ' '
        return None

    def _close_input(self) -> None:
        """Stop the cursor blink job, unhook the keyboard, and destroy the input pill."""
        if getattr(self, "_cursor_job", None) is not None:
            try:
                self.root.after_cancel(self._cursor_job)
            except Exception:
                pass
            self._cursor_job = None
        if getattr(self, "_input_kb_hook", None) is not None:
            try:
                import keyboard as kb
                kb.unhook(self._input_kb_hook)
            except Exception:
                pass
            self._input_kb_hook = None
        if getattr(self, "_input_frame", None) is not None:
            try:
                self._input_frame.destroy()
            except Exception:
                pass
            self._input_frame = None

    def stop(self):
        """Cleanly shut down the overlay (close input, stop update loop, destroy window)."""
        self._close_input()
        self._running = False
        self.root.destroy()


if __name__ == "__main__":
    dialog = SelectorDialog()
    hwnd = dialog.selected_hwnd

    if hwnd is None:
        print("No window selected. Exiting.")
    else:
        print(f"Monitoring HWND={hwnd} — '{win32gui.GetWindowText(hwnd)}'")
        overlay = ChatOverlay(hwnd)
        overlay.root.mainloop()
