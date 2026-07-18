"""加载并验证 P3-04 Runtime DB 环境与 schema 冻结合同。"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENVIRONMENTS = (
    "backtest",
    "spot_dry_run",
    "futures_dry_run",
    "replay",
    "testnet_contract",
    "spot_live_canary",
    "futures_live_canary",
)
POSTGRESQL_ENVIRONMENTS = {"spot_live_canary", "futures_live_canary"}
RECOVERY_ORDER = (
    "exchange_facts",
    "runtime_db",
    "freqtrade_reconcile",
    "safe_disposition",
    "audit_backfill",
)
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
IDENTITY = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
ROLE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    name: str
    backend: str
    identity: str
    db_url: str | None
    db_url_env: str | None
    database: str | None
    schema: str | None
    owner_role: str | None
    watchdog_role: str | None


@dataclass(frozen=True, slots=True)
class RuntimeDatabaseContract:
    freqtrade_version: str
    migration_owner: str
    schema_manifest_path: Path
    rpo_seconds: int
    rto_seconds: int
    recovery_order: tuple[str, ...]
    environments: dict[str, RuntimeEnvironment]


@dataclass(frozen=True, slots=True)
class RuntimeSchemaManifest:
    freqtrade_version: str
    freqtrade_source_commit: str
    tables: dict[str, tuple[str, ...]]
    schema_sha256: str


def _mapping(value: object, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{location} must be a table")
    return value


def _string(document: dict[str, Any], key: str, location: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location}.{key} must be a non-empty string")
    return value


def load_runtime_database_contract(path: str | Path) -> RuntimeDatabaseContract:
    config_path = Path(path)
    with config_path.open("rb") as stream:
        document = tomllib.load(stream)
    expected = {
        "schema_version",
        "freqtrade_version",
        "schema_manifest_path",
        "migration_owner",
        "rpo_seconds",
        "rto_seconds",
        "recovery_order",
        "environments",
    }
    if set(document) != expected or document.get("schema_version") != 1:
        raise ValueError("runtime DB contract root keys or schema_version are invalid")
    if document.get("migration_owner") != "freqtrade":
        raise ValueError("only Freqtrade may own Runtime DB migrations")
    if document.get("rpo_seconds") != 300 or document.get("rto_seconds") != 60:
        raise ValueError("Runtime DB RPO/RTO must remain 300/60 seconds")
    raw_order = document.get("recovery_order")
    if not isinstance(raw_order, list) or tuple(raw_order) != RECOVERY_ORDER:
        raise ValueError("runtime recovery order differs from ADR-0007")

    raw_environments = _mapping(document.get("environments"), "environments")
    if set(raw_environments) != set(ENVIRONMENTS):
        raise ValueError("runtime DB environments must match the frozen isolated layers")
    environments: dict[str, RuntimeEnvironment] = {}
    sqlite_urls: set[str] = set()
    identities: set[str] = set()
    for name in ENVIRONMENTS:
        raw = _mapping(raw_environments[name], f"environments.{name}")
        backend = _string(raw, "backend", f"environments.{name}")
        identity = _string(raw, "identity", f"environments.{name}")
        if IDENTITY.fullmatch(identity) is None or identity in identities:
            raise ValueError("runtime DB identities must be valid and unique")
        identities.add(identity)
        if backend == "sqlite":
            if set(raw) != {"backend", "db_url", "identity"}:
                raise ValueError(f"SQLite environment {name} has unexpected keys")
            db_url = _string(raw, "db_url", f"environments.{name}")
            if not db_url.startswith("sqlite:////") or db_url in sqlite_urls:
                raise ValueError("SQLite Runtime DB URLs must be absolute and unique")
            sqlite_urls.add(db_url)
            environment = RuntimeEnvironment(
                name, backend, identity, db_url, None, None, None, None, None
            )
        elif backend == "postgresql" and name in POSTGRESQL_ENVIRONMENTS:
            required = {
                "backend",
                "db_url_env",
                "database",
                "schema",
                "owner_role",
                "watchdog_role",
                "identity",
            }
            if set(raw) != required:
                raise ValueError("live PostgreSQL environment has unexpected keys")
            db_url_env = _string(raw, "db_url_env", f"environments.{name}")
            database = _string(raw, "database", f"environments.{name}")
            schema = _string(raw, "schema", f"environments.{name}")
            owner_role = _string(raw, "owner_role", f"environments.{name}")
            watchdog_role = _string(raw, "watchdog_role", f"environments.{name}")
            if ENV_NAME.fullmatch(db_url_env) is None:
                raise ValueError("live db_url_env is invalid")
            if any(
                ROLE.fullmatch(value) is None
                for value in (database, schema, owner_role, watchdog_role)
            ):
                raise ValueError("live database/schema/role names are invalid")
            if owner_role == watchdog_role:
                raise ValueError("Freqtrade owner and watchdog roles must differ")
            environment = RuntimeEnvironment(
                name,
                backend,
                identity,
                None,
                db_url_env,
                database,
                schema,
                owner_role,
                watchdog_role,
            )
        else:
            raise ValueError(f"unsupported Runtime DB backend for {name}")
        environments[name] = environment

    return RuntimeDatabaseContract(
        freqtrade_version=_string(document, "freqtrade_version", "root"),
        migration_owner="freqtrade",
        schema_manifest_path=Path(_string(document, "schema_manifest_path", "root")),
        rpo_seconds=300,
        rto_seconds=60,
        recovery_order=RECOVERY_ORDER,
        environments=environments,
    )


def load_runtime_schema_manifest(path: str | Path) -> RuntimeSchemaManifest:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {
        "freqtrade_source_commit",
        "freqtrade_version",
        "tables",
    }:
        raise ValueError("runtime schema manifest root is invalid")
    raw_tables = _mapping(document["tables"], "tables")
    tables: dict[str, tuple[str, ...]] = {}
    for table_name, raw_columns in raw_tables.items():
        if not isinstance(table_name, str) or not isinstance(raw_columns, list):
            raise TypeError("runtime schema table definition is invalid")
        if not all(isinstance(column, str) for column in raw_columns):
            raise TypeError("runtime schema columns must be strings")
        columns = tuple(raw_columns)
        if columns != tuple(sorted(set(columns))):
            raise ValueError("runtime schema columns must be sorted and unique")
        tables[table_name] = columns
    canonical = json.dumps(tables, sort_keys=True, separators=(",", ":")).encode()
    return RuntimeSchemaManifest(
        freqtrade_version=_string(document, "freqtrade_version", "manifest"),
        freqtrade_source_commit=_string(document, "freqtrade_source_commit", "manifest"),
        tables=tables,
        schema_sha256=hashlib.sha256(canonical).hexdigest(),
    )
