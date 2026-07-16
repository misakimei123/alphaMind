from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parents[2]


def load_yaml(relative_path: str) -> dict[str, object]:
    path = PROJECT_ROOT / relative_path
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_capability_matrix_classifies_order_and_reconciliation_requirements() -> None:
    matrix = load_yaml("configs/common/exchange-capabilities.yaml")

    freqtrade = matrix["freqtrade"]
    permissions = matrix["api_permissions"]
    orders = matrix["orders"]
    environments = matrix["environments"]
    assert isinstance(freqtrade, dict)
    assert isinstance(permissions, dict)
    assert isinstance(orders, dict)
    assert isinstance(environments, dict)

    assert freqtrade["officially_supported_spot"] is True
    assert freqtrade["owns_all_trade_writes"] is True
    assert freqtrade["stoploss_on_exchange_spot"] is False
    assert permissions["spot_trade_without_withdrawal"] is True
    assert "Withdrawal" in permissions["forbidden_live_permissions"]
    assert orders["client_order_id"]["supported"] is True
    assert orders["create_ack_is_terminal"] is False
    assert orders["cancel_ack_is_terminal"] is False
    assert orders["fetch_realtime"] is True
    assert orders["fetch_history"] is True
    assert orders["fetch_executions"] is True
    assert environments["testnet"]["usage"] == "independent_contract_harness_only"


def test_final_holdout_degradation_is_recorded() -> None:
    manifest = load_yaml("data/manifests/regime-manifest.yaml")
    dataset_contract = manifest["dataset_contract"]
    assert isinstance(dataset_contract, dict)
    holdout = dataset_contract["final_holdout"]
    assert isinstance(holdout, dict)

    assert holdout == {
        "start": "2025-07-01T00:00:00Z",
        "end_exclusive": "2026-07-01T00:00:00Z",
        "state": "DEGRADED_TO_DEVELOPMENT",
        "access_count": 1,
        "first_accessed_at_utc": "2026-07-16T07:01:34Z",
        "first_access_commit": None,
        "first_access_worktree_base_commit": "630380e",
        "degraded_reason": "p1_03_full_partition_ohlcv_integrity_scan",
        "access_evidence": (
            "data/manifests/source/"
            "bybit-spot-ohlcv-20260716T070451Z-ef232b839406.holdout-access.json"
        ),
    }


def test_trial_budget_is_frozen_at_fourteen_without_cartesian_search() -> None:
    strategy_card = load_yaml("research/strategy_cards/donchian_trend_v0.1.0.yaml")
    trials = strategy_card["parameter_trials"]
    assert isinstance(trials, dict)
    perturbations = trials["perturbations"]
    robustness = trials["robustness_only"]
    assert isinstance(perturbations, dict)
    assert isinstance(robustness, dict)

    planned_trials = 1 + sum(len(values) for values in perturbations.values())
    if robustness["reuse_baseline_parameters"] is True:
        planned_trials += 1

    assert planned_trials == 14
    assert trials["maximum_parameterized_trials"] == planned_trials
    assert trials["cartesian_product_allowed"] is False
    assert trials["failed_trials_must_be_retained"] is True
