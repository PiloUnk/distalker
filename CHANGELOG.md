# Changelog

## Unreleased

**Playing**

- Portal channels now fill Dispatcharr's stream statistics — resolution,
  codecs, pixel format, FPS and output bitrate — like any other source. Nothing
  probes the stream: Dispatcharr reads those off ffmpeg's own output, and the
  arguments we shipped were too quiet to say anything. The default gains
  `-loglevel info -stats`, and is replaced on installs still carrying the old
  one, unless you wrote your own. Sources played through the fallback command
  report the same way.

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
