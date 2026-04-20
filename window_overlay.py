import os
import sys

def _fix_tcl_paths():
    base = getattr(sys, "base_prefix", sys.prefix)
    tcl = os.path.join(base, "tcl", "tcl8.6")
    tk_ = os.path.join(base, "tcl", "tk8.6")
    if os.path.isdir(tcl):
        os.environ.setdefault("TCL_LIBRARY", tcl)
        os.environ.setdefault("TK_LIBRARY", tk_)

_fix_tcl_paths()

import tkinter as tk
from tkinter import ttk, messagebox
import win32gui
import win32process
import win32api
import win32con

UPDATE_INTERVAL_MS = 100


def list_visible_windows() -> list[tuple[int, str, str]]:
    """Retorna lista de (hwnd, titulo, nome_do_processo) de janelas visíveis."""
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
    """Retorna (x, y, largura, altura) ou None."""
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return left, top, right - left, bottom - top
    except Exception:
        return None


class SelectorDialog:
    """Diálogo para o usuário escolher a janela alvo."""

    def __init__(self, master: tk.Misc | None = None):
        self.selected_hwnd: int | None = None
        self._master = master

        if master is not None:
            self.root = tk.Toplevel(master)
        else:
            self.root = tk.Tk()

        self.root.title("Selecionar janela")
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

        tk.Label(top, text="Filtrar:").pack(side="left")
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._refresh_list())
        tk.Entry(top, textvariable=self._filter_var, width=30).pack(side="left", padx=4)

        tk.Button(top, text="Atualizar", command=self._refresh_list).pack(side="right")

        cols = ("proc", "title", "hwnd")
        frame = tk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=8)

        self._tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        self._tree.heading("proc",  text="Processo")
        self._tree.heading("title", text="Título da janela")
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
        tk.Button(btn_frame, text="OK", width=12, command=self._confirm).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Cancelar", width=12, command=self.root.destroy).pack(side="left", padx=6)

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
            messagebox.showwarning("Aviso", "Selecione uma janela primeiro.")
            return
        self.selected_hwnd = int(sel[0])
        self.root.destroy()


class ChatOverlay:
    """Overlay transparente sobre o GTA que exibe o chat traduzido abaixo do chat SA-MP."""

    MAX_MESSAGES = 9
    # SA-MP chat nativo ocupa aprox. 50-72% da altura — começamos logo abaixo
    CHAT_Y_RATIO = 0.73
    CHAT_X_OFFSET = 8
    LINE_HEIGHT = 19
    FONT = ("Arial", 10, "bold")
    TEXT_COLOR = "#FFFFFF"
    # Sombra quase preta (não puro preto, pois preto = transparente com -transparentcolor)
    SHADOW_COLOR = "#111111"

    def __init__(self, hwnd: int, master: tk.Misc | None = None):
        self._hwnd = hwnd
        self._running = True
        self._messages: list[str] = []
        self._pos_x: int | None = 48
        self._pos_y: int | None = 330

        self.root = tk.Toplevel(master) if master is not None else tk.Tk()
        self._configure_window()
        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._update()

    def _configure_window(self):
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", "black")
        self.root.attributes("-alpha", 1.0)
        self.root.geometry("1x1+0+0")
        self.root.configure(bg="black")

    def add_message(self, text: str):
        self._messages.append(text)
        if len(self._messages) > self.MAX_MESSAGES:
            self._messages.pop(0)

    def clear_messages(self):
        self._messages.clear()

    def set_style(self, font_family: str, font_size: int, color: str) -> None:
        self.FONT = (font_family, font_size, "bold")
        self.TEXT_COLOR = color

    def get_style(self) -> tuple[str, int, str]:
        family = self.FONT[0]
        size   = self.FONT[1]
        return family, size, self.TEXT_COLOR

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

    def _redraw(self, w: int, h: int):
        self.canvas.delete("all")
        chat_x, chat_y = self.get_position(h)

        for i, msg in enumerate(self._messages):
            y = chat_y + i * self.LINE_HEIGHT
            if y + self.LINE_HEIGHT > h:
                break
            # sombra para legibilidade
            self.canvas.create_text(
                chat_x + 1, y + 1,
                text=msg, anchor="nw",
                fill=self.SHADOW_COLOR,
                font=self.FONT,
            )
            self.canvas.create_text(
                chat_x, y,
                text=msg, anchor="nw",
                fill=self.TEXT_COLOR,
                font=self.FONT,
            )

    def _update(self):
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
            self.root.deiconify()
        else:
            self.root.withdraw()

        self.root.after(UPDATE_INTERVAL_MS, self._update)

    def stop(self):
        self._running = False
        self.root.destroy()


if __name__ == "__main__":
    dialog = SelectorDialog()
    hwnd = dialog.selected_hwnd

    if hwnd is None:
        print("Nenhuma janela selecionada. Encerrando.")
    else:
        print(f"Monitorando HWND={hwnd} — '{win32gui.GetWindowText(hwnd)}'")
        overlay = ChatOverlay(hwnd)
        overlay.root.mainloop()