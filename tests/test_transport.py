"""What a portal request carries, and when it is worth making twice.

Two changes share this file because they are the same decision seen from two
sides: what to do about a portal that does not behave like Ministra. One sends
the identity in every form a portal might read it in; the other accepts that a
portal can simply be having a bad minute.

The retry half matters most for what it must *not* do. Retrying at tune time
would keep a dead source alive long enough to stop Dispatcharr failing over to
a working one, and retrying a refused login is how a MAC gets banned.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import stalker_api as s  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Replays a script of answers, recording every call and every sleep."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        answer = self.script.pop(0) if self.script else FakeResponse()
        if isinstance(answer, Exception):
            raise answer
        return answer


def portal(script, retries=0, token="", **cfg_kwargs):
    cfg = s.PortalConfig(
        slug="t", name="T", url="http://p.example/c/portal.php",
        mac="00:1A:79:AA:BB:CC", **cfg_kwargs
    )
    p = s.Portal(cfg, token=token, retries=retries)
    p.session = FakeSession(script)
    return p


def no_sleeping():
    """Swap time.sleep out, collecting what would have been waited.

    Returns the list and the undo, so a backoff can be asserted on without the
    test suite actually spending it.
    """
    slept = []
    original = s.time.sleep
    s.time.sleep = slept.append
    return slept, (lambda: setattr(s.time, "sleep", original))


# -- identity in the query string ---------------------------------------


def test_the_mac_travels_in_the_query_as_well_as_the_cookie():
    p = portal([FakeResponse(payload={"js": {}})])
    p._get_json("action=get_genres")
    _, url, kwargs = p.session.calls[0]
    assert "mac=00%3A1A%3A79%3AAA%3ABB%3ACC" in url, url
    assert "mac=00%3A1A%3A79%3AAA%3ABB%3ACC" in kwargs["headers"]["Cookie"]


def test_the_token_travels_in_the_query_as_well_as_the_header():
    p = portal([FakeResponse(payload={"js": {}})], token="TOK")
    p._get_json("action=get_genres")
    _, url, kwargs = p.session.calls[0]
    assert "token=TOK" in url, url
    assert kwargs["headers"]["Authorization"] == "Bearer TOK"


def test_the_handshake_still_proves_nothing_it_has_not_earned():
    """No token in either place before there is one to send."""
    p = portal([FakeResponse(payload={"js": {"token": "NEW"}})])
    p.handshake()
    _, url, kwargs = p.session.calls[0]
    assert "Authorization" not in kwargs["headers"]
    # handshake sends its own empty 'token=' by protocol; what must not appear
    # is a second, authenticating one appended by the common parameters.
    assert url.count("token=") == 1, url
    assert "mac=" in url, "the MAC is still needed to be recognised"


def test_credentials_are_posted_with_the_identity_on_the_url():
    p = portal([FakeResponse(payload={"js": True})], token="TOK",
               username="joe", password="pw")
    p.authenticate()
    method, url, kwargs = p.session.calls[0]
    assert method == "POST"
    assert "mac=" in url and "token=TOK" in url, url
    # The password stays in the body, where a proxy log will not keep it.
    assert "pw" not in url
    assert kwargs["data"]["password"] == "pw"


# -- retrying -----------------------------------------------------------


def test_nothing_is_retried_by_default():
    """The resolver's setting, and the one that protects failover."""
    import requests

    p = portal([requests.ConnectionError("down"), FakeResponse(payload={"js": {}})])
    try:
        p._get_json("action=get_genres")
    except s.PortalError:
        pass
    else:
        raise AssertionError("a portal that is down must fail on the first try")
    assert len(p.session.calls) == 1, p.session.calls


def test_a_transient_failure_is_ridden_out_when_retries_are_asked_for():
    import requests

    slept, restore = no_sleeping()
    try:
        p = portal(
            [requests.ConnectionError("down"),
             FakeResponse(502, text="bad gateway"),
             FakeResponse(payload={"js": {"ok": 1}})],
            retries=2,
        )
        assert p._get_json("action=get_genres") == {"js": {"ok": 1}}
        assert len(p.session.calls) == 3
        assert slept == [1.0, 2.0], slept
    finally:
        restore()


def test_the_attempts_do_run_out():
    slept, restore = no_sleeping()
    try:
        p = portal([FakeResponse(503, text="busy")] * 3, retries=2)
        try:
            p._get_json("action=get_genres")
        except s.PortalError as exc:
            assert "503" in str(exc), exc
        else:
            raise AssertionError("a portal that never answers must raise")
        assert len(p.session.calls) == 3
    finally:
        restore()


def test_a_refusal_is_never_retried():
    """Repeating a rejected login is how a MAC gets itself banned."""
    slept, restore = no_sleeping()
    try:
        p = portal([FakeResponse(403, text="no")] * 3, retries=2)
        try:
            p._get_json("action=get_genres")
        except s.PortalAuthError:
            pass
        else:
            raise AssertionError("403 must be an auth error")
        assert len(p.session.calls) == 1, p.session.calls
        assert slept == []
    finally:
        restore()


def test_a_verdict_is_never_retried():
    """404 is the portal having made up its mind; asking again changes nothing."""
    slept, restore = no_sleeping()
    try:
        p = portal([FakeResponse(404, text="gone")] * 3, retries=2)
        try:
            p._get_json("action=get_genres")
        except s.PortalError as exc:
            assert "404" in str(exc), exc
        assert len(p.session.calls) == 1, p.session.calls
    finally:
        restore()


def test_prose_instead_of_json_is_not_a_reason_to_ask_again():
    """It arrives with a 200 attached, so only the body says anything is wrong.

    Retrying would also delay the resolver's re-authentication, which is what
    this particular body is supposed to trigger.
    """
    slept, restore = no_sleeping()
    try:
        p = portal([FakeResponse(200, text="Authorization failed.")] * 3, retries=2)
        try:
            p._get_json("action=get_genres")
        except s.PortalAuthError:
            pass
        else:
            raise AssertionError("expected the session to be reported as dead")
        assert len(p.session.calls) == 1, p.session.calls
    finally:
        restore()


def test_the_sync_asks_for_retries_and_the_test_action_does_not():
    """The one asymmetry that matters, pinned so a refactor keeps it.

    'Test portals' runs on the request thread, where three attempts at a
    60-second timeout outlast any proxy in front of Dispatcharr.
    """
    import importlib.util
    import types

    pkg = types.ModuleType("distalker_probe")
    pkg.__path__ = [REPO]
    sys.modules["distalker_probe"] = pkg
    spec = importlib.util.spec_from_file_location(
        "distalker_probe.sync", os.path.join(REPO, "sync.py")
    )
    sync = importlib.util.module_from_spec(spec)
    sys.modules["distalker_probe.sync"] = sync
    spec.loader.exec_module(sync)

    built = []
    original = sync.Portal
    sync.Portal = lambda cfg, **kw: built.append(kw) or original(cfg, **kw)
    _, restore = no_sleeping()  # sync_portal would otherwise back off for real
    try:
        cfg = s.PortalConfig(slug="t", name="T", url="http://p.example/c/portal.php",
                             mac="00:1A:79:AA:BB:CC")
        for call in (lambda: sync.test_portal(cfg), lambda: sync.sync_portal(cfg, _Logger())):
            try:
                call()
            except Exception:
                pass  # no portal is listening; only the construction matters
    finally:
        sync.Portal = original
        restore()

    assert built[0].get("retries", 0) == 0, f"test_portal must not retry: {built[0]}"
    assert built[1].get("retries") == sync.SYNC_RETRIES, built[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


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
    print("\n" + ("ALL TRANSPORT TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
