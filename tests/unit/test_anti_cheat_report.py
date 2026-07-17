from __future__ import annotations

from pathlib import Path

import pytest

from scripts.build_anti_cheat_report import (
    load_config,
    parse_lookahead_csv,
    parse_recursive_output,
)


def test_frozen_anti_cheat_config_loads() -> None:
    config = load_config(Path("configs/research/anti-cheat-v1.toml"))
    assert config["freqtrade_version"] == "2026.6"
    assert config["alternate_exchange"]["performance_selection_allowed"] is False


def test_lookahead_csv_requires_no_bias_and_enough_signals(tmp_path: Path) -> None:
    path = tmp_path / "lookahead.csv"
    path.write_text(
        "filename,strategy,has_bias,total_signals,biased_entry_signals,"
        "biased_exit_signals,biased_indicators\n"
        "strategy.py,Strategy,False,100,0,0,\n",
        encoding="utf-8",
    )
    assert parse_lookahead_csv(path, 20)["total_signals"] == 100

    path.write_text(
        "filename,strategy,has_bias,total_signals,biased_entry_signals,"
        "biased_exit_signals,biased_indicators\n"
        "strategy.py,Strategy,True,100,1,0,channel\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="reported bias"):
        parse_lookahead_csv(path, 20)

    path.write_text(
        "filename,strategy,has_bias,total_signals,biased_entry_signals,"
        "biased_exit_signals,biased_indicators\n"
        "strategy.py,Strategy,False,100,1,0,channel\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="contradicts"):
        parse_lookahead_csv(path, 20)


def test_recursive_output_requires_both_zero_variance_and_no_lookahead() -> None:
    output = (
        "No variance on indicator(s) found due to recursive formula.\n"
        "No lookahead bias on indicators found.\n"
    )
    assert parse_recursive_output(output)["zero_variance"] is True
    with pytest.raises(RuntimeError, match="did not prove"):
        parse_recursive_output("No variance on indicator(s) found due to recursive formula.\n")
