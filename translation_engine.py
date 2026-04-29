"""
TranslationEngine — offline translation package management and route cache.

Separates all argostranslate logic from the UI so ControlPanel only calls
high-level methods (translate, install_packages, etc.) and never imports
argostranslate directly.
"""

import json
import sys
import threading
from pathlib import Path
from typing import Callable


class TranslationEngine:
    """
    Manages argostranslate packages and translation routes.

    Thread safety:
        _route_cache is replaced atomically (Python GIL-safe dict swap).
        _rebuild_pending ensures rebuild requests are never silently dropped:
        if a rebuild is requested while one is running, the follow-up is
        queued and executed immediately after the current worker finishes.
    """

    def __init__(self, schedule_fn: Callable | None = None):
        """
        schedule_fn: a function that schedules a callable on the UI thread,
                     e.g. lambda fn: root.after(0, fn).
        """
        self._route_cache: dict = {}
        self._rebuild_in_progress = False
        self._rebuild_pending = False
        self._pending_on_rebuilt: Callable | None = None
        self._schedule_fn = schedule_fn

    def _ui(self, fn: Callable) -> None:
        if self._schedule_fn:
            self._schedule_fn(fn)
        else:
            fn()

    # ── Translation ───────────────────────────────────────────────────────────

    def translate(self, text: str, src: str, tgt: str) -> str:
        """Translate text using the route cache. Returns original on any failure."""
        route = self._route_cache.get((src, tgt))
        if route is None:
            return text
        try:
            result = text
            for t in route:
                result = t.translate(result)
                if _is_garbled(result, text):
                    return text
            return result or text
        except Exception as e:
            print(f"[translate] {src}→{tgt}: {e}", file=sys.stderr)
            return text

    def is_installed(self, src: str, tgt: str) -> bool:
        """True if a valid route exists in the current cache."""
        return (src, tgt) in self._route_cache

    # ── Package queries ───────────────────────────────────────────────────────

    def get_installed_pairs(self) -> list[tuple[str, str]]:
        """Return (from_code, to_code) for every package on disk."""
        try:
            import argostranslate.package as ap
            return [(p.from_code, p.to_code) for p in ap.get_installed_packages()]
        except Exception:
            return []

    def get_broken_pairs(self) -> set[tuple[str, str]]:
        """Return (from_code, to_code) for BPE-broken packages excluded from routes."""
        try:
            import argostranslate.settings as s
            return _detect_broken(s.data_dir / "packages")
        except Exception:
            return set()

    # ── Route cache rebuild ───────────────────────────────────────────────────

    def rebuild_route_cache(self, on_rebuilt: Callable | None = None) -> None:
        """
        Rebuild translation route cache in a background thread.

        on_rebuilt is called on the UI thread after the cache is ready.
        If a rebuild is already running, the follow-up is queued so no
        request is ever silently dropped.
        """
        if self._rebuild_in_progress:
            self._rebuild_pending = True
            if on_rebuilt:
                self._pending_on_rebuilt = on_rebuilt
            return
        self._rebuild_in_progress = True
        self._rebuild_pending = False
        self._pending_on_rebuilt = on_rebuilt
        threading.Thread(target=self._rebuild_worker, daemon=True,
                         name="argos-rebuild").start()

    def _rebuild_worker(self) -> None:
        try:
            import argostranslate.translate as at
            import argostranslate.package as ap
            import argostranslate.settings as s

            # Clear lru_cache here, not in the caller, to guarantee freshness.
            at.get_installed_languages.cache_clear()

            broken = _detect_broken(s.data_dir / "packages")
            lang_map = {lang.code: lang for lang in at.get_installed_languages()}

            direct: dict = {}
            for pkg in ap.get_installed_packages():
                pair = (pkg.from_code, pkg.to_code)
                if pair in broken:
                    continue
                fl = lang_map.get(pkg.from_code)
                tl = lang_map.get(pkg.to_code)
                if not fl or not tl:
                    continue
                t = fl.get_translation(tl)
                if t:
                    direct[pair] = t

            routes: dict = {pair: [t] for pair, t in direct.items()}

            for (a, b), t1 in direct.items():
                for (c, d), t2 in direct.items():
                    if b == c and (a, d) not in routes:
                        routes[(a, d)] = [t1, t2]

            for (a, b), chain in list(routes.items()):
                if len(chain) != 2:
                    continue
                for (c, d), t3 in direct.items():
                    if b == c and (a, d) not in routes:
                        routes[(a, d)] = chain + [t3]

            self._route_cache = routes
        except Exception as e:
            print(f"[argos-rebuild] {e}", file=sys.stderr)
        finally:
            self._rebuild_in_progress = False
            cb = self._pending_on_rebuilt
            self._pending_on_rebuilt = None
            if cb:
                self._ui(cb)
            if self._rebuild_pending:
                self.rebuild_route_cache()

    # ── Install ───────────────────────────────────────────────────────────────

    def install_packages(self, src: str, tgt: str, on_done: Callable) -> None:
        """Download and install packages for src→tgt in a background thread."""
        threading.Thread(target=self._install_worker, args=(src, tgt, on_done),
                         daemon=True, name="argos-install").start()

    def _install_worker(self, src: str, tgt: str, on_done: Callable) -> None:
        try:
            import argostranslate.package as ap
            import argostranslate.translate as at
            ap.update_package_index()
            avail = {(p.from_code, p.to_code): p for p in ap.get_available_packages()}

            # Returns True if the pair is available and its package is not BPE.
            # download() is cached locally by argostranslate so repeated calls
            # for the same pair do not hit the network again.
            def _ok(pair: tuple) -> bool:
                return pair in avail and not _is_package_bpe(avail[pair].download())

            # Resolve which pairs to install, always covering both directions so
            # Server Chat and User Chat both work after a single download action.
            # Priority: direct → bridge via any intermediate language.
            wanted: list[tuple[str, str]] = []

            if _ok((src, tgt)):
                wanted = [(src, tgt), (tgt, src)]
            else:
                # Try bridge languages in priority order. Using a fixed list avoids
                # downloading every available package just to check BPE status.
                bridges = ["pt", "en", "fr", "de", "it", "ar", "ru", "zh", "ja", "ko"]
                for mid in bridges:
                    if mid in (src, tgt):
                        continue
                    if _ok((src, mid)) and _ok((mid, tgt)):
                        # Forward: src→mid→tgt  |  Reverse: tgt→mid→src
                        wanted = [(src, mid), (mid, tgt)]
                        if _ok((tgt, mid)):
                            wanted.append((tgt, mid))
                        if _ok((mid, src)):
                            wanted.append((mid, src))
                        break

            if not wanted:
                self._ui(lambda: on_done(False))
                return

            at.get_installed_languages.cache_clear()
            installed = {
                (lang.code, t.to_lang.code)
                for lang in at.get_installed_languages()
                for t in lang.translations_from
            }
            newly_installed = False
            for pair in wanted:
                if pair not in avail or pair in installed:
                    continue
                ap.install_from_path(avail[pair].download())
                newly_installed = True

            if newly_installed or any(pair in installed for pair in wanted[:2]):
                self.rebuild_route_cache(on_rebuilt=lambda: on_done(True))
            else:
                self._ui(lambda: on_done(False))
        except Exception as e:
            print(f"[argos-install] {e}", file=sys.stderr)
            self._ui(lambda: on_done(False))

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete_package(self, src: str, tgt: str, on_done: Callable) -> None:
        """Delete installed package for src→tgt in a background thread."""
        threading.Thread(target=self._delete_worker, args=(src, tgt, on_done),
                         daemon=True, name="argos-delete").start()

    def _delete_worker(self, src: str, tgt: str, on_done: Callable) -> None:
        try:
            import argostranslate.settings as argo_settings
            import shutil
            packages_dir = argo_settings.data_dir / "packages"
            deleted = False
            if packages_dir.exists():
                for entry in packages_dir.iterdir():
                    if not entry.is_dir():
                        continue
                    meta_file = entry / "metadata.json"
                    if not meta_file.exists():
                        continue
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        if meta.get("from_code") == src and meta.get("to_code") == tgt:
                            shutil.rmtree(str(entry), ignore_errors=True)
                            deleted = True
                    except Exception:
                        continue
            if deleted:
                import argostranslate.translate as at
                at.get_installed_languages.cache_clear()
                self.rebuild_route_cache(on_rebuilt=lambda: on_done(True))
            else:
                self._ui(lambda: on_done(False))
        except Exception as e:
            print(f"[argos-delete] {e}", file=sys.stderr)
            self._ui(lambda: on_done(False))


# ── Module-level helpers ──────────────────────────────────────────────────────

def _detect_broken(pkgs_dir: Path) -> set[tuple[str, str]]:
    """Return (from_code, to_code) pairs whose vocabulary uses BPE format.

    BPE packages (@@-suffix tokens) are incompatible with ctranslate2 v4 and
    produce looping garbled output. SentencePiece packages (▁ U+2581 tokens)
    are fine.
    """
    broken: set = set()
    if not pkgs_dir.exists():
        return broken
    for d in pkgs_dir.iterdir():
        if not d.is_dir():
            continue
        meta_f = d / "metadata.json"
        vocab_f = d / "model" / "shared_vocabulary.json"
        if not meta_f.exists() or not vocab_f.exists():
            continue
        try:
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
            vocab = json.loads(vocab_f.read_text())
            bpe = sum(1 for tok in vocab if tok.endswith("@@"))
            spm = sum(1 for tok in vocab if chr(0x2581) in tok)
            if bpe > spm:
                broken.add((meta["from_code"], meta["to_code"]))
        except Exception:
            continue
    return broken


def _is_package_bpe(pkg_path: str) -> bool:
    """Return True if a downloaded package zip uses BPE vocabulary format.

    Peeks inside the zip before installation so broken packages are never
    written to disk.
    """
    import zipfile
    try:
        with zipfile.ZipFile(pkg_path) as zf:
            vocab_names = [n for n in zf.namelist() if n.endswith("shared_vocabulary.json")]
            if not vocab_names:
                return False  # no BPE vocab file — SentencePiece .model, fine
            vocab = json.loads(zf.read(vocab_names[0]))
            bpe = sum(1 for tok in vocab if tok.endswith("@@"))
            spm = sum(1 for tok in vocab if chr(0x2581) in tok)
            return bpe > spm
    except Exception:
        return False


def _is_garbled(text: str, source: str) -> bool:
    if not text:
        return False
    if len(text) > len(source) * 5 and len(text) > 200:
        return True
    if len(text) >= 32:
        chunk = text[:80]
        for i in range(len(chunk) - 8):
            if chunk.count(chunk[i:i + 8]) >= 4:
                return True
    return False
