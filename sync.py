# Copyright (C) 2026 PiloUnk
# SPDX-License-Identifier: AGPL-3.0-only
# See LICENSE for the full terms and NOTICE for prior-art attribution.
"""Django-side work: generate M3U files and wire up Dispatcharr's models.

Everything in here runs inside Dispatcharr (plugin action or Celery task) and
may freely use the ORM. Nothing here runs on the tune-time hot path -- that is
``resolver.py``, which must stay Django-free.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional
from xml.sax.saxutils import escape, quoteattr

from .stalker_api import (
    ChannelEntry,
    Portal,
    PortalConfig,
    PortalError,
    as_int,
    encode_pseudo_url,
    hold_auto_sync,
    python_executable,
    save_fallback,
    save_portal,
)

# Where Dispatcharr's own M3U upload endpoint puts files. Reusing it keeps our
# generated playlists alongside user-uploaded ones and inside the data volume.
M3U_DIR = "/data/uploads/m3us"

# The same idea for guides. Kept out of Dispatcharr's own 'cached_epg', which it
# fills with files named after a source id -- ours are named after a portal and
# are inputs to that machinery rather than products of it.
XMLTV_DIR = "/data/uploads/epgs"

# How long a sync mutes the scheduled path after asking for a re-read. Matches
# plugin.AUTO_SYNC_COOLDOWN, and is defined here because this module may not
# import from plugin.py -- the dependency runs the other way.
AUTO_SYNC_COOLDOWN = 1800

STREAM_PROFILE_NAME = "Distalker"
ACCOUNT_PREFIX = "Distalker: "

# Dispatcharr resolves the stream profile from the channel, so once a channel
# carries ours, *every* source on it reaches the resolver -- including sources
# from other providers. Those are handed to this profile instead.
DEFAULT_FALLBACK_PROFILE = "ffmpeg"

# Profiles Dispatcharr implements internally rather than as a command; there is
# no process to spawn, so the resolver cannot stand in for them.
INTERNAL_PROFILES = ("Proxy", "Redirect")

# Marker written into M3UAccount.custom_properties so we can find our own
# accounts again without relying on the display name, which users may rename.
MARKER_KEY = "distalker"


# ---------------------------------------------------------------------------
# M3U generation
# ---------------------------------------------------------------------------


def _attr(value: str) -> str:
    """Make a string safe to sit inside a quoted M3U attribute."""
    return (value or "").replace('"', "").replace("\n", " ").replace("\r", " ").strip()


def build_m3u(
    portal: Portal,
    channels: List[ChannelEntry],
    genres: Dict[str, str],
) -> str:
    """Render the portal's channel list as a standard M3U playlist.

    ``group-title`` carries the portal's own genre, which is what Dispatcharr
    turns into Channel Groups -- so the existing M3U Accounts / Groups UI does
    all the filtering, and this plugin does none.

    Catch-up is deliberately not advertised here even though the portal tells
    us about it and Dispatcharr would read it -- see ``ChannelEntry`` for why
    the badge would be a promise nothing can keep.
    """
    slug = portal.cfg.slug
    lines = ["#EXTM3U"]

    for channel in channels:
        group = genres.get(channel.genre_id, "").strip() or "Other"
        # A stable tvg-id now means EPG can bind to these channels later
        # without rewriting every stream hash.
        tvg_id = f"{slug}.{channel.channel_id}" if channel.channel_id else ""

        attrs = [
            f'tvg-id="{_attr(tvg_id)}"',
            f'tvg-name="{_attr(channel.name)}"',
            f'tvg-logo="{_attr(portal.logo_url(channel.logo))}"',
            f'group-title="{_attr(group)}"',
        ]
        if channel.number:
            attrs.append(f'tvg-chno="{_attr(channel.number)}"')

        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{_attr(channel.name)}")
        lines.append(encode_pseudo_url(slug, channel.cmd))

    return "\n".join(lines) + "\n"


def write_m3u(slug: str, content: str) -> str:
    """Write the playlist atomically so a refresh never reads a half-file."""
    os.makedirs(M3U_DIR, exist_ok=True)
    path = os.path.join(M3U_DIR, f"distalker-{slug}.m3u")

    fd, temp_path = tempfile.mkstemp(dir=M3U_DIR, prefix=f".distalker-{slug}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise

    return path


# ---------------------------------------------------------------------------
# XMLTV generation
# ---------------------------------------------------------------------------
#
# Dispatcharr reads a guide with lxml's iterparse and takes five things from it
# (apps/epg/tasks.py): a channel's id, its first display-name and its icon;
# then each programme's channel, start, stop, title, desc and sub-title.
# Everything else in the XMLTV vocabulary is ignored, so none of it is written.

# Characters XML 1.0 has no way to carry. Portals do send them -- a stray 0x03
# inside a programme description is enough to make lxml's recovery drop the
# element around it, so they are removed rather than escaped.
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xml_text(value: Any) -> str:
    """A string safe to place between XML tags."""
    return escape(_ILLEGAL_XML.sub("", str(value or "")))


def _xmltv_time(value: Any) -> str:
    """A Unix timestamp as XMLTV writes it, or '' if it is not one.

    ``YYYYMMDDHHMMSS +0000``: exactly the 20 characters Dispatcharr's
    ``parse_xmltv_time`` expects, and always UTC, because a portal's epoch is
    an instant and the local time it corresponds to is nobody's business here.
    """
    seconds = as_int(value, -1)
    if seconds <= 0:
        return ""
    try:
        moment = datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.strftime("%Y%m%d%H%M%S +0000")


def build_xmltv(
    portal: Portal,
    channels: List[ChannelEntry],
    epg_data: Dict[str, Any],
) -> Iterator[str]:
    """Yield an XMLTV document for the channels the portal has a guide for.

    A generator, and ``epg_data`` is emptied as it goes: the guide for a large
    portal is the biggest structure this plugin ever holds, and building the
    document as one string would mean holding it twice. The caller writes each
    piece out and nothing accumulates.

    Only channels with at least one programme get an entry. A ``<channel>``
    with nothing under it still becomes a row in Dispatcharr's EPG table, and
    13,000 rows that will never match a programme are not a guide, they are
    thirteen thousand empty promises in the channel-to-EPG picker.
    """
    slug = portal.cfg.slug

    def tvg_id(channel: ChannelEntry) -> str:
        # The identifier already written into the playlist. The two must agree
        # exactly or nothing binds -- see test_the_playlist_and_the_guide_agree.
        return f"{slug}.{channel.channel_id}" if channel.channel_id else ""

    listed = [
        channel for channel in channels
        if tvg_id(channel) and epg_data.get(channel.channel_id)
    ]

    yield '<?xml version="1.0" encoding="UTF-8"?>\n'
    yield '<tv generator-info-name="Distalker">\n'

    for channel in listed:
        yield f"  <channel id={quoteattr(tvg_id(channel))}>\n"
        yield f"    <display-name>{_xml_text(channel.name)}</display-name>\n"
        logo = portal.logo_url(channel.logo)
        if logo:
            yield f"    <icon src={quoteattr(logo)} />\n"
        yield "  </channel>\n"

    for channel in listed:
        # pop, not get: this is where the memory goes back.
        programmes = epg_data.pop(channel.channel_id, None) or []
        channel_id = quoteattr(tvg_id(channel))
        for start, stop, title, description in _timeline(programmes):
            yield (
                f"  <programme start={quoteattr(start)} stop={quoteattr(stop)} "
                f"channel={channel_id}>\n"
            )
            yield f"    <title>{title}</title>\n"
            if description:
                yield f"    <desc>{description}</desc>\n"
            yield "  </programme>\n"

    yield "</tv>\n"


def _timeline(programmes: Any) -> Iterator[tuple]:
    """One channel's programmes, in order and without overlaps.

    Portals do send overlapping entries -- the same show listed twice with two
    start times and one end, which is what a guide that has been corrected in
    place looks like from outside. XMLTV permits it and readers do not expect
    it: Dispatcharr picks a programme for an instant by searching the ones that
    span it (``_match_epg_program_by_timeslot``), so two candidates for the same
    minute is an arbitrary answer to "what is on now".

    Earliest start wins, since it is the one covering the whole slot. Anything
    beginning before the kept programme ends is dropped; touching exactly, which
    is what back-to-back programmes do, is not an overlap and is kept.
    """
    usable = []
    for programme in programmes if isinstance(programmes, list) else []:
        if not isinstance(programme, dict):
            continue
        start = _xmltv_time(programme.get("start_timestamp"))
        stop = _xmltv_time(programme.get("stop_timestamp"))
        # A programme without both ends is not a programme. Dispatcharr would
        # store it with a nonsense duration rather than reject it -- and the
        # portal's own 'duration' field is no help: it arrives negative on
        # programmes that plainly last two hours.
        if not start or not stop or stop <= start:
            continue
        title = _xml_text(programme.get("name"))
        if not title:
            continue
        usable.append((start, stop, title, _xml_text(programme.get("descr"))))

    # The timestamps sort correctly as strings: fixed width, most significant
    # first, and all of them UTC.
    usable.sort(key=lambda item: (item[0], item[1]))

    last_stop = ""
    for entry in usable:
        if entry[0] < last_stop:
            continue
        last_stop = entry[1]
        yield entry


def write_xmltv(slug: str, chunks: Iterator[str]) -> str:
    """Stream a guide to disk atomically, as :func:`write_m3u` does for a playlist."""
    os.makedirs(XMLTV_DIR, exist_ok=True)
    path = os.path.join(XMLTV_DIR, f"distalker-{slug}.xml")

    fd, temp_path = tempfile.mkstemp(dir=XMLTV_DIR, prefix=f".distalker-{slug}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for chunk in chunks:
                handle.write(chunk)
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise

    return path


# ---------------------------------------------------------------------------
# Dispatcharr model wiring
# ---------------------------------------------------------------------------


def distalker_accounts():
    """Every M3U account this plugin owns."""
    from apps.m3u.models import M3UAccount

    return M3UAccount.objects.filter(
        custom_properties__has_key=MARKER_KEY
    )


def upsert_account(cfg: PortalConfig, file_path: str, refresh_hours: int = 0):
    """Create or update the M3U account backing one portal.

    Dispatcharr supports file-backed accounts as a first-class refresh source
    (``apps/m3u/tasks.py`` branches on ``account.file_path``), so the plugin
    never needs to serve HTTP -- which is the entire point of not needing a
    port per portal.
    """
    from apps.m3u.models import M3UAccount

    account = distalker_accounts().filter(
        custom_properties__distalker__slug=cfg.slug
    ).first()

    created = False
    if account is None:
        account, created = M3UAccount.objects.get_or_create(
            name=ACCOUNT_PREFIX + cfg.name,
            defaults={"account_type": M3UAccount.Types.STADNARD},
        )

    account.name = ACCOUNT_PREFIX + cfg.name
    account.file_path = file_path
    account.server_url = None
    account.account_type = M3UAccount.Types.STADNARD
    account.is_active = True
    # Portals typically allow a single connection per MAC; exceeding it is the
    # fastest way to get the account blocked.
    account.max_streams = cfg.max_streams
    # Dispatcharr's own schedule, turned into ours. Left at 0 the account is
    # never re-read on its own and every fetch is a button press -- which is
    # what this plugin did for its whole life, because a plugin cannot register
    # a Celery task of its own (see tasks.py). Set, Dispatcharr creates and
    # runs the periodic task itself, and the m3u_refresh it ends with is what
    # calls the plugin back to re-fetch the portal. The clock is borrowed
    # rather than built.
    account.refresh_interval = max(0, int(refresh_hours or 0))

    properties = dict(account.custom_properties or {})
    properties[MARKER_KEY] = {"slug": cfg.slug, "portal": cfg.url}
    account.custom_properties = properties

    account.save()

    if created:
        announce_new_account(account.id)
    return account, created


def distalker_epg_sources():
    """Every EPG source this plugin owns, found by marker rather than by name."""
    from apps.epg.models import EPGSource

    return EPGSource.objects.filter(custom_properties__has_key=MARKER_KEY)


def upsert_epg_source(cfg: PortalConfig, file_path: str):
    """Create or update the EPG source backing one portal.

    The mirror image of :func:`upsert_account`, and for the same reason: a
    source with a ``file_path`` and no ``url`` is a first-class case in
    Dispatcharr (``apps/epg/tasks.py`` takes the local-file branch when
    ``not source.url``), so the guide costs no HTTP endpoint either.
    """
    from apps.epg.models import EPGSource

    source = distalker_epg_sources().filter(
        **{f"custom_properties__{MARKER_KEY}__slug": cfg.slug}
    ).first()

    created = False
    if source is None:
        source, created = EPGSource.objects.get_or_create(
            name=ACCOUNT_PREFIX + cfg.name,
            defaults={"source_type": "xmltv"},
        )

    source.name = ACCOUNT_PREFIX + cfg.name
    source.source_type = "xmltv"
    source.file_path = file_path
    # Blank, and that is what selects the local-file branch. A source with both
    # would be downloaded from the URL and our file ignored.
    source.url = None
    source.is_active = True
    # We rewrite the file ourselves before asking for a re-parse, so a schedule
    # of Dispatcharr's own would only re-read a file that had not changed.
    source.refresh_interval = 0

    properties = dict(source.custom_properties or {})
    properties[MARKER_KEY] = {"slug": cfg.slug, "portal": cfg.url}
    source.custom_properties = properties

    source.save()
    return source, created


def deactivate_epg_source(cfg: PortalConfig) -> bool:
    """Switch off the guide for a portal that no longer asks for one.

    Not deleted: the plugin does not remove things a user can see and may have
    configured around -- the same restraint that leaves an M3U account standing
    when its portal line goes away. Deactivating is enough to stop a guide that
    is no longer refreshed from binding itself to channels.
    """
    source = distalker_epg_sources().filter(
        **{f"custom_properties__{MARKER_KEY}__slug": cfg.slug}
    ).first()
    if source is None or not source.is_active:
        return False

    source.is_active = False
    source.save(update_fields=["is_active"])
    return True


def request_reparse(account_id: int, refresh_hours: int) -> str:
    """Ask Dispatcharr to read the playlist just written. Returns which task.

    **Exactly one task, never both.** ``refresh_single_m3u_account`` refreshes
    the groups itself, so dispatching ``refresh_m3u_groups`` alongside it puts
    the pair in a race for the same per-account lock -- and the loser reports
    "Failed to refresh M3U groups" at the user, leaving the account in Pending
    Setup. Which is worth stating in a function of its own, because the two
    calls read as complementary and are not.

    Off a schedule, only the groups are refreshed: that has been this plugin's
    behaviour throughout, and importing the streams is a step the user
    completes by choosing groups.

    On a schedule, the whole account is re-read instead. Dispatcharr looked at
    the *previous* playlist moments ago -- that refresh is what woke us to
    write this one -- so without this the channel list would sit one cycle
    behind the portal for ever. It emits ``m3u_refresh`` again, which is the
    event that brought us here; the cooldown claimed before the sync started is
    what keeps that from going round for ever.
    """
    from apps.m3u.tasks import refresh_m3u_groups, refresh_single_m3u_account

    if refresh_hours:
        # Before dispatching, not after: the task about to run emits the event
        # the schedule listens for, and a sync the user started by hand claims
        # no cooldown of its own -- so without this its echo comes back as a
        # full re-fetch of every portal.
        hold_auto_sync(AUTO_SYNC_COOLDOWN)
        refresh_single_m3u_account.delay(account_id)
        return "refresh_single_m3u_account"

    refresh_m3u_groups.delay(account_id)
    return "refresh_m3u_groups"


def refresh_epg_source(source_id: int) -> None:
    """Ask Dispatcharr to read the guide we just wrote.

    Its own task, dispatched by name from a registered module rather than
    defined here -- a task a plugin defines cannot be consumed at all, which is
    the whole story recorded in tasks.py.
    """
    from apps.epg.tasks import refresh_epg_data

    refresh_epg_data.delay(source_id)


def apply_refresh_interval(refresh_hours: int, logger) -> int:
    """Put the schedule on every account this plugin owns, without fetching.

    ``upsert_account`` writes the interval too, but only for portals a sync
    actually fetched -- and changing the schedule changes nothing about any
    portal, so the plan calls them all unchanged and fetches none of them. The
    setting would appear to do nothing until the next unrelated re-fetch.

    So it is applied here instead, on every sync, for every account: a handful
    of small updates and no network at all.
    """
    wanted = max(0, int(refresh_hours or 0))
    changed = 0
    try:
        for account in distalker_accounts():
            if account.refresh_interval != wanted:
                account.refresh_interval = wanted
                account.save(update_fields=["refresh_interval"])
                changed += 1
    except Exception:
        logger.debug("distalker: could not apply the refresh schedule", exc_info=True)
        return 0

    if changed:
        logger.info(
            "distalker: %s on %d M3U account(s)",
            f"scheduled refresh set to every {wanted}h" if wanted
            else "scheduled refresh switched off",
            changed,
        )
    return changed


def announce_new_account(account_id: int) -> None:
    """Tell the open UI that an M3U account it has never heard of now exists.

    Dispatcharr emits ``playlist_created`` from its own API view, so a browser
    that adds an M3U learns about it immediately. An account created from a
    background thread emits nothing -- and the frontend's refresh handler bails
    out on any account it has not got in its store ("Playlist not in store yet
    ... waiting for playlist_created event"), so without this the new row does
    not appear until the page is reloaded, and every status and progress update
    about it is discarded on the way in.

    Best effort in every direction: this is Dispatcharr's own internal event,
    not a plugin contract, and no part of a sync should fail because a browser
    was not told about it.
    """
    try:
        from core.utils import send_websocket_update

        send_websocket_update(
            "updates", "update", {"type": "playlist_created", "playlist_id": account_id}
        )
    except Exception:
        logging.getLogger(__name__).debug(
            "distalker: could not announce the new M3U account", exc_info=True
        )


def announce(title: str, message: str, failed: bool = False) -> None:
    """Put a sync's result where the user is, rather than where the panel is.

    The plugin panel cannot refresh itself, so everything an action reports
    into the 'Last action' box stays invisible until someone presses the
    refresh icon on the Plugins page -- which is a poor way to learn that a
    background sync finished ten minutes ago. Dispatcharr's own notification
    centre is not: 'high' is the priority at which its frontend also raises a
    toast, and the entry survives in the bell afterwards.

    One row, reused: a stable notification_key means Distalker holds a single
    notification rather than piling one up per sync, and a real row means the
    Dismiss button in the UI has something to dismiss. A fabricated payload
    would hand the interface an id its API cannot act on.

    Never raises. The model, the helper and the event name are all Dispatcharr
    internals rather than anything a plugin is promised.
    """
    try:
        from core.models import SystemNotification
        from core.utils import send_websocket_notification

        notification, _ = SystemNotification.objects.update_or_create(
            notification_key="distalker-sync",
            defaults={
                "notification_type": (
                    SystemNotification.NotificationType.WARNING
                    if failed
                    else SystemNotification.NotificationType.INFO
                ),
                "priority": SystemNotification.Priority.HIGH,
                "title": title,
                "message": message[:1000],
                "is_active": True,
            },
        )
        send_websocket_notification(notification)
    except Exception:
        logging.getLogger(__name__).debug(
            "distalker: could not raise a notification", exc_info=True
        )


def record_expiry(account, expires) -> None:
    """Put the portal's expiry where Dispatcharr already shows expiry dates.

    ``M3UAccountProfile.exp_date`` is what ``M3UAccountSerializer`` exposes as
    ``earliest_expiration``/``all_expirations``, i.e. the account expiry the UI
    displays for Xtream accounts. Writing it there means a portal subscription
    is reported like any other, with no interface of our own.

    Best effort in both directions: no expiry leaves whatever is there alone
    (the user may have typed one), and a failure is never worth losing a sync
    over.
    """
    if expires is None:
        return
    try:
        profile = account.profiles.filter(is_default=True).first()
        if profile is None:
            return
        if profile.exp_date != expires:
            profile.exp_date = expires
            profile.save(update_fields=["exp_date"])
    except Exception:
        pass


def portal_status(cfg: PortalConfig) -> Dict[str, Any]:
    """What is known about one portal without asking the portal.

    Feeds the panel's only report, so it must stay cheap enough to run for
    every portal on every action: two indexed queries and no network.
    """
    from apps.channels.models import Stream

    account = distalker_accounts().filter(
        custom_properties__distalker__slug=cfg.slug
    ).first()
    if account is None:
        return {"name": cfg.name, "synced": False, "channels": 0, "expires": None}

    profile = account.profiles.filter(is_default=True).first()
    return {
        "name": cfg.name,
        "synced": True,
        "channels": Stream.objects.filter(m3u_account=account).count(),
        "expires": getattr(profile, "exp_date", None),
    }


def install_stream_profile():
    """Create/refresh the stream profile that hands tuning to resolver.py.

    The interpreter is Dispatcharr's own, so the resolver runs with
    ``requests`` and ``redis`` already available regardless of whether this is
    a Docker or bare-metal install -- but picked deliberately rather than taken
    from ``sys.executable``, which is the uWSGI binary in the process that now
    runs the sync. See ``stalker_api.python_executable``.
    """
    from core.models import StreamProfile

    resolver_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resolver.py")
    interpreter = python_executable()

    profile, created = StreamProfile.objects.get_or_create(
        name=STREAM_PROFILE_NAME,
        defaults={
            "command": interpreter,
            "parameters": f"{resolver_path} {{streamUrl}} {{userAgent}}",
            "is_active": True,
            "locked": False,
        },
    )

    if not created:
        profile.command = interpreter
        profile.parameters = f"{resolver_path} {{streamUrl}} {{userAgent}}"
        profile.is_active = True
        profile.save()

    return profile, created


def publish_fallback(profile_name: str) -> str:
    """Publish the profile that plays a channel's non-Distalker sources.

    Returns a warning to show the user, or "" when the choice was honoured.
    Never raises: a fallback that cannot be resolved must not stop a sync, it
    just means the resolver falls back to its own built-in ffmpeg command.
    """
    from core.models import StreamProfile

    name = (profile_name or "").strip() or DEFAULT_FALLBACK_PROFILE

    if name == STREAM_PROFILE_NAME:
        save_fallback("", "")
        return (
            f"fallback profile cannot be '{STREAM_PROFILE_NAME}' itself -- that would "
            "loop; using the built-in ffmpeg command instead"
        )

    profile = StreamProfile.objects.filter(name=name).first()
    if profile is None:
        save_fallback("", "")
        return (
            f"no stream profile named '{name}'; other sources on Distalker channels "
            "will use the built-in ffmpeg command"
        )

    if profile.locked and profile.name in INTERNAL_PROFILES:
        save_fallback("", "")
        return (
            f"'{name}' is handled inside Dispatcharr rather than by a command, so the "
            "resolver cannot stand in for it; using the built-in ffmpeg command instead"
        )

    if not (profile.command or "").strip():
        save_fallback("", "")
        return f"stream profile '{name}' has no command; using the built-in ffmpeg one"

    save_fallback(profile.command, profile.parameters or "")
    return ""


def apply_stream_profile() -> Dict[str, int]:
    """Point everything this plugin owns at the Distalker stream profile.

    Dispatcharr picks the profile from two different places depending on what is
    being played, and both need setting:

    * playing a **channel** reads the channel's own profile and ignores the
      stream's -- the ``# @TODO: honor stream's stream profile`` in
      ``apps/channels/models.py``;
    * playing a **stream** directly, which is what the preview button and a
      by-stream URL do, reads ``Stream.get_stream_profile()`` instead.

    Miss the second and a preview falls back to the global default, which for
    most installs is ``Proxy``: Dispatcharr then fetches the pseudo-URL itself
    and fails to resolve ``distalker.invalid``, never reaching the resolver.

    Returns the counts it changed, per kind.
    """
    from apps.channels.models import Channel, Stream

    profile, _ = install_stream_profile()
    accounts = list(distalker_accounts().values_list("id", flat=True))
    if not accounts:
        return {"channels": 0, "streams": 0}

    channel_ids = list(
        Channel.objects.filter(streams__m3u_account_id__in=accounts)
        .exclude(stream_profile=profile)
        .values_list("id", flat=True)
        .distinct()
    )
    if channel_ids:
        Channel.objects.filter(id__in=channel_ids).update(stream_profile=profile)

    streams = Stream.objects.filter(m3u_account_id__in=accounts).exclude(
        stream_profile=profile
    )
    stream_count = streams.update(stream_profile=profile)

    return {"channels": len(channel_ids), "streams": stream_count}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


# Three attempts at anything a sync asks the portal for. A provider that
# hiccups once should not cost the user their whole line-up until the next
# scheduled run, and a sync has the time -- nothing is waiting on it.
#
# Worst case per call is (retries + 1) x timeout plus the backoff, so 187s at
# the default 60s timeout. Portals are synced one after another under a lock
# that expires after 1800s (see claim_sync_lock): raising either number far
# enough that a run could outlive its own lock would let a second run start on
# top of the first.
SYNC_RETRIES = 2


def sync_portal(
    cfg: PortalConfig,
    logger,
    trigger_refresh: bool = True,
    refresh_hours: int = 0,
) -> Dict[str, Any]:
    """Full sync for one portal: log in, fetch, write M3U, refresh account."""
    portal = Portal(cfg, retries=SYNC_RETRIES)
    portal.login()
    for warning in portal.warnings:
        logger.warning("distalker: %s: %s", cfg.name, warning)

    # Read while the session is open and before the long call: two short
    # requests that cost nothing next to a whole line-up, and a blocked account
    # is worth saying out loud rather than leaving as an empty channel list.
    snapshot = portal.account_snapshot()
    if snapshot["blocked"]:
        logger.warning("distalker: portal '%s' reports the account as blocked", cfg.name)

    channels = portal.list_channels(
        progress=lambda note: logger.info("distalker: %s: %s", cfg.name, note)
    )
    genres = portal.get_genres()

    # The resolver reads this at tune time; publish it before the M3U lands so
    # a channel can never reference a portal Redis doesn't know about yet.
    save_portal(cfg)

    path = write_m3u(cfg.slug, build_m3u(portal, channels, genres))
    account, account_created = upsert_account(cfg, path, refresh_hours)
    record_expiry(account, snapshot["expires"])

    if trigger_refresh:
        request_reparse(account.id, refresh_hours)

    logger.info(
        "distalker: synced '%s' -- %d channels in %d groups -> %s",
        cfg.name,
        len(channels),
        len(genres),
        path,
    )

    rewritten = sum(1 for channel in channels if channel.cmd_rewritten)
    if rewritten:
        # Worth saying: this changes what the portal is handed back at tune
        # time, and it is the difference between a channel that plays and one
        # that does not on the providers concerned.
        logger.info(
            "distalker: %s: %d channel(s) answered with a resolved link rather "
            "than a marker, and were rewritten to one -- see canonical_cmd",
            cfg.name,
            rewritten,
        )

    epg = sync_epg(cfg, portal, channels, logger, trigger_refresh=trigger_refresh)

    return {
        "portal": cfg.name,
        "slug": cfg.slug,
        "channels": len(channels),
        "groups": len(genres),
        "account_id": account.id,
        "account_created": account_created,
        "file": path,
        "expires": snapshot["expires"],
        "blocked": snapshot["blocked"],
        "epg": epg,
    }


def sync_epg(
    cfg: PortalConfig,
    portal: Portal,
    channels: List[ChannelEntry],
    logger,
    trigger_refresh: bool = True,
) -> Optional[Dict[str, Any]]:
    """Fetch the guide and hand it to Dispatcharr. Returns None when off.

    Runs last, and cannot fail the sync around it. That is the whole design of
    this function: the guide is an extra, it is by a wide margin the largest
    thing fetched here, and a portal has many more ways to disappoint over
    100 MB than over a channel list. A sync that ends with a working line-up
    and no guide is a good outcome; one that loses the line-up because the
    guide was too big is not.
    """
    if not cfg.epg:
        # Inside its own guard for the same reason as everything else here: a
        # portal that never wanted a guide must not fail its sync over one.
        try:
            if deactivate_epg_source(cfg):
                logger.info(
                    "distalker: '%s' no longer asks for a guide; its EPG source "
                    "is switched off (not deleted)",
                    cfg.name,
                )
        except Exception:
            logger.debug("distalker: could not check for a stale guide", exc_info=True)
        return None

    try:
        logger.info(
            "distalker: %s: fetching %d hours of guide for %d channels",
            cfg.name,
            cfg.epg_hours,
            len(channels),
        )
        epg_data = portal.get_epg_info(cfg.epg_hours, scratch_dir=_scratch_dir())
        if not epg_data:
            # Said plainly, because it is a property of the provider rather
            # than a fault to chase: a portal can carry thousands of channels
            # and no programmes for any of them. Leaving 'epg=1' on costs one
            # wasted request per sync and nothing else.
            logger.warning(
                "distalker: portal '%s' has no programme guide -- it answered "
                "with an empty one. Remove 'epg=1' from its line to stop "
                "asking.",
                cfg.name,
            )
            return None

        # build_xmltv empties epg_data as it writes, so nothing is counted
        # afterwards -- count now, while it is still there to count.
        covered = sum(1 for c in channels if epg_data.get(c.channel_id))

        epg_path = write_xmltv(cfg.slug, build_xmltv(portal, channels, epg_data))
        source, source_created = upsert_epg_source(cfg, epg_path)

        if trigger_refresh:
            refresh_epg_source(source.id)

        logger.info(
            "distalker: guide for '%s' -- %d channels covered -> %s",
            cfg.name,
            covered,
            epg_path,
        )
        return {
            "channels": covered,
            "hours": cfg.epg_hours,
            "file": epg_path,
            "source_id": source.id,
            "source_created": source_created,
        }
    except Exception as exc:
        logger.warning(
            "distalker: could not build the guide for '%s': %s", cfg.name, exc
        )
        logger.debug("distalker: guide failure detail", exc_info=True)
        return None


def _scratch_dir() -> Optional[str]:
    """Where to stream a guide while it downloads.

    Beside the finished file rather than in the system temp: the guide can be
    hundreds of megabytes, and a container's /tmp is often a small tmpfs -- in
    memory, which is precisely what streaming to a file is meant to avoid.
    """
    try:
        os.makedirs(XMLTV_DIR, exist_ok=True)
        return XMLTV_DIR
    except OSError:
        return None


def sync_all(
    portals: List[PortalConfig],
    logger,
    trigger_refresh: bool = True,
    refresh_hours: int = 0,
) -> Dict[str, Any]:
    """Sync every configured portal, surviving individual failures."""
    results: List[Dict[str, Any]] = []
    errors: List[str] = []

    for cfg in portals:
        try:
            results.append(
                sync_portal(
                    cfg, logger, trigger_refresh=trigger_refresh,
                    refresh_hours=refresh_hours,
                )
            )
        except PortalError as exc:
            errors.append(f"{cfg.name}: {exc}")
            logger.error("distalker: sync failed for '%s': %s", cfg.name, exc)
        except Exception as exc:
            errors.append(f"{cfg.name}: unexpected error: {exc}")
            logger.exception("distalker: unexpected sync failure for '%s'", cfg.name)

    return {"synced": results, "errors": errors}


def test_portal(cfg: PortalConfig) -> Dict[str, Any]:
    """Log in and read the genre list, without touching any Dispatcharr model.

    Deliberately *not* the channel list: that arrives in a single response the
    portal may take minutes to assemble, and this runs on the request thread,
    where anything slower than the shortest proxy timeout between the browser
    and Dispatcharr comes back as a 504. Genres are a short list and prove the
    same thing -- that the MAC authenticates and the session works. The channel
    count comes from a sync, which no longer blocks a request.

    Retries are off here for the same reason, and deliberately not shared with
    :data:`SYNC_RETRIES`: three attempts at a 60-second timeout is three minutes
    of a request thread, and the proxy in front of Dispatcharr gives up long
    before that. A portal that needs a second attempt to answer is a portal
    this action should report as unwell, not one it should wait out.
    """
    portal = Portal(cfg)
    portal.login()
    genres = portal.get_genres()
    return {
        "portal": cfg.name,
        "url": cfg.url,
        # What the portal actually asked for, not what was configured: a line
        # carrying credentials against a portal that never requests them is
        # reported as the handshake-only portal it is.
        "auth": portal.auth_method,
        "groups": len(genres),
        "warnings": portal.warnings,
    }
