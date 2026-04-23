"""Gerenciamento de presets de configuração — %APPDATA%/SAMP-Translate/config.json."""

import json
import os
from pathlib import Path

_APPDATA       = Path(os.environ.get("APPDATA", "."))
CONFIG_DIR     = _APPDATA / "SAMP-Translate"
CONFIG_FILE    = CONFIG_DIR / "config.json"
DEFAULT_NAME   = "Padrão"
_PLACEHOLDER   = "─ Selecionar ─"


def _default_preset() -> dict:
    return {
        "shortcuts":      {"chat_key": "y", "toggle_key": "z", "clear_key": "", "filters_key": ""},
        "translation":    {
            "enabled": False, "source": _PLACEHOLDER, "target": _PLACEHOLDER,
            "user_enabled": False, "user_source": _PLACEHOLDER, "user_target": _PLACEHOLDER,
        },
        "filters":        [],
        "ignore_self":    {"enabled": False, "name": ""},
        "chat_style":     {"font": "Arial", "size": 11, "color": "#FFFFFF", "max_messages": 12},
        "chat_position":  {"x": None, "y": None},
        "input_position": {"x": None, "y": None},
        "status":         {"visible": True, "x": None, "y": None, "font_size": 10},
    }


class ConfigManager:

    def __init__(self) -> None:
        self._data: dict = {}
        self.load()

    # ── Persistência ──────────────────────────────────────────────────────────

    def load(self) -> None:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception:
            self._data = {
                "active_preset": DEFAULT_NAME,
                "presets": {DEFAULT_NAME: _default_preset()},
            }

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    # ── Acesso ────────────────────────────────────────────────────────────────

    @property
    def preset_names(self) -> list[str]:
        return list(self._data.get("presets", {}).keys())

    @property
    def active_preset(self) -> str:
        return self._data.get("active_preset", DEFAULT_NAME)

    def get_preset(self, name: str) -> dict | None:
        return self._data.get("presets", {}).get(name)

    def set_active(self, name: str) -> None:
        self._data["active_preset"] = name

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(self, name: str) -> None:
        self._data.setdefault("presets", {}).setdefault(name, _default_preset())

    def delete(self, name: str) -> None:
        self._data.get("presets", {}).pop(name, None)
        if self.active_preset == name:
            names = self.preset_names
            self._data["active_preset"] = names[0] if names else DEFAULT_NAME

    def rename(self, old: str, new: str) -> None:
        presets = self._data.get("presets", {})
        if old in presets and new not in presets:
            presets[new] = presets.pop(old)
            if self.active_preset == old:
                self._data["active_preset"] = new

    # ── Export / Import ───────────────────────────────────────────────────────

    def export_preset(self, name: str, path: str) -> None:
        preset = self.get_preset(name)
        if preset is None:
            raise KeyError(name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"preset_name": name, **preset}, f, ensure_ascii=False, indent=2)

    def import_preset(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        name = data.pop("preset_name", Path(path).stem)
        base, n = name, 1
        while name in self._data.get("presets", {}):
            name = f"{base} ({n})"
            n += 1
        self._data.setdefault("presets", {})[name] = data
        return name

    # ── Coletar estado atual → preset ─────────────────────────────────────────

    def collect(self, panel, overlay) -> dict:
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

        if overlay is not None:
            family, size, color = overlay.get_style()
            p["chat_style"] = {
                "font":         family,
                "size":         size,
                "color":        color,
                "max_messages": overlay.get_max_messages(),
            }
            p["chat_position"]  = {"x": overlay._pos_x,      "y": overlay._pos_y}
            p["input_position"] = {"x": overlay._input_pos_x, "y": overlay._input_pos_y}
            p["status"] = {
                "visible":   overlay.get_status_visible(),
                "x":         overlay._status_pos_x,
                "y":         overlay._status_pos_y,
                "font_size": overlay.get_status_font_size(),
            }

        return p

    def save_current(self, name: str, panel, overlay) -> None:
        self._data.setdefault("presets", {})[name] = self.collect(panel, overlay)
        self._data["active_preset"] = name

    # ── Aplicar preset → estado do app ───────────────────────────────────────

    def apply(self, name: str, panel, overlay) -> None:
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

        self.apply_overlay(preset, overlay)
        self._data["active_preset"] = name

    def apply_overlay(self, preset_or_name, overlay) -> None:
        """Aplica somente as configurações visuais ao overlay (pode ser chamado separado)."""
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
