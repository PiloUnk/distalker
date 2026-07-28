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
    assert portal().logo_url("canal.png") == (
        "http://p.example/c/misc/logos/320/canal.png"
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
