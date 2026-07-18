"""R1-05 Freqtrade 多文件配置合并与 spot/futures 实例隔离合同。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphamind.config.instruments import MarketKind

JsonObject = dict[str, Any]


class FreqtradeRuntimeConfigError(ValueError):
    """Freqtrade 配置链无法证明实例隔离。"""


@dataclass(frozen=True, slots=True)
class FreqtradeInstanceConfig:
    market: MarketKind
    entry_path: Path
    source_paths: tuple[Path, ...]
    source_sha256: Mapping[str, str]
    merged: JsonObject
    merged_sha256: str


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path) -> JsonObject:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise FreqtradeRuntimeConfigError(
            f"Freqtrade config {path.name} is missing or invalid"
        ) from None
    if not isinstance(document, dict):
        raise FreqtradeRuntimeConfigError(f"Freqtrade config {path.name} must be an object")
    return document


def _deep_merge(base: JsonObject, override: Mapping[str, Any]) -> JsonObject:
    merged = deepcopy(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_freqtrade_config_chain(
    entry_path: str | Path,
    *,
    config_root: str | Path,
    market: MarketKind | str,
) -> FreqtradeInstanceConfig:
    """按 add_config_files 顺序深合并；入口文件最后覆盖基础片段。"""

    root = Path(config_root).resolve()
    entry = Path(entry_path)
    entry = (root / entry).resolve() if not entry.is_absolute() else entry.resolve()
    if not entry.is_relative_to(root) or entry.suffix.lower() != ".json":
        raise FreqtradeRuntimeConfigError("Freqtrade config must stay in its JSON config root")

    ordered_paths: list[Path] = []
    active: set[Path] = set()

    def load_recursive(path: Path) -> JsonObject:
        if not path.is_relative_to(root):
            raise FreqtradeRuntimeConfigError("add_config_files path escapes config root")
        if path in active:
            raise FreqtradeRuntimeConfigError("add_config_files contains a cycle")
        active.add(path)
        document = _load_json(path)
        includes = document.get("add_config_files", [])
        if not isinstance(includes, list) or any(
            not isinstance(item, str) or not item or Path(item).is_absolute() for item in includes
        ):
            raise FreqtradeRuntimeConfigError("add_config_files must contain relative JSON paths")
        merged: JsonObject = {}
        for raw_include in includes:
            include_path = (path.parent / raw_include).resolve()
            if include_path.suffix.lower() != ".json" or not include_path.is_relative_to(root):
                raise FreqtradeRuntimeConfigError("add_config_files path is outside config root")
            merged = _deep_merge(merged, load_recursive(include_path))
        own = {key: value for key, value in document.items() if key != "add_config_files"}
        merged = _deep_merge(merged, own)
        active.remove(path)
        if path not in ordered_paths:
            ordered_paths.append(path)
        return merged

    merged = load_recursive(entry)
    hashes = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in ordered_paths
    }
    return FreqtradeInstanceConfig(
        market=MarketKind(market),
        entry_path=entry,
        source_paths=tuple(ordered_paths),
        source_sha256=hashes,
        merged=merged,
        merged_sha256=_canonical_sha256(merged),
    )


def _contains_secret_key(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in {"key", "secret", "password", "token", "jwt_secret_key", "ws_token"}:
                return True
            if _contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def validate_freqtrade_instance_contract(
    instance: FreqtradeInstanceConfig,
    *,
    expected_bot_identity: str,
    expected_db_url: str,
    expected_pairs: tuple[str, ...],
    environment: str,
) -> None:
    """复核最终配置，不把配置文件存在误当作实例可安全隔离。"""

    merged = instance.merged
    if merged.get("bot_name") != expected_bot_identity:
        raise FreqtradeRuntimeConfigError("Freqtrade bot_name does not match runtime identity")
    if merged.get("db_url") != expected_db_url:
        raise FreqtradeRuntimeConfigError("Freqtrade db_url does not match runtime database")
    if environment in {"dry_run", "demo", "testnet"} and merged.get("dry_run") is not True:
        raise FreqtradeRuntimeConfigError("non-live runtime requires dry_run=true")
    if environment == "live" and merged.get("dry_run") is not False:
        raise FreqtradeRuntimeConfigError("live runtime requires dry_run=false")
    if merged.get("initial_state") != "stopped" or merged.get("force_entry_enable") is not False:
        raise FreqtradeRuntimeConfigError(
            "Freqtrade instance must start stopped with force entry disabled"
        )
    if "api_server" in merged or "telegram" in merged:
        raise FreqtradeRuntimeConfigError("R1-05 instances must not expose API or Telegram")
    if _contains_secret_key(merged):
        raise FreqtradeRuntimeConfigError("Freqtrade JSON must not contain credentials")

    exchange = merged.get("exchange")
    if not isinstance(exchange, Mapping) or exchange.get("name") != "bybit":
        raise FreqtradeRuntimeConfigError("Freqtrade exchange must be Bybit")
    pairs = exchange.get("pair_whitelist")
    if pairs != list(expected_pairs):
        raise FreqtradeRuntimeConfigError(
            "Freqtrade pair whitelist does not match market capability"
        )
    if merged.get("position_adjustment_enable") is not False:
        raise FreqtradeRuntimeConfigError("position adjustment remains disabled before R4/R5")

    order_types = merged.get("order_types")
    if not isinstance(order_types, Mapping):
        raise FreqtradeRuntimeConfigError("order_types must be explicit per market")
    if instance.market is MarketKind.SPOT:
        if merged.get("trading_mode") != "spot" or merged.get("margin_mode") != "":
            raise FreqtradeRuntimeConfigError("spot instance trading or margin mode is invalid")
        if order_types.get("stoploss_on_exchange") is not False:
            raise FreqtradeRuntimeConfigError("Bybit spot stoploss must remain bot-managed")
    else:
        if merged.get("trading_mode") != "futures" or merged.get("margin_mode") != "isolated":
            raise FreqtradeRuntimeConfigError("futures instance must use isolated futures mode")
        if merged.get("liquidation_buffer") != 0.05:
            raise FreqtradeRuntimeConfigError("futures liquidation_buffer must be 0.05")
        if order_types.get("stoploss_on_exchange") is not True:
            raise FreqtradeRuntimeConfigError("Bybit futures must configure exchange stoploss")
