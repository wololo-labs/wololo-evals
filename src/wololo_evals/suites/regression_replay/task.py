"""regression-replay suite (SPEC S5.6): replay ainb-reflect-memory's
corrections corpus and score whether a candidate skill/prompt would avoid
needing the same human correction again.

Each raw correction row (``correction_raw`` kind in
``core/contracts/learning.py``, owned by ainb-reflect-memory) becomes one
Inspect sample. The correction's ``user_message`` and preceding transcript
``context`` reconstruct the situation; ``signal_class`` + ``matched_signals``
give the judge the exact failure to check for. Single-arm (k=5 median per
dimension); statistical n is the corpus size, so a verdict needs >= 5
corrections (DDL floor on ``n_trials``).

The corpus is TRUSTED reference material (it is our own reflect-memory export,
not attacker-controllable). It is woven into the judge's system-message rubric,
never into the nonce-fenced user message; only the candidate content is fenced
(SPEC S8).
"""
from __future__ import annotations

from dataclasses import dataclass, field

SUITE = "regression-replay"
ARM = "single"


@dataclass(frozen=True)
class CorrectionSample:
    """One replayed human correction from the corrections corpus."""

    correction_id: str
    user_message: str              # what the human said when they corrected the agent
    signal_class: str              # the failure class (e.g. "unrequested-abstraction")
    matched_signals: tuple[str, ...] = ()   # concrete signals matched in the original row
    context: str = ""              # preceding transcript context, optional


def reference_block(sample: CorrectionSample) -> str:
    """The TRUSTED reference woven into the judge's system message: the exact
    failure this replayed correction is checking the candidate against."""
    signals = ", ".join(sample.matched_signals) if sample.matched_signals else "(none recorded)"
    lines = [
        "Replayed human correction to check the candidate against:",
        f"- signal_class: {sample.signal_class}",
        f"- matched_signals: {signals}",
        f"- the human's correction message: {' '.join(sample.user_message.split())}",
    ]
    if sample.context.strip():
        lines.append(f"- preceding context: {' '.join(sample.context.split())}")
    return "\n".join(lines)


def rubric_text_with_reference(base_rubric_text: str, sample: CorrectionSample) -> str:
    """Per-sample system-message body: the pinned rubric plus this sample's
    trusted failure reference. The candidate content stays nonce-fenced."""
    return base_rubric_text + "\n\n" + reference_block(sample)


def corpus_from_rows(rows: list[dict]) -> list[CorrectionSample]:
    """Build samples from raw ``correction_raw``-shaped dicts (fail-closed on a
    row missing the fields the judge needs to know what to check for)."""
    corpus: list[CorrectionSample] = []
    for row in rows:
        try:
            corpus.append(
                CorrectionSample(
                    correction_id=str(row["correction_id"]),
                    user_message=str(row["user_message"]),
                    signal_class=str(row["signal_class"]),
                    matched_signals=tuple(row.get("matched_signals", ()) or ()),
                    context=str(row.get("context", "")),
                )
            )
        except KeyError as exc:
            raise ValueError(f"correction row missing required field: {exc}") from exc
    return corpus
