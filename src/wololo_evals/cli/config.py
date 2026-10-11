"""Matrix + benchmark config loading for the file-first CLI (open, Apache-2.0).

Parses the §13 matrix TOML and §10 ``benchmark.json`` into plain frozen
records the rest of ``cli/`` builds ``RunSpec`` combos from. Pure stdlib
(``tomllib``/``json``) — imports no database and no engine SDK; the only
package import is ``core.interfaces`` for the shared vocabulary.
"""
from __future__ import annotations

import json
import pathlib
import tomllib
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from ..core import interfaces as ci

__all__ = [
    "ModelEntry",
    "PromptEntry",
    "BaselineEntry",
    "AssetEntry",
    "MatrixConfig",
    "Benchmark",
    "load_matrix_config",
    "load_benchmark",
]


@dataclass(frozen=True)
class ModelEntry:
    """One ``[[models]]`` row: an ``engine`` bound to a concrete ``model``."""

    id: str
    engine: str
    model: str

    def __post_init__(self) -> None:
        if self.engine not in ci.ENGINE_IDS:
            raise ValueError(f"model {self.id!r} engine {self.engine!r} not in {ci.ENGINE_IDS}")


@dataclass(frozen=True)
class PromptEntry:
    """One ``[[prompts]]`` row (stock/system-prompt variant under test)."""

    id: str


@dataclass(frozen=True)
class BaselineEntry:
    """One ``[[baselines]]`` control row. ``inline`` replaces the asset/prompt
    contribution entirely (§13); baseline combos never mount the asset."""

    id: str
    inline: Optional[str] = None


@dataclass(frozen=True)
class AssetEntry:
    """One ``[[assets]]`` row, or the singular ``[workflow]`` asset when no
    ``[[assets]]`` block is present. ``pair_role`` marks old/new for a
    version-pair evaluation (§18.3)."""

    id: str
    kind: str
    path: Optional[str] = None
    git_ref: Optional[str] = None
    inline: Optional[str] = None
    pair_role: Optional[str] = None


@dataclass(frozen=True)
class MatrixConfig:
    """Parsed §13 matrix config. ``base_dir`` is the config file's parent —
    ``benchmark``/``output_dir``/``fixtures_dir`` resolve relative to it."""

    schema_version: str
    benchmark: str
    output_dir: str
    base_dir: pathlib.Path
    seeds: int = 1
    concurrency: int = 1
    max_cost_usd: Optional[float] = None
    baseline_run: Optional[str] = None
    fixtures_dir: Optional[str] = None
    #: Slug the asset under evaluation actually ships, when it differs from the
    #: benchmark's own ``expected_skill``. One benchmark asks one question; two
    #: assets answering it are entitled to different names.
    expected_skill: Optional[str] = None
    #: Text prepended to every case prompt. Declares what an arm measures: a
    #: preamble naming the asset scores its contents, no preamble scores
    #: discovery too.
    prompt_preamble: Optional[str] = None
    #: Run only these case ids. Lets one casebook be calibrated a few cases at a
    #: time, for instance a control run on cases that have not been measured
    #: yet, without copying the casebook.
    only_cases: Sequence[str] = ()
    models: Sequence[ModelEntry] = ()
    prompts: Sequence[PromptEntry] = ()
    baselines: Sequence[BaselineEntry] = ()
    assets: Sequence[AssetEntry] = ()
    engine_ranges: Mapping[str, str] = field(default_factory=dict)
    workflow: Mapping[str, object] = field(default_factory=dict)
    raw: Mapping[str, object] = field(default_factory=dict)

    @property
    def benchmark_dir(self) -> pathlib.Path:
        return (self.base_dir / self.benchmark).resolve()

    @property
    def output_path(self) -> pathlib.Path:
        return (self.base_dir / self.output_dir).resolve()

    @property
    def matrix_id(self) -> str:
        """Matrix id is the config filename stem (§15.1)."""
        stem = self.raw.get("__config_stem__")
        return str(stem) if stem else "matrix"


def _asset_entries(data: Mapping[str, object]) -> list[AssetEntry]:
    """``[[assets]]`` when present, else the singular ``[workflow]`` block."""
    rows = data.get("assets")
    if isinstance(rows, list) and rows:
        out: list[AssetEntry] = []
        for row in rows:
            out.append(
                AssetEntry(
                    id=str(row["id"]),
                    kind=str(row.get("kind", "workflow_md")),
                    path=row.get("path"),
                    git_ref=row.get("git_ref"),
                    inline=row.get("inline"),
                    pair_role=row.get("pair_role"),
                )
            )
        return out
    wf = data.get("workflow")
    if isinstance(wf, Mapping) and wf.get("id"):
        return [
            AssetEntry(
                id=str(wf["id"]),
                kind=str(wf.get("kind", "workflow_md")),
                path=wf.get("path") or (None if wf.get("ref") == "local" else None),
                git_ref=wf.get("git_ref"),
                inline=wf.get("inline"),
            )
        ]
    return []


def load_matrix_config(path: str | pathlib.Path) -> MatrixConfig:
    """Load and validate a §13 matrix config TOML."""
    p = pathlib.Path(path)
    with p.open("rb") as fh:
        data = tomllib.load(fh)

    models = [
        ModelEntry(id=str(m["id"]), engine=str(m["engine"]), model=str(m["model"]))
        for m in data.get("models", [])
    ]
    prompts = [PromptEntry(id=str(pr["id"])) for pr in data.get("prompts", [])]
    baselines = [
        BaselineEntry(id=str(b["id"]), inline=b.get("inline"))
        for b in data.get("baselines", [])
    ]
    engine_ranges = {
        str(eid): str(cfg.get("version_range", ""))
        for eid, cfg in (data.get("engines", {}) or {}).items()
    }
    raw = dict(data)
    raw["__config_stem__"] = p.stem

    if not models:
        raise ValueError(f"matrix config {p} declares no [[models]]")

    seeds = int(data.get("seeds", 1))
    if seeds < 1:
        raise ValueError(f"matrix config {p}: seeds must be at least 1, got {seeds}")

    return MatrixConfig(
        schema_version=str(data.get("schema_version", "2.0.0")),
        benchmark=str(data["benchmark"]),
        output_dir=str(data.get("output_dir", "out")),
        base_dir=p.parent.resolve(),
        seeds=seeds,
        concurrency=int(data.get("concurrency", 1)),
        max_cost_usd=(
            float(data["max_cost_usd"]) if data.get("max_cost_usd") is not None else None
        ),
        baseline_run=data.get("baseline_run"),
        fixtures_dir=data.get("fixtures_dir"),
        expected_skill=data.get("expected_skill"),
        prompt_preamble=data.get("prompt_preamble"),
        only_cases=tuple(str(x) for x in data.get("only_cases", ())),
        models=tuple(models),
        prompts=tuple(prompts),
        baselines=tuple(baselines),
        assets=tuple(_asset_entries(data)),
        engine_ranges=engine_ranges,
        workflow=dict(data.get("workflow", {})),
        raw=raw,
    )


@dataclass(frozen=True)
class Benchmark:
    """Parsed §10 ``benchmark.json``. ``evaluation_type`` selects the §16.1
    gate family; ``pass_threshold`` is the §16.2 case-pass bar."""

    schema_version: str
    id: str
    evaluation_type: str
    description: str = ""
    pass_threshold: float = 0.7
    golden_set: Optional[str] = None
    engines: Sequence[str] = ()
    dir: Optional[pathlib.Path] = None

    def __post_init__(self) -> None:
        if self.evaluation_type not in ci.EVALUATION_TYPES:
            raise ValueError(
                f"benchmark {self.id!r} evaluation_type {self.evaluation_type!r} "
                f"not in {ci.EVALUATION_TYPES}"
            )


def load_benchmark(benchmark_dir: str | pathlib.Path) -> Benchmark:
    """Load ``<benchmark_dir>/benchmark.json``."""
    d = pathlib.Path(benchmark_dir)
    with (d / "benchmark.json").open("rb") as fh:
        data = json.load(fh)
    return Benchmark(
        schema_version=str(data.get("schema_version", "1.0.0")),
        id=str(data["id"]),
        evaluation_type=str(data["evaluation_type"]),
        description=str(data.get("description", "")),
        pass_threshold=float(data.get("pass_threshold", 0.7)),
        golden_set=data.get("golden_set"),
        engines=tuple(data.get("engines", ())),
        dir=d.resolve(),
    )
