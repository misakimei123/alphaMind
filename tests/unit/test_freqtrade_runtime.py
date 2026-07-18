from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from alphamind.config import (
    FreqtradeInstanceConfig,
    FreqtradeRuntimeConfigError,
    MarketKind,
    load_effective_config,
    load_freqtrade_config_chain,
    validate_freqtrade_instance_contract,
)

PROJECT_ROOT = Path(__file__).parents[2]
CONFIG_ROOT = PROJECT_ROOT / "configs" / "freqtrade"


def _load_pair(
    market: MarketKind,
) -> tuple[FreqtradeInstanceConfig, tuple[str, ...], dict[str, Any]]:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    section = effective.runtime["execution"][market.value]
    assert isinstance(section, dict)
    instance = load_freqtrade_config_chain(
        PROJECT_ROOT / section["freqtrade_config_path"],
        config_root=CONFIG_ROOT,
        market=market,
    )
    return instance, effective.market_capability_snapshot.available_pairs(market), section


def test_spot_and_futures_chains_merge_to_distinct_safe_instances() -> None:
    spot, spot_pairs, spot_runtime = _load_pair(MarketKind.SPOT)
    futures, futures_pairs, futures_runtime = _load_pair(MarketKind.FUTURES)

    assert spot.source_paths == (
        CONFIG_ROOT / "common.json",
        CONFIG_ROOT / "spot.json",
        CONFIG_ROOT / "spot-instruments.generated.json",
        CONFIG_ROOT / "spot.dry-run.json",
    )
    assert futures.source_paths == (
        CONFIG_ROOT / "common.json",
        CONFIG_ROOT / "futures.json",
        CONFIG_ROOT / "futures-instruments.generated.json",
        CONFIG_ROOT / "futures.dry-run.json",
    )
    assert spot.merged_sha256 != futures.merged_sha256
    assert spot.merged["exchange"]["pair_whitelist"] == list(spot_pairs)
    assert futures.merged["exchange"]["pair_whitelist"] == list(futures_pairs)
    assert spot.merged["bot_name"] != futures.merged["bot_name"]
    assert spot.merged["db_url"] != futures.merged["db_url"]
    assert spot_runtime["api_key_env"] != futures_runtime["api_key_env"]


def test_live_templates_merge_to_isolated_non_connectable_instances() -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    templates = (
        (
            MarketKind.SPOT,
            "spot.live.template.json",
            "alphamind_spot_live",
            "postgresql+psycopg://<spot-set-at-runtime>",
        ),
        (
            MarketKind.FUTURES,
            "futures.live.template.json",
            "alphamind_futures_live",
            "postgresql+psycopg://<futures-set-at-runtime>",
        ),
    )
    merged_hashes: set[str] = set()
    for market, filename, identity, db_url in templates:
        instance = load_freqtrade_config_chain(
            filename,
            config_root=CONFIG_ROOT,
            market=market,
        )
        validate_freqtrade_instance_contract(
            instance,
            expected_bot_identity=identity,
            expected_db_url=db_url,
            expected_pairs=effective.market_capability_snapshot.available_pairs(market),
            environment="live",
        )
        assert "dry_run_wallet" not in instance.merged
        merged_hashes.add(instance.merged_sha256)
    assert len(merged_hashes) == 2


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("bot_name", "wrong-bot", "bot_name"),
        ("db_url", "sqlite:////wrong.sqlite", "db_url"),
        ("initial_state", "running", "start stopped"),
        ("trading_mode", "spot", "isolated futures mode"),
    ],
)
def test_futures_instance_contract_rejects_identity_and_mode_drift(
    field: str,
    value: str,
    message: str,
) -> None:
    instance, pairs, runtime = _load_pair(MarketKind.FUTURES)
    merged = deepcopy(instance.merged)
    merged[field] = value

    with pytest.raises(FreqtradeRuntimeConfigError, match=message):
        validate_freqtrade_instance_contract(
            replace(instance, merged=merged),
            expected_bot_identity=runtime["bot_identity"],
            expected_db_url=runtime["runtime_db_path"],
            expected_pairs=pairs,
            environment="dry_run",
        )


def test_instance_contract_rejects_cross_market_pairs_and_json_credentials() -> None:
    instance, pairs, runtime = _load_pair(MarketKind.FUTURES)
    crossed = deepcopy(instance.merged)
    crossed["exchange"]["pair_whitelist"] = ["BTC/USDT"]
    with pytest.raises(FreqtradeRuntimeConfigError, match="pair whitelist"):
        validate_freqtrade_instance_contract(
            replace(instance, merged=crossed),
            expected_bot_identity=runtime["bot_identity"],
            expected_db_url=runtime["runtime_db_path"],
            expected_pairs=pairs,
            environment="dry_run",
        )

    credentialed = deepcopy(instance.merged)
    credentialed["exchange"]["key"] = "must-not-live-in-json"
    with pytest.raises(FreqtradeRuntimeConfigError, match="must not contain credentials"):
        validate_freqtrade_instance_contract(
            replace(instance, merged=credentialed),
            expected_bot_identity=runtime["bot_identity"],
            expected_db_url=runtime["runtime_db_path"],
            expected_pairs=pairs,
            environment="dry_run",
        )


def test_config_chain_rejects_escape_and_cycles(tmp_path: Path) -> None:
    root = tmp_path / "configs"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (root / "escape.json").write_text(
        json.dumps({"add_config_files": ["../outside.json"]}),
        encoding="utf-8",
    )
    with pytest.raises(FreqtradeRuntimeConfigError, match="outside config root"):
        load_freqtrade_config_chain(
            "escape.json",
            config_root=root,
            market=MarketKind.SPOT,
        )

    (root / "a.json").write_text(
        json.dumps({"add_config_files": ["b.json"]}),
        encoding="utf-8",
    )
    (root / "b.json").write_text(
        json.dumps({"add_config_files": ["a.json"]}),
        encoding="utf-8",
    )
    with pytest.raises(FreqtradeRuntimeConfigError, match="cycle"):
        load_freqtrade_config_chain("a.json", config_root=root, market=MarketKind.SPOT)
