# Changelog

## 0.9.4

**After upgrading, restart Dispatcharr, then press Test portals and Re-fetch
all.** The restart is what puts every worker on the new code, since plugins are
loaded once per process. Sync on its own would report every portal as unchanged
and fetch nothing — it compares your settings against what was last published,
and nothing in them changed, only the code did.

- **Stalker compatibility improvements.**
- **Security improvements in the Settings panel, which now masks credentials.**

<!-- details -->

**Talking to more portals**

- **A refusal is recognised however the portal words it.** Ministra answers a
  rejected session with HTTP 200 and a bare line of text, so only one exact
  phrase was ever understood; the others arrived as "portal returned non-JSON
  response" and cost a channel its failover. `Access denied.`, `Unauthorized
  request.`, the stock server's numeric debug counter and the refusals
  non-Ministra panels put inside the JSON envelope are all read now, and each
  says which of the three things is actually wrong: the session, the account,
  or the MAC.
- **A portal is looked for on every path one is served from.** Two were probed;
  five are now — the URL as written first, then `/server/load.php`,
  `/c/portal.php`, `/portal.php` and `/stalker_portal/server/load.php`. An
  explicit `.php` in your own URL is no longer swapped for a guess, since that
  path is the address the provider handed out.
- **The box describes itself the way a real one does.** The handshake and
  profile now carry `prehash`, `client_type`, `video_out` and the metrics blob
  a MAG sends, which is what some panels authenticate on and what the admin
  panel reads to show a box as connected.
- **A command reaches the portal decoded exactly once.** A `%` inside a channel
  command was being encoded twice, so those channels asked for a link that
  never existed.
- **Channels the portal marks as needing no temporary link now play without one**
  — the portal's own player skips `create_link` for them, and so does this. It
  removes the one request known to go wrong on those providers.
- **The portal's own streams are fetched as the box that authenticated**, with
  the session and MAC that minted the link. Never to anyone else's CDN: a
  stream on another host gets no credentials, which is what keeps a
  subscriber's MAC out of a request that has no business carrying it.
- Three things the portal was already saying are now read: the expiry date
  where Ministra puts it, a device-conflict message with the setting that fixes
  it named, and a channel listed twice in one response counted once.

**Credentials are no longer displayed**

- **The Portals box hides them once saved.** The MAC, the password and the rest
  of the account identity come back as `••••`: the real line lives in
  `/data/distalker/portals.txt`, which the settings panel cannot read. Names,
  URLs and the tuning keys stay, so the box still reads as your own
  configuration — and a portal is still deleted by deleting its line.
- This matters beyond the screen. Dispatcharr serves a plugin's settings row to
  every account on the install, so the credentials were in an API response
  anyone could read.
- **Editing works through the bullets.** Renaming a portal and repointing its
  URL both keep the MAC you cannot see; pasting a line back in full still
  works. Change a line's name *and* its URL in one edit and there is nothing
  left to recognise it by — the plugin then quotes that line back and asks you
  to retype it, rather than parsing a MAC address made of bullets.
- **Keep your own copy of each portal line in a password manager.** The box will
  not give a credential back, and if `/data/distalker` is ever lost the last
  copy goes with it. Nothing is redacted until that file is known to hold the
  same list, so a registry that could not be written leaves the credentials
  where they were.
- Hiding is not encryption. The resolver reads the MAC on every tune in a
  process with no database, so `portals.txt` and the state mirrors go on
  holding it in the clear at `0600` — the README says which files those are.
- **Settings the panel no longer has a field for are dropped.** Removing a field
  from the manifest stops the panel rendering it and nothing else, so the
  Add-portal form retired in 0.4.0 left a MAC, a password and a portal URL in
  that same API response — for a portal you may have deleted months ago. They
  are cleared on the first action after upgrading.

## 0.9.3

**After upgrading, restart Dispatcharr, then press Test portals and Re-fetch
all.** The restart is what puts every worker on the new code, since plugins are
loaded once per process. Sync on its own would then report every portal as
unchanged and fetch nothing — it compares your settings against what was last
published, and none of them changed, only the code did. Test writes nothing and
is where a portal that now wants credentials will say so.

- **Portals can now serve their programme guide.** `epg=1` on a portal line
  writes an XMLTV file and registers it under **EPGs**; off unless asked for,
  because a guide is by far the largest thing a sync downloads.
- **Distalker can be put on a schedule.** *Refresh every (hours)* re-fetches
  each portal on that interval — channels, and the guide where `epg=1` is set —
  without anyone pressing Sync. Left at `0`, everything stays manual.
- **Channels on portals that answer with a resolved link now play**, at sync
  time and at tune time both, and stop duplicating themselves on every sync.
- **The portal now decides which authentication it gets**, so an expired or
  blocked account says so in the provider's own words instead of arriving as an
  empty channel list.
- **Portals that will not list their channels in one request now sync**, a page
  at a time, and a sync survives a portal having a bad minute.
- **The channel is named in the request as well as in the command**, for the
  providers that could not find it in the command alone. Reported and first
  fixed by [@shayward](https://github.com/shayward).
- Channel logos and archive flags are read as the portal sends them.

<!-- details -->

**Refreshing by itself**

- **Distalker can now be put on a schedule.** Set *Refresh every (hours)* and
  each portal is re-fetched on that interval — channels, and the guide where
  `epg=1` is set — without anyone pressing Sync. Left at `0`, nothing changes:
  everything stays manual, as before.
- The clock is Dispatcharr's, not ours. A plugin cannot register a Celery task
  a stock install can consume, so the setting drives the refresh interval of
  the M3U accounts this plugin owns, and the event Dispatcharr emits at the end
  of each refresh is what calls back to re-fetch the portal.
- Refreshing an M3U account that is not one of ours does nothing, and a
  scheduled run cannot follow another within thirty minutes — the second is the
  echo of the first, and answering it would loop. In practice it does more than
  that: twelve accounts sharing one interval all fire at the same second, and
  eleven of those are turned away.
- A sync started by hand no longer comes back as a scheduled one. Every synced
  portal asks Dispatcharr to re-read its playlist, and that re-read emits the
  event the schedule listens for — so adding a single portal could set off a
  re-fetch of every other one.
- A scheduled sync runs inside the task that woke it rather than in a thread of
  its own. The thread was right for the button, which answers a browser and
  cannot block; it was wrong here, where the worker hosting it is reaped when
  the pool scales down — taking the sync with it, silently.
- **A scheduled sync now appears in the log.** It always ran; it just wrote
  everything it did to a logger that prints from the process serving the
  panel and not from the one running the schedule. Channel counts, guide
  sizes and failures alike went nowhere, which made a refresh that worked
  indistinguishable from one that never fired.
- The portal whose own refresh woke the sync is asked a second time, a minute
  later, to re-read its playlist. Its first request is refused: the sync runs
  inside that account's refresh task, which holds the account's lock until we
  are done. Left as it was, that one portal would be downloaded on every cycle
  and read on the next — a day late on a daily schedule, and a different
  portal each time, since they race to be the one that wakes us.

**Guide**

- **Distalker can now fetch a portal's programme guide.** Add `epg=1` to a
  portal line and the next sync writes an XMLTV file and registers it under
  **EPGs** as `Distalker: <portal>` — the same arrangement as the M3U account
  it already creates for the channels. Matching is automatic: the `tvg-id` the
  playlist has always carried is what Dispatcharr joins on.
- Off unless asked for, because it is by a wide margin the largest thing a sync
  downloads — a portal answers for its whole line-up at once, so a
  13,000-channel provider means roughly 100 MB for a single day. `epg_hours=48`
  raises the default 24; a guide past 200 MB is abandoned with a message saying
  to lower it.
- A guide that fails never fails the sync around it. You keep the channel list.
- Channels the portal has no programmes for are left out rather than written as
  empty entries, and removing `epg=1` deactivates the EPG source instead of
  deleting it.
- **Only whole-grid guides are read.** A portal that serves programmes one
  channel at a time is reported as having no guide — which, measured across
  twelve portals, is close to the truth: that route covered none of the
  channels anyone had configured, at the price of one request per channel.
- A portal with no guide says so plainly and suggests removing `epg=1`, rather
  than reporting the same message as a portal that answered with something
  unrecognisable — which is now reported as exactly that, and asks to be.

**Playing**

- **Channels a portal answers about with a resolved link now play.** Portals
  are meant to name a channel with a marker and turn that into a link when
  asked; some answer with the link itself, and then cannot read it back — one
  spliced part of the URL into its own stream parameter, another returned that
  parameter empty. Either way the channel was unplayable. Such a command is now
  rebuilt into the marker the portal expects.
- It also stops those channels duplicating. The links carry a token that
  changes on every request, and Dispatcharr identifies a stream partly by its
  URL — so every sync invented a new stream and stranded the previous one. One
  portal was producing 647 duplicates an hour.
- A command that already looks like a marker is left untouched, which is every
  channel on every other portal tested — their identities do not move, and no
  duplicate is created by the fix itself. The sync log says how many were
  rewritten.
- **The same portals are also caught at tune time.** The rewrite above needs
  the portal to number its channels; a listing that does not still hands back a
  resolved link, as does every portal synced before this release. One family of
  them answers by gluing its own base in front of whatever it was given, so a
  command that was already a URL comes back with `/user/pass/` twice and gets
  401 — while the command itself plays. That reply is now recognised by its
  shape and the command played instead, with a line on the channel's log
  saying so. Nothing is asked of the provider to decide it: a check request
  would take a connection slot from the tune that is about to need it.
- **The channel is now named in the request as well as in the command.** Some
  providers cannot find it in the command they are handed back and answer about
  no channel at all — the link arrives with its stream number left empty, and
  plays nothing. Both shapes a command takes here carry that number, and it is
  sent alongside, so those portals resolve the channel that was asked for.
  Reported and first fixed by [@shayward](https://github.com/shayward).
- Sent only when there is one to send, so a portal that never needed it sees
  the request it has always seen, and the number can only ever be one the
  portal itself wrote.

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
