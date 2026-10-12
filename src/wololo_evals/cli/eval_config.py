"""``eval.yaml``: the one file a skill repo keeps to say how it is evaluated.

Validated strictly: an unknown or malformed key is an :class:`EvalConfigError`
that names the key, because a typo (``max_cost``) silently falling back to a
default is how a cost cap goes missing. Paths resolve against the file's own
directory, not the process cwd. Pure stdlib plus PyYAML.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import yaml

from ..core import interfaces as ci

__all__ = ["EvalConfig", "EvalConfigError", "load_eval_config", "resolve_benchmark"]

_KEYS = ("asset", "benchmark", "engine", "model", "max_cost_usd", "thresholds")
_REQUIRED = ("asset", "benchmark", "engine", "model")
_THRESHOLD_KEYS = ("pass", "max_regression")

#: Benchmarks shipped in this repository, resolved by name from a source
#: checkout (the action installs from one).
_BUILTIN_DIR = pathlib.Path(__file__).resolve().parents[3] / "benchmarks"


class EvalConfigError(ValueError):
    """``eval.yaml`` is unusable; the message names the offending key."""


@dataclass(frozen=True)
class EvalConfig:
    path: pathlib.Path
    asset: pathlib.Path
    benchmark: str
    engine: str
    model: str
    max_cost_usd: Optional[float]
    #: Minimum mean benchmark score the skill arm must reach (0 disables it).
    pass_score: float
    #: Tolerated drop against the no-skill arm (the gate's ``max_regression``).
    max_regression: float

    @property
    def base_dir(self) -> pathlib.Path:
        return self.path.parent


def _fail(path: pathlib.Path, key: str, problem: str) -> EvalConfigError:
    return EvalConfigError(f"{path.name}: `{key}` {problem}")


def _number(path: pathlib.Path, key: str, value: Any, *, low: float, strict: bool) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if ok and (value > low if strict else value >= low):
        return float(value)
    bound = f"greater than {low:g}" if strict else f"at least {low:g}"
    raise _fail(path, key, f"must be a number {bound}, got {value!r}")


def _text(path: pathlib.Path, key: str, value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise _fail(path, key, f"must be a non-empty string, got {value!r}")


def load_eval_config(path: str | pathlib.Path) -> EvalConfig:
    p = pathlib.Path(path).resolve()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvalConfigError(f"{p.name}: cannot read {p}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise EvalConfigError(f"{p.name}: not valid YAML: {exc}") from exc
    if not isinstance(data, Mapping):
        raise EvalConfigError(f"{p.name}: must be a mapping of keys, got {type(data).__name__}")

    for key in data:
        if key not in _KEYS:
            raise _fail(p, str(key), f"is not a known key (known: {', '.join(_KEYS)})")
    for key in _REQUIRED:
        if key not in data:
            raise _fail(p, key, "is required")

    asset = (p.parent / _text(p, "asset", data["asset"])).resolve()
    if not asset.is_dir():
        raise _fail(p, "asset", f"must be the skill directory, no directory at {asset}")

    engine = _text(p, "engine", data["engine"])
    if engine not in ci.ENGINE_IDS:
        raise _fail(p, "engine", f"must be one of {', '.join(ci.ENGINE_IDS)}, got {engine!r}")

    cost = data.get("max_cost_usd")
    if "max_cost_usd" in data:
        cost = _number(p, "max_cost_usd", cost, low=0, strict=True)

    thresholds = data.get("thresholds", {})
    if not isinstance(thresholds, Mapping):
        raise _fail(p, "thresholds", f"must be a mapping, got {thresholds!r}")
    for key in thresholds:
        if key not in _THRESHOLD_KEYS:
            raise _fail(
                p, f"thresholds.{key}",
                f"is not a known key (known: {', '.join(_THRESHOLD_KEYS)})",
            )
    pass_score = _number(p, "thresholds.pass", thresholds.get("pass", 0), low=0, strict=False)
    if pass_score > 1:
        raise _fail(p, "thresholds.pass", f"must be between 0 and 1, got {pass_score:g}")
    max_regression = _number(
        p, "thresholds.max_regression", thresholds.get("max_regression", 0), low=0, strict=False
    )

    cfg = EvalConfig(
        path=p,
        asset=asset,
        benchmark=_text(p, "benchmark", data["benchmark"]),
        engine=engine,
        model=_text(p, "model", data["model"]),
        max_cost_usd=cost,
        pass_score=pass_score,
        max_regression=max_regression,
    )
    resolve_benchmark(cfg)  # fail on a bad `benchmark` now, not mid-run
    return cfg


def resolve_benchmark(cfg: EvalConfig, override: Optional[str] = None) -> pathlib.Path:
    """A benchmark path next to the config, else a built-in benchmark name."""
    value = override or cfg.benchmark
    for candidate in ((cfg.base_dir / value), (_BUILTIN_DIR / value)):
        if (candidate / "benchmark.json").is_file():
            return candidate.resolve()
    builtin = sorted(p.parent.name for p in _BUILTIN_DIR.glob("*/benchmark.json"))
    raise EvalConfigError(
        f"{cfg.path.name}: `benchmark` {value!r} is neither a benchmark directory "
        f"(with benchmark.json) next to the config nor a built-in ({', '.join(builtin)})"
    )
