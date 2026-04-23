"""
Leitor de chat do SA-MP 0.3.DL-R1 via leitura de memória (pymem).

O buffer de chat é heap-alocado, portanto o endereço muda a cada sessão.
Na inicialização, fazemos um scan por assinatura para localizar o array.

Uso:
    reader = SampChatReader()
    reader.attach()          # conecta ao gta_sa.exe e localiza o buffer

    for msg in reader.poll():
        print(msg)           # ChatMessage(text, msg_type, color)

    # ou em loop contínuo:
    reader.run(callback=print)
"""

import ctypes
import ctypes.wintypes
import re
import struct
import time
from dataclasses import dataclass
from typing import Callable, Iterator

import pymem
import pymem.process

# ---------------------------------------------------------------------------
# Layout do struct CChatLine — SA-MP 0.3.DL-R1
# Determinado via análise de memória em 19/04/2026.
#
#   +0x00  DWORD  — sempre 0 (reservado)
#   +0x04  DWORD  — tipo da mensagem (1-8; 0 = slot vazio)
#   +0x08  DWORD  — cor ARGB do texto  (byte alpha sempre 0xFF)
#   +0x0C  DWORD  — sempre 0
#   +0x10  ...    — campos internos (timestamps, ponteiros)
#   +0x30  char[] — texto da mensagem (null-terminated, até ~200 bytes)
# ---------------------------------------------------------------------------

_ENTRY_SIZE : int = 0xFC   # 252 bytes por entrada
_MAX_LINES  : int = 100

_LINE_TYPE  : int = 0x04   # DWORD — tipo
_LINE_COLOR : int = 0x08   # DWORD — cor ARGB
_LINE_TEXT  : int = 0x30   # char[] — texto

_TEXT_MAX   : int = _ENTRY_SIZE - _LINE_TEXT   # espaço restante (~204 bytes)

# Padrão de bytes em struct+0x04 .. struct+0x0F:
#   type  DWORD (1-10)  +  color com alpha=FF  +  DWORD 0
#   Buscado com regex sobre os bytes crus (sem assumir alinhamento de 4 bytes).
_ENTRY_PATTERN = re.compile(
    b'[\x01-\x0a]\x00\x00\x00'   # type DWORD, valor 1-10
    b'[\x00-\xff]{3}\xff'         # color ARGB, alpha=FF
    b'\x00\x00\x00\x00'          # DWORD seguinte = 0
)
_PATTERN_OFF        : int = 0x04   # padrão começa em struct+0x04
_SIG_MIN_CONSECUTIVE: int = 8      # entradas consecutivas para confirmar

# ---------------------------------------------------------------------------

# Tipos conhecidos (não exaustivo — 0.3.DL pode ter mais)
LINE_TYPE_NONE   = 0
LINE_TYPE_DEBUG  = 1
LINE_TYPE_CHAT   = 2
LINE_TYPE_INFO   = 4
LINE_TYPE_ACTION = 8


@dataclass(frozen=True)
class ChatMessage:
    text:     str
    msg_type: int
    color:    int   # ARGB

    def __str__(self) -> str:
        return self.text


# ---------------------------------------------------------------------------
# Scan de memória
# ---------------------------------------------------------------------------

_PAGE_READABLE = {0x02, 0x04, 0x20, 0x40}
_MEM_COMMIT    = 0x1000
_kernel32      = ctypes.WinDLL("kernel32", use_last_error=True)


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress",       ctypes.c_void_p),
        ("AllocationBase",    ctypes.c_void_p),
        ("AllocationProtect", ctypes.wintypes.DWORD),
        ("RegionSize",        ctypes.c_size_t),
        ("State",             ctypes.wintypes.DWORD),
        ("Protect",           ctypes.wintypes.DWORD),
        ("Type",              ctypes.wintypes.DWORD),
    ]


def _iter_regions(handle):
    addr = 0
    mbi  = _MBI()
    sz   = ctypes.sizeof(mbi)
    while _kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), sz):
        base = mbi.BaseAddress or 0
        if mbi.State == _MEM_COMMIT and mbi.Protect in _PAGE_READABLE:
            yield base, mbi.RegionSize
        addr = base + mbi.RegionSize
        if addr >= 0xFFFFFFFF:
            break


def find_chat_array(pm: pymem.Pymem) -> int | None:
    """
    Escaneia a memória do processo procurando o array de CChatLine.
    Usa regex sobre bytes brutos para não depender de alinhamento de endereço.
    Retorna o endereço absoluto do primeiro slot do array, ou None.
    """
    handle = pm.process_handle
    for region_base, region_size in _iter_regions(handle):
        if region_size > 64 * 1024 * 1024:
            continue
        try:
            data = pm.read_bytes(region_base, region_size)
        except Exception:
            continue

        # Coleta todas as posições onde o padrão casa (= struct+0x04 de cada entrada)
        hits = [m.start() for m in _ENTRY_PATTERN.finditer(data)]
        if len(hits) < _SIG_MIN_CONSECUTIVE:
            continue

        hit_set = set(hits)

        for h in hits:
            # Verifica se há entradas consecutivas espaçadas de _ENTRY_SIZE
            count = 1
            while (h + count * _ENTRY_SIZE) in hit_set:
                count += 1
                if count >= _SIG_MIN_CONSECUTIVE:
                    break

            if count < _SIG_MIN_CONSECUTIVE:
                continue

            # Encontrou sequência válida — recua para o primeiro slot do array
            struct_start = h - _PATTERN_OFF
            while struct_start >= _ENTRY_SIZE:
                prev_pat = struct_start - _ENTRY_SIZE + _PATTERN_OFF
                if prev_pat in hit_set:
                    struct_start -= _ENTRY_SIZE
                else:
                    break

            return region_base + struct_start

    return None


# ---------------------------------------------------------------------------
# Reader principal
# ---------------------------------------------------------------------------

class SampChatReader:
    """Lê o chat do SA-MP 0.3.DL-R1 via memória do processo gta_sa.exe."""

    def __init__(self):
        self._pm:           pymem.Pymem | None = None
        self._array_base:   int = 0
        self._prev_counter: dict[str, int] = {}  # key → count in last snapshot

    # ------------------------------------------------------------------
    # Conexão
    # ------------------------------------------------------------------

    def attach(self) -> None:
        """Conecta ao gta_sa.exe, aguarda samp.dll e localiza o buffer de chat."""
        self._pm = pymem.Pymem("gta_sa.exe")

        samp_mod = pymem.process.module_from_name(self._pm.process_handle, "samp.dll")
        if samp_mod is None:
            raise RuntimeError("samp.dll não encontrado — SA-MP está aberto e conectado a um servidor?")

        addr = find_chat_array(self._pm)
        if addr is None:
            raise RuntimeError(
                "Buffer de chat não encontrado. "
                "Certifique-se de estar conectado a um servidor com mensagens no chat."
            )
        self._array_base = addr
        # Seed counter with current buffer so first poll() sees no history.
        counter: dict[str, int] = {}
        for msg in self._snapshot():
            if msg is not None:
                key = f"{msg.msg_type}|{msg.text}"
                counter[key] = counter.get(key, 0) + 1
        self._prev_counter = counter

    def is_attached(self) -> bool:
        try:
            return self._pm is not None and bool(
                pymem.process.module_from_name(self._pm.process_handle, "samp.dll")
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Leitura interna
    # ------------------------------------------------------------------

    def _read_line(self, index: int) -> ChatMessage | None:
        if not self._array_base:
            return None
        addr = self._array_base + index * _ENTRY_SIZE
        try:
            msg_type = self._pm.read_int(addr + _LINE_TYPE)
            if msg_type == LINE_TYPE_NONE:
                return None

            color    = self._pm.read_int(addr + _LINE_COLOR)
            raw_text = self._pm.read_bytes(addr + _LINE_TEXT, _TEXT_MAX)
            text     = raw_text.split(b"\x00", 1)[0].decode("windows-1252", errors="replace").strip()

            if not text:
                return None

            return ChatMessage(text=text, msg_type=msg_type, color=color)
        except Exception:
            return None

    def _snapshot(self) -> list[ChatMessage | None]:
        return [self._read_line(i) for i in range(_MAX_LINES)]

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def poll(self) -> Iterator[ChatMessage]:
        """Retorna apenas mensagens novas desde o último poll().

        Compara contagens de conteúdo (não índices) para ser imune ao
        deslocamento do ring buffer. Mensagens repetidas são detectadas
        quando sua contagem no buffer aumenta.
        """
        current     = self._snapshot()
        new_counter: dict[str, int] = {}
        first_msg:   dict[str, ChatMessage] = {}
        ordered:     list[str] = []

        for msg in current:
            if msg is None:
                continue
            key = f"{msg.msg_type}|{msg.text}"
            if key not in new_counter:
                new_counter[key] = 0
                first_msg[key]   = msg
                ordered.append(key)
            new_counter[key] += 1

        new_msgs: list[ChatMessage] = []
        for key in ordered:
            extra = new_counter[key] - self._prev_counter.get(key, 0)
            if extra > 0:
                new_msgs.extend([first_msg[key]] * extra)

        self._prev_counter = new_counter
        yield from new_msgs

    def run(
        self,
        callback: Callable[[ChatMessage], None],
        interval: float = 0.3,
    ) -> None:
        """Loop contínuo: chama callback(msg) para cada nova mensagem."""
        print("Aguardando mensagens do SA-MP… (Ctrl+C para parar)")
        while True:
            try:
                if not self.is_attached():
                    print("Processo encerrado.")
                    break
                for msg in self.poll():
                    callback(msg)
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\nEncerrado pelo usuário.")
                break
            except Exception as exc:
                print(f"[erro] {exc}")
                time.sleep(1.0)


# ---------------------------------------------------------------------------
# Execução direta — teste rápido
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Conectando ao gta_sa.exe…")
    reader = SampChatReader()
    try:
        reader.attach()
        print(f"Buffer de chat localizado em: 0x{reader._array_base:08X}")
        print(f"({_MAX_LINES} slots × {_ENTRY_SIZE} bytes = {_MAX_LINES * _ENTRY_SIZE} bytes)\n")
    except Exception as e:
        print(f"Falha: {e}")
        raise SystemExit(1)

    reader.run(callback=lambda msg: print(f"[type={msg.msg_type}] {msg}"))