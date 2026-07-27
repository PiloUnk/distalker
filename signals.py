"""Assign the stream profile the moment a channel gains a portal source.

Dispatcharr resolves the stream profile from the channel, so a channel holding
one of this plugin's streams cannot tune until it carries the ``Distalker``
profile. The **Assign** action does that in bulk, but only when someone presses
it -- which means a trip to the plugin panel every time a channel is built by
hand. These receivers do it as the stream is attached.

Three routes attach a stream to a channel, and they do *not* fire the same
signals:

===========================================  =================  ==============
Route                                        ``post_save``      ``m2m_changed``
===========================================  =================  ==============
``ChannelStream.objects.create(...)``        yes                no
``channel.streams.add(stream)``              no [#bulk]_        yes
``ChannelStream.objects.bulk_create(...)``   no                 no
===========================================  =================  ==============

.. [#bulk] ``add()`` on a many-to-many with an explicit through model creates
   the through rows with ``bulk_create``, which skips per-row signals.

So both receivers are needed to cover the two interactive routes -- the first
is how the Channels form saves, the second how a stream is attached to an
existing channel. The third, used by *create channels from streams* on more
than one stream, emits nothing at all: Django's ``bulk_create`` never has.

Nor does any of this fire for a channel built while the plugin was disabled or
being upgraded, since a plugin that is not loaded has no receivers. Such a
channel keeps the installation's default profile and fails to tune with a DNS
error naming ``distalker.invalid`` and nothing else. The last line of defence
is the ``channel_error`` event, which re-runs **Assign** after a failed start;
see ``_action_apply_profile``.

Nothing in here may raise. The receivers run inside the transaction that is
creating the channel, so an exception would not merely miss an assignment --
it would fail the user's save.
"""

from __future__ import annotations

import logging

from .sync import MARKER_KEY, STREAM_PROFILE_NAME

# Django dispatches on (signal, sender, dispatch_uid), so reconnecting after a
# plugin reload replaces the old receiver instead of stacking another one.
DISPATCH_UID = "distalker.auto_assign_stream_profile"

logger = logging.getLogger(__name__)


def resolve_targets(action, instance_pk, pk_set, reverse):
    """Read an ``m2m_changed`` payload into (channel_ids, stream_ids).

    Returns ``None`` for the events that cannot have attached anything --
    removals, clears, and the ``pre_*`` half of every change.

    The signal fires from both ends: ``channel.streams.add(stream)`` sends the
    channel as the instance, ``stream.channels.add(channel)`` sends the stream
    and sets ``reverse``.
    """
    if action != "post_add" or not pk_set:
        return None
    ids = sorted(pk_set)
    if reverse:
        return ids, [instance_pk]
    return [instance_pk], ids


def _profile_wanted(stream_ids) -> bool:
    """True if any of these streams came from a portal this plugin manages.

    The first thing either receiver asks, because on an install with no portals
    -- or on the hundreds of channel/stream links an unrelated M3U account
    creates -- it has to be the only thing they cost.
    """
    from apps.channels.models import Stream

    return Stream.objects.filter(
        id__in=stream_ids,
        m3u_account__custom_properties__has_key=MARKER_KEY,
    ).exists()


def _auto_assign_enabled() -> bool:
    """Whether the user still wants this done for them.

    Read late, only once a portal stream is known to be involved, so the common
    case never pays for it.
    """
    from apps.plugins.models import PluginConfig

    from . import tasks

    cfg = PluginConfig.objects.filter(key=tasks.resolve_plugin_key()).first()
    if cfg is None:
        return True
    return bool((cfg.settings or {}).get("auto_apply_profile", True))


def assign(channel_ids, stream_ids) -> int:
    """Point the given channels at the Distalker profile. Never raises."""
    try:
        if not channel_ids or not stream_ids:
            return 0
        if not _profile_wanted(stream_ids):
            return 0
        if not _auto_assign_enabled():
            return 0

        from core.models import StreamProfile

        from apps.channels.models import Channel

        profile = StreamProfile.objects.filter(name=STREAM_PROFILE_NAME).first()
        if profile is None:
            # Sync has never run, so there is nothing to point at yet. The
            # profile is installed by the next sync, which also assigns it.
            return 0

        updated = (
            Channel.objects.filter(id__in=channel_ids)
            .exclude(stream_profile=profile)
            .update(stream_profile=profile)
        )
        if updated:
            logger.info(
                "distalker: assigned the %s profile to %d channel(s) gaining a "
                "portal stream",
                STREAM_PROFILE_NAME,
                updated,
            )
        return updated
    except Exception:
        logger.exception("distalker: could not auto-assign the stream profile")
        return 0


def _on_channel_stream_created(sender, instance, created, **kwargs):
    if not created:
        return
    assign([instance.channel_id], [instance.stream_id])


def _on_streams_changed(sender, instance, action, pk_set, reverse, **kwargs):
    targets = resolve_targets(action, instance.pk, pk_set, reverse)
    if targets:
        assign(*targets)


def connect() -> bool:
    """Wire the receivers up. Returns False if Django was not ready for it.

    The two imports are kept apart on purpose. No Django at all means the module
    was imported by the test suite or by tooling reading the manifest, and there
    is nothing to hook. Django without ``apps.channels`` means we *are* inside
    Dispatcharr and the hook failed -- which costs the user silent, unexplained
    auto-assignment for as long as nobody looks, so it is said out loud.
    """
    try:
        from django.db.models.signals import m2m_changed, post_save
    except ImportError:
        logger.debug("distalker: no Django here; auto-assign not connected")
        return False

    try:
        from apps.channels.models import Channel, ChannelStream
    except ImportError:
        logger.warning(
            "distalker: Django is loaded but apps.channels is not importable; "
            "channels will not receive the stream profile as portal streams are "
            "attached to them",
            exc_info=True,
        )
        return False

    try:
        post_save.connect(
            _on_channel_stream_created,
            sender=ChannelStream,
            dispatch_uid=DISPATCH_UID,
        )
        m2m_changed.connect(
            _on_streams_changed,
            sender=Channel.streams.through,
            dispatch_uid=DISPATCH_UID,
        )
        return True
    except Exception:
        logger.exception("distalker: could not connect the auto-assign signals")
        return False


def disconnect() -> None:
    """Unwire them, so a disabled plugin stops touching other people's saves."""
    try:
        from django.db.models.signals import m2m_changed, post_save

        from apps.channels.models import Channel, ChannelStream

        post_save.disconnect(
            sender=ChannelStream, dispatch_uid=DISPATCH_UID
        )
        m2m_changed.disconnect(
            sender=Channel.streams.through, dispatch_uid=DISPATCH_UID
        )
    except ImportError:
        logger.debug("distalker: no Django here; nothing to disconnect")
    except Exception:
        logger.exception("distalker: could not disconnect the auto-assign signals")
