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


def test_asset_with_a_link_out_of_the_directory_is_refused(tmp_path):
    cfg = _write(tmp_path, _GOOD)
    secret = tmp_path / "secret.txt"
    secret.write_text("x", encoding="utf-8")
    (tmp_path / "skill" / "notes.md").symlink_to(secret)
    with pytest.raises(EvalConfigError, match=r"`asset`.*notes\.md"):
        load_eval_config(cfg)


# --- the `eval:` block of apm.yml --------------------------------------------

_BLOCK = "eval:\n" + "".join(f"  {l}\n" for l in _GOOD.splitlines())


def _apm(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    (tmp_path / "skill").mkdir(exist_ok=True)
    manifest = tmp_path / "apm.yml"
    manifest.write_text("name: pack\nversion: 1.0.0\n" + text, encoding="utf-8")
    return manifest


def test_apm_block_loads_with_paths_relative_to_apm_yml(tmp_path, capsys):
    cfg = load_eval_config(_apm(tmp_path, _BLOCK))
    assert cfg.path == (tmp_path / "apm.yml").resolve()
    assert cfg.asset == (tmp_path / "skill").resolve()
    assert (cfg.engine, cfg.model, cfg.max_cost_usd) == ("claude", "claude-sonnet-5-5", 2.5)
    assert (cfg.pass_score, cfg.max_regression) == (0.6, 0.05)
    assert cfg.report_json == (tmp_path / "eval-gate-report.json").resolve()
    assert capsys.readouterr().err == ""


def test_apm_paths_do_not_follow_the_process_cwd(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    manifest = _apm(pack, _BLOCK)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert load_eval_config(manifest).asset == (pack / "skill").resolve()


@pytest.mark.parametrize(
    ("edit", "key"),
    [
        (lambda t: t.replace("max_cost_usd", "max_cost"), "max_cost"),
        (lambda t: t.replace("  model: claude-sonnet-5-5\n", ""), "model"),
        (lambda t: t.replace("  benchmark: qe-skill\n", ""), "benchmark"),
        (lambda t: t.replace("engine: claude", "engine: gpt"), "engine"),
        (lambda t: t.replace("    pass:", "    passs:"), "thresholds.passs"),
        (lambda t: t.replace("asset: skill", "asset: nowhere"), "asset"),
    ],
)
def test_apm_block_errors_name_the_key_and_the_block(tmp_path, edit, key):
    with pytest.raises(EvalConfigError) as err:
        load_eval_config(_apm(tmp_path, edit(_BLOCK)))
    assert f"`{key}`" in str(err.value)
    assert "apm.yml `eval:`" in str(err.value)


@pytest.mark.parametrize("text", ["", "eval:\n", "eval: [a, b]\n", "eval: nope\n"])
def test_apm_without_a_usable_block_is_refused(tmp_path, text):
    with pytest.raises(EvalConfigError, match=r"apm\.yml"):
        load_eval_config(_apm(tmp_path, text))


def test_apm_asset_defaults_to_the_packs_only_skill(tmp_path):
    skill = tmp_path / ".apm" / "skills" / "only"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: only\n---\n", encoding="utf-8")
    block = _BLOCK.replace("  asset: skill\n", "")
    assert load_eval_config(_apm(tmp_path, block)).asset == skill.resolve()

    other = tmp_path / ".apm" / "skills" / "other"
    other.mkdir()
    (other / "SKILL.md").write_text("---\nname: other\n---\n", encoding="utf-8")
    with pytest.raises(EvalConfigError, match=r"`asset`.*required"):
        load_eval_config(tmp_path / "apm.yml")


def test_apm_report_key_moves_the_report_but_not_out_of_the_pack(tmp_path):
    cfg = load_eval_config(_apm(tmp_path, _BLOCK + "  report: out/gate.json\n"))
    assert cfg.report_json == (tmp_path / "out" / "gate.json").resolve()
    for bad in ("../gate.json", "gate.txt"):
        with pytest.raises(EvalConfigError, match="`report`"):
            load_eval_config(_apm(tmp_path, _BLOCK + f"  report: {bad}\n"))


def test_apm_block_wins_over_a_sibling_eval_yaml_with_a_notice(tmp_path, capsys):
    _write(tmp_path, _GOOD.replace("model: claude-sonnet-5-5", "model: legacy"))
    for entry in ("apm.yml", "eval.yaml"):
        manifest = _apm(tmp_path, _BLOCK)
        cfg = load_eval_config(tmp_path / entry)
        assert (cfg.model, cfg.path) == ("claude-sonnet-5-5", manifest.resolve())
        err = capsys.readouterr().err
        assert err.count("notice:") == 1 and "both exist" in err and "using apm.yml" in err


def test_eval_yaml_alone_still_works_with_a_one_line_deprecation_notice(tmp_path, capsys):
    cfg = load_eval_config(_write(tmp_path, _GOOD))
    assert cfg.model == "claude-sonnet-5-5"
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("notice:") and "deprecated" in err[0]


def test_eval_yaml_is_used_when_apm_yml_has_no_block_or_is_unreadable(tmp_path, capsys):
    manifest = _write(tmp_path, _GOOD).parent / "apm.yml"
    manifest.write_text("name: pack\n", encoding="utf-8")
    assert load_eval_config(tmp_path / "eval.yaml").model == "claude-sonnet-5-5"
    manifest.write_text("name: [unclosed\n", encoding="utf-8")
    assert load_eval_config(tmp_path / "eval.yaml").model == "claude-sonnet-5-5"
    assert load_eval_config(manifest.parent / "eval.yaml").model == "claude-sonnet-5-5"
    capsys.readouterr()
