"""The skill-behaviour suite (wave-2 SPEC M4): a paired v1-vs-v2 skill-upgrade
A/B on the harness-under-eval bridge, proven without a docker daemon.

Layers, mirroring test_harness_uplift.py:
1. pure slot helpers (compose / activation / trace-conformance) -- no fixtures;
2. inspect_adapter.run_skill_behaviour over a fake SandboxExec + injected judge
   (both arms install a skill, v2 conforms better than v1 so the paired uplift
   is a genuine positive number), plus the distractor non-activation case, the
   injection tripwire, and the judge-disabled (verdict=skipped) path;
3. worker.execute_run on a real skill-behaviour run -> paired uplift stats,
   trials/scores/leaderboard rows on the real RLS Postgres (this also proves the
   003 migration extended the runs.eval_suite CHECK to accept the new value);
4. the API create_run route accepts eval_suite='skill-behaviour' post-migration.

The injected judge (a closure returning canned schema-valid JSON, ignoring its
prompt) keeps every downstream number deterministic -- test-architecture.md §3.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from sqlalchemy import text

from wololo_evals import versioning
from wololo_evals.crypto import EnvelopeCipher
from wololo_evals.db import tenant_txn
from wololo_evals.harnesses import resolve_runner
from wololo_evals.inspect_adapter import run_skill_behaviour
from wololo_evals.jobs.worker import execute_run
from wololo_evals.suites import base as suites_base
from wololo_evals.suites.skill_behaviour import task as sb

from support import JWKS_PATH, mint_token

TENANT = "meridian"
SUBMIT = ["wololo-evals:submit"]
DECLARED_WORKFLOW = ("read", "bash", "edit")

# The A/B under test: v2 follows the whole declared workflow in three turns; v1
# activates but only reads, in five turns. Both fix the task (acceptance passes).
SKILL_V1 = {"SKILL.md": "# dropdowns\nversion: v1\nread the page, then guess."}
SKILL_V2 = {"SKILL.md": "# dropdowns\nversion: v2\nread, run bash probe, then edit."}


# ---------------------------------------------------------------------------
# layer 1: pure slot helpers
# ---------------------------------------------------------------------------
def test_compose_judge_enabled_matches_base_composite():
    values = {
        "activation_correct": 1.0,
        "task_outcome": 1.0,
        "trace_conformance": 0.5,
        "llm_judge_overall": 0.8,
    }
    rubric = suites_base.load_rubric("skill-behaviour")
    assert sb.compose(values) == pytest.approx(0.87)
    # the rubric weights (0.25/0.40/0.20/0.15) reproduce compose exactly when
    # every slot participates -- the two composition paths cannot silently drift.
    assert suites_base.composite(rubric, values) == pytest.approx(sb.compose(values))


def test_compose_judge_disabled_drops_judge_weight_from_denominator():
    values = {"activation_correct": 1.0, "task_outcome": 1.0, "trace_conformance": 0.5}
    # (0.25 + 0.40 + 0.20*0.5) / 0.85 -- judge weight leaves the denominator.
    assert sb.compose(values, judge_disabled=True) == pytest.approx(0.75 / 0.85)


def test_compose_hard_gate_zeroes_regardless_of_slots():
    perfect = {
        "activation_correct": 1.0, "task_outcome": 1.0,
        "trace_conformance": 1.0, "llm_judge_overall": 1.0,
    }
    assert sb.compose(perfect, hard_gate_failed=True) == 0.0


def test_activation_correct_is_recall_and_non_activation():
    fired = [{"stage": "skill-activated"}, {"stage": "post-llm-event"}]
    silent = [{"stage": "post-llm-event"}]
    # genuine case: recall -- must fire
    assert sb.activation_correct_score(fired, expects_activation=True) == 1.0
    assert sb.activation_correct_score(silent, expects_activation=True) == 0.0
    # distractor: non-activation -- must stay silent
    assert sb.activation_correct_score(silent, expects_activation=False) == 1.0
    assert sb.activation_correct_score(fired, expects_activation=False) == 0.0


def test_trace_conformance_is_ordered_fraction_of_declared_workflow():
    full = [{"stage": "tool-call", "verb": v} for v in ("read", "bash", "edit")]
    partial = [{"stage": "tool-call", "verb": "read"}]
    assert sb.trace_conformance_score(full, DECLARED_WORKFLOW) == pytest.approx(1.0)
    assert sb.trace_conformance_score(partial, DECLARED_WORKFLOW) == pytest.approx(1 / 3)
    # no declared workflow -> nothing to conform to
    assert sb.trace_conformance_score(partial, ()) == 1.0


def test_load_rubric_rejects_suite_field_mismatch(tmp_path, monkeypatch):
    # A rubric whose declared `suite:` disagrees with the registry key it is
    # loaded under must be rejected -- otherwise one suite's rubric (pins, judge
    # k/temperature, rubric_version) could silently masquerade as another's.
    # Point a fresh registry key at a tmp dir holding a well-formed rubric whose
    # only fault is the wrong suite field; the weight-sum guard stays satisfied
    # (0.25+0.40+0.20+0.15 == 1.0) so the mismatch guard is what fires.
    good = suites_base.rubric_path("skill-behaviour").read_text()
    bad = good.replace("suite: skill-behaviour", "suite: wrong-key", 1)
    assert "suite: wrong-key" in bad and "suite: skill-behaviour" not in bad
    (tmp_path / "rubric.yaml").write_text(bad)
    monkeypatch.setitem(suites_base.SUITE_DIRS, "skill-behaviour-alias", tmp_path)

    with pytest.raises(ValueError, match="!= registry key 'skill-behaviour-alias'"):
        suites_base.load_rubric("skill-behaviour-alias")


# ---------------------------------------------------------------------------
# fake sandbox: distinguishes the installed skill VERSION by its content, and
# emits a version-specific activation + tool-call + turn trace.
# ---------------------------------------------------------------------------
class FakeSandbox:
    def __init__(self):
        self.version = "none"
        self.harness_runs: list[str] = []

    @staticmethod
    def _events(version: str) -> str:
        turns = 3 if version == "v2" else 5
        verbs = ("read", "bash", "edit") if version == "v2" else ("read",)
        evts: list[dict] = [{"stage": "skill-activated"}]
        evts += [{"stage": "tool-call", "verb": v} for v in verbs]
        evts += [{"stage": "post-llm-event", "i": i} for i in range(turns)]
        return "\n".join(json.dumps(e) for e in evts)

    async def exec(self, cmd, env=None, timeout=0):
        c0 = cmd[0]
        if c0 in ("rm", "mkdir"):
            return (0, "", "")
        if c0 == "git":
            if "clone" in cmd:
                self.version = "none"  # fresh checkout each trial
                return (0, "", "")
            if "checkout" in cmd:
                return (0, "", "")
            if "diff" in cmd:
                return (0, f"diff --git a/ui.js b/ui.js\n+  // {self.version}\n", "")
            if "status" in cmd:
                return (0, "", "")
        if c0 == "cat":
            return (0, self._events(self.version), "")
        if c0 == "sh":
            script, rest = cmd[2], cmd[4:]
            if "printf" in script:  # skill install: [-, content, path]
                self.version = "v2" if "v2" in rest[0] else "v1"
                return (0, "", "")
            if 'exec "$@"' in script:
                if rest and rest[0] in ("claude", "hermes"):
                    self.harness_runs.append(rest[0])
                    return (0, json.dumps({"result": "done"}), "")
                return (0, "", "")  # acceptance command passes
        raise AssertionError(f"fake sandbox got an unexpected command: {cmd}")


def _judge(scores: dict, *, injection: bool = False):
    def call(system: str, user: str) -> str:
        return json.dumps(
            {"dimension_scores": dict(scores),
             "rationale": "deterministic test judge",
             "injection_suspected": injection}
        )
    return call


def _fixture(*, expects_activation: bool = True) -> sb.SkillBehaviourFixture:
    return sb.SkillBehaviourFixture(
        fixture_id="dropdowns",
        repo="/srv/fixtures/dropdowns.git",
        ref="deadbeef",
        task_prompt="Make the dropdown selectable.",
        acceptance_command=("true",),
        expects_activation=expects_activation,
        declared_workflow=DECLARED_WORKFLOW,
        max_turns=8,
        wall_clock_cap_s=120,
    )


# ---------------------------------------------------------------------------
# layer 2: the inspect adapter over the fake seam
# ---------------------------------------------------------------------------
async def test_run_skill_behaviour_upgrade_beats_incumbent_every_pair():
    fake = FakeSandbox()
    runner = resolve_runner("claude-code")
    outcome = await asyncio.to_thread(
        run_skill_behaviour,
        _fixture(), runner, 2, "dropdowns", SKILL_V1, SKILL_V2,
        _judge({"llm_judge_overall": 0.8}),
        k=5, exec_factory=lambda: fake.exec,
    )
    assert outcome.status == "completed", outcome.error
    assert len(outcome.trials) == 4  # 2 arms x 2 trials
    by_arm: dict[str, list[dict]] = {"baseline": [], "treatment": []}
    for tr in outcome.trials:
        by_arm[tr["arm"]].append(tr)
    # v2 conforms to the whole workflow, v1 only reads -> strictly higher every pair
    assert min(t["composite"] for t in by_arm["treatment"]) > max(
        t["composite"] for t in by_arm["baseline"]
    )
    # activation is a recall hit on both arms (both skills fire); v2 conforms fully
    tv2 = by_arm["treatment"][0]["dimension_values"]
    assert tv2["activation_correct"] == 1.0
    assert tv2["trace_conformance"] == pytest.approx(1.0)
    assert tv2["task_outcome"] == 1.0
    assert by_arm["baseline"][0]["dimension_values"]["trace_conformance"] == pytest.approx(1 / 3)
    assert fake.harness_runs.count("claude") == 4  # real harness cmd per sample


async def test_run_skill_behaviour_distractor_non_activation_scored():
    """A distractor case demands the skill stay silent; a skill that fires fails
    activation_correct. The fake always activates, so expects_activation=False
    drives activation_correct to 0."""
    fake = FakeSandbox()
    runner = resolve_runner("claude-code")
    outcome = await asyncio.to_thread(
        run_skill_behaviour,
        _fixture(expects_activation=False), runner, 1, "dropdowns", SKILL_V1, SKILL_V2,
        _judge({"llm_judge_overall": 0.8}),
        k=5, exec_factory=lambda: fake.exec,
    )
    assert outcome.status == "completed", outcome.error
    for tr in outcome.trials:
        assert tr["dimension_values"]["activation_correct"] == 0.0


async def test_run_skill_behaviour_judge_disabled_skips_the_slot():
    fake = FakeSandbox()
    runner = resolve_runner("claude-code")
    # a judge that would ERROR if called -- proves the slot is truly skipped
    def _boom(system, user):
        raise AssertionError("judge must not be called when judge_enabled=False")

    outcome = await asyncio.to_thread(
        run_skill_behaviour,
        _fixture(), runner, 1, "dropdowns", SKILL_V1, SKILL_V2, _boom,
        k=5, exec_factory=lambda: fake.exec, judge_enabled=False,
    )
    assert outcome.status == "completed", outcome.error
    tr = outcome.trials[0]
    assert "llm_judge_overall" not in tr["dimension_values"]
    assert tr["extras"]["judge_disabled"] is True
    assert tr["judge_samples"] == []
    # composite renormalised over the three rule-based slots (no 0.85 cap)
    assert tr["composite"] == pytest.approx(
        sb.compose(tr["dimension_values"], judge_disabled=True)
    )


async def test_run_skill_behaviour_injection_tripwire_errors():
    fake = FakeSandbox()
    runner = resolve_runner("claude-code")
    outcome = await asyncio.to_thread(
        run_skill_behaviour,
        _fixture(), runner, 1, "dropdowns", SKILL_V1, SKILL_V2,
        _judge({"llm_judge_overall": 1.0}, injection=True),
        k=5, exec_factory=lambda: fake.exec,
    )
    assert outcome.status == "errored"
    assert outcome.error == "judge-integrity"


# ---------------------------------------------------------------------------
# layer 3: the worker on the real RLS Postgres (also proves the 003 migration)
# ---------------------------------------------------------------------------
@pytest.fixture()
def cipher(tmp_path):
    return EnvelopeCipher("aa" * 32, "test-master-1", str(tmp_path / "keyring.json"))


async def _make_skill_run(engine, cipher) -> str:
    payload = {
        "harness": "claude-code",
        "skill_slug": "dropdowns",
        "skill_files_baseline": SKILL_V1,
        "skill_files_treatment": SKILL_V2,
        "model": "claude-sonnet-4",
        "task_fixture": {
            "fixture_id": "dropdowns",
            "repo": "/srv/fixtures/dropdowns.git",
            "ref": "deadbeef",
            "task_prompt": "Make the dropdown selectable.",
            "acceptance_command": ["true"],
            "expects_activation": True,
            "declared_workflow": list(DECLARED_WORKFLOW),
            "max_turns": 8,
            "wall_clock_cap_s": 120,
        },
    }
    ct, kid, sha = cipher.encrypt_payload(TENANT, payload)
    cid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    sv = versioning.suite_version(suites_base.suite_dir("skill-behaviour"))
    rv = versioning.rubric_version(suites_base.rubric_path("skill-behaviour"))
    async with tenant_txn(engine, TENANT) as conn:
        await conn.execute(
            text(
                "insert into wololo_evals.candidates "
                "(id, tenant_id, candidate_type, candidate_ref, proposed_by, "
                " baseline_ref, payload_ciphertext, payload_key_id, payload_sha256) "
                "values (:id,:t,'skill',:ref,'actor','dropdowns@v1',:ct,:kid,:sha)"
            ),
            {"id": cid, "t": TENANT, "ref": "dropdowns@v2", "ct": ct,
             "kid": kid, "sha": sha},
        )
        # This insert of eval_suite='skill-behaviour' fails the runs CHECK unless
        # migration 003 extended it -- the DB-registration proof.
        await conn.execute(
            text(
                "insert into wololo_evals.runs "
                "(id, tenant_id, candidate_id, eval_suite, status, suite_version, "
                " rubric_version, n_trials, eval_log_prefix) "
                "values (:id,:t,:c,'skill-behaviour','queued',:sv,:rv,5,:lp)"
            ),
            {"id": rid, "t": TENANT, "c": cid, "sv": sv, "rv": rv,
             "lp": f"evals/{TENANT}/{rid}/"},
        )
    return rid


async def test_worker_skill_behaviour_paired_verdict_and_rows(app_engine, cipher):
    rid = await _make_skill_run(app_engine, cipher)
    fake = FakeSandbox()
    async with tenant_txn(app_engine, TENANT) as conn:
        out = await execute_run(
            conn, rid, cipher=cipher, call_model=_judge({"llm_judge_overall": 0.8}),
            k=5, exec_factory=lambda: fake.exec,
        )
    assert out["status"] == "passed", out
    vs = out["verdict_stats"]
    assert vs["n"] == 5 and vs["wins"] == 5      # v2 beats v1 on every pair
    assert vs["uplift_pct"] > 0
    assert vs["judge_disabled"] is False

    async with tenant_txn(app_engine, TENANT) as conn:
        trials = (await conn.execute(text(
            "select arm, count(*) from wololo_evals.trials where run_id = :r group by arm"
        ), {"r": rid})).all()
        dims = (await conn.execute(text(
            "select dimension from wololo_evals.scores "
            "where run_id = :r and trial_index is null order by dimension"
        ), {"r": rid})).scalars().all()
        lb = (await conn.execute(text(
            "select harness, model from wololo_evals.leaderboard where run_id = :r"
        ), {"r": rid})).first()
        run = (await conn.execute(text(
            "select status from wololo_evals.runs where id = :r"
        ), {"r": rid})).first()

    assert {arm: n for arm, n in trials} == {"baseline": 5, "treatment": 5}
    # every rubric dimension (all four slots) has an aggregate score row
    assert dims == [
        "activation_correct", "llm_judge_overall", "task_outcome", "trace_conformance"
    ]
    assert lb.harness == "claude-code" and lb.model == "claude-sonnet-4"
    assert run.status == "passed"


# ---------------------------------------------------------------------------
# layer 4: the API create_run route accepts the new suite post-migration
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(app_engine, tmp_path):
    from wololo_evals.api import AppDeps, create_app
    from wololo_evals.auth import Verifier
    from wololo_evals.blobstore import FsBlobStore

    deps = AppDeps(
        engine=app_engine,
        verifier=Verifier(JWKS_PATH, "test-issuer"),
        cipher=EnvelopeCipher("aa" * 32, "test-master-1", str(tmp_path / "keyring.json")),
        blobstore=FsBlobStore(str(tmp_path / "blobs")),
    )
    transport = httpx.ASGITransport(app=create_app(deps))
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def _drain_job(worker_engine, run_id: str) -> None:
    """Retire the queued eval-run job this test left behind so another test's
    ``run_once`` does not grab it off the session-scoped, oldest-first queue
    (mirrors test_api.py::_drain_job)."""
    async with worker_engine.begin() as conn:
        await conn.execute(
            text("update wololo_evals.jobs set status = 'done' where idempotency_key = :k"),
            {"k": f"eval-run:{run_id}"},
        )


async def test_create_run_accepts_skill_behaviour(client, worker_engine):
    token = mint_token(TENANT, SUBMIT)
    headers = {"Authorization": f"Bearer {token}"}
    async with client:
        cand = await client.post(
            "/api/v1/candidates",
            json={
                "candidate_type": "skill",
                "candidate_ref": "dropdowns@v2",
                "baseline_ref": "dropdowns@v1",
                "payload": {"SKILL.md": "# dropdowns v2"},
            },
            headers=headers,
        )
        assert cand.status_code == 201, cand.text
        cid = cand.json()["id"]
        run = await client.post(
            f"/api/v1/candidates/{cid}/runs",
            json={"eval_suite": "skill-behaviour", "n_trials": 5},
            headers=headers,
        )
    assert run.status_code == 201, run.text
    body = run.json()
    assert body["eval_suite"] == "skill-behaviour"
    assert body["status"] == "queued"
    await _drain_job(worker_engine, body["id"])
