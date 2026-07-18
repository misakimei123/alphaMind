"""生成或核对由 Instrument Registry 派生的 Freqtrade pairlist。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from alphamind.config import MarketKind, load_effective_config
from alphamind.config.freqtrade import render_freqtrade_instrument_overlay


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=Path("configs/alphamind/runtime.example.yaml"),
    )
    parser.add_argument(
        "--market",
        choices=("all", *tuple(MarketKind)),
        default="all",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    project_root = args.project_root.resolve()
    effective = load_effective_config(
        project_root,
        args.runtime_config,
        environ={},
    )
    if args.market == "all" and args.output is not None:
        parser.error("--output can only be used with one market")
    markets = tuple(MarketKind) if args.market == "all" else (MarketKind(args.market),)
    for market in markets:
        output_path = args.output or Path(
            f"configs/freqtrade/{market.value}-instruments.generated.json"
        )
        if not output_path.is_absolute():
            output_path = project_root / output_path
        output_path = output_path.resolve()
        if not output_path.is_relative_to(project_root):
            parser.error("output must stay inside the project root")
        expected = render_freqtrade_instrument_overlay(
            effective.instrument_registry,
            market,
            effective.market_capability_snapshot,
        )
        if args.check:
            try:
                actual = output_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                print(
                    f"generated {market.value} instrument config is missing or unreadable",
                    file=sys.stderr,
                )
                return 2
            if actual != expected:
                print(
                    f"generated {market.value} instrument config is stale",
                    file=sys.stderr,
                )
                return 3
            continue
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(expected, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
