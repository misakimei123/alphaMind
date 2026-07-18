"""离线检查仓库内的 Markdown 链接和高置信度敏感信息。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from detect_secrets.core.secrets_collection import SecretsCollection
from detect_secrets.settings import transient_settings
from markdown_it import MarkdownIt

URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
SENSITIVE_FILENAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
SECRET_BASELINE = ".secrets.baseline"
DEFAULT_SECRET_SETTINGS: dict[str, object] = {
    "plugins_used": [
        {"name": "AWSKeyDetector"},
        {"name": "GitHubTokenDetector"},
        {"name": "KeywordDetector"},
        {"name": "PrivateKeyDetector"},
        {"name": "SlackDetector"},
    ]
}
MARKDOWN = MarkdownIt("commonmark")


def repository_files(root: Path) -> list[Path]:
    """返回已跟踪和未忽略的新文件，避免门禁漏掉尚未暂存的敏感文件。"""

    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    paths = completed.stdout.decode("utf-8").split("\0")
    return [Path(path) for path in paths if path]


def _markdown_destinations(text: str) -> Iterable[tuple[str, int]]:
    """由 CommonMark token 返回链接目标及其使用位置。"""

    for token in MARKDOWN.parse(text):
        if token.type != "inline" or token.children is None or token.map is None:
            continue
        line = token.map[0] + 1
        for child in token.children:
            if child.type in {"softbreak", "hardbreak"}:
                line += 1
                continue
            attribute = "href" if child.type == "link_open" else "src"
            if child.type not in {"link_open", "image"}:
                continue
            destination = child.attrGet(attribute)
            if isinstance(destination, str) and destination:
                yield destination, line


def scan_markdown_links(root: Path, relative_paths: Iterable[Path]) -> list[str]:
    """验证 Markdown 本地链接存在且没有逃逸到仓库之外。"""

    findings: list[str] = []
    resolved_root = root.resolve()
    for relative_path in relative_paths:
        if relative_path.suffix.lower() != ".md":
            continue
        file_path = root / relative_path
        display_path = relative_path.as_posix()
        if not file_path.is_file():
            continue
        text = file_path.read_text(encoding="utf-8")
        for destination, line in _markdown_destinations(text):
            if (
                not destination
                or destination.startswith(("#", "//"))
                or URI_SCHEME.match(destination)
            ):
                continue
            path_part = unquote(destination.split("#", 1)[0].split("?", 1)[0])
            candidate = (file_path.parent / path_part.replace("\\", "/")).resolve()
            if not candidate.is_relative_to(resolved_root):
                findings.append(f"{display_path}:{line}: local link escapes repository")
            elif not candidate.exists():
                findings.append(f"{display_path}:{line}: missing local link target: {path_part}")
    return findings


def _secret_policy(root: Path) -> tuple[dict[str, object], set[tuple[str, str, str]]]:
    path = root / SECRET_BASELINE
    if not path.is_file():
        return DEFAULT_SECRET_SETTINGS, set()
    document = json.loads(path.read_text(encoding="utf-8"))
    plugins = document.get("plugins_used")
    filters = document.get("filters_used")
    if not isinstance(plugins, list) or not isinstance(filters, list):
        raise ValueError(f"{SECRET_BASELINE} must define plugins_used and filters_used")
    settings: dict[str, object] = {"plugins_used": plugins, "filters_used": filters}
    results = document.get("results", {})
    if not isinstance(results, dict):
        raise ValueError(f"{SECRET_BASELINE} results must be an object")
    fingerprints: set[tuple[str, str, str]] = set()
    for raw_path, raw_findings in results.items():
        if not isinstance(raw_path, str) or not isinstance(raw_findings, list):
            raise ValueError(f"{SECRET_BASELINE} contains an invalid result")
        for raw_finding in raw_findings:
            if not isinstance(raw_finding, dict):
                raise ValueError(f"{SECRET_BASELINE} contains an invalid finding")
            finding: dict[str, Any] = raw_finding
            detector = finding.get("type")
            hashed_secret = finding.get("hashed_secret")
            if not isinstance(detector, str) or not isinstance(hashed_secret, str):
                raise ValueError(f"{SECRET_BASELINE} finding is incomplete")
            fingerprints.add((raw_path.replace("\\", "/"), detector, hashed_secret))
    return settings, fingerprints


def scan_secrets(root: Path, relative_paths: Iterable[Path]) -> list[str]:
    """使用 detect-secrets 报告新 secret，输出中不回显疑似凭据内容。"""

    findings: list[str] = []
    settings, baseline = _secret_policy(root)
    candidates: list[tuple[Path, str]] = []
    for relative_path in relative_paths:
        file_path = root / relative_path
        display_path = relative_path.as_posix()
        if not file_path.is_file():
            continue
        if display_path == SECRET_BASELINE:
            continue
        lower_name = relative_path.name.lower()
        if lower_name in SENSITIVE_FILENAMES or relative_path.suffix.lower() in SENSITIVE_SUFFIXES:
            findings.append(f"{display_path}: sensitive filename must not be tracked")
            continue
        candidates.append((file_path, display_path))

    secrets = SecretsCollection()
    with transient_settings(settings):
        for file_path, _ in candidates:
            secrets.scan_file(str(file_path))
    detected = secrets.json()
    for file_path, display_path in candidates:
        for raw_finding in detected.get(str(file_path), []):
            detector = raw_finding.get("type")
            hashed_secret = raw_finding.get("hashed_secret")
            line = raw_finding.get("line_number")
            if not isinstance(detector, str) or not isinstance(hashed_secret, str):
                continue
            if (display_path, detector, hashed_secret) in baseline:
                continue
            line_suffix = f":{line}" if isinstance(line, int) else ""
            findings.append(f"{display_path}{line_suffix}: suspected secret ({detector})")
    return findings


def scan_repository(root: Path) -> tuple[list[Path], list[str]]:
    paths = repository_files(root)
    findings = scan_markdown_links(root, paths)
    findings.extend(scan_secrets(root, paths))
    return paths, sorted(set(findings))


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    paths, findings = scan_repository(root)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "files_checked": len(paths)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
