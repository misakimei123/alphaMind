import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from alphamind.runtime_db import (
    RecoveryPhase,
    backup_sqlite_runtime_database,
    evaluate_recovery,
    inspect_sqlite_runtime_database,
    load_runtime_database_contract,
    load_runtime_schema_manifest,
    restore_sqlite_runtime_database,
)

PROJECT_ROOT = Path(__file__).parents[2]
CONTRACT_PATH = PROJECT_ROOT / "configs/common/runtime-db-contract.toml"
MANIFEST_PATH = PROJECT_ROOT / "configs/common/freqtrade-runtime-schema-2026.6.json"


def create_runtime_database(
    path: Path,
    *,
    open_trade: bool = True,
    open_order: bool = True,
    filled_order: bool = True,
) -> None:
    manifest = load_runtime_schema_manifest(MANIFEST_PATH)
    connection = sqlite3.connect(path)
    for table_name, columns in manifest.tables.items():
        definitions = ", ".join(f'"{column}" TEXT' for column in columns)
        connection.execute(f'CREATE TABLE "{table_name}" ({definitions})')
    if open_trade:
        connection.execute("INSERT INTO trades (id, is_open) VALUES ('1', '1')")
    if open_order:
        connection.execute("INSERT INTO orders (id, ft_is_open, filled) VALUES ('1', '1', '0.25')")
    if filled_order:
        connection.execute("INSERT INTO orders (id, ft_is_open, filled) VALUES ('2', '0', '1.0')")
    connection.commit()
    connection.close()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_runtime_contract_freezes_isolated_environments_and_migration_owner() -> None:
    contract = load_runtime_database_contract(CONTRACT_PATH)
    manifest = load_runtime_schema_manifest(MANIFEST_PATH)
    runtime_lock = (PROJECT_ROOT / "configs/common/runtime-versions.toml").read_text(
        encoding="utf-8"
    )
    assert contract.freqtrade_version == "2026.6"
    assert manifest.freqtrade_version == contract.freqtrade_version
    assert manifest.freqtrade_source_commit in runtime_lock
    assert contract.migration_owner == "freqtrade"
    assert contract.rpo_seconds == 300
    assert contract.rto_seconds == 60
    assert tuple(contract.environments) == (
        "backtest",
        "spot_dry_run",
        "futures_dry_run",
        "replay",
        "testnet_contract",
        "spot_live_canary",
        "futures_live_canary",
    )
    sqlite_urls = [
        environment.db_url
        for environment in contract.environments.values()
        if environment.backend == "sqlite"
    ]
    assert len(sqlite_urls) == len(set(sqlite_urls)) == 5
    live_environments = [
        contract.environments["spot_live_canary"],
        contract.environments["futures_live_canary"],
    ]
    assert len({environment.db_url_env for environment in live_environments}) == 2
    assert len({environment.database for environment in live_environments}) == 2
    assert len({environment.schema for environment in live_environments}) == 2
    for live in live_environments:
        assert live.db_url is None
        assert live.owner_role != live.watchdog_role


def test_freqtrade_configs_match_runtime_contract_without_live_credentials() -> None:
    contract = load_runtime_database_contract(CONTRACT_PATH)
    config_names = {
        "backtest": "backtest.json",
        "spot_dry_run": "spot.dry-run.json",
        "futures_dry_run": "futures.dry-run.json",
        "replay": "replay.json",
        "testnet_contract": "contract.json",
    }
    for environment_name, config_name in config_names.items():
        config = json.loads(
            (PROJECT_ROOT / "configs/freqtrade" / config_name).read_text(encoding="utf-8")
        )
        assert config["db_url"] == contract.environments[environment_name].db_url
    live_names = ("spot.live.template.json", "futures.live.template.json")
    live_urls: set[str] = set()
    for name in live_names:
        live = json.loads((PROJECT_ROOT / "configs/freqtrade" / name).read_text(encoding="utf-8"))
        assert live["db_url"].startswith("postgresql+psycopg://<")
        assert "sqlite" not in live["db_url"]
        assert "key" not in json.dumps(live).lower()
        assert "secret" not in json.dumps(live).lower()
        live_urls.add(live["db_url"])
    assert len(live_urls) == 2


def test_sqlite_inspection_is_query_only_and_detects_schema_drift(tmp_path: Path) -> None:
    manifest = load_runtime_schema_manifest(MANIFEST_PATH)
    database = tmp_path / "runtime.sqlite"
    create_runtime_database(database)
    before = file_sha256(database)

    inspection = inspect_sqlite_runtime_database(database, manifest)
    assert inspection.healthy is True
    assert inspection.query_only is True
    assert inspection.schema_sha256 == manifest.schema_sha256
    assert (inspection.open_trades, inspection.open_orders, inspection.filled_orders) == (1, 1, 2)
    assert file_sha256(database) == before

    connection = sqlite3.connect(database)
    connection.execute("ALTER TABLE trades ADD COLUMN unexpected TEXT")
    connection.commit()
    connection.close()
    drift = inspect_sqlite_runtime_database(database, manifest)
    assert drift.healthy is False
    assert drift.reason_codes == ("runtime_db_schema_mismatch",)


def test_missing_or_corrupt_runtime_database_fails_closed(tmp_path: Path) -> None:
    manifest = load_runtime_schema_manifest(MANIFEST_PATH)
    missing = inspect_sqlite_runtime_database(tmp_path / "missing.sqlite", manifest)
    assert missing.healthy is False
    assert missing.reason_codes == ("runtime_db_missing",)

    corrupt_path = tmp_path / "corrupt.sqlite"
    corrupt_path.write_bytes(b"not a sqlite database")
    corrupt = inspect_sqlite_runtime_database(corrupt_path, manifest)
    assert corrupt.healthy is False
    assert corrupt.reason_codes == ("runtime_db_unavailable",)


def test_sqlite_backup_restore_and_rollback_preserve_complete_runtime_state(
    tmp_path: Path,
) -> None:
    manifest = load_runtime_schema_manifest(MANIFEST_PATH)
    source = tmp_path / "source.sqlite"
    backup = tmp_path / "backup.sqlite"
    target = tmp_path / "target.sqlite"
    rollback = tmp_path / "rollback.sqlite"
    create_runtime_database(source)
    create_runtime_database(target, open_trade=False, open_order=False, filled_order=False)

    started = time.perf_counter()
    backup_result = backup_sqlite_runtime_database(source, backup, manifest)
    assert backup_result.inspection.healthy is True
    assert backup_result.sha256 == file_sha256(backup)
    with pytest.raises(FileExistsError, match="already exists"):
        backup_sqlite_runtime_database(source, backup, manifest)
    with pytest.raises(RuntimeError, match="must be stopped"):
        restore_sqlite_runtime_database(backup, target, rollback, manifest, freqtrade_stopped=False)

    restore_result = restore_sqlite_runtime_database(
        backup, target, rollback, manifest, freqtrade_stopped=True
    )
    assert restore_result.inspection.healthy is True
    assert (restore_result.inspection.open_trades, restore_result.inspection.open_orders) == (1, 1)
    assert inspect_sqlite_runtime_database(rollback, manifest).open_trades == 0
    assert time.perf_counter() - started < 60


def test_recovery_order_blocks_entry_until_runtime_reconcile_and_safe_disposition(
    tmp_path: Path,
) -> None:
    manifest = load_runtime_schema_manifest(MANIFEST_PATH)
    database = tmp_path / "runtime.sqlite"
    create_runtime_database(database)
    healthy = inspect_sqlite_runtime_database(database, manifest)
    unhealthy = inspect_sqlite_runtime_database(tmp_path / "missing.sqlite", manifest)

    assert (
        evaluate_recovery(
            healthy,
            exchange_facts_available=False,
            freqtrade_reconciled=False,
            safe_disposition_complete=False,
            audit_available=False,
        ).phase
        is RecoveryPhase.EXCHANGE_FACTS_REQUIRED
    )
    runtime_failed = evaluate_recovery(
        unhealthy,
        exchange_facts_available=True,
        freqtrade_reconciled=False,
        safe_disposition_complete=False,
        audit_available=False,
    )
    assert runtime_failed.phase is RecoveryPhase.RUNTIME_RESTORE_REQUIRED
    assert runtime_failed.entry_allowed is False
    assert runtime_failed.alert_required is True
    reconcile = evaluate_recovery(
        healthy,
        exchange_facts_available=True,
        freqtrade_reconciled=False,
        safe_disposition_complete=False,
        audit_available=False,
    )
    assert reconcile.phase is RecoveryPhase.FREQTRADE_RECONCILE_REQUIRED
    assert reconcile.safe_exit_allowed is True
    disposition = evaluate_recovery(
        healthy,
        exchange_facts_available=True,
        freqtrade_reconciled=True,
        safe_disposition_complete=False,
        audit_available=False,
    )
    assert disposition.phase is RecoveryPhase.SAFE_DISPOSITION
    assert disposition.entry_allowed is False


def test_audit_outage_does_not_reverse_runtime_recovery_or_block_safe_exit(
    tmp_path: Path,
) -> None:
    manifest = load_runtime_schema_manifest(MANIFEST_PATH)
    database = tmp_path / "runtime.sqlite"
    create_runtime_database(database)
    inspection = inspect_sqlite_runtime_database(database, manifest)
    degraded = evaluate_recovery(
        inspection,
        exchange_facts_available=True,
        freqtrade_reconciled=True,
        safe_disposition_complete=True,
        audit_available=False,
    )
    assert degraded.phase is RecoveryPhase.RUNTIME_READY_AUDIT_DEGRADED
    assert degraded.entry_allowed is True
    assert degraded.safe_exit_allowed is True
    assert degraded.audit_backfill_allowed is False
    assert degraded.reason_codes == ("audit_backfill_pending",)


def test_postgresql_template_grants_watchdog_only_read_access() -> None:
    sql = (PROJECT_ROOT / "configs/postgres/live-runtime-roles.sql").read_text(encoding="utf-8")
    normalized = " ".join(sql.lower().split())
    assert 'alter role :"watchdog_role" set default_transaction_read_only = on' in normalized
    assert 'grant usage on schema :"runtime_schema" to :"watchdog_role"' in normalized
    assert 'grant select on all tables in schema :"runtime_schema"' in normalized
    assert 'revoke create on schema :"runtime_schema" from :"watchdog_role"' in normalized
    assert "grant insert" not in normalized
    assert "grant update" not in normalized
    assert "grant delete" not in normalized
