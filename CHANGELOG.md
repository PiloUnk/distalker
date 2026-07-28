# Changelog

## Unreleased

**After upgrading, press Test portals, then Re-fetch all.** Sync alone will
report every portal as unchanged and fetch nothing: it compares your settings
against what was last published, and none of them changed — only the code did.
Test first, since it writes nothing and is where a portal that now needs
credentials will say so.

**Connecting**

- **The portal now decides which authentication it gets.** Distalker reads the
  `status` its profile request comes back with and does what it asks: nothing
  further when the session is already good, or `do_auth` followed by a second
  profile call when the portal says it wants credentials. It used to pick the
  flow itself from whether a username and password happened to be configured,
  which was wrong in both directions — it ran a device-ID step at portals that
  wanted a password, and had no way to tell a refused account from an empty
  channel list.
- A portal that refuses the account now says so in the provider's own words —
  "subscription expired", "blocked" — instead of failing later as a channel
  list that came back empty and a suggestion to check the MAC address.
- A portal that wants credentials and has none on its line now fails the sync
  with that as the message, rather than appearing to work.
- Expired sessions are recognised from the plain-text `Authorization failed.`
  some portals answer with, and from HTTP 401/403, instead of being reported as
  a portal talking nonsense.
- The box's profile now carries the full identity every other Stalker client
  sends, `signature` included — which had been a documented setting that no
  request ever contained, so setting it configured nothing.
- Portals with no profile endpoint at all keep working on the MAC alone, with a
  warning. That tolerance is deliberate: most portals this plugin meets are not
  Ministra and answer with less than it would.
- The `device_id_auth` line key is gone. Nothing needs to be changed: it was
  never something to write on a portal line, only a value the plugin derived
  for itself, and lines are re-read on every sync.
- The MAC and the session token now travel in the query string as well as in
  the cookie and the `Authorization` header. Portals read one form or the
  other, and sending both costs nothing.
- **A portal reached at the wrong path now finds itself.** Ministra answers on
  both `…/c/portal.php` and `…/server/load.php`, and installs differ in which
  they expose; being handed the one your provider does not serve used to mean a
  404 and no suggestion. The other path is now tried once, and the log says
  which one worked so you can put it on the portal line and stop paying for the
  failed request. Only a 404 or a reply that is not JSON earns the second
  attempt — a portal that is merely down answers the same way on both.

**Syncing**

- **Portals that will not list their channels in one request now sync.** Some
  cap `get_all_channels`, some never implemented it; either way the portal was
  unusable, since the empty answer was reported as a probable wrong MAC. The
  line-up is now collected a page at a time instead when that happens — slower
  by a long way on a big bouquet, and the only way those portals work at all.
  The sync log says when it has fallen back and how far along it is.
- A portal that refuses the session is not paged as a second attempt, and an
  empty listing from both routes still reports the original "check the MAC
  address" message, which remains the likelier explanation.
- Channel logos survive two shapes that used to come out broken: a logo served
  from another scheme than `http(s)` was treated as a filename and glued behind
  the portal's logo path, and an inline `data:` image got the same treatment.
  The first is now left alone, the second dropped — Dispatcharr keeps this in a
  URL field, where a base64 payload does not belong.
- **A sync survives a portal having a bad minute.** Requests made while
  syncing are attempted up to three times, one then two then four seconds
  apart, where a single dropped connection or gateway error used to cost the
  whole line-up until the next scheduled run. Only failures that another
  attempt could fix are repeated: a refused login, a blocked account or a
  missing endpoint still fails immediately.
- Nothing is retried at tune time, deliberately. A source that is not answering
  has to fail fast enough for Dispatcharr to move to the next one, which is the
  same reason `-reconnect` is not in the default ffmpeg arguments. "Test
  portals" does not retry either — it answers a click, and three attempts at
  the portal timeout outlast the browser waiting for it.

## 0.9.2

**Playing**

- Portal channels now fill Dispatcharr's stream statistics — resolution,
  codecs, pixel format, FPS and output bitrate — like any other source. Nothing
  probes the stream: Dispatcharr reads those off ffmpeg's own output, and the
  arguments we shipped were too quiet to say anything. The default gains
  `-loglevel info -stats`, and is replaced on installs still carrying the old
  one, unless you wrote your own. Sources played through the fallback command
  report the same way.

**Developing**

- `./build.sh -dev` packs the working tree into `dist/distalker-dev.zip`, for
  trying a change on a real install before committing it.

**Licence**

- Distalker is now **AGPL-3.0-only**, the licence Dispatcharr itself uses. The
  plugin runs inside Dispatcharr's process and imports its models, so the two
  form one program in practice; they now place the same obligation on whoever
  redistributes them. Running it, modifying it and selling it are all still
  permitted — publishing a modified version, or offering one over a network,
  now owes users the source.
- Prior-art attribution moves to a new `NOTICE` file, and is corrected there:
  stalkerhek is GPL-3.0, not MIT as the 0.9.1 `LICENSE` stated.
- Releases up to and including 0.9.1 remain under MIT. That grant cannot be
  withdrawn and is not being withdrawn.

## 0.9.1

First release. Stalker/MAG portal support for Dispatcharr, with no sidecar
container and no published port per portal.

**Configuring it**

- Portals are one textarea, one line each: `URL | MAC`. The name is optional and
  taken from the host; write one in front only for a different label, or when
  two portals share a host, which the sync then asks you to resolve.
- MAC addresses are accepted with dashes, in lower case, or as bare hex.
- A line commented out with `#` suspends that portal without losing its M3U
  account, streams or channels.
- Anything unusual — credentials, `max_streams`, the STB identity — is a
  `key=value` pair on the line.

**Syncing**

- **Sync** fetches only what changed: portals you added or edited are
  downloaded, deleted ones stop resolving, the rest are left alone. Adding a
  portal no longer re-downloads the ones that already work.
- **Re-fetch all** downloads every line-up again, for when a provider changed
  one rather than you.
- **Test** authenticates and reads the group list, answering in seconds.
- Syncs run in the background: a plugin action runs on the request thread, and
  a busy portal takes longer than the proxy in front of Dispatcharr allows. You
  get a notification when one finishes, and a second press while one is running
  is refused rather than spending the portal's single connection twice.
- The M3U Accounts table shows a portal appearing and filling up as it syncs,
  without a page reload.
- The panel reports one line per portal — channel count, subscription expiry
  read from the portal, and whether it says the account is blocked. The expiry
  also lands on the M3U account, where Dispatcharr already displays it.

**Playing**

- The `Distalker` stream profile is assigned as a channel gains a portal
  stream, again after every M3U refresh, and once more after any channel fails
  to start — which repairs channels created while the plugin was not loaded.
- A channel mixing a portal source with another provider's plays those other
  sources through a configurable fallback profile.
- Playback survives Dispatcharr's Redis coming back empty from a restart, and
  Redis being unreachable altogether: everything the resolver needs is mirrored
  to `/data/distalker/state/` (`0600`, and it holds your credentials).

**Known limitations**

- **Restart Dispatcharr after installing or updating.** Plugins load per
  process, and the container runs several uWSGI workers; a worker that has not
  reloaded has none of this plugin's signal receivers, so a channel created
  through it silently misses the stream profile.
- **There is no scheduled sync.** A plugin's Celery task cannot be consumed on
  a stock install, so there is no honest way to offer one. Press **Sync** when
  you want one, and **Re-fetch all** when a provider's line-up has moved rather
  than your list.
- **`max_streams` defaults to 1 and cannot be detected.** Portals do not tell
  the box what the account is allowed. Raise it only on what your provider told
  you — exceeding the limit is the quickest way to a blocked MAC.
- **The STB identity keys are untested.** Every portal this has run against
  authenticates on the URL and the MAC alone.
- Live TV only: no VOD, no series, no EPG yet.
- Credentials are stored unencrypted, in the Dispatcharr database and on disk.
