"""``bench``: run one benchmark as a single combo and write a §15.3
``scorecard.json`` (open, Apache-2.0).

The first engine/model in the config is used unless ``--engine``/``--model``
selects another. The whole per-case/seed lifecycle is shared with ``matrix``
(``matrix._execute_combo``) so bench and matrix compose scores identically.
"""
from __future__ import annotations

import pathlib
from typing import Optional

from ..core import interfaces as ci
from . import artefact_writer as aw
from . import cases_compile
from . import matrix as _matrix
from .config import MatrixConfig, PromptEntry, load_benchmark

__all__ = ["run_bench", "select_combo"]


def select_combo(
    config: MatrixConfig, *, engine: Optional[str] = None, model: Optional[str] = None
) -> _matrix.Combo:
    """The single combo bench runs: first model (or the one matching filters)
    crossed with the first prompt and first asset under test."""
    candidates = list(config.models)
    if engine is not None:
        candidates = [m for m in candidates if m.engine == engine]
    if model is not None:
        candidates = [m for m in candidates if m.id == model or m.model == model]
    if not candidates:
        raise ValueError(f"no model matches engine={engine!r} model={model!r}")
    m = candidates[0]
    prompt = config.prompts[0] if config.prompts else PromptEntry(id="stock")
    asset = config.assets[0] if config.assets else None
    parts = [m.id, prompt.id] + ([asset.id] if asset else [])
    return _matrix.Combo(
        combo_id="__".join(parts),
        engine_id=m.engine,
        model_id=m.id,
        prompt_id=prompt.id,
        role="treatment",
        asset=asset,
    )


def run_bench(
    config: MatrixConfig,
    adapter_factory: _matrix.AdapterFactory,
    grader: _matrix.Grader,
    *,
    timestamp: str,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    out: Optional[str | pathlib.Path] = None,
    normaliser_factory: Optional[_matrix.NormaliserFactory] = None,
    seeds: Optional[int] = None,
) -> pathlib.Path:
    """Run one combo and write ``scorecard.json`` under ``out`` (or the config's
    ``output_dir``). Returns the scorecard path."""
    benchmark = load_benchmark(config.benchmark_dir)
    _matrix._validate_engines(config, benchmark)
    all_cases = cases_compile.load_cases(config.benchmark_dir / "cases.jsonl")
    combo = select_combo(config, engine=engine, model=model)

    out_dir = pathlib.Path(out) if out is not None else config.output_path
    out_dir.mkdir(parents=True, exist_ok=True)
    events = aw.EventLog()
    engine_versions: dict[str, str] = {}

    _matrix._execute_combo(
        combo,
        all_cases,
        seeds if seeds is not None else config.seeds,
        out_dir,
        benchmark=benchmark,
        base_dir=config.base_dir,
        adapter_factory=adapter_factory,
        grader=grader,
        normaliser_factory=normaliser_factory,
        events=events,
        timestamp=timestamp,
        cap=config.max_cost_usd,
        cumulative=0.0,
        smoke=False,
        engine_versions=engine_versions,
    )
    return ci.artefacts.scorecard_path(out_dir)
