"""Reading an m2m_changed payload into something worth acting on.

The signal fires from both ends of the relation and for every phase of every
change, so most of what reaches the receiver is noise. Django is not available
here, so this covers the sorting -- the assignment itself is a single ORM
update behind it.
"""
import importlib.util
import logging
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# The never-raises test deliberately drives assign() into its exception
# handler; the traceback it logs would read as a failure in the CI output.
logging.disable(logging.CRITICAL)


def load_signals_module():
    """signals.py imports from .sync, so it needs its package context."""
    pkg = types.ModuleType("distalker_pkg")
    pkg.__path__ = [REPO]
    pkg.__package__ = "distalker_pkg"
    sys.modules["distalker_pkg"] = pkg
    spec = importlib.util.spec_from_file_location(
        "distalker_pkg.signals", os.path.join(REPO, "signals.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["distalker_pkg.signals"] = module
    spec.loader.exec_module(module)
    return module


sig = load_signals_module()

CHANNEL, STREAM = 7, 42


def test_a_forward_add_names_the_channel_and_its_new_streams():
    """channel.streams.add(stream) -- the instance is the channel."""
    assert sig.resolve_targets("post_add", CHANNEL, {STREAM, 43}, False) == (
        [CHANNEL],
        [STREAM, 43],
    )


def test_a_reverse_add_is_read_the_other_way_round():
    """stream.channels.add(channel) -- the instance is the stream."""
    assert sig.resolve_targets("post_add", STREAM, {CHANNEL, 8}, True) == (
        [CHANNEL, 8],
        [STREAM],
    )


def test_removals_and_clears_are_ignored():
    for action in ("post_remove", "post_clear", "pre_remove", "pre_clear"):
        assert sig.resolve_targets(action, CHANNEL, {STREAM}, False) is None


def test_the_pre_half_of_an_add_is_ignored():
    """Acting on pre_add would assign against a link that may never commit."""
    assert sig.resolve_targets("pre_add", CHANNEL, {STREAM}, False) is None


def test_an_empty_set_is_ignored():
    assert sig.resolve_targets("post_add", CHANNEL, set(), False) is None
    assert sig.resolve_targets("post_add", CHANNEL, None, False) is None


def test_assign_declines_empty_work_without_touching_django():
    """The early return must come before any model import, or this raises."""
    assert sig.assign([], [STREAM]) == 0
    assert sig.assign([CHANNEL], []) == 0


def test_assign_never_raises():
    """It runs inside the transaction saving the user's channel.

    With no Django importable, every path through it ends in the exception
    handler -- which is exactly the guarantee being pinned.
    """
    assert sig.assign([CHANNEL], [STREAM]) == 0


def test_the_receivers_share_one_dispatch_uid():
    """Reconnecting on reload must replace the receivers, not stack them."""
    assert sig.DISPATCH_UID


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
    print("\n" + ("ALL SIGNAL TESTS PASSED" if not failures else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
