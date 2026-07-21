import json
from pathlib import Path

from scripts.build_r2_06_feature_comparison import build_report, render_markdown

PROJECT_ROOT = Path(__file__).parents[2]
SCENARIO_PATH = PROJECT_ROOT / "tests/fixtures/decision/r2-06-ai-dry-run.yaml"
REPORT_ROOT = PROJECT_ROOT / "research/reports/ai-feature-comparison/r2-06-v2"


def test_r2_06_comparison_covers_frozen_contexts_and_required_metrics() -> None:
    report = build_report(PROJECT_ROOT, SCENARIO_PATH)

    assert report["evidence_type"] == "frozen_offline_policy_fixture"
    assert report["provider_network_called"] is False
    assert report["win_rate_claimed"] is False
    assert len(report["scenarios"]) == 11
    assert len({scenario["category"] for scenario in report["scenarios"]}) == 11
    assert report["metrics"] == {
        "baseline": {
            "schema_valid_rate": "1.0000",
            "expected_hold_hit_rate": "0.5556",
            "key_conflict_citation_rate": "0.0000",
            "invalid_or_unauthorized_action_rate": "0.0000",
            "input_tokens_estimate": 13100,
            "output_tokens_estimate": 2265,
            "cost_usd_estimate": "0.066725",
        },
        "expanded": {
            "schema_valid_rate": "1.0000",
            "expected_hold_hit_rate": "1.0000",
            "key_conflict_citation_rate": "1.0000",
            "invalid_or_unauthorized_action_rate": "0.0000",
            "input_tokens_estimate": 16296,
            "output_tokens_estimate": 2325,
            "cost_usd_estimate": "0.075617",
        },
    }
    assert report["delta"] == {
        "input_tokens_estimate": 3196,
        "output_tokens_estimate": 60,
        "cost_usd_estimate": "0.008892",
    }


def test_r2_06_comparison_report_is_deterministic_and_committed() -> None:
    first = build_report(PROJECT_ROOT, SCENARIO_PATH)
    second = build_report(PROJECT_ROOT, SCENARIO_PATH)
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )

    committed_json = json.loads((REPORT_ROOT / "report.json").read_text(encoding="utf-8"))
    committed_markdown = (REPORT_ROOT / "report.md").read_text(encoding="utf-8")
    assert committed_json == first
    assert committed_markdown == render_markdown(first)
