"""Surviving Redis.

Dispatcharr's Redis is a cache with no persistence: it comes back empty from
every restart of the container. The resolver reads its portal credentials from
there and cannot ask Django instead, so an empty Redis used to mean every
channel failed with "portal '<slug>' is unknown to Redis" until somebody
thought to press Sync -- observed in the wild, twice, before it was understood.

Everything published now goes to a file as well. These tests pin that the file
is written, that it is read when Redis is empty, unreachable or corrupt, that
Redis is repopulated from it, and that the one thing with no mirror -- the
session token -- degrades to "no cache" rather than raising.
"""
import importlib.util
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="distalker-state-")
os.environ["DISTALKER_DATA_DIR"] = _TMP

sys.path.insert(0, REPO)

import stalker_api as s  # noqa: E402

# resolver.py runs as a script, so it imports as a plain top-level module.
_spec = importlib.util.spec_from_file_location(
    "resolver", os.path.join(REPO, "resolver.py")
)
resolver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolver)

CFG = s.PortalConfig(
    slug="livingroom",
    name="Living Room",
    url="http://portal.example/c/",
    mac="00:1A:79:AA:BB:CC",
    username="jo",
    password="hunter2",
    timeout=90,
)


class FakeRedis:
    """Just enough Redis, with a switch for each way it lets you down."""

    def __init__(self, broken=False):
        self.store = {}
        self.expiry = {}
        self.broken = broken
        self.sets = 0

    def _check(self):
        if self.broken:
            raise ConnectionError("Connection refused")

    def get(self, key):
        self._check()
        return self.store.get(key)

    def set(self, key, value, ex=None, nx=False):
        self._check()
        if nx and key in self.store:
            return None
        self.sets += 1
        self.store[key] = value
        self.expiry[key] = ex
        return True

    def ttl(self, key):
        self._check()
        if key not in self.store:
            return -2
        return self.expiry.get(key) or -1

    def delete(self, *keys):
        self._check()
        for key in keys:
            self.store.pop(key, None)


def reset():
    if os.path.isdir(s.STATE_DIR):
        shutil.rmtree(s.STATE_DIR)


# -- the mirror ---------------------------------------------------------------

def test_publishing_a_portal_writes_the_mirror():
    reset()
    client = FakeRedis()
    s.save_portal(CFG, client)
    assert os.path.exists(s._state_path(f"portal-{CFG.slug}")), (
        "the file is the whole point: Redis will not survive the next restart"
    )


def test_the_mirror_is_not_world_readable():
    """It holds the MAC and, where the provider issued one, the password."""
    reset()
    s.save_portal(CFG, FakeRedis())
    mode = os.stat(s._state_path(f"portal-{CFG.slug}")).st_mode & 0o777
    assert mode == 0o600, f"mode {mode:o}"


def test_a_wiped_redis_still_resolves_the_portal():
    reset()
    client = FakeRedis()
    s.save_portal(CFG, client)

    client.store.clear()  # what a container restart does
    loaded = s.load_portal(CFG.slug, client)

    assert loaded is not None, "the tune must not die because Redis restarted"
    assert loaded.mac == CFG.mac
    assert loaded.password == "hunter2", "credentials must survive intact"
    assert loaded.timeout == 90, "and so must per-portal settings"


def test_reading_from_the_mirror_puts_it_back_in_redis():
    """Otherwise every tune after a restart pays for the file."""
    reset()
    client = FakeRedis()
    s.save_portal(CFG, client)
    client.store.clear()

    s.load_portal(CFG.slug, client)
    assert s._portal_key(CFG.slug) in client.store

    before = client.sets
    s.load_portal(CFG.slug, client)
    assert client.sets == before, "the second read is served by Redis"


def test_an_unreachable_redis_still_resolves_the_portal():
    reset()
    s.save_portal(CFG, FakeRedis())
    assert s.load_portal(CFG.slug, FakeRedis(broken=True)) is not None


def test_corrupt_redis_content_falls_through_to_the_mirror():
    reset()
    client = FakeRedis()
    s.save_portal(CFG, client)
    client.store[s._portal_key(CFG.slug)] = "{not json"

    loaded = s.load_portal(CFG.slug, client)
    assert loaded is not None and loaded.mac == CFG.mac


def test_an_unknown_portal_is_still_unknown():
    reset()
    assert s.load_portal("never-registered", FakeRedis()) is None


def test_removing_a_portal_removes_its_mirror():
    """Otherwise a deleted portal would keep resolving after the next restart."""
    reset()
    client = FakeRedis()
    s.save_portal(CFG, client)
    s.forget_portal(CFG.slug, client)

    assert not os.path.exists(s._state_path(f"portal-{CFG.slug}"))
    assert s.load_portal(CFG.slug, client) is None


def test_the_fallback_command_survives_too():
    reset()
    client = FakeRedis()
    # The client is not optional here: without it save_fallback opens a real
    # connection, which passes on a machine that happens to run Redis and fails
    # in CI, which is the honest environment.
    s.save_fallback("/usr/bin/ffmpeg", "-i {streamUrl}", client)
    client.store.clear()

    spec = s.load_fallback(client)
    assert spec == {"command": "/usr/bin/ffmpeg", "parameters": "-i {streamUrl}"}


def test_a_fallback_nobody_published_is_still_none():
    reset()
    assert s.load_fallback(FakeRedis()) is None


# -- one sync at a time, across four uWSGI workers ----------------------------

def test_only_one_sync_can_claim_the_lock():
    """A flag guards one process; the container runs four workers, and the
    second press lands on whichever is free."""
    client = FakeRedis()
    assert s.claim_sync_lock("first", client=client) is True
    assert s.claim_sync_lock("second", client=client) is False, (
        "two syncs would spend the portal's single connection on each other"
    )


def test_releasing_only_works_for_the_holder():
    """A sync that overran its TTL must not cancel the one that replaced it."""
    client = FakeRedis()
    s.claim_sync_lock("first", client=client)

    s.release_sync_lock("someone-else", client=client)
    assert s.claim_sync_lock("third", client=client) is False, "still held"

    s.release_sync_lock("first", client=client)
    assert s.claim_sync_lock("third", client=client) is True


def test_an_unreachable_redis_does_not_forbid_syncing():
    """None is not False: refusing to sync because a cache is down would be
    worse than the duplicate the lock exists to prevent."""
    assert s.claim_sync_lock("first", client=FakeRedis(broken=True)) is None
    s.release_sync_lock("first", client=FakeRedis(broken=True))  # must not raise


def test_the_lock_carries_how_long_the_sync_has_been_going():
    client = FakeRedis()
    s.claim_sync_lock("first", ttl=1800, client=client)
    assert s.sync_lock_age(ttl=1800, client=client) == 0

    client.expiry[s._sync_lock_key()] = 1500  # five minutes in
    assert s.sync_lock_age(ttl=1800, client=client) == 300

    client.store.clear()
    assert s.sync_lock_age(client=client) is None, "nothing running, nothing to say"


# -- the token, which has no mirror on purpose --------------------------------

def test_the_token_cache_degrades_instead_of_raising():
    """A tune with Redis down should cost a handshake, not fail."""
    broken = FakeRedis(broken=True)
    assert s.get_cached_token(CFG.slug, broken) is None
    s.set_cached_token(CFG.slug, "abc", 3600, broken)  # must not raise
    s.clear_cached_token(CFG.slug, broken)  # nor this


def test_the_token_is_not_written_to_disk():
    """It expires within the hour; a stale one on disk would only mislead."""
    reset()
    s.set_cached_token(CFG.slug, "abc", 3600, FakeRedis())
    assert not os.path.isdir(s.STATE_DIR) or not [
        name for name in os.listdir(s.STATE_DIR) if "token" in name
    ]


def test_a_channel_the_portal_refuses_still_leaves_a_usable_token():
    """The handshake worked; only the link did not.

    Caching after the link would mean a portal refusing one channel -- a
    connection limit, a subscription gap -- sent every later tune through a
    fresh handshake to learn the same thing.
    """
    reset()
    client = FakeRedis()
    s.save_portal(CFG, client)

    class Refusing(s.Portal):
        def login(self):
            self.token = "fresh"
            return self.token

        def create_link(self, cmd):
            raise s.PortalError("connection limit reached")

    original_portal, original_redis = s.Portal, s.get_redis
    s.Portal, s.get_redis = Refusing, lambda: client
    try:
        resolver.resolve(CFG.slug, "ffmpeg http://localhost/ch/1_")
    except s.PortalError:
        pass
    else:
        raise AssertionError("the refusal must still reach the caller")
    finally:
        s.Portal, s.get_redis = original_portal, original_redis

    assert s.get_cached_token(CFG.slug, client) == "fresh"


# -- the interpreter the stream profile is built with -------------------------

def test_a_python_is_chosen_even_when_the_sync_runs_under_uwsgi():
    """Since 0.8.2 the sync runs in the uWSGI process, where sys.executable
    is the uwsgi binary. It happens to run the resolver anyway; relying on
    that is another matter."""
    original = sys.executable
    fake_bin = os.path.join(_TMP, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    python = os.path.join(fake_bin, "python3")
    with open(python, "w") as handle:
        handle.write("")
    try:
        sys.executable = os.path.join(fake_bin, "uwsgi")
        assert s.python_executable() == python
    finally:
        sys.executable = original


def test_a_real_interpreter_is_left_alone():
    original = sys.executable
    try:
        sys.executable = "/usr/local/bin/python3.13"
        assert s.python_executable() == "/usr/local/bin/python3.13"
    finally:
        sys.executable = original


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
    print("\n" + ("ALL STATE TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
