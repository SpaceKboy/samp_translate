"""
SA-MP 0.3.DL-R1 chat reader via process memory (pymem).

The chat buffer is heap-allocated, so its base address changes every session.
On startup we perform a signature scan to locate the CChatLine array at runtime.

Usage:
    reader = SampChatReader()
    reader.attach()          # connect to gta_sa.exe and locate the buffer

    for msg in reader.poll():
        print(msg)           # ChatMessage(text, msg_type, color)

    # or in a continuous polling loop:
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
# CChatLine struct layout — SA-MP 0.3.DL-R1
# Determined via memory analysis on 2026-04-19. Not officially documented.
#
#   +0x00  DWORD  — always 0 (reserved/padding)
#   +0x04  DWORD  — message type (1–8; 0 = empty slot)
#   +0x08  DWORD  — text color as ARGB  (alpha byte is always 0xFF)
#   +0x0C  DWORD  — always 0
#   +0x10  ...    — internal fields (timestamps, pointers)
#   +0x30  char[] — message text (null-terminated, up to ~200 bytes)
# ---------------------------------------------------------------------------

_ENTRY_SIZE: int = 0xFC  # 252 bytes per chat entry
_MAX_LINES:  int = 100   # ring buffer holds 100 slots

_LINE_TYPE:  int = 0x04  # DWORD — message type
_LINE_COLOR: int = 0x08  # DWORD — ARGB color
_LINE_TEXT:  int = 0x30  # char[] — null-terminated text

_TEXT_MAX: int = _ENTRY_SIZE - _LINE_TEXT  # remaining space for text (~204 bytes)

# Byte pattern matched at struct+0x04 through struct+0x0F:
#   type DWORD (1–10) | ARGB color with alpha=FF | zero DWORD
# Applied as a raw-byte regex so alignment assumptions are unnecessary.
_ENTRY_PATTERN = re.compile(
    b'[\x01-\x0a]\x00\x00\x00'   # type DWORD, value 1–10
    b'[\x00-\xff]{3}\xff'         # ARGB color, alpha byte = FF
    b'\x00\x00\x00\x00'          # next DWORD = 0
)
_PATTERN_OFF:         int = 0x04  # pattern starts at struct+0x04
_SIG_MIN_CONSECUTIVE: int = 8     # minimum consecutive valid entries required to confirm array

# ---------------------------------------------------------------------------

# Known message types (non-exhaustive — 0.3.DL may define more)
LINE_TYPE_NONE   = 0
LINE_TYPE_DEBUG  = 1
LINE_TYPE_CHAT   = 2
LINE_TYPE_INFO   = 4
LINE_TYPE_ACTION = 8


@dataclass(frozen=True)
class ChatMessage:
    """Immutable snapshot of a single SA-MP chat line."""
    text:     str
    msg_type: int
    color:    int   # ARGB packed as a 32-bit integer

    def __str__(self) -> str:
        return self.text


# ---------------------------------------------------------------------------
# Memory scanning helpers
# ---------------------------------------------------------------------------

# Windows memory protection constants for readable pages
_PAGE_READABLE = {0x02, 0x04, 0x20, 0x40}
_MEM_COMMIT    = 0x1000
_kernel32      = ctypes.WinDLL("kernel32", use_last_error=True)


class _MBI(ctypes.Structure):
    """MEMORY_BASIC_INFORMATION — returned by VirtualQueryEx."""
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
    """Yield (base_address, size) for every committed, readable memory region in the process."""
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
    Scan process memory for the CChatLine array.

    Uses a raw-byte regex so no address-alignment assumptions are needed.
    Returns the absolute address of the first slot, or None if not found.

    The scan skips regions larger than 64 MB to avoid heap arenas that are
    unlikely to contain a compact 25 KB chat buffer.
    """
    handle = pm.process_handle
    for region_base, region_size in _iter_regions(handle):
        if region_size > 64 * 1024 * 1024:
            continue
        try:
            data = pm.read_bytes(region_base, region_size)
        except Exception:
            continue

        # Collect all byte offsets where the entry pattern matches (= struct+0x04)
        hits = [m.start() for m in _ENTRY_PATTERN.finditer(data)]
        if len(hits) < _SIG_MIN_CONSECUTIVE:
            continue

        hit_set = set(hits)

        for h in hits:
            # Count how many consecutive entries are spaced exactly _ENTRY_SIZE apart
            count = 1
            while (h + count * _ENTRY_SIZE) in hit_set:
                count += 1
                if count >= _SIG_MIN_CONSECUTIVE:
                    break

            if count < _SIG_MIN_CONSECUTIVE:
                continue

            # Valid sequence found — walk backward to find slot 0 of the array
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
# Chat reader
# ---------------------------------------------------------------------------

class SampChatReader:
    """
    Reads the SA-MP 0.3.DL-R1 chat buffer from gta_sa.exe memory.

    Thread-safety: attach() and poll() may be called from a background thread,
    but must not be called concurrently with each other.
    """

    def __init__(self):
        self._pm:           pymem.Pymem | None = None
        self._array_base:   int = 0
        # Stores (key → count) from the previous poll snapshot for deduplication
        self._prev_counter: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def attach(self) -> None:
        """
        Connect to gta_sa.exe, verify samp.dll is loaded, and locate the chat buffer.

        Raises RuntimeError if gta_sa.exe is not running, samp.dll is absent,
        or the chat array cannot be found (e.g. not yet connected to a server).
        """
        self._pm = pymem.Pymem("gta_sa.exe")

        samp_mod = pymem.process.module_from_name(self._pm.process_handle, "samp.dll")
        if samp_mod is None:
            raise RuntimeError(
                "samp.dll not found — is SA-MP running and connected to a server?"
            )

        addr = find_chat_array(self._pm)
        if addr is None:
            raise RuntimeError(
                "Chat buffer not found. "
                "Make sure you are connected to a server with at least some chat messages."
            )
        self._array_base = addr

        # Seed the counter with the current buffer contents so that the first
        # poll() call does not re-emit the entire history.
        counter: dict[str, int] = {}
        for msg in self._snapshot():
            if msg is not None:
                key = f"{msg.msg_type}|{msg.text}"
                counter[key] = counter.get(key, 0) + 1
        self._prev_counter = counter

    def is_attached(self) -> bool:
        """Return True if the process is still running and samp.dll is loaded."""
        try:
            return self._pm is not None and bool(
                pymem.process.module_from_name(self._pm.process_handle, "samp.dll")
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Internal reading
    # ------------------------------------------------------------------

    def _read_line(self, index: int) -> ChatMessage | None:
        """Read a single chat slot at the given index. Returns None for empty slots."""
        if not self._array_base:
            return None
        addr = self._array_base + index * _ENTRY_SIZE
        try:
            msg_type = self._pm.read_int(addr + _LINE_TYPE)
            if msg_type == LINE_TYPE_NONE:
                return None

            color    = self._pm.read_int(addr + _LINE_COLOR)
            raw_text = self._pm.read_bytes(addr + _LINE_TEXT, _TEXT_MAX)
            # SA-MP encodes chat text in Windows-1252 (Western European code page)
            text = raw_text.split(b"\x00", 1)[0].decode("windows-1252", errors="replace").strip()

            if not text:
                return None

            return ChatMessage(text=text, msg_type=msg_type, color=color)
        except Exception:
            return None

    def _snapshot(self) -> list[ChatMessage | None]:
        """Dump all 100 chat slots into a list (None for empty slots)."""
        return [self._read_line(i) for i in range(_MAX_LINES)]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poll(self) -> Iterator[ChatMessage]:
        """
        Yield only messages that are new since the last poll() call.

        Uses content-count comparison instead of index tracking so the method
        is immune to the ring buffer rotating between polls. A repeated message
        (same type + text) is considered new only when its count in the buffer
        has increased since the last snapshot.
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
        """
        Continuous polling loop — calls callback(msg) for every new message.

        Blocks until the process exits or a KeyboardInterrupt is received.
        """
        print("Waiting for SA-MP messages… (Ctrl+C to stop)")
        while True:
            try:
                if not self.is_attached():
                    print("Process has exited.")
                    break
                for msg in self.poll():
                    callback(msg)
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\nStopped by user.")
                break
            except Exception as exc:
                print(f"[error] {exc}")
                time.sleep(1.0)


# ---------------------------------------------------------------------------
# Direct execution — quick smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Connecting to gta_sa.exe…")
    reader = SampChatReader()
    try:
        reader.attach()
        print(f"Chat buffer found at: 0x{reader._array_base:08X}")
        print(f"({_MAX_LINES} slots × {_ENTRY_SIZE} bytes = {_MAX_LINES * _ENTRY_SIZE} bytes)\n")
    except Exception as e:
        print(f"Failed: {e}")
        raise SystemExit(1)

    reader.run(callback=lambda msg: print(f"[type={msg.msg_type}] {msg}"))
