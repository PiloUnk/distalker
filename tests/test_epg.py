"""Turning a portal's guide into something Dispatcharr will read.

The whole feature rests on one agreement: the identifier written into the
playlist and the identifier written into the guide have to be the same string,
because that is the only thing joining a channel to its programmes. Nothing
warns when they drift -- the guide simply matches nothing -- so the first test
here is the one that pins it.

The rest is the parsing that has to survive a portal sending rubbish, and the
promise that a guide which goes wrong never takes the channel list with it.
"""
import importlib.util
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import stalker_api as s  # noqa: E402


def load_sync():
    """sync.py as Dispatcharr's loader sees it: a namespace package."""
    pkg = types.ModuleType("distalker_epg")
    pkg.__path__ = [REPO]
    sys.modules["distalker_epg"] = pkg
    spec = importlib.util.spec_from_file_location(
        "distalker_epg.sync", os.path.join(REPO, "sync.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["distalker_epg.sync"] = module
    spec.loader.exec_module(module)
    return module


sync = load_sync()


def portal(**cfg_kwargs):
    cfg = s.PortalConfig(
        slug="mock", name="Mock", url="http://p.example/c/portal.php",
        mac="00:1A:79:AA:BB:CC", **cfg_kwargs
    )
    return s.Portal(cfg)


def channel(cid="101", name="One", logo=""):
    return s.ChannelEntry(
        channel_id=cid, name=name, cmd=f"ffmpeg http://x/{cid}", logo=logo,
        genre_id="1", number="1",
    )


def programme(start=1785276000, stop=1785279600, name="Show", descr="Plot"):
    return {"id": "9", "name": name, "descr": descr,
            "start_timestamp": start, "stop_timestamp": stop}


def xmltv(p, channels, epg_data):
    return "".join(sync.build_xmltv(p, channels, epg_data))


# -- the invariant --------------------------------------------------------


def test_the_playlist_and_the_guide_agree_on_the_identifier():
    """The one thing that must never drift.

    tvg-id in the M3U, channel id in the XMLTV, and the 'channel' attribute on
    every programme: three places, one string. If any of them changes shape the
    guide stops matching and nothing says so.
    """
    p = portal()
    channels = [channel("101"), channel("102", "Two")]
    data = {"101": [programme()], "102": [programme()]}

    playlist = sync.build_m3u(p, channels, {"1": "News"})
    guide = xmltv(p, channels, data)

    for cid in ("101", "102"):
        expected = f"mock.{cid}"
        assert f'tvg-id="{expected}"' in playlist, expected
        assert f'<channel id="{expected}">' in guide, expected
        assert f'channel="{expected}"' in guide, expected


def test_a_channel_with_no_id_is_in_neither():
    """No identifier, nothing to join on, so it is not offered a guide."""
    p = portal()
    channels = [channel("")]
    guide = xmltv(p, channels, {"": [programme()]})
    assert "<channel" not in guide, guide


# -- what Dispatcharr's parser needs --------------------------------------


def test_timestamps_are_exactly_what_the_parser_expects():
    """20 characters, UTC. parse_xmltv_time reads [:14] then [15:20]."""
    stamp = sync._xmltv_time(1785276000)
    assert len(stamp) == 20, repr(stamp)
    assert stamp == "20260728220000 +0000", stamp
    # UTC whatever the host thinks the time is, since the parser trusts the
    # offset we write and a portal's epoch means one instant either way.
    assert stamp.endswith(" +0000")


def test_a_timestamp_that_is_not_one_is_refused():
    for bad in (None, "", "abc", 0, -5, {}):
        assert sync._xmltv_time(bad) == "", repr(bad)


def test_timestamps_arriving_as_strings_still_work():
    """Portals send numbers as strings about as often as as numbers."""
    assert sync._xmltv_time("1785276000") == sync._xmltv_time(1785276000)


def test_the_document_carries_what_the_parser_reads():
    p = portal()
    guide = xmltv(p, [channel(logo="a.png")], {"101": [programme()]})
    assert guide.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<display-name>One</display-name>" in guide
    assert 'src="http://p.example/c/misc/logos/320/a.png"' in guide
    assert "<title>Show</title>" in guide
    assert "<desc>Plot</desc>" in guide
    assert guide.rstrip().endswith("</tv>")


# -- portals sending rubbish ----------------------------------------------


def test_channels_without_programmes_are_left_out():
    """An empty <channel> becomes a permanent empty row in the EPG picker."""
    p = portal()
    guide = xmltv(p, [channel("101"), channel("102", "Two")], {"101": [programme()]})
    assert "mock.101" in guide
    assert "mock.102" not in guide, guide


def test_unusable_programmes_are_dropped_not_written():
    p = portal()
    bad = [
        programme(start=0),
        programme(stop=0),
        programme(start=1785279600, stop=1785276000),  # ends before it starts
        programme(start=1785276000, stop=1785276000),  # no duration
        programme(name=""),
        "not a dict",
        None,
    ]
    guide = xmltv(p, [channel()], {"101": bad})
    assert "<programme" not in guide, guide


def test_a_description_is_optional():
    p = portal()
    guide = xmltv(p, [channel()], {"101": [programme(descr="")]})
    assert "<title>Show</title>" in guide
    assert "<desc>" not in guide


def test_markup_in_a_title_cannot_break_the_document():
    p = portal()
    guide = xmltv(
        p, [channel(name='A & B <"x">')],
        {"101": [programme(name="Tom & Jerry </title>", descr="1 < 2")]},
    )
    assert "&amp;" in guide and "&lt;" in guide
    assert "</title>" in guide.split("<title>")[1][:60]
    # Proof rather than inspection: it has to actually parse.
    import xml.etree.ElementTree as ET

    root = ET.fromstring(guide)
    assert root.find("programme/title").text == "Tom & Jerry </title>"
    assert root.find("channel/display-name").text == 'A & B <"x">'


def test_control_characters_are_removed_rather_than_escaped():
    """XML 1.0 cannot carry them at all, and portals do send them."""
    import xml.etree.ElementTree as ET

    p = portal()
    guide = xmltv(p, [channel()], {"101": [programme(descr="be\x03fore\x00after")]})
    assert ET.fromstring(guide).find("programme/desc").text == "beforeafter"


# -- overlapping entries ---------------------------------------------------


def test_the_same_show_listed_twice_is_written_once():
    """What a guide corrected in place looks like from outside.

    Observed on a real portal: one show, one end time, two start times ten
    minutes apart. Written as-is, Dispatcharr has two candidates spanning the
    same minute and picks one arbitrarily for "what is on now".
    """
    p = portal()
    both = [
        programme(start=1785275700, stop=1785284400),
        programme(start=1785276300, stop=1785284400),
    ]
    guide = xmltv(p, [channel()], {"101": both})
    assert guide.count("<programme") == 1, guide
    # The earlier start wins: it covers the whole slot.
    assert 'start="20260728215500 +0000"' in guide, guide


def test_back_to_back_programmes_are_both_kept():
    """Touching is not overlapping, and it is what a schedule looks like."""
    p = portal()
    run = [
        programme(start=1785276000, stop=1785279600, name="First"),
        programme(start=1785279600, stop=1785283200, name="Second"),
    ]
    guide = xmltv(p, [channel()], {"101": run})
    assert guide.count("<programme") == 2, guide


def test_programmes_are_written_in_order():
    """Whatever order the portal sent them in."""
    p = portal()
    shuffled = [
        programme(start=1785283200, stop=1785286800, name="Third"),
        programme(start=1785276000, stop=1785279600, name="First"),
        programme(start=1785279600, stop=1785283200, name="Second"),
    ]
    guide = xmltv(p, [channel()], {"101": shuffled})
    assert guide.index("First") < guide.index("Second") < guide.index("Third"), guide


def test_one_bad_entry_does_not_swallow_the_rest():
    """A junk row must not become an overlap that hides real programmes."""
    p = portal()
    mixed = [
        programme(start=0, stop=0, name="Junk"),
        programme(start=1785276000, stop=1785279600, name="Real"),
    ]
    guide = xmltv(p, [channel()], {"101": mixed})
    assert "Junk" not in guide
    assert guide.count("<programme") == 1, guide


# -- memory ----------------------------------------------------------------


def test_the_guide_is_emptied_as_it_is_written():
    """The generator gives the memory back; a large portal depends on it."""
    p = portal()
    data = {"101": [programme()], "102": [programme()]}
    channels = [channel("101"), channel("102", "Two")]

    chunks = sync.build_xmltv(p, channels, data)
    list(chunks)
    assert data == {}, data


# -- the portal call -------------------------------------------------------


class FakeResponse:
    def __init__(self, body=b"", status=200):
        self.status_code = status
        self._body = body

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


def fetching(body, status=200):
    p = portal()
    p.asked = []

    def fake_request(method, url, **kwargs):
        p.asked.append((method, url, kwargs))
        return FakeResponse(body, status)

    p._request = fake_request
    return p


def test_the_guide_request_asks_for_the_hours_it_was_given():
    import json as _json

    p = fetching(_json.dumps({"js": {"data": {"101": []}}}).encode())
    p.get_epg_info(48)
    _, url, kwargs = p.asked[0]
    assert "action=get_epg_info" in url and "period=48" in url, url
    assert "type=itv" in url, url
    # Streamed, or the point of the scratch file is lost.
    assert kwargs.get("stream") is True, kwargs


def test_a_portal_with_no_guide_is_not_an_error():
    """An empty guide is a property of the provider, not a fault.

    Observed on eight portals out of twelve, always as ``{"js": {"data": []}}``:
    thousands of channels, no programmes for any of them.
    """
    import json as _json

    for payload in ({"js": {"data": []}}, {"js": {}}):
        p = fetching(_json.dumps(payload).encode())
        assert p.get_epg_info(24) == {}, payload


def test_a_shape_nobody_has_met_is_reported_rather_than_swallowed():
    """Silently calling it 'no guide' is how it would stay unmet.

    A flat list of programmes would be perfectly usable if anyone knew it was
    arriving, so the one thing not to do is treat it as an empty portal.
    """
    import json as _json

    for payload, expected in (
        ({"js": []}, "list"),
        ({"js": "nope"}, "str"),
        ({"js": {"data": [{"name": "x"}]}}, "list"),
        ({"js": {"data": "nope"}}, "str"),
    ):
        p = fetching(_json.dumps(payload).encode())
        try:
            p.get_epg_info(24)
        except s.PortalError as exc:
            assert expected in str(exc), (payload, str(exc))
        else:
            raise AssertionError(f"{payload} should not pass for an empty guide")


def test_a_guide_that_is_not_json_is_reported_as_such():
    p = fetching(b"<html>go away</html>")
    try:
        p.get_epg_info(24)
    except s.PortalError as exc:
        assert "not JSON" in str(exc), exc
    else:
        raise AssertionError("prose is not a guide")


def test_an_oversized_guide_is_refused_before_it_is_parsed():
    """The cap exists so the worker is not killed finding the limit."""
    original = s.EPG_MAX_BYTES
    s.EPG_MAX_BYTES = 1024
    try:
        p = fetching(b"x" * 4096)
        try:
            p.get_epg_info(24)
        except s.PortalError as exc:
            assert "larger than" in str(exc) and "epg_hours" in str(exc), exc
        else:
            raise AssertionError("an oversized guide must be refused")
    finally:
        s.EPG_MAX_BYTES = original


def test_a_refused_guide_is_an_auth_error():
    p = fetching(b"", status=403)
    try:
        p.get_epg_info(24)
    except s.PortalAuthError:
        pass
    else:
        raise AssertionError("403 must stay typed as a refusal")


# -- the line ---------------------------------------------------------------


def test_the_guide_is_off_unless_the_line_asks():
    (cfg,), errors = s.parse_portals("A | http://a.example/c/ | 00:1A:79:AA:BB:01")
    assert not errors
    assert cfg.epg is False
    assert cfg.epg_hours == s.DEFAULT_EPG_HOURS


def test_the_ways_a_person_writes_yes():
    for value in ("1", "true", "yes", "on", "TRUE", "Yes"):
        (cfg,), errors = s.parse_portals(
            f"A | http://a.example/c/ | 00:1A:79:AA:BB:01 | epg={value}"
        )
        assert not errors and cfg.epg is True, value
    for value in ("0", "no", "false", "off", ""):
        (cfg,), _ = s.parse_portals(
            f"A | http://a.example/c/ | 00:1A:79:AA:BB:01 | epg={value}"
        )
        assert cfg.epg is False, value


def test_the_period_is_read_and_checked():
    (cfg,), errors = s.parse_portals(
        "A | http://a.example/c/ | 00:1A:79:AA:BB:01 | epg=1 epg_hours=48"
    )
    assert not errors and cfg.epg_hours == 48

    for bad in ("abc", "0", "-3"):
        _, errors = s.parse_portals(
            f"A | http://a.example/c/ | 00:1A:79:AA:BB:01 | epg=1 epg_hours={bad}"
        )
        assert errors, bad


def test_a_written_line_round_trips():
    line = s.format_portal_line(
        "A", "http://a.example/c/", "00:1A:79:AA:BB:01", epg=True, epg_hours=48
    )
    assert "epg=1" in line and "epg_hours=48" in line, line
    (cfg,), errors = s.parse_portals(line)
    assert not errors and cfg.epg is True and cfg.epg_hours == 48

    # The default period says nothing the default does not already say.
    line = s.format_portal_line(
        "A", "http://a.example/c/", "00:1A:79:AA:BB:01", epg=True
    )
    assert "epg=1" in line and "epg_hours" not in line, line

    line = s.format_portal_line("A", "http://a.example/c/", "00:1A:79:AA:BB:01")
    assert "epg" not in line, line


def test_turning_the_guide_on_forces_a_fetch():
    """Otherwise the plan calls the portal unchanged and nothing happens.

    The same trap as every other setting that does not touch the line-up: the
    plan compares against what was published, so a key it does not compare is
    a key the user can set with no effect at all.
    """
    spec = importlib.util.spec_from_file_location(
        "distalker_epg.plugin", os.path.join(REPO, "plugin.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["distalker_epg.plugin"] = module
    spec.loader.exec_module(module)

    keys = module.Plugin.FETCH_KEYS
    assert "epg" in keys and "epg_hours" in keys, keys
    for key in module.Plugin.LINEUP_KEYS:
        assert key in keys, key


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
    print("\n" + ("ALL EPG TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
