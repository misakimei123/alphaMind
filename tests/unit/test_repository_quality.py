import copy
import hashlib
import json
import tomllib
from datetime import datetime
from pathlib import Path

import yaml

from alphamind.research.data_quality import validate_partition
from scripts.build_clean_dataset import (
    _canonical_report_sha256,
    _development_bounds,
    canonical_markdown_sha256,
)
from scripts.check_repository import scan_markdown_links, scan_repository, scan_secrets

PROJECT_ROOT = Path(__file__).parents[2]


def _quality_case_rows(
    base_rows: list[dict[str, object]],
    case: dict[str, object],
) -> list[dict[str, object]]:
    rows = copy.deepcopy(base_rows)
    updates = case.get("updates", {})
    assert isinstance(updates, dict)
    for index_text, fields in updates.items():
        assert isinstance(index_text, str)
        assert isinstance(fields, dict)
        rows[int(index_text)].update(fields)
    drop_indices = case.get("drop_indices", [])
    assert isinstance(drop_indices, list)
    rows = [row for index, row in enumerate(rows) if index not in drop_indices]
    order = case.get("order")
    if order is not None:
        assert isinstance(order, list)
        rows = [rows[index] for index in order]
    append_indices = case.get("append_indices", [])
    assert isinstance(append_indices, list)
    rows.extend(copy.deepcopy(base_rows[index]) for index in append_indices)
    return rows


def test_current_repository_has_no_broken_local_links_or_detected_secrets() -> None:
    _, findings = scan_repository(PROJECT_ROOT)

    assert findings == []


def test_infrastructure_reuses_mature_libraries() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = {
        requirement.split(">", 1)[0].split("<", 1)[0].split("[", 1)[0]
        for requirement in project["project"]["dependencies"]
    }
    dev_dependencies = {
        requirement.split(">", 1)[0].split("<", 1)[0].split("[", 1)[0]
        for requirement in project["project"]["optional-dependencies"]["dev"]
    }
    assert {
        "beautifulsoup4",
        "feedparser",
        "filelock",
        "httpx",
        "jsonschema",
        "openai",
        "pyyaml",
    } <= dependencies
    assert {"detect-secrets", "markdown-it-py"} <= dev_dependencies

    forbidden_by_file = {
        "src/alphamind/market/bybit.py": (
            "urllib.request",
            "urlopen(",
            "Transport = Callable",
        ),
        "src/alphamind/news/http.py": (
            "urllib.request",
            "urlopen(",
            "class NewsHttpResponse",
        ),
        "src/alphamind/news/adapters.py": ("xml.etree", "ElementTree"),
        "src/alphamind/news/collector.py": ("HTMLParser",),
        "src/alphamind/scheduler/core.py": ("import msvcrt", 'import_module("fcntl")'),
        "src/alphamind/ai/provider.py": ("urllib.request", "urlopen("),
        "scripts/check_repository.py": (
            "MARKDOWN_LINK =",
            "SENSITIVE_ASSIGNMENT =",
            "HIGH_CONFIDENCE_SECRETS =",
        ),
    }
    for relative_path, forbidden_values in forbidden_by_file.items():
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert all(value not in source for value in forbidden_values), relative_path


def test_markdown_link_checker_reports_missing_and_escaping_targets(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "present.md").write_text("present", encoding="utf-8")
    readme = docs / "README.md"
    readme.write_text(
        "[present](present.md)\n[missing](missing.md)\n[escape](../../outside.md)\n",
        encoding="utf-8",
    )

    findings = scan_markdown_links(tmp_path, [Path("docs/README.md")])

    assert findings == [
        "docs/README.md:2: missing local link target: missing.md",
        "docs/README.md:3: local link escapes repository",
    ]


def test_markdown_link_checker_uses_commonmark_links_not_code_examples(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    readme = docs / "README.md"
    readme.write_text(
        "`[not a link](missing-inline.md)`\n\n"
        "```markdown\n[also not a link](missing-fence.md)\n```\n\n"
        "[reference][target]\n\n[target]: missing-reference.md\n",
        encoding="utf-8",
    )

    assert scan_markdown_links(tmp_path, [Path("docs/README.md")]) == [
        "docs/README.md:7: missing local link target: missing-reference.md"
    ]


def test_secret_checker_rejects_credentials_without_echoing_value(tmp_path: Path) -> None:
    secret_file = tmp_path / "config.py"
    credential_name = "api_" + "key"
    secret_file.write_text(
        f'{credential_name} = "realistic-secret-value"\n',
        encoding="utf-8",
    )

    findings = scan_secrets(tmp_path, [Path("config.py")])

    assert findings == ["config.py:1: suspected secret (Secret Keyword)"]
    assert "realistic-secret-value" not in findings[0]


def test_secret_checker_allows_explicit_placeholder(tmp_path: Path) -> None:
    template = tmp_path / "config.example.py"
    template.write_text('api_key = "<set-at-runtime>"\n', encoding="utf-8")

    assert scan_secrets(tmp_path, [Path("config.example.py")]) == []


def test_linux_ci_is_read_only_pinned_and_runs_deterministic_gates() -> None:
    workflow_path = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict)

    assert workflow["permissions"] == {"contents": "read"}
    quality_job = workflow["jobs"]["quality"]
    assert quality_job["runs-on"] == "ubuntu-latest"
    steps = quality_job["steps"]
    uses = [step["uses"] for step in steps if "uses" in step]
    commands = "\n".join(step["run"] for step in steps if "run" in step)

    assert uses == [
        "actions/checkout@v7",
        "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b",
    ]
    assert "uv sync --locked --extra dev" in commands
    assert "scripts/check_repository.py" in commands
    assert "uv run mypy" in commands
    assert "uv run ruff format --check ." in commands
    assert "uv run pytest" in commands
    assert "secrets." not in workflow_path.read_text(encoding="utf-8")


def test_data_quality_fixed_anomaly_fixtures() -> None:
    fixture_path = PROJECT_ROOT / "tests/fixtures/data_quality/anomalies.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    base_rows = fixture["base_rows"]
    cases = fixture["cases"]
    assert isinstance(base_rows, list)
    assert isinstance(cases, list)
    timeframe = fixture["timeframe"]
    interval_start = fixture["interval_start"]
    interval_end_exclusive = fixture["interval_end_exclusive"]
    assert isinstance(timeframe, str)
    assert isinstance(interval_start, str)
    assert isinstance(interval_end_exclusive, str)
    start = datetime.fromisoformat(interval_start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(interval_end_exclusive.replace("Z", "+00:00"))

    for case in cases:
        assert isinstance(case, dict)
        rows = _quality_case_rows(base_rows, case)
        result = validate_partition(
            rows,
            timeframe=timeframe,
            interval_start=start,
            interval_end_exclusive=end,
        )
        issues = result["issues"]
        assert isinstance(issues, list)
        actual_codes = {issue["code"] for issue in issues}
        assert set(case["expected_codes"]).issubset(actual_codes), case["id"]
        assert result["status"] == case["expected_status"], case["id"]
        assert result == validate_partition(
            rows,
            timeframe=timeframe,
            interval_start=start,
            interval_end_exclusive=end,
        )


def test_data_quality_report_hash_excludes_self_reference() -> None:
    report: dict[str, object] = {
        "dataset_id": "bybit-spot-development-example",
        "status": "ACCEPTED",
        "report_content_sha256": "0" * 64,
        "report_markdown_sha256": "1" * 64,
    }
    first = _canonical_report_sha256(report)
    report["report_content_sha256"] = "f" * 64
    report["report_markdown_sha256"] = "e" * 64

    assert _canonical_report_sha256(report) == first


def test_markdown_hash_canonicalizes_windows_checkout_line_endings(tmp_path: Path) -> None:
    markdown = tmp_path / "evidence.md"
    markdown.write_bytes(b"first\r\nsecond\r\n")

    assert canonical_markdown_sha256(markdown) == hashlib.sha256(b"first\nsecond\n").hexdigest()


def test_data_quality_bounds_follow_holdout_state() -> None:
    manifest = {
        "source": {
            "requested_end_exclusive": "2026-07-01T00:00:00Z",
        },
        "split_contract": {
            "development_start": "2022-01-01T00:00:00Z",
            "development_end_exclusive": "2025-07-01T00:00:00Z",
        },
    }

    sealed_start, sealed_end = _development_bounds(manifest, "SEALED_UNREAD")
    degraded_start, degraded_end = _development_bounds(manifest, "DEGRADED_TO_DEVELOPMENT")

    assert sealed_start.isoformat() == "2022-01-01T00:00:00+00:00"
    assert sealed_end.isoformat() == "2025-07-01T00:00:00+00:00"
    assert degraded_start == sealed_start
    assert degraded_end.isoformat() == "2026-07-01T00:00:00+00:00"
