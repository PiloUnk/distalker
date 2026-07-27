"""Stalker portal client and shared Redis state.

This module is deliberately free of Django imports. It is shared by two very
different callers:

  * ``plugin.py`` / ``sync.py``, running inside Dispatcharr's Django process
  * ``resolver.py``, running as a bare subprocess spawned by the stream
    profile at tune time, with no Django loaded

Only ``requests``, ``redis`` and the standard library may be imported here.
Both are already dependencies of Dispatcharr, so no extra install is needed.

The protocol implementation is a Python reimplementation informed by stalkerhek
(MIT), originally by erkexzcx and continued in later forks. See LICENSE.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests

# The portal only ever sees a MAG set-top box. Every request carries this
# fingerprint; portals reject anything that looks like a browser.
USER_AGENT = (
    "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
    "(KHTML, like Gecko) MAG200 stbapp ver: 4 rev: 2116 Mobile Safari/533.3"
)

DEFAULT_MODEL = "MAG254"
DEFAULT_SERIAL = "0000000000000"
DEFAULT_DEVICE_ID = "f" * 64
DEFAULT_SIGNATURE = "f" * 64
DEFAULT_TIMEZONE = "UTC"

# Seconds to wait on any single portal request. Generous by HTTP standards
# because get_all_channels is one request for the entire line-up, and a busy
# portal can take minutes to assemble it.
DEFAULT_TIMEOUT = 60

# STB identity keys accepted both as global plugin settings and as per-portal
# 'key=value' overrides on a portal line.
STB_KEYS = ("model", "serial", "device_id", "device_id2", "signature", "timezone")

# No -reconnect, on purpose, and it is not an oversight to be helpfully fixed.
#
# ffmpeg's own reconnection retries the URL it was given -- and a Stalker link
# expires within seconds, so it retries something that is guaranteed dead, for
# as long as anyone lets it. Meanwhile the process stays alive and produces
# nothing, so Dispatcharr sees neither an exit nor an error, and its retry and
# failover never fire: a channel with a portal source and an Xtream one behind
# it sat frozen instead of switching, which is the whole point of having two.
#
# Dispatcharr respawns this command on every connection attempt, which reruns
# the resolver and asks the portal for a *fresh* link. That is the only
# reconnection that can succeed here, so it gets to own it.
#
# -rw_timeout (microseconds) covers the other half: a source that stalls
# without ever closing. Ten seconds, because Dispatcharr tries three times
# before moving to the next source and a client gives up at around fifty --
# three attempts have to fit inside that with room for each portal round-trip.
# A live stream that goes ten seconds without a byte has already failed.
DEFAULT_FFMPEG_ARGS = (
    "-hide_banner -loglevel error "
    "-user_agent {ua} -referer {referer} -headers {headers} "
    "-rw_timeout 10000000 "
    "-i {url} -c copy -f mpegts pipe:1"
)

# Defaults this plugin shipped before 0.9.1, replaced on sight because they
# defeat that failover. Two of them: the manifest's copy had drifted from the
# code's and lost the MAG headers, and a background sync persisted whichever it
# was holding.
SUPERSEDED_FFMPEG_ARGS = (
    "-hide_banner -loglevel error -user_agent {ua} -referer {referer} "
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-i {url} -c copy -f mpegts pipe:1",
    "-hide_banner -loglevel error -user_agent {ua} -referer {referer} "
    "-headers {headers} "
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-i {url} -c copy -f mpegts pipe:1",
)


def is_superseded_ffmpeg_args(value: str) -> bool:
    """Whether this is a default we shipped, rather than something chosen.

    Whitespace is normalised so a line the user only reflowed still counts as
    untouched; anything else they wrote is theirs and stays.
    """
    current = " ".join((value or "").split())
    return any(current == " ".join(old.split()) for old in SUPERSEDED_FFMPEG_ARGS)

# Headers a MAG box sends when fetching the stream itself, beyond the
# User-Agent and Referer that ffmpeg has dedicated flags for. Providers do
# check these: a request that authenticated fine against the portal can still
# be refused at the stream if it does not look like the same box.
def stream_headers(model: str = DEFAULT_MODEL) -> Dict[str, str]:
    return {
        "X-User-Agent": f"Model: {model}; Link: Ethernet",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

REDIS_PREFIX = "distalker"

# Host used in the pseudo-URLs written into the generated M3U. It is never
# resolved or fetched -- the stream profile hands the whole string to
# resolver.py. ".invalid" is reserved by RFC 2606 precisely for this.
PSEUDO_HOST = "distalker.invalid"

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


class PortalError(Exception):
    """Raised when the portal rejects us or answers with nonsense."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class PortalConfig:
    """One Stalker portal account. Serialised into Redis for the resolver."""

    slug: str
    name: str
    url: str
    mac: str
    username: str = ""
    password: str = ""
    device_id: str = DEFAULT_DEVICE_ID
    device_id2: str = DEFAULT_DEVICE_ID
    serial_number: str = DEFAULT_SERIAL
    model: str = DEFAULT_MODEL
    timezone: str = DEFAULT_TIMEZONE
    # Accepted for parity with stalkerhek's profile settings. stalkerhek only
    # ever sends it from its STB proxy mode (proxy/proxy.go); none of the
    # handshake / auth / create_link requests this plugin makes include it.
    signature: str = DEFAULT_SIGNATURE
    device_id_auth: bool = False
    max_streams: int = 1
    ffmpeg_args: str = DEFAULT_FFMPEG_ARGS
    # Travels to Redis with the rest, so the resolver waits as long as the sync
    # does rather than giving up on a portal the sync copes with.
    timeout: int = DEFAULT_TIMEOUT

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PortalConfig":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# Any scheme, rather than a list of the ones seen so far. The point of this
# check is to tell a playable link from a local path or a portal answering with
# prose -- not to decide what ffmpeg can open, which ffmpeg knows better than we
# do. A whitelist here would have rejected the multicast an operator's portal
# hands out (udp://, rtp://) purely because nobody had met one yet.
_LINK_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)


def extract_link(raw: str) -> str:
    """Pull the playable URL out of a portal's command string.

    ``create_link`` answers with a little command line rather than a URL:
    usually ``ffmpeg http://host/stream?token=...``, sometimes the URL alone,
    sometimes with the URL quoted, and nothing stops a portal from putting its
    own options after it. Taking the *first* field that looks like a URL copes
    with all of those; taking the last, as this used to, quietly broke on a
    trailing option and rejected a quoted URL outright -- the closing quote is
    part of the token, so it no longer starts with a scheme.

    Returns "" when there is nothing playable, which the caller reports with
    the portal's answer attached.
    """
    for token in (raw or "").split():
        candidate = token.strip("\"'")
        if _LINK_RE.match(candidate):
            return candidate
    return ""


def slugify(value: str) -> str:
    """Reduce a display name to something safe for URLs, keys and filenames."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "portal"


def name_from_url(url: str) -> str:
    """A label for a portal nobody bothered to name.

    The hostname is what providers call themselves and what the user already
    typed, so asking for it twice buys nothing. ``http://myportal.example:2095/c/``
    becomes ``myportal``.
    """
    host = url.split("//", 1)[-1].split("/", 1)[0].split("@")[-1]
    host = host.split(":", 1)[0].strip().strip(".")
    if not host:
        return ""

    labels = [part for part in host.split(".") if part]
    if not labels:
        return ""
    # Drop the public suffix, but never the whole name: 'localhost' has none,
    # and a bare IP would lose its last octet.
    if len(labels) > 1 and not labels[-1].isdigit():
        labels = labels[:-1]
    return ".".join(labels)


# Shapes the expiry has been seen in. The field is free text a reseller typed,
# so anything unrecognised is simply not an expiry -- never an error.
_EXPIRY_FORMATS = (
    "%B %d, %Y, %I:%M %p",   # August 18, 2027, 4:53 pm
    "%B %d, %Y",             # August 18, 2027
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d.%m.%Y",
)


def parse_expiry(value: Any) -> Optional[datetime]:
    """Read a subscription expiry out of a free-text portal field."""
    text = str(value or "").strip()
    if not text or text.startswith("0000-00-00"):
        return None
    for fmt in _EXPIRY_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def normalize_mac(mac: str) -> str:
    """Accept the shapes MAC addresses are quoted in, emit the one we use.

    Providers write them with dashes, dots, in lower case or as twelve bare
    hex digits, and every one of those used to be rejected with a message that
    named the format without saying what was wrong with the input.
    """
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", mac or "")
    if len(cleaned) != 12:
        return (mac or "").strip().upper()
    return ":".join(cleaned[i:i + 2] for i in range(0, 12, 2)).upper()


def split_portal_line(line: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Split one portal line into its raw parts, without applying defaults.

    Returns ``({"name", "url", "mac", "extras", "named"}, errors)``. ``extras``
    holds exactly the ``key=value`` pairs actually written on the line, so a
    caller can tell a value the user set from one it would only be inheriting.
    ``named`` says whether the name was written or read off the URL, which is
    what lets a collision between two derived names be reported as the
    different problem it is.
    """
    errors: List[str] = []
    parts = [p.strip() for p in line.strip().split("|")]

    if len(parts) < 2:
        return None, ["expected at least 'url | mac'"]

    # The name is optional -- it is a label, and the portal's hostname makes a
    # better one than something typed twice. Which field is which is decided by
    # where the MAC sits, not by what the first field looks like: a name may
    # perfectly well contain a dot or a colon.
    if len(parts) >= 3 and not MAC_RE.match(normalize_mac(parts[1])):
        name, url, mac = parts[0], parts[1], parts[2]
        extra_parts = parts[3:]
    else:
        name, url, mac = "", parts[0], parts[1]
        extra_parts = parts[2:]

    named = bool(name)
    if not name:
        name = name_from_url(url)
    if not name:
        errors.append("empty name, and none could be read from the URL")

    mac = normalize_mac(mac)
    # The scheme is optional, but a bare word is far more likely to be a typo
    # than a hostname.
    if not url.lower().startswith(("http://", "https://")):
        host_part = url.split("/", 1)[0]
        if "." not in host_part and ":" not in host_part and host_part != "localhost":
            errors.append(f"'{url}' does not look like a portal URL")
    if not MAC_RE.match(mac):
        errors.append(f"'{mac}' is not a MAC address (AA:BB:CC:DD:EE:FF)")

    # Extras may be spread across further '|' segments and/or separated by
    # spaces within one segment. shlex lets values containing spaces be
    # quoted: password="two words".
    extras: Dict[str, str] = {}
    for segment in extra_parts:
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError as exc:
            errors.append(f"cannot parse '{segment}': {exc}")
            break
        for token in tokens:
            if "=" not in token:
                errors.append(f"'{token}' is not key=value")
                continue
            key, _, value = token.partition("=")
            extras[key.strip().lower()] = value.strip()

    if errors:
        return None, errors
    return {"name": name, "url": url, "mac": mac, "extras": extras,
            "named": named}, []


def parse_portals(text: str) -> Tuple[List[PortalConfig], List[str]]:
    """Parse the multi-line ``portals`` setting.

    One portal per line::

        Name | http://portal.example/c/ | 00:1A:79:XX:XX:XX | key=value ...

    Recognised trailing ``key=value`` pairs: username, password, max_streams,
    plus every key in :data:`STB_KEYS`. Every setting belongs to the portal it
    is written on -- there are no global values to inherit from.

    Returns ``(portals, errors)`` so the caller can report every bad line at
    once instead of dying on the first.
    """
    portals: List[PortalConfig] = []
    errors: List[str] = []
    seen_slugs: Dict[str, int] = {}

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parsed, line_errors = split_portal_line(line)
        if parsed is None:
            errors.extend(f"line {lineno}: {err}" for err in line_errors)
            continue

        name, url, mac, extras = parsed["name"], parsed["url"], parsed["mac"], parsed["extras"]

        slug = slugify(name)
        if slug in seen_slugs:
            # Two subscriptions on one provider is a normal thing to have, and
            # both lines then derive the same name from the same host. The
            # plugin must not invent a suffix to tell them apart: the slug is
            # encoded into every channel's URL, so an identity that shifts when
            # a line is added or deleted would silently rebind existing
            # channels to the wrong portal. Naming one of them is the user's
            # call, and takes a second.
            first = seen_slugs[slug]
            if not parsed["named"]:
                errors.append(
                    f"line {lineno}: takes its name from the same host as line "
                    f"{first} ('{name}'); write a name in front of one of them, "
                    f"e.g. 'Living room | {url} | {mac}'"
                )
            else:
                errors.append(f"line {lineno}: duplicate portal name '{name}'")
            continue
        seen_slugs[slug] = lineno

        try:
            max_streams = int(extras.get("max_streams", 1))
        except ValueError:
            errors.append(f"line {lineno}: max_streams must be a number")
            continue

        def resolve(key: str, fallback: str) -> Tuple[str, bool]:
            """This portal's value, else the built-in default.

            The bool reports whether the line supplied a value of its own.
            """
            value = extras.get(key, "").strip()
            return (value, True) if value else (fallback, False)

        model, _ = resolve("model", DEFAULT_MODEL)
        serial, _ = resolve("serial", DEFAULT_SERIAL)
        signature, _ = resolve("signature", DEFAULT_SIGNATURE)
        timezone, _ = resolve("timezone", DEFAULT_TIMEZONE)
        device_id, has_device_id = resolve("device_id", DEFAULT_DEVICE_ID)
        # Real MAG boxes usually carry the same value in both slots, so a lone
        # device_id fills device_id2 rather than silently reverting to ffff...
        device_id2, _ = resolve(
            "device_id2", device_id if has_device_id else DEFAULT_DEVICE_ID
        )

        username = extras.get("username", "")
        password = extras.get("password", "")
        cfg = PortalConfig(
            slug=slug,
            name=name,
            url=normalize_portal_url(url),
            mac=mac.upper(),
            username=username,
            password=password,
            device_id=device_id,
            device_id2=device_id2,
            serial_number=serial,
            model=model,
            timezone=timezone,
            signature=signature,
            max_streams=max_streams,
            # Absence of credentials selects device-ID auth, exactly as
            # stalkerhek does (`deviceIdAuth := pd.Username == "" &&
            # pd.Password == ""` in webui/profiles.go). The default ffff...
            # IDs are what a MAC-only portal expects, so this fires even when
            # the user supplied no IDs of their own.
            device_id_auth=not (username and password),
        )
        portals.append(cfg)

    return portals, errors


def _quote_if_needed(value: str) -> str:
    """Quote a key=value payload only when it would otherwise not survive parsing."""
    if value and (any(c.isspace() for c in value) or '"' in value or "'" in value):
        return shlex.quote(value)
    return value


def format_portal_line(
    name: str,
    url: str,
    mac: str,
    username: str = "",
    password: str = "",
    max_streams: int = 1,
    stb: Optional[Dict[str, str]] = None,
) -> str:
    """Render one canonical line for the Portals setting.

    Used by the "Add portal" action so users never have to write the line
    format by hand, while the setting itself stays plain text -- readable,
    diffable, and editable in bulk.

    ``stb`` carries this portal's STB identity. Only values actually set are
    written, so lines stay short and it stays obvious which portals differ
    from the MAG254 defaults.
    """
    parts = [name.strip(), url.strip(), mac.strip().upper()]

    extras = []
    if username:
        extras.append(f"username={_quote_if_needed(username)}")
    if password:
        extras.append(f"password={_quote_if_needed(password)}")
    # Only when it says something: a line reading 'max_streams=1' is the value
    # every portal already has, and it was the first thing users asked to stop
    # seeing on lines they never wrote themselves.
    if int(max_streams) != 1:
        extras.append(f"max_streams={int(max_streams)}")

    for key in STB_KEYS:
        value = (stb or {}).get(key, "").strip()
        if value:
            extras.append(f"{key}={_quote_if_needed(value)}")

    parts.append(" ".join(extras))
    return " | ".join(parts)


def normalize_portal_url(url: str) -> str:
    """Point the URL at the portal endpoint itself.

    Users paste all sorts of things: the bare host, the site root, ``/c/``,
    ``/stalker_portal/c/``. The API lives at ``portal.php``; older installs use
    ``load.php``, which is preserved when named explicitly.

    Mirrors stalkerhek's ``normalizePortalURL`` (webui/profiles.go) so a portal
    URL that worked there works here unchanged.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "http://" + url

    parsed = urlparse(url)
    path = parsed.path
    lower = path.lower()

    if lower.endswith(("/portal.php", "/load.php")):
        pass  # explicit endpoint: leave exactly as given
    elif path in ("", "/"):
        path = "/portal.php"
    elif lower.endswith(".php"):
        # Some other .php file: swap in portal.php from the same directory.
        directory = path.rsplit("/", 1)[0]
        path = f"{directory}/portal.php"
    else:
        path = path.rstrip("/") + "/portal.php"

    return parsed._replace(path=path).geturl()


# ---------------------------------------------------------------------------
# Shared state: Redis, mirrored to disk
# ---------------------------------------------------------------------------
#
# Dispatcharr's Redis is a cache, not a database. It runs inside the container
# with no persistence configured, so every restart comes back empty -- and the
# resolver, which reads its portal credentials from there and has no Django to
# ask instead, then fails every tune with "portal '<slug>' is unknown to Redis"
# until somebody presses Sync. Nothing about that is the user's fault and
# nothing tells them what to do.
#
# So everything published here is mirrored into a file, read only when Redis
# has nothing to say, and written straight back into Redis so the next tune
# takes the fast path again. Only the session token stays Redis-only: it is
# short-lived by nature and losing it costs one extra handshake.
#
# The directory matches registry.py's, but the environment variable is read
# again here rather than imported: resolver.py loads this module as a plain
# top-level script, where a package-relative import cannot work.

STATE_DIR = os.path.join(
    os.environ.get("DISTALKER_DATA_DIR", "/data/distalker"), "state"
)


def _state_path(name: str) -> str:
    return os.path.join(STATE_DIR, f"{name}.json")


def _mirror_write(name: str, payload: Any) -> None:
    """Best effort: a mirror that cannot be written must not fail a sync."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
            # Portal credentials. The registry file next door is written the
            # same way, and both are only ever read by this plugin.
            os.chmod(tmp, 0o600)
            os.replace(tmp, _state_path(name))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError, TypeError):
        pass


def _mirror_read(name: str) -> Optional[Any]:
    try:
        with open(_state_path(name), "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def _mirror_forget(name: str) -> None:
    try:
        os.unlink(_state_path(name))
    except OSError:
        pass


def _client_or_none(client=None):
    """A Redis client, or None when there is no reaching it.

    Everything that reads shared state goes through this, so a Redis that is
    down or empty degrades to the mirror instead of killing the tune.
    """
    if client is not None:
        return client
    try:
        return get_redis()
    except Exception:
        return None


def python_executable() -> str:
    """The interpreter to spawn the resolver with.

    ``sys.executable`` was the obvious answer while the sync ran in a Celery
    worker. Since 0.8.2 it runs in a thread of the uWSGI process, where
    ``sys.executable`` is ``.../bin/uwsgi`` -- which does still run the script,
    through uWSGI's own embedded interpreter, but only by accident. Pick a real
    interpreter next to it instead, so the stream profile says the same thing
    whichever process happened to write it.
    """
    candidate = sys.executable or ""
    if os.path.basename(candidate).startswith("python"):
        return candidate

    directory = os.path.dirname(candidate)
    for name in ("python3", "python"):
        sibling = os.path.join(directory, name)
        if directory and os.path.exists(sibling):
            return sibling

    return shutil.which("python3") or shutil.which("python") or "python3"


def get_redis():
    """Connect using the same environment Dispatcharr itself reads.

    resolver.py inherits this environment through ``posix_spawn``, so it needs
    no configuration file of its own.
    """
    import redis

    kwargs: Dict[str, Any] = {
        "host": os.environ.get("REDIS_HOST", "localhost"),
        "port": int(os.environ.get("REDIS_PORT", 6379)),
        "db": int(os.environ.get("REDIS_DB", 0)),
        "decode_responses": True,
        "socket_timeout": 5,
    }
    password = os.environ.get("REDIS_PASSWORD")
    if password:
        kwargs["password"] = password
    return redis.Redis(**kwargs)


def _portal_key(slug: str) -> str:
    return f"{REDIS_PREFIX}:portal:{slug}"


def _token_key(slug: str) -> str:
    return f"{REDIS_PREFIX}:token:{slug}"


def _portal_mirror(slug: str) -> str:
    return f"portal-{slug}"


def save_portal(cfg: PortalConfig, client=None) -> None:
    """Publish a portal for the resolver to find at tune time.

    The mirror is written first: if Redis then refuses, the sync fails and says
    so, but playback still works from the file.
    """
    payload = cfg.to_dict()
    _mirror_write(_portal_mirror(cfg.slug), payload)
    client = client or get_redis()
    client.set(_portal_key(cfg.slug), json.dumps(payload))


def load_portal(slug: str, client=None) -> Optional[PortalConfig]:
    client = _client_or_none(client)

    raw = None
    if client is not None:
        try:
            raw = client.get(_portal_key(slug))
        except Exception:
            raw = None
    if raw:
        try:
            return PortalConfig.from_dict(json.loads(raw))
        except (ValueError, TypeError):
            pass  # Corrupt: the mirror is more trustworthy than half a portal.

    payload = _mirror_read(_portal_mirror(slug))
    if not payload:
        return None

    try:
        cfg = PortalConfig.from_dict(payload)
    except (ValueError, TypeError):
        return None

    # Put it back, so this costs a file read once per restart rather than once
    # per tune.
    if client is not None:
        try:
            client.set(_portal_key(slug), json.dumps(payload))
        except Exception:
            pass
    return cfg


def _sync_lock_key() -> str:
    return f"{REDIS_PREFIX}:sync-lock"


def claim_sync_lock(token: str, ttl: int = 1800, client=None) -> Optional[bool]:
    """Claim the right to run a sync, for the whole installation.

    A module-level flag only guards one process, and the container runs four
    uWSGI workers: the first press returns immediately, so the second is
    distributed to whichever worker is free -- usually a different one, which
    knows nothing of the sync already in flight and starts a second. That means
    two logins and two channel-list downloads on a MAC most providers allow one
    connection for.

    Returns True when claimed, False when someone else holds it, and None when
    Redis cannot say -- which the caller treats as "carry on", since refusing
    to sync because Redis is down would be worse than the duplicate it prevents.

    The TTL exists for the process that is killed mid-sync: a normal finish
    releases the lock, so it only ever matters after a hard stop.
    """
    client = _client_or_none(client)
    if client is None:
        return None
    try:
        return bool(client.set(_sync_lock_key(), token, nx=True, ex=max(60, int(ttl))))
    except Exception:
        return None


def release_sync_lock(token: str, client=None) -> None:
    """Release the lock, but only while it is still ours.

    Checked rather than deleted outright so a sync that overran its TTL cannot
    cancel the one that legitimately replaced it.
    """
    client = _client_or_none(client)
    if client is None:
        return
    try:
        if client.get(_sync_lock_key()) == token:
            client.delete(_sync_lock_key())
    except Exception:
        pass


def sync_lock_age(ttl: int = 1800, client=None) -> Optional[int]:
    """Roughly how many seconds the running sync has been going, or None.

    Read off what is left of the lock's TTL, which costs nothing and is only
    ever used to make a message less blunt than "already running".
    """
    client = _client_or_none(client)
    if client is None:
        return None
    try:
        remaining = int(client.ttl(_sync_lock_key()))
    except Exception:
        return None
    if remaining < 0:
        return None
    return max(0, int(ttl) - remaining)


def published_slugs() -> List[str]:
    """Every portal currently published, read from the mirror alone.

    The mirror is used rather than Redis because it is the durable half: a
    restart empties Redis, and a diff that read it would call every portal new
    and re-fetch the lot. Django-free, so the resolver could use it too.
    """
    try:
        names = os.listdir(STATE_DIR)
    except OSError:
        return []
    return sorted(
        name[len("portal-"):-len(".json")]
        for name in names
        if name.startswith("portal-") and name.endswith(".json")
    )


def forget_portal(slug: str, client=None) -> None:
    _mirror_forget(_portal_mirror(slug))
    client = client or get_redis()
    client.delete(_portal_key(slug), _token_key(slug))


def _fallback_key() -> str:
    return f"{REDIS_PREFIX}:fallback"


def save_fallback(command: str, parameters: str, client=None) -> None:
    """Publish the command that plays sources this plugin did not create.

    Written by the sync action, read by the resolver. It goes through Redis for
    the same reason the portals do: the resolver runs on the hot path of every
    tune and must not import Django to look a stream profile up.
    """
    payload = {"command": command, "parameters": parameters}
    _mirror_write("fallback", payload)
    client = client or get_redis()
    client.set(_fallback_key(), json.dumps(payload))


def _valid_fallback(spec: Any) -> Optional[Dict[str, str]]:
    if not isinstance(spec, dict) or not spec.get("command"):
        return None
    return {
        "command": str(spec.get("command") or ""),
        "parameters": str(spec.get("parameters") or ""),
    }


def load_fallback(client=None) -> Optional[Dict[str, str]]:
    """The published fallback command, or None if sync has not run."""
    client = _client_or_none(client)

    if client is not None:
        try:
            raw = client.get(_fallback_key())
        except Exception:
            raw = None
        if raw:
            try:
                spec = _valid_fallback(json.loads(raw))
            except ValueError:
                spec = None
            if spec:
                return spec

    return _valid_fallback(_mirror_read("fallback"))


# The token is the one piece of state with no mirror: it expires within the
# hour anyway, and losing it costs a single extra handshake. So these three
# degrade to "no cache" rather than raising, which is what lets a tune succeed
# with Redis down altogether.


def get_cached_token(slug: str, client=None) -> Optional[str]:
    client = _client_or_none(client)
    if client is None:
        return None
    try:
        return client.get(_token_key(slug))
    except Exception:
        return None


def set_cached_token(slug: str, token: str, ttl: int, client=None) -> None:
    client = _client_or_none(client)
    if client is None:
        return
    try:
        client.set(_token_key(slug), token, ex=max(60, ttl))
    except Exception:
        pass


def clear_cached_token(slug: str, client=None) -> None:
    client = _client_or_none(client)
    if client is None:
        return
    try:
        client.delete(_token_key(slug))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Portal client
# ---------------------------------------------------------------------------


@dataclass
class ChannelEntry:
    """One channel as advertised by the portal."""

    channel_id: str
    name: str
    cmd: str
    logo: str = ""
    genre_id: str = ""
    number: str = ""


class Portal:
    """A Stalker portal session.

    Usage is always: ``login()`` once, then any number of API calls. The token
    is what authorises everything, so callers that cached one can pass it in
    and skip the handshake.
    """

    def __init__(self, cfg: PortalConfig, token: str = "", timeout: Optional[int] = None):
        self.cfg = cfg
        self.token = token
        # The portal's own setting unless a caller insists, so every request
        # made about a portal honours what the user configured for it.
        self.timeout = timeout or getattr(cfg, "timeout", None) or DEFAULT_TIMEOUT
        self.session = requests.Session()
        # Non-fatal notes from login(), for the caller to surface.
        self.warnings: List[str] = []

    # -- plumbing ---------------------------------------------------------

    @property
    def origin(self) -> str:
        parsed = urlparse(self.cfg.url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _headers(self, with_auth: bool = True) -> Dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "X-User-Agent": f"Model: {self.cfg.model}; Link: Ethernet",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": self.origin + "/",
            "Origin": self.origin,
            "Cookie": (
                f"PHPSESSID=null; sn={quote(self.cfg.serial_number)}; "
                f"mac={quote(self.cfg.mac)}; stb_lang=en; "
                f"timezone={quote(self.cfg.timezone)};"
            ),
        }
        if with_auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    def _get_json(self, query: str, with_auth: bool = True) -> Any:
        url = f"{self.cfg.url}?{query}"
        try:
            resp = self.session.get(
                url, headers=self._headers(with_auth), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise PortalError(f"request to portal failed: {exc}") from exc

        if resp.status_code < 200 or resp.status_code >= 300:
            snippet = (resp.text or "").strip()[:300]
            raise PortalError(
                f"portal returned HTTP {resp.status_code}"
                + (f": {snippet}" if snippet else "")
            )

        try:
            return resp.json()
        except ValueError:
            snippet = (resp.text or "").strip()[:300]
            raise PortalError(f"portal returned non-JSON response: {snippet}")

    # -- authentication ---------------------------------------------------

    def handshake(self) -> str:
        """Reserve a token. The portal may hand back a different one."""
        data = self._get_json(
            f"type=stb&action=handshake&token={self.token}&JsHttpRequest=1-xml",
            with_auth=False,
        )
        js = data.get("js") if isinstance(data, dict) else None
        if isinstance(js, dict) and js.get("token"):
            self.token = str(js["token"])
        if not self.token:
            raise PortalError("handshake did not yield a token")
        return self.token

    def authenticate(self) -> None:
        """Associate credentials with the token (username/password portals)."""
        form = {
            "type": "stb",
            "action": "do_auth",
            "login": self.cfg.username,
            "password": self.cfg.password,
            "device_id": self.cfg.device_id,
            "device_id2": self.cfg.device_id2,
            "JsHttpRequest": "1-xml",
        }
        headers = self._headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            resp = self.session.post(
                self.cfg.url, data=form, headers=headers, timeout=self.timeout
            )
            payload = resp.json()
        except requests.RequestException as exc:
            raise PortalError(f"authentication request failed: {exc}") from exc
        except ValueError:
            raise PortalError("authentication returned a non-JSON response")

        if not payload.get("js"):
            raise PortalError(payload.get("text") or "invalid credentials")

    def authenticate_with_device_ids(self) -> None:
        """Second-step auth for portals keyed on device IDs rather than login."""
        data = self._get_json(
            "type=stb&action=get_profile&JsHttpRequest=1-xml&hd=1"
            f"&sn={self.cfg.serial_number}&stb_type={self.cfg.model}"
            f"&device_id={self.cfg.device_id}&device_id2={self.cfg.device_id2}"
            "&auth_second_step=1"
        )
        js = data.get("js") if isinstance(data, dict) else None
        if not isinstance(js, dict) or not js.get("id"):
            raise PortalError(
                (data or {}).get("text") or "device ID authentication rejected"
            )

    def login(self) -> str:
        """Handshake plus whichever auth flow this portal needs.

        Credentials win when present. Otherwise the device-ID step runs, which
        is what stalkerhek does for MAC-only portals -- but its failure is not
        fatal here: plenty of portals authorise purely on the MAC cookie and
        either lack ``get_profile`` or answer it without a profile id. If the
        session really is unauthorised, the very next call fails with a far
        more useful message than "device ID authentication rejected".
        """
        self.handshake()

        if self.cfg.username and self.cfg.password:
            self.authenticate()
        elif self.cfg.device_id_auth:
            try:
                self.authenticate_with_device_ids()
            except PortalError as exc:
                self.warnings.append(
                    f"device-ID authentication did not succeed ({exc}); "
                    "continuing with MAC-only authorisation"
                )

        return self.token

    def account_snapshot(self) -> Dict[str, Any]:
        """What the portal will say about the subscription itself.

        Two calls, both made once per sync and never at tune time. Neither is
        required for anything to work, so this returns what it managed to read
        and never raises.

        ``get_main_info`` is where resellers put the expiry date: Ministra shows
        the ``phone`` field in the MAG interface, so that is the field they fill
        in with it -- observed on every portal tested, in the form
        "August 18, 2027, 4:53 pm". ``get_profile`` carries ``blocked``, which
        turns "nothing plays and I do not know why" into one line of the report.

        No connection limit is available from either: neither ``max_online`` nor
        an equivalent exists in the responses, and ``playback_limit`` is a
        portal-wide Ministra default (3 on unrelated providers, next to
        ``tv_playback_retry_limit`` = 3), not this account's allowance. Guessing
        upward from it would be the quickest way to have a MAC blocked.
        """
        snapshot: Dict[str, Any] = {"expires": None, "blocked": False}

        try:
            js = self._get_json(
                "type=account_info&action=get_main_info&JsHttpRequest=1-xml"
            ).get("js")
            if isinstance(js, dict):
                snapshot["expires"] = parse_expiry(js.get("phone"))
        except Exception:
            pass

        try:
            js = self._get_json(
                "type=stb&action=get_profile&hd=1&JsHttpRequest=1-xml"
            ).get("js")
            if isinstance(js, dict):
                snapshot["blocked"] = str(js.get("blocked") or "0") not in ("0", "")
        except Exception:
            pass

        return snapshot

    def watchdog(self) -> None:
        """Keep-alive ping. Only needed by portals that drop idle sessions."""
        self._get_json(
            "action=get_events&event_active_id=0&init=0&type=watchdog"
            "&cur_play_type=1&JsHttpRequest=1-xml"
        )

    # -- content ----------------------------------------------------------

    def get_genres(self) -> Dict[str, str]:
        data = self._get_json("action=get_genres&type=itv&JsHttpRequest=1-xml")
        js = data.get("js") if isinstance(data, dict) else None
        if not isinstance(js, list):
            return {}
        return {
            str(item.get("id")): str(item.get("title") or "")
            for item in js
            if isinstance(item, dict) and item.get("id") is not None
        }

    def get_all_channels(self) -> List[ChannelEntry]:
        data = self._get_json("type=itv&action=get_all_channels&JsHttpRequest=1-xml")
        js = data.get("js") if isinstance(data, dict) else None
        # A bare list here means "no channels" -- almost always a wrong MAC or
        # an expired subscription rather than a genuinely empty portal.
        if not isinstance(js, dict):
            raise PortalError(
                "portal returned no channel data (check the MAC address and portal URL)"
            )

        rows = js.get("data")
        if not isinstance(rows, list) or not rows:
            raise PortalError(
                "portal returned an empty channel list (check the MAC address and portal URL)"
            )

        channels: List[ChannelEntry] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cmd = str(row.get("cmd") or "").strip()
            name = str(row.get("name") or "").strip()
            if not cmd or not name:
                continue
            channels.append(
                ChannelEntry(
                    channel_id=str(row.get("id") or ""),
                    name=name,
                    cmd=cmd,
                    logo=str(row.get("logo") or ""),
                    genre_id=str(row.get("tv_genre_id") or ""),
                    number=str(row.get("number") or ""),
                )
            )
        return channels

    def create_link(self, cmd: str) -> str:
        """Ask the portal for a playable URL for ``cmd``.

        The answer is a command line rather than a URL -- typically
        ``"ffmpeg http://host/stream.m3u8?token=..."`` -- so :func:`extract_link`
        finds the playable part. These links are short-lived, which is why this
        is called at tune time rather than sync time.
        """
        data = self._get_json(
            f"action=create_link&type=itv&cmd={quote(cmd, safe='')}&JsHttpRequest=1-xml"
        )
        js = data.get("js") if isinstance(data, dict) else None
        if not isinstance(js, dict):
            raise PortalError("create_link returned no data (session may have expired)")

        raw = str(js.get("cmd") or "").strip()
        if not raw:
            raise PortalError("create_link returned an empty command")

        link = extract_link(raw)
        if not link:
            raise PortalError(f"create_link returned an unusable command: {raw[:200]}")
        return link

    def logo_url(self, logo: str) -> str:
        """Absolute URL for a channel logo, or '' when there isn't one."""
        if not logo:
            return ""
        if logo.startswith(("http://", "https://")):
            return logo
        parsed = urlparse(self.cfg.url)
        base_dir = parsed.path.rsplit("/", 1)[0] or ""
        return f"{parsed.scheme}://{parsed.netloc}{base_dir}/misc/logos/320/{logo}"


# ---------------------------------------------------------------------------
# Pseudo-URL encoding
# ---------------------------------------------------------------------------


def encode_pseudo_url(slug: str, cmd: str) -> str:
    """Encode portal + channel command into the URL stored on the Stream row.

    Kept ``http://``-shaped so it survives Django's ``URLField`` and does not
    trip Dispatcharr's UDP/RTSP scheme sniffing. Nothing ever fetches it.
    """
    import base64

    token = base64.urlsafe_b64encode(cmd.encode("utf-8")).decode("ascii").rstrip("=")
    return f"http://{PSEUDO_HOST}/{slug}/{token}"


def is_pseudo_url(url: str) -> bool:
    """True if this URL is one of ours, rather than another source's.

    A channel may carry several sources -- a portal channel with an Xtream
    fallback, say -- and Dispatcharr resolves the stream profile from the
    *channel*, so every one of them reaches the resolver once the Distalker
    profile is set. Telling them apart is the first thing it has to do.
    """
    try:
        return urlparse(url).hostname == PSEUDO_HOST
    except ValueError:
        return False


def decode_pseudo_url(url: str) -> Tuple[str, str]:
    """Inverse of :func:`encode_pseudo_url`. Raises ValueError if malformed."""
    import base64

    parsed = urlparse(url)
    if parsed.hostname != PSEUDO_HOST:
        raise ValueError(f"not a distalker URL: {url}")

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 2:
        raise ValueError(f"malformed distalker URL: {url}")

    slug, token = parts
    padding = "=" * (-len(token) % 4)
    cmd = base64.urlsafe_b64decode(token + padding).decode("utf-8")
    return slug, cmd
