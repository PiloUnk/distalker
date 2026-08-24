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

PORTAL = "livingroom | http://portal.example/c/ | 00:1A:79:AA:BB:CC | max_streams=1\n"


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
    p, store = make_plugin({"new_name": "livingroom"})
    merged = {"new_name": "livingroom", "portals": ""}   # defaults merged by the loader

    result = p._reconcile_registry(merged, NullLogger())

    assert result["portals"] == PORTAL, "the clobbered list must come back"
    assert store["settings"]["portals"] == s.mask_portals(PORTAL), (
        "and be written back to the DB, redacted -- the row is what the API serves"
    )
    portals, errors = s.parse_portals(result["portals"])
    assert not errors and [x.name for x in portals] == ["livingroom"]


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
    store["settings"] = {"new_name": "livingroom", "new_url": "http://portal.example/c/"}

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
        "new_name": "livingroom",
        "new_url": "http://portal.example/c/",
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


# -- redaction: the row the API serves is not the list the plugin works from --

def test_the_settings_row_never_holds_a_credential():
    """Dispatcharr serves the settings row to every account on the install, and
    the panel paints it into a textarea. Neither is a place for a MAC."""
    reset()
    p, store = make_plugin({})
    p._save_settings({"portals": PORTAL})

    assert "00:1A:79:AA:BB:CC" not in store["settings"]["portals"]
    assert s.is_masked(store["settings"]["portals"])
    assert registry.load_registry() == PORTAL, "the file keeps the real thing"


def test_the_panel_sending_the_redaction_back_changes_nothing():
    """The panel can only return what it was shown. That must read as 'no
    change', not as the user having replaced every MAC with bullets."""
    reset()
    registry.save_registry(PORTAL)
    masked = s.mask_portals(PORTAL)

    p, _ = make_plugin({"portals": masked})
    result = p._reconcile_registry({"portals": masked}, NullLogger())

    assert result["portals"] == PORTAL, "handlers must be given the real list"
    assert registry.load_registry() == PORTAL, "and the file must be untouched"
    assert not registry.is_pending(), "the panel is in step with the file"


def test_reopening_the_panel_does_not_rewrite_the_file():
    """A line that never named its portal comes back from the redaction with
    the derived name written out, because that is what makes the MAC's position
    findable. That is the rendering's doing, not an edit, and reading it as one
    would rewrite the user's file on every click."""
    reset()
    unnamed = "http://portal.example/c/ | 00:1A:79:AA:BB:CC | epg=1\n"
    registry.save_registry(unnamed)
    shown = s.mask_portals(unnamed)
    assert shown.startswith("portal | "), "the derived name is written out"

    p, _ = make_plugin({"portals": shown})
    result = p._reconcile_registry({"portals": shown}, NullLogger())

    assert result["portals"] == unnamed
    assert registry.load_registry() == unnamed, "the file must be left alone"
    assert not registry.is_pending()


def test_an_edit_made_through_the_redaction_keeps_the_hidden_half():
    """Changing a portal's URL while its MAC is hidden is the ordinary case,
    and the MAC the user cannot see must survive it."""
    reset()
    registry.save_registry(PORTAL)

    edited = s.mask_portals(PORTAL).replace("http://portal.example/c/",
                                            "http://moved.example/c/")
    p, store = make_plugin({"portals": edited})
    result = p._reconcile_registry({"portals": edited}, NullLogger())

    assert "http://moved.example/c/" in result["portals"], "the edit must stick"
    assert "00:1A:79:AA:BB:CC" in result["portals"], "and the MAC must come back"
    assert registry.load_registry() == result["portals"]
    assert s.is_masked(store["settings"]["portals"]), "the row stays redacted"


def test_an_upgrade_hides_credentials_the_row_already_holds():
    """Installs coming from an earlier version have their MAC in the row. A
    panel that only ever presses Sync never edits the box, so nothing else
    would ever rewrite it."""
    reset()
    registry.save_registry(PORTAL)

    p, store = make_plugin({"portals": PORTAL})       # as an older version left it
    result = p._reconcile_registry({"portals": PORTAL}, NullLogger())

    assert result["portals"] == PORTAL
    assert s.is_masked(store["settings"]["portals"]), "the row must be redacted now"
    assert registry.load_registry() == PORTAL


def test_nothing_is_hidden_until_the_file_is_known_to_hold_it():
    """Redacting is only safe once there are two copies. A registry that could
    not be written leaves the row as the only one there is, and hiding the only
    copy of a credential is how a configuration gets lost."""
    reset()
    original = plugin_mod.save_registry
    plugin_mod.save_registry = lambda text, pending=False: False
    try:
        p, store = make_plugin({"portals": PORTAL})
        result = p._reconcile_registry({"portals": PORTAL}, NullLogger())
        assert result["portals"] == PORTAL
        assert store["settings"]["portals"] == PORTAL, "still the only copy"
    finally:
        plugin_mod.save_registry = original


def test_a_redaction_is_never_written_over_the_real_list():
    """The panel holds bullets and the file that explains them is gone -- a
    recreated volume, a partial restore. Adopting what the panel sends would
    make the loss permanent by writing the bullets into the file."""
    reset()
    masked = s.mask_portals(PORTAL)

    p, store = make_plugin({"portals": masked})
    result = p._reconcile_registry({"portals": masked}, NullLogger())

    assert s.is_masked(result["portals"]), "nothing can fill these back in"
    assert registry.load_registry() is None, "and nothing may be written"
    assert store["settings"]["portals"] == masked, "the row is left as it was"


def test_a_line_that_could_not_be_filled_back_in_is_named_and_refused():
    """Parsing on would report a MAC address made of bullets, which explains
    nothing. The line itself is quoted back instead, because retyping it is
    the only thing that fixes this."""
    reset()
    masked = s.mask_portals(PORTAL)
    p, _ = make_plugin({})
    try:
        p._portals({"portals": masked})
    except plugin_mod.PortalError as exc:   # plugin.py holds its own import
        assert "livingroom" in str(exc), exc
        assert registry.REGISTRY_PATH in str(exc), exc
        assert "retype" in str(exc), exc
    else:
        raise AssertionError("a redacted list must not be parsed")


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
