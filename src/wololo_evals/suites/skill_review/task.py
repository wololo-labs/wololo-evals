"""skill-review suite (SPEC S5.1): static SKILL.md scoring, no execution.

The single Inspect sample is the skill's rendered content; the solver is a
no-op (the sample already contains everything to grade); the scorer is the
injection-hardened judge over the four rubric dimensions. Single-arm suite:
the SPEC S7 knife-edge visibility comes from k=5 judge-sample variance."""
from __future__ import annotations

SUITE = "skill-review"
ARM = "single"


def render_candidate(payload: dict) -> str:
    """Render a candidate skill payload into the judged content.

    ``payload`` carries ``skill_md`` (the SKILL.md text) plus optional
    ``files`` ({relative path: content}) for scripts/references. Everything
    here is attacker-controllable: the judge wraps it in the nonce fence."""
    if "skill_md" not in payload or not str(payload["skill_md"]).strip():
        raise ValueError("skill candidate payload requires non-empty 'skill_md'")
    parts = ["## SKILL.md\n", str(payload["skill_md"])]
    for path, content in sorted(payload.get("files", {}).items()):
        parts.append(f"\n\n## supporting file: {path}\n")
        parts.append(str(content))
    return "".join(parts)
