import json
import re
from pathlib import Path

import pytest

from scripts.build_execution_model_report import build_report, render_markdown

PROJECT_ROOT = Path(__file__).parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs/research/execution-model-v1.toml"
REPORT_ROOT = PROJECT_ROOT / "research/reports/execution-model/p2-04-v1"


def test_execution_report_discloses_all_scenarios_and_evidence_boundaries() -> None:
    report = build_report(CONFIG_PATH)
    scenarios = report["scenarios"]

    assert isinstance(scenarios, list)
    assert len(scenarios) == 11
    assert len({item["scenario_id"] for item in scenarios}) == 11
    assert report["fill_contract"] == {
        "timing": "next_candle_open",
        "limit_fill_policy": "explicit_confirmation",
        "same_candle_fill_allowed": False,
        "partial_fill_claimed": False,
    }
    assert re.fullmatch(r"[0-9a-f]{64}", report["config_sha256"])
    assert set(report["evidence_boundary"]) == {
        "historical_backtest",
        "dry_run",
        "live_canary",
    }


def test_execution_report_is_deterministic_and_markdown_lists_each_scenario() -> None:
    first = build_report(CONFIG_PATH)
    second = build_report(CONFIG_PATH)
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )

    markdown = render_markdown(first)
    for scenario in first["scenarios"]:
        assert scenario["scenario_id"] in markdown

    committed_json = json.loads((REPORT_ROOT / "report.json").read_text(encoding="utf-8"))
    committed_markdown = (REPORT_ROOT / "report.md").read_text(encoding="utf-8")
    assert committed_json == first
    assert committed_markdown == markdown


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("schema_version = 1", "schema_version = true", "schema_version"),
        ("delay_periods = [1]", "delay_periods = [true]", "delay contract"),
    ],
)
def test_execution_report_rejects_boolean_values_for_integer_contracts(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    config_text = CONFIG_PATH.read_text(encoding="utf-8").replace(old, new)
    invalid_config = tmp_path / "execution-model-v1.toml"
    invalid_config.write_text(config_text, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        build_report(invalid_config)
