"""
Configuration preset manager for SAMP Translate.

Presets are persisted as JSON at:
    %APPDATA%/SAMP-Translate/config.json

Each preset is a self-contained snapshot of all user settings (shortcuts,
translation pair, filters, chat style, overlay positions). The active preset
is restored automatically on the next launch.
"""

import json
import os
from pathlib import Path

_APPDATA:      Path = Path(os.environ.get("APPDATA", "."))
CONFIG_DIR:    Path = _APPDATA / "SAMP-Translate"
CONFIG_FILE:   Path = CONFIG_DIR / "config.json"
DEFAULT_NAME:  str  = "Default"
_PLACEHOLDER:  str  = "─ Select ─"  # sentinel used when no language is chosen


def _default_preset() -> dict:
    """Return a fresh preset dict populated with factory defaults."""
    return {
        "shortcuts": {
            "chat_key":    "y",
            "toggle_key":  "z",
            "clear_key":   "",
            "filters_key": "",
        },
        "translation": {
            "enabled":      False,
            "source":       _PLACEHOLDER,
            "target":       _PLACEHOLDER,
            "user_enabled": False,
            "user_source":  _PLACEHOLDER,
            "user_target":  _PLACEHOLDER,
        },
        "filters":               [],
        "ignore_self":           {"enabled": False, "name": ""},
        "no_translate_commands": False,
        "chat_style": {
            "font":         "Arial",
            "size":         11,
            "color":        "#FFFFFF",
            "max_messages": 12,
        },
        "chat_position":  {"x": None, "y": None},
        "input_position": {"x": None, "y": None},
        "status": {
            "visible":   True,
            "x":         None,
            "y":         None,
            "font_size": 10,
        },
    }


class ConfigManager:
    """
    Load, save, and apply named configuration presets.

    The internal data structure:
        {
            "active_preset": "Default",
            "presets": {
                "Default": { ...preset dict... },
                ...
            }
        }
    """

    def __init__(self) -> None:
        self._data: dict = {}
        self.load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load config from disk; fall back to a single default preset on any error."""
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception:
            self._data = {
                "active_preset": DEFAULT_NAME,
                "presets": {DEFAULT_NAME: _default_preset()},
            }

    def save(self) -> None:
        """Persist the current in-memory state to disk (creates the directory if needed)."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def preset_names(self) -> list[str]:
        """Ordered list of all saved preset names."""
        return list(self._data.get("presets", {}).keys())

    @property
    def active_preset(self) -> str:
        """Name of the currently active preset."""
        return self._data.get("active_preset", DEFAULT_NAME)

    def get_preset(self, name: str) -> dict | None:
        """Return the preset dict for the given name, or None if it does not exist."""
        return self._data.get("presets", {}).get(name)

    def set_active(self, name: str) -> None:
        """Mark a preset as active (does not save to disk)."""
        self._data["active_preset"] = name

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(self, name: str) -> None:
        """Create a new preset with default values if it does not already exist."""
        self._data.setdefault("presets", {}).setdefault(name, _default_preset())

    def delete(self, name: str) -> None:
        """Delete a preset, falling back to the first remaining preset if it was active."""
        self._data.get("presets", {}).pop(name, None)
        if self.active_preset == name:
            names = self.preset_names
            self._data["active_preset"] = names[0] if names else DEFAULT_NAME

    def rename(self, old: str, new: str) -> None:
        """Rename a preset; updates the active pointer if the renamed preset was active."""
        presets = self._data.get("presets", {})
        if old in presets and new not in presets:
            presets[new] = presets.pop(old)
            if self.active_preset == old:
                self._data["active_preset"] = new

    # ── Export / Import ───────────────────────────────────────────────────────

    def export_preset(self, name: str, path: str) -> None:
        """Serialize a preset to a standalone JSON file."""
        preset = self.get_preset(name)
        if preset is None:
            raise KeyError(name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"preset_name": name, **preset}, f, ensure_ascii=False, indent=2)

    def import_preset(self, path: str) -> str:
        """
        Load a preset from a JSON file exported by export_preset().

        If a preset with the same name already exists, a numeric suffix is
        appended (e.g. "MyPreset (2)") to avoid overwriting it.
        Returns the final name the preset was stored under.
        """
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        name = data.pop("preset_name", Path(path).stem)
        base, n = name, 1
        while name in self._data.get("presets", {}):
            name = f"{base} ({n})"
            n += 1
        self._data.setdefault("presets", {})[name] = data
        return name

    # ── Collect current app state → preset dict ───────────────────────────────

    def collect(self, panel, overlay) -> dict:
        """
        Build a preset dict from the live state of the control panel and overlay.

        panel  — ControlPanel instance
        overlay — ChatOverlay instance (may be None before a window is selected)
        """
        p = _default_preset()

        p["shortcuts"] = dict(panel._shortcuts)

        t = panel._translation
        p["translation"] = {
            "enabled":      t["enabled"].get(),
            "source":       t["source"].get(),
            "target":       t["target"].get(),
            "user_enabled": t["user_enabled"].get(),
            "user_source":  t["user_source"].get(),
            "user_target":  t["user_target"].get(),
        }

        p["filters"] = [
            {
                "name":    f["name"],
                "keyword": f["keyword"],
                "type":    f.get("type", "whitelist"),
                "color":   f.get("color", ""),
                "active":  f["var"].get(),
            }
            for f in panel._filters
        ]

        p["ignore_self"] = {
            "enabled": panel._ignore_self["var"].get(),
            "name":    panel._ignore_self["name"],
        }

        p["no_translate_commands"] = panel._no_translate_commands.get()

        if overlay is not None:
            family, size, color = overlay.get_style()
            p["chat_style"] = {
                "font":         family,
                "size":         size,
                "color":        color,
                "max_messages": overlay.get_max_messages(),
            }
            p["chat_position"]  = {"x": overlay._pos_x,       "y": overlay._pos_y}
            p["input_position"] = {"x": overlay._input_pos_x,  "y": overlay._input_pos_y}
            p["status"] = {
                "visible":   overlay.get_status_visible(),
                "x":         overlay._status_pos_x,
                "y":         overlay._status_pos_y,
                "font_size": overlay.get_status_font_size(),
            }

        return p

    def save_current(self, name: str, panel, overlay) -> None:
        """Collect the current app state and store it under the given preset name."""
        self._data.setdefault("presets", {})[name] = self.collect(panel, overlay)
        self._data["active_preset"] = name

    # ── Apply preset → app state ──────────────────────────────────────────────

    def apply(self, name: str, panel, overlay) -> None:
        """
        Apply a named preset to the control panel and overlay.

        Restores shortcuts, translation settings, filters, and visual options.
        Also re-registers keyboard hooks so the new shortcuts take effect immediately.
        """
        import tkinter as tk
        preset = self.get_preset(name)
        if preset is None:
            return

        panel._shortcuts.update(preset.get("shortcuts", {}))
        if panel._hwnd:
            panel._setup_chat_hook()
            panel._setup_toggle_hook()
            panel._setup_clear_hook()
            panel._setup_filters_hook()

        td = preset.get("translation", {})
        t  = panel._translation
        t["enabled"].set(td.get("enabled", False))
        t["source"].set(td.get("source", _PLACEHOLDER))
        t["target"].set(td.get("target", _PLACEHOLDER))
        t["user_enabled"].set(td.get("user_enabled", False))
        t["user_source"].set(td.get("user_source", _PLACEHOLDER))
        t["user_target"].set(td.get("user_target", _PLACEHOLDER))

        panel._filters.clear()
        for f in preset.get("filters", []):
            panel._filters.append({
                "name":    f["name"],
                "keyword": f["keyword"],
                "type":    f.get("type", "whitelist"),
                "color":   f.get("color", ""),
                "var":     tk.BooleanVar(value=f.get("active", False)),
            })

        ig = preset.get("ignore_self", {})
        panel._ignore_self["var"].set(ig.get("enabled", False))
        panel._ignore_self["name"] = ig.get("name", "")

        panel._no_translate_commands.set(preset.get("no_translate_commands", False))

        self.apply_overlay(preset, overlay)
        self._data["active_preset"] = name

    def apply_overlay(self, preset_or_name, overlay) -> None:
        """
        Apply only the visual/positional settings from a preset to the overlay.

        Accepts either a preset name (str) or a preset dict directly so it can
        be called independently of the full apply() flow.
        """
        if overlay is None:
            return
        if isinstance(preset_or_name, str):
            preset = self.get_preset(preset_or_name)
            if preset is None:
                return
        else:
            preset = preset_or_name

        cs = preset.get("chat_style", {})
        overlay.set_style(cs.get("font", "Arial"), cs.get("size", 11), cs.get("color", "#FFFFFF"))
        overlay.set_max_messages(cs.get("max_messages", 12))

        cp = preset.get("chat_position", {})
        if cp.get("x") is not None:
            overlay.set_position(cp["x"], cp["y"])

        ip = preset.get("input_position", {})
        if ip.get("x") is not None:
            overlay.set_input_position(ip["x"], ip["y"])

        st = preset.get("status", {})
        overlay.set_status_visible(st.get("visible", True))
        if st.get("x") is not None:
            overlay.set_status_position(st["x"], st["y"])
        overlay.set_status_font_size(st.get("font_size", 10))
