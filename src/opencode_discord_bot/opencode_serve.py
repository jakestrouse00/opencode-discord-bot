"""Lifecycle manager for an `opencode serve` subprocess spawned alongside the
Discord control bot.

The bot talks to an `opencode serve` HTTP server (default
`http://127.0.0.1:4096`, see `config.opencode_server_url`). Previously the
user had to start that server separately. This module lets the bot spawn it
as a child process on login (`OpencodeBot.on_connect`) and tear it down when
the bot closes (`OpencodeBot.close`).

Design:
- Binary discovery: `shutil.which("opencode")` first (choco/scoop/binary
  installs), then `npx -y -p opencode-ai opencode` (npm global fallback). The
  latter is the documented Windows install path when opencode isn't on PATH.
- Spawn: `subprocess.Popen` with `serve` + the configured flags. On Windows
  `CREATE_NEW_PROCESS_GROUP` so a single CTRL_BREAK_EVENT reaches the whole
  tree; on POSIX `start_new_session=True` so `os.killpg` reaches children.
- Readiness: poll `GET /global/health` until 200 or `startup_timeout` elapses.
  The server takes a second or two to bind; without this gate the bot would
  race the server on startup.
- Teardown: `stop()` is idempotent and safe to call twice (atexit + finally).
  Windows: `taskkill /PID <pid> /T /F` (tree-kill, since terminate() only
  kills the launcher). POSIX: `SIGTERM` the group, escalate to `SIGKILL`
  after a grace period.

This module imports only stdlib + `bot.config` so importing it has no heavy
deps (no PySide6, no pydantic-ai) — mirrors the bot's lightweight-import
convention. It is a standalone copy of `core/opencode_serve.py`, decoupled
from `core.settings` so the `bot/` package is self-contained.
"""

from __future__ import annotations

import base64
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence

from opencode_discord_bot.config import config

# The project directory: the user's current working directory at launch time.
# Used as the default cwd for the spawned `opencode serve` subprocess so the
# server's `process.cwd()` resolves to the user's project (where their
# `.opencode/` lives) — see the `defaultDirectory` resolver in opencode's
# `workspace-routing.ts`, which falls back to `process.cwd()` when neither
# `?directory=` nor the `x-opencode-directory` header is present on a request.
# Without this, a bot launched from an arbitrary directory would pin every
# session's project dir to that launch dir, and `.opencode/plans/` would
# resolve to `<launch-dir>/.opencode/plans/` (which may not exist) — the
# silent plan-loss bug. `Path.cwd()` is correct here (not
# `Path(__file__).resolve().parent.parent`, which would resolve to the
# package install dir in the standalone context) because the bot should
# spawn `opencode serve` in the user's current project directory.
_REPO_ROOT = Path.cwd()


def _resolve_opencode_argv() -> list[str] | None:
    """Return the argv list to launch `opencode`, or None if unavailable.

    Prefers a bare `opencode` on PATH (choco/scoop/binary install). Falls back
    to `npx -y -p opencode-ai opencode` so a user with only Node installed can
    still run (npx downloads the package on first use). Returns None if neither
    `opencode` nor `npx` is resolvable on PATH.

    Windows quirk: `npx` and `opencode` typically install as `.CMD` batch
    wrappers. `subprocess.Popen` with a list (no shell) calls `CreateProcess`
    directly, which can't launch `.CMD`/`.BAT` files — it needs `cmd.exe /c`.
    So when the resolved binary is a `.cmd`/`.bat`, we prepend ``cmd /c`` and
    use the full resolved path. `.exe` binaries are launched directly.
    """
    opencode_path = shutil.which("opencode")
    if opencode_path:
        return _wrap_if_batch(opencode_path)
    npx_path = shutil.which("npx")
    if npx_path:
        return _wrap_if_batch(npx_path) + ["-y", "-p", "opencode-ai", "opencode"]
    return None


def _wrap_if_batch(exe_path: str) -> list[str]:
    """Prepend `cmd /c` if the resolved binary is a .cmd/.bat wrapper.

    `CreateProcess` (used by `subprocess.Popen` without `shell=True`) only
    launches `.exe` files directly; `.cmd`/`.bat` need `cmd.exe /c` to interpret
    them. Using the full resolved path (not just the bare name) avoids re-
    searching PATH inside cmd.exe and is robust to spaces in the path.
    """
    lower = exe_path.lower()
    if lower.endswith(".cmd") or lower.endswith(".bat"):
        return ["cmd", "/c", exe_path]
    return [exe_path]


# --- Windows Job Object (kill-on-close) backstop ---
# On Windows, assigning a child process to a Job Object with
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE makes the OS kill the child (and its whole
# tree) when the *last* handle to the job is closed — which happens when the
# Python parent process exits, however it exits (clean return, sys.exit,
# TerminateProcess, segfault). This is the only reliable way to reap a detached
# subprocess tree when the parent is force-killed; try/finally and atexit do
# not run under TerminateProcess. No-op on non-Windows.
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    # Win64 width-matched ctypes aliases (wintypes lacks SIZE_T/PVOID/ULONGLONG).
    _SIZE_T = ctypes.c_size_t
    _PVOID = ctypes.c_void_p
    _ULONGLONG = ctypes.c_ulonglong

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", _SIZE_T),
            ("MaximumWorkingSetSize", _SIZE_T),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", _PVOID),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", _ULONGLONG),
            ("WriteOperationCount", _ULONGLONG),
            ("OtherOperationCount", _ULONGLONG),
            ("ReadTransferCount", _ULONGLONG),
            ("WriteTransferCount", _ULONGLONG),
            ("OtherTransferCount", _ULONGLONG),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", _SIZE_T),
            ("JobMemoryLimit", _SIZE_T),
            ("PeakProcessMemoryUsed", _SIZE_T),
            ("PeakJobMemoryUsed", _SIZE_T),
        ]

    # Keep a module-level ref so the job handle isn't GC'd (which would close it
    # prematurely and kill the child before the parent exits).
    _job_handle = None


def _assign_kill_on_close_job(pid: int) -> None:
    """Assign `pid` to a Job Object that kills the process tree when this
    Python process exits. Windows-only; no-op on other platforms.

    Creates one job for the process lifetime (lazily on first call) and assigns
    each spawned child to it. The job handle is held in the module-global
    `_job_handle` so it stays open until the interpreter shuts down — at which
    point the OS closes the handle and reaps every assigned process.
    """
    if sys.platform != "win32":
        return
    global _job_handle
    try:
        if _job_handle is None:
            _job_handle = _kernel32.CreateJobObjectW(None, None)
            if not _job_handle:
                return
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = _kernel32.SetInformationJobObject(
                _job_handle,
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                # Couldn't set the kill-on-close flag; the job is useless, so
                # close it and leave _job_handle None so we don't try again.
                _kernel32.CloseHandle(_job_handle)
                _job_handle = None
                return
        _kernel32.AssignProcessToJobObject(_job_handle, wintypes.HANDLE(pid))
    except Exception:  # noqa: BLE001 — safety-net path must never raise
        pass


class OpencodeServe:
    """Owns a single `opencode serve` subprocess for the process lifetime.

    Usage:
        svc = OpencodeServe()
        if svc.start():           # spawns + waits for /global/health
            ...                   # bot can connect
        # on exit:
        svc.stop()                # idempotent, tree-kills the subprocess
    """

    def __init__(
        self,
        *,
        port: int | None = None,
        hostname: str | None = None,
        cors: Sequence[str] | None = None,
        startup_timeout: float | None = None,
        enabled: bool | None = None,
        password: str | None = None,
        cwd: str | None = None,
    ) -> None:
        self._port = port if port is not None else config.opencode_serve_port
        self._hostname = (
            hostname if hostname is not None else config.opencode_serve_hostname
        )
        self._cors = (
            list(cors) if cors is not None else list(config.opencode_serve_cors)
        )
        self._startup_timeout = (
            startup_timeout
            if startup_timeout is not None
            else config.opencode_serve_startup_timeout
        )
        self._enabled = (
            enabled if enabled is not None else config.opencode_serve_enabled
        )
        self._password = (
            password if password is not None else config.opencode_server_password
        )
        # Pin the subprocess cwd so `opencode serve`'s `process.cwd()` resolves
        # to the user's project (see `_REPO_ROOT` above). Defaults to
        # `_REPO_ROOT` when not specified so the bot can spawn from any
        # directory and still get the right project dir.
        self._cwd = cwd if cwd is not None else str(_REPO_ROOT)
        self._proc: subprocess.Popen[bytes] | None = None
        self._started = False
        self._reused = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def url(self) -> str:
        return f"http://{self._hostname}:{self._port}"

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        """Spawn the server and wait for `/global/health` to respond 200.

        Returns True if the server is up (or already running). Returns False
        (without raising) if disabled, if the opencode binary isn't found, or
        if the server doesn't become healthy within the startup timeout — the
        bot should still start in all those cases so the user isn't blocked.

        If a healthy server is already listening at the target URL (e.g. a
        previous run left one running, or the user started `opencode serve`
        manually), it is reused and no subprocess is spawned. `stop()` won't
        kill a reused server (it has no proc to kill) — that's correct, since
        the bot didn't start it.
        """
        if not self._enabled:
            return False
        if self.is_running:
            return True

        # Pre-flight: if a server is already healthy at the URL, reuse it
        # instead of spawning a duplicate (which would fail to bind the port
        # and waste the startup timeout). This handles the common case of a
        # stale `opencode serve` from a previous bot run.
        if self._probe_healthy():
            print(
                f"opencode serve: reusing existing server at {self.url}",
                file=sys.stderr,
            )
            self._reused = True
            return True

        argv = _resolve_opencode_argv()
        if argv is None:
            print(
                "opencode serve: binary not found (no `opencode` or `npx` on PATH); "
                "skipping. Install opencode or npm install -g opencode-ai to enable.",
                file=sys.stderr,
            )
            return False

        cmd = argv + ["serve", "--port", str(self._port), "--hostname", self._hostname]
        for origin in self._cors:
            cmd += ["--cors", origin]

        popen_kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            # Pin the subprocess cwd so the server's `process.cwd()` resolves
            # to the user's project. `opencode serve` has no `--cwd` flag and
            # the server's `defaultDirectory` resolver falls back to
            # `process.cwd()` when no `?directory=` query /
            # `x-opencode-directory` header is on the request (the bot's
            # `OpencodeClient` sends neither). Without this, a bot launched
            # from an arbitrary dir would resolve every session's project dir
            # to that dir and `.opencode/plans/` would land under
            # `<dir>/.opencode/plans/` (which may not exist) — silent plan loss.
            "cwd": self._cwd,
            # `opencode serve` returns 401 on every endpoint unless
            # OPENCODE_SERVER_PASSWORD is set, so seed the subprocess env with
            # the configured password. A copy of the parent env lets the server
            # pick up OPENCODE_SERVER_USERNAME, PATH, etc. An explicit env var
            # override (already in os.environ) wins over the setting default.
            "env": {**os.environ, "OPENCODE_SERVER_PASSWORD": self._password},
        }
        if sys.platform == "win32":
            # New process group so CTRL_BREAK_EVENT reaches the tree. The
            # CREATE_NO_WINDOW flag suppresses a flashing console window.
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True

        try:
            self._proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError as e:
            print(f"opencode serve: failed to spawn {cmd[0]}: {e}", file=sys.stderr)
            self._proc = None
            return False

        if sys.platform == "win32":
            # Safety net for force-kill / hard crash: assign the child to a Job
            # Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so the OS reaps the
            # whole `opencode serve` tree when the Python parent dies — even if
            # the try/finally + atexit teardown never runs (e.g. TerminateProcess
            # from Task Manager, or an unhandled segfault). The explicit stop()
            # path is still the normal teardown; the job is the backstop.
            _assign_kill_on_close_job(self._proc.pid)

        if not self._wait_healthy(self._startup_timeout):
            print(
                f"opencode serve: server did not become healthy at {self.url} "
                f"within {self._startup_timeout}s; stopping it and continuing without.",
                file=sys.stderr,
            )
            self.stop()
            return False

        self._started = True
        print(f"opencode serve: ready at {self.url}", file=sys.stderr)
        return True

    def _auth_header(self) -> str:
        """Basic-auth header value for the configured password."""
        return (
            "Basic " + base64.b64encode(f"opencode:{self._password}".encode()).decode()
        )

    def _probe_healthy(self) -> bool:
        """One-shot authed GET /global/health. True if 200, False otherwise.

        Used as a pre-flight to detect an already-running server at the target
        URL so we reuse it instead of spawning a duplicate that can't bind.
        Never raises.
        """
        try:
            req = urllib.request.Request(
                f"{self.url}/global/health",
                headers={"Authorization": self._auth_header()},
            )
            with urllib.request.urlopen(req, timeout=2.0) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001 — probe must never raise
            return False

    def _wait_healthy(self, timeout: float) -> bool:
        """Poll GET /global/health until 200 or timeout. Never raises.

        Sends basic auth (opencode:<password>) because `opencode serve` returns
        401 on every endpoint when OPENCODE_SERVER_PASSWORD is set — a 401 means
        the server IS listening, but we keep polling for the authed 200 to
        confirm it's fully ready and the password is correct.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        url = f"{self.url}/global/health"
        auth_header = self._auth_header()
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                # Process exited before becoming healthy — drain its output so
                # the user can see why (e.g. port already in use).
                self._drain_output_to_stderr()
                return False
            try:
                req = urllib.request.Request(
                    url, headers={"Authorization": auth_header}
                )
                with urllib.request.urlopen(req, timeout=1.0) as r:
                    if r.status == 200:
                        return True
            except urllib.error.HTTPError as e:
                # 401 = server is up but auth failed (wrong password). Keep
                # polling briefly in case it's a startup race, but this likely
                # means a config mismatch — fall through to the timeout path,
                # which stops the server and logs the issue.
                if e.code == 401:
                    pass
                else:
                    pass
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                pass
            time.sleep(0.25)
        return False

    def _drain_output_to_stderr(self) -> None:
        """Copy any buffered subprocess stdout/stderr to our stderr (best-effort)."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            out = self._proc.stdout.read(4000)
            if out:
                sys.stderr.write(out.decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 — teardown/diagnostic path must not raise
            pass

    def stop(self) -> None:
        """Tear down the subprocess tree. Idempotent; never raises.

        A reused server (one we detected already running at start time, rather
        than spawned ourselves) is left alone — the bot didn't start it, so it
        shouldn't kill it.
        """
        if self._reused:
            return
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            self._proc = None
            return

        if sys.platform == "win32":
            # taskkill /T tree-kills (Popen.terminate only kills the launcher
            # for npx-based spawns, leaving node grandchildren alive).
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
            except Exception:  # noqa: BLE001 — teardown must not raise
                try:
                    self._proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        else:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
            # Grace period, then SIGKILL the group if still alive.
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    try:
                        self._proc.kill()
                    except Exception:  # noqa: BLE001
                        pass

        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            self._proc.stdout.close() if self._proc.stdout else None
        except Exception:  # noqa: BLE001
            pass
        self._proc = None
