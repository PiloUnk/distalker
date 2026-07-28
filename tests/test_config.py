"""Portal-line parsing, STB identity defaults and pseudo-URL encoding."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stalker_api as s  # noqa: E402


def test_builtin_defaults():
    (portal,), errors = s.parse_portals("Plain | http://d.example/c/ | 00:1A:79:AA:BB:03")
    assert not errors
    assert portal.model == s.DEFAULT_MODEL
    assert portal.serial_number == s.DEFAULT_SERIAL
    assert portal.timezone == s.DEFAULT_TIMEZONE
    assert portal.device_id == s.DEFAULT_DEVICE_ID
    assert portal.signature == s.DEFAULT_SIGNATURE
    # No credentials selects device-ID auth even with the default ffff... IDs,
    # matching stalkerhek's `deviceIdAuth := Username == "" && Password == ""`.
    # This is the plain URL+MAC case, so it must not regress.
    assert portal.device_id_auth is True


def test_stb_identity_is_per_portal():
    """Each portal carries its own; nothing is inherited from anywhere."""
    text = (
        "Custom | http://a.example/c/ | 00:1A:79:AA:BB:CC | "
        "model=MAG322 serial=SN12345 device_id=" + "A" * 64 + " timezone=Europe/Paris\n"
        "Plain | http://b.example/c/ | 00:1A:79:AA:BB:01\n"
    )
    portals, errors = s.parse_portals(text)
    assert not errors

    custom, plain = portals
    assert (custom.model, custom.timezone) == ("MAG322", "Europe/Paris")
    assert custom.serial_number == "SN12345"
    # A lone device_id mirrors into device_id2.
    assert custom.device_id == custom.device_id2 == "A" * 64

    # The neighbouring portal is untouched by any of that.
    assert plain.model == s.DEFAULT_MODEL
    assert plain.serial_number == s.DEFAULT_SERIAL
    assert plain.timezone == s.DEFAULT_TIMEZONE
    assert plain.device_id == s.DEFAULT_DEVICE_ID


def test_credentials_beat_device_id_auth():
    (portal,), _ = s.parse_portals(
        "U | http://c.example/c/ | 00:1A:79:AA:BB:02 | username=joe password=pw device_id="
        + "A" * 64
    )
    assert portal.username == "joe" and portal.password == "pw"
    assert portal.device_id_auth is False


def test_extras_split_by_space_and_pipe_and_quotes():
    text = (
        'A | http://a.example/c/ | 00:1A:79:DD:EE:01 | username=joe password=secret\n'
        'B | http://b.example/c/ | 00:1A:79:DD:EE:02 | username=k | password=x | model=MAG322\n'
        'C | http://c.example/c/ | 00:1A:79:DD:EE:03 | password="two words" max_streams=3\n'
    )
    portals, errors = s.parse_portals(text)
    assert not errors
    assert portals[0].password == "secret"
    assert (portals[1].username, portals[1].password, portals[1].model) == ("k", "x", "MAG322")
    assert portals[2].password == "two words" and portals[2].max_streams == 3


def test_bad_lines_are_reported_not_silently_dropped():
    text = (
        "Good | http://a.example/c/ | 00:1A:79:AA:BB:CC\n"
        "Bad URL | not-a-url | 00:1A:79:AA:BB:CC\n"
        "Bad MAC | http://x.example/c/ | ZZ:ZZ\n"
        "Good | http://dup.example/c/ | 00:1A:79:AA:BB:CC\n"
        "Junk | http://j.example/c/ | 00:1A:79:AA:BB:04 | notakeyvalue\n"
        "Short line\n"
    )
    portals, errors = s.parse_portals(text)
    assert len(portals) == 1
    assert len(errors) == 5
    assert any("does not look like a portal URL" in e for e in errors)
    assert any("MAC address" in e for e in errors)
    assert any("duplicate" in e for e in errors)
    assert any("key=value" in e for e in errors)


def test_comments_and_blank_lines_ignored():
    portals, errors = s.parse_portals("\n# a comment\n\nA | http://a.example/c/ | 00:1A:79:AA:BB:CC\n")
    assert len(portals) == 1 and not errors


def test_url_normalisation():
    # Each case mirrors stalkerhek's normalizePortalURL (webui/profiles.go).
    cases = {
        "http://a.example/c/": "http://a.example/c/portal.php",
        "http://a.example": "http://a.example/portal.php",
        "http://a.example/": "http://a.example/portal.php",
        "http://a.example/portal.php": "http://a.example/portal.php",
        # An explicit load.php must survive: older portals only serve that.
        "http://a.example/stalker_portal/server/load.php":
            "http://a.example/stalker_portal/server/load.php",
        # Any other .php is swapped for portal.php in the same directory.
        "http://a.example/c/other.php": "http://a.example/c/portal.php",
        # A missing scheme is filled in rather than rejected.
        "somedomain.com:8080/c/": "http://somedomain.com:8080/c/portal.php",
        # Ports and deep paths must be preserved.
        "http://somedomain.com:8080/c/": "http://somedomain.com:8080/c/portal.php",
    }
    for raw, expected in cases.items():
        assert s.normalize_portal_url(raw) == expected, f"{raw} -> {s.normalize_portal_url(raw)}"


def test_url_only_and_mac_only_config():
    """The most common setup: portal URL + MAC, nothing else."""
    (portal,), errors = s.parse_portals("Mine | http://somedomain.com:8080/c/ | 00:1A:79:AA:BB:CC")
    assert not errors
    assert portal.url == "http://somedomain.com:8080/c/portal.php"
    assert portal.mac == "00:1A:79:AA:BB:CC"
    assert portal.device_id_auth is True
    assert portal.username == "" and portal.password == ""
    assert portal.max_streams == 1


def test_partial_credentials_fall_back_to_device_auth():
    """A username with no password is not usable credentials."""
    (portal,), _ = s.parse_portals("A | http://a.example/c/ | 00:1A:79:AA:BB:01 | username=joe")
    assert portal.device_id_auth is True


def test_pseudo_url_roundtrip():
    cmd = "ffmpeg http://provider.example/live/1234.m3u8?token=abc def"
    url = s.encode_pseudo_url("living-room", cmd)
    assert s.decode_pseudo_url(url) == ("living-room", cmd)


def test_pseudo_url_rejects_foreign_urls():
    for bad in ["http://example.com/a/b", "http://distalker.invalid/only-one-part"]:
        try:
            s.decode_pseudo_url(bad)
        except ValueError:
            continue
        raise AssertionError(f"should have rejected {bad}")


def test_a_line_needs_only_a_url_and_a_mac():
    """The shortest line anyone should have to write."""
    (portal,), errors = s.parse_portals("http://myportal.example:2095/c/ | 00:1A:79:AA:BB:CC")
    assert not errors
    assert portal.name == "myportal", "the host is the label nobody should type twice"
    assert portal.slug == "myportal"
    assert portal.mac == "00:1A:79:AA:BB:CC"
    assert portal.max_streams == 1, "absent means one, which is what portals allow"


def test_a_written_name_wins_over_the_host():
    (portal,), errors = s.parse_portals("Living Room | http://a.example/c/ | 00:1A:79:AA:BB:CC")
    assert not errors and portal.name == "Living Room"


def test_the_mac_says_which_field_is_which():
    """A name may contain a dot or a colon; only the MAC's position is reliable."""
    (portal,), errors = s.parse_portals("Chez J.P | http://a.example/c/ | 00:1A:79:AA:BB:CC")
    assert not errors
    assert portal.name == "Chez J.P" and portal.url.startswith("http://a.example/")


def test_the_shapes_a_mac_gets_quoted_in_are_accepted():
    for written in ("00-1a-79-dd-ee-ff", "00:1a:79:dd:ee:ff", "001A79DDEEFF", "00.1a.79.dd.ee.ff"):
        (portal,), errors = s.parse_portals(f"http://a.example/c/ | {written}")
        assert not errors, f"{written}: {errors}"
        assert portal.mac == "00:1A:79:DD:EE:FF", written


def test_nonsense_in_the_mac_field_is_still_refused():
    _, errors = s.parse_portals("http://a.example/c/ | not-a-mac")
    assert errors and "not a MAC" in errors[0]


def test_two_portals_on_one_host_ask_for_a_name():
    """Deriving both names from the host collides, and the fix is the user's.

    Suffixing automatically is not an option: the slug is encoded into every
    channel's URL, so an identity that shifted when a line was added or removed
    would silently rebind existing channels to the other portal.
    """
    text = ("http://shared.example/c/ | 00:1A:79:AA:BB:CC\n"
            "http://shared.example/c/ | 00:1A:79:DD:EE:FF\n")
    portals, errors = s.parse_portals(text)
    assert len(portals) == 1
    assert errors and "same host as line 1" in errors[0]
    assert "write a name in front" in errors[0], "the message must say what to do"


def test_a_named_duplicate_is_reported_as_one():
    text = ("Home | http://a.example/c/ | 00:1A:79:AA:BB:CC\n"
            "Home | http://b.example/c/ | 00:1A:79:DD:EE:FF\n")
    _, errors = s.parse_portals(text)
    assert errors and "duplicate portal name" in errors[0]


def test_a_line_carrying_only_defaults_is_written_bare():
    """The 0.2.x migration rewrites lines; it must not add noise while it does.

    Users asked for exactly this: max_streams=1 is what every portal has, and
    seeing it on a line nobody typed is what made the setting look mandatory.
    """
    line = s.format_portal_line("Plain", "http://a.example/c/", "00:1A:79:AA:BB:CC")
    assert "max_streams" not in line
    assert line == "Plain | http://a.example/c/ | 00:1A:79:AA:BB:CC | "

    line = s.format_portal_line("Two", "http://a.example/c/", "00:1A:79:AA:BB:CC", max_streams=2)
    assert "max_streams=2" in line, "a value that says something is kept"


def test_form_values_needing_quotes_survive():
    line = s.format_portal_line("P", "http://a.example/c/", "00:1A:79:AA:BB:CC", "jo e", 'pa"ss', 3)
    (portal,), errors = s.parse_portals(line)
    assert not errors
    assert portal.username == "jo e"
    assert portal.password == 'pa"ss'
    assert portal.max_streams == 3


def test_stb_values_survive_a_line_rewrite():
    """What the 0.2.x migration does: read a line, write it back whole."""
    stb = {
        "model": "MAG322",
        "serial": "SN04417723",
        "device_id": "a" * 64,
        "device_id2": "b" * 64,
        "signature": "c" * 64,
        "timezone": "Europe/Paris",
    }
    line = s.format_portal_line("Living Room", "http://a.example/c/", "00:1A:79:AA:BB:CC", stb=stb)
    parsed, errors = s.split_portal_line(line)
    assert not errors and parsed["named"] is True
    for key, value in stb.items():
        assert parsed["extras"][key] == value, f"{key} did not survive the round trip"

    (portal,), errors = s.parse_portals(line)
    assert not errors
    assert portal.model == "MAG322" and portal.timezone == "Europe/Paris"


def test_unset_stb_values_are_not_invented():
    """A portal left on the defaults keeps a line free of values nobody chose."""
    line = s.format_portal_line("Plain", "http://a.example/c/", "00:1A:79:AA:BB:CC")
    parsed, _ = s.split_portal_line(line)
    for key in s.STB_KEYS:
        assert key not in parsed["extras"], f"{key} was written despite being unset"

    (portal,), _ = s.parse_portals(line)
    assert portal.model == s.DEFAULT_MODEL


def test_a_derived_name_is_flagged_as_derived():
    parsed, _ = s.split_portal_line("http://a.example/c/ | 00:1A:79:AA:BB:CC")
    assert parsed["named"] is False


def test_expiry_is_read_from_the_field_resellers_use():
    """Ministra shows 'phone' in the MAG UI, so that is where the date goes."""
    assert s.parse_expiry("August 18, 2027, 4:53 pm").year == 2027
    assert s.parse_expiry("April 8, 2027, 12:39 pm").month == 4
    assert s.parse_expiry("2027-04-08").day == 8
    # Free text: anything else is simply not an expiry.
    assert s.parse_expiry("+33 6 12 34 56 78") is None
    assert s.parse_expiry("0000-00-00 00:00:00") is None
    assert s.parse_expiry("") is None
    assert s.parse_expiry(None) is None


def test_the_default_arguments_let_dispatcharr_fail_over():
    """ffmpeg must not reconnect on its own: it retries an expired portal link
    while staying alive, so Dispatcharr sees no failure and never switches to
    the channel's other sources. Its own retry respawns the resolver, which is
    the only reconnection that can get a fresh link."""
    args = s.DEFAULT_FFMPEG_ARGS
    assert "-reconnect" not in args, "this froze channels instead of failing over"
    assert "-rw_timeout" in args, "a stalled read must end, not hang"
    # Three attempts have to fit inside a client's patience, around 50 seconds.
    micros = int(args.split("-rw_timeout ")[1].split()[0])
    assert micros / 1_000_000 * 3 < 45, "three attempts would outlast the viewer"


def test_the_default_arguments_feed_dispatcharrs_statistics():
    """Nothing probes the stream. Dispatcharr reads the resolution, codecs and
    bitrate off the stderr of whatever the stream profile spawned -- which is
    ffmpeg's own, since the resolver execs it -- so the input dump has to be
    emitted (info level) and the periodic stats line has to be asked for
    (-stats, which ffmpeg otherwise drops below info). Quieten either and the
    channel plays with an empty statistics panel and nothing to say why."""
    args = s.DEFAULT_FFMPEG_ARGS
    assert "-loglevel info" in args, "the Input #0 / Stream #0:0 dump is info-level"
    assert "-stats" in args, "the output bitrate comes from the frame= line"


def test_a_shipped_default_is_replaced_but_a_choice_is_not():
    for old in s.SUPERSEDED_FFMPEG_ARGS:
        assert s.is_superseded_ffmpeg_args(old)
        assert s.is_superseded_ffmpeg_args("  " + old.replace(" ", "  ") + "\n"), (
            "reflowed whitespace is still the same default"
        )
    assert not s.is_superseded_ffmpeg_args(s.DEFAULT_FFMPEG_ARGS)
    assert not s.is_superseded_ffmpeg_args(
        s.SUPERSEDED_FFMPEG_ARGS[0] + " -loglevel debug"
    ), "a line someone edited is theirs, reconnect options included"
    assert not s.is_superseded_ffmpeg_args("")


def test_the_link_is_found_wherever_the_portal_puts_it():
    """create_link answers with a command line, not a URL, and portals differ.

    This used to take the last whitespace-separated field, which broke on a
    quoted URL -- the closing quote rides along, so the token no longer starts
    with a scheme -- and on anything a portal chose to append after it.
    """
    for raw, expected in (
        ("ffmpeg http://host/ch/1_", "http://host/ch/1_"),
        ("http://host/ch/1_", "http://host/ch/1_"),
        ('ffmpeg "http://host/ch/1_?token=a b"'.replace(" b", "b"), "http://host/ch/1_?token=ab"),
        ("ffmpeg 'http://host/ch/1_'", "http://host/ch/1_"),
        ("ffmpeg http://host/ch/1_ -x 2", "http://host/ch/1_"),
        # An operator's portal hands out multicast; ffmpeg plays it, and a
        # whitelist of schemes would have refused it for no reason.
        ("ffmpeg udp://@239.1.1.1:1234", "udp://@239.1.1.1:1234"),
        ("ffmpeg rtp://239.0.0.1:5004", "rtp://239.0.0.1:5004"),
        ("ffmpeg srt://host:9000?mode=caller", "srt://host:9000?mode=caller"),
    ):
        assert s.extract_link(raw) == expected, raw


def test_a_command_with_nothing_playable_is_refused():
    """A local path or an apology is not a link, and saying so beats handing
    ffmpeg something it will fail on obscurely."""
    for raw in ("auto /media/file.mpg", "Sorry, this channel is not available", "", "   "):
        assert s.extract_link(raw) == "", raw


def test_config_survives_redis_roundtrip():
    (portal,), _ = s.parse_portals("A | http://a.example/c/ | 00:1A:79:AA:BB:CC | max_streams=4")
    assert s.PortalConfig.from_dict(portal.to_dict()) == portal


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
    print("\n" + ("ALL CONFIG TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
