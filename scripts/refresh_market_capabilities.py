"""刷新或核对 Bybit 主网公开市场能力快照，不读取任何 API 凭据。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import jsonschema
import yaml

from alphamind.config import load_instrument_registry
from alphamind.market import (
    BybitInstrumentClient,
    CapabilityError,
    build_market_capability_snapshot,
    load_market_capability_snapshot,
)


def _inside(root: Path, path: Path, *, label: str) -> Path:
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label} must stay inside the project root")
    return resolved


def _global_max_leverage(runtime_path: Path) -> Decimal:
    try:
        runtime = yaml.safe_load(runtime_path.read_text(encoding="utf-8"))
        raw = os.environ.get(
            "ALPHAMIND_GLOBAL_MAX_LEVERAGE",
            runtime["execution"]["futures"]["global_max_leverage"],
        )
        value = Decimal(raw)
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, InvalidOperation):
        raise ValueError("runtime global_max_leverage could not be loaded") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("runtime global_max_leverage must be finite and positive")
    return value


def _schema_validate(document: dict[str, object], schema_path: Path) -> None:
    try:
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(document)
    except (OSError, UnicodeError, yaml.YAMLError, jsonschema.SchemaError):
        raise ValueError("market capability schema could not be loaded") from None
    except jsonschema.ValidationError as error:
        location = ".".join(str(item) for item in error.absolute_path) or "root"
        raise ValueError(
            f"market capability snapshot failed schema validation at {location}"
        ) from None


def _atomic_write(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    serialized = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--instrument-registry",
        type=Path,
        default=Path("configs/alphamind/instruments.example.yaml"),
    )
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=Path("configs/alphamind/runtime.example.yaml"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("configs/alphamind/market-capabilities.snapshot.json"),
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--maximum-age-hours", type=int, default=24)
    args = parser.parse_args(argv)

    try:
        root = args.project_root.resolve()
        registry_path = _inside(root, args.instrument_registry, label="instrument registry")
        runtime_path = _inside(root, args.runtime_config, label="runtime config")
        output_path = _inside(root, args.output, label="output")
        schema_path = _inside(
            root,
            Path("data/schemas/market-capability-snapshot.schema.yaml"),
            label="schema",
        )
        registry = load_instrument_registry(registry_path)
        if args.check:
            snapshot = load_market_capability_snapshot(
                output_path,
                registry=registry,
                now_utc=datetime.now(UTC),
                maximum_age=timedelta(hours=args.maximum_age_hours),
            )
        else:
            fetched = BybitInstrumentClient().fetch()
            snapshot = build_market_capability_snapshot(
                registry,
                fetched,
                global_max_leverage=_global_max_leverage(runtime_path),
            )
            document = snapshot.to_dict()
            _schema_validate(document, schema_path)
            _atomic_write(output_path, document)
    except (CapabilityError, OSError, ValueError) as error:
        print(
            json.dumps({"status": "invalid", "error": str(error)}, sort_keys=True), file=sys.stderr
        )
        return 2

    print(
        json.dumps(
            {
                "status": "ok",
                "snapshot_id": snapshot.snapshot_id,
                "fetched_at_utc": snapshot.to_dict()["fetched_at_utc"],
                "available_spot_pairs": list(snapshot.available_pairs("spot")),
                "available_futures_pairs": list(snapshot.available_pairs("futures")),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
