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
import tempfile
from typing import Any, Dict, List, Optional

from .stalker_api import (
    ChannelEntry,
    Portal,
    PortalConfig,
    PortalError,
    encode_pseudo_url,
    python_executable,
    save_fallback,
    save_portal,
)

# Where Dispatcharr's own M3U upload endpoint puts files. Reusing it keeps our
# generated playlists alongside user-uploaded ones and inside the data volume.
M3U_DIR = "/data/uploads/m3us"

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
# Dispatcharr model wiring
# ---------------------------------------------------------------------------


def distalker_accounts():
    """Every M3U account this plugin owns."""
    from apps.m3u.models import M3UAccount

    return M3UAccount.objects.filter(
        custom_properties__has_key=MARKER_KEY
    )


def upsert_account(cfg: PortalConfig, file_path: str):
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
    # We regenerate the file ourselves before every refresh, so Dispatcharr
    # scheduling its own re-parse of a stale file would only cause confusion.
    account.refresh_interval = 0

    properties = dict(account.custom_properties or {})
    properties[MARKER_KEY] = {"slug": cfg.slug, "portal": cfg.url}
    account.custom_properties = properties

    account.save()

    if created:
        announce_new_account(account.id)
    return account, created


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


def sync_portal(cfg: PortalConfig, logger, trigger_refresh: bool = True) -> Dict[str, Any]:
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

    channels = portal.get_all_channels()
    genres = portal.get_genres()

    # The resolver reads this at tune time; publish it before the M3U lands so
    # a channel can never reference a portal Redis doesn't know about yet.
    save_portal(cfg)

    path = write_m3u(cfg.slug, build_m3u(portal, channels, genres))
    account, account_created = upsert_account(cfg, path)
    record_expiry(account, snapshot["expires"])

    if trigger_refresh:
        from apps.m3u.tasks import refresh_m3u_groups

        refresh_m3u_groups.delay(account.id)

    logger.info(
        "distalker: synced '%s' -- %d channels in %d groups -> %s",
        cfg.name,
        len(channels),
        len(genres),
        path,
    )

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
    }


def sync_all(portals: List[PortalConfig], logger, trigger_refresh: bool = True) -> Dict[str, Any]:
    """Sync every configured portal, surviving individual failures."""
    results: List[Dict[str, Any]] = []
    errors: List[str] = []

    for cfg in portals:
        try:
            results.append(sync_portal(cfg, logger, trigger_refresh=trigger_refresh))
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
