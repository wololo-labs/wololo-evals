"""waza program grader: mutation kill rate of the suite the agent left in the workspace.

Q5 glue prototype. waza hands over the post-run workspace in WAZA_WORKSPACE_DIR.
The code under test is the pristine fixture copy next to this file, never the
workspace's quoting/, because the agent can write to the workspace. The suite is
the shipped tests plus the tests_generated/ the agent wrote, the same set the gate
scores in benchmarks/qe-skill/qe-target.json. The full report goes to kill_rate.json
inside the workspace (waza's program grader is binary, so the number needs a
sidecar), and the grader exits 0 when the kill rate meets the threshold.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

from wololo_evals.scoring.mutation import MutationError, run_mutation_analysis

HERE = pathlib.Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "quoting-service"
# The gate's own settings: same test set and threshold as benchmarks/qe-skill/qe-target.json.
QE_TARGET = HERE.parents[3] / "benchmarks" / "qe-skill" / "qe-target.json"
THRESHOLD = json.loads(QE_TARGET.read_text(encoding="utf-8"))["qe"]["mutation_threshold"]


def main() -> int:
    ws = pathlib.Path(os.environ.get("WAZA_WORKSPACE_DIR", ""))
    target = FIXTURE / "quoting"
    tests = ws / "tests_generated"
    if not tests.is_dir():
        print(f"missing tests_generated/ under {ws}", file=sys.stderr)
        return 1
    # The agent writes into this workspace, so it can plant symlinks at any depth. The scorer copies
    # these trees with shutil.copytree, which follows links and would run an outside file as a test.
    if tests.is_symlink() or any(p.is_symlink() for p in tests.rglob("*")):
        print("tests_generated/ contains a symlink; refusing to score", file=sys.stderr)
        return 1
    try:
        report = run_mutation_analysis(target, [FIXTURE / "tests", tests], timeout_s=800)
    except MutationError as exc:
        print(f"mutation analysis failed: {exc}", file=sys.stderr)
        return 1
    payload = {**report.as_dict(), "kill_rate": report.kill_rate, "threshold": THRESHOLD}
    # Exclusive create, then rename over the destination. A symlink the agent planted at either
    # name is replaced, never followed, so the write cannot land outside the workspace.
    tmp = ws / ".kill_rate.json.tmp"
    try:
        tmp.unlink(missing_ok=True)  # removes a planted link itself, never its target
        with open(tmp, "x", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, ws / "kill_rate.json")
    except OSError as exc:
        print(f"could not write kill_rate.json: {exc}", file=sys.stderr)
        return 1
    print(f"kill_rate={report.kill_rate:.4f} executed={report.kill_rate_executed:.4f}")
    return 0 if report.kill_rate >= THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(main())
