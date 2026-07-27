"""Surviving the settings panel.

Dispatcharr's plugin panel overwrites PluginConfig.settings with its own React
state before running any action, and never re-reads the result. Anything this
plugin writes into its settings is therefore destroyed by the next click,
because the panel is still holding the state it fetched beforehand.

These tests pin the recovery: the portal list is mirrored to a file, an absent
key means the panel clobbered it, and a different key means the user edited the
textarea by hand -- unless the plugin has written since the panel loaded, in
which case the panel is replaying a stale copy and the file wins.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Point the registry at a scratch directory before plugin.py imports it.
_TMP = tempfile.mkdtemp(prefix="distalker-test-")
os.environ["DISTALKER_DATA_DIR"] = _TMP

sys.path.insert(0, REPO)


def load_plugin_module():
    pkg = types.ModuleType("distalker_pkg")
    pkg.__path__ = [REPO]
    pkg.__package__ = "distalker_pkg"
    sys.modules["distalker_pkg"] = pkg
    spec = importlib.util.spec_from_file_location(
        "distalker_pkg.plugin", os.path.join(REPO, "plugin.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["distalker_pkg.plugin"] = module
    spec.loader.exec_module(module)
    return module


plugin_mod = load_plugin_module()
import registry  # noqa: E402
import stalker_api as s  # noqa: E402

PORTAL = "weaseltv | http://weaseltv.live/c/ | 00:1A:79:29:53:38 | max_streams=1\n"


class NullLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def exception(self, *a, **k): pass


def make_plugin(raw_settings):
    """A Plugin whose DB access is replaced by an in-memory dict.

    Only the row write is stubbed, so the mirroring in ``_save_settings`` --
    including the pending marker -- is the real thing under test.
    """
    p = plugin_mod.Plugin()
    store = {"settings": dict(raw_settings)}

    def write(settings):
        store["settings"] = dict(settings)

    p._write_settings = write
    p._raw_settings = lambda: dict(store["settings"])
    return p, store


def reset():
    for path in (registry.REGISTRY_PATH, registry.PENDING_PATH):
        if os.path.exists(path):
            os.unlink(path)


def test_registry_file_lands_outside_the_plugin_directory():
    """Re-importing a build wipes /data/plugins/distalker; the list must survive."""
    assert not os.path.abspath(registry.REGISTRY_PATH).startswith(os.path.abspath(REPO))


def test_absent_key_means_clobbered_and_is_restored():
    reset()
    registry.save_registry(PORTAL)

    # The panel POSTed a state captured before the portal existed: no key at all.
    p, store = make_plugin({"new_name": "weaseltv"})
    merged = {"new_name": "weaseltv", "portals": ""}   # defaults merged by the loader

    result = p._reconcile_registry(merged, NullLogger())

    assert result["portals"] == PORTAL, "the clobbered list must come back"
    assert store["settings"]["portals"] == PORTAL, "and be written back to the DB"
    portals, errors = s.parse_portals(result["portals"])
    assert not errors and [x.name for x in portals] == ["weaseltv"]


def test_hand_edited_textarea_wins():
    reset()
    registry.save_registry(PORTAL)

    edited = PORTAL + "second | http://b.example/c/ | 00:1A:79:AA:BB:02 | max_streams=1\n"
    p, _ = make_plugin({"portals": edited})

    result = p._reconcile_registry({"portals": edited}, NullLogger())

    assert result["portals"] == edited, "the user's edit must not be reverted"
    assert registry.load_registry() == edited, "and must be adopted into the file"


def test_deliberately_emptied_list_is_respected():
    """Clearing the textarea by hand is a real intent, not a clobber.

    No pending marker, so the panel is in step with the file and the empty
    value it sends is the user's own doing.
    """
    reset()
    registry.save_registry(PORTAL)

    p, _ = make_plugin({"portals": ""})           # key present, value empty
    result = p._reconcile_registry({"portals": ""}, NullLogger())

    assert result["portals"] == ""
    assert (registry.load_registry() or "") == ""


def test_first_run_with_no_file_writes_one():
    reset()
    assert registry.load_registry() is None

    p, _ = make_plugin({"portals": PORTAL})
    p._reconcile_registry({"portals": PORTAL}, NullLogger())

    assert registry.load_registry() == PORTAL


def test_nothing_anywhere_is_harmless():
    reset()
    p, store = make_plugin({})
    result = p._reconcile_registry({"portals": ""}, NullLogger())
    assert result["portals"] == ""
    assert "portals" not in store["settings"], "must not invent a save"


def test_save_settings_mirrors_to_the_file():
    reset()
    p, _ = make_plugin({})
    p._save_settings({"portals": PORTAL, "new_name": ""})
    assert registry.load_registry() == PORTAL


def test_add_then_stale_click_does_not_lose_the_portal():
    """The exact sequence that lost the portal before this fix."""
    reset()

    # 1. Add portal runs and persists the list.
    p, store = make_plugin({})
    p._save_settings({"portals": PORTAL})
    assert registry.load_registry() == PORTAL

    # 2. The panel, still holding its pre-add state, overwrites settings.
    store["settings"] = {"new_name": "weaseltv", "new_url": "http://weaseltv.live/c/"}

    # 3. The user clicks another action.
    result = p._reconcile_registry(dict(store["settings"], portals=""), NullLogger())

    assert result["portals"] == PORTAL, "the portal must survive the stale save"


def test_stale_empty_textarea_does_not_erase_the_list():
    """The panel sends portals:"" from a page loaded before the first Add.

    Observed in the wild: the key is present and empty, so the pre-marker code
    read it as a deliberate wipe and emptied the file.
    """
    reset()

    p, store = make_plugin({})
    p._save_settings({"portals": PORTAL, "new_name": ""})

    # The panel PUTs the state it captured at page load, textarea and all.
    store["settings"] = {
        "new_name": "weaseltv",
        "new_url": "http://weaseltv.live/c/",
        "portals": "",
    }

    result = p._reconcile_registry(dict(store["settings"]), NullLogger())

    assert result["portals"] == PORTAL, "an empty stale textarea must not erase the list"
    assert registry.load_registry() == PORTAL, "and the file must keep it"


def test_stale_panel_does_not_lose_the_second_portal():
    """Adding a portal while the panel holds the previous list.

    The stale value is non-empty and parses fine, so nothing about the text
    itself gives it away -- only that the plugin has written since.
    """
    reset()

    p, store = make_plugin({"portals": PORTAL})
    both = PORTAL + "second | http://b.example/c/ | 00:1A:79:AA:BB:02 | max_streams=1\n"
    p._save_settings({"portals": both})

    # The panel still holds the one-portal list it loaded with.
    store["settings"] = {"portals": PORTAL}
    result = p._reconcile_registry({"portals": PORTAL}, NullLogger())

    assert result["portals"] == both, "the portal just added must survive"
    assert registry.load_registry() == both


def test_a_reopened_panel_is_authoritative_again():
    """Once the panel quotes the current list back, hand edits work as before."""
    reset()

    p, store = make_plugin({})
    p._save_settings({"portals": PORTAL})
    assert registry.is_pending(), "the panel has not seen this write yet"

    # The user reopens the panel: its state now matches the file.
    store["settings"] = {"portals": PORTAL}
    p._reconcile_registry({"portals": PORTAL}, NullLogger())
    assert not registry.is_pending(), "the panel has caught up"

    # A hand edit made from that reopened panel must stick.
    store["settings"] = {"portals": ""}
    result = p._reconcile_registry({"portals": ""}, NullLogger())
    assert result["portals"] == ""
    assert (registry.load_registry() or "") == ""


def test_a_marker_left_over_from_an_outside_edit_is_ignored():
    """portals.txt changed by hand invalidates the marker rather than freezing it."""
    reset()

    p, _ = make_plugin({})
    p._save_settings({"portals": PORTAL})
    assert registry.is_pending()

    # Someone edits the file directly, or restores a backup.
    registry.save_registry(PORTAL + "# a note\n")
    assert not registry.is_pending(), "the marker no longer describes the file"


if __name__ == "__main__":
    failures = 0
    try:
        for name, fn in sorted(globals().items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures += 1
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + ("ALL REGISTRY TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
