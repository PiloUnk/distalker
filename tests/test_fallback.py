"""Sources on a Distalker channel that are not the portal's.

Dispatcharr resolves the stream profile from the channel, never from the source
being played, so a channel listing a portal source alongside an Xtream one
sends both to the resolver. Refusing the second would kill the channel exactly
when Dispatcharr failed over to it, which is the moment it is needed.

These tests pin the sorting -- ours from theirs -- and the command built for
theirs, including the two ways the published fallback can be unusable.
"""
import importlib.util
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import stalker_api as s  # noqa: E402

# resolver.py runs as a script, so it imports as a plain top-level module.
spec = importlib.util.spec_from_file_location("resolver", os.path.join(REPO, "resolver.py"))
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)

PORTAL_URL = s.encode_pseudo_url("livingroom", "ffmpeg http://portal.example/live/1")
XTREAM_URL = "http://xtream.example:8080/live/user/pass/123.ts"


class FakeRedis:
    """Just enough of redis-py for the two calls these helpers make."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, **kwargs):
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)


# -- telling the sources apart ------------------------------------------------

def test_our_own_urls_are_recognised():
    assert s.is_pseudo_url(PORTAL_URL)
    slug, cmd = s.decode_pseudo_url(PORTAL_URL)
    assert slug == "livingroom"


def test_another_providers_url_is_not_ours():
    assert not s.is_pseudo_url(XTREAM_URL)
    assert not s.is_pseudo_url("https://example.com/stream.m3u8")
    assert not s.is_pseudo_url("")


def test_a_malformed_url_does_not_raise():
    """urlparse rejects some hosts outright; that must read as 'not ours'."""
    assert not s.is_pseudo_url("http://[not-an-address/live.ts")


# -- publishing and reading the fallback --------------------------------------

def test_the_fallback_survives_the_redis_roundtrip():
    client = FakeRedis()
    s.save_fallback("/usr/bin/ffmpeg", "-i {streamUrl} -c copy -f mpegts pipe:1", client)
    spec = s.load_fallback(client)
    assert spec["command"] == "/usr/bin/ffmpeg"
    assert "{streamUrl}" in spec["parameters"], "placeholders expand at tune time, not here"


def test_an_unpublished_fallback_reads_as_none():
    assert s.load_fallback(FakeRedis()) is None


def test_an_empty_command_reads_as_none():
    """publish_fallback writes one to clear a fallback it could not honour."""
    client = FakeRedis()
    s.save_fallback("", "", client)
    assert s.load_fallback(client) is None


def test_corrupt_json_reads_as_none():
    client = FakeRedis()
    client.store[f"{s.REDIS_PREFIX}:fallback"] = "{not json"
    assert s.load_fallback(client) is None


# -- the command the resolver builds for someone else's source ----------------

def test_the_published_profile_is_used_with_its_placeholders_expanded():
    spec = {"command": "/usr/bin/ffmpeg", "parameters": "-user_agent {userAgent} -i {streamUrl} -c copy"}
    cmd = resolver.build_fallback_command(spec, XTREAM_URL, "TiviMate")
    assert cmd == [
        "/usr/bin/ffmpeg",
        "-user_agent", "TiviMate",
        "-i", XTREAM_URL,
        "-c", "copy",
    ]


def test_no_published_profile_falls_back_to_plain_ffmpeg():
    cmd = resolver.build_fallback_command(None, XTREAM_URL, "TiviMate")
    assert cmd[0] == "ffmpeg"
    assert XTREAM_URL in cmd and "TiviMate" in cmd
    assert "pipe:1" in cmd, "Dispatcharr reads the stream from stdout"


def test_a_profile_pointing_back_here_is_refused():
    """Naming Distalker as its own fallback would fork this script forever."""
    spec = {"command": "/usr/bin/python3", "parameters": "/data/plugins/distalker/resolver.py {streamUrl} {userAgent}"}
    cmd = resolver.build_fallback_command(spec, XTREAM_URL, "TiviMate")
    assert cmd[0] == "ffmpeg", "the loop must be broken, not entered"
    assert "resolver.py" not in " ".join(cmd)


def test_unparseable_parameters_do_not_kill_the_source():
    spec = {"command": "/usr/bin/ffmpeg", "parameters": '-i {streamUrl} -metadata title="unclosed'}
    cmd = resolver.build_fallback_command(spec, XTREAM_URL, "")
    assert cmd[0] == "ffmpeg", "a broken profile must still play the stream"


def test_the_builtin_command_reports_its_statistics_too():
    """A source played through here reaches Dispatcharr the same way a portal
    one does -- as our stderr -- so it gets the same treatment: without these
    two flags ffmpeg says nothing the statistics panel can be filled from."""
    cmd = resolver.build_fallback_command(None, XTREAM_URL, "UA")
    assert "info" in cmd[cmd.index("-loglevel") + 1]
    assert "-stats" in cmd


def test_a_missing_user_agent_is_not_the_literal_placeholder():
    cmd = resolver.build_fallback_command(None, XTREAM_URL, "")
    assert "{userAgent}" not in " ".join(cmd)


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
    print("\n" + ("ALL FALLBACK TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
