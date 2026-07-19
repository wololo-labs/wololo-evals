"""Offline image verification for ``wololo-evals/codex-runner``
(docs/plans/wave1-contracts.md I2).

Proves the sandbox image for the Codex CLI harness-under-eval is buildable and
correct WITHOUT any live model call or auth:

- ``docker build`` the image,
- ``docker run --rm … codex --version`` prints a version (no model, no auth —
  ``--version`` needs neither), exercising the entrypoint's argv passthrough,
- ``docker history`` carries no baked secret (auth must arrive at run time via
  read-only mounts + env from ``engines/container_auth.py``, never a layer).

Skip-guarded (never silently passes) when no docker daemon is reachable — the
build environment here HAS one, but CI elsewhere may not.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

IMAGE = "wololo-evals/codex-runner"
_IMAGE_DIR = pathlib.Path(__file__).resolve().parent.parent / "images" / "codex-runner"
# Version pinned in the Dockerfile ARG; asserted against `codex --version`.
_EXPECTED_VERSION = "0.141"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except (subprocess.TimeoutExpired, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="no docker daemon reachable (`docker info` failed)"
)


@pytest.fixture(scope="module")
def built_image() -> str:
    """Build (or rebuild) the codex-runner image once for this module."""
    assert (_IMAGE_DIR / "Dockerfile").is_file(), f"missing Dockerfile in {_IMAGE_DIR}"
    proc = subprocess.run(
        ["docker", "build", "-t", IMAGE, str(_IMAGE_DIR)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, f"docker build failed:\n{proc.stdout}\n{proc.stderr}"
    return IMAGE


def test_codex_version_prints_inside_container(built_image: str) -> None:
    """`codex --version` runs headless in-container and prints the pinned version.

    No auth, no model call — the entrypoint execs the passthrough argv.
    """
    proc = subprocess.run(
        ["docker", "run", "--rm", built_image, "codex", "--version"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"codex --version failed:\n{proc.stdout}\n{proc.stderr}"
    out = (proc.stdout + proc.stderr).strip()
    assert out, "codex --version printed nothing"
    assert _EXPECTED_VERSION in out, f"unexpected codex version banner: {out!r}"


def test_no_baked_secret_in_layers(built_image: str) -> None:
    """No credential/token is baked into any image layer.

    Auth is a run-time concern (read-only mount + env); the layer history must
    be clean.
    """
    proc = subprocess.run(
        ["docker", "history", "--no-trunc", built_image],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"docker history failed:\n{proc.stderr}"
    lowered = proc.stdout.lower()
    for needle in ("token", "secret", "auth.json", "credential", "api_key", "apikey"):
        assert needle not in lowered, f"baked secret marker {needle!r} found in image history"


def test_seam_env_and_dirs_present(built_image: str) -> None:
    """Seam paths exist and CODEX_HOME/WOLOLO_TRANSCRIPT_DIR default correctly."""
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            built_image,
            "sh",
            "-c",
            'printf "%s\\n%s\\n" "$CODEX_HOME" "$WOLOLO_TRANSCRIPT_DIR"; '
            "test -d /workspace && test -d /profile && test -d /transcript && echo DIRS_OK",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"seam probe failed:\n{proc.stdout}\n{proc.stderr}"
    assert "/profile" in proc.stdout
    assert "/transcript" in proc.stdout
    assert "DIRS_OK" in proc.stdout
