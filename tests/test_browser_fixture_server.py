"""Browser fixture server (GAP 1): the harness-managed static host that a later
browser-harness trial drives a real headless browser against.

Proves: the four dropdown fixtures each serve 200 with their expected marker
text; the port is ephemeral (never a fixed well-known port) and loopback-only;
path traversal out of the pages tree is refused; and the served pages carry no
external URL (self-contained, offline-safe). Pure: stdlib HTTP against a
throwaway loopback server; no Postgres, no network, no browser.
"""
from __future__ import annotations

import pathlib
import re
import urllib.error
import urllib.request

import pytest

from wololo_evals.engines.browser_fixture_server import (
    BrowserFixtureServer,
    launch_headless_chromium,
)

PAGES_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "browser" / "pages"
)

# Each fixture page keyed to a marker string that only appears when the right
# page is served (title/heading text lifted from the fixtures themselves).
PAGE_MARKERS = {
    "native-select.html": "Native select",
    "react-select.html": "Custom combobox",
    "portal-menu.html": "Portal menu",
    "distractor-no-dropdown.html": "Licensing FAQ",
}


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_serves_each_fixture_page_with_marker():
    with BrowserFixtureServer(PAGES_DIR) as srv:
        for page, marker in PAGE_MARKERS.items():
            status, body = _get(f"{srv.base_url}/{page}")
            assert status == 200, page
            assert marker in body, f"{page} missing marker {marker!r}"


def test_binds_loopback_on_ephemeral_port():
    with BrowserFixtureServer(PAGES_DIR) as srv:
        assert srv.base_url.startswith("http://127.0.0.1:")
        # Ephemeral: kernel-assigned, never a fixed well-known/default port.
        assert srv.port > 1024


def test_two_servers_run_concurrently_on_distinct_ports():
    with BrowserFixtureServer(PAGES_DIR) as a, BrowserFixtureServer(PAGES_DIR) as b:
        assert a.port != b.port
        assert _get(f"{a.base_url}/native-select.html")[0] == 200
        assert _get(f"{b.base_url}/portal-menu.html")[0] == 200


def test_refuses_path_traversal():
    with BrowserFixtureServer(PAGES_DIR) as srv:
        # A crafted ..-escape must not reach the repo's LICENSE outside pages/.
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{srv.base_url}/../../LICENSE")
        assert exc.value.code == 404


def test_served_pages_contain_no_external_urls():
    with BrowserFixtureServer(PAGES_DIR) as srv:
        for page in PAGE_MARKERS:
            _, body = _get(f"{srv.base_url}/{page}")
            assert not re.search(r"https?://", body), f"{page} has an external URL"


def test_stop_is_idempotent_outside_context_manager():
    srv = BrowserFixtureServer(PAGES_DIR).start()
    port = srv.port
    assert _get(f"{srv.base_url}/native-select.html")[0] == 200
    srv.stop()
    srv.stop()  # second stop is a no-op, not a crash
    assert port > 1024


def test_missing_pages_dir_fails_loud(tmp_path):
    with pytest.raises(FileNotFoundError):
        BrowserFixtureServer(tmp_path / "does-not-exist")


def test_launch_headless_chromium_is_availability_gated():
    # Deliverable is dependency-light: with no Chromium binary this returns None
    # rather than raising. When one exists we own the process and tear it down.
    proc = launch_headless_chromium("http://127.0.0.1:1/none")
    if proc is None:
        return
    try:
        assert proc.pid > 0
    finally:
        proc.terminate()
        proc.wait(timeout=5)
