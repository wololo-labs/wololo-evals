"""repo-eval fixture generation (SPEC S5.3): walk a repo's real seeded commit
history and turn a qualifying commit into a :class:`RepoEvalFixture`.

Runs on the host at run-creation time (NOT inside the trial sandbox): it only
reads the local git repo with ``git`` plumbing, never mutates it. A commit
qualifies when it resolves a single, isolable unit of work, matched by the
SPEC's heuristics:

- the message has a conventional-commit ``fix:`` or ``feat:`` prefix,
- it touches 1..8 files,
- it includes a paired test change (a changed path that looks like a test).

The historical diff is used only as a scale reference for churn_penalty; it is
redacted from the sample (the harness checks out the PARENT commit and sees
the commit message alone).
"""
from __future__ import annotations

import re
import subprocess
from typing import Iterator, Optional, Sequence

from .task import RepoEvalFixture

_CONVENTIONAL = re.compile(r"^(fix|feat)(\([^)]*\))?!?:", re.IGNORECASE)
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)test_|_test\.|\.test\.|\.spec\.")


def _git(repo_path: str, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", repo_path, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout


def _looks_like_test(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def changed_files(repo_path: str, sha: str) -> list[str]:
    out = _git(repo_path, "show", "--no-renames", "--name-only", "--format=", sha)
    return [line.strip() for line in out.splitlines() if line.strip()]


def historical_lines_changed(repo_path: str, sha: str) -> int:
    """Added + removed lines across the commit, from ``git show --numstat``.
    Binary files (``-\t-``) contribute nothing."""
    out = _git(repo_path, "show", "--no-renames", "--numstat", "--format=", sha)
    total = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        added, removed = parts[0], parts[1]
        if added.isdigit():
            total += int(added)
        if removed.isdigit():
            total += int(removed)
    return total


def qualifies(repo_path: str, sha: str) -> bool:
    subject = _git(repo_path, "show", "-s", "--format=%s", sha).strip()
    if not _CONVENTIONAL.match(subject):
        return False
    files = changed_files(repo_path, sha)
    if not (1 <= len(files) <= 8):
        return False
    return any(_looks_like_test(f) for f in files)


def qualifying_commits(repo_path: str, *, ref: str = "HEAD", limit: int = 500) -> Iterator[str]:
    log = _git(repo_path, "log", "--no-merges", "--format=%H", f"-n{limit}", ref)
    for sha in log.splitlines():
        sha = sha.strip()
        if sha and qualifies(repo_path, sha):
            yield sha


def _infer_acceptance_command(files: Sequence[str]) -> tuple[str, ...]:
    tests = [f for f in files if _looks_like_test(f)]
    py_tests = [f for f in tests if f.endswith(".py")]
    if py_tests:
        return ("python", "-m", "pytest", "-q", *py_tests)
    raise ValueError(
        "cannot infer acceptance command for non-python tests "
        f"({tests!r}); pass acceptance_command explicitly"
    )


def build_fixture(
    repo_path: str,
    sha: str,
    *,
    repo_clone_source: Optional[str] = None,
    acceptance_command: Optional[Sequence[str]] = None,
    max_turns: int = 20,
    wall_clock_cap_s: int = 900,
) -> RepoEvalFixture:
    """Build a :class:`RepoEvalFixture` for one commit. ``repo_clone_source``
    is the path/URL the sandbox will clone (defaults to ``repo_path``); the
    sandbox always checks out the PARENT so the historical diff is redacted."""
    subject = _git(repo_path, "show", "-s", "--format=%s%n%n%b", sha).strip()
    files = changed_files(repo_path, sha)
    cmd = tuple(acceptance_command) if acceptance_command else _infer_acceptance_command(files)
    return RepoEvalFixture(
        fixture_id=f"repo-eval-{sha[:12]}",
        repo=repo_clone_source or repo_path,
        parent_ref=f"{sha}^",
        commit_ref=sha,
        task_prompt=subject,
        acceptance_command=cmd,
        historical_lines_changed=historical_lines_changed(repo_path, sha),
        max_turns=max_turns,
        wall_clock_cap_s=wall_clock_cap_s,
    )


def generate(
    repo_path: str,
    *,
    ref: str = "HEAD",
    repo_clone_source: Optional[str] = None,
    acceptance_command: Optional[Sequence[str]] = None,
) -> RepoEvalFixture:
    """Pick the most recent qualifying commit and build its fixture. Raises if
    the repo has no commit matching the isolable-unit-of-work heuristics."""
    for sha in qualifying_commits(repo_path, ref=ref):
        try:
            return build_fixture(
                repo_path,
                sha,
                repo_clone_source=repo_clone_source,
                acceptance_command=acceptance_command,
            )
        except ValueError:
            continue  # non-python test with no explicit command; keep walking
    raise ValueError(f"no qualifying repo-eval commit in {repo_path!r} at {ref}")
