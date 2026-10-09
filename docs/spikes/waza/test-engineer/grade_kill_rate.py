"""waza program grader: mutation kill rate of the suite the agent left in the workspace.

Q5 glue prototype. waza hands over the post-run workspace in WAZA_WORKSPACE_DIR.
The grader scores tests_generated/ against quoting/ with the wololo-evals mutation
scorer, writes the full report to kill_rate.json inside the workspace (waza's
program grader is binary, so the number needs a sidecar), and exits 0 when the
kill rate meets the threshold in qe-target.json.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

from wololo_evals.scoring.mutation import MutationError, run_mutation_analysis

THRESHOLD = 0.24  # benchmarks/qe-skill/qe-target.json: mutation_threshold


def main() -> int:
    ws = pathlib.Path(os.environ.get("WAZA_WORKSPACE_DIR", ""))
    target = ws / "quoting"
    tests = ws / "tests_generated"
    if not target.is_dir() or not tests.is_dir():
        print(f"missing quoting/ or tests_generated/ under {ws}", file=sys.stderr)
        return 1
    # The agent writes into this workspace, so it can plant symlinks at any depth. The scorer copies
    # these trees with shutil.copytree, which follows links and would run an outside file as a test.
    for root in (target, tests):
        if root.is_symlink() or any(p.is_symlink() for p in root.rglob("*")):
            print(f"{root.name}/ contains a symlink; refusing to score", file=sys.stderr)
            return 1
    try:
        report = run_mutation_analysis(target, [tests], timeout_s=800)
    except MutationError as exc:
        print(f"mutation analysis failed: {exc}", file=sys.stderr)
        return 1
    payload = {**report.as_dict(), "kill_rate": report.kill_rate, "threshold": THRESHOLD}
    # Exclusive create, then rename over the destination. A symlink the agent planted at either
    # name is replaced, never followed, so the write cannot land outside the workspace.
    tmp = ws / ".kill_rate.json.tmp"
    tmp.unlink(missing_ok=True)  # removes a planted link itself, never its target
    try:
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
