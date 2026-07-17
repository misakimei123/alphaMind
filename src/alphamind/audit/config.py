"""P3-03 outbox/writer 路径与审计 provenance 配置。"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alphamind.audit.events import AuditProvenance

SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")
INSTANCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
STRATEGY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
SEMVER_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class AuditStorageConfig:
    outbox_path: Path
    audit_db_path: Path
    producer_instance_id: str


@dataclass(frozen=True, slots=True)
class AuditRuntimeConfig:
    storage: AuditStorageConfig
    provenance: AuditProvenance


def _require_string(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"audit config {key} must be a non-empty string")
    return value


def _load_document(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("rb") as stream:
        document = tomllib.load(stream)
    if document.get("schema_version") != 1:
        raise ValueError("audit config schema_version must be 1")
    return document


def load_audit_storage_config(path: str | Path) -> AuditStorageConfig:
    document = _load_document(path)
    expected = {
        "schema_version",
        "outbox_path",
        "audit_db_path",
        "producer_instance_id",
        "project_commit_env",
        "strategy_id",
        "strategy_version",
        "strategy_config_path",
        "runtime_lock_path",
    }
    if set(document) != expected:
        raise ValueError(f"audit config keys must be exactly {sorted(expected)}")
    producer_instance_id = _require_string(document, "producer_instance_id")
    if INSTANCE_PATTERN.fullmatch(producer_instance_id) is None:
        raise ValueError("audit producer_instance_id is invalid")
    outbox_path = Path(_require_string(document, "outbox_path"))
    audit_db_path = Path(_require_string(document, "audit_db_path"))
    if outbox_path == audit_db_path:
        raise ValueError("outbox and Audit DB must be independent files")
    return AuditStorageConfig(outbox_path, audit_db_path, producer_instance_id)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if SHA256_PATTERN.fullmatch(digest) is None:
        raise RuntimeError("unexpected SHA-256 result")
    return digest


def load_audit_runtime_config(path: str | Path) -> AuditRuntimeConfig:
    document = _load_document(path)
    storage = load_audit_storage_config(path)
    project_commit_env = _require_string(document, "project_commit_env")
    if ENV_NAME_PATTERN.fullmatch(project_commit_env) is None:
        raise ValueError("audit project_commit_env is invalid")
    project_commit = os.environ.get(project_commit_env, "").lower()
    if COMMIT_PATTERN.fullmatch(project_commit) is None:
        # provenance 不完整时宁可阻止启动，也不能写入伪造或不可追溯的审计事实。
        raise ValueError(f"{project_commit_env} must contain the deployed 40-character commit")
    strategy_id = _require_string(document, "strategy_id")
    strategy_version = _require_string(document, "strategy_version")
    if STRATEGY_ID_PATTERN.fullmatch(strategy_id) is None:
        raise ValueError("audit strategy_id is invalid")
    if SEMVER_PATTERN.fullmatch(strategy_version) is None:
        raise ValueError("audit strategy_version is invalid")
    strategy_config_path = Path(_require_string(document, "strategy_config_path"))
    runtime_lock_path = Path(_require_string(document, "runtime_lock_path"))
    provenance = AuditProvenance(
        project_commit=project_commit,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        strategy_config_sha256=_file_sha256(strategy_config_path),
        runtime_lock_sha256=_file_sha256(runtime_lock_path),
    )
    return AuditRuntimeConfig(storage, provenance)
