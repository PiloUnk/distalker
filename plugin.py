# Copyright (C) 2026 PiloUnk
# SPDX-License-Identifier: AGPL-3.0-only
# See LICENSE for the full terms and NOTICE for prior-art attribution.
"""Distalker -- Stalker/MAG portal support for Dispatcharr.

Replaces the "one proxy container, one published port per portal" pattern with
something that lives entirely inside Dispatcharr:

  * a **sync** step turns each portal's channel list into a generated M3U file
    backing a native M3U account, so grouping and filtering use Dispatcharr's
    own UI
  * a **resolve** step (``resolver.py``, spawned by a stream profile) asks the
    portal for a fresh link at tune time, because Stalker links expire within
    seconds

No listener, no ports, no second container.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from . import signals, tasks
from .registry import (
    REGISTRY_PATH,
    clear_pending,
    digest,
    is_pending,
    load_registry,
    save_registry,
)
from .stalker_api import (
    DEFAULT_EPG_HOURS,
    DEFAULT_FFMPEG_ARGS,
    DEFAULT_TIMEOUT,
    STB_KEYS,
    PortalConfig,
    PortalError,
    forget_portal,
    format_portal_line,
    is_superseded_ffmpeg_args,
    load_portal,
    parse_portals,
    published_slugs,
    save_portal,
    split_portal_line,
    sync_lock_age,
)

from .sync import (
    announce,
    apply_stream_profile,
    install_stream_profile,
    portal_status,
    publish_fallback,
    sync_all,
    test_portal,
)

# Settings written by versions before 0.3.0, when the STB identity was one
# global block rather than a property of each portal.
LEGACY_STB_SETTINGS = {
    "stb_model": "model",
    "stb_serial": "serial",
    "stb_device_id": "device_id",
    "stb_device_id2": "device_id2",
    "stb_signature": "signature",
    "stb_timezone": "timezone",
}

_MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.json")

with open(_MANIFEST_PATH, "r", encoding="utf-8") as _handle:
    MANIFEST: Dict[str, Any] = json.load(_handle)

# Set once the pre-0.9.4 periodic task has been cleared out of this process.
_schedule_dropped = False


class Plugin:
    """Entry point discovered by Dispatcharr's plugin loader."""

    # Sourced from plugin.json so the manifest stays the single definition of
    # the UI and can never drift from what the code expects.
    name = MANIFEST["name"]
    version = MANIFEST["version"]
    description = MANIFEST["description"]
    author = MANIFEST.get("author", "")
    help_url = MANIFEST.get("help_url", "")
    fields = MANIFEST.get("fields", [])
    actions = MANIFEST.get("actions", [])

    # -- lifecycle --------------------------------------------------------

    def __init__(self) -> None:
        # Dispatcharr instantiates the plugin once per process that loads it,
        # and only while it is enabled -- so this is where the auto-assign
        # receivers belong. They are keyed by dispatch_uid, so a reload
        # replaces them rather than stacking a second set.
        signals.connect()

    def run(self, action: str, params: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        logger = context.get("logger") or logging.getLogger(__name__)
        settings = context.get("settings") or {}

        handler = getattr(self, f"_action_{action}", None)
        if handler is None:
            return {"status": "error", "message": f"Unknown action: {action}"}

        try:
            # Repair the list before anything reads it, so a migration or an
            # action never operates on a half-erased set of portals.
            settings = self._reconcile_registry(settings, logger)
            settings = self._migrate_legacy_globals(settings, logger)
            settings = self._migrate_ffmpeg_args(settings, logger)
            self._drop_legacy_schedule(logger)

            result = handler(params or {}, settings, logger)

            # No handler writes the portal list any more -- the panel is its
            # only author -- but the hook stays for the ones that rewrite other
            # settings, and it is what keeps portal passwords out of the API
            # response when they do.
            settings = result.pop("settings", settings)
            if self._worth_recording(params or {}, result):
                self._record(settings, result.get("message", ""))
            return result
        except PortalError as exc:
            logger.error("distalker: %s failed: %s", action, exc)
            return self._failed(settings, f"{action} failed: {exc}", logger)
        except Exception as exc:
            logger.exception("distalker: %s raised", action)
            return self._failed(
                settings, f"{action} failed: {type(exc).__name__}: {exc}", logger
            )

    def stop(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Called when the plugin is disabled, deleted or reloaded.

        There is nothing long-running to tear down -- no threads, no sockets --
        but the scheduled sync must go, otherwise beat keeps firing at a
        disabled plugin, and the auto-assign receivers must go with it, or a
        disabled plugin would carry on rewriting other people's channels.
        """
        logger = context.get("logger") or logging.getLogger(__name__)
        signals.disconnect()
        try:
            tasks.remove_schedule()
        except Exception:
            logger.exception("distalker: could not remove the scheduled sync")
        logger.info("distalker: stopped")
        return {"status": "ok"}

    # -- helpers ----------------------------------------------------------

    def _migrate_legacy_globals(self, settings: Dict[str, Any], logger) -> Dict[str, Any]:
        """Fold pre-0.3.0 global STB settings onto each portal that lacks them.

        The identity used to be one block applied to every portal. Dropping it
        silently would change how existing portals authenticate, so any value
        that actually differed from the built-in default is written onto every
        portal line that does not already say otherwise, then the old keys are
        cleared so this runs once.
        """
        carried = {}
        for legacy_key, line_key in LEGACY_STB_SETTINGS.items():
            value = (settings.get(legacy_key) or "").strip()
            # The old model field shipped with MAG254 pre-filled, which is also
            # the built-in default -- not a real override, so not worth carrying.
            if value and not (line_key == "model" and value == "MAG254"):
                carried[line_key] = value

        present = [k for k in LEGACY_STB_SETTINGS if k in settings]
        if not present:
            return settings

        updated = dict(settings)
        for legacy_key in present:
            updated.pop(legacy_key, None)

        if carried:
            rewritten, changed = [], 0
            for line in (settings.get("portals") or "").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    rewritten.append(line)
                    continue
                parsed, _ = split_portal_line(line)
                if parsed is None:
                    rewritten.append(line)
                    continue
                extras = parsed["extras"]
                merged = {k: v for k, v in carried.items() if k not in extras}
                if not merged:
                    rewritten.append(line)
                    continue
                stb = {k: extras.get(k, merged.get(k, "")) for k in STB_KEYS}
                rewritten.append(
                    format_portal_line(
                        parsed["name"],
                        parsed["url"],
                        parsed["mac"],
                        extras.get("username", ""),
                        extras.get("password", ""),
                        int(extras.get("max_streams", 1) or 1),
                        stb,
                        # Carried through explicitly. This rewrite keeps only
                        # what it is handed, so anything left out here is
                        # silently deleted from the user's line.
                        extras.get("epg", "").strip().lower()
                        in ("1", "true", "yes", "on"),
                        int(extras.get("epg_hours", DEFAULT_EPG_HOURS)
                            or DEFAULT_EPG_HOURS),
                    )
                )
                changed += 1

            body = "\n".join(entry for entry in rewritten if entry.strip())
            updated["portals"] = (body + "\n") if body else ""
            if changed:
                logger.info(
                    "distalker: moved global STB settings onto %d portal(s); "
                    "each portal now carries its own identity",
                    changed,
                )

        self._save_settings(updated)
        return updated

    def _drop_legacy_schedule(self, logger) -> None:
        """Delete the periodic task versions before 0.9.4 created.

        Beat published it happily and nothing ever consumed it: a plugin's
        Celery task cannot be registered on a stock install, because Dispatcharr
        imports plugins from worker_process_init -- in the pool's children --
        while the consumer that resolves a task name to a strategy is the
        parent. So the only thing the schedule produced was a stack trace in the
        log twice a day, and a settings field promising something that never
        happened.

        Once per process, and never mind if it fails: it is one row that does
        nothing either way.
        """
        global _schedule_dropped
        if _schedule_dropped:
            return
        _schedule_dropped = True
        try:
            tasks.remove_schedule()
        except Exception:
            logger.debug("distalker: could not remove the old scheduled sync", exc_info=True)

    def _migrate_ffmpeg_args(self, settings: Dict[str, Any], logger) -> Dict[str, Any]:
        """Replace a shipped default that stopped Dispatcharr failing over.

        Until 0.9.1 the default carried ffmpeg's own -reconnect options, which
        retry a portal link that has already expired while keeping the process
        alive -- so Dispatcharr saw neither an exit nor an error and never moved
        to the channel's other sources. Changing the manifest default does not
        reach a value already stored, and one is stored on every install, so it
        is replaced here.

        Only if it is untouched. Anything the user wrote themselves is theirs,
        even if it still carries the reconnect options: they may have put them
        back on purpose, and silently editing someone's command line is worse
        than leaving a bad default in place.
        """
        current = settings.get("ffmpeg_args") or ""
        if not is_superseded_ffmpeg_args(current):
            return settings

        updated = dict(settings)
        updated["ffmpeg_args"] = DEFAULT_FFMPEG_ARGS
        logger.info(
            "distalker: replaced the pre-0.9.1 ffmpeg arguments, which prevented "
            "Dispatcharr from failing over to a channel's other sources"
        )
        self._save_settings(updated)
        return updated

    def _settings_with_defaults(self) -> Dict[str, Any]:
        """Stored settings over the manifest's defaults.

        Dispatcharr merges these before an action; a Celery task arrives with
        neither, so it has to do the merge itself.
        """
        merged = {f["id"]: f["default"] for f in self.fields if "default" in f}
        merged.update(self._raw_settings())
        return merged

    @staticmethod
    def _raw_settings() -> Dict[str, Any]:
        """The stored settings, without the manifest defaults merged in.

        The merged copy in ``context`` cannot distinguish "the panel dropped
        this key" from "the value is empty", and that distinction is exactly
        what :meth:`_reconcile_registry` needs.
        """
        from apps.plugins.models import PluginConfig

        cfg = PluginConfig.objects.filter(key=tasks.resolve_plugin_key()).first()
        return dict(cfg.settings or {}) if cfg else {}

    def _write_settings(self, settings: Dict[str, Any]) -> None:
        """Write the settings row. Isolated so tests can stand in for the DB.

        The context carries no plugin key, so the key is resolved the same way
        the scheduled task does -- by name, falling back to the default.
        """
        from apps.plugins.models import PluginConfig

        cfg = PluginConfig.objects.get(key=tasks.resolve_plugin_key())
        cfg.settings = settings
        cfg.save(update_fields=["settings", "updated_at"])

    def _save_settings(self, settings: Dict[str, Any]) -> None:
        """Persist settings the plugin changed itself."""
        self._write_settings(settings)

        # Mirror the list somewhere the settings panel cannot reach, and flag it
        # as a write the open panel has no way of knowing about. Saves that
        # leave the list alone -- recording a status, clearing the form -- skip
        # this entirely: rewriting the same text would renew the marker and go
        # on distrusting a panel that is in fact still in step with the list.
        text = settings.get("portals") or ""
        if digest(text) != digest(load_registry()):
            save_registry(text, pending=True)

    @staticmethod
    def _worth_recording(params: Dict[str, Any], result: Dict[str, Any]) -> bool:
        """Whether this run should overwrite the Last action box.

        A click always deserves an answer -- it is the only one the user gets.
        An action reached through an event is a different matter: ``m3u_refresh``
        fires for every M3U account on the install and ``channel_error`` for
        every channel that fails to tune, ours or not. Recording those would
        bury the result the user is actually waiting to read behind a string of
        "assigned the profile to 0 channel(s)". So an event run stays silent
        unless it failed, or actually changed something.
        """
        if not params.get("event"):
            return True
        if result.get("status") != "ok":
            return True
        return bool(result.get("changed"))

    def _record(self, settings: Dict[str, Any], message: str) -> None:
        """Persist what an action just did, in the panel's Last action box.

        The notification carrying the same text is gone by the next page load,
        and the panel cannot be refreshed from here, so this is the only account
        of an action that outlives the click.
        """
        updated = dict(settings)
        updated["status"] = message or "(no message)"
        self._save_settings(updated)

    def _failed(self, settings: Dict[str, Any], message: str, logger) -> Dict[str, Any]:
        """Report a failure, and leave it in the panel rather than the last success.

        A status box still boasting about the sync before the one that just
        broke is worse than no status box at all.
        """
        try:
            self._record(settings, message)
        except Exception:
            logger.exception("distalker: could not record the failure in the settings")
        return {"status": "error", "message": message}

    def _reconcile_registry(self, settings: Dict[str, Any], logger) -> Dict[str, Any]:
        """Reconcile the durable portal list with the settings panel's copy.

        The panel overwrites all of ``PluginConfig.settings`` with its own
        state before every action, using whatever it fetched when the page was
        loaded. If that predates a portal this plugin added, the key vanishes
        entirely -- so an absent key means "clobbered", and the file wins.

        A key that is present but different is either a hand edit of the
        textarea or the same stale state, this time carrying the list as it
        looked before the plugin's last write. The two are told apart by the
        pending marker: while it is set, the panel has demonstrably not seen
        what the plugin wrote, so the file wins. Once the panel quotes the
        current list back -- which happens as soon as it is reopened -- the
        marker clears and the textarea is authoritative again.
        """
        stored = load_registry()
        raw = self._raw_settings()
        panel_value = raw.get("portals")

        if panel_value is None:
            if stored:
                logger.info(
                    "distalker: the settings panel dropped the portal list; "
                    "restoring it from %s",
                    REGISTRY_PATH,
                )
                settings = dict(settings)
                settings["portals"] = stored
                self._save_settings(settings)
            return settings

        if (panel_value or "").strip() == (stored or "").strip():
            # The panel has caught up, so whatever it sends next can be trusted.
            clear_pending()
            return settings

        if is_pending():
            logger.info(
                "distalker: the settings panel replayed the portal list as it "
                "was before the last change; restoring it from %s. Reopen the "
                "panel to edit the list by hand.",
                REGISTRY_PATH,
            )
            settings = dict(settings)
            settings["portals"] = stored or ""
            self._save_settings(settings)
            return settings

        # Hand-edited in the textarea, or the very first run.
        if not save_registry(panel_value):
            logger.warning(
                "distalker: could not write %s; the portal list is only "
                "stored in the plugin settings and may be lost",
                REGISTRY_PATH,
            )

        return settings

    # -- actions ----------------------------------------------------------

    def _portals(self, settings: Dict[str, Any]) -> List[PortalConfig]:
        """Parse the configured portals, refusing to proceed on a bad line.

        Partially applying a mistyped config is worse than doing nothing: it
        would leave half the accounts pointing at stale files.
        """
        portals, errors = parse_portals(settings.get("portals") or "")
        if errors:
            raise PortalError("invalid portal configuration -- " + "; ".join(errors))
        if not portals:
            raise PortalError("no portals configured")

        ffmpeg_args = (settings.get("ffmpeg_args") or "").strip()
        if ffmpeg_args:
            for portal in portals:
                portal.ffmpeg_args = ffmpeg_args

        try:
            timeout = int(settings.get("portal_timeout") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            raise PortalError("portal timeout must be a whole number of seconds")
        for portal in portals:
            portal.timeout = max(5, timeout)

        return portals

    def _action_test_portals(self, params, settings, logger) -> Dict[str, Any]:
        portals = self._portals(settings)

        results, errors = [], []
        for cfg in portals:
            try:
                results.append(test_portal(cfg))
            except PortalError as exc:
                errors.append(f"{cfg.name}: {exc}")
            except Exception as exc:
                errors.append(f"{cfg.name}: {type(exc).__name__}: {exc}")

        summary = ", ".join(
            f"{r['portal']}: authenticated, {r['groups']} group(s)" for r in results
        )
        if errors:
            return {
                "status": "error" if not results else "ok",
                "message": (summary + " | " if summary else "") + "failed: " + "; ".join(errors),
                "results": results,
                "errors": errors,
            }

        return {
            "status": "ok",
            "message": (summary + ". Next: press Sync.") if summary else "no portals tested",
            "results": results,
        }

    # Fields that decide what a portal will hand back. A change to any of them
    # can change the line-up, so the portal is fetched again; a change to
    # anything else -- ffmpeg arguments, the timeout, max_streams -- cannot, and
    # must not cost a download of everything the user already has.
    LINEUP_KEYS = (
        "url", "mac", "username", "password", "device_id", "device_id2",
        "serial_number", "model", "timezone", "signature",
    )

    # Everything a change to which means the portal has to be asked again.
    # The line-up keys, plus the guide: turning 'epg=1' on changes nothing
    # about the channels, so without this the plan would call the portal
    # unchanged, fetch nothing, and leave the user pressing Sync at a setting
    # that appears to do nothing.
    FETCH_KEYS = LINEUP_KEYS + ("epg", "epg_hours")

    def _plan(self, portals: List[PortalConfig]) -> Dict[str, Any]:
        """Sort the configured portals into what needs the network and what does not.

        Compared against what is actually published rather than against the
        previous contents of the settings box: the published copy is what the
        resolver reads, so agreeing with it is the thing that matters, and it
        survives the panel replaying a stale list.

        A portal synced by a version older than 0.8.4 has no published copy yet
        and is therefore treated as new -- one extra fetch, once.
        """
        published = set(published_slugs())
        plan = {"new": [], "changed": [], "unchanged": [], "removed": []}

        for cfg in portals:
            previous = load_portal(cfg.slug) if cfg.slug in published else None
            if previous is None:
                plan["new"].append(cfg)
            elif any(getattr(cfg, key) != getattr(previous, key) for key in self.FETCH_KEYS):
                plan["changed"].append(cfg)
            else:
                plan["unchanged"].append(cfg)

        plan["removed"] = sorted(published - {cfg.slug for cfg in portals})
        return plan

    def _action_sync_now(self, params, settings, logger) -> Dict[str, Any]:
        """Apply the list, and fetch only what that actually requires.

        Fetching a line-up is one request per portal that a busy provider can
        take minutes to answer, and this runs on the request thread: anything
        slower than the shortest proxy timeout between the browser and
        Dispatcharr comes back as a 504 whatever the plugin does. So the work
        goes to a thread. The parse and the plan stay here, because both are
        cheap and because the user deserves to be told what is about to happen
        -- and a mistyped line must fail at the click, not out of sight.
        """
        portals = self._portals(settings)
        plan = self._plan(portals)

        if not (plan["new"] or plan["changed"] or plan["removed"]):
            # Nothing to fetch, but the cheap half is still worth doing: it is
            # what repairs an emptied Redis, and it costs no network.
            self._republish(settings, logger)
            return {
                "status": "ok",
                "message": (
                    f"nothing to fetch -- {len(portals)} portal(s) already synced and "
                    "unchanged. Press 'Re-fetch all' to download their line-ups again."
                ),
                "changed": True,
            }

        if not tasks.run_sync_in_background():
            return {"status": "ok", "message": self._already_running()}

        return {"status": "ok", "message": "sync started: " + self._describe(plan)}

    def _action_resync_all(self, params, settings, logger) -> Dict[str, Any]:
        """Re-fetch every portal, changed or not."""
        portals = self._portals(settings)

        if not tasks.run_sync_in_background(full=True):
            return {"status": "ok", "message": self._already_running()}

        return {
            "status": "ok",
            "message": (
                f"re-fetching all {len(portals)} portal(s) in the background. Refresh "
                "the Plugins page to read the result in Last action."
            ),
        }

    @staticmethod
    def _already_running() -> str:
        """Turn down a second press without being blunt about it.

        Most portals allow one connection, so two syncs would spend it on each
        other. What is left of the lock's TTL says how long the first has been
        going, which costs a single Redis read and turns "no" into something the
        user can act on -- and since 0.9.x a finished sync raises a
        notification, so there is nothing to go and refresh.
        """
        age = sync_lock_age()
        started = f" (started {max(1, age // 60)} min ago)" if age else ""
        return (
            f"a sync is already running{started}. You will get a notification when "
            "it finishes."
        )

    @staticmethod
    def _describe(plan: Dict[str, Any]) -> str:
        """The plan in the words of someone who has to wait for it."""
        parts = []
        for key, verb in (("new", "adding"), ("changed", "re-fetching")):
            if plan[key]:
                parts.append(f"{verb} {', '.join(cfg.name for cfg in plan[key])}")
        if plan["removed"]:
            parts.append(f"dropping {', '.join(plan['removed'])}")
        if plan["unchanged"]:
            parts.append(f"leaving {len(plan['unchanged'])} untouched")
        return "; ".join(parts) + ". Refresh the Plugins page for the result."

    def run_sync_now(self, full: bool = False) -> Dict[str, Any]:
        """The sync itself, off the request thread. Entry point for the thread.

        Loads its own settings, because a background thread has no request
        context and no panel state to be handed.
        """
        logger = logging.getLogger(__name__)
        settings = self._settings_with_defaults()
        settings = self._reconcile_registry(settings, logger)
        settings = self._migrate_legacy_globals(settings, logger)
        settings = self._migrate_ffmpeg_args(settings, logger)

        try:
            result = self._sync_portals(settings, logger, full=full)
        except Exception as exc:
            logger.exception("distalker: sync failed")
            self._record(settings, f"sync failed: {type(exc).__name__}: {exc}")
            raise

        self._record(settings, result["message"])
        return result

    def _sync_portals(self, settings: Dict[str, Any], logger, full: bool = False) -> Dict[str, Any]:
        portals = self._portals(settings)
        plan = self._plan(portals)

        # The profile must exist before channels reference it, and repairing it
        # here means a moved install directory heals on the next sync.
        install_stream_profile()

        # Republished every sync so the resolver picks up a profile the user
        # renamed, retuned or only just created.
        fallback_warning = publish_fallback(settings.get("fallback_profile") or "")
        if fallback_warning:
            logger.warning("distalker: %s", fallback_warning)

        # Every portal is published, including the ones not being fetched: this
        # is what the resolver reads, and it must describe the list as it stands
        # now rather than as it stood at each portal's last download.
        for cfg in portals:
            save_portal(cfg)

        targets = portals if full else plan["new"] + plan["changed"]
        outcome = sync_all(targets, logger)

        # Portals the user deleted stop resolving. Their M3U account and
        # channels stay: deleting those is a decision, not a side effect.
        for slug in plan["removed"]:
            try:
                forget_portal(slug)
            except Exception:
                logger.warning("distalker: could not unpublish '%s'", slug, exc_info=True)

        message = self._report(portals, plan, outcome, full, logger)
        if fallback_warning:
            message += " | " + fallback_warning

        failed_everything = bool(outcome["errors"]) and not outcome["synced"]

        # Say so where the user actually is. A sync runs in the background and
        # can otherwise only report into a settings box that stays invisible
        # until someone refreshes the Plugins page -- which is no way to learn
        # that the thing you started ten minutes ago has finished.
        if self._worth_announcing(plan, outcome, full):
            announce(
                "Distalker: sync failed" if outcome["errors"] else "Distalker: sync complete",
                self._announcement(message, outcome),
                failed=bool(outcome["errors"]),
            )
        return {
            "status": "error" if failed_everything else "ok",
            "message": message,
            "plan": {k: [getattr(c, "name", c) for c in v] for k, v in plan.items()},
            **outcome,
        }

    @staticmethod
    def _worth_announcing(plan, outcome, full) -> bool:
        """Whether this sync is worth interrupting the user for.

        A toast for every M3U refresh in the house would be unbearable within a
        day, and the same reasoning already governs the status box: say
        something when something happened, and nothing when nothing did.
        """
        return bool(
            outcome["errors"]
            or outcome["synced"]
            or plan["removed"]
            or full
        )

    @staticmethod
    def _announcement(message: str, outcome) -> str:
        """The report, plus the step Dispatcharr will be waiting for.

        A brand-new account stops at "Pending Setup" until its groups are
        chosen -- that is Dispatcharr's flow for any M3U, not something this
        plugin can skip, and nothing on screen connects it to the sync that
        just ran unless we say so.
        """
        if any(result.get("account_created") for result in outcome["synced"]):
            return (
                message
                + " -- a new M3U account is waiting in Pending Setup: choose its "
                "groups in M3U Accounts to finish."
            )
        return message

    @staticmethod
    def _report(portals, plan, outcome, full, logger) -> str:
        """One line per portal, which is what the panel can actually show.

        The status box is the only channel this plugin has to report anything,
        and it is read long after the action that filled it. So it carries the
        state of every portal rather than the story of the last click.
        """
        fetched = {r["slug"]: r for r in outcome["synced"]}
        lines = []

        for cfg in portals:
            try:
                state = portal_status(cfg)
            except Exception:
                logger.exception("distalker: could not read the state of '%s'", cfg.name)
                continue

            just = fetched.get(cfg.slug)
            channels = just["channels"] if just else state["channels"]
            entry = f"{cfg.name}: {channels} channels"
            if just is None and not state["synced"]:
                entry = f"{cfg.name}: not synced yet"
            if state["expires"]:
                entry += f", expires {state['expires']:%d %b %Y}"
            if just and just.get("blocked"):
                entry += " -- THE PORTAL REPORTS THIS ACCOUNT AS BLOCKED"
            # Dispatcharr's own guide toast names the source by its numeric id
            # and nothing else (apps/epg/utils.py, send_epg_update), so this is
            # the only place the portal's name and its guide appear together.
            if just and just.get("epg"):
                entry += f", guide for {just['epg']['channels']} of them"
            if just:
                entry += " (just fetched)"
            lines.append(entry)

        for slug in plan["removed"]:
            lines.append(f"{slug}: removed from the list; its M3U account is untouched")

        summary = " | ".join(lines) if lines else "no portals configured"
        if outcome["errors"]:
            summary += " | failed: " + "; ".join(outcome["errors"])
        elif not fetched and not full:
            summary += " | nothing needed fetching"
        return summary

    def _republish(self, settings: Dict[str, Any], logger) -> int:
        """Put the portals back where the resolver looks for them.

        Sync writes them, but sync is the one thing a user has no reason to run
        again after a restart -- nothing about their setup changed. So this
        runs on the assign path too, which every M3U refresh and every failed
        tune reaches: the first attempt after a restart may still fail, and
        after it the state is whole again without anyone being told to press
        anything.

        Cheap: no portal is contacted, it is a parse and a few small writes.
        Never raises -- an unconfigured or mistyped portal list is the add and
        sync actions' problem to report, not this one's.
        """
        try:
            portals = self._portals(settings)
        except PortalError:
            return 0
        except Exception:
            logger.exception("distalker: could not read the portals to republish")
            return 0

        published = 0
        for cfg in portals:
            try:
                save_portal(cfg)
                published += 1
            except Exception:
                logger.exception("distalker: could not republish portal '%s'", cfg.slug)

        try:
            publish_fallback(settings.get("fallback_profile") or "")
        except Exception:
            logger.exception("distalker: could not republish the fallback profile")

        return published

    def _action_apply_profile(self, params, settings, logger) -> Dict[str, Any]:
        # Also reached as an event handler: m3u_refresh, which fires for every
        # M3U account in the system -- including ones we do not own -- and
        # channel_error, which fires when a tune fails.
        #
        # channel_error is the safety net for a channel the receivers in
        # signals.py could not catch: one created while the plugin was disabled
        # or being upgraded, or through ChannelStream.objects.bulk_create, which
        # emits nothing. Such a channel keeps the installation's default profile
        # and Dispatcharr fetches the pseudo-URL itself, failing on DNS with a
        # message that names neither this plugin nor the real cause. Repairing
        # it here costs a handful of queries on a path that has already failed,
        # and the next attempt plays.
        if params.get("event") and not settings.get("auto_apply_profile", True):
            return {"status": "ok", "message": "auto-assign disabled; skipped"}

        # Before the profile, because a channel with the right profile and no
        # portal in Redis fails just as thoroughly as one without it.
        self._republish(settings, logger)

        updated = apply_stream_profile()
        return {
            "status": "ok",
            "message": (
                f"assigned the Distalker profile to {updated['channels']} channel(s) "
                f"and {updated['streams']} stream(s)"
            ),
            "updated": updated,
            "changed": bool(updated["channels"] or updated["streams"]),
        }
