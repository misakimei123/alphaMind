"""运行一次手工只读周期、持续调度，或查看周期状态。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from alphamind.config import ConfigError, load_effective_config
from alphamind.scheduler.core import (
    CycleRunner,
    CycleScheduler,
    CycleStateStore,
    CycleStatus,
    CycleTrigger,
)
from alphamind.scheduler.snapshot import build_read_only_snapshot_handler


def _mapping(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{location} must be an object")
    return value


def _integer(value: object, location: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{location} must be an integer")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=Path("configs/alphamind/runtime.example.yaml"),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    try:
        effective = load_effective_config(
            args.project_root.resolve(),
            args.runtime_config,
            environ={},
        )
        scheduler_config = _mapping(effective.runtime["scheduler"], "scheduler")
        state_path = effective.project_root / str(scheduler_config["state_db_path"])
        snapshot_directory = effective.project_root / str(scheduler_config["snapshot_directory"])
        store = CycleStateStore(state_path)
        if args.status:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cycles": [record.to_dict() for record in store.recent(args.limit)],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0

        runner = CycleRunner(
            state_store=store,
            lock_path=state_path.with_suffix(".lock"),
            snapshot_directory=snapshot_directory,
            timeout_seconds=float(
                _integer(scheduler_config["cycle_timeout_seconds"], "cycle_timeout_seconds")
            ),
            effective_config_sha256=effective.effective_sha256,
            handler=build_read_only_snapshot_handler(effective),
        )
        if args.daemon:
            scheduler = CycleScheduler(
                runner,
                interval_minutes=_integer(
                    scheduler_config["decision_cycle_minutes"], "decision_cycle_minutes"
                ),
            )
            try:
                scheduler.run_forever()
            except KeyboardInterrupt:
                return 0
            return 0

        result = runner.run(CycleTrigger.MANUAL)
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return {
            CycleStatus.ABANDONED: 5,
            CycleStatus.SUCCEEDED: 0,
            CycleStatus.SKIPPED_OVERLAP: 2,
            CycleStatus.FAILED: 3,
            CycleStatus.TIMED_OUT: 4,
            CycleStatus.RUNNING: 5,
        }[result.status]
    except (ConfigError, OSError, RuntimeError, TypeError, ValueError) as error:
        print(
            json.dumps(
                {"status": "error", "error_class": type(error).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
