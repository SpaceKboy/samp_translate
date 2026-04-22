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


def _draw_rounded_rect(canvas, x1, y1, x2, y2, r, fill="#111111", outline="white", lw=1):
    """Desenha retângulo com cantos arredondados no canvas."""
    # preenchimento: dois retângulos + quatro arcos de canto
    canvas.create_rectangle(x1+r, y1,   x2-r, y2,   fill=fill, outline="")
    canvas.create_rectangle(x1,   y1+r, x2,   y2-r, fill=fill, outline="")
    for ax, ay, start in [
        (x1,     y1,     90), (x2-2*r, y1,     0),
        (x1,     y2-2*r, 180),(x2-2*r, y2-2*r, 270),
    ]:
        canvas.create_arc(ax, ay, ax+2*r, ay+2*r, start=start, extent=90, fill=fill, outline="")
    # contorno
    canvas.create_line(x1+r, y1, x2-r, y1, fill=outline, width=lw)
    canvas.create_line(x1+r, y2, x2-r, y2, fill=outline, width=lw)
    canvas.create_line(x1, y1+r, x1, y2-r, fill=outline, width=lw)
    canvas.create_line(x2, y1+r, x2, y2-r, fill=outline, width=lw)
    for ax, ay, start in [
        (x1,     y1,     90), (x2-2*r, y1,     0),
        (x1,     y2-2*r, 180),(x2-2*r, y2-2*r, 270),
    ]:
        canvas.create_arc(ax, ay, ax+2*r, ay+2*r,
                          start=start, extent=90, style="arc", outline=outline, width=lw)


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
        self._input_pos_x: int | None = 48
        self._input_pos_y: int | None = 267

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

    def add_message(self, text: str, color: str | None = None) -> None:
        self._messages.append((text, color))
        if len(self._messages) > self.MAX_MESSAGES:
            self._messages.pop(0)

    def clear_messages(self) -> None:
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

    def get_input_position(self, window_h: int) -> tuple[int, int]:
        if self._input_pos_x is not None and self._input_pos_y is not None:
            return self._input_pos_x, self._input_pos_y
        cx, cy = self.get_position(window_h)
        return cx, cy + self.MAX_MESSAGES * self.LINE_HEIGHT + 6

    def set_input_position(self, x: int, y: int) -> None:
        self._input_pos_x = x
        self._input_pos_y = y

    def _redraw(self, w: int, h: int):
        self.canvas.delete("all")
        chat_x, chat_y = self.get_position(h)

        for i, (msg, msg_color) in enumerate(self._messages):
            y = chat_y + i * self.LINE_HEIGHT
            if y + self.LINE_HEIGHT > h:
                break
            text_color = msg_color if msg_color else self.TEXT_COLOR
            self.canvas.create_text(
                chat_x + 1, y + 1,
                text=msg, anchor="nw",
                fill=self.SHADOW_COLOR,
                font=self.FONT,
            )
            self.canvas.create_text(
                chat_x, y,
                text=msg, anchor="nw",
                fill=text_color,
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

    # Mapeamento shift+tecla para US QWERTY
    _SHIFT_MAP = {
        '1':'!','2':'@','3':'#','4':'$','5':'%','6':'^','7':'&','8':'*','9':'(','0':')',
        '`':'~','-':'_','=':'+','[':'{',']':'}','\\':'|',';':':','\'':'"',',':'<','.':'>','/':'?',
    }

    # ── dimensões do input ────────────────────────────────────────────────────
    _INPUT_W  = 340
    _INPUT_H  = 28
    _INPUT_R  = 14   # = H//2 → forma de pílula
    _INPUT_PAD_LEFT  = 14   # margem esquerda interna
    _INPUT_TEXT_X    = 22   # texto começa após o cursor

    def _input_position(self) -> tuple[int, int]:
        if self._input_pos_x is not None and self._input_pos_y is not None:
            return self._input_pos_x, self._input_pos_y
        x = self._pos_x if self._pos_x is not None else self.CHAT_X_OFFSET
        y = (self._pos_y if self._pos_y is not None else 330) + self.MAX_MESSAGES * self.LINE_HEIGHT + 6
        return x, y

    def _make_input_canvas(self, x: int, y: int) -> tk.Canvas:
        c = tk.Canvas(self.root, width=self._INPUT_W, height=self._INPUT_H,
                      bg="black", highlightthickness=0, bd=0)
        c.place(x=x, y=y)
        return c

    def _draw_input_canvas(self, text: str = "", cursor: bool = False) -> None:
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
        _draw_rounded_rect(c, 0, 0, W-1, H-1, R, fill="#111111", outline="white", lw=1)
        if cursor:
            px = self._INPUT_PAD_LEFT
            c.create_line(px, 5, px, H-5, fill="white", width=2)
        if text:
            c.create_text(self._INPUT_TEXT_X, H//2, text=text,
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
        """Mostra o campo de texto visualmente (sem hook) para orientar o usuário."""
        if getattr(self, "_input_frame", None) is not None:
            try:
                if self._input_frame.winfo_exists():
                    return
            except Exception:
                pass
        x, y = self._input_position()
        self._input_frame = self._make_input_canvas(x, y)
        self._input_kb_hook = None
        self._draw_input_canvas(text="Chat de texto", cursor=False)

    def move_input_preview(self, x: int, y: int) -> None:
        """Reposiciona o preview do campo de texto em tempo real."""
        if getattr(self, "_input_frame", None) is not None:
            try:
                if self._input_frame.winfo_exists():
                    self._input_frame.place(x=x, y=y)
            except Exception:
                pass

    def show_input(self, on_submit, on_cancel=None) -> None:
        """Exibe caixinha de texto sobre o overlay sem roubar o foco do GTA."""
        if getattr(self, "_input_frame", None) is not None:
            try:
                if self._input_frame.winfo_exists():
                    return
            except Exception:
                pass

        x, y = self._input_position()
        self._input_frame = self._make_input_canvas(x, y)
        self._input_var    = tk.StringVar()
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
        """Hook global que captura todo o teclado enquanto o input está aberto."""
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
        """Converte nome de tecla em caractere, respeitando shift e caps lock."""
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
        self._close_input()
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