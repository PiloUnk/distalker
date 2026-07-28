# Contributing

The [README](README.md) covers using Distalker. This file covers changing it:
how to run it from source, what the code is shaped like, and — mostly — which
Dispatcharr behaviours forced it into that shape. Almost every oddity here is a
workaround for something invisible from our own source, so a reader without that
context would reasonably conclude half of it is superstition.

## Running from source

Bind-mount the repository into Dispatcharr's plugin directory:

```yaml
services:
  dispatcharr:
    volumes:
      - ./distalker:/data/plugins/distalker
```

Then reload without restarting the container:

```bash
curl -X POST http://localhost:9191/api/plugins/plugins/reload/
```

Changes land without a rebuild. It reloads **one process**, though — see
*The plugin is loaded per process* below for why that matters.

## Tests

No test runner, no linter, no package manager. Every test file is a standalone
script with its own `__main__` block:

```bash
python3 tests/test_config.py       # portal-line parsing, STB defaults, pseudo-URLs
python3 tests/test_auth.py         # which authentication a portal asks for
python3 tests/test_transport.py    # what a request carries, and when it is retried
python3 tests/test_listing.py      # reading a portal's channel list
python3 tests/test_registry.py     # surviving the settings panel
python3 tests/test_manifest.py     # plugin.json vs plugin.py, and run()'s plumbing
python3 tests/test_fallback.py     # non-portal sources on a Distalker channel
python3 tests/test_signals.py      # auto-assign signal payload handling
python3 tests/test_migration.py    # upgrade paths
python3 tests/test_state.py        # surviving a wiped or unreachable Redis
python3 tests/test_mock_portal.py  # end-to-end against a fake portal
```

They need `pip install requests redis celery` — all three ship with Dispatcharr,
so the plugin adds no dependencies of its own. Django is **not** among them: the
tests exercise only the Django-free paths, and code that imports models must
degrade rather than raise.

**No test may touch a real Redis.** Every call into the shared state must be
passed a fake client, because the `client or get_redis()` default opens a
connection — which succeeds on a development machine that happens to run Redis
and fails in CI, so the mistake hides locally and only surfaces after a push.
Reproduce CI's environment anywhere with:

```bash
REDIS_PORT=6390 python3 tests/test_state.py
```

CI runs `python3 -m compileall -q .`, then each file in turn, then `./build.sh`.

## Building and releasing

```bash
./build.sh        # dist/distalker-<version>.zip, version read from plugin.json
./build.sh -dev   # dist/distalker-dev.zip, built from the working tree
```

It archives from `HEAD`, so **only committed files ship** — what someone
installs is exactly what the repository says.

`-dev` is for trying a change on a real install before committing it, which some
of them need: nothing here can be exercised without a Dispatcharr to load it.
It packs the working tree through a throwaway index rather than zipping the
directory, so the guarantee that only *tracked* files ship survives — no
`__pycache__`, no local M3U, no `.claude/`. It carries no version, because it is
not a release and a ZIP claiming to be 0.9.1 should be the real 0.9.1. The
archive prefix is still `distalker/`, so Dispatcharr installs it under the usual
key and replaces whatever is there; restart the container afterwards, since
plugins load per process.

The `version` in `plugin.json` is a *release* number, not a build counter: leave
it alone while developing, and raise it when cutting a release. A release is a
tag:

```bash
git tag v0.9.1 && git push origin v0.9.1
```

which the workflow gates on two things — the tag must match `plugin.json`, and
`CHANGELOG.md` must have a section for that version, which becomes the release
body. A changelog nobody is obliged to write is one that stops being written.

Keep changes to `.github/workflows/` in their own commit: pushing one needs a
token carrying the `workflow` scope, which not every contributor's will have,
and splitting it is what makes the rest shippable when it does not.

## Architecture

Two halves that never meet at runtime, and the split is the point:

- **Sync** (`plugin.py`, `sync.py`, `tasks.py`) runs inside Dispatcharr and may
  use the ORM freely. It writes an `.m3u` per portal to `/data/uploads/m3us/`,
  backs it with a file-backed `M3UAccount`, and publishes each portal's
  credentials where the resolver will find them.
- **Resolve** (`resolver.py`) is spawned per tune by the `Distalker` stream
  profile. **It must never import Django** — that would cost a second and a
  database connection on the hot path of every channel start. Anything it needs
  travels through Redis and its disk mirror.
- `stalker_api.py` is the shared protocol layer: portal-line parsing, the
  Stalker HTTP client, the pseudo-URL codec, and every Redis key. Both halves
  import it, and `resolver.py` imports it as a plain top-level module — so it
  may not use package-relative imports.

Channels carry `http://distalker.invalid/<slug>/<base64-cmd>` as their stream
URL. Nothing ever fetches it; it exists to carry identity through Dispatcharr's
database until the resolver decodes it. The slug comes from the portal's name,
which is why two portals deriving the same name from one host is a hard error
rather than an auto-suffixed pair: an identity that shifted when a line was
added or deleted would silently rebind existing channels to the other portal.

### Redis is a cache, not a database

It runs inside the container with no persistence, so it comes back empty from
every restart — which used to kill every channel until someone pressed Sync,
twice observed before it was understood. Everything `save_portal` and
`save_fallback` publish is therefore mirrored to `/data/distalker/state/*.json`
(`0600`: credentials), read only when Redis has nothing, and written back to
Redis on first use. Reads go through `_client_or_none`, so an unreachable Redis
degrades rather than raising. The session token is deliberately *not* mirrored:
it expires within the hour, and losing it costs one handshake.

Writing the mirror only on sync left the same trap one step along — a restart
before the first sync, or a lost data volume, and there is again nothing to
read. `Plugin._republish` runs on the assign path instead, which the button,
every `m3u_refresh` and every `channel_error` all reach.

### The ffmpeg defaults carry no `-reconnect`, and that is load-bearing

ffmpeg's own reconnection retries the URL it was handed, which for Stalker is a
link that expired seconds after being issued — and while it retries, the process
stays alive and silent, so Dispatcharr sees neither an exit nor an error and
never reaches its retry or its failover to the channel's other sources.
Dispatcharr respawns the command per connection attempt, which reruns the
resolver and gets a *fresh* link, so the reconnection belongs to it.
`-rw_timeout` bounds a stalled read at 10s, chosen so three attempts fit inside
the ~50s a client waits.

`SUPERSEDED_FFMPEG_ARGS` and `Plugin._migrate_ffmpeg_args` replace that old
default on installs still carrying it, and only if it is untouched.
`plugin.json`'s copy of the string had already drifted from the code's and lost
the MAG headers; `test_manifest.py` now pins them equal.

`stalker_api.python_executable()` picks the interpreter the stream profile
spawns. `sys.executable` was right while the sync ran in a Celery worker; it now
runs in a uWSGI thread, where it is `…/bin/uwsgi`. uWSGI does run the script
through its embedded interpreter, so this was silent, but nothing should depend
on it.

### They carry `-loglevel info -stats`, and that is load-bearing too

Dispatcharr never probes a stream. Its statistics panel — resolution, codecs,
pixel format, FPS, output bitrate — is parsed out of the **stderr of whatever
the stream profile spawned**: `apps/proxy/live_proxy/input/manager.py` reads the
pipe and `services/log_parsers.py` matches three shapes. `Input #0, mpegts` and
`Stream #0:0 … Video:` come from ffmpeg's input dump, which is emitted at info
level, and the output bitrate comes from the periodic `frame= … bitrate=` line,
which ffmpeg prints below info only when `-stats` was given explicitly. Quieten
either and the channel plays with an empty panel and nothing to say why — which
is what 0.9.1 shipped, hence a third entry in `SUPERSEDED_FFMPEG_ARGS`.

The resolver `exec`s ffmpeg, so that stderr is already the pipe Dispatcharr
reads: nothing has to be forwarded, and `BUILTIN_FALLBACK_ARGS` carries the same
two flags so a non-portal source on our channel is not the odd one out. Our own
`[distalker] …` lines share the pipe and are parsed too; none of them match, and
they are all written before the `exec`, so they cannot end the input phase
(`ffmpeg_input_phase`) that gates the video and audio lines.

The profile's command is the Python interpreter, not `ffmpeg`, so Dispatcharr's
command→parser map misses and it falls back to `LogParserFactory.auto_parse`,
which tries the ffmpeg parser first. Correct by accident, but stable: do not be
tempted to disguise the command to hit the map.

### The plugin is loaded per process, and there are several

`PluginManager` is a singleton **per process**, and the stock image runs four
uWSGI workers (`docker/uwsgi.ini`: `workers = 4`, `lazy-apps = true`, gevent)
plus a Celery parent and its prefork children. Each loads plugins independently:

| Process | Where discovery happens |
| --- | --- |
| every uWSGI worker | `apps/plugins/apps.py` `ready()`, at boot |
| Celery prefork children | `dispatcharr/celery.py`, `worker_process_init` |
| Celery parent | never — hence the unconsumable `@shared_task` |
| `manage.py shell` | never: `shell` is in the skip list, so call `discover_plugins()` yourself |

Loading instantiates `Plugin()`, which is why `__init__` is where
`signals.connect()` belongs — and why a worker that has *not* loaded the plugin
has no receivers at all. A channel created through that worker gets no stream
profile. This is not hypothetical: it is how two channels were lost during an
afternoon of reinstalls.

Reloads propagate through `/data/plugins/.reload_token`, a file whose **mtime**
is the version counter. An import force-reloads locally and touches it; every
other process reloads when it next calls `discover_plugins(use_cache=True)` —
which only two paths do: serving the Plugins API, and dispatching a system event
(`apps/connect/utils.py`). A worker doing neither keeps the old code for as long
as it lives, so a restart remains the only way to converge all of them at once.
`DISPATCH_UID` means a reload *replaces* the receivers rather than stacking a
second set.

### A plugin's `@shared_task` cannot be consumed

The default queue runs a prefork pool; Dispatcharr imports plugins from
`worker_process_init`, which fires in the pool's *children*; the consumer that
resolves a task name to a strategy is the *parent*, which therefore never learns
it and logs "Received unregistered task". Restarting does not help.

So `tasks.run_sync_in_background()` uses a thread instead — a greenlet, given
uWSGI's early gevent patch — guarded by a module flag *and* a Redis lock, since
the flag only covers one of four workers. `remove_schedule()` survives to delete
the periodic task earlier versions created.

### The settings panel fights you

`PluginCard.jsx` overwrites all of `PluginConfig.settings` with its own React
state before running any action, and **never re-reads the result** — neither
`runPluginAction` nor `updatePluginSettings` refetches the plugin list.
Consequences that shape the code:

- Anything the plugin writes into its settings is invisible until the user
  presses the refresh icon on the Plugins page. Closing and reopening the modal
  is *not* enough.
- **`fields` is cached when the plugin loads**, so the settings schema is
  static: a `select` can never list the registered portals, and no action can
  fill a box the user is looking at.
- Hence one textarea and no form: there is no Add/Load/Remove filling boxes
  nobody can see, and the panel is the list's only author. What is left of the
  reconciliation is restoring `/data/distalker/portals.txt` when the panel sends
  the setting back empty.
- `status` is the only thing the plugin still writes there, so it holds the
  state of every portal rather than the story of the last click
  (`Plugin._report`). Anything more urgent goes to Dispatcharr's notification
  centre, which does reach an open browser (`sync.announce`).

### Actions

`plugin.json` is the single definition of the UI; `plugin.py` reads it at import
and exposes it as `Plugin.fields` / `Plugin.actions`. Each action `id` maps to a
`_action_<id>` method — `tests/test_manifest.py` pins that both ways round.

**Long work does not belong in an action.** A plugin action runs on the uWSGI
request thread, so anything slower than the shortest proxy timeout in front of
Dispatcharr returns a 504 — observed at ~60s on a setup whose own nginx allows
300. `_action_sync_now` parses the config (cheap, so typos still fail at the
click), plans the work, and hands it to a thread.

**Sync fetches only what changed.** `Plugin._plan()` compares each parsed portal
against the published copy (`load_portal`, i.e. Redis then the mirror) and sorts
it into new / changed / unchanged / removed. "Changed" means one of
`LINEUP_KEYS` moved — URL, MAC, credentials, STB identity — because nothing else
can alter what the portal hands back. The comparison is against the *published*
copy rather than the previous settings value on purpose: that is what the
resolver reads, and it cannot be replayed stale by the panel.

Event names in an action's `events` list must exist in
`core.models.SystemEvent.EVENT_TYPES`; nothing validates them, so
`test_manifest.py` does. Event handlers reach `run()` with
`params = {"event", "payload"}`, which is how `_worth_recording` keeps
`m3u_refresh` — one per account on the install — and `channel_error` from
overwriting the status box with a run the user never asked for.

### Stream profiles: two lookups, both of which must be set

`Channel.get_stream_profile()` ignores `stream.stream_profile` (the
`# @TODO: honor stream's stream profile` in `apps/channels/models.py`), but the
**direct stream** path in `apps/proxy/live_proxy/url_utils.py` — the preview
button, or a by-stream URL — calls `Stream.get_stream_profile()`, which honours
it. `apply_stream_profile()` therefore sets both; setting only the channel
leaves previews falling back to the global default, and a `Proxy` default makes
Dispatcharr fetch the pseudo-URL itself and fail on DNS.

Transcoding is *not* our concern: `OutputProfile` is a separate stage taking
MPEG-TS on `pipe:0` and writing `pipe:1`, one process per (channel, profile).
So `-c copy` is correct here, and someone wanting 720p composes an OutputProfile
with us.

A channel carrying our profile sends **every** one of its sources to
`resolver.py`, including other providers'. Hence `resolver.py` branches on
`is_pseudo_url()` and hands anything else to `passthrough()`, which execs the
published fallback profile. Every failure mode there — Redis down, corrupt JSON,
unparseable parameters, a profile pointing back at the resolver — must end in
the built-in ffmpeg command, never an error. `Proxy` and `Redirect` cannot be
fallbacks: Dispatcharr implements them internally, so there is no process to
stand in for.

### Assigning the profile

`signals.py` assigns it as a channel gains a portal stream. Three routes attach
a stream and they do not agree on signals: `ChannelStream.objects.create()`
fires `post_save` only (the Channels form, and `from-stream/`),
`channel.streams.add()` fires `m2m_changed` only, and
`ChannelStream.objects.bulk_create()` fires neither (`from-stream/bulk/`, in a
Celery task). Hence two receivers and a button that stays. The receivers run
inside the transaction saving the user's channel, so `assign()` swallows every
exception.

No receiver can cover a channel created while the plugin is **not loaded** —
during an upgrade, or while it is disabled — and the symptom is a DNS error on
`distalker.invalid` that names nothing. So `apply_profile` also subscribes to
`channel_error`: a failed start re-runs it, and the next attempt plays.

## Diagnosing a live install

The tests cover the Django-free paths; everything else has to be asked of a
running Dispatcharr. Which probe to reach for comes from one line of the log:

- `HTTP reader connecting to http://distalker.invalid/…` → the channel is
  **not** on the Distalker profile. Dispatcharr is fetching the pseudo-URL
  itself, so the resolver never ran. Look at the assignment.
- `Server closed connection`, `Error reading stderr … Bad file descriptor` →
  the resolver **did** run and exited. Its reason went to a stderr Dispatcharr
  lost with the pipe; run it by hand.

**Is the plugin loaded, and are its receivers attached?** `shell` skips
discovery, so ask for it, then read the receivers off the signal:

```python
import sys
from apps.plugins.loader import PluginManager
PluginManager.get().discover_plugins(sync_db=False)
sig = sys.modules['_dispatcharr_plugin_distalker.signals']   # the loader's package name
from django.db.models.signals import post_save
from apps.channels.models import ChannelStream
r = post_save._live_receivers(ChannelStream)
print([getattr(f, '__module__', f) for f in (list(r[0]) + list(r[1] or []) if isinstance(r, tuple) else r)])
```

This proves it for *the shell's* process, which has just loaded the current
version — never for the uWSGI worker that served the request you are
investigating.

**Why did a channel not get the profile?** `sig.assign([channel_id],
stream_ids)` replays it; `sig._profile_wanted(ids)` and
`sig._auto_assign_enabled()` are the two guards. Compare
`channel.stream_profile` with `channel.get_stream_profile()` — they differ when
a `ChannelOverride` row carries one, since `effective_stream_profile_obj`
resolves the override first.

**Why did the resolver die?** Run it exactly as the stream profile does, keeping
stderr:

```bash
docker exec -i dispatcharr /dispatcharrpy/bin/python \
  /data/plugins/distalker/resolver.py 'http://distalker.invalid/<slug>/<b64>' 'UA' > /dev/null
```

It costs one portal connection. `--probe` instead of playing shows what the
provider answered. `ls -l /data/distalker/state` says whether it had anything to
read at all.

## Conventions

- Comments explain **why**, not what. Prefer one paragraph of reasoning over a
  line-by-line narration, and record the constraint that forced a decision.
- Commit messages follow the same rule: what was wrong, why the fix takes this
  shape, what it costs. They are long by design.
- Every source file carries the three-line `SPDX-License-Identifier` header.
  Anything contributed here is contributed under **AGPL-3.0-only**, the same
  licence as the rest: with no CLA, nothing can be relicensed later without
  every contributor agreeing, so the licence has to be right on the way in.
- `logo.svg` is the source for `logo.png`, and `banner.svg` for the README's
  `banner.png`; both document their regeneration command in a header comment,
  and both need `cairosvg` (ImageMagick's built-in SVG renderer mangles arcs and
  strokes). The icon renders at 48px and 28px, which rules out gradients, text
  and thin strokes.
