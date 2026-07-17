"""运行 P3-03 Audit Writer sidecar。"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from alphamind.audit import AuditOutbox, AuditWriter, SQLiteAuditSink, load_audit_storage_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/freqtrade/common/audit-outbox.toml"),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not 0.1 <= args.poll_seconds <= 60:
        raise ValueError("poll-seconds must be in [0.1, 60]")
    storage = load_audit_storage_config(args.config)
    outbox = AuditOutbox(storage.outbox_path)
    sink = SQLiteAuditSink(storage.audit_db_path)
    writer = AuditWriter(outbox, sink)
    try:
        while True:
            now = datetime.now(UTC)
            result = writer.run_once(now=now)
            metrics = outbox.metrics(now=now)
            print(
                json.dumps(
                    {
                        "claimed": result.claimed,
                        "cleaned": result.cleaned,
                        "dead_lettered": result.dead_lettered,
                        "delivered": result.delivered,
                        "entry_backpressure": metrics.entry_backpressure,
                        "file_bytes": metrics.file_bytes,
                        "in_flight": metrics.in_flight,
                        "oldest_unconfirmed_seconds": metrics.oldest_unconfirmed_seconds,
                        "pending": metrics.pending,
                        "retried": result.retried,
                        "status": "ok",
                        "warning": metrics.warning,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if args.once:
                return 0
            time.sleep(args.poll_seconds)
    finally:
        sink.close()
        outbox.close()


if __name__ == "__main__":
    raise SystemExit(main())
