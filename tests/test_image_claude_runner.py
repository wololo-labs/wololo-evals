"""Offline verification for the ``wololo-evals/claude-runner`` harness image
(wave1-contracts.md I1). Builds the image and shells out to a handful of
checks that need NO auth and make NO model call:

* ``claude --version`` runs inside the container and prints a version core
  inside ``ClaudeAdapter.default_version_range`` (">=1.0.0,<3.0.0").
* ``git`` is present (the harness seam clones / diffs /workspace in-container).
* the profile/transcript seam env resolves to the in-sandbox dirs, never a host
  ``$HOME`` path.
* ``docker history`` carries no baked secret.

Skip-marked (loudly) when no docker daemon is reachable: a docker CLI with its
daemon stopped used to fail the build fixture instead of skipping. The daemon
IS present in the reference environment, so these run there.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_DOCKER = shutil.which("docker")
_IMAGE = "wololo-evals/claude-runner"
_IMAGE_DIR = Path(__file__).resolve().parents[1] / "images" / "claude-runner"



def _daemon_up() -> bool:
    if _DOCKER is None:
        return False
    try:
        return subprocess.run([_DOCKER, "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(
    not _daemon_up(), reason="no docker daemon reachable (`docker info` failed); harness-image checks need one"
)


def _run(*argv: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_DOCKER, *argv],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def built_image() -> str:
    """Build the image once for the module. A build failure fails loudly with
    the docker output rather than skipping."""
    try:
        _run("build", "-t", _IMAGE, str(_IMAGE_DIR), timeout=1800)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - surface logs
        pytest.fail(
            "docker build failed:\n"
            f"stdout:\n{exc.stdout}\n\nstderr:\n{exc.stderr}"
        )
    return _IMAGE


def test_claude_version_runs_in_container(built_image: str) -> None:
    """``claude --version`` runs headless in the container with no auth and no
    model call, and reports a version inside the adapter's pinned range."""
    out = _run("run", "--rm", built_image, "claude", "--version", timeout=120)
    combined = out.stdout + out.stderr
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", combined)
    assert m, f"no version core in `claude --version` output: {combined!r}"
    major = int(m.group(1))
    # ClaudeAdapter.default_version_range == ">=1.0.0,<3.0.0"
    assert 1 <= major < 3, f"claude version {m.group(0)} outside >=1.0.0,<3.0.0"


def test_git_available_for_seam(built_image: str) -> None:
    """The seam runs ``git clone`` / ``git diff`` on /workspace inside this
    image, so git must be installed."""
    out = _run("run", "--rm", built_image, "git", "--version", timeout=60)
    assert "git version" in out.stdout


def test_profile_env_points_to_sandbox_not_host_home(built_image: str) -> None:
    """{profile_env} (CLAUDE_CONFIG_DIR) and WOLOLO_TRANSCRIPT_DIR resolve to the
    in-sandbox dirs — the CLI never reads/writes the host $HOME."""
    out = _run(
        "run",
        "--rm",
        built_image,
        "sh",
        "-c",
        "printf '%s\\n%s\\n' \"$CLAUDE_CONFIG_DIR\" \"$WOLOLO_TRANSCRIPT_DIR\"",
        timeout=60,
    )
    config_dir, transcript_dir = out.stdout.splitlines()[:2]
    assert config_dir == "/profile"
    assert transcript_dir == "/transcript"


def test_no_baked_secret_in_image(built_image: str) -> None:
    """No credential material is baked into any layer — auth is a run-time
    concern (env + read-only mounts), never a COPY/ENV in the image."""
    out = _run("history", "--no-trunc", built_image, timeout=60)
    low = out.stdout.lower()
    for needle in ("token", "api_key", "apikey", "oauth", "secret", "credential"):
        assert needle not in low, f"image history mentions {needle!r}: baked secret?"
