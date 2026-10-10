"""waza program grader: mutation kill rate of the suite the agent left in the workspace.

Q5 glue prototype. waza hands over the post-run workspace in WAZA_WORKSPACE_DIR.
The code under test is the pristine fixture copy next to this file, never the
workspace's quoting/, because the agent can write to the workspace. The suite is
the shipped tests plus the tests_generated/ the agent wrote, the same set the gate
scores in benchmarks/qe-skill/qe-target.json. The full report goes to kill_rate.json
inside the workspace (waza's program grader is binary, so the number needs a
sidecar). Exit codes: 0 when the kill rate meets the threshold, 1 when it is below the
threshold, 2 for an invalid run or a grader infrastructure failure. waza's program grader
treats any non-zero code as a fail, so the code is for the operator, not for waza.

Known limitation: this grader does not resist gaming. Agent test code runs as a process with this
user's file and process access. mutmut sets MUTANT_UNDER_TEST at runtime, so a test can read which
mutant is live and behave differently under it, and the kill rate can be inflated. File-name, symlink
and environment checks cannot stop that. Resisting it needs process isolation (a container or VM per
scoring run), which this grader does not have. Do not treat its numbers as tamper-proof.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "quoting-service"
# The gate's own settings: same test set and threshold as benchmarks/qe-skill/qe-target.json.
QE_TARGET = HERE.parents[3] / "benchmarks" / "qe-skill" / "qe-target.json"
# Variables the scorer's mutmut run may keep. Everything else, including tokens, is removed before it runs.
KEEP_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR")


def tree_digest(root: pathlib.Path) -> str:
    """Hash every file under root, so a change to the pristine fixture is detectable after a run."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and not any(part in ("__pycache__", ".pytest_cache") for part in p.parts):
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def main() -> int:
    try:
        from wololo_evals.scoring.mutation import MutationError, run_mutation_analysis

        threshold = json.loads(QE_TARGET.read_text(encoding="utf-8"))["qe"]["mutation_threshold"]
    except Exception as exc:  # a missing package or settings file is an infrastructure failure
        print(f"grader setup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    raw = os.environ.get("WAZA_WORKSPACE_DIR")
    if not raw:
        # An empty path resolves to the working directory, so an unset variable must fail, not fall back.
        print("WAZA_WORKSPACE_DIR is not set; refusing to grade the working directory", file=sys.stderr)
        return 2
    ws = pathlib.Path(raw)
    if not ws.is_dir():
        print(f"WAZA_WORKSPACE_DIR is not a directory: {ws}", file=sys.stderr)
        return 2
    tests = ws / "tests_generated"
    if not tests.is_dir():
        print(f"missing tests_generated/ under {ws}", file=sys.stderr)
        return 2
    # The agent writes into this workspace, so it can plant symlinks at any depth. The scorer copies
    # these trees with shutil.copytree, which follows links and would run an outside file as a test.
    if tests.is_symlink() or any(p.is_symlink() for p in tests.rglob("*")):
        print("tests_generated/ contains a symlink; refusing to score", file=sys.stderr)
        return 2
    # The scorer stages the shipped tests and the agent's tests into one directory. An agent file that
    # shares a shipped name overwrites the shipped test, and a conftest.py runs inside the scoring run
    # and can change every outcome. Refuse both, and anything that is not a plain test_*.py module.
    shipped = {p.name for p in (FIXTURE / "tests").rglob("*.py")}
    for p in tests.rglob("*"):
        if p.is_dir():
            continue
        if not p.is_file() or p.suffix != ".py" or not p.name.startswith("test_") or p.name in shipped:
            print(f"tests_generated/ has a file that may not be scored: {p.relative_to(ws)}", file=sys.stderr)
            return 2
    # Agent test code runs inside the scorer's mutmut run. Give it a throwaway copy of the code under test,
    # strip the environment of tokens, and hash the pristine fixture before and after, so a run that changed
    # the fixture is rejected. This detects tampering; it does not prevent it (see the limitation in the docstring).
    before = tree_digest(FIXTURE)
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="grader-"))
    try:
        shutil.copytree(FIXTURE / "quoting", scratch / "quoting")
        shutil.copytree(FIXTURE / "tests", scratch / "tests")
        for key in list(os.environ):
            if key not in KEEP_ENV:
                del os.environ[key]
        os.environ["HOME"] = str(scratch)
        # The scorer applies timeout_s to each mutmut call, and it can make two (run, then export-cicd-stats).
        # 2 x 400 s plus staging stays under waza's 900 s program-grader timeout (eval.yaml).
        report = run_mutation_analysis(scratch / "quoting", [scratch / "tests", tests], timeout_s=400)
    except MutationError as exc:
        print(f"mutation analysis failed: {exc}", file=sys.stderr)
        return 2
    finally:
        after = tree_digest(FIXTURE)
        shutil.rmtree(scratch, ignore_errors=True)
    if after != before:
        print("the pristine fixture changed during scoring; this run is invalid", file=sys.stderr)
        return 2
    payload = {**report.as_dict(), "kill_rate": report.kill_rate, "threshold": threshold}
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
        return 2
    print(f"kill_rate={report.kill_rate:.4f} executed={report.kill_rate_executed:.4f}")
    return 0 if report.kill_rate >= threshold else 1


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # anything unexpected is an invalid run, never a below-threshold result
        print(f"grader error: {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)
