"""打印经过校验、应用环境覆盖并自动脱敏的 alphaMind 有效配置。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from alphamind.config.loader import DEFAULT_RUNTIME_CONFIG, ConfigError, load_effective_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--runtime-config", type=Path, default=DEFAULT_RUNTIME_CONFIG)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--require-execution-ready",
        action="store_true",
        help="return status 3 when a deferred Freqtrade runtime config is still missing",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        effective = load_effective_config(
            args.project_root,
            args.runtime_config,
            environ=environ,
        )
    except ConfigError as error:
        print(
            json.dumps({"status": "invalid", "error": str(error)}, sort_keys=True), file=sys.stderr
        )
        return 2

    output = effective.to_safe_dict()
    output["status"] = "ok" if effective.execution_ready else "configuration_valid"
    print(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    if args.require_execution_ready and not effective.execution_ready:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
