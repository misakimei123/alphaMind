"""从有效配置执行一次只读新闻采集并输出结构化结果。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from alphamind.config import ConfigError, load_effective_config
from alphamind.news import NewsCollector, NewsStateError, NewsStateStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=Path("configs/alphamind/runtime.example.yaml"),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path("user_data/state/news-cursors.json"),
    )
    parser.add_argument("--force", action="store_true", help="ignore source poll intervals")
    parser.add_argument("--pretty", action="store_true")
    return parser


def _under_root(project_root: Path, path: Path) -> Path:
    resolved_root = project_root.resolve()
    candidate = path if path.is_absolute() else resolved_root / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        raise NewsStateError("news state path must stay inside the project root") from None
    return resolved


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    now_utc: datetime | None = None,
) -> int:
    args = _parser().parse_args(argv)
    project_root = args.project_root.resolve()
    try:
        effective = load_effective_config(
            project_root,
            args.runtime_config,
            environ=environ,
        )
        state_path = _under_root(project_root, args.state_path)
        result = NewsCollector(
            effective,
            state_store=NewsStateStore(state_path),
            environ=environ,
        ).collect(now_utc=now_utc or datetime.now(UTC), force=args.force)
    except (ConfigError, NewsStateError, ValueError) as error:
        print(
            json.dumps({"status": "invalid", "error": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except RuntimeError:
        print(
            json.dumps({"status": "failed", "error": "news collection failed"}, sort_keys=True),
            file=sys.stderr,
        )
        return 3

    output = result.to_dict()
    output["status"] = "ok"
    print(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
