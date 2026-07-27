"""Durable storage for the portal list, outside the plugin's settings.

Dispatcharr's plugin panel writes its own React state over ``PluginConfig.settings``
before running any action, and never re-reads the result::

    // PluginCard.jsx
    // Save settings before running to ensure backend uses latest values
    await onSaveSettings(plugin.key, settings);

So anything a plugin writes into its own settings is destroyed by the next
action the user clicks, because the panel is still holding the state it fetched
before that write. The portal list therefore lives in a file that only this
plugin touches, and the settings copy is treated as a mirror that can be
repaired from it.

The file sits outside the plugin directory on purpose: re-importing a newer
build replaces ``/data/plugins/distalker`` wholesale, which would take the
registry with it.

Alongside it sits a *pending* marker, which answers the question the file alone
cannot: when the panel sends a portal list that differs from the stored one, is
that a hand edit of the textarea, or the panel replaying what it loaded before
this plugin last wrote? The marker records the list as the plugin itself last
saved it, and is cleared the moment the panel quotes the current list back --
i.e. once the user has reopened the panel and it has caught up.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Optional

REGISTRY_DIR = os.environ.get("DISTALKER_DATA_DIR", "/data/distalker")
REGISTRY_PATH = os.path.join(REGISTRY_DIR, "portals.txt")
PENDING_PATH = os.path.join(REGISTRY_DIR, "portals.pending")


def digest(text: Optional[str]) -> str:
    """A stable fingerprint of a portal list, ignoring surrounding whitespace."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def load_registry() -> Optional[str]:
    """Return the stored portal list, or None if none has been written yet.

    None and "" mean different things here: None is "this plugin has never
    saved a list", while "" is "the list is deliberately empty".
    """
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def save_registry(text: str, pending: bool = False) -> bool:
    """Write the portal list atomically. Returns False if the write failed.

    A failure is not fatal -- the settings copy still works for the current
    session -- so callers log and carry on rather than aborting the action.

    ``pending`` marks the write as one the plugin made on its own initiative,
    which the open panel cannot know about yet. Writes that merely adopt what
    the panel just sent leave it False: the panel is in step by definition.
    """
    try:
        os.makedirs(REGISTRY_DIR, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=REGISTRY_DIR, prefix=".portals-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text or "")
            os.replace(temp_path, REGISTRY_PATH)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
        _write_pending(digest(text) if pending else None)
        return True
    except OSError:
        return False


def _write_pending(value: Optional[str]) -> None:
    """Set or clear the marker. Never fatal -- worst case we lose the hint."""
    try:
        if value is None:
            if os.path.exists(PENDING_PATH):
                os.unlink(PENDING_PATH)
            return
        os.makedirs(REGISTRY_DIR, exist_ok=True)
        with open(PENDING_PATH, "w", encoding="utf-8") as handle:
            handle.write(value)
    except OSError:
        pass


def clear_pending() -> None:
    """Forget the marker, the panel having proved it holds the current list."""
    _write_pending(None)


def is_pending() -> bool:
    """True if the plugin has written the list since the panel last loaded it.

    The marker is only believed while it still describes the file on disk, so a
    list changed by any other route -- a hand-edited ``portals.txt``, a restored
    backup -- invalidates it rather than freezing the textarea out forever.
    """
    try:
        with open(PENDING_PATH, "r", encoding="utf-8") as handle:
            marker = handle.read().strip()
    except OSError:
        return False
    return bool(marker) and marker == digest(load_registry())
