"""Running a sync off the request thread, and cleaning up after the one we
used to schedule.

There is no Celery task here, and that is the conclusion of an experiment
rather than an omission. A plugin's ``@shared_task`` cannot be consumed on a
stock install: Dispatcharr imports plugins from ``worker_process_init``, which
fires in the prefork pool's *children*, while the consumer that resolves a task
name to a strategy is the *parent*. It therefore never learns the name and
answers every dispatch with "Received unregistered task ... has been ignored
and discarded" -- twice a day, for a sync that never ran.

So the sync runs in a thread of whichever process was asked for it, and the
periodic task is deleted wherever an earlier version left one.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# One sync at a time per process. Portals commonly allow a single connection
# per MAC, and two overlapping syncs would spend it on each other.
_SYNC_LOCK = threading.Lock()
_sync_running = False

PLUGIN_KEY = "distalker"
PLUGIN_NAME = "Distalker"
# Only still named so the row an earlier version created can be found and
# deleted.
PERIODIC_TASK_NAME = "distalker-portal-sync"


def resolve_plugin_key() -> str:
    """Find the key Dispatcharr actually registered this plugin under.

    Normally ``distalker``, taken from the directory name. But an import of a
    flat ZIP derives the key from the *filename* instead, and the sanitiser
    rewrites hyphens and dots to underscores -- so a hand-made archive can land
    under something like ``distalker_0_1_0``. Looking the plugin up by name
    finds it either way, which is what the auto-assign receivers need to read
    their own settings.
    """
    from apps.plugins.models import PluginConfig

    if PluginConfig.objects.filter(key=PLUGIN_KEY).exists():
        return PLUGIN_KEY

    cfg = PluginConfig.objects.filter(name=PLUGIN_NAME).first()
    if cfg:
        logger.warning(
            "distalker: installed under the key '%s' rather than '%s'", cfg.key, PLUGIN_KEY
        )
        return cfg.key

    return PLUGIN_KEY


def run_sync_in_background(full: bool = False) -> bool:
    """Start a sync off the request thread, without going through Celery.

    ``full`` re-fetches every portal instead of only the ones whose line
    changed, which is the "Re-fetch all" button.

    Queueing would be tidier, but a plugin's ``@shared_task`` cannot be consumed
    on a stock install: the default queue runs a prefork pool, Dispatcharr
    imports plugins from ``worker_process_init`` -- which fires in the *children*
    -- and the consumer that resolves a task name to a strategy is the parent,
    which therefore never learns the name and answers every dispatch with
    "Received unregistered task ... has been ignored and discarded".

    A thread has none of that problem. uWSGI monkey-patches gevent early, so
    this is a greenlet that yields on the portal's I/O rather than a thread
    fighting the hub, and the request returns immediately either way.

    Returns False if a sync is already running, so a second press is a no-op
    rather than a second set of requests to the same portal -- most allow only
    one connection.

    Two guards, because one is not enough. The flag catches a second press
    served by this process; the Redis lock catches the far likelier case of it
    landing on one of the other uWSGI workers, which has its own flag and no
    idea what this one is doing.
    """
    import threading
    from uuid import uuid4

    from .stalker_api import claim_sync_lock, release_sync_lock

    global _sync_running

    with _SYNC_LOCK:
        if _sync_running:
            return False
        _sync_running = True

    token = uuid4().hex
    # None means Redis could not answer; carry on rather than refuse to sync
    # because a cache is down.
    claimed = claim_sync_lock(token)
    if claimed is False:
        with _SYNC_LOCK:
            _sync_running = False
        return False

    def _run():
        global _sync_running
        try:
            from django.db import close_old_connections

            from .plugin import Plugin

            try:
                Plugin().run_sync_now(full=full)
            except Exception:
                logger.exception("distalker: background sync failed")
            finally:
                # This greenlet checked out its own connection; the wrapper
                # around run() covers the request's, not ours.
                close_old_connections()
        finally:
            if claimed:
                release_sync_lock(token)
            with _SYNC_LOCK:
                _sync_running = False

    threading.Thread(target=_run, name="distalker-sync", daemon=True).start()
    return True


def remove_schedule() -> None:
    """Drop the periodic task versions before 0.9.4 created.

    Nothing creates it any more; this clears out the installs that already have
    one, where beat goes on publishing a task no worker can resolve.
    """
    from django_celery_beat.models import PeriodicTask

    PeriodicTask.objects.filter(name=PERIODIC_TASK_NAME).delete()
