"""The manifest and the code that serves it.

plugin.json defines the panel; plugin.py implements it. Nothing at import time
checks that the two agree, so a renamed action or a field the code reads but the
manifest never renders fails only when a user clicks the button.

These tests also cover the plumbing run() does around every handler: recording
what happened in the Last action box, folding the handler's settings into one
save, and clearing out the periodic task earlier versions scheduled.
"""
import contextlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="distalker-manifest-")
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

with open(os.path.join(REPO, "plugin.json"), "r", encoding="utf-8") as handle:
    MANIFEST = json.load(handle)

# Straight from Dispatcharr's Plugins.md; anything else the panel will not render.
FIELD_TYPES = {"boolean", "number", "string", "text", "select", "info"}


class NullLogger:
    """Swallows anything a logger is asked to do.

    A catch-all rather than a list of methods: naming them one by one means a
    handler that reaches for a level nobody thought of raises AttributeError
    *inside* run()'s own except clause, and the failure surfaces as the action
    reporting something unrelated -- which is a long way to travel to discover
    that a stub was missing a method.
    """

    def __getattr__(self, _name):
        return lambda *a, **k: None


class FakeTasks:
    """Stands in for the tasks module, recording what it was asked for."""

    def __init__(self, already_running=False):
        self.started = []
        self.removed = 0
        self.already_running = already_running

    def run_sync_in_background(self, full=False):
        if self.already_running:
            return False
        self.started.append({"full": full})
        return True

    def remove_schedule(self):
        self.removed += 1


def make_plugin(raw_settings, already_running=False):
    p = plugin_mod.Plugin()
    store = {"settings": dict(raw_settings)}
    p._write_settings = lambda settings: store.__setitem__("settings", dict(settings))
    p._raw_settings = lambda: dict(store["settings"])

    fake = FakeTasks(already_running=already_running)
    original = plugin_mod.tasks
    plugin_mod.tasks = fake
    plugin_mod._schedule_dropped = False
    store["restore"] = lambda: setattr(plugin_mod, "tasks", original)
    return p, store, fake


def reset():
    for path in (registry.REGISTRY_PATH, registry.PENDING_PATH):
        if os.path.exists(path):
            os.unlink(path)
    if os.path.isdir(s.STATE_DIR):
        shutil.rmtree(s.STATE_DIR)


def publish(line):
    """Pretend a sync has already published this line, as the mirror would."""
    portals, errors = s.parse_portals(line)
    assert not errors, errors
    s._mirror_write(f"portal-{portals[0].slug}", portals[0].to_dict())
    return portals[0]


def run(p, action, settings):
    return p.run(action, {}, {"settings": dict(settings), "logger": NullLogger()})


PORTALS = "http://weaseltv.live/c/ | 00:1A:79:29:53:38\n"


@contextlib.contextmanager
def stubbed():
    """Stand in for the three things that need Dispatcharr or Redis.

    apply_profile is the cheapest handler to use as a vehicle for run()'s own
    plumbing, and the no-op branch of sync republishes -- neither can reach the
    ORM from here.
    """
    originals = (plugin_mod.apply_stream_profile, plugin_mod.save_portal,
                 plugin_mod.publish_fallback)
    plugin_mod.apply_stream_profile = lambda: {"channels": 1, "streams": 1}
    plugin_mod.save_portal = lambda cfg: None
    plugin_mod.publish_fallback = lambda name: ""
    try:
        yield
    finally:
        (plugin_mod.apply_stream_profile, plugin_mod.save_portal,
         plugin_mod.publish_fallback) = originals


# -- manifest / code agreement ------------------------------------------------

def test_every_action_has_a_handler():
    for action in MANIFEST["actions"]:
        assert hasattr(plugin_mod.Plugin, f"_action_{action['id']}"), (
            f"manifest advertises '{action['id']}' with no handler behind it"
        )


def test_every_handler_is_advertised():
    """A handler no action reaches is dead code, or a button someone forgot."""
    advertised = {a["id"] for a in MANIFEST["actions"]}
    for name in dir(plugin_mod.Plugin):
        if name.startswith("_action_"):
            assert name[len("_action_"):] in advertised, f"{name} is unreachable"


def test_field_ids_are_unique_and_typed():
    seen = set()
    for field in MANIFEST["fields"]:
        assert field["id"] not in seen, f"duplicate field id {field['id']}"
        seen.add(field["id"])
        assert field["type"] in FIELD_TYPES, f"{field['id']}: unrenderable type"


def test_the_panel_is_the_portal_list_and_little_else():
    """The form is gone on purpose: twelve boxes for what three fields say.

    Anything the plugin writes into its own settings is invisible until the
    user refreshes the Plugins page, which made a form whose Load button filled
    boxes nobody could see. The textarea has no such problem -- and with the
    plugin no longer writing it, the panel is its only author.
    """
    ids = [f["id"] for f in MANIFEST["fields"]]
    assert "portals" in ids
    assert not [i for i in ids if i.startswith("new_")], "the add form is gone"


def test_the_manifest_and_the_code_agree_on_the_ffmpeg_arguments():
    """Two copies of one string, and they had already drifted: the manifest's
    had lost the MAG headers, and a background sync persists whichever copy it
    is holding -- so the headers stopped being sent for anyone it saved for."""
    default = next(f for f in MANIFEST["fields"] if f["id"] == "ffmpeg_args")["default"]
    assert default == s.DEFAULT_FFMPEG_ARGS


def test_nothing_schedules_a_sync_any_more():
    """A plugin's Celery task cannot be consumed on a stock install -- the
    consumer that resolves a task name is the prefork parent, which never
    imports plugins. Keeping the setting meant a field that promised something
    it could not deliver, and beat publishing into the void twice a day."""
    ids = {f["id"] for f in MANIFEST["fields"]}
    assert "sync_interval_hours" not in ids
    assert not hasattr(plugin_mod.Plugin, "_follow_schedule")
    assert not hasattr(plugin_mod.tasks, "apply_schedule")


def test_an_old_installs_periodic_task_is_deleted():
    """Not creating it no longer removes the one an earlier version left."""
    reset()
    p, store, fake = make_plugin({"portals": PORTALS})
    try:
        with stubbed():
            run(p, "apply_profile", store["settings"])
            assert fake.removed == 1
            run(p, "apply_profile", store["settings"])
            assert fake.removed == 1, "once per process, not once per action"
    finally:
        store["restore"]()


def test_settings_the_code_reads_are_declared():
    ids = {f["id"] for f in MANIFEST["fields"]}
    for key in (
        "portals",
        "status",
        "ffmpeg_args",
        "fallback_profile",
        "portal_timeout",
        "auto_apply_profile",
    ):
        assert key in ids, f"the code reads '{key}' but the panel never renders it"


# -- what run() does around a handler -----------------------------------------

def test_an_action_records_what_it_did():
    reset()
    p, store, _ = make_plugin({"portals": PORTALS})
    try:
        with stubbed():
            result = run(p, "apply_profile", store["settings"])
        assert result["status"] == "ok"
        assert store["settings"]["status"] == result["message"], (
            "the Last action box is the only report that outlives the notification"
        )
    finally:
        store["restore"]()


def test_a_failure_is_recorded_too():
    reset()
    p, store, _ = make_plugin({})
    try:
        result = run(p, "sync_now", {"portals": "nonsense without pipes"})
        assert result["status"] == "error"
        assert "invalid portal configuration" in store["settings"]["status"]
    finally:
        store["restore"]()


def test_settings_never_travel_back_in_the_result():
    """The result becomes an API response; portal passwords live in settings."""
    reset()
    p, store, _ = make_plugin({})
    try:
        result = run(p, "sync_now", {
            "portals": "http://weaseltv.live/c/ | 00:1A:79:29:53:38 | password=hunter2"
        })
        assert "settings" not in result
        assert "hunter2" not in json.dumps(result)
    finally:
        store["restore"]()


def test_a_raised_failure_replaces_the_last_success():
    """Otherwise the box goes on boasting about the run before the broken one."""
    reset()
    p, store, _ = make_plugin({"portals": PORTALS})
    try:
        with stubbed():
            run(p, "apply_profile", store["settings"])
        assert "channel(s)" in store["settings"]["status"]

        result = run(p, "sync_now", {"portals": "nonsense without pipes"})
        assert result["status"] == "error"
        assert "channel(s)" not in store["settings"]["status"]
    finally:
        store["restore"]()


# -- the diff, which is what makes the one button usable ----------------------

def test_a_new_portal_is_the_only_thing_fetched():
    reset()
    publish("http://old.example/c/ | 00:1A:79:00:00:01")
    p, store, fake = make_plugin({})
    try:
        result = run(p, "sync_now", {"portals": (
            "http://old.example/c/ | 00:1A:79:00:00:01\n"
            "http://new.example/c/ | 00:1A:79:00:00:02\n"
        )})
        assert fake.started == [{"full": False}], "the work must be handed off"
        assert "adding new" in result["message"]
        assert "leaving 1 untouched" in result["message"]
        assert "old" not in result["message"].split("adding")[1].split(";")[0]
    finally:
        store["restore"]()


def test_nothing_changed_means_nothing_is_fetched():
    """The whole point: adding a portal must not re-download the others."""
    reset()
    publish("http://old.example/c/ | 00:1A:79:00:00:01")
    p, store, fake = make_plugin({})
    try:
        with stubbed():
            result = run(p, "sync_now", {"portals": "http://old.example/c/ | 00:1A:79:00:00:01"})
        assert fake.started == [], "no portal should be contacted"
        assert "nothing to fetch" in result["message"]
        assert "Re-fetch all" in result["message"], "and the way out must be named"
    finally:
        store["restore"]()


def test_an_edited_portal_is_fetched_again():
    reset()
    publish("http://old.example/c/ | 00:1A:79:00:00:01")
    p, store, fake = make_plugin({})
    try:
        result = run(p, "sync_now", {"portals": "http://old.example/c/ | 00:1A:79:AA:AA:AA"})
        assert fake.started == [{"full": False}]
        assert "re-fetching old" in result["message"], "a new MAC is a different line-up"
    finally:
        store["restore"]()


def test_a_cosmetic_change_costs_nothing():
    """ffmpeg arguments, the timeout and max_streams cannot change a line-up."""
    reset()
    publish("http://old.example/c/ | 00:1A:79:00:00:01")
    p, store, fake = make_plugin({})
    try:
        with stubbed():
            result = run(p, "sync_now", {
                "portals": "http://old.example/c/ | 00:1A:79:00:00:01 | max_streams=3",
                "ffmpeg_args": "-i {url} -c copy -f mpegts pipe:1",
                "portal_timeout": 120,
            })
        assert fake.started == []
        assert "nothing to fetch" in result["message"]
    finally:
        store["restore"]()


def test_a_deleted_line_is_dropped_without_touching_its_channels():
    reset()
    publish("http://gone.example/c/ | 00:1A:79:00:00:09")
    p, store, fake = make_plugin({})
    try:
        result = run(p, "sync_now", {"portals": "http://new.example/c/ | 00:1A:79:00:00:02"})
        assert "dropping gone" in result["message"]
        assert fake.started == [{"full": False}]
    finally:
        store["restore"]()


def test_refetching_everything_ignores_the_diff():
    reset()
    publish("http://old.example/c/ | 00:1A:79:00:00:01")
    p, store, fake = make_plugin({})
    try:
        result = run(p, "resync_all", {"portals": "http://old.example/c/ | 00:1A:79:00:00:01"})
        assert fake.started == [{"full": True}], "the whole point of the second button"
        assert "re-fetching all 1 portal" in result["message"]
    finally:
        store["restore"]()


def test_a_second_press_does_not_start_a_second_sync():
    """Portals commonly allow one connection per MAC; two syncs spend it on each other."""
    reset()
    p, store, fake = make_plugin({}, already_running=True)
    try:
        result = run(p, "sync_now", {"portals": PORTALS})
        assert fake.started == []
        assert result["status"] == "ok", "a no-op, not an error"
        assert "already running" in result["message"]
    finally:
        store["restore"]()


def test_sync_still_refuses_a_mistyped_portal_at_the_click():
    """Parsing is cheap, so a typo must not be discovered later in a thread."""
    reset()
    p, store, fake = make_plugin({})
    try:
        result = run(p, "sync_now", {"portals": "this is not a portal line"})
        assert result["status"] == "error"
        assert fake.started == [], "nothing should be handed off"
    finally:
        store["restore"]()


def test_a_sync_that_did_nothing_does_not_interrupt_anyone():
    """The notification raises a toast, so the rule is the status box's rule:
    say something when something happened, and nothing when nothing did."""
    quiet = {"new": [], "changed": [], "unchanged": ["a"], "removed": []}
    nothing = {"synced": [], "errors": []}
    assert plugin_mod.Plugin._worth_announcing(quiet, nothing, False) is False

    assert plugin_mod.Plugin._worth_announcing(quiet, nothing, True) is True, "asked for"
    assert plugin_mod.Plugin._worth_announcing(
        quiet, {"synced": [{"portal": "a"}], "errors": []}, False
    ) is True
    assert plugin_mod.Plugin._worth_announcing(
        quiet, {"synced": [], "errors": ["a: refused"]}, False
    ) is True
    assert plugin_mod.Plugin._worth_announcing(
        {"new": [], "changed": [], "unchanged": [], "removed": ["gone"]}, nothing, False
    ) is True


def test_a_new_account_says_what_dispatcharr_is_waiting_for():
    """A fresh M3U account stops at Pending Setup until its groups are chosen.
    That is Dispatcharr's flow for any M3U, and nothing on screen ties it to
    the sync that just ran unless the notification says so."""
    fresh = {"synced": [{"portal": "a", "account_created": True}], "errors": []}
    assert "Pending Setup" in plugin_mod.Plugin._announcement("a: 12 channels", fresh)

    known = {"synced": [{"portal": "a", "account_created": False}], "errors": []}
    assert plugin_mod.Plugin._announcement("a: 12 channels", known) == "a: 12 channels"


# -- events -------------------------------------------------------------------

def test_an_event_run_that_changed_nothing_leaves_the_status_box_alone():
    """m3u_refresh fires per account and channel_error per failed tune.

    Both reach apply_profile, and both usually have nothing to do. Recording
    them would bury the result the user is waiting to read -- the sync they
    just started -- under a run they never asked for.
    """
    reset()
    p, store, _ = make_plugin({"status": "synced 412 channels"})
    original = plugin_mod.apply_stream_profile
    plugin_mod.apply_stream_profile = lambda: {"channels": 0, "streams": 0}
    try:
        result = p.run(
            "apply_profile",
            {"event": "channel_error", "payload": {}},
            {"settings": dict(store["settings"]), "logger": NullLogger()},
        )
        assert result["status"] == "ok"
        assert store["settings"]["status"] == "synced 412 channels", (
            "an event that changed nothing must not overwrite the box"
        )
    finally:
        plugin_mod.apply_stream_profile = original
        store["restore"]()


def test_an_event_run_that_repaired_a_channel_is_recorded():
    """The one case worth saying: a channel was tuning against the wrong profile."""
    reset()
    p, store, _ = make_plugin({"status": "synced 412 channels"})
    original = plugin_mod.apply_stream_profile
    plugin_mod.apply_stream_profile = lambda: {"channels": 1, "streams": 0}
    try:
        p.run(
            "apply_profile",
            {"event": "channel_error", "payload": {}},
            {"settings": dict(store["settings"]), "logger": NullLogger()},
        )
        assert "1 channel(s)" in store["settings"]["status"]
    finally:
        plugin_mod.apply_stream_profile = original
        store["restore"]()


def test_a_button_press_is_recorded_even_when_it_changed_nothing():
    """A click is the only answer the user gets; silence would read as a failure."""
    reset()
    p, store, _ = make_plugin({"status": "synced 412 channels"})
    original = plugin_mod.apply_stream_profile
    plugin_mod.apply_stream_profile = lambda: {"channels": 0, "streams": 0}
    try:
        run(p, "apply_profile", dict(store["settings"]))
        assert "0 channel(s)" in store["settings"]["status"]
    finally:
        plugin_mod.apply_stream_profile = original
        store["restore"]()


def test_assigning_the_profile_also_republishes_the_portals():
    """A restart empties Redis, and nothing about the user's setup changed, so
    they have no reason to press Sync. Every path that reaches the assign --
    the button, an M3U refresh, a failed tune -- puts the portals back."""
    reset()
    p, store, _ = make_plugin({"portals": PORTALS})
    published = []
    originals = (plugin_mod.apply_stream_profile, plugin_mod.save_portal,
                 plugin_mod.publish_fallback)
    plugin_mod.apply_stream_profile = lambda: {"channels": 0, "streams": 0}
    plugin_mod.save_portal = lambda cfg: published.append(cfg.slug)
    plugin_mod.publish_fallback = lambda name: ""
    try:
        p.run(
            "apply_profile",
            {"event": "channel_error", "payload": {}},
            {"settings": dict(store["settings"]), "logger": NullLogger()},
        )
        assert published == ["weaseltv"]
    finally:
        (plugin_mod.apply_stream_profile, plugin_mod.save_portal,
         plugin_mod.publish_fallback) = originals
        store["restore"]()


def test_an_unusable_portal_list_does_not_sink_the_assign():
    """The assign is reached by events; a broken list is sync's to report."""
    reset()
    p, store, _ = make_plugin({"portals": "this is not a portal line"})
    original = plugin_mod.apply_stream_profile
    plugin_mod.apply_stream_profile = lambda: {"channels": 2, "streams": 0}
    try:
        result = run(p, "apply_profile", dict(store["settings"]))
        assert result["status"] == "ok"
        assert "2 channel(s)" in result["message"]
    finally:
        plugin_mod.apply_stream_profile = original
        store["restore"]()


def test_actions_only_subscribe_to_events_dispatcharr_emits():
    """A typo in an events list is silent: the action simply never runs."""
    # core.models.SystemEvent.EVENT_TYPES, which is what log_system_event()
    # dispatches from.
    known = {
        "channel_start", "channel_stop", "channel_buffering", "channel_failover",
        "channel_reconnect", "channel_error", "client_connect", "client_disconnect",
        "recording_start", "recording_end", "stream_switch", "m3u_refresh",
        "m3u_download", "epg_refresh", "epg_download", "login_success",
        "login_failed", "logout", "m3u_blocked", "epg_blocked", "vod_start",
        "vod_stop",
    }
    for action in MANIFEST["actions"]:
        for event in action.get("events", []):
            assert event in known, f"{action['id']}: nothing emits '{event}'"


# -- odds and ends ------------------------------------------------------------

def test_a_worker_merges_the_manifest_defaults_itself():
    """A background sync arrives with no panel state, so defaults are its problem."""
    reset()
    p, store, _ = make_plugin({"portals": "x"})
    try:
        merged = p._settings_with_defaults()
        assert merged["portals"] == "x", "stored values win"
        assert merged["portal_timeout"] == 60, "and the rest come from the manifest"
        assert merged["auto_apply_profile"] is True
    finally:
        store["restore"]()


def test_unknown_actions_are_refused():
    reset()
    p, store, _ = make_plugin({})
    try:
        result = run(p, "add_portal", {})
        assert result["status"] == "error", "a removed action must not half-work"
    finally:
        store["restore"]()


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
    print("\n" + ("ALL MANIFEST TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
