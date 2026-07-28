"""The 0.2.x -> 0.3.0 upgrade path.

Before 0.3.0 the STB identity was a single global block applied to every
portal. It is now a property of each portal, so any value that actually
differed from the built-in default has to be written onto the existing lines
or those portals would quietly start authenticating differently.

plugin.py is loaded the way Dispatcharr's loader does it, so the relative
imports resolve. Django is never imported -- _save_settings is stubbed.
"""
import importlib.util
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_plugin_module():
    pkg = types.ModuleType("distalker_pkg")
    pkg.__path__ = [REPO]
    pkg.__package__ = "distalker_pkg"
    sys.modules["distalker_pkg"] = pkg

    spec = importlib.util.spec_from_file_location(
        "distalker_pkg.plugin", os.path.join(REPO, "plugin.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["distalker_pkg.plugin"] = module
    spec.loader.exec_module(module)
    return module


plugin_mod = load_plugin_module()
sys.path.insert(0, REPO)
import stalker_api as s  # noqa: E402


class Recorder:
    """Captures what the plugin would have persisted."""

    def __init__(self):
        self.saved = None

    def __call__(self, settings):
        self.saved = settings


class NullLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


def migrate(settings):
    p = plugin_mod.Plugin()
    recorder = Recorder()
    p._save_settings = recorder
    result = p._migrate_legacy_globals(dict(settings), NullLogger())
    return result, recorder


def migrate_args(settings):
    p = plugin_mod.Plugin()
    recorder = Recorder()
    p._save_settings = recorder
    result = p._migrate_ffmpeg_args(dict(settings), NullLogger())
    return result, recorder


def test_every_default_we_shipped_is_replaced():
    """Changing the manifest default does not reach a value already stored, and
    one is stored on every install -- so each default this plugin has shipped is
    replaced: the ones that kept a dead source alive and Dispatcharr from
    switching away from it, and the one too quiet to report any statistics."""
    import stalker_api as sa

    for old in sa.SUPERSEDED_FFMPEG_ARGS:
        result, recorder = migrate_args({"portals": "x", "ffmpeg_args": old})
        assert result["ffmpeg_args"] == sa.DEFAULT_FFMPEG_ARGS
        assert "-reconnect" not in result["ffmpeg_args"]
        assert recorder.saved is not None, "the replacement must be persisted"


def test_arguments_someone_wrote_are_left_alone():
    """Silently editing a command line someone chose is worse than a bad default."""
    mine = "-hide_banner -i {url} -c copy -f mpegts pipe:1 -reconnect 1"
    result, recorder = migrate_args({"ffmpeg_args": mine})
    assert result["ffmpeg_args"] == mine
    assert recorder.saved is None, "nothing to save, so nothing was written"


def test_absent_arguments_are_not_invented():
    result, recorder = migrate_args({"portals": "x"})
    assert "ffmpeg_args" not in result
    assert recorder.saved is None


def test_real_global_values_move_onto_every_portal():
    settings = {
        "portals": (
            "A | http://a.example/c/ | 00:1A:79:AA:BB:01 | max_streams=1\n"
            "B | http://b.example/c/ | 00:1A:79:AA:BB:02 | max_streams=2\n"
        ),
        "stb_model": "MAG322",
        "stb_timezone": "Europe/Paris",
        "stb_serial": "",
        "stb_device_id": "",
        "stb_device_id2": "",
        "stb_signature": "",
    }
    result, recorder = migrate(settings)

    assert recorder.saved is not None, "migration must persist"
    for key in plugin_mod.LEGACY_STB_SETTINGS:
        assert key not in result, f"{key} should be gone so this runs once"

    portals, errors = s.parse_portals(result["portals"])
    assert not errors
    assert len(portals) == 2
    for portal in portals:
        assert portal.model == "MAG322"
        assert portal.timezone == "Europe/Paris"
    # Per-portal settings that were already there survive untouched.
    assert [p.max_streams for p in portals] == [1, 2]


def test_default_model_is_not_carried():
    """MAG254 was the shipped default, not a choice worth recording."""
    settings = {
        "portals": "A | http://a.example/c/ | 00:1A:79:AA:BB:01 | max_streams=1\n",
        "stb_model": "MAG254",
        "stb_serial": "",
    }
    result, _ = migrate(settings)
    assert "model=" not in result["portals"], "default model must not clutter every line"
    (portal,), _ = s.parse_portals(result["portals"])
    assert portal.model == "MAG254", "behaviour is unchanged either way"


def test_existing_per_portal_values_win():
    settings = {
        "portals": "A | http://a.example/c/ | 00:1A:79:AA:BB:01 | model=MAG250 max_streams=1\n",
        "stb_model": "MAG322",
    }
    result, _ = migrate(settings)
    (portal,), _ = s.parse_portals(result["portals"])
    assert portal.model == "MAG250", "a line's own value must not be overwritten"


def test_nothing_to_do_is_a_no_op():
    """No legacy keys at all: a fresh 0.3.0 install must not be rewritten."""
    settings = {"portals": "A | http://a.example/c/ | 00:1A:79:AA:BB:01 | max_streams=1\n"}
    result, recorder = migrate(settings)
    assert recorder.saved is None, "must not write settings when there is nothing to migrate"
    assert result == settings


def test_comments_and_empty_list_survive():
    settings = {
        "portals": "# my portals\nA | http://a.example/c/ | 00:1A:79:AA:BB:01 | max_streams=1\n",
        "stb_timezone": "Europe/Paris",
    }
    result, _ = migrate(settings)
    assert result["portals"].startswith("# my portals")

    empty, _ = migrate({"portals": "", "stb_timezone": "Europe/Paris"})
    assert empty["portals"] == ""
    assert "stb_timezone" not in empty


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
    print("\n" + ("ALL MIGRATION TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
