"""Cross-process purchase lock + duplicate-attempt dedup.

Solves two problems that were previously implicit and broken:

1. **Race on the same truck.** If `good360_monitor.py` (Path A) and
   `good360_daemon.py` (Path B) both decide to buy the same truck within
   a few milliseconds of each other, both can drive a checkout to
   completion and double-charge the card. The existing `LOCK_FILE` in
   the monitor only protects against *its own* re-entry — there's no
   coordination across processes.

2. **Duplicate sequential attempts.** A monitor scan running every 10s
   can pick up the same available truck twice in a row (e.g. when a
   previous attempt was still finalizing). Without a dedup window, that
   produces noise alerts and wastes time on a truck we just bought (or
   just failed to buy).

Both problems are solved with one helper. The lock state lives under
`$WORKDIR/purchase_locks/` — `workdir` is a named docker volume that
both `monitor` and `daemon` containers bind-mount, so `fcntl.flock` is
visible to both processes.

Usage:

    from purchase_lock import exclusive_purchase_lock

    with exclusive_purchase_lock(truck_url) as (ok, reason):
        if not ok:
            return 'SKIPPED', reason          # locked_by_other | dedup
        # ...drive Playwright through checkout...

The kernel auto-releases `flock` on process exit, so a crash mid-purchase
never permanently wedges the truck — the next attempt acquires cleanly.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import time
from contextlib import contextmanager
from typing import Iterator

LOCK_DIR = os.path.join(os.environ.get("WORKDIR", "/app/workdir"), "purchase_locks")

# Default dedup window. Truck listings cycle in seconds, so we want to
# block "just retriggered by overlapping scans" but allow a legitimate
# retry several minutes later if the truck somehow reappears.
DEFAULT_DEDUP_WINDOW_SECONDS = 60


def _lock_path(key: str) -> str:
    """Map an arbitrary key (truck URL or id) to a stable filename. The
    sha1 prefix keeps the path filesystem-safe even when the key contains
    `?`, `/`, or other characters disallowed on some filesystems."""
    os.makedirs(LOCK_DIR, exist_ok=True)
    h = hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:16]
    return os.path.join(LOCK_DIR, f"{h}.lock")


def _last_attempt_path(lock_path: str) -> str:
    return lock_path + ".last_attempt"


@contextmanager
def exclusive_purchase_lock(
    key: str,
    *,
    dedup_within_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
) -> Iterator[tuple[bool, str]]:
    """Acquire an exclusive purchase lock for `key`, or yield a skip signal.

    Yields `(ok, reason)`:
      * `(True,  "acquired")`        — caller may proceed with the purchase
      * `(False, "locked_by_other")` — another process holds the lock now
      * `(False, "dedup")`           — a previous attempt finished within
                                       `dedup_within_seconds`; skip to
                                       avoid duplicate purchases / noise

    On exit (success OR exception), the lock releases and a "last attempt
    finished at" timestamp is written so the next dedup check can see it.
    """
    path = _lock_path(key)
    last_path = _last_attempt_path(path)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        # Step 1: non-blocking exclusive lock. If we can't grab it, another
        # process is currently in the critical section — bail immediately.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False, "locked_by_other"
            return

        # Step 2: dedup. We hold the lock now — check whether the previous
        # holder finished too recently to retry. The .last_attempt file is
        # touched at release; mtime is the precise "finished at" wall clock.
        try:
            last_mtime = os.path.getmtime(last_path)
            age = time.time() - last_mtime
            if age < dedup_within_seconds:
                yield False, f"dedup:{int(age)}s_since_last"
                return
        except OSError:
            # No prior attempt recorded — fresh territory, proceed.
            pass

        # Step 3: write our identity into the lock file for debuggability
        # (so `cat $LOCK_DIR/*.lock` shows who's holding what).
        try:
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            ident = f"pid={os.getpid()} host={os.uname().nodename} key={key[:120]}\n"
            os.write(fd, ident.encode("utf-8", errors="replace"))
        except OSError:
            pass

        yield True, "acquired"
    finally:
        # Touch the last-attempt timestamp BEFORE releasing the lock so a
        # competing process that immediately re-acquires can read the
        # fresh mtime and dedup against us.
        try:
            with open(last_path, "ab") as _f:
                pass  # opening 'ab' suffices to create + bump mtime
            os.utime(last_path, None)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
