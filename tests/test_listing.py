"""Reading a portal's channel list.

What arrives is a Ministra response only on the portals that are Ministra.
Everywhere else it is a rough approximation of one, and the parsing has to
survive rows that are missing fields, logos in shapes that are not filenames,
and -- once a portal declines to answer get_all_channels at all -- a listing
that has to be collected a page at a time.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import stalker_api as s  # noqa: E402


def portal(**cfg_kwargs):
    cfg = s.PortalConfig(
        slug="t", name="T", url="http://p.example/c/portal.php",
        mac="00:1A:79:AA:BB:CC", **cfg_kwargs
    )
    return s.Portal(cfg)


def row(**overrides):
    base = {"id": "1", "name": "One", "cmd": "ffmpeg http://x/1", "number": "1"}
    base.update(overrides)
    return base


# -- logos ---------------------------------------------------------------


def test_a_bare_filename_is_resolved_against_the_portal():
    assert portal().logo_url("alpha.png") == (
        "http://p.example/c/misc/logos/320/alpha.png"
    )


def test_an_absolute_logo_is_left_alone():
    for url in ("http://cdn.example/a.png", "https://cdn.example/a.png"):
        assert portal().logo_url(url) == url


def test_a_logo_on_some_other_scheme_is_still_a_url():
    """It used to be treated as a filename and glued behind the logo path."""
    assert portal().logo_url("ftp://cdn.example/a.png") == "ftp://cdn.example/a.png"


def test_an_inline_image_is_dropped_rather_than_mangled():
    """Valid, and useless here: it lands in a URL field on Dispatcharr's side.

    The old test was that a logo starts with http, so this went through the
    'must be a filename' branch and produced .../misc/logos/320/data:image...
    """
    assert portal().logo_url("data:image/png;base64,iVBORw0KGgo=") == ""
    assert portal().logo_url("DATA:image/png;base64,iVBORw0KGgo=") == ""


def test_nothing_stays_nothing():
    assert portal().logo_url("") == ""
    assert portal().logo_url("   ") == ""


# -- rows ----------------------------------------------------------------


def test_a_row_becomes_a_channel():
    channel = s.Portal._channel_from_row(
        row(logo="a.png", tv_genre_id="7")
    )
    assert (channel.channel_id, channel.name, channel.number) == ("1", "One", "1")
    assert (channel.logo, channel.genre_id) == ("a.png", "7")


def test_rows_that_are_not_channels_are_skipped():
    for bad in (None, [], "nope", row(cmd=""), row(name=""), row(name="   ")):
        assert s.Portal._channel_from_row(bad) is None, bad


def test_catch_up_is_read_off_the_row():
    """Read, not published -- see ChannelEntry. This pins that it arrives."""
    channel = s.Portal._channel_from_row(
        row(enable_tv_archive=1, tv_archive_duration=7)
    )
    assert channel.tv_archive is True
    assert channel.tv_archive_duration == "7"

    # Portals write the flag as a string about as often as as a number.
    assert s.Portal._channel_from_row(row(enable_tv_archive="1")).tv_archive is True
    for off in (0, "0", "", None):
        assert s.Portal._channel_from_row(row(enable_tv_archive=off)).tv_archive is False


def test_a_channel_without_catch_up_says_so_quietly():
    channel = s.Portal._channel_from_row(row())
    assert channel.tv_archive is False and channel.tv_archive_duration == ""


def test_the_playlist_does_not_advertise_catch_up():
    """The badge would be a promise Dispatcharr cannot keep for a portal.

    Its catch-up player builds Xtream URLs from a server address and
    credentials a Distalker source has none of, so a channel flagged here
    would show the indicator and then fail to play back.
    """
    import importlib.util
    import types

    pkg = types.ModuleType("distalker_listing")
    pkg.__path__ = [REPO]
    sys.modules["distalker_listing"] = pkg
    spec = importlib.util.spec_from_file_location(
        "distalker_listing.sync", os.path.join(REPO, "sync.py")
    )
    sync = importlib.util.module_from_spec(spec)
    sys.modules["distalker_listing.sync"] = sync
    spec.loader.exec_module(sync)

    p = portal()
    channels = [s.Portal._channel_from_row(row(enable_tv_archive=1,
                                               tv_archive_duration=7))]
    m3u = sync.build_m3u(p, channels, {})
    assert "tv_archive" not in m3u, m3u
    assert "catchup" not in m3u.lower(), m3u


# -- commands the portal cannot read back ---------------------------------


def test_a_marker_is_left_exactly_as_it_came():
    """Every portal but one answers this way, and their identities must not move.

    The stream hash is partly the URL, ours encodes this command: rewriting a
    command that was already fine would invent a new stream for every channel
    on every portal, once.
    """
    for cmd in (
        "ffmpeg http://localhost/ch/553690_",
        "http://localhost/ch/553690_",
        "ffmpeg http://prov/ch/101",
        "ffrt3 http://localhost/ch/42_",
    ):
        assert s.canonical_cmd(cmd, "553690") == cmd, cmd


def test_a_resolved_link_is_rebuilt_into_a_marker():
    """The portal cannot read its own resolved link back.

    Observed: handed one, it looks for the channel id inside, fails, and
    splices a piece of the URL into the parameter --
    '...&stream=-portal.example:80/play/live.'. Another provider returns the
    same parameter empty. Either way the link does not play.
    """
    resolved = (
        "ffmpeg http://portal.example:80/play/live.php?mac=00:1A:79:AA:BB:CC"
        "&stream=553690&extension=ts&play_token=aaaaaaaaaa"
    )
    assert s.canonical_cmd(resolved, "553690") == (
        "ffmpeg http://localhost/ch/553690_"
    )


def test_the_rewrite_is_what_makes_the_identity_hold_still():
    """The token rotates on every request; the marker does not.

    One portal produced 647 duplicate streams an hour before this, each sync
    inventing a new URL for the same channel and stranding the last one.
    """
    def resolved(token):
        return (
            f"ffmpeg http://host/play/live.php?stream=553690&play_token={token}"
        )

    first = s.canonical_cmd(resolved("aaaaaaaaaa"), "553690")
    second = s.canonical_cmd(resolved("bbbbbbbbbb"), "553690")
    assert first == second, (first, second)


def test_nothing_is_rebuilt_without_an_id_to_rebuild_it_from():
    resolved = "ffmpeg http://host/play/live.php?stream=553690&play_token=x"
    assert s.canonical_cmd(resolved, "") == resolved


def test_a_command_carrying_no_url_is_left_to_the_portal():
    """VOD commands look like this, and guessing at them would be worse."""
    for cmd in ("auto /media/1234.mpg", "", "   "):
        assert s.canonical_cmd(cmd, "553690") == cmd, repr(cmd)


def test_the_rewrite_is_reported_on_the_channel():
    rewritten = s.Portal._channel_from_row(
        row(cmd="ffmpeg http://host/play/live.php?stream=1&play_token=x")
    )
    assert rewritten.cmd == "ffmpeg http://localhost/ch/1_"
    assert rewritten.cmd_rewritten is True

    untouched = s.Portal._channel_from_row(row(cmd="ffmpeg http://localhost/ch/1_"))
    assert untouched.cmd_rewritten is False


# -- paging --------------------------------------------------------------


def scripted(all_channels, pages):
    """A portal whose two listing calls answer from canned data.

    ``all_channels`` is the get_all_channels payload, or an exception to raise.
    ``pages`` maps a page number to its 'js' object; a page not in it answers
    empty, which is what a portal past its last page does.
    """
    p = portal()
    p.pages_asked = []

    def fake_get_json(query, with_auth=True):
        if "get_all_channels" in query:
            if isinstance(all_channels, Exception):
                raise all_channels
            return all_channels
        page = int(query.split("&p=")[1].split("&")[0])
        p.pages_asked.append(page)
        return {"js": pages.get(page, {"data": []})}

    p._get_json = fake_get_json
    return p


def page(ids, **extra):
    js = {"data": [row(id=str(i), name=f"Ch {i}", cmd=f"ffmpeg http://x/{i}")
                   for i in ids]}
    js.update(extra)
    return js


REFUSED = s.PortalError("portal returned an empty channel list (check the MAC address)")


def test_a_portal_that_answers_in_one_request_is_never_paged():
    p = scripted({"js": {"data": [row()]}}, {})
    assert len(p.list_channels()) == 1
    assert p.pages_asked == [], "paging must stay the expensive last resort"


def test_paging_takes_over_when_the_single_request_will_not():
    p = scripted(REFUSED, {1: page([1, 2]), 2: page([3])})
    channels = p.list_channels()
    assert [c.channel_id for c in channels] == ["1", "2", "3"]
    assert p.pages_asked == [1, 2, 3], p.pages_asked


def test_the_reported_page_count_bounds_the_walk():
    """The guard open-tv lacks: the portal said how much there was.

    Its last page is full, so 'stop on an empty page' would ask for one more;
    these pages never repeat, so 'stop on a repeat' would never fire either.
    """
    pages = {1: page([1, 2], total_items=4, max_page_items=2), 2: page([3, 4])}
    p = scripted(REFUSED, pages)
    assert len(p.list_channels()) == 4
    assert p.pages_asked == [1, 2], p.pages_asked


def test_a_page_count_sent_as_strings_still_counts():
    pages = {1: page([1, 2], total_items="3", max_page_items="2"), 2: page([3])}
    p = scripted(REFUSED, pages)
    assert len(p.list_channels()) == 3
    assert p.pages_asked == [1, 2], p.pages_asked


def test_an_odd_remainder_gets_its_last_page():
    pages = {1: page([1, 2], total_items=5, max_page_items=2),
             2: page([3, 4]), 3: page([5])}
    p = scripted(REFUSED, pages)
    assert len(p.list_channels()) == 5
    assert p.pages_asked == [1, 2, 3], p.pages_asked


def test_a_portal_replaying_its_last_page_does_not_loop_forever():
    """Clamping 'p' instead of running out is common, and open-tv hangs on it."""
    p = portal()
    p.pages_asked = []

    def fake_get_json(query, with_auth=True):
        if "get_all_channels" in query:
            raise REFUSED
        p.pages_asked.append(int(query.split("&p=")[1].split("&")[0]))
        return {"js": page([1, 2])}  # the same two channels, always

    p._get_json = fake_get_json
    channels = p.list_channels()
    assert [c.channel_id for c in channels] == ["1", "2"]
    assert p.pages_asked == [1, 2], p.pages_asked


def test_an_empty_page_ends_it():
    p = scripted(REFUSED, {1: page([1, 2])})
    assert len(p.list_channels()) == 2
    assert p.pages_asked == [1, 2], p.pages_asked


def test_the_hard_cap_catches_a_portal_inventing_channels():
    """No total, never empty, never repeating: only the cap is left."""
    p = portal()
    counter = [0]

    def fake_get_json(query, with_auth=True):
        if "get_all_channels" in query:
            raise REFUSED
        counter[0] += 1
        return {"js": page([counter[0]])}

    p._get_json = fake_get_json
    original = s.ORDERED_LIST_PAGE_CAP
    s.ORDERED_LIST_PAGE_CAP = 5
    try:
        assert len(p.list_channels()) == 5
    finally:
        s.ORDERED_LIST_PAGE_CAP = original


def test_duplicates_across_pages_are_collapsed():
    p = scripted(REFUSED, {1: page([1, 2]), 2: page([2, 3])})
    assert [c.channel_id for c in p.list_channels()] == ["1", "2", "3"]


def test_when_neither_works_the_useful_message_survives():
    """Paging must not replace 'check the MAC' with something vaguer.

    An empty listing is far more often a wrong MAC than a portal that needs
    paging, so the first failure stays the one the user is shown.
    """
    p = scripted(REFUSED, {})
    try:
        p.list_channels()
    except s.PortalError as exc:
        assert "MAC address" in str(exc), exc


def test_a_refused_session_is_not_paged_at_all():
    p = scripted(s.PortalAuthError("blocked"), {1: page([1])})
    try:
        p.list_channels()
    except s.PortalAuthError:
        pass
    else:
        raise AssertionError("a refusal must not be retried by another route")
    assert p.pages_asked == [], "asking again is how a MAC gets noticed"


def test_the_sync_is_told_what_is_happening():
    notes = []
    p = scripted(REFUSED, {1: page([1, 2], total_items=4, max_page_items=2),
                           2: page([3, 4])})
    p.list_channels(progress=notes.append)
    joined = " | ".join(notes)
    assert "page at a time" in joined, notes
    assert "2 pages" in joined, notes
    assert "4 channels" in joined, notes


def test_the_page_request_asks_for_everything():
    p = portal()
    asked = []
    p._get_json = lambda q, with_auth=True: asked.append(q) or {"js": {"data": []}}
    p.get_ordered_list(3)
    assert "genre=*" in asked[0] and "sortby=number" in asked[0], asked
    assert "fav=0" in asked[0] and "&p=3" in asked[0], asked


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
    print("\n" + ("ALL LISTING TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
