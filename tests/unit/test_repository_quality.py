from pathlib import Path

import yaml

from scripts.check_repository import scan_markdown_links, scan_repository, scan_secrets

PROJECT_ROOT = Path(__file__).parents[2]


def test_current_repository_has_no_broken_local_links_or_detected_secrets() -> None:
    _, findings = scan_repository(PROJECT_ROOT)

    assert findings == []


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


def test_secret_checker_rejects_credentials_without_echoing_value(tmp_path: Path) -> None:
    secret_file = tmp_path / "config.py"
    credential_name = "api_" + "key"
    secret_file.write_text(
        f'{credential_name} = "realistic-secret-value"\n',
        encoding="utf-8",
    )

    findings = scan_secrets(tmp_path, [Path("config.py")])

    assert findings == ["config.py:1: suspected sensitive assignment"]
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
