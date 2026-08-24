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
                     "num_banks=1", "image_version=216", "hd=1", "ver=", "hw_version=",
                     # The two other clients send these and this one did not.
                     # Harmless to a portal that ignores them, and the shape a
                     # reseller's access filter looks for in one that does not.
                     "client_type=STB", "video_out=hdmi"):
        assert expected in query, f"{expected} missing from {query}"


def test_the_box_is_described_where_the_admin_panel_reads_it():
    """'metrics' is what a portal stores and shows its operator.

    Echoing the handshake's nonce back inside it is the part a portal could
    actually check: it issued that value one request ago, and only something
    that read the answer can quote it.
    """
    p = portal({"handshake": {"js": {"token": "TOK", "random": "R1"}},
                "get_profile": {"js": {"status": 0}}},
               serial_number="SN1", model="MAG322")
    p.login()
    query = [q for q in p.queries if "action=get_profile" in q][0]
    for expected in ("%22random%22%3A%22R1%22", "%22model%22%3A%22MAG322%22",
                     "%22sn%22%3A%22SN1%22", "%22type%22%3A%22STB%22"):
        assert expected in query, f"{expected} missing from {query}"


def test_the_prehash_is_this_box_rather_than_every_box():
    """A constant shared by every user of one client is the version that fails.

    Nothing in Ministra reads it; an access_filter.php in front of it can, and
    that is the whole reason to send one at all.
    """
    p = portal({"handshake": HANDSHAKE, "get_profile": {"js": {"status": 0}}})
    p.login()
    expected = "prehash=" + s.prehash("00:1A:79:AA:BB:CC")
    assert any(expected in q for q in p.queries if "action=handshake" in q), p.queries
    assert any(expected in q for q in p.queries if "action=get_profile" in q), p.queries
    # The MAC, not the box, and not case-sensitive about how it was written.
    assert s.prehash("00:1a:79:aa:bb:cc") == s.prehash("00:1A:79:AA:BB:CC")
    assert len(s.prehash("00:1A:79:AA:BB:CC")) == 40


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
    p.session.request = lambda *a, **k: FakeResponse()
    try:
        p._get_json("action=get_all_channels")
    except s.PortalAuthError as exc:
        assert "no longer authorised" in str(exc), exc
    else:
        raise AssertionError("'Authorization failed.' must be typed as an auth error")


def test_every_shape_a_refusal_arrives_in():
    """The table, so a shape met next is added here rather than argued about."""
    for body in ("Authorization failed.",
                 # The counter the stock server appends. The exact comparison
                 # this replaces did not recognise it, and it is the refusal
                 # the resolver exists to recover from.
                 "Authorization failed. 75",
                 "authorization failed",
                 "Access denied.",
                 "  Unauthorized request.\n"):
        assert s.auth_refusal(body), body

    for body in ("",
                 # A proxy or WAF page: 38 characters, so only matching the
                 # whole body keeps it out -- and it must stay out, or a host
                 # that never answered sends the resolver re-authenticating.
                 "<html><body>Access denied</body></html>",
                 "Access denied by policy",
                 "ffmpeg http://host/stream"):
        assert not s.auth_refusal(body), body


def test_a_refusal_can_arrive_as_perfectly_good_json():
    """Panels that are not Ministra refuse inside the envelope, not instead of it.

    Every one of these used to read as an ordinary reply: get_genres answered
    this way counted as a portal with no genres, and 'Test portals' reported it
    as authenticated with 0 groups.
    """
    assert s.envelope_refusal({"js": "Authorization failed."})
    assert s.envelope_refusal({"js": {"msg": "Access denied."}})
    # The panel's own wording is what gets quoted back.
    assert "Invalid token" in s.envelope_refusal({"js": {"error": "Invalid token"}})


def test_a_portal_asking_for_a_password_is_not_a_portal_refusing():
    """The one false positive that would cost a working install.

    'msg' is where a status-2 reply writes the sentence login() reads to know
    it should call do_auth. Reading it here would turn every portal that says
    'Authorization required' into a hard refusal and skip the step it asked
    for; a status-1 'msg' is the provider's own wording, which login() quotes
    better than a substitute could.
    """
    assert not s.envelope_refusal({"js": {"status": 2, "msg": "Authorization required"}})
    assert not s.envelope_refusal({"js": {"status": 1, "msg": "Access denied."}})
    # A reply that worked carries no refusal at all, in any field.
    assert not s.envelope_refusal({"js": {"data": [], "total_items": 0}})
    assert not s.envelope_refusal({"js": [{"id": "1", "title": "All"}]})


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


def test_the_other_endpoint_is_tried_when_this_one_is_not_an_api():
    """Ministra answers on two paths and installs expose different ones."""
    tried = []

    def handshake(query):
        tried.append(len(tried))
        if len(tried) == 1:
            raise s.PortalEndpointError("portal returned HTTP 404")
        return HANDSHAKE

    p = portal({"handshake": handshake, "get_profile": {"js": {"status": 0}}})
    assert p.login() == "TOK"
    assert p.url == "http://p.example/server/load.php", p.url
    # The configured URL is untouched, because logos are resolved against it.
    assert p.cfg.url == "http://p.example/c/portal.php"
    assert any("does at" in w for w in p.warnings), p.warnings


def test_a_portal_that_is_simply_down_is_not_asked_twice():
    """Both paths live on one host; a second try buys nothing but delay."""
    p = portal({"handshake": s.PortalError("request to portal failed: refused")})
    try:
        p.login()
    except s.PortalError as exc:
        assert "refused" in str(exc), exc
    assert len([q for q in p.queries if "handshake" in q]) == 1, p.queries


def test_when_neither_endpoint_answers_the_configured_one_is_blamed():
    p = portal({"handshake": s.PortalEndpointError("portal returned HTTP 404")})
    try:
        p.login()
    except s.PortalError as exc:
        assert "404" in str(exc), exc
    # Reset, so nothing downstream reports a path the user never wrote.
    assert p.url == p.cfg.url, p.url


def test_the_two_endpoints_map_onto_each_other():
    cases = {
        "http://h/c/portal.php": "http://h/server/load.php",
        "http://h/stalker_portal/c/portal.php": "http://h/stalker_portal/server/load.php",
        "http://h/server/load.php": "http://h/c/portal.php",
        "http://h:8080/c/portal.php": "http://h:8080/server/load.php",
        # Nothing sensible to swap to.
        "http://h/something.cgi": "",
    }
    for given, expected in cases.items():
        assert s.alternate_endpoint(given) == expected, given


def test_there_is_no_watchdog():
    """Removed rather than left dead: nothing here can call one.

    Keeping a Stalker session warm needs something alive between requests. The
    sync is a task that ends and the resolver becomes ffmpeg, so a ping method
    sat uncalled for two releases, reading as a feature that existed.
    """
    assert not hasattr(s.Portal, "watchdog")


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
