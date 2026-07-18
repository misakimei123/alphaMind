import ast
import json
from pathlib import Path

import yaml

from alphamind.config import (
    MarketKind,
    load_effective_config,
    load_freqtrade_config_chain,
    load_instrument_registry,
)
from scripts.verify_freqtrade_contract import EXPECTED_CALLBACK_PARAMETERS

PROJECT_ROOT = Path(__file__).parents[2]
CONFIG_ROOT = PROJECT_ROOT / "configs" / "freqtrade"
STRATEGY_ROOT = PROJECT_ROOT / "user_data" / "strategies"
LOCKED_IMAGE = (
    "freqtradeorg/freqtrade@sha256:1e9298ae0895531fd47c4f13d10e5708b3b8b6e5241292f364fc23f201b5acaa"
)


def load_json(name: str) -> dict[str, object]:
    document = json.loads((CONFIG_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_compose_uses_only_locked_linux_image_and_has_no_live_service() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert "live" not in services
    assert services
    for service in services.values():
        assert service["image"] == LOCKED_IMAGE
        assert service["platform"] == "linux/amd64"
        assert service["profiles"]
        assert service["restart"] == "no"
        if "volumes" in service and service["image"] == LOCKED_IMAGE:
            inherited_or_local = service["volumes"]
            if service in (
                services["backtest"],
                services["spot-dry-run"],
                services["futures-dry-run"],
                services["replay"],
            ):
                assert "./configs/alphamind:/freqtrade/alphamind:ro" in inherited_or_local

    spot = services["spot-dry-run"]
    futures = services["futures-dry-run"]
    assert spot["profiles"] == ["dry-run", "spot-dry-run"]
    assert futures["profiles"] == ["dry-run", "futures-dry-run"]
    assert "/freqtrade/configs/spot.dry-run.json" in spot["command"]
    assert "/freqtrade/configs/futures.dry-run.json" in futures["command"]
    assert "ALPHAMIND_BYBIT_SPOT_API_KEY" in spot["environment"]["FREQTRADE__EXCHANGE__KEY"]
    assert "ALPHAMIND_BYBIT_FUTURES_API_KEY" in futures["environment"]["FREQTRADE__EXCHANGE__KEY"]
    assert "FUTURES" not in json.dumps(spot["environment"])
    assert "SPOT" not in json.dumps(futures["environment"])
    assert "--strategy-path" not in spot["command"]
    assert "--strategy-path" not in futures["command"]
    assert "--strategy-path" not in services["backtest"]["command"]
    assert services["replay"]["network_mode"] == "none"
    assert services["audit-writer"]["profiles"] == ["audit"]
    assert services["audit-writer"]["network_mode"] == "none"
    assert "/workspace/scripts/run_audit_writer.py" in services["audit-writer"]["command"]
    assert all("user_data/db" not in mount for mount in services["audit-writer"]["volumes"])
    assert services["data-snapshot"]["profiles"] == ["data"]
    assert "/workspace/scripts/create_source_snapshot.py" in services["data-snapshot"]["command"]
    assert services["data-quality"]["profiles"] == ["data"]
    assert services["data-quality"]["network_mode"] == "none"
    assert services["data-quality"]["environment"]["PYTHONPATH"] == "/workspace/src:/workspace"
    assert "/workspace/scripts/build_clean_dataset.py" in services["data-quality"]["command"]
    assert services["benchmark-report"]["profiles"] == ["research"]
    assert services["benchmark-report"]["network_mode"] == "none"
    assert services["benchmark-report"]["environment"]["PYTHONPATH"] == "/workspace/src:/workspace"
    assert "/workspace/scripts/build_benchmark_report.py" in services["benchmark-report"]["command"]


def test_mode_configs_are_isolated_and_live_template_has_no_credentials() -> None:
    common = load_json("common.json")
    assert common["timeframe"] == "4h"
    assert "trading_mode" not in common
    assert "margin_mode" not in common
    assert common["initial_state"] == "stopped"
    assert "api_server" not in common
    assert common["exchange"] == {
        "name": "bybit",
        "pair_blacklist": [],
    }
    generated_spot = load_json("spot-instruments.generated.json")
    generated_futures = load_json("futures-instruments.generated.json")
    registry = load_instrument_registry(
        PROJECT_ROOT / "configs" / "alphamind" / "instruments.example.yaml"
    )
    assert generated_spot == {
        "exchange": {"pair_whitelist": list(registry.enabled_pairs(MarketKind.SPOT))}
    }
    assert generated_futures == {
        "exchange": {"pair_whitelist": list(registry.enabled_pairs(MarketKind.FUTURES))}
    }

    mode_files = (
        "backtest.json",
        "spot.dry-run.json",
        "futures.dry-run.json",
        "replay.json",
        "contract.json",
        "spot.live.template.json",
        "futures.live.template.json",
    )
    modes = {name: load_json(name) for name in mode_files}
    spot_files = (
        "backtest.json",
        "spot.dry-run.json",
        "replay.json",
        "contract.json",
        "spot.live.template.json",
    )
    assert all(modes[name]["add_config_files"][1] == "spot.json" for name in spot_files)
    assert all(
        modes[name]["add_config_files"][2] == "spot-instruments.generated.json"
        for name in spot_files
    )
    for name in ("futures.dry-run.json", "futures.live.template.json"):
        assert modes[name]["add_config_files"] == [
            "common.json",
            "futures.json",
            "futures-instruments.generated.json",
        ]
    assert modes["backtest.json"]["dry_run"] is True
    assert modes["spot.dry-run.json"]["dry_run"] is True
    assert modes["futures.dry-run.json"]["dry_run"] is True
    assert modes["replay.json"]["dry_run"] is True
    assert modes["contract.json"]["dry_run"] is True
    assert modes["spot.live.template.json"]["dry_run"] is False
    assert modes["futures.live.template.json"]["dry_run"] is False

    database_urls = {mode["db_url"] for mode in modes.values()}
    bot_identities = {mode["bot_name"] for mode in modes.values()}
    assert len(database_urls) == len(modes)
    assert len(bot_identities) == len(modes)
    for name in ("spot.live.template.json", "futures.live.template.json"):
        assert modes[name]["db_url"].startswith("postgresql+psycopg://<")
        assert "sqlite" not in modes[name]["db_url"]
        assert "dry_run_wallet" not in modes[name]

    serialized_live = json.dumps(
        [modes["spot.live.template.json"], modes["futures.live.template.json"]],
        sort_keys=True,
    ).lower()
    for forbidden_key in ('"key"', '"secret"', '"password"', '"token"'):
        assert forbidden_key not in serialized_live


def test_locked_callback_contract_covers_strategy_and_risk_adapter_hooks() -> None:
    assert tuple(EXPECTED_CALLBACK_PARAMETERS) == (
        "bot_start",
        "bot_loop_start",
        "populate_indicators",
        "populate_entry_trend",
        "populate_exit_trend",
        "custom_stake_amount",
        "custom_stoploss",
        "confirm_trade_entry",
        "order_filled",
    )


def test_p3_02_has_one_fail_closed_freqtrade_strategy() -> None:
    strategy_files = list(STRATEGY_ROOT.glob("*.py"))
    assert [path.name for path in strategy_files] == ["DonchianTrendStrategy.py"]

    source = strategy_files[0].read_text(encoding="utf-8")
    tree = ast.parse(source)
    strategy_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DonchianTrendStrategy"
    ]
    assert len(strategy_classes) == 1
    strategy_class = strategy_classes[0]
    assert any(
        isinstance(base, ast.Name) and base.id == "IStrategy" for base in strategy_class.bases
    )

    methods = {
        node.name: node
        for node in strategy_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {
        "bot_loop_start",
        "bot_start",
        "confirm_trade_entry",
        "custom_stake_amount",
        "custom_stoploss",
        "order_filled",
        "populate_entry_trend",
        "populate_exit_trend",
        "populate_indicators",
        "version",
    } <= methods.keys()

    # dataframe 主路径继续禁止绝对/未来访问；callback 只消费 tail(1) 的已分析结果。
    assert ".rolling(" in source
    assert ".shift(1)" in source
    assert ".iloc[" not in source
    assert "shift(-" not in source
    assert "requests." not in source
    assert "urlopen(" not in source
    assert "sqlite" not in source.lower()
    assert "return 0.0" in source


def test_p3_02_config_selects_risk_sized_strategy_and_disables_position_adjustment() -> None:
    spot = load_freqtrade_config_chain(
        "spot.dry-run.json",
        config_root=CONFIG_ROOT,
        market=MarketKind.SPOT,
    ).merged
    futures = load_freqtrade_config_chain(
        "futures.dry-run.json",
        config_root=CONFIG_ROOT,
        market=MarketKind.FUTURES,
    ).merged
    assert spot["strategy"] == "DonchianTrendStrategy"
    assert spot["timeframe"] == "4h"
    assert spot["trading_mode"] == "spot"
    assert futures["trading_mode"] == "futures"
    assert futures["margin_mode"] == "isolated"
    assert spot["position_adjustment_enable"] is False
    assert futures["position_adjustment_enable"] is False
    assert spot["stake_amount"] == "unlimited"
    assert spot["use_exit_signal"] is True
    assert spot["exit_profit_only"] is False
    assert spot["ignore_roi_if_entry_signal"] is False
    assert "telegram" not in spot
    assert "telegram" not in futures
    expected_pricing = {
        "price_side": "same",
        "use_order_book": True,
        "order_book_top": 1,
        "price_last_balance": 0.0,
    }
    assert spot["entry_pricing"] == expected_pricing
    assert spot["exit_pricing"] == expected_pricing


def test_effective_config_validates_both_merged_runtime_instances() -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})

    assert effective.execution_ready is True
    assert effective.warnings == ()
    assert (
        effective.source_sha256["freqtrade_spot_merged"]
        != effective.source_sha256["freqtrade_futures_merged"]
    )
