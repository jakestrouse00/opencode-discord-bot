"""Single-instance enforcement for the opencode Discord bot.

Discord allows exactly ONE gateway session per bot token. If a second
`python -m opencode_discord_bot` process starts with the same token while
the first is still connected, Discord forcibly invalidates the first
(`INVALID_SESSION` / opcode 9) and the two processes trade the single
gateway back and forth in a reconnect loop. Whichever process happens to
receive a slash-command interaction can lose the gateway mid-dispatch
(before it can send the 3-second interaction ack), so Discord shows the
user "The application did not respond" — even though the command callback
is correct.

This module prevents that by taking an exclusive OS-level file lock on a
well-known path before the bot starts the gateway. The lock is:

  - **Cross-platform:** `msvcrt.locking` on Windows, `fcntl.flock` on
    POSIX. Both are atomic and non-busy (the OS blocks the second caller
    until the holder releases, or returns `EACCES`/`EWOULDBLOCK` if the
    file was opened non-blocking — we use blocking mode and convert the
    failure to a clear error message).
  - **Auto-released on exit:** the OS releases the lock when the process
    exits, however it exits (clean `close()`, `KeyboardInterrupt`,
    `SIGKILL`, crash, power loss). No `finally`-block cleanup is required
    for correctness — `SingletonLock.close()` is best-effort and only
    matters for explicit mid-process release (which the bot doesn't use;
    the lock is held for the whole process lifetime).
  - **Stale-safe:** because the OS owns the lock, a crashed previous
    process does NOT leave a stale lock — the kernel releases it on
    process teardown. This is the key advantage over a PID-file approach,
    which requires stale-PID detection logic (is the PID still alive? is
    it a recycled PID pointing at a different process?).

The lock file path defaults to `.opencode-discord-bot.lock` in the current
working directory (matching `BotConfig.model_config`'s cwd-relative
`.env`). Override via the `OC_SINGLETON_LOCK` env var (absolute path) for
non-standard launch dirs.

Usage (in `__main__.py`, before `bot.start(token)`):

    from opencode_discord_bot.singleton import SingletonLock
    lock = SingletonLock.acquire_or_raise()
    try:
        await bot.start(token)
    finally:
        await bot.close()
        lock.close()  # optional — OS releases on exit anyway

`acquire_or_raise()` opens the lock file (creating it if missing), takes
the exclusive lock, writes the current PID + start time into it (for
diagnostic `cat`-the-lockfile debugging — NOT for stale detection), and
returns a `SingletonLock` handle. If the lock is already held by another
process, it raises `SingletonLockError` with a message naming the lock
path and the holder PID (when readable); `__main__.py` catches that,
prints the message to stderr, and `sys.exit(1)`s — so the second launch
fails fast with a clear reason instead of racing the gateway.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_DEFAULT_LOCK_PATH = Path(".opencode-discord-bot.lock")
_ENV_OVERRIDE = "OC_SINGLETON_LOCK"


class SingletonLockError(RuntimeError):
    """Raised when the bot is already running in another process.

    The message is user-facing (printed to stderr by `__main__.py` before
    `sys.exit(1)`), so it names the lock path and the holder PID when
    readable. NOT a subclass of `OSError` — a real OS-level lock failure
    (permissions, disk full) surfaces as the underlying `OSError` instead,
    so the caller can distinguish "another instance is running" (expected,
    clean exit) from "the lock mechanism itself broke" (unexpected, real
    error).
    """


class SingletonLock:
    """Handle for an acquired single-instance file lock.

    Construct via `SingletonLock.acquire_or_raise()` (the classmethod that
    does the actual locking); do NOT call `__init__` directly. Hold the
    returned handle for the process lifetime; call `close()` to release
    early (optional — the OS releases on process exit regardless).
    """

    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd = fd
        self._closed = False

    @classmethod
    def acquire_or_raise(cls, path: Path | None = None) -> "SingletonLock":
        """Take the exclusive singleton lock, or raise `SingletonLockError`.

        `path` defaults to `$OC_SINGLETON_LOCK` if set, else
        `.opencode-discord-bot.lock` in the cwd (matching `BotConfig`'s
        cwd-relative `.env`). The file is created if it doesn't exist;
        existing contents are overwritten with the current PID + timestamp
        for diagnostic purposes (the OS lock is the real gate, not the
        file contents).

        Raises `SingletonLockError` if another process holds the lock.
        Raises `OSError` (subclass) for genuine filesystem failures
        (can't create the file, permissions, etc.) — those are real errors
        and propagate so the caller doesn't silently run unguarded.
        """
        if path is None:
            env_path = os.environ.get(_ENV_OVERRIDE)
            path = Path(env_path) if env_path else _DEFAULT_LOCK_PATH
        path = Path(path)
        # Open for read+write, create if missing. Binary mode — we write
        # ASCII diagnostics, but on Windows `msvcrt.locking` requires a
        # file opened with a binary mode to avoid newline translation
        # surprises on the locking byte range.
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            cls._lock_fd(fd)
        except _LockHeldError as e:
            # Another process owns the lock. Read the holder PID from the
            # file body for a better error message (best-effort — the file
            # may be empty or garbage if a previous holder crashed mid-write,
            # which is fine; the OS lock is the source of truth).
            os.close(fd)
            holder = _read_holder_pid(path)
            if holder is not None:
                msg = (
                    f"opencode Discord bot is already running (PID {holder}). "
                    f"Only one instance can connect to the Discord gateway at a "
                    f"time — a second would race the first for the single "
                    f"gateway session and cause 'The application did not "
                    f"respond' errors. Lock file: {path}. To override, stop "
                    f"the running instance first."
                )
            else:
                msg = (
                    f"opencode Discord bot is already running in another "
                    f"process. Only one instance can connect to the Discord "
                    f"gateway at a time. Lock file: {path}. To override, stop "
                    f"the running instance first."
                )
            raise SingletonLockError(msg) from e

        # We hold the lock. Write our PID + start time into the file body
        # for diagnostic `cat`-the-lockfile debugging. Truncate first so a
        # shorter new value doesn't leave stale tail bytes from a longer
        # old value. Seek back to 0 after write so a subsequent reader
        # (or our own _read_holder_pid on a re-acquire attempt) sees the
        # new content from the top.
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            payload = f"pid={os.getpid()} started={time.time()}\n".encode()
            os.write(fd, payload)
            os.fsync(fd)
        except OSError:
            # Diagnostic write failed — the OS lock is still held, so this
            # is non-fatal. The bot can run; we just lose the debug info.
            pass
        return cls(path, fd)

    @staticmethod
    def _lock_fd(fd: int) -> None:
        """Take an exclusive lock on `fd` or raise `_LockHeldError`.

        Dispatches on platform: `msvcrt.locking` on Windows,
        `fcntl.flock` on POSIX. Both block until the lock is available
        (no busy-wait); the second caller would block forever here if not
        for the non-blocking flag we pass — on Windows `LK_NBLCK` returns
        `EACCES` immediately, on POSIX `LOCK_EX | LOCK_NB` returns
        `EWOULDBLOCK`. We convert both to `_LockHeldError`.
        """
        if sys.platform == "win32":
            import msvcrt

            try:
                # Lock 1 byte at offset 0, non-blocking. The byte range
                # must overlap a real region of the file; since the file
                # may be freshly created and empty, we write at least one
                # byte before locking — but `os.O_CREAT` + `os.open` gives
                # us an empty file, and `msvcrt.locking` on an empty file
                # at offset 0 for 1 byte returns EINVAL. Workaround: lock
                # byte 0 by seeking to 0 and locking 1 byte AFTER the
                # diagnostic write (which makes the file non-empty). To
                # keep the lock step independent of the write step, pad
                # the file to at least 1 byte here.
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    os.write(fd, b"\x00")
                    os.lseek(fd, 0, os.SEEK_SET)
                except OSError:
                    pass
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as e:
                # EACCES (errno 13) = locked by another process.
                # Anything else = real FS error; let it propagate.
                import errno

                if e.errno in (errno.EACCES, errno.EDEADLOCK):
                    raise _LockHeldError() from e
                raise
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                import errno

                if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise _LockHeldError() from e
                raise

    def close(self) -> None:
        """Release the lock and close the file descriptor.

        Optional — the OS releases the lock on process exit however the
        process exits, so calling `close()` is only needed for an explicit
        mid-process release (which the bot never does; it holds the lock
        for its whole lifetime). Idempotent: a second `close()` is a no-op.
        """
        if self._closed:
            return
        self._closed = True
        try:
            if sys.platform != "win32":
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
            # On Windows, closing the fd releases the `msvcrt.locking`
            # lock automatically — there's no explicit unlock call.
            os.close(self._fd)
        except OSError:
            # Best-effort teardown — a failure here can't be recovered
            # and shouldn't mask a clean shutdown.
            pass

    @property
    def path(self) -> Path:
        return self._path


class _LockHeldError(Exception):
    """Internal: another process holds the OS lock on the lock file."""


def _read_holder_pid(path: Path) -> int | None:
    """Best-effort read of the `pid=<n>` field from the lock file body.

    Returns the PID if the file contains a parseable `pid=` line, else
    None. The file body is diagnostic only (the OS lock is the real
    gate), so a missing/garbage body is normal after a crash and just
    means we can't name the holder in the error message.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("pid="):
            try:
                return int(line[4:].split()[0])
            except (ValueError, IndexError):
                return None
    return None