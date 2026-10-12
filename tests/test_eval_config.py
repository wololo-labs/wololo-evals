"""eval.yaml loading: valid files load, and every bad key is named."""
from __future__ import annotations

import pathlib

import pytest

from wololo_evals.cli.eval_config import EvalConfigError, load_eval_config

_REPO = pathlib.Path(__file__).resolve().parents[1]

_GOOD = """\
asset: skill
benchmark: qe-skill
engine: claude
model: claude-sonnet-5-5
max_cost_usd: 2.5
thresholds:
  pass: 0.6
  max_regression: 0.05
"""


def _write(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    (tmp_path / "skill").mkdir(exist_ok=True)
    cfg = tmp_path / "eval.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_valid_file_loads_with_paths_relative_to_the_file(tmp_path):
    cfg = load_eval_config(_write(tmp_path, _GOOD))
    assert cfg.asset == (tmp_path / "skill").resolve()
    assert (cfg.engine, cfg.model, cfg.max_cost_usd) == ("claude", "claude-sonnet-5-5", 2.5)
    assert (cfg.pass_score, cfg.max_regression) == (0.6, 0.05)


def test_optional_keys_default(tmp_path):
    text = "asset: skill\nbenchmark: qe-skill\nengine: claude\nmodel: m\n"
    cfg = load_eval_config(_write(tmp_path, text))
    assert cfg.max_cost_usd is None
    assert (cfg.pass_score, cfg.max_regression) == (0, 0)


@pytest.mark.parametrize(
    ("edit", "key"),
    [
        (lambda t: t.replace("max_cost_usd", "max_cost"), "max_cost"),
        (lambda t: t.replace("max_cost_usd: 2.5", "max_cost_usd: free"), "max_cost_usd"),
        (lambda t: t.replace("max_cost_usd: 2.5", "max_cost_usd: 0"), "max_cost_usd"),
        (lambda t: t.replace("engine: claude", "engine: gpt"), "engine"),
        (lambda t: t.replace("model: claude-sonnet-5-5\n", ""), "model"),
        (lambda t: t.replace("asset: skill", "asset: nowhere"), "asset"),
        (lambda t: t.replace("benchmark: qe-skill", "benchmark: nope"), "benchmark"),
        (lambda t: t.replace("pass: 0.6", "pass: 1.5"), "thresholds.pass"),
        (lambda t: t.replace("max_regression: 0.05", "max_regression: -1"), "thresholds.max_regression"),
        (lambda t: t.replace("  pass: 0.6", "  passs: 0.6"), "thresholds.passs"),
    ],
)
def test_bad_key_is_named_in_the_error(tmp_path, edit, key):
    with pytest.raises(EvalConfigError) as err:
        load_eval_config(_write(tmp_path, edit(_GOOD)))
    assert f"`{key}`" in str(err.value)


def test_not_a_mapping_and_not_yaml_are_errors(tmp_path):
    with pytest.raises(EvalConfigError, match="mapping"):
        load_eval_config(_write(tmp_path, "- a\n- b\n"))
    with pytest.raises(EvalConfigError, match="not valid YAML"):
        load_eval_config(_write(tmp_path, "asset: [unclosed\n"))


def test_builtin_benchmarks_ship_with_the_repo():
    assert (_REPO / "benchmarks" / "qe-skill" / "benchmark.json").is_file()
