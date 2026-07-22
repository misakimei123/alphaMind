"""AI MVP 业务配置的加载、环境覆盖、跨文件校验和安全展示。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from referencing import Registry, Resource

from alphamind.config.freqtrade_runtime import (
    FreqtradeRuntimeConfigError,
    load_freqtrade_config_chain,
    validate_freqtrade_instance_contract,
)
from alphamind.config.instruments import (
    InstrumentRegistry,
    InstrumentRegistryError,
    MarketKind,
    parse_instrument_registry,
)
from alphamind.market.capabilities import (
    MarketCapabilitySnapshot,
    parse_market_capability_snapshot,
)

DEFAULT_RUNTIME_CONFIG = Path("configs/alphamind/runtime.example.yaml")
SCHEMA_DIRECTORY = Path("data/schemas")

JsonObject = dict[str, Any]
OverrideParser = Callable[[str], object]


class ConfigError(ValueError):
    """配置无法安全加载时抛出的脱敏错误。"""


@dataclass(frozen=True, slots=True)
class RuntimeDependency:
    """后续运行阶段需要但不属于 R1-01 控制面的文件。"""

    component: str
    path: str
    required: bool
    exists: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "component": self.component,
            "path": self.path,
            "required": self.required,
            "exists": self.exists,
        }


@dataclass(frozen=True, slots=True)
class EffectiveConfig:
    """通过 schema、覆盖和跨文件校验后的完整配置快照。"""

    project_root: Path
    runtime: JsonObject
    instruments: JsonObject
    market_capabilities: JsonObject
    ai_profile: JsonObject
    news_sources: JsonObject
    source_sha256: Mapping[str, str]
    effective_sha256: str
    applied_overrides: tuple[str, ...]
    runtime_dependencies: tuple[RuntimeDependency, ...]
    warnings: tuple[str, ...]

    @property
    def execution_ready(self) -> bool:
        return all(not item.required or item.exists for item in self.runtime_dependencies)

    @property
    def instrument_registry(self) -> InstrumentRegistry:
        return parse_instrument_registry(
            self.instruments,
            source_sha256=self.source_sha256["instruments"],
        )

    @property
    def market_capability_snapshot(self) -> MarketCapabilitySnapshot:
        return parse_market_capability_snapshot(
            self.market_capabilities,
            registry=self.instrument_registry,
            source_sha256=self.source_sha256["market_capabilities"],
        )

    def to_safe_dict(self) -> dict[str, object]:
        """返回可打印的脱敏配置；环境变量名可见，但永不读取对应 secret 值。"""

        return {
            "schema_version": 1,
            "effective_sha256": self.effective_sha256,
            "applied_overrides": list(self.applied_overrides),
            "execution_ready": self.execution_ready,
            "runtime_dependencies": [item.to_dict() for item in self.runtime_dependencies],
            "warnings": list(self.warnings),
            "source_sha256": dict(sorted(self.source_sha256.items())),
            "configuration": _redact(
                {
                    "runtime": self.runtime,
                    "instruments": self.instruments,
                    "market_capabilities": self.market_capabilities,
                    "ai_profile": self.ai_profile,
                    "news_sources": self.news_sources,
                }
            ),
        }


def _parse_bool(raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ConfigError("boolean environment overrides must be 'true' or 'false'")


def _parse_int(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigError("integer environment override is invalid") from error


def _parse_decimal_string(raw: str) -> str:
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ConfigError("decimal environment override is invalid") from error
    if not value.is_finite() or value <= 0:
        raise ConfigError("decimal environment override must be finite and positive")
    return raw


def _parse_text(raw: str) -> str:
    if not raw:
        raise ConfigError("text environment override must not be empty")
    return raw


ENVIRONMENT_OVERRIDES: Mapping[str, tuple[tuple[str, ...], OverrideParser]] = {
    "ALPHAMIND_AI_PROFILE_PATH": (("decision", "ai_profile_path"), _parse_text),
    "ALPHAMIND_ENVIRONMENT": (("environment",), _parse_text),
    "ALPHAMIND_DECISION_CYCLE_MINUTES": (
        ("scheduler", "decision_cycle_minutes"),
        _parse_int,
    ),
    "ALPHAMIND_CYCLE_TIMEOUT_SECONDS": (
        ("scheduler", "cycle_timeout_seconds"),
        _parse_int,
    ),
    "ALPHAMIND_NEWS_LOOKBACK_HOURS": (("scheduler", "news_lookback_hours"), _parse_int),
    "ALPHAMIND_MAX_ACTIONS_PER_CYCLE": (
        ("scheduler", "max_actions_per_cycle"),
        _parse_int,
    ),
    "ALPHAMIND_SPOT_ENABLED": (("execution", "spot", "enabled"), _parse_bool),
    "ALPHAMIND_FUTURES_ENABLED": (("execution", "futures", "enabled"), _parse_bool),
    "ALPHAMIND_GLOBAL_MAX_LEVERAGE": (
        ("execution", "futures", "global_max_leverage"),
        _parse_decimal_string,
    ),
    "ALPHAMIND_RISK_SNAPSHOT_MAX_AGE_SECONDS": (
        ("risk", "risk_snapshot_max_age_seconds"),
        _parse_int,
    ),
    "ALPHAMIND_MAXIMUM_APPROVAL_PRICE_DRIFT_FRACTION": (
        ("risk", "maximum_approval_price_drift_fraction"),
        _parse_decimal_string,
    ),
    "ALPHAMIND_NEWS_REQUIRED_FOR_RISK_INCREASE": (
        ("risk", "news_required_for_risk_increase"),
        _parse_bool,
    ),
    "ALPHAMIND_ALLOW_ADD_TO_LOSING_POSITION": (
        ("risk", "allow_add_to_losing_position"),
        _parse_bool,
    ),
    "ALPHAMIND_APPROVAL_TTL_MINUTES": (("approval", "ttl_minutes"), _parse_int),
}


def _load_yaml(path: Path, *, label: str) -> JsonObject:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigError(f"{label} could not be read as UTF-8 YAML") from None
    if not isinstance(loaded, dict):
        raise ConfigError(f"{label} must be a YAML object")
    return loaded


def _load_json(path: Path, *, label: str) -> JsonObject:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError(f"{label} could not be read as UTF-8 JSON") from None
    if not isinstance(loaded, dict):
        raise ConfigError(f"{label} must be a JSON object")
    return loaded


def _resolve_repo_path(
    project_root: Path,
    raw_path: str | Path,
    *,
    label: str,
    must_exist: bool,
) -> Path:
    candidate_path = Path(raw_path)
    if candidate_path.is_absolute():
        candidate = candidate_path.resolve()
    else:
        candidate = (project_root / candidate_path).resolve()
    if not candidate.is_relative_to(project_root):
        raise ConfigError(f"{label} must stay inside the project root")
    if must_exist and not candidate.is_file():
        raise ConfigError(f"{label} does not exist or is not a file")
    return candidate


def _load_schema_registry(project_root: Path) -> tuple[dict[str, JsonObject], Registry]:
    schema_root = _resolve_repo_path(
        project_root,
        SCHEMA_DIRECTORY,
        label="schema directory",
        must_exist=False,
    )
    if not schema_root.is_dir():
        raise ConfigError("schema directory does not exist")

    schemas: dict[str, JsonObject] = {}
    resources: list[tuple[str, Resource[Any]]] = []
    for path in sorted(schema_root.glob("*.schema.yaml")):
        schema = _load_yaml(path, label=f"schema {path.name}")
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError:
            raise ConfigError(f"schema {path.name} is invalid") from None
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str):
            raise ConfigError(f"schema {path.name} is missing a string $id")
        schemas[path.name] = schema
        resources.append((schema_id, Resource.from_contents(schema)))
    return schemas, Registry().with_resources(resources)


def _validate_schema(
    document: JsonObject,
    *,
    schema_name: str,
    label: str,
    schemas: Mapping[str, JsonObject],
    registry: Registry,
) -> None:
    schema = schemas.get(schema_name)
    if schema is None:
        raise ConfigError(f"required schema {schema_name} is missing")
    validator = jsonschema.Draft202012Validator(
        schema,
        registry=registry,
        format_checker=jsonschema.FormatChecker(),
    )
    error = next(iter(validator.iter_errors(document)), None)
    if error is None:
        return
    location = ".".join(str(part) for part in error.absolute_path) or "root"
    # 不回显 instance 或原始 jsonschema message，避免误把 secret 值写入日志。
    raise ConfigError(f"{label} failed schema validation at {location} ({error.validator})")


def _set_nested(document: JsonObject, path: tuple[str, ...], value: object) -> None:
    target = document
    for key in path[:-1]:
        child = target.get(key)
        if not isinstance(child, dict):
            raise ConfigError(f"override target {'.'.join(path)} is not an object")
        target = child
    final_key = path[-1]
    if final_key not in target:
        raise ConfigError(f"override target {'.'.join(path)} does not exist")
    target[final_key] = value


def _apply_environment_overrides(
    runtime: JsonObject,
    environ: Mapping[str, str],
) -> tuple[JsonObject, tuple[str, ...]]:
    effective = deepcopy(runtime)
    applied: list[str] = []
    for name, (path, parser) in ENVIRONMENT_OVERRIDES.items():
        if name not in environ:
            continue
        try:
            value = parser(environ[name])
        except ConfigError as error:
            raise ConfigError(f"{name}: {error}") from error
        _set_nested(effective, path, value)
        applied.append(name)
    return effective, tuple(sorted(applied))


def _nested_string(document: Mapping[str, Any], path: tuple[str, ...]) -> str:
    current: object = document
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            raise ConfigError(f"required configuration path {'.'.join(path)} is missing")
        current = current[key]
    if not isinstance(current, str):
        raise ConfigError(f"required configuration path {'.'.join(path)} must be a string")
    return current


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _ensure_unique(values: list[object], *, label: str) -> None:
    serialized = [json.dumps(value, ensure_ascii=False, sort_keys=True) for value in values]
    if len(set(serialized)) != len(serialized):
        raise ConfigError(f"{label} must be unique")


def _validate_instruments(instruments: JsonObject) -> None:
    try:
        parse_instrument_registry(instruments)
    except InstrumentRegistryError as error:
        raise ConfigError(str(error)) from None


def _validate_news_sources(news_sources: JsonObject) -> None:
    sources = news_sources["sources"]
    if not isinstance(sources, list):
        raise ConfigError("news sources must be a list")
    ids: list[object] = []
    priorities: list[object] = []
    for source in sources:
        if not isinstance(source, dict):
            raise ConfigError("news source must be an object")
        ids.append(source["source_id"])
        priorities.append(source["priority"])
    _ensure_unique(ids, label="news source ids")
    _ensure_unique(priorities, label="news source priorities")


def _validate_cost_and_time(runtime: JsonObject, ai_profile: JsonObject) -> None:
    scheduler = runtime["scheduler"]
    approval = runtime["approval"]
    request = ai_profile["request"]
    cost = ai_profile["cost"]
    if not all(isinstance(item, dict) for item in (scheduler, approval, request, cost)):
        raise ConfigError("runtime timing and AI cost sections must be objects")
    if approval["ttl_minutes"] > scheduler["decision_cycle_minutes"]:
        raise ConfigError("approval ttl must not exceed the decision cycle")
    if request["timeout_seconds"] >= scheduler["cycle_timeout_seconds"]:
        raise ConfigError("AI request timeout must be shorter than the cycle timeout")

    one_million = Decimal("1000000")
    attempt_cost = (
        Decimal(request["max_input_tokens"])
        * Decimal(cost["input_per_million_tokens"])
        / one_million
        + Decimal(request["max_output_tokens"])
        * Decimal(cost["output_per_million_tokens"])
        / one_million
    )
    per_cycle = Decimal(cost["maximum_cost_per_cycle"])
    cycles_per_day = Decimal(-(-1440 // scheduler["decision_cycle_minutes"]))
    if attempt_cost > per_cycle:
        raise ConfigError("AI per-cycle cost cap cannot cover one maximum-size attempt")
    if per_cycle * cycles_per_day > Decimal(cost["maximum_cost_per_utc_day"]):
        raise ConfigError("AI daily cost cap cannot cover configured decision cycles")


def _validate_cross_file_contracts(
    project_root: Path,
    runtime: JsonObject,
    instruments: JsonObject,
    instrument_registry_sha256: str,
    market_capabilities: JsonObject,
    ai_profile: JsonObject,
    news_sources: JsonObject,
) -> tuple[tuple[RuntimeDependency, ...], tuple[str, ...], dict[str, str]]:
    execution = runtime["execution"]
    decision = runtime["decision"]
    risk = runtime["risk"]
    if (
        not isinstance(execution, dict)
        or not isinstance(decision, dict)
        or not isinstance(risk, dict)
    ):
        raise ConfigError("runtime execution, decision and risk sections must be objects")
    if execution["exchange"] != instruments["exchange"]:
        raise ConfigError("runtime exchange and instrument registry exchange do not match")
    if news_sources["instrument_registry_path"] != execution["instrument_registry_path"]:
        raise ConfigError("news source registry does not reference the runtime instrument registry")
    try:
        registry_model = parse_instrument_registry(
            instruments,
            source_sha256=instrument_registry_sha256,
        )
        capability_model = parse_market_capability_snapshot(
            market_capabilities,
            registry=registry_model,
        )
    except (InstrumentRegistryError, ValueError) as error:
        raise ConfigError(str(error)) from None
    if capability_model.exchange != execution["exchange"]:
        raise ConfigError("market capability exchange does not match runtime exchange")
    configured_global_leverage = Decimal(execution["futures"]["global_max_leverage"])
    if capability_model.global_max_leverage != configured_global_leverage:
        raise ConfigError("market capability global leverage does not match runtime config")

    spot = execution["spot"]
    futures = execution["futures"]
    if not isinstance(spot, dict) or not isinstance(futures, dict):
        raise ConfigError("spot and futures runtime sections must be objects")
    if spot["runtime_db_path"] == futures["runtime_db_path"]:
        raise ConfigError("spot and futures runtime databases must be distinct")
    if spot["bot_identity"] == futures["bot_identity"]:
        raise ConfigError("spot and futures bot identities must be distinct")
    _ensure_unique(
        [
            spot["api_key_env"],
            spot["api_secret_env"],
            futures["api_key_env"],
            futures["api_secret_env"],
        ],
        label="spot/futures credential environment names",
    )

    required_paths = {
        "risk_limits": _nested_string(runtime, ("risk", "risk_limits_path")),
        "prompt": _nested_string(ai_profile, ("prompt", "path")),
        "model_decision_schema": _nested_string(ai_profile, ("structured_output", "schema_path")),
        "trade_action_schema": _nested_string(
            ai_profile, ("structured_output", "runtime_action_schema_path")
        ),
    }
    required_hashes: dict[str, str] = {}
    for label, raw_path in required_paths.items():
        resolved = _resolve_repo_path(
            project_root,
            raw_path,
            label=label,
            must_exist=True,
        )
        required_hashes[label] = _sha256(resolved)
    if required_hashes["prompt"] != ai_profile["prompt"]["sha256"]:
        raise ConfigError("configured prompt sha256 does not match the prompt file")

    proposal_store_path = _nested_string(runtime, ("approval", "store_path"))
    notification_outbox_path = _nested_string(runtime, ("approval", "notification_outbox_path"))
    operational_control_path = _nested_string(runtime, ("operations", "control_store_path"))
    state_databases = {
        proposal_store_path,
        notification_outbox_path,
        operational_control_path,
        _nested_string(runtime, ("scheduler", "state_db_path")),
    }
    if len(state_databases) != 4:
        raise ConfigError("runtime state databases must be distinct")

    for label, raw_path in (
        ("scheduler state DB", _nested_string(runtime, ("scheduler", "state_db_path"))),
        (
            "scheduler snapshot directory",
            _nested_string(runtime, ("scheduler", "snapshot_directory")),
        ),
        ("risk snapshot", _nested_string(runtime, ("risk", "snapshot_path"))),
        ("proposal store", proposal_store_path),
        ("Telegram notification outbox", notification_outbox_path),
        ("operational control store", operational_control_path),
    ):
        _resolve_repo_path(project_root, raw_path, label=label, must_exist=False)

    dependencies: list[RuntimeDependency] = []
    warnings: list[str] = []
    for component, section, market in (
        ("spot", spot, MarketKind.SPOT),
        ("futures", futures, MarketKind.FUTURES),
    ):
        raw_path = section["freqtrade_config_path"]
        resolved = _resolve_repo_path(
            project_root,
            raw_path,
            label=f"{component} Freqtrade config",
            must_exist=False,
        )
        exists = resolved.is_file()
        required = bool(section["enabled"])
        dependencies.append(RuntimeDependency(component, raw_path, required, exists))
        if required and not exists:
            warnings.append(
                f"{component} execution is enabled but its Freqtrade config is "
                "a pending R1-05 dependency"
            )
        if not exists:
            continue
        try:
            instance = load_freqtrade_config_chain(
                resolved,
                config_root=project_root / "configs" / "freqtrade",
                market=market,
            )
            validate_freqtrade_instance_contract(
                instance,
                expected_bot_identity=section["bot_identity"],
                expected_db_url=section["runtime_db_path"],
                expected_pairs=capability_model.available_pairs(market),
                environment=runtime["environment"],
            )
        except FreqtradeRuntimeConfigError as error:
            raise ConfigError(f"{component} Freqtrade contract: {error}") from None
        required_hashes[f"freqtrade_{component}_merged"] = instance.merged_sha256
        for relative_path, digest in instance.source_sha256.items():
            key = relative_path.replace("/", "_").replace(".", "_")
            required_hashes[f"freqtrade_{component}_{key}"] = digest

    _validate_instruments(instruments)
    _validate_news_sources(news_sources)
    _validate_cost_and_time(runtime, ai_profile)
    return tuple(dependencies), tuple(warnings), required_hashes


def _redact(value: object, *, key: str = "") -> object:
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value]
    normalized_key = key.lower()
    if normalized_key.endswith("_env"):
        return value
    secret_names = {
        "api_key",
        "authorization",
        "credential",
        "password",
        "private_key",
        "secret",
        "access_token",
        "token",
    }
    if isinstance(value, str) and (
        normalized_key in secret_names
        or any(normalized_key.endswith(f"_{name}") for name in secret_names)
    ):
        return "<redacted>"
    return value


def load_effective_config(
    project_root: str | Path,
    runtime_config: str | Path = DEFAULT_RUNTIME_CONFIG,
    *,
    environ: Mapping[str, str] | None = None,
) -> EffectiveConfig:
    """加载 R1-01 有效配置；只读取显式白名单覆盖，不读取 secret 环境变量。"""

    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ConfigError("project root does not exist or is not a directory")
    runtime_path = _resolve_repo_path(
        root,
        runtime_config,
        label="runtime config",
        must_exist=True,
    )
    raw_runtime = _load_yaml(runtime_path, label="runtime config")
    runtime, applied_overrides = _apply_environment_overrides(
        raw_runtime,
        os.environ if environ is None else environ,
    )

    schemas, registry = _load_schema_registry(root)
    _validate_schema(
        runtime,
        schema_name="runtime-config.schema.yaml",
        label="runtime config",
        schemas=schemas,
        registry=registry,
    )

    document_specs = {
        "instruments": (
            _nested_string(runtime, ("execution", "instrument_registry_path")),
            "instrument-registry.schema.yaml",
            "yaml",
        ),
        "market_capabilities": (
            _nested_string(runtime, ("execution", "market_capability_snapshot_path")),
            "market-capability-snapshot.schema.yaml",
            "json",
        ),
        "ai_profile": (
            _nested_string(runtime, ("decision", "ai_profile_path")),
            "ai-profile.schema.yaml",
            "yaml",
        ),
        "news_sources": (
            _nested_string(runtime, ("decision", "news_source_registry_path")),
            "news-source-registry.schema.yaml",
            "yaml",
        ),
    }
    documents: dict[str, JsonObject] = {}
    source_hashes = {"runtime": _sha256(runtime_path)}
    for label, (raw_path, schema_name, file_format) in document_specs.items():
        path = _resolve_repo_path(root, raw_path, label=label, must_exist=True)
        document = (
            _load_json(path, label=label)
            if file_format == "json"
            else _load_yaml(path, label=label)
        )
        _validate_schema(
            document,
            schema_name=schema_name,
            label=label,
            schemas=schemas,
            registry=registry,
        )
        documents[label] = document
        source_hashes[label] = _sha256(path)

    dependencies, warnings, required_hashes = _validate_cross_file_contracts(
        root,
        runtime,
        documents["instruments"],
        source_hashes["instruments"],
        documents["market_capabilities"],
        documents["ai_profile"],
        documents["news_sources"],
    )
    source_hashes.update(required_hashes)
    effective_payload = {
        "runtime": runtime,
        "instruments": documents["instruments"],
        "market_capabilities": documents["market_capabilities"],
        "ai_profile": documents["ai_profile"],
        "news_sources": documents["news_sources"],
        "runtime_dependency_sha256": dict(sorted(required_hashes.items())),
    }
    return EffectiveConfig(
        project_root=root,
        runtime=runtime,
        instruments=documents["instruments"],
        market_capabilities=documents["market_capabilities"],
        ai_profile=documents["ai_profile"],
        news_sources=documents["news_sources"],
        source_sha256=source_hashes,
        effective_sha256=_canonical_sha256(effective_payload),
        applied_overrides=applied_overrides,
        runtime_dependencies=dependencies,
        warnings=warnings,
    )
