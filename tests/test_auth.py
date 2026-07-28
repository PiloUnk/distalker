"""Which authentication a portal gets, and who decides.

The portal decides. ``get_profile`` answers with a ``status`` that says whether
the session is good (0), needs credentials presented first (2), or is refused
(anything else) -- and the refusal carries the provider's own wording, which is
the only part of it worth showing a user.

What these tests mostly pin down is the tolerance around that machine, because
that is where a strict reading breaks real installs: most portals this plugin
meets are not Ministra, and they answer with less than Ministra would.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import stalker_api as s  # noqa: E402


def portal(replies, **cfg_kwargs):
    """A Portal whose HTTP layer is a script of canned answers.

    ``replies`` maps an action to a payload, or to a callable taking the query
    string, or to an exception instance to raise. Every request made is
    recorded on ``portal.queries``.
    """
    cfg = s.PortalConfig(
        slug="t", name="T", url="http://p.example/c/portal.php",
        mac="00:1A:79:AA:BB:CC", **cfg_kwargs
    )
    p = s.Portal(cfg)
    p.queries = []

    def fake_get_json(query, with_auth=True):
        p.queries.append(query)
        action = ""
        for field in query.split("&"):
            if field.startswith("action="):
                action = field.split("=", 1)[1]
        reply = replies.get(action, {"js": {}})
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(query)
        return reply

    p._get_json = fake_get_json
    p.authenticate = lambda: p.queries.append("POST action=do_auth")
    return p


HANDSHAKE = {"js": {"token": "TOK", "not_valid": 1}}


def test_status_zero_is_authenticated():
    p = portal({"handshake": HANDSHAKE, "get_profile": {"js": {"status": 0, "id": 7}}})
    assert p.login() == "TOK"
    assert p.auth_method == "profile"
    assert not p.warnings


def test_a_profile_without_a_status_is_taken_as_fine():
    """The clone case, and the one a strict reading would break.

    pvr.stalker treats a missing status as a failure. Portals that answer with
    a bare profile -- no status anywhere -- are common, they worked before this
    machine existed, and they must keep working.
    """
    p = portal({"handshake": HANDSHAKE, "get_profile": {"js": {"id": 42, "fname": "x"}}})
    assert p.login() == "TOK"
    assert not p.warnings


def test_status_two_runs_do_auth_then_the_second_step():
    seen = []

    def profile(query):
        second = "auth_second_step=1" in query
        seen.append(second)
        return {"js": {"status": 0 if second else 2}}

    p = portal(
        {"handshake": HANDSHAKE, "get_profile": profile},
        username="joe", password="pw",
    )
    p.login()
    assert seen == [False, True], seen
    assert "POST action=do_auth" in p.queries
    assert p.auth_method == "credentials"


def test_status_two_without_credentials_says_what_to_add():
    """The case the old guess could not see at all.

    Without credentials on the line the previous version ran a device-ID step,
    got a shrug, and carried on to fetch an empty channel list. The user was
    then told to check their MAC address, which was not the problem.
    """
    p = portal({"handshake": HANDSHAKE, "get_profile": {"js": {"status": 2}}})
    try:
        p.login()
    except s.PortalAuthError as exc:
        assert "username" in str(exc) and "password" in str(exc), exc
    else:
        raise AssertionError("a portal asking for credentials must not be ignored")


def test_the_portal_gets_the_last_word_on_why():
    p = portal({
        "handshake": HANDSHAKE,
        "get_profile": {"js": {"status": 1, "msg": "generic",
                               "block_msg": "Subscription expired on 12/06"}},
    })
    try:
        p.login()
    except s.PortalAuthError as exc:
        # block_msg beats msg: it is the specific one, written for this case.
        assert str(exc) == "Subscription expired on 12/06", exc
    else:
        raise AssertionError("status 1 must refuse the session")


def test_a_second_step_that_still_fails_is_refused():
    p = portal(
        {"handshake": HANDSHAKE,
         "get_profile": {"js": {"status": 2, "msg": "bad credentials"}}},
        username="joe", password="wrong",
    )
    try:
        p.login()
    except s.PortalAuthError as exc:
        assert "bad credentials" in str(exc), exc
    else:
        raise AssertionError("credentials the portal keeps rejecting must raise")


def test_a_portal_with_no_get_profile_still_logs_in():
    """MAC-only portals that never implemented it, warned about but served."""
    p = portal({
        "handshake": HANDSHAKE,
        "get_profile": s.PortalError("portal returned HTTP 404"),
    })
    assert p.login() == "TOK"
    assert p.auth_method == "handshake only"
    assert any("404" in w for w in p.warnings), p.warnings


def test_an_explicit_refusal_is_never_downgraded_to_a_warning():
    """The difference between 'did not answer' and 'answered no'."""
    p = portal({
        "handshake": HANDSHAKE,
        "get_profile": s.PortalAuthError("portal refused the session (HTTP 403)"),
    })
    try:
        p.login()
    except s.PortalAuthError:
        pass
    else:
        raise AssertionError("a refusal must not be swallowed as a warning")


def test_not_valid_travels_back_as_not_valid_token():
    p = portal({"handshake": HANDSHAKE, "get_profile": {"js": {"status": 0}}})
    p.login()
    profile_query = [q for q in p.queries if "action=get_profile" in q][0]
    assert "not_valid_token=1" in profile_query, profile_query

    p = portal({"handshake": {"js": {"token": "TOK", "not_valid": 0}},
                "get_profile": {"js": {"status": 0}}})
    p.login()
    profile_query = [q for q in p.queries if "action=get_profile" in q][0]
    assert "not_valid_token=0" in profile_query, profile_query


def test_the_whole_stb_identity_is_sent():
    """Every field libstalkerclient sends, signature included.

    signature was a documented setting that no request ever carried, so a user
    who set it was configuring nothing.
    """
    p = portal({"handshake": HANDSHAKE, "get_profile": {"js": {"status": 0}}},
               signature="a" * 64, serial_number="SN1", model="MAG322")
    p.login()
    query = [q for q in p.queries if "action=get_profile" in q][0]
    for expected in ("signature=" + "a" * 64, "sn=SN1", "stb_type=MAG322",
                     "num_banks=1", "image_version=216", "hd=1", "ver=", "hw_version="):
        assert expected in query, f"{expected} missing from {query}"


def test_a_dead_session_in_plain_text_is_an_auth_error():
    """Ministra answers 200 with prose, not JSON, once a token has expired.

    Typed, because the resolver's cached-token path re-authenticates on it,
    and because the retry work still to come must not retry it.
    """
    import json as _json

    class FakeResponse:
        status_code = 200
        text = "Authorization failed."

        def json(self):
            raise _json.JSONDecodeError("no", "Authorization failed.", 0)

    cfg = s.PortalConfig(slug="t", name="T", url="http://p.example/c/portal.php",
                         mac="00:1A:79:AA:BB:CC")
    p = s.Portal(cfg)
    p.session.get = lambda *a, **k: FakeResponse()
    try:
        p._get_json("action=get_all_channels")
    except s.PortalAuthError as exc:
        assert "no longer authorised" in str(exc), exc
    else:
        raise AssertionError("'Authorization failed.' must be typed as an auth error")


def test_create_link_expiry_is_typed_so_the_resolver_can_recover():
    """A dead token is usually a hollow success, not a refusal.

    The resolver's optimistic path re-authenticates on PortalAuthError alone,
    so these two have to carry that type or the cached-token path could never
    recover from the one thing it exists to survive.
    """
    p = portal({})
    for payload in ({"js": False}, {"js": {"cmd": ""}}, {"js": {"cmd": "   "}}):
        p._get_json = lambda q, with_auth=True, _p=payload: _p
        try:
            p.create_link("ffmpeg http://x/1")
        except s.PortalAuthError:
            pass
        else:
            raise AssertionError(f"{payload} must be an auth error")


def test_a_reply_that_is_simply_not_a_link_is_not_an_auth_error():
    """Re-authenticating cannot turn prose into a URL, so it must not try."""
    p = portal({})
    p._get_json = lambda q, with_auth=True: {"js": {"cmd": "no link here"}}
    try:
        p.create_link("x")
    except s.PortalAuthError:
        raise AssertionError("an unusable command must not trigger a re-login")
    except s.PortalError:
        pass


def test_an_auth_error_is_still_a_portal_error():
    """Callers that only catch PortalError must not start leaking exceptions."""
    assert issubclass(s.PortalAuthError, s.PortalError)


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
    print("\n" + ("ALL AUTH TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
