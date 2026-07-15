"""离线核对 alphaMind 与 Freqtrade 运行时版本锁。"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def load_lock(path: Path) -> dict[str, Any]:
    with path.open("rb") as lock_file:
        lock = tomllib.load(lock_file)

    required_tables = {"research_runtime", "freqtrade_runtime", "sources"}
    missing = required_tables - set(lock)
    if missing:
        raise ValueError(f"runtime lock is missing tables: {', '.join(sorted(missing))}")
    if lock.get("schema_version") != 1:
        raise ValueError("runtime lock schema_version must be 1")

    freqtrade = lock["freqtrade_runtime"]
    for key in ("docker_manifest_digest", "docker_platform_digest"):
        if not DIGEST_PATTERN.fullmatch(freqtrade[key]):
            raise ValueError(f"freqtrade_runtime.{key} is not a full sha256 digest")
    if not COMMIT_PATTERN.fullmatch(freqtrade["source_commit"]):
        raise ValueError("freqtrade_runtime.source_commit is not a full git commit")
    expected_reference = f"{freqtrade['docker_repository']}@{freqtrade['docker_platform_digest']}"
    if freqtrade["docker_reference"] != expected_reference:
        raise ValueError("docker_reference must use the locked platform digest")
    return lock


def _uv_version() -> str:
    completed = subprocess.run(
        ["uv", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip().removeprefix("uv ").split()[0]


def verify_research_runtime(lock: dict[str, Any]) -> dict[str, str]:
    expected = lock["research_runtime"]
    actual = {
        "python": ".".join(map(str, sys.version_info[:3])),
        "uv": _uv_version(),
    }
    for key, value in actual.items():
        if value != expected[key]:
            raise RuntimeError(f"research {key} mismatch: expected {expected[key]}, got {value}")
    return actual


def verify_freqtrade_runtime(lock: dict[str, Any]) -> dict[str, str]:
    expected = lock["freqtrade_runtime"]
    actual = {
        "python": ".".join(map(str, sys.version_info[:3])),
        "freqtrade": importlib.metadata.version("freqtrade"),
        "ccxt": importlib.metadata.version("ccxt"),
    }
    for key, value in actual.items():
        expected_key = "version" if key == "freqtrade" else key
        if value != expected[expected_key]:
            raise RuntimeError(
                f"freqtrade {key} mismatch: expected {expected[expected_key]}, got {value}"
            )
    return actual


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/common/runtime-versions.toml"),
    )
    parser.add_argument(
        "--target",
        choices=("metadata", "research", "freqtrade"),
        default="metadata",
    )
    args = parser.parse_args()

    lock = load_lock(args.config)
    if args.target == "research":
        actual = verify_research_runtime(lock)
    elif args.target == "freqtrade":
        actual = verify_freqtrade_runtime(lock)
    else:
        actual = {"status": "metadata_valid"}

    # 机器可读输出便于 CI 和后续证据归档，不输出任何环境变量或敏感配置。
    print(json.dumps({"target": args.target, "actual": actual}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
