"""Component integration, SPEC S14 rung 2: at least one REAL containerized
harness-under-eval trial per harness through the production path
(``exec_factory=None`` -> ``inspect_adapter._sandbox_exec`` -> the real
``sandbox().exec`` running the pinned harness CLI in a network-disabled
per-trial docker cell).

This is the ONE thing the injected exec seam in ``test_harness_uplift.py``
cannot prove: that the real sandbox path executes. It is an operator
precondition, not something CI here can run — there is no docker daemon in the
reference build environment — so it is gated behind the ``compose`` marker and
skips LOUDLY (never silently passes) until the daemon + a model API key exist.

Run it once the stack is up::

    docker compose up -d --wait
    WOLOLO_EVALS_COMPOSE_TRIAL=1 ANTHROPIC_API_KEY=... \
        pytest -m compose tests/test_compose_integration.py

The trial drives ``run_task_uplift`` with the docker sandbox (exec_factory
left None) for one paired trial and asserts a real per-trial composite comes
back — the exact production code path, no fake executor anywhere.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.compose

REQUIRED_ENV = "WOLOLO_EVALS_COMPOSE_TRIAL"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=15
        ).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _preconditions() -> str | None:
    """Return the missing-precondition reason, or None when good to run."""
    if os.environ.get(REQUIRED_ENV) != "1":
        return (
            f"{REQUIRED_ENV}=1 not set: the real containerized trial is an "
            "operator precondition, run `docker compose up -d --wait` first"
        )
    if not _docker_available():
        return "no docker daemon reachable (`docker info` failed)"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("WOLOLO_EVALS_JUDGE_API_KEY")):
        return "no model API key (ANTHROPIC_API_KEY / WOLOLO_EVALS_JUDGE_API_KEY)"
    return None


@pytest.mark.parametrize("harness", ["claude-code", "hermes"])
def test_real_containerized_task_uplift_trial(harness):
    reason = _preconditions()
    if reason:
        pytest.skip(f"compose trial precondition unmet: {reason}")

    # Imported lazily so collection never fails when inspect_ai's docker
    # sandbox provider is unusable in a daemonless environment.
    from wololo_evals.harnesses import resolve_runner
    from wololo_evals.inspect_adapter import run_task_uplift
    from wololo_evals.judge import http_call_model
    from wololo_evals.settings import settings
    from wololo_evals.suites.task_uplift.task import TaskFixture

    cfg = settings()
    call_model = http_call_model(
        cfg.judge_model, base_url=cfg.judge_base_url, api_key=cfg.judge_api_key
    )
    fixture = TaskFixture(
        fixture_id="compose-smoke",
        repo=os.environ.get("WOLOLO_EVALS_FIXTURE_REPO", "/srv/fixtures/atlas-kyc-svc.git"),
        ref=os.environ.get("WOLOLO_EVALS_FIXTURE_REF", "HEAD"),
        task_prompt="Fix the KYC silent-drop bug.",
        acceptance_command=("python", "-m", "pytest", "-q", "tests/test_kyc.py"),
        max_turns=8,
        wall_clock_cap_s=600,
    )
    runner = resolve_runner(harness)
    # exec_factory left None on purpose: this exercises the REAL sandbox().exec
    # production path, not the injected fake seam.
    outcome = run_task_uplift(
        fixture, runner, n_trials=1,
        skill_slug="kyc-fix",
        skill_files={"SKILL.md": "# kyc-fix\nAlways add a validation gate."},
        call_model=call_model,
        k=5, sandbox_type="docker",
    )
    assert outcome.status in ("completed", "errored")
    if outcome.status == "completed":
        assert outcome.trials, "a real containerized trial produced no scored trial"
        assert 0.0 <= outcome.trials[0]["composite"] <= 1.0
