"""
SAMP Translate — ponto de entrada principal.

Fluxo:
    1. Abre ControlPanel (menu externo compacto, always-on-top)
    2. Usuário clica em "Selecionar janela GTA" → SelectorDialog
    3. Inicia ChatOverlay sobreposto ao GTA (Toplevel transparente)
    4. Thread de fundo lê o chat SA-MP → queue.Queue
    5. Loop tkinter drena a fila → ChatOverlay.add_message()
"""

import os
import sys
import queue
import threading
import time
import dataclasses

def _fix_tcl_paths() -> None:
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

# ── Paleta ───────────────────────────────────────────────────────────────────

BG       = "#1a1a2e"
BG_PANEL = "#16213e"
FG       = "#e0e0e0"
FG_DIM   = "#888888"
ACCENT   = "#00FF00"
FONT_UI  = ("Segoe UI", 9)

POLL_INTERVAL_MS = 200

LANGUAGES: dict[str, str] = {
    "Português": "pt",
    "Espanhol":  "es",
}


# ── Diálogo de posição do chat ───────────────────────────────────────────────

class ChatPositionDialog:
    STEP = 5  # pixels por clique de seta

    def __init__(self, master: tk.Misc, overlay: ChatOverlay, hwnd: int):
        self._overlay = overlay
        self._hwnd = hwnd

        self.root = tk.Toplevel(master)
        self.root.title("Posição do chat")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        # Inicializa com posição atual (usa altura da janela do GTA se disponível)
        rect = get_window_rect(hwnd)
        h = rect[3] if rect else 600
        x, y = overlay.get_position(h)

        self._x = tk.IntVar(value=x)
        self._y = tk.IntVar(value=y)

        self._build_ui()
        self.root.grab_set()

    def _build_ui(self) -> None:
        # coordenadas
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

        # d-pad
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

        # fechar
        tk.Button(
            self.root, text="Fechar", command=self.root.destroy,
            bg=BG_PANEL, fg=FG, relief="flat", padx=14, pady=4, cursor="hand2",
        ).pack(pady=(2, 10))

    def _apply(self) -> None:
        self._overlay.set_position(self._x.get(), self._y.get())

    def _move_up(self)    -> None: self._y.set(max(0, self._y.get() - self.STEP)); self._apply()
    def _move_down(self)  -> None: self._y.set(self._y.get() + self.STEP);         self._apply()
    def _move_left(self)  -> None: self._x.set(max(0, self._x.get() - self.STEP)); self._apply()
    def _move_right(self) -> None: self._x.set(self._x.get() + self.STEP);         self._apply()


# ── Diálogo de posição do chat de texto ─────────────────────────────────────

class ChatInputPositionDialog:
    STEP = 5

    def __init__(self, master: tk.Misc, overlay: ChatOverlay, hwnd: int):
        self._overlay = overlay
        self._hwnd = hwnd

        self.root = tk.Toplevel(master)
        self.root.title("Posição do chat de texto")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        rect = get_window_rect(hwnd)
        h = rect[3] if rect else 600
        x, y = overlay.get_input_position(h)

        self._x = tk.IntVar(value=x)
        self._y = tk.IntVar(value=y)

        self._build_ui()
        self.root.grab_set()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
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
            self.root, text="Fechar", command=self._on_close,
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


# ── Diálogo de estilo do chat ────────────────────────────────────────────────

FONTS_AVAILABLE = ["Arial", "Consolas", "Courier New", "Impact", "Segoe UI",
                   "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana"]

class ChatStyleDialog:
    def __init__(self, master: tk.Misc, overlay: ChatOverlay):
        self._overlay = overlay

        self.root = tk.Toplevel(master)
        self.root.title("Editar chat")
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

        # fonte
        row_font = tk.Frame(self.root, bg=BG, **pad)
        row_font.pack(fill="x")
        tk.Label(row_font, text="Fonte:", bg=BG, fg=FG, font=FONT_UI, width=8, anchor="w").pack(side="left")
        font_menu = tk.OptionMenu(row_font, self._font_var, *FONTS_AVAILABLE)
        font_menu.config(bg=BG_PANEL, fg=FG, relief="flat", activebackground=ACCENT,
                         activeforeground="#000", highlightthickness=0)
        font_menu["menu"].config(bg=BG_PANEL, fg=FG)
        font_menu.pack(side="left", fill="x", expand=True)

        # tamanho
        row_size = tk.Frame(self.root, bg=BG, **pad)
        row_size.pack(fill="x")
        tk.Label(row_size, text="Tamanho:", bg=BG, fg=FG, font=FONT_UI, width=8, anchor="w").pack(side="left")
        tk.Spinbox(
            row_size, from_=6, to=48, textvariable=self._size_var, width=5,
            bg=BG_PANEL, fg=FG, relief="flat", buttonbackground=BG_PANEL,
        ).pack(side="left")

        # quantidade de linhas
        row_lines = tk.Frame(self.root, bg=BG, **pad)
        row_lines.pack(fill="x")
        tk.Label(row_lines, text="Linhas:", bg=BG, fg=FG, font=FONT_UI, width=8, anchor="w").pack(side="left")
        tk.Spinbox(
            row_lines, from_=1, to=30, textvariable=self._lines_var, width=5,
            bg=BG_PANEL, fg=FG, relief="flat", buttonbackground=BG_PANEL,
        ).pack(side="left")

        # cor
        row_color = tk.Frame(self.root, bg=BG, **pad)
        row_color.pack(fill="x")
        tk.Label(row_color, text="Cor (hex):", bg=BG, fg=FG, font=FONT_UI, width=8, anchor="w").pack(side="left")
        color_entry = tk.Entry(row_color, textvariable=self._color_var, width=10,
                               bg=BG_PANEL, fg=FG, relief="flat", insertbackground=FG)
        color_entry.pack(side="left")
        self._color_preview = tk.Label(row_color, text="  ██  ", bg=BG, fg=self._color_var.get(),
                                       font=("Segoe UI", 11))
        self._color_preview.pack(side="left", padx=(8, 0))
        self._color_var.trace_add("write", self._update_preview)

        # botões
        btn_row = tk.Frame(self.root, bg=BG, padx=14, pady=8)
        btn_row.pack(fill="x")
        tk.Button(
            btn_row, text="Aplicar", command=self._apply,
            bg=ACCENT, fg="#000", relief="flat", padx=12, pady=4, cursor="hand2",
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            btn_row, text="Fechar", command=self.root.destroy,
            bg=BG_PANEL, fg=FG, relief="flat", padx=12, pady=4, cursor="hand2",
        ).pack(side="left")

    def _update_preview(self, *_) -> None:
        try:
            color = self._color_var.get()
            self._color_preview.config(fg=color)
        except Exception:
            pass

    def _apply(self) -> None:
        try:
            color = self._color_var.get()
            self.root.winfo_rgb(color)  # valida a cor antes de aplicar
        except Exception:
            return
        self._overlay.set_style(self._font_var.get(), self._size_var.get(), color)
        self._overlay.set_max_messages(self._lines_var.get())


# ── Diálogo de filtros ────────────────────────────────────────────────────────

class AddFilterDialog:
    """Janela pequena para criar um filtro personalizado."""

    def __init__(self, master: tk.Misc, on_confirm):
        self._on_confirm = on_confirm

        self.root = tk.Toplevel(master)
        self.root.title("Adicionar filtro")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        self._build_ui()
        self.root.grab_set()

    def _build_ui(self) -> None:
        pad = dict(padx=14, pady=6)

        tk.Label(self.root, text="Adicionar filtro", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold"), padx=14, pady=10).pack(anchor="w")

        row_name = tk.Frame(self.root, bg=BG, **pad)
        row_name.pack(fill="x")
        tk.Label(row_name, text="Nome:", bg=BG, fg=FG, font=FONT_UI, width=12, anchor="w").pack(side="left")
        self._name_var = tk.StringVar()
        tk.Entry(row_name, textvariable=self._name_var, width=22,
                 bg=BG_PANEL, fg=FG, relief="flat", insertbackground=FG).pack(side="left")

        row_kw = tk.Frame(self.root, bg=BG, **pad)
        row_kw.pack(fill="x")
        tk.Label(row_kw, text="Palavra-chave:", bg=BG, fg=FG, font=FONT_UI, width=12, anchor="w").pack(side="left")
        self._kw_var = tk.StringVar()
        tk.Entry(row_kw, textvariable=self._kw_var, width=22,
                 bg=BG_PANEL, fg=FG, relief="flat", insertbackground=FG).pack(side="left")

        btn_row = tk.Frame(self.root, bg=BG, padx=14, pady=8)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Adicionar", command=self._confirm,
                  bg=ACCENT, fg="#000", relief="flat", padx=12, pady=4, cursor="hand2").pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Cancelar", command=self.root.destroy,
                  bg=BG_PANEL, fg=FG, relief="flat", padx=12, pady=4, cursor="hand2").pack(side="left")

    def _confirm(self) -> None:
        name = self._name_var.get().strip()
        keyword = self._kw_var.get().strip()
        if name and keyword:
            self._on_confirm(name, keyword)
            self.root.destroy()


class FiltersDialog:
    """Janela para ativar/desativar filtros e configurar 'Me ignorar'."""

    def __init__(self, master: tk.Misc, filters: list[dict], overlay: ChatOverlay | None,
                 ignore_self: dict, on_filter_change):
        self._filters          = filters
        self._overlay          = overlay
        self._ignore_self      = ignore_self
        self._on_filter_change = on_filter_change

        self.root = tk.Toplevel(master)
        self.root.title("Filtros")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        self._build_ui()

    def _build_ui(self) -> None:
        tk.Label(
            self.root, text="Filtros de chat", bg=BG, fg=ACCENT,
            font=("Segoe UI", 11, "bold"), padx=14, pady=10,
        ).pack(anchor="w")

        tk.Label(
            self.root,
            text="Filtros ativos mostram apenas mensagens com a palavra-chave.",
            bg=BG, fg=FG_DIM, font=("Segoe UI", 8), padx=14,
        ).pack(anchor="w")

        tk.Frame(self.root, bg=BG_PANEL, height=1).pack(fill="x", padx=14, pady=6)

        # ── Me ignorar ──
        ignore_frame = tk.Frame(self.root, bg=BG_PANEL, padx=10, pady=8)
        ignore_frame.pack(fill="x", padx=14, pady=(0, 6))

        tk.Checkbutton(
            ignore_frame, text="Me ignorar",
            variable=self._ignore_self["var"],
            command=self._on_ignore_toggle,
            bg=BG_PANEL, fg=FG, selectcolor=BG,
            activebackground=BG_PANEL, activeforeground=ACCENT,
            font=FONT_UI, cursor="hand2",
        ).pack(side="left")

        self._lbl_ignore_name = tk.Label(
            ignore_frame,
            text=self._ignore_self["name"] or "nenhum nome definido",
            bg=BG_PANEL,
            fg=ACCENT if self._ignore_self["name"] else FG_DIM,
            font=("Segoe UI", 8),
        )
        self._lbl_ignore_name.pack(side="right")

        tk.Frame(self.root, bg=BG_PANEL, height=1).pack(fill="x", padx=14, pady=6)

        self._list_frame = tk.Frame(self.root, bg=BG, padx=14, pady=4)
        self._list_frame.pack(fill="x")
        self._render_filters()

        tk.Frame(self.root, bg=BG_PANEL, height=1).pack(fill="x", padx=14, pady=6)

        bottom = tk.Frame(self.root, bg=BG, padx=14, pady=4)
        bottom.pack(fill="x")
        tk.Button(bottom, text="+ Adicionar filtro", command=self._open_add_filter,
                  bg=BG_PANEL, fg=ACCENT, relief="flat", padx=10, pady=4, cursor="hand2",
                  activebackground=ACCENT, activeforeground="#000").pack(side="left")
        tk.Button(bottom, text="Fechar", command=self.root.destroy,
                  bg=BG_PANEL, fg=FG, relief="flat", padx=14, pady=4, cursor="hand2").pack(side="right")

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

        for f in self._filters:
            row = tk.Frame(self._list_frame, bg=BG_PANEL, padx=10, pady=8)
            row.pack(fill="x", pady=(0, 6))

            f["var"].trace_add("write", lambda *_, v=f["var"]: self._on_toggle())

            tk.Checkbutton(
                row, text=f["name"],
                variable=f["var"],
                bg=BG_PANEL, fg=FG, selectcolor=BG,
                activebackground=BG_PANEL, activeforeground=ACCENT,
                font=FONT_UI, cursor="hand2",
            ).pack(side="left")

            tk.Label(
                row, text=f'"{f["keyword"]}"',
                bg=BG_PANEL, fg=FG_DIM, font=("Segoe UI", 8),
            ).pack(side="right")

        if not self._filters:
            tk.Label(self._list_frame, text="Nenhum filtro criado ainda.",
                     bg=BG, fg=FG_DIM, font=FONT_UI).pack(pady=6)

    def _on_toggle(self) -> None:
        if self._overlay:
            self._overlay.clear_messages()

    def _open_add_filter(self) -> None:
        AddFilterDialog(master=self.root, on_confirm=self._add_filter)

    def _add_filter(self, name: str, keyword: str) -> None:
        self._filters.append({
            "name": name,
            "keyword": keyword,
            "var": tk.BooleanVar(value=False),
        })
        self._render_filters()
        if self._overlay:
            self._overlay.clear_messages()


# ── Diálogo de nome do jogador ────────────────────────────────────────────────

class PlayerNameDialog:
    """Janela para o usuário inserir seu nome no jogo."""

    def __init__(self, master: tk.Misc, current_name: str, on_confirm, on_cancel):
        self._on_confirm = on_confirm
        self._on_cancel  = on_cancel

        self.root = tk.Toplevel(master)
        self.root.title("Adicione seu nome no jogo")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)

        self._name_var = tk.StringVar(value=current_name)
        self._build_ui()
        self.root.grab_set()

    def _build_ui(self) -> None:
        tk.Label(self.root, text="Adicione seu nome no jogo", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold"), padx=14, pady=10).pack(anchor="w")

        tk.Label(self.root,
                 text="Mensagens que começarem com este nome serão ignoradas.",
                 bg=BG, fg=FG_DIM, font=("Segoe UI", 8), padx=14).pack(anchor="w")

        row = tk.Frame(self.root, bg=BG, padx=14, pady=10)
        row.pack(fill="x")
        tk.Label(row, text="Nome:", bg=BG, fg=FG, font=FONT_UI).pack(side="left", padx=(0, 6))
        tk.Entry(row, textvariable=self._name_var, width=26,
                 bg=BG_PANEL, fg=FG, relief="flat", insertbackground=FG).pack(side="left")

        btn_row = tk.Frame(self.root, bg=BG, padx=14, pady=6)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Confirmar", command=self._confirm,
                  bg=ACCENT, fg="#000", relief="flat", padx=12, pady=4, cursor="hand2").pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Cancelar", command=self._cancel,
                  bg=BG_PANEL, fg=FG, relief="flat", padx=12, pady=4, cursor="hand2").pack(side="left")

    def _confirm(self) -> None:
        name = self._name_var.get().strip()
        if name:
            self._on_confirm(name)
            self.root.destroy()

    def _cancel(self) -> None:
        self._on_cancel()
        self.root.destroy()


# ── Menu Chat ────────────────────────────────────────────────────────────────

class ChatMenuDialog:
    """Janela de opções do chat — posição e estilo."""

    def __init__(self, master: tk.Misc, overlay: ChatOverlay, hwnd: int):
        self._overlay = overlay
        self._hwnd    = hwnd

        self.root = tk.Toplevel(master)
        self.root.title("Customizar chat")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        self._build_ui()

    def _build_ui(self) -> None:
        tk.Label(self.root, text="Customizar chat", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold"), padx=14, pady=10).pack(anchor="w")

        btn_frame = tk.Frame(self.root, bg=BG, padx=14, pady=4)
        btn_frame.pack(fill="x")

        _btn = dict(relief="flat", padx=8, pady=6, cursor="hand2",
                    activebackground=ACCENT, activeforeground="#000")

        tk.Button(btn_frame, text="Posição do chat", command=self._open_position,
                  bg=BG_PANEL, fg=FG, **_btn).pack(fill="x")

        tk.Button(btn_frame, text="Posição chat de texto", command=self._open_input_position,
                  bg=BG_PANEL, fg=FG, **_btn).pack(fill="x", pady=(6, 0))

        tk.Button(btn_frame, text="Editar chat", command=self._open_style,
                  bg=BG_PANEL, fg=FG, **_btn).pack(fill="x", pady=(6, 12))

    def _open_position(self) -> None:
        ChatPositionDialog(master=self.root, overlay=self._overlay, hwnd=self._hwnd)

    def _open_input_position(self) -> None:
        ChatInputPositionDialog(master=self.root, overlay=self._overlay, hwnd=self._hwnd)

    def _open_style(self) -> None:
        ChatStyleDialog(master=self.root, overlay=self._overlay)


# ── Diálogo de tradução ───────────────────────────────────────────────────────

class TranslationDialog:
    """Janela de configuração da tradução do chat."""

    def __init__(self, master: tk.Misc, translation: dict,
                 check_fn, download_fn):
        self._translation = translation
        self._check_fn    = check_fn
        self._download_fn = download_fn

        self.root = tk.Toplevel(master)
        self.root.title("Tradução")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        self._build_ui()
        self._refresh_status()
        self._refresh_user_status()

    def _build_ui(self) -> None:
        tk.Label(self.root, text="Tradução", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 11, "bold"), padx=14, pady=10).pack(anchor="w")

        langs = list(LANGUAGES.keys())

        # ── Chat Servidor ──
        self._build_section(
            title="Chat Servidor",
            enabled_var=self._translation["enabled"],
            source_var=self._translation["source"],
            target_var=self._translation["target"],
            langs=langs,
            on_change=self._refresh_status,
        )

        # ── Chat Usuário ──
        self._build_section(
            title="Chat Usuário",
            enabled_var=self._translation["user_enabled"],
            source_var=self._translation["user_source"],
            target_var=self._translation["user_target"],
            langs=langs,
            on_change=self._refresh_user_status,
        )

        # ── Pacotes offline ──
        pkg_frame = tk.Frame(self.root, bg=BG_PANEL, padx=12, pady=10)
        pkg_frame.pack(fill="x", padx=14, pady=(0, 10))

        tk.Label(pkg_frame, text="Pacotes offline", bg=BG_PANEL, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Frame(pkg_frame, bg="#2a2a4a", height=1).pack(fill="x", pady=(4, 8))

        tk.Label(pkg_frame, text="Chat Servidor:", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self._lbl_pkg_status = tk.Label(pkg_frame, text="Verificando…",
                                        bg=BG_PANEL, fg=FG_DIM, font=FONT_UI)
        self._lbl_pkg_status.pack(anchor="w")
        self._btn_download = tk.Button(
            pkg_frame, text="Baixar pacotes",
            command=self._download_packages,
            bg=BG_PANEL, fg=ACCENT, relief="flat", padx=10, pady=4,
            activebackground=ACCENT, activeforeground="#000", cursor="hand2",
        )
        self._btn_download.pack(anchor="w", pady=(4, 10))

        tk.Label(pkg_frame, text="Chat Usuário:", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self._lbl_user_pkg_status = tk.Label(pkg_frame, text="Verificando…",
                                             bg=BG_PANEL, fg=FG_DIM, font=FONT_UI)
        self._lbl_user_pkg_status.pack(anchor="w")
        self._btn_user_download = tk.Button(
            pkg_frame, text="Baixar pacotes",
            command=self._download_user_packages,
            bg=BG_PANEL, fg=ACCENT, relief="flat", padx=10, pady=4,
            activebackground=ACCENT, activeforeground="#000", cursor="hand2",
        )
        self._btn_user_download.pack(anchor="w", pady=(4, 0))

        tk.Button(self.root, text="Fechar", command=self.root.destroy,
                  bg=BG_PANEL, fg=FG, relief="flat", padx=14, pady=4,
                  cursor="hand2").pack(pady=(6, 10))

    def _build_section(self, title: str, enabled_var, source_var, target_var,
                       langs: list, on_change) -> None:
        section = tk.Frame(self.root, bg=BG_PANEL, padx=12, pady=10)
        section.pack(fill="x", padx=14, pady=(0, 8))

        header = tk.Frame(section, bg=BG_PANEL)
        header.pack(fill="x")
        tk.Label(header, text=title, bg=BG_PANEL, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Checkbutton(
            header, text="Habilitar",
            variable=enabled_var,
            bg=BG_PANEL, fg=FG, selectcolor=BG,
            activebackground=BG_PANEL, activeforeground=ACCENT,
            font=FONT_UI, cursor="hand2",
        ).pack(side="right")

        tk.Frame(section, bg="#2a2a4a", height=1).pack(fill="x", pady=(6, 8))

        row_in = tk.Frame(section, bg=BG_PANEL)
        row_in.pack(fill="x", pady=(0, 6))
        tk.Label(row_in, text="Entrada:", bg=BG_PANEL, fg=FG_DIM,
                 font=FONT_UI, width=8, anchor="w").pack(side="left")
        om_in = tk.OptionMenu(row_in, source_var, *langs, command=lambda _: on_change())
        om_in.config(bg=BG, fg=FG, relief="flat", activebackground=ACCENT,
                     activeforeground="#000", highlightthickness=0, width=12)
        om_in["menu"].config(bg=BG, fg=FG)
        om_in.pack(side="left")

        row_out = tk.Frame(section, bg=BG_PANEL)
        row_out.pack(fill="x")
        tk.Label(row_out, text="Saída:", bg=BG_PANEL, fg=FG_DIM,
                 font=FONT_UI, width=8, anchor="w").pack(side="left")
        om_out = tk.OptionMenu(row_out, target_var, *langs, command=lambda _: on_change())
        om_out.config(bg=BG, fg=FG, relief="flat", activebackground=ACCENT,
                      activeforeground="#000", highlightthickness=0, width=12)
        om_out["menu"].config(bg=BG, fg=FG)
        om_out.pack(side="left")

    def _current_codes(self) -> tuple[str, str]:
        return (LANGUAGES.get(self._translation["source"].get(), "es"),
                LANGUAGES.get(self._translation["target"].get(), "pt"))

    def _current_user_codes(self) -> tuple[str, str]:
        return (LANGUAGES.get(self._translation["user_source"].get(), "pt"),
                LANGUAGES.get(self._translation["user_target"].get(), "es"))

    def _refresh_status(self) -> None:
        src, tgt = self._current_codes()
        installed = self._check_fn(src, tgt)
        if installed:
            self._lbl_pkg_status.config(text="✓ Instalados — tradução offline pronta", fg=ACCENT)
            self._btn_download.config(state="disabled", fg=FG_DIM)
        else:
            self._lbl_pkg_status.config(text="✗ Pacotes não instalados", fg="#ff6666")
            self._btn_download.config(state="normal", fg=ACCENT)

    def _refresh_user_status(self) -> None:
        src, tgt = self._current_user_codes()
        installed = self._check_fn(src, tgt)
        if installed:
            self._lbl_user_pkg_status.config(text="✓ Instalados — tradução offline pronta", fg=ACCENT)
            self._btn_user_download.config(state="disabled", fg=FG_DIM)
        else:
            self._lbl_user_pkg_status.config(text="✗ Pacotes não instalados", fg="#ff6666")
            self._btn_user_download.config(state="normal", fg=ACCENT)

    def _download_packages(self) -> None:
        src, tgt = self._current_codes()
        self._btn_download.config(state="disabled", text="Baixando…", fg=FG_DIM)
        self._lbl_pkg_status.config(text="Baixando pacotes (necessário internet)…", fg=FG_DIM)

        def on_done(ok: bool):
            if ok:
                self._lbl_pkg_status.config(text="✓ Instalado com sucesso!", fg=ACCENT)
                self._btn_download.config(text="Baixar pacotes", state="disabled", fg=FG_DIM)
            else:
                self._lbl_pkg_status.config(text="✗ Falha no download.", fg="#ff6666")
                self._btn_download.config(text="Baixar pacotes", state="normal", fg=ACCENT)

        self._download_fn(src, tgt, on_done)

    def _download_user_packages(self) -> None:
        src, tgt = self._current_user_codes()
        self._btn_user_download.config(state="disabled", text="Baixando…", fg=FG_DIM)
        self._lbl_user_pkg_status.config(text="Baixando pacotes (necessário internet)…", fg=FG_DIM)

        def on_done(ok: bool):
            if ok:
                self._lbl_user_pkg_status.config(text="✓ Instalado com sucesso!", fg=ACCENT)
                self._btn_user_download.config(text="Baixar pacotes", state="disabled", fg=FG_DIM)
            else:
                self._lbl_user_pkg_status.config(text="✗ Falha no download.", fg="#ff6666")
                self._btn_user_download.config(text="Baixar pacotes", state="normal", fg=ACCENT)

        self._download_fn(src, tgt, on_done)


# ── Painel de controle (menu externo) ────────────────────────────────────────

class ControlPanel:
    def __init__(self):
        self._hwnd: int = 0
        self._raw_queue:     queue.Queue = queue.Queue()  # reader → translator
        self._display_queue: queue.Queue = queue.Queue()  # translator → UI
        self._reader = SampChatReader()
        self._reader_thread:      threading.Thread | None = None
        self._translator_thread:  threading.Thread | None = None
        self._overlay: ChatOverlay | None = None

        self.root = tk.Tk()
        self.root.title("SAMP Translate")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Filtros — cada entrada: name, keyword, var (BooleanVar)
        self._filters: list[dict] = [
            {"name": "Jogadores próximos", "keyword": "dice", "var": tk.BooleanVar(value=False)},
        ]
        self._ignore_self: dict = {"var": tk.BooleanVar(value=False), "name": ""}
        self._translation: dict = {
            "enabled":      tk.BooleanVar(value=False),
            "source":       tk.StringVar(value="Espanhol"),
            "target":       tk.StringVar(value="Português"),
            "user_enabled": tk.BooleanVar(value=False),
            "user_source":  tk.StringVar(value="Português"),
            "user_target":  tk.StringVar(value="Espanhol"),
        }
        self._argos_cache: dict = {}  # (src_code, tgt_code) → translator object
        self._translation["enabled"].trace_add("write", self._on_translation_toggle)
        self._translation["user_enabled"].trace_add("write", self._on_translation_toggle)
        self._kb_hook = None

        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # cabeçalho
        header = tk.Frame(self.root, bg=BG, padx=12, pady=8)
        header.pack(fill="x")

        tk.Label(
            header, text="SAMP Translate",
            bg=BG, fg=ACCENT, font=("Segoe UI", 12, "bold"),
        ).pack(side="left")

        # painel de status
        status = tk.Frame(self.root, bg=BG_PANEL, padx=12, pady=8)
        status.pack(fill="x", padx=10, pady=(0, 6))

        self._dot = tk.Label(
            status, text="●", bg=BG_PANEL, fg="red", font=("Segoe UI", 10),
        )
        self._dot.grid(row=0, column=0, padx=(0, 5))

        self._lbl_status = tk.Label(
            status, text="Desconectado", bg=BG_PANEL, fg=FG, font=FONT_UI,
        )
        self._lbl_status.grid(row=0, column=1, sticky="w")

        self._lbl_window = tk.Label(
            status, text="Nenhuma janela selecionada",
            bg=BG_PANEL, fg=FG_DIM, font=FONT_UI,
        )
        self._lbl_window.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # botão de seleção
        btn_frame = tk.Frame(self.root, bg=BG, padx=10, pady=8)
        btn_frame.pack(fill="x")

        tk.Button(
            btn_frame, text="Selecionar janela GTA",
            command=self._select_window,
            bg=BG_PANEL, fg=FG, relief="flat", padx=8, pady=6,
            activebackground=ACCENT, activeforeground="#000",
            cursor="hand2",
        ).pack(fill="x")

        tk.Button(
            btn_frame, text="Customizar chat",
            command=self._open_chat_menu,
            bg=BG_PANEL, fg=FG_DIM, relief="flat", padx=8, pady=4,
            activebackground=ACCENT, activeforeground="#000",
            cursor="hand2",
        ).pack(fill="x", pady=(4, 0))

        tk.Button(
            btn_frame, text="Tradução",
            command=self._open_translation_dialog,
            bg=BG_PANEL, fg=FG_DIM, relief="flat", padx=8, pady=4,
            activebackground=ACCENT, activeforeground="#000",
            cursor="hand2",
        ).pack(fill="x", pady=(4, 0))

        tk.Button(
            btn_frame, text="Filtros",
            command=self._open_filters_dialog,
            bg=BG_PANEL, fg=FG_DIM, relief="flat", padx=8, pady=4,
            activebackground=ACCENT, activeforeground="#000",
            cursor="hand2",
        ).pack(fill="x", pady=(4, 0))

        tk.Button(
            btn_frame, text="Limpar chat",
            command=self._clear_chat,
            bg=BG_PANEL, fg=FG_DIM, relief="flat", padx=8, pady=4,
            activebackground="#ff4444", activeforeground="#fff",
            cursor="hand2",
        ).pack(fill="x", pady=(4, 0))

    # ── Seleção de janela ─────────────────────────────────────────────────────

    def _select_window(self) -> None:
        dialog = SelectorDialog(master=self.root)
        hwnd = dialog.selected_hwnd
        if hwnd is None:
            return

        self._hwnd = hwnd
        title = win32gui.GetWindowText(hwnd)
        self._lbl_window.config(text=title or f"HWND={hwnd}")

        self._start_overlay(hwnd)
        self._start_reader()

    # ── Overlay ───────────────────────────────────────────────────────────────

    def _start_overlay(self, hwnd: int) -> None:
        if self._overlay is not None:
            self._overlay.stop()
        self._overlay = ChatOverlay(hwnd, master=self.root)
        self._setup_y_hook()

    # ── Leitor de chat (thread) ───────────────────────────────────────────────

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
                    self._raw_queue.put(msg)   # só leitura — sem tradução aqui
                time.sleep(POLL_INTERVAL_MS / 1000)
            except Exception:
                time.sleep(1.0)

    def _translation_loop(self) -> None:
        """Thread dedicado à tradução — nunca bloqueia o reader."""
        while True:
            try:
                msg = self._raw_queue.get()
                translated = self._try_translate(msg)
                self._display_queue.put((msg, translated))  # (original, traduzido)
            except Exception:
                pass

    def _try_translate(self, msg):
        if not self._translation["enabled"].get():
            return msg
        src = LANGUAGES.get(self._translation["source"].get(), "es")
        tgt = LANGUAGES.get(self._translation["target"].get(), "pt")
        if src == tgt:
            return msg
        translator = self._argos_cache.get((src, tgt))
        if not translator:
            return msg
        try:
            result = translator.translate(msg.text)
            return dataclasses.replace(msg, text=result or msg.text)
        except Exception:
            return msg

    def _prewarm_translator(self, src: str, tgt: str) -> None:
        """Carrega o modelo argostranslate em thread separada para não bloquear a fila."""
        key = (src, tgt)
        if key in self._argos_cache:
            return

        def _worker():
            try:
                import argostranslate.translate as at
                installed = at.get_installed_languages()
                from_lang = next((l for l in installed if l.code == src), None)
                to_lang   = next((l for l in installed if l.code == tgt), None)
                if from_lang and to_lang:
                    t = from_lang.get_translation(to_lang)
                    if t:
                        self._argos_cache[key] = t
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="argos-prewarm").start()

    def _on_translation_toggle(self, *_) -> None:
        if self._translation["enabled"].get():
            while not self._raw_queue.empty():
                try:
                    self._raw_queue.get_nowait()
                except queue.Empty:
                    break
            src = LANGUAGES.get(self._translation["source"].get(), "es")
            tgt = LANGUAGES.get(self._translation["target"].get(), "pt")
            self._prewarm_translator(src, tgt)
        if self._translation["user_enabled"].get():
            u_src = LANGUAGES.get(self._translation["user_source"].get(), "pt")
            u_tgt = LANGUAGES.get(self._translation["user_target"].get(), "es")
            self._prewarm_translator(u_src, u_tgt)

    def _check_argos_installed(self, src: str, tgt: str) -> bool:
        try:
            import argostranslate.translate as at
            installed = at.get_installed_languages()
            from_lang = next((l for l in installed if l.code == src), None)
            to_lang   = next((l for l in installed if l.code == tgt), None)
            if not from_lang or not to_lang:
                return False
            return from_lang.get_translation(to_lang) is not None
        except Exception:
            return False

    def _download_argos_packages(self, src: str, tgt: str, on_done) -> None:
        def _worker():
            try:
                import argostranslate.package as ap
                ap.update_package_index()
                available = ap.get_available_packages()
                pkg = next((p for p in available
                            if p.from_code == src and p.to_code == tgt), None)
                if pkg:
                    ap.install_from_path(pkg.download())
                    self._argos_cache.pop((src, tgt), None)
                    self._prewarm_translator(src, tgt)
                    self.root.after(0, lambda: on_done(True))
                else:
                    self.root.after(0, lambda: on_done(False))
            except Exception:
                self.root.after(0, lambda: on_done(False))
        threading.Thread(target=_worker, daemon=True, name="argos-download").start()

    def _open_translation_dialog(self) -> None:
        TranslationDialog(
            master=self.root,
            translation=self._translation,
            check_fn=self._check_argos_installed,
            download_fn=self._download_argos_packages,
        )

    def _set_connected(self) -> None:
        self._dot.config(fg=ACCENT)
        self._lbl_status.config(text="Conectado", fg=ACCENT)

    def _set_disconnected(self) -> None:
        self._dot.config(fg="red")
        self._lbl_status.config(text="Desconectado", fg="red")

    def _clear_chat(self) -> None:
        if self._overlay is not None:
            self._overlay.clear_messages()

    # ── Drenagem da fila → overlay ────────────────────────────────────────────

    def _drain_queue(self) -> None:
        active = [f for f in self._filters if f["var"].get()]
        ignore_name = self._ignore_self["name"].lower() if self._ignore_self["var"].get() else ""
        while not self._display_queue.empty():
            original, translated = self._display_queue.get_nowait()
            # Filtros sempre aplicados no texto original (antes da tradução)
            if active and not any(f["keyword"].lower() in original.text.lower() for f in active):
                continue
            if ignore_name and original.text.lower().startswith(ignore_name):
                continue
            if self._overlay:
                self._overlay.add_message(translated.text)
        self.root.after(POLL_INTERVAL_MS, self._drain_queue)

    # ── Menu Chat ─────────────────────────────────────────────────────────────

    def _open_chat_menu(self) -> None:
        if self._overlay is None:
            return
        ChatMenuDialog(master=self.root, overlay=self._overlay, hwnd=self._hwnd)

    # ── Filtros ───────────────────────────────────────────────────────────────

    def _open_filters_dialog(self) -> None:
        FiltersDialog(master=self.root, filters=self._filters, overlay=self._overlay,
                      ignore_self=self._ignore_self, on_filter_change=self._clear_chat)

    # ── Envio de mensagem (tecla Y) ───────────────────────────────────────────

    def _setup_y_hook(self) -> None:
        """Hook global: intercepta Y quando GTA está em foco."""
        try:
            import keyboard

            if self._kb_hook is not None:
                try:
                    keyboard.unhook(self._kb_hook)
                except Exception:
                    pass

            def _handler(event):
                try:
                    if win32gui.GetForegroundWindow() == self._hwnd:
                        self.root.after(0, self._show_send_input)
                except Exception:
                    pass

            # on_press_key('y') suprime APENAS o Y — não bloqueia todas as teclas do jogo
            self._kb_hook = keyboard.on_press_key('y', _handler, suppress=True)
        except ImportError:
            pass

    def _show_send_input(self) -> None:
        if self._overlay is None:
            return
        self._overlay.show_input(on_submit=self._on_send_submit)

    def _on_send_submit(self, text: str) -> None:
        """Traduz (usando config do Chat Usuário) e envia ao chat do SA-MP."""
        def _worker():
            result = text
            if self._translation["user_enabled"].get():
                src = LANGUAGES.get(self._translation["user_source"].get(), "pt")
                tgt = LANGUAGES.get(self._translation["user_target"].get(), "es")
                if src != tgt:
                    translator = self._argos_cache.get((src, tgt))
                    if translator:
                        try:
                            result = translator.translate(text) or text
                        except Exception:
                            pass
            self._send_to_samp(result)

        threading.Thread(target=_worker, daemon=True, name="samp-send").start()

    def _send_to_samp(self, text: str) -> None:
        """Copia texto para o clipboard e envia via chat do SA-MP (T → Ctrl+V → Enter)."""
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

    # ── Encerramento ──────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        if self._kb_hook is not None:
            try:
                import keyboard
                keyboard.unhook(self._kb_hook)
            except Exception:
                pass
        if self._overlay:
            self._overlay.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    app = ControlPanel()
    app.run()


if __name__ == "__main__":
    main()