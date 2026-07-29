"""Waking the plugin up without a task it is allowed to register.

A plugin cannot own a Celery task on a stock install -- the whole story is in
tasks.py -- so the schedule is borrowed: Dispatcharr refreshes each M3U account
on its own timer, and the m3u_refresh it ends with is dispatched to plugin
actions. Distalker answers that event by re-fetching every portal.

Three guards decide whether an event is allowed to do that, and each covers a
different way the arrangement would otherwise misbehave. They are what this
file pins, because two of them protect other people's installs rather than
this feature: without the account check, refreshing any unrelated playlist
starts a round of portal logins; without the cooldown, the sync's own closing
refresh re-enters the event that started it.
"""
import importlib.util
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import stalker_api as s  # noqa: E402


def load_plugin():
    pkg = types.ModuleType("distalker_sched")
    pkg.__path__ = [REPO]
    pkg.__package__ = "distalker_sched"
    sys.modules["distalker_sched"] = pkg
    spec = importlib.util.spec_from_file_location(
        "distalker_sched.plugin", os.path.join(REPO, "plugin.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["distalker_sched.plugin"] = module
    spec.loader.exec_module(module)
    return module


plugin_mod = load_plugin()

# plugin.py reaches stalker_api through a relative import, so the package has
# its own module object -- a different one from the `stalker_api` imported at
# the top of this file. Patching one does not touch the other, and the copy
# that matters is the one the plugin actually calls into.
plugin_api = sys.modules["distalker_sched.stalker_api"]


class FakeRedis:
    """Enough of redis-py for a SET NX EX, and nothing else."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


class Recorder:
    """A plugin whose sync is replaced by a note that it was asked for."""

    def __init__(self, started=True):
        self.plugin = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
        self.calls = []
        self.started = started

    def resync(self, params, settings):
        original = plugin_mod.tasks.run_sync_in_background
        portals = plugin_mod.Plugin._portals

        def fake_sync(full=False):
            self.calls.append(full)
            return self.started

        plugin_mod.tasks.run_sync_in_background = fake_sync
        plugin_mod.Plugin._portals = lambda self, settings: []
        try:
            return self.plugin._action_resync_all(params, settings, _Logger())
        finally:
            plugin_mod.tasks.run_sync_in_background = original
            plugin_mod.Plugin._portals = portals


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def event(account="Distalker: Mock"):
    return {"event": "m3u_refresh", "payload": {"account_name": account}}


def fresh_redis():
    """Point the cooldown at a Redis nobody else has touched.

    Both module objects, since the plugin's copy is the one that counts and
    the test's own asserts read through the other.
    """
    client = FakeRedis()
    modules = [s, plugin_api]
    originals = [m._client_or_none for m in modules]
    for module in modules:
        module._client_or_none = lambda c=None: client

    def restore():
        for module, original in zip(modules, originals):
            module._client_or_none = original

    return client, restore


# -- the guards ------------------------------------------------------------


def test_a_button_press_always_syncs():
    """No event, no guards: a click is the user asking, and gets an answer."""
    client, restore = fresh_redis()
    try:
        rec = Recorder()
        result = rec.resync({}, {"refresh_hours": 0})
        assert rec.calls == [True], rec.calls
        assert result["changed"] is True
        # Untouched: the cooldown is for the schedule, not for the button.
        assert client.store == {}, client.store
    finally:
        restore()


def test_an_event_does_nothing_until_the_user_asks_for_a_schedule():
    """The default. An upgrade must not start contacting portals by itself."""
    client, restore = fresh_redis()
    try:
        rec = Recorder()
        result = rec.resync(event(), {"refresh_hours": 0})
        assert rec.calls == [], rec.calls
        assert result["changed"] is False
        assert "off" in result["message"], result
    finally:
        restore()


def test_somebody_elses_playlist_is_not_our_business():
    """m3u_refresh fires for every account on the install, not just ours."""
    client, restore = fresh_redis()
    try:
        for name in ("Movies", "", "distalker: lowercase", "Not Distalker: x"):
            rec = Recorder()
            result = rec.resync(event(name), {"refresh_hours": 12})
            assert rec.calls == [], f"{name}: {rec.calls}"
            assert "not one of ours" in result["message"], result
    finally:
        restore()


def test_the_second_event_of_a_cycle_is_the_one_we_caused():
    """The sync ends by asking for a re-read, which re-emits this event.

    The first event syncs; the second lands inside the cooldown and stops
    there. Without this the two would take turns for ever, each round costing
    a login and a full channel download.
    """
    client, restore = fresh_redis()
    try:
        first = Recorder()
        assert first.resync(event(), {"refresh_hours": 12})["changed"] is True
        assert first.calls == [True]

        second = Recorder()
        result = second.resync(event(), {"refresh_hours": 12})
        assert second.calls == [], second.calls
        assert "too recently" in result["message"], result
    finally:
        restore()


def test_a_scheduled_run_refetches_everything():
    """Planned would find nothing: on a schedule the portal list never changed."""
    client, restore = fresh_redis()
    try:
        rec = Recorder()
        rec.resync(event(), {"refresh_hours": 6})
        assert rec.calls == [True], "the schedule must force a full re-fetch"
    finally:
        restore()


def test_a_sync_already_running_is_not_started_twice():
    client, restore = fresh_redis()
    try:
        rec = Recorder(started=False)
        result = rec.resync(event(), {"refresh_hours": 6})
        assert result["changed"] is False
        assert "already running" in result["message"], result
    finally:
        restore()


def test_the_schedule_is_applied_on_the_path_that_fetches_nothing():
    """The one that matters, and the one that was missed.

    Changing the schedule changes no portal, so Sync's plan finds nothing to
    fetch and returns early -- through _republish, not through the sync. Put
    the interval only on the fetching path and the setting appears to do
    nothing at all, which is exactly how it behaved before this test existed.
    """
    calls = []
    originals = (
        plugin_mod.apply_refresh_interval,
        plugin_mod.publish_fallback,
        plugin_mod.Plugin._portals,
    )
    plugin_mod.apply_refresh_interval = lambda hours, logger: calls.append(hours)
    plugin_mod.publish_fallback = lambda name: ""
    plugin_mod.Plugin._portals = lambda self, settings: []
    try:
        plugin = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
        plugin._republish({"refresh_hours": 6}, _Logger())
        assert calls == [6], calls
    finally:
        (plugin_mod.apply_refresh_interval,
         plugin_mod.publish_fallback,
         plugin_mod.Plugin._portals) = originals


def test_only_one_reparse_task_is_ever_dispatched():
    """Both at once put them in a race for the same per-account lock.

    refresh_single_m3u_account refreshes the groups itself. Asking for
    refresh_m3u_groups as well made the loser report "Failed to refresh M3U
    groups" at the user and left the account stuck in Pending Setup -- which
    looked like a portal problem and was not.
    """
    import importlib.util as _u
    import types as _t

    calls = []

    class Task:
        def __init__(self, name):
            self.name = name

        def delay(self, account_id):
            calls.append((self.name, account_id))

    fake_tasks = _t.ModuleType("apps.m3u.tasks")
    fake_tasks.refresh_m3u_groups = Task("refresh_m3u_groups")
    fake_tasks.refresh_single_m3u_account = Task("refresh_single_m3u_account")
    apps = _t.ModuleType("apps")
    m3u = _t.ModuleType("apps.m3u")
    saved = {k: sys.modules.get(k) for k in ("apps", "apps.m3u", "apps.m3u.tasks")}
    sys.modules.update({"apps": apps, "apps.m3u": m3u, "apps.m3u.tasks": fake_tasks})

    spec = _u.spec_from_file_location(
        "distalker_sched.sync", os.path.join(REPO, "sync.py")
    )
    sync = _u.module_from_spec(spec)
    sys.modules["distalker_sched.sync"] = sync
    try:
        spec.loader.exec_module(sync)

        assert sync.request_reparse(7, 0) == "refresh_m3u_groups"
        assert calls == [("refresh_m3u_groups", 7)], calls

        calls.clear()
        assert sync.request_reparse(7, 6) == "refresh_single_m3u_account"
        assert calls == [("refresh_single_m3u_account", 7)], calls
    finally:
        for key, module in saved.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module


# -- the setting -----------------------------------------------------------


def test_the_schedule_is_off_by_default_and_never_below_an_hour():
    """Below an hour the cooldown outlasts the interval, swallowing every
    other run -- a schedule that quietly does half of what it says."""
    read = plugin_mod.Plugin._refresh_hours
    assert read({}) == 0
    assert read({"refresh_hours": 0}) == 0
    assert read({"refresh_hours": ""}) == 0
    assert read({"refresh_hours": "nonsense"}) == 0
    assert read({"refresh_hours": -5}) == 0
    assert read({"refresh_hours": 1}) == 1
    assert read({"refresh_hours": "12"}) == 12


def test_the_cooldown_outlasts_a_sync():
    assert plugin_mod.AUTO_SYNC_COOLDOWN >= 900, plugin_mod.AUTO_SYNC_COOLDOWN


def test_the_action_is_subscribed_to_the_event():
    """Without this in the manifest nothing is ever dispatched here."""
    actions = {a["id"]: a for a in plugin_mod.MANIFEST["actions"]}
    assert "m3u_refresh" in (actions["resync_all"].get("events") or []), actions


def test_the_setting_exists_and_is_off():
    fields = {f["id"]: f for f in plugin_mod.MANIFEST["fields"]}
    assert fields["refresh_hours"]["default"] == 0, fields["refresh_hours"]


# -- the cooldown itself ---------------------------------------------------


def test_the_cooldown_refuses_rather_than_assumes_when_redis_is_gone():
    """The opposite of the sync lock, and on purpose.

    That one guards a button the user just pressed and carries on when Redis
    cannot answer. This one guards against a loop nobody asked for, so silence
    means no.
    """
    original = plugin_api._client_or_none
    plugin_api._client_or_none = lambda c=None: None
    try:
        assert plugin_api.claim_auto_sync() is False
    finally:
        plugin_api._client_or_none = original


def test_the_cooldown_is_claimed_once():
    client, restore = fresh_redis()
    try:
        assert plugin_api.claim_auto_sync(ttl=60) is True
        assert plugin_api.claim_auto_sync(ttl=60) is False
    finally:
        restore()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print("\n" + ("ALL SCHEDULE TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
