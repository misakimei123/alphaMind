"""SQLite Runtime DB 的严格只读检查和停机整库 backup/restore。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from alphamind.runtime_db.contract import RuntimeSchemaManifest

TABLES_SQL = (
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
)
OPEN_TRADES_SQL = "SELECT COUNT(*) FROM trades WHERE is_open = 1"
OPEN_ORDERS_SQL = "SELECT COUNT(*) FROM orders WHERE ft_is_open = 1"
FILLED_ORDERS_SQL = "SELECT COUNT(*) FROM orders WHERE filled > 0"


@dataclass(frozen=True, slots=True)
class RuntimeDatabaseInspection:
    healthy: bool
    reason_codes: tuple[str, ...]
    schema_sha256: str | None
    open_trades: int
    open_orders: int
    filled_orders: int
    query_only: bool


@dataclass(frozen=True, slots=True)
class SQLiteBackupResult:
    path: Path
    sha256: str
    size_bytes: int
    completed_at_utc: datetime
    inspection: RuntimeDatabaseInspection


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _schema_tables(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    tables: dict[str, tuple[str, ...]] = {}
    for (table_name,) in connection.execute(TABLES_SQL):
        if not isinstance(table_name, str):
            raise TypeError("SQLite table name must be a string")
        # 表名只能来自已经冻结的 sqlite_master 结果，不接受外部输入。
        escaped = table_name.replace('"', '""')
        columns = tuple(
            sorted(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{escaped}")'))
        )
        tables[table_name] = columns
    return tables


def _schema_sha256(tables: dict[str, tuple[str, ...]]) -> str:
    canonical = json.dumps(tables, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def inspect_sqlite_runtime_database(
    path: str | Path,
    manifest: RuntimeSchemaManifest,
) -> RuntimeDatabaseInspection:
    """只执行登记的 PRAGMA/SELECT；缺失、损坏或 schema 漂移均 fail-closed。"""

    connection: sqlite3.Connection | None = None
    try:
        connection = _readonly_connection(Path(path))
        query_only = connection.execute("PRAGMA query_only").fetchone()[0] == 1
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if not query_only:
            return RuntimeDatabaseInspection(
                False, ("runtime_db_not_read_only",), None, 0, 0, 0, False
            )
        if quick_check != "ok":
            return RuntimeDatabaseInspection(
                False, ("runtime_db_integrity_failed",), None, 0, 0, 0, True
            )
        tables = _schema_tables(connection)
        schema_hash = _schema_sha256(tables)
        if tables != manifest.tables or schema_hash != manifest.schema_sha256:
            return RuntimeDatabaseInspection(
                False, ("runtime_db_schema_mismatch",), schema_hash, 0, 0, 0, True
            )
        return RuntimeDatabaseInspection(
            True,
            (),
            schema_hash,
            int(connection.execute(OPEN_TRADES_SQL).fetchone()[0]),
            int(connection.execute(OPEN_ORDERS_SQL).fetchone()[0]),
            int(connection.execute(FILLED_ORDERS_SQL).fetchone()[0]),
            True,
        )
    except FileNotFoundError:
        return RuntimeDatabaseInspection(False, ("runtime_db_missing",), None, 0, 0, 0, False)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return RuntimeDatabaseInspection(False, ("runtime_db_unavailable",), None, 0, 0, 0, False)
    finally:
        if connection is not None:
            connection.close()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    # Windows 对只读 descriptor 的 fsync 返回 EBADF；rb+ 不改变内容但可提交文件缓冲。
    with path.open("rb+") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def backup_sqlite_runtime_database(
    source: str | Path,
    destination: str | Path,
    manifest: RuntimeSchemaManifest,
) -> SQLiteBackupResult:
    """使用 SQLite online backup 复制完整 Freqtrade DB，不解释或修改 Trade/Order。"""

    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("Runtime DB backup destination must differ from source")
    if destination_path.exists():
        raise FileExistsError("Runtime DB backup destination already exists")
    inspection = inspect_sqlite_runtime_database(source_path, manifest)
    if not inspection.healthy:
        raise RuntimeError(f"Runtime DB backup rejected: {inspection.reason_codes}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(f".{destination_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        source_connection = _readonly_connection(source_path)
        destination_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(destination_connection)
            destination_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            destination_connection.commit()
        finally:
            destination_connection.close()
            source_connection.close()
        _fsync_file(temporary)
        copied_inspection = inspect_sqlite_runtime_database(temporary, manifest)
        if not copied_inspection.healthy:
            raise RuntimeError("copied Runtime DB failed schema or integrity verification")
        os.replace(temporary, destination_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return SQLiteBackupResult(
        destination_path,
        _file_sha256(destination_path),
        destination_path.stat().st_size,
        datetime.now(UTC),
        copied_inspection,
    )


def restore_sqlite_runtime_database(
    backup: str | Path,
    target: str | Path,
    rollback_backup: str | Path,
    manifest: RuntimeSchemaManifest,
    *,
    freqtrade_stopped: bool,
) -> SQLiteBackupResult:
    """停机状态下原子替换整库，并先保存可回退的现库副本。"""

    if not freqtrade_stopped:
        raise RuntimeError("Freqtrade must be stopped before Runtime DB restore")
    backup_path = Path(backup)
    target_path = Path(target)
    rollback_path = Path(rollback_backup)
    resolved = {path.resolve() for path in (backup_path, target_path, rollback_path)}
    if len(resolved) != 3:
        raise ValueError("backup, target and rollback paths must be distinct")
    backup_inspection = inspect_sqlite_runtime_database(backup_path, manifest)
    if not backup_inspection.healthy:
        raise RuntimeError("Runtime DB restore source failed verification")
    for suffix in ("-wal", "-shm"):
        if Path(f"{target_path}{suffix}").exists():
            raise RuntimeError("target SQLite sidecars exist; stop and checkpoint Freqtrade first")
    if target_path.exists():
        backup_sqlite_runtime_database(target_path, rollback_path, manifest)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.restore")
    try:
        shutil.copyfile(backup_path, temporary)
        _fsync_file(temporary)
        restored_inspection = inspect_sqlite_runtime_database(temporary, manifest)
        if not restored_inspection.healthy:
            raise RuntimeError("prepared Runtime DB restore failed verification")
        os.replace(temporary, target_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return SQLiteBackupResult(
        target_path,
        _file_sha256(target_path),
        target_path.stat().st_size,
        datetime.now(UTC),
        restored_inspection,
    )
