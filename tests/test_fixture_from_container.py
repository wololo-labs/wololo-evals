"""Integrate-phase proof (real docker, NO model): the host-side browser fixture
server is reachable from *inside* a container over the Docker gateway alias.

This closes the last unverified seam in the live docker-sandbox wiring. Every
other piece is proven offline (image ``--version`` in-container; auth env/mount
threading over a fake exec in ``test_sandbox_wiring``), but "a container can
actually reach the host fixture server on ``host.docker.internal``" can only be
shown by running a real container against a real server. That is exactly — and
only — what this test does: it starts :class:`BrowserFixtureServer` on the host
loopback (ephemeral port), then

    docker run --rm --add-host=host.docker.internal:host-gateway \
        <slim curl image> curl -s http://host.docker.internal:<port>/<page>

and asserts the fixture page comes back. No auth, no ``claude``/``codex``, no
model — a plain static ``curl`` over the gateway.

The URL the container fetches is built by the production helper
:func:`wololo_evals.inspect_adapter.container_fixture_url` (loopback ->
``host.docker.internal``), so this also proves the wiring's rewrite is the exact
URL that is routable from a container, not merely a hand-written string.

Skip-guards (loud, never a silent pass on a broken assumption):

* docker CLI absent -> module skipped.
* no curl-capable slim image can be obtained (offline registry) -> skipped.
* ``--add-host=host.docker.internal:host-gateway`` not honoured by this Docker
  (curl exit 6, "couldn't resolve host") -> skipped, since older Linux Docker
  (< 20.10) lacks the ``host-gateway`` magic alias. It IS supported on Docker
  Desktop and Docker >= 20.10, so it runs in the reference environment.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from wololo_evals.inspect_adapter import ADD_HOST, container_fixture_url
from wololo_evals.engines.browser_fixture_server import BrowserFixtureServer

_DOCKER = shutil.which("docker")

pytestmark = pytest.mark.skipif(
    _DOCKER is None,
    reason="docker CLI not on PATH; container-reachability proof needs it",
)

PAGES_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "browser" / "pages"
)

# One fixture page + a marker string that only appears when that exact page is
# served (lifted from the fixtures themselves — same contract as
# ``test_browser_fixture_server``).
FIXTURE_PAGE = "native-select.html"
FIXTURE_MARKER = "Native select"

# Slim, curl-carrying images to try in order. The runner images are Node/Python
# slim variants with NO curl, so a dedicated curl image is used for the probe.
_CURL_IMAGES = ("curlimages/curl:latest", "curlimages/curl")

# curl(1) exit codes we branch on.
_CURL_COULD_NOT_RESOLVE_HOST = 6  # host.docker.internal alias not mapped
_CURL_COULD_NOT_CONNECT = 7  # alias resolved but nothing listening (a real bug)


def _image_present(image: str) -> bool:
    proc = subprocess.run(
        [_DOCKER, "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0


def _try_pull(image: str) -> bool:
    proc = subprocess.run(
        [_DOCKER, "pull", image],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc.returncode == 0


@pytest.fixture(scope="module")
def curl_image() -> str:
    """A locally-available slim image carrying ``curl``.

    Prefers an already-pulled image (offline-friendly); falls back to a single
    bounded pull. If none can be obtained (no registry access, no local copy),
    the reachability proof is skipped rather than failing — the missing piece is
    the operator's registry access, not the wiring under test.
    """
    for image in _CURL_IMAGES:
        if _image_present(image):
            return image
    for image in _CURL_IMAGES:
        if _try_pull(image):
            return image
    pytest.skip(
        "no curl-capable slim image available locally or pullable "
        f"(tried {list(_CURL_IMAGES)}); container-reachability proof needs one"
    )


def _curl_in_container(image: str, url: str) -> subprocess.CompletedProcess[str]:
    """``docker run`` a container that curls ``url`` over the host gateway.

    ``--add-host=host.docker.internal:host-gateway`` is the whole point: it maps
    the alias the container uses (``ADD_HOST``) to the host, so a loopback
    server on the host is reachable. ``-sS`` keeps curl quiet but surfaces
    errors on stderr; ``--max-time`` bounds a hung fetch.
    """
    return subprocess.run(
        [
            _DOCKER,
            "run",
            "--rm",
            f"--add-host={ADD_HOST}",
            image,
            "curl",
            "-sS",
            "--max-time",
            "15",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_fixture_server_reachable_from_container(curl_image: str) -> None:
    """A real container curls the host fixture server over
    ``host.docker.internal`` and gets the fixture page back.

    Proves the last live-untested seam: host loopback server <- container, via
    the production URL rewrite. No model, no auth — a static page over curl.
    """
    with BrowserFixtureServer(PAGES_DIR) as srv:
        # The exact URL the trial wiring would inject into the container.
        url = container_fixture_url(srv.base_url, FIXTURE_PAGE)
        assert url == (
            f"http://host.docker.internal:{srv.port}/{FIXTURE_PAGE}"
        ), url

        proc = _curl_in_container(curl_image, url)

        if proc.returncode == _CURL_COULD_NOT_RESOLVE_HOST:
            pytest.skip(
                "curl exit 6 (could not resolve host.docker.internal): this "
                "Docker does not honour --add-host=host.docker.internal:"
                "host-gateway (need Docker Desktop or Linux Docker >= 20.10)"
            )
        assert proc.returncode == 0, (
            f"curl failed (exit {proc.returncode}); "
            f"stderr={proc.stderr!r} stdout={proc.stdout[:200]!r}. "
            f"(exit {_CURL_COULD_NOT_CONNECT} => alias resolved but the host "
            "server was unreachable — a real wiring/networking bug)"
        )
        assert FIXTURE_MARKER in proc.stdout, (
            f"fixture page fetched from the container is missing marker "
            f"{FIXTURE_MARKER!r}; got {proc.stdout[:200]!r}"
        )


def test_container_gets_the_specific_page_not_a_default(curl_image: str) -> None:
    """The container reaches the *addressed* page, not an index/default — a
    second page with its own marker rules out "any 200 counts". Confirms the
    server routes by path across the gateway exactly as it does on the host."""
    page, marker = "portal-menu.html", "Portal menu"
    with BrowserFixtureServer(PAGES_DIR) as srv:
        url = container_fixture_url(srv.base_url, page)
        proc = _curl_in_container(curl_image, url)
        if proc.returncode == _CURL_COULD_NOT_RESOLVE_HOST:
            pytest.skip(
                "curl exit 6: --add-host host-gateway not honoured by this Docker"
            )
        assert proc.returncode == 0, (
            f"curl failed (exit {proc.returncode}); stderr={proc.stderr!r}"
        )
        assert marker in proc.stdout, (
            f"expected {marker!r} from {page}; got {proc.stdout[:200]!r}"
        )
