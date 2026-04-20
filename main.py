"""
SAMP Translate — ponto de entrada principal.

Fluxo:
    1. Abre SelectorDialog → usuário escolhe a janela do GTA/SA-MP
    2. Conecta o SampChatReader ao processo gta_sa.exe
    3. Exibe o painel principal (chat log + status)
    4. Inicia o WindowOverlay sobre a janela selecionada
    5. Thread de fundo lê o chat e envia mensagens para a fila
    6. Loop tkinter drena a fila e atualiza a UI
"""

import os
import sys
import queue
import threading
import time

# ── fix Tcl/Tk para venvs ───────────────────────────────────────────────────
def _fix_tcl_paths() -> None:
    base = getattr(sys, "base_prefix", sys.prefix)
    tcl = os.path.join(base, "tcl", "tcl8.6")
    tk_ = os.path.join(base, "tcl", "tk8.6")
    if os.path.isdir(tcl):
        os.environ.setdefault("TCL_LIBRARY", tcl)
        os.environ.setdefault("TK_LIBRARY", tk_)

_fix_tcl_paths()

import tkinter as tk
from tkinter import ttk
import win32gui

from window_overlay import SelectorDialog, WindowOverlay
from samp_chat import SampChatReader, ChatMessage

# ── Paleta / UI ─────────────────────────────────────────────────────────────

BG        = "#1a1a2e"
BG_PANEL  = "#16213e"
BG_LOG    = "#0f3460"
FG        = "#e0e0e0"
FG_DIM    = "#888888"
ACCENT    = "#00FF00"
FONT_UI   = ("Segoe UI", 9)
FONT_LOG  = ("Consolas", 9)

POLL_INTERVAL_MS = 200   # frequência de drenagem da fila na UI


# ── App principal ────────────────────────────────────────────────────────────

class App:
    def __init__(self):
        self._hwnd: int = 0
        self._chat_queue: queue.Queue[ChatMessage | None] = queue.Queue()
        self._reader = SampChatReader()
        self._reader_thread: threading.Thread | None = None
        self._overlay: WindowOverlay | None = None

        self.root = tk.Tk()
        self.root.title("SAMP Translate")
        self.root.configure(bg=BG)
        self.root.minsize(600, 400)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()

    # ── Construção da UI ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── cabeçalho ──
        header = tk.Frame(self.root, bg=BG, pady=6)
        header.pack(fill="x", padx=10)

        tk.Label(
            header, text="SAMP Translate",
            bg=BG, fg=ACCENT, font=("Segoe UI", 13, "bold"),
        ).pack(side="left")

        self._btn_overlay = tk.Button(
            header, text="Selecionar janela",
            command=self._select_window,
            bg=BG_PANEL, fg=FG, relief="flat", padx=8, pady=3,
            activebackground=ACCENT, activeforeground="#000",
            cursor="hand2",
        )
        self._btn_overlay.pack(side="right")

        # ── status ──
        status_frame = tk.Frame(self.root, bg=BG_PANEL, padx=10, pady=5)
        status_frame.pack(fill="x", padx=10, pady=(0, 4))

        tk.Label(status_frame, text="Janela:", bg=BG_PANEL, fg=FG_DIM, font=FONT_UI).grid(
            row=0, column=0, sticky="w")
        self._lbl_window = tk.Label(status_frame, text="—", bg=BG_PANEL, fg=FG, font=FONT_UI)
        self._lbl_window.grid(row=0, column=1, sticky="w", padx=(4, 20))

        tk.Label(status_frame, text="SA-MP:", bg=BG_PANEL, fg=FG_DIM, font=FONT_UI).grid(
            row=0, column=2, sticky="w")
        self._lbl_samp = tk.Label(status_frame, text="Desconectado", bg=BG_PANEL, fg="red", font=FONT_UI)
        self._lbl_samp.grid(row=0, column=3, sticky="w", padx=4)

        # ── log de chat ──
        log_frame = tk.Frame(self.root, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        tk.Label(log_frame, text="Chat", bg=BG, fg=FG_DIM, font=FONT_UI).pack(anchor="w")

        text_frame = tk.Frame(log_frame, bg=BG_LOG)
        text_frame.pack(fill="both", expand=True)

        self._log = tk.Text(
            text_frame,
            bg=BG_LOG, fg=FG,
            font=FONT_LOG,
            state="disabled",
            wrap="word",
            relief="flat",
            padx=6, pady=4,
        )
        sb = ttk.Scrollbar(text_frame, orient="vertical", command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        self._log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # tags de cor para o log
        self._log.tag_config("prefix", foreground="#f0c040", font=(*FONT_LOG, "bold"))
        self._log.tag_config("info",   foreground="#80c8ff")
        self._log.tag_config("dim",    foreground=FG_DIM)

        # ── rodapé ──
        footer = tk.Frame(self.root, bg=BG, pady=4)
        footer.pack(fill="x", padx=10)

        self._btn_clear = tk.Button(
            footer, text="Limpar",
            command=self._clear_log,
            bg=BG_PANEL, fg=FG, relief="flat", padx=8, pady=2,
            cursor="hand2",
        )
        self._btn_clear.pack(side="right")

        self._lbl_count = tk.Label(footer, text="0 mensagens", bg=BG, fg=FG_DIM, font=FONT_UI)
        self._lbl_count.pack(side="left")

    # ── Seleção de janela ─────────────────────────────────────────────────────

    def _select_window(self) -> None:
        """Abre SelectorDialog; ao confirmar, inicia overlay e leitor de chat."""
        dialog = SelectorDialog(master=self.root)
        hwnd = dialog.selected_hwnd
        if hwnd is None:
            return

        self._hwnd = hwnd
        title = win32gui.GetWindowText(hwnd)
        self._lbl_window.config(text=f"{title}  (HWND={hwnd})")

        self._start_overlay(hwnd)
        self._start_reader()

    # ── Overlay ───────────────────────────────────────────────────────────────

    def _start_overlay(self, hwnd: int) -> None:
        if self._overlay is not None:
            self._overlay.stop()

        self._overlay = WindowOverlay(hwnd, master=self.root)

    # ── Leitor de chat (thread) ───────────────────────────────────────────────

    def _start_reader(self) -> None:
        if self._reader_thread and self._reader_thread.is_alive():
            return

        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="samp-chat-reader"
        )
        self._reader_thread.start()
        self.root.after(POLL_INTERVAL_MS, self._drain_queue)

    def _reader_loop(self) -> None:
        """Roda na thread de fundo: conecta e publica mensagens na fila."""
        # Tenta conectar em loop (o jogador pode abrir o jogo depois)
        while True:
            try:
                self._reader.attach()
                self.root.after(0, lambda: self._lbl_samp.config(
                    text="Conectado", fg=ACCENT))
                break
            except Exception:
                time.sleep(2.0)

        while True:
            try:
                if not self._reader.is_attached():
                    self.root.after(0, lambda: self._lbl_samp.config(
                        text="Desconectado", fg="red"))
                    break

                for msg in self._reader.poll():
                    self._chat_queue.put(msg)

                time.sleep(POLL_INTERVAL_MS / 1000)

            except Exception:
                time.sleep(1.0)

    # ── Drenagem da fila → UI ─────────────────────────────────────────────────

    def _drain_queue(self) -> None:
        """Chamado pelo loop tkinter; move mensagens da fila para o log."""
        while not self._chat_queue.empty():
            msg = self._chat_queue.get_nowait()
            self._append_message(msg)
        self.root.after(POLL_INTERVAL_MS, self._drain_queue)

    def _append_message(self, msg: ChatMessage) -> None:
        self._log.config(state="normal")
        self._log.insert("end", f"{msg.text}\n", "info")
        self._log.see("end")
        self._log.config(state="disabled")

        # atualiza contador
        cur = self._lbl_count.cget("text")
        n = int(cur.split()[0]) + 1
        self._lbl_count.config(text=f"{n} mensagens")

    # ── Utilitários ───────────────────────────────────────────────────────────

    def _clear_log(self) -> None:
        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")
        self._lbl_count.config(text="0 mensagens")

    def _on_close(self) -> None:
        if self._overlay:
            self._overlay.stop()
        self.root.destroy()

    # ── Inicialização ─────────────────────────────────────────────────────────

    def run(self) -> None:
        self.root.mainloop()


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    app = App()
    app.run()


if __name__ == "__main__":
    main()