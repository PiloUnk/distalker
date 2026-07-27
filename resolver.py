#!/usr/bin/env python3
"""Resolve a Distalker pseudo-URL to a live portal link, then become ffmpeg.

Dispatcharr spawns this at tune time via the "Distalker" stream profile:

    <python> resolver.py http://distalker.invalid/<slug>/<b64cmd> <userAgent>

It reads the portal credentials from Redis (written there by the plugin's
sync action), asks the portal for a fresh link, and ``exec``s ffmpeg so that
MPEG-TS lands on stdout -- exactly what Dispatcharr expects from a
command-based stream profile.

It also has to cope with URLs that are not ours at all. Dispatcharr resolves
the stream profile from the *channel*, never from the source being played
(``# @TODO: honor stream's stream profile`` in ``apps/channels/models.py``),
so a channel that lists a portal source *and*, say, an Xtream one sends both
here. Anything that is not a pseudo-URL is handed to the fallback profile --
see :func:`passthrough` -- rather than refused, which would leave the channel
with no working source the moment Dispatcharr failed over.

Django is deliberately never imported: this runs on the hot path of every
tune, and loading Django would cost a second and open needless DB connections.

Exit codes matter. Anything non-zero tells Dispatcharr the stream failed so it
can fail over to another source, so every error path must exit non-zero
rather than hang.
"""

from __future__ import annotations

import os
import shutil
import sys

# Running as a script puts this directory on sys.path, so the shared protocol
# module imports as a plain top-level module.
import stalker_api
from stalker_api import PortalError


def log(message: str) -> None:
    """Diagnostics go to stderr, which Dispatcharr captures per channel."""
    print(f"[distalker] {message}", file=sys.stderr, flush=True)


def resolve(slug: str, cmd: str) -> tuple[str, stalker_api.PortalConfig]:
    """Return a playable URL for ``cmd``, refreshing the session if needed."""
    try:
        client = stalker_api.get_redis()
    except Exception as exc:
        # Not fatal any more: the portal is mirrored on disk, and the only
        # thing Redis holds exclusively is the session token.
        log(f"cannot reach Redis ({exc}); reading the mirrored portal instead")
        client = None

    cfg = stalker_api.load_portal(slug, client)
    if cfg is None:
        raise PortalError(
            f"portal '{slug}' is unknown to both Redis and "
            f"{stalker_api.STATE_DIR} -- run the plugin's 'Sync portals' "
            "action to republish it"
        )

    cached = stalker_api.get_cached_token(slug, client)
    portal = stalker_api.Portal(cfg, token=cached or "")

    if cached:
        # Optimistic path: reuse the cached token and skip the handshake.
        try:
            link = portal.create_link(cmd)
            return link, cfg
        except PortalError as exc:
            log(f"cached session rejected ({exc}); re-authenticating")
            stalker_api.clear_cached_token(slug, client)
            portal = stalker_api.Portal(cfg)

    portal.login()
    for warning in portal.warnings:
        log(f"{slug}: {warning}")
    stalker_api.set_cached_token(slug, portal.token, ttl=3600, client=client)
    return portal.create_link(cmd), cfg


def build_ffmpeg_command(cfg: stalker_api.PortalConfig, url: str) -> list:
    """Expand the portal's ffmpeg template into an argv list.

    Referer and Origin are derived from the **stream** URL, not the portal.
    Providers routinely serve the stream from a different host or port than
    the portal API, and expect the request to look like it came from there.
    """
    import shlex
    from urllib.parse import urlparse

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    headers = dict(stalker_api.stream_headers(cfg.model))
    headers["Origin"] = origin
    header_blob = "".join(f"{k}: {v}\r\n" for k, v in headers.items())

    template = cfg.ffmpeg_args or stalker_api.DEFAULT_FFMPEG_ARGS
    replacements = {
        "{url}": url,
        "{ua}": stalker_api.USER_AGENT,
        "{referer}": origin + "/",
        "{headers}": header_blob,
    }

    args = []
    for part in shlex.split(template):
        for placeholder, value in replacements.items():
            part = part.replace(placeholder, value)
        args.append(part)

    # Templates saved before the headers existed have no {headers} placeholder.
    # The MAG headers are part of what this plugin is, not user tuning, so add
    # them rather than silently dropping them for anyone with stored settings.
    if "-headers" not in args:
        insert_at = args.index("-i") if "-i" in args else len(args)
        args[insert_at:insert_at] = ["-headers", header_blob]

    return ["ffmpeg"] + args


# Used when no fallback has been published, or when the published one turns out
# to be unusable. Deliberately plain: a straight remux, no reconnect flags, no
# MAG headers -- this source is somebody else's and we know nothing about it.
BUILTIN_FALLBACK_ARGS = (
    "-hide_banner -loglevel error -user_agent {userAgent} -i {streamUrl} "
    "-c copy -f mpegts pipe:1"
)


def build_fallback_command(spec, url: str, user_agent: str) -> list:
    """Expand the published fallback profile into an argv list.

    ``spec`` is what the plugin published from the chosen Dispatcharr stream
    profile -- its command and parameters, with Dispatcharr's own placeholders
    left in. None, or anything that would re-enter this script, falls back to
    :data:`BUILTIN_FALLBACK_ARGS`.
    """
    import shlex

    replacements = {"{streamUrl}": url, "{userAgent}": user_agent or ""}

    def expand(parts):
        out = []
        for part in parts:
            for placeholder, value in replacements.items():
                part = part.replace(placeholder, value)
            out.append(part)
        return out

    if spec and "resolver.py" not in f"{spec.get('command')} {spec.get('parameters')}":
        try:
            return [spec["command"]] + expand(shlex.split(spec.get("parameters") or ""))
        except ValueError as exc:  # unbalanced quotes in the profile's parameters
            log(f"fallback profile parameters are unparseable ({exc}); using ffmpeg")
    elif spec:
        log("fallback profile points back at this resolver; using ffmpeg instead")

    return ["ffmpeg"] + expand(shlex.split(BUILTIN_FALLBACK_ARGS))


def passthrough(url: str, user_agent: str) -> int:
    """Play a source this plugin did not create, then never return.

    Reached whenever a Distalker channel falls over to one of its other
    sources. Redis being unreachable is not fatal here: the built-in command
    plays an ordinary HTTP stream perfectly well, and refusing to play would
    turn a working fallback source into a dead channel.
    """
    spec = None
    try:
        spec = stalker_api.load_fallback()
    except Exception as exc:
        log(f"cannot read the fallback profile from Redis ({exc}); using ffmpeg")

    command = build_fallback_command(spec, url, user_agent)
    log(f"not a portal source; playing it with {os.path.basename(command[0])}")

    executable = shutil.which(command[0]) or command[0]
    try:
        os.execv(executable, command)
    except OSError as exc:
        log(f"cannot execute {executable}: {exc}")
        return 1

    return 1  # unreachable: execv never returns on success


def probe(pseudo_url: str) -> int:
    """Resolve a link and report what the provider says, without playing it.

    Run inside the Dispatcharr container when a channel fails to tune::

        <python> resolver.py --probe http://distalker.invalid/<slug>/<b64cmd>

    ffmpeg reduces every failure to "Server returned 5XX", which hides the
    provider's actual message -- usually the useful part, such as a connection
    limit or an expired subscription.
    """
    import requests

    try:
        slug, cmd = stalker_api.decode_pseudo_url(pseudo_url)
    except ValueError as exc:
        log(str(exc))
        return 2

    try:
        link, cfg = resolve(slug, cmd)
    except Exception as exc:
        log(f"resolve failed: {exc}")
        return 1

    print(f"portal   : {cfg.name} ({cfg.url})")
    print(f"channel  : {cmd}")
    print(f"resolved : {link}")

    from urllib.parse import urlparse

    parsed = urlparse(link)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = dict(stalker_api.stream_headers(cfg.model))
    headers["User-Agent"] = stalker_api.USER_AGENT
    headers["Referer"] = origin + "/"
    headers["Origin"] = origin

    try:
        resp = requests.get(link, headers=headers, stream=True, timeout=20)
    except requests.RequestException as exc:
        print(f"request  : FAILED {exc}")
        return 1

    print(f"status   : {resp.status_code} {resp.reason}")
    for key in ("Content-Type", "Content-Length", "Location", "Server"):
        if key in resp.headers:
            print(f"  {key}: {resp.headers[key]}")

    body = resp.raw.read(400, decode_content=True) or b""
    if body:
        text = body.decode("utf-8", "replace").strip()
        print(f"body     : {text[:400]}")
    resp.close()

    return 0 if 200 <= resp.status_code < 300 else 1


def main(argv: list) -> int:
    if len(argv) < 2:
        log("usage: resolver.py [--probe] <distalker-url> [user-agent]")
        return 2

    if argv[1] == "--probe":
        if len(argv) < 3:
            log("usage: resolver.py --probe <distalker-url>")
            return 2
        return probe(argv[2])

    pseudo_url = argv[1]
    user_agent = argv[2] if len(argv) > 2 else ""

    # Another source on the same channel, handed to us only because Dispatcharr
    # picks the profile per channel rather than per source.
    if not stalker_api.is_pseudo_url(pseudo_url):
        return passthrough(pseudo_url, user_agent)

    try:
        slug, cmd = stalker_api.decode_pseudo_url(pseudo_url)
    except ValueError as exc:
        log(str(exc))
        return 2

    try:
        link, cfg = resolve(slug, cmd)
    except PortalError as exc:
        log(f"{slug}: {exc}")
        return 1
    except Exception as exc:  # never let an unexpected error look like success
        log(f"{slug}: unexpected failure: {exc}")
        return 1

    log(f"{slug}: resolved -> {link.split('?', 1)[0]}")

    command = build_ffmpeg_command(cfg, link)
    executable = shutil.which(command[0]) or command[0]
    try:
        os.execv(executable, command)
    except OSError as exc:
        log(f"cannot execute {executable}: {exc}")
        return 1

    return 1  # unreachable: execv never returns on success


if __name__ == "__main__":
    sys.exit(main(sys.argv))
