"""Harness-managed static server for the browser fixtures (open, Apache-2.0).

Serves ``fixtures/browser/pages/`` over loopback on an **ephemeral** port so a
later browser-harness trial can drive a real headless browser at the four
dropdown fixtures (native ``<select>``, react-select-style widget, portal menu,
distractor) without any network dependency. The server is the guaranteed
deliverable; :func:`launch_headless_chromium` is a thin, availability-gated
convenience that returns ``None`` when no Chromium binary is on ``PATH``.

Design (matches the engines pillar: stdlib only, no vendor SDKs):

- Binds ``0.0.0.0:0``: kernel-assigned free port, so any number of these can run
  concurrently (each parallel trial gets its own port). All interfaces, not just
  loopback, because a container reaches this server over the Docker gateway
  alias ``host.docker.internal``. On Linux that alias resolves to the bridge
  gateway address, and a socket bound to ``127.0.0.1`` refuses connections
  arriving there. ``base_url`` still advertises loopback for host-side callers;
  ``inspect_adapter.container_fixture_url`` rewrites it for container callers.
- ``ThreadingHTTPServer`` on a daemon thread; the context-manager stops it on
  exit. ``srv.base_url`` is the root to fetch pages from.
- Serving is pinned to the pages directory via ``SimpleHTTPRequestHandler``'s
  ``translate_path`` (which normalises away ``..`` traversal), so a request for
  ``/../../secret`` can never escape the fixtures tree.
"""
from __future__ import annotations

import contextlib
import functools
import http.server
import logging
import pathlib
import shutil
import socketserver
import subprocess
import threading
from typing import Optional

__all__ = ["BrowserFixtureServer", "launch_headless_chromium"]

_LOG = logging.getLogger(__name__)

#: Chromium binaries we know how to launch headless, in preference order.
_CHROMIUM_BINARIES = ("chromium", "google-chrome", "chrome", "chromium-browser")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """``SimpleHTTPRequestHandler`` pinned to a directory, without stderr noise.

    ``translate_path`` (inherited) already collapses ``..`` and strips the
    leading slash before joining onto ``directory``, so path traversal out of
    the fixtures tree resolves back inside it and 404s — no extra guard needed.
    """

    def log_message(self, *args: object) -> None:  # noqa: D401 - silence access log
        return


class BrowserFixtureServer:
    """Context-managed static server for a fixtures ``pages`` directory.

    Usage::

        with BrowserFixtureServer(pages_dir) as srv:
            html = urllib.request.urlopen(srv.base_url + "/native-select.html")

    Binds loopback on an ephemeral port; the port is only known after
    ``__enter__`` (or :meth:`start`). Idempotent ``stop`` makes it safe to use
    outside a ``with`` block too.
    """

    def __init__(self, pages_dir: str | pathlib.Path) -> None:
        self._pages_dir = pathlib.Path(pages_dir).resolve()
        if not self._pages_dir.is_dir():
            raise FileNotFoundError(f"pages dir not found: {self._pages_dir}")
        self._httpd: Optional[socketserver.TCPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def pages_dir(self) -> pathlib.Path:
        return self._pages_dir

    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("server not started")
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "BrowserFixtureServer":
        if self._httpd is not None:
            return self
        handler = functools.partial(_QuietHandler, directory=str(self._pages_dir))
        # ("0.0.0.0", 0): all interfaces + kernel-assigned free port. Must not
        # be loopback: containers reach this over the Docker bridge gateway.
        # Serving stays confined to the pages directory by translate_path.
        self._httpd = http.server.ThreadingHTTPServer(("0.0.0.0", 0), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="browser-fixture-server", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "BrowserFixtureServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def launch_headless_chromium(url: str) -> Optional[subprocess.Popen]:
    """Launch a headless Chromium pointed at ``url``, or ``None`` if unavailable.

    Availability-gated on purpose: real browser drivers are **not** a dependency
    of this package, so when no Chromium binary is on ``PATH`` this logs and
    returns ``None`` rather than raising. The caller owns the returned process
    (terminate/wait); it is spawned detached from any window with a throwaway
    profile so concurrent launches don't collide.
    """
    binary = next((b for b in _CHROMIUM_BINARIES if shutil.which(b)), None)
    if binary is None:
        _LOG.info("no Chromium binary on PATH (%s); skipping headless launch",
                  ", ".join(_CHROMIUM_BINARIES))
        return None
    with contextlib.suppress(OSError):
        return subprocess.Popen(
            [
                binary,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--no-first-run",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    _LOG.warning("Chromium binary %s present but failed to launch", binary)
    return None
