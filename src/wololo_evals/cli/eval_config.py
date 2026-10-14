"""The ``eval:`` block of ``apm.yml``: how a skill repo says it is evaluated.

The legacy ``eval.yaml`` (the same keys at the top level) still loads, with a
deprecation notice on stderr. When ``apm.yml`` carries an ``eval:`` block it
wins over a sibling ``eval.yaml``.

Validated strictly: an unknown or malformed key is an :class:`EvalConfigError`
that names the key, because a typo (``max_cost``) silently falling back to a
default is how a cost cap goes missing. Paths resolve against the file's own
directory, not the process cwd. Pure stdlib plus PyYAML.
"""
from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import yaml

from ..core import interfaces as ci

__all__ = ["EvalConfig", "EvalConfigError", "load_eval_config", "resolve_benchmark"]

_KEYS = ("asset", "benchmark", "engine", "model", "max_cost_usd", "thresholds", "report")
_REQUIRED = ("asset", "benchmark", "engine", "model")
_APM_NAMES = ("apm.yml", "apm.yaml")
_LEGACY_NAME = "eval.yaml"
_DEFAULT_REPORT = "eval-gate-report.json"
_THRESHOLD_KEYS = ("pass", "max_regression")

#: Benchmarks shipped in this repository, resolved by name from a source
#: checkout (the action installs from one).
_BUILTIN_DIR = pathlib.Path(__file__).resolve().parents[3] / "benchmarks"


class EvalConfigError(ValueError):
    """The eval config is unusable; the message names the offending key."""


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
    #: The gate report (its ``.md`` twin is written beside it); default beside the config.
    report: Optional[pathlib.Path] = None

    @property
    def report_json(self) -> pathlib.Path:
        return self.report or self.base_dir / _DEFAULT_REPORT

    @property
    def skill_slug(self) -> str:
        """The skill's ``name`` from its SKILL.md frontmatter, else its directory name."""
        try:
            text = (self.asset / "SKILL.md").read_text(encoding="utf-8")
            front = yaml.safe_load(text.split("---")[1]) if text.startswith("---") else {}
            return str(front["name"])
        except (OSError, IndexError, KeyError, TypeError, yaml.YAMLError):
            return self.asset.name

    @property
    def base_dir(self) -> pathlib.Path:
        return self.path.parent


def _fail(label: str, key: str, problem: str) -> EvalConfigError:
    return EvalConfigError(f"{label}: `{key}` {problem}")


def _number(label: str, key: str, value: Any, *, low: float, strict: bool) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if ok and (value > low if strict else value >= low):
        return float(value)
    bound = f"greater than {low:g}" if strict else f"at least {low:g}"
    raise _fail(label, key, f"must be a number {bound}, got {value!r}")


def _text(label: str, key: str, value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise _fail(label, key, f"must be a non-empty string, got {value!r}")


def _read_yaml(p: pathlib.Path) -> Any:
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvalConfigError(f"{p.name}: cannot read {p}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise EvalConfigError(f"{p.name}: not valid YAML: {exc}") from exc


def _notice(message: str) -> None:
    print(f"notice: {message}", file=sys.stderr)


_MISSING: Any = object()


def _eval_block(apm: pathlib.Path) -> Any:
    """The ``eval:`` value of an apm.yml, or ``_MISSING`` when the key is absent."""
    data = _read_yaml(apm)
    if not isinstance(data, Mapping):
        raise EvalConfigError(f"{apm.name}: must be a mapping of keys, got {type(data).__name__}")
    return data.get("eval", _MISSING)


def _locate(p: pathlib.Path) -> tuple[pathlib.Path, Any, str]:
    """Pick the config source for ``p``: (file, mapping, error label).

    ``apm.yml`` is the home of the config. A bare ``eval.yaml`` still works
    unless a sibling ``apm.yml`` declares ``eval:``, which wins.
    """
    legacy = p.parent / _LEGACY_NAME
    if p.name in _APM_NAMES:
        block = _eval_block(p)
        if block is not _MISSING:
            if legacy.is_file():
                _notice(f"{p.name} `eval:` and {_LEGACY_NAME} both exist; using {p.name}, remove {_LEGACY_NAME}")
            return p, block, f"{p.name} `eval:`"
        if legacy.is_file():
            _notice(f"{p.name} has no `eval:` block; using {_LEGACY_NAME}, which is deprecated: move it under `eval:`")
            return legacy, _read_yaml(legacy), legacy.name
        raise EvalConfigError(f"{p.name}: has no `eval:` block")
    for name in _APM_NAMES:
        apm = p.parent / name
        if apm.is_file():
            try:
                block = _eval_block(apm)
            except EvalConfigError:
                continue  # an unrelated, unreadable manifest must not hide a working eval.yaml
            if block is not _MISSING:
                _notice(f"{apm.name} `eval:` and {p.name} both exist; using {apm.name}, remove {p.name}")
                return apm, block, f"{apm.name} `eval:`"
    _notice(f"{p.name} is deprecated: move its keys under `eval:` in apm.yml")
    return p, _read_yaml(p), p.name


def _default_asset(base: pathlib.Path) -> Optional[str]:
    """The pack's only skill directory, when there is exactly one."""
    found = sorted(d for d in (base / ".apm" / "skills").glob("*") if (d / "SKILL.md").is_file())
    return str(found[0].relative_to(base)) if len(found) == 1 else None


def load_eval_config(path: str | pathlib.Path) -> EvalConfig:
    """Load the eval config for ``path``: an ``apm.yml`` (its ``eval:`` key) or a legacy ``eval.yaml``."""
    p = pathlib.Path(path).resolve()
    src, data, label = _locate(p)
    if not isinstance(data, Mapping):
        raise EvalConfigError(f"{label}: must be a mapping of keys, got {type(data).__name__}")
    p = src

    for key in data:
        if key not in _KEYS:
            raise _fail(label, str(key), f"is not a known key (known: {', '.join(_KEYS)})")
    if "asset" not in data and p.name in _APM_NAMES:
        default = _default_asset(p.parent)
        if default:
            data = {**data, "asset": default}
    for key in _REQUIRED:
        if key not in data:
            raise _fail(label, key, "is required" + (
                " (no `asset` given and the pack does not have exactly one .apm/skills/<name>/SKILL.md)"
                if key == "asset" and p.name in _APM_NAMES else ""))

    asset = (p.parent / _text(label, "asset", data["asset"])).resolve()
    if not asset.is_dir():
        raise _fail(label, "asset", f"must be the skill directory, no directory at {asset}")

    for f in asset.rglob("*"):
        # A live run sends the asset's files to the model; a link that leaves
        # the directory would send whatever it points at.
        if not f.resolve().is_relative_to(asset):
            raise _fail(label, "asset", f"contains {f.relative_to(asset)}, a link that leaves the skill directory")

    engine = _text(label, "engine", data["engine"])
    if engine not in ci.ENGINE_IDS:
        raise _fail(label, "engine", f"must be one of {', '.join(ci.ENGINE_IDS)}, got {engine!r}")

    cost = data.get("max_cost_usd")
    if "max_cost_usd" in data:
        cost = _number(label, "max_cost_usd", cost, low=0, strict=True)

    thresholds = data.get("thresholds", {})
    if not isinstance(thresholds, Mapping):
        raise _fail(label, "thresholds", f"must be a mapping, got {thresholds!r}")
    for key in thresholds:
        if key not in _THRESHOLD_KEYS:
            raise _fail(
                label, f"thresholds.{key}",
                f"is not a known key (known: {', '.join(_THRESHOLD_KEYS)})",
            )
    pass_score = _number(label, "thresholds.pass", thresholds.get("pass", 0), low=0, strict=False)
    if pass_score > 1:
        raise _fail(label, "thresholds.pass", f"must be between 0 and 1, got {pass_score:g}")
    max_regression = _number(
        p, "thresholds.max_regression", thresholds.get("max_regression", 0), low=0, strict=False
    )

    report = None
    if "report" in data:
        name = _text(label, "report", data["report"])
        report = (p.parent / name).resolve()
        if not report.is_relative_to(p.parent) or report.suffix != ".json":
            raise _fail(label, "report", f"must be a .json path inside {p.parent.name}/, got {name!r}")

    cfg = EvalConfig(
        path=p,
        asset=asset,
        benchmark=_text(label, "benchmark", data["benchmark"]),
        engine=engine,
        model=_text(label, "model", data["model"]),
        max_cost_usd=cost,
        pass_score=pass_score,
        max_regression=max_regression,
        report=report,
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
