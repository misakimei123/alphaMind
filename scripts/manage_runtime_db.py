"""验证、备份或恢复由 Freqtrade 拥有的完整 SQLite Runtime DB。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from alphamind.runtime_db import (
    backup_sqlite_runtime_database,
    inspect_sqlite_runtime_database,
    load_runtime_schema_manifest,
    restore_sqlite_runtime_database,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "configs/common/freqtrade-runtime-schema-2026.6.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--database", type=Path, required=True)

    backup = commands.add_parser("backup")
    backup.add_argument("--source", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)

    restore = commands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--target", type=Path, required=True)
    restore.add_argument("--rollback-backup", type=Path, required=True)
    restore.add_argument("--confirm-freqtrade-stopped", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    manifest = load_runtime_schema_manifest(args.manifest)
    if args.command == "verify":
        inspection = inspect_sqlite_runtime_database(args.database, manifest)
        verify_payload = {
            "healthy": inspection.healthy,
            "reason_codes": inspection.reason_codes,
            "schema_sha256": inspection.schema_sha256,
            "open_trades": inspection.open_trades,
            "open_orders": inspection.open_orders,
            "filled_orders": inspection.filled_orders,
            "query_only": inspection.query_only,
        }
        print(json.dumps(verify_payload, sort_keys=True))
        return 0 if inspection.healthy else 1
    if args.command == "backup":
        operation_result = backup_sqlite_runtime_database(args.source, args.destination, manifest)
    else:
        operation_result = restore_sqlite_runtime_database(
            args.backup,
            args.target,
            args.rollback_backup,
            manifest,
            freqtrade_stopped=args.confirm_freqtrade_stopped,
        )
    print(
        json.dumps(
            {
                "healthy": operation_result.inspection.healthy,
                "path": str(operation_result.path),
                "schema_sha256": operation_result.inspection.schema_sha256,
                "sha256": operation_result.sha256,
                "size_bytes": operation_result.size_bytes,
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
