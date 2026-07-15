import json
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
RUNTIME_LOCK = PROJECT_ROOT / "configs" / "common" / "runtime-versions.toml"
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_runtime_lock.py"


def test_runtime_lock_contains_exact_freqtrade_artifacts() -> None:
    with RUNTIME_LOCK.open("rb") as lock_file:
        lock = tomllib.load(lock_file)

    research = lock["research_runtime"]
    freqtrade = lock["freqtrade_runtime"]
    assert research == {
        "python": "3.12.9",
        "uv": "0.11.7",
        "requires_python": ">=3.12,<3.15",
        "compatibility_test_python": "3.14.4",
    }
    assert freqtrade["version"] == "2026.6"
    assert freqtrade["source_tag"] == "2026.6"
    assert freqtrade["source_commit"] == "b604e2fd70539f7f73d3c62c16ce0b155bbab319"
    assert freqtrade["python"] == "3.14.6"
    assert freqtrade["ccxt"] == "4.5.61"
    assert freqtrade["docker_platform"] == "linux/amd64"
    assert freqtrade["docker_reference"].endswith(freqtrade["docker_platform_digest"])


def test_runtime_lock_metadata_verifier() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(VERIFY_SCRIPT),
            "--config",
            str(RUNTIME_LOCK),
            "--target",
            "metadata",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(completed.stdout)
    assert report == {"actual": {"status": "metadata_valid"}, "target": "metadata"}


def test_research_runtime_matches_lock_on_primary_python() -> None:
    if sys.version_info[:3] != (3, 12, 9):
        return

    completed = subprocess.run(
        [
            sys.executable,
            str(VERIFY_SCRIPT),
            "--config",
            str(RUNTIME_LOCK),
            "--target",
            "research",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert report["actual"] == {"python": "3.12.9", "uv": "0.11.7"}
