"""Production docker-sandbox PROVISIONING wiring (SPEC I3, wave-1 critic fix).

``wire_container_trial`` / ``ContainerTrialWiring`` decide the runner image, the
read-only credential mounts, the ``host.docker.internal`` add-host and the
fixture URL — but selecting the image / attaching mounts / setting extra_hosts
is a *provisioning-time* concern that ``sandbox().exec(env=...)`` cannot express.
``engines.sandbox_compose`` renders exactly that provisioning as an Inspect
docker compose spec, and ``inspect_adapter`` threads it so a REAL run provisions
the runner-image sandbox (the fake-exec unit path is untouched).

Two layers, both offline, NO model call ever:

(a) unit — the rendered compose spec carries the right image, the read-only
    cred mounts, the extra_hosts, and the non-secret fixture env, with NO secret
    value on disk;
(b) integration — provision the ``claude-runner`` sandbox THROUGH Inspect and
    run ``claude --version`` (no auth, no model) inside it, proving the runner
    image was used and that the mount + add-host + fixture env took effect.
    Skips loudly when docker is unavailable.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from wololo_evals.engines.container_auth import BindMount, build_plan
from wololo_evals.engines.sandbox_compose import (
    SERVICE_NAME,
    render_compose_yaml,
    sandbox_compose_file,
)
from wololo_evals.inspect_adapter import (
    ADD_HOST,
    ContainerTrialWiring,
    probe_container,
    wire_container_trial,
)

# Sentinel secret — only ever injected via source_env, never a real token. Its
# ABSENCE from the on-disk compose file is the security assertion.
_CLAUDE_TOKEN = "sk-claude-oauth-SENTINEL-MUST-NOT-REACH-DISK"


# ---------------------------------------------------------------------------
# (a) unit: the rendered compose spec — image, ro mounts, add-host, fixture env
# ---------------------------------------------------------------------------
def _claude_wiring(home: Path, *, fixture="http://127.0.0.1:51000", page="native-select.html"):
    """A hermetic claude wiring: fake token via source_env, cred file under an
    injected home, a dummy base exec (unit tests never exec)."""
    cred = home / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text('{"token": "unused-by-mount"}')
    wiring = wire_container_trial(
        "claude",
        fixture,
        page,
        source_env={"CLAUDE_CODE_OAUTH_TOKEN": _CLAUDE_TOKEN},
        home=str(home),
        base_exec_factory=lambda: (lambda *a, **k: None),
    )
    return wiring, cred


def test_compose_selects_runner_image_and_service(tmp_path):
    wiring, _ = _claude_wiring(tmp_path / "home")
    doc = yaml.safe_load(render_compose_yaml(wiring))

    assert set(doc["services"]) == {SERVICE_NAME}  # Inspect's default service
    svc = doc["services"][SERVICE_NAME]
    assert svc["image"] == "wololo-evals/claude-runner"
    # keep-alive so the container idles for docker-exec trial steps
    assert svc["command"] == ["sleep", "infinity"]


def test_compose_attaches_readonly_credential_mount(tmp_path):
    wiring, cred = _claude_wiring(tmp_path / "home")
    svc = yaml.safe_load(render_compose_yaml(wiring))["services"][SERVICE_NAME]

    # dest tracks CLAUDE_CONFIG_DIR=/profile (the mount-fix alignment) so the
    # CLI reads the credential where it looks for its config.
    assert svc["volumes"] == [
        f"{cred}:/profile/.credentials.json:ro"
    ]
    # every mount the wiring carries is read-only (host auth is never writable)
    assert all(m.read_only for m in wiring.mounts)


def test_compose_sets_extra_hosts_and_fixture_env(tmp_path):
    wiring, _ = _claude_wiring(tmp_path / "home")
    svc = yaml.safe_load(render_compose_yaml(wiring))["services"][SERVICE_NAME]

    assert svc["extra_hosts"] == [ADD_HOST]  # host.docker.internal:host-gateway
    # the loopback fixture URL is rewritten onto host.docker.internal
    assert svc["environment"] == {
        "WOLOLO_FIXTURE_URL": "http://host.docker.internal:51000/native-select.html"
    }


def test_compose_never_writes_a_secret_value(tmp_path):
    """The auth env KEY is known but its VALUE must never touch the compose file
    on disk — auth flows via the mounted cred file and exec-time env only."""
    wiring, _ = _claude_wiring(tmp_path / "home")
    text = render_compose_yaml(wiring)

    assert _CLAUDE_TOKEN not in text
    # the auth KEY name is deliberately absent from the compose environment too;
    # only the non-secret fixture URL is emitted there.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in text
    # and the plan advertises the KEY (values resolved lazily elsewhere)
    assert wiring.plan.env_keys == ("CLAUDE_CODE_OAUTH_TOKEN",)


def test_compose_file_context_materialises_then_cleans_up(tmp_path):
    wiring, _ = _claude_wiring(tmp_path / "home")
    with sandbox_compose_file(wiring) as compose_path:
        p = Path(compose_path)
        assert p.is_file()
        doc = yaml.safe_load(p.read_text())
        assert doc["services"][SERVICE_NAME]["image"] == "wololo-evals/claude-runner"
    # throwaway dir removed on context exit
    assert not p.exists()
    assert not p.parent.exists()


def test_render_raises_for_harness_without_runner_image():
    """A wiring with no runner image cannot be provisioned — render must fail
    loudly rather than silently fall back to Inspect's generic default image."""
    plan = build_plan("claude", source_env={}, home="/nonexistent")
    wiring = ContainerTrialWiring(
        harness="imageless",
        engine_id="claude",
        image=None,
        plan=plan,
        fixture_url="http://host.docker.internal:5000/",
        add_host=ADD_HOST,
        exec_factory=lambda: (lambda *a, **k: None),
    )
    with pytest.raises(ValueError, match="no runner image"):
        render_compose_yaml(wiring)


# ---------------------------------------------------------------------------
# (b) integration: provision the real runner image via Inspect, run claude
#     --version inside it (no auth, no model). Skips loudly without docker.
# ---------------------------------------------------------------------------
_DOCKER = shutil.which("docker")


def _docker_daemon_up() -> bool:
    if _DOCKER is None:
        return False
    try:
        return (
            subprocess.run(
                [_DOCKER, "info"], capture_output=True, timeout=30
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _runner_image_present() -> bool:
    try:
        out = subprocess.run(
            [_DOCKER, "image", "inspect", "wololo-evals/claude-runner"],
            capture_output=True,
            timeout=30,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark_docker = pytest.mark.skipif(
    not _docker_daemon_up(),
    reason="docker daemon unavailable; live-provisioning proof needs it",
)


@pytest.fixture()
def shareable_home():
    """A temp home under the user's HOME so Docker Desktop file sharing can
    bind-mount the credential file (its default share list excludes /tmp and
    /var/folders on macOS, where a file bind-mount degrades to an empty dir)."""
    d = Path(tempfile.mkdtemp(prefix="wololo-prov-", dir=str(Path.home())))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytestmark_docker
def test_inspect_provisions_runner_image_and_runs_version(shareable_home):
    """REAL provisioning, NO model: Inspect accepts the rendered compose spec,
    brings up wololo-evals/claude-runner with the ro cred mount + add-host +
    fixture env, and ``claude --version`` runs inside it."""
    if not _runner_image_present():
        pytest.skip(
            "wololo-evals/claude-runner image not built; run "
            "`docker build -t wololo-evals/claude-runner images/claude-runner`"
        )

    cred = shareable_home / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True)
    cred_marker = '{"marker": "wololo-mount-probe"}'
    cred.write_text(cred_marker)

    # Real base exec (default _sandbox_exec -> sandbox().exec); auth VALUE is a
    # sentinel injected via source_env, no real token, and --version needs none.
    wiring = wire_container_trial(
        "claude",
        "http://127.0.0.1:51999",
        "native-select.html",
        source_env={"CLAUDE_CODE_OAUTH_TOKEN": _CLAUDE_TOKEN},
        home=str(shareable_home),
    )
    assert wiring.image == "wololo-evals/claude-runner"
    # the spec Inspect will provision from names exactly the runner image
    assert "wololo-evals/claude-runner" in render_compose_yaml(wiring)

    # One provisioned sandbox; a compound probe verifies image + mount + add-host
    # + fixture env in a single container bring-up.
    probe = (
        "claude --version; "
        "echo '---HOSTS---'; getent hosts host.docker.internal "
        "|| grep host.docker.internal /etc/hosts; "
        "echo '---CRED---'; cat /profile/.credentials.json; "
        "echo '---FIXURL---'; printf '%s\\n' \"$WOLOLO_FIXTURE_URL\"; "
        "echo '---RO---'; "
        "(touch /profile/.credentials.json && echo WRITABLE) || echo READONLY"
    )
    code, out, err = probe_container(wiring, ["sh", "-c", probe], timeout=120)
    combined = out + err
    assert code == 0, f"probe exited {code}: {combined!r}"

    # 1. runner image was used: only claude-runner carries the claude CLI.
    m = re.search(r"(\d+)\.(\d+)\.(\d+)\s*\(Claude Code\)", combined)
    assert m, f"no claude version banner in probe output: {combined!r}"
    assert 1 <= int(m.group(1)) < 3, f"claude {m.group(0)} outside >=1.0.0,<3.0.0"

    # 2. add-host took effect: host.docker.internal resolves inside the container.
    assert "host.docker.internal" in combined

    # 3. read-only credential mount took effect: the host file is visible AND
    #    unwritable from inside the container.
    assert cred_marker in combined, "credential bind-mount did not take effect"
    assert "READONLY" in combined and "WRITABLE" not in combined

    # 4. the non-secret fixture URL was injected, rewritten onto host.docker.internal.
    assert "http://host.docker.internal:51999/native-select.html" in combined

    # 5. no secret VALUE leaked into the captured output.
    assert _CLAUDE_TOKEN not in combined
