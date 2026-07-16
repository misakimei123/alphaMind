"""离线检查仓库内的 Markdown 链接和高置信度敏感信息。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote

MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]*\]\((?P<target>[^)\n]+)\)")
URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|client[_-]?secret|password|private[_-]?key|access[_-]?token)"
    r"\b\s*[:=]\s*[\"']([^\"'\r\n]{8,})[\"']"
)
HIGH_CONFIDENCE_SECRETS = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GitHub token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b|\bgh[opsu]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)
SENSITIVE_FILENAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
PLACEHOLDER_MARKERS = {
    "${",
    "$env:",
    "changeme",
    "example",
    "fixture",
    "not-set",
    "placeholder",
    "redacted",
}


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


def _link_destination(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        return target[1 : target.index(">")]
    return target.split(maxsplit=1)[0]


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
        for match in MARKDOWN_LINK.finditer(text):
            destination = _link_destination(match.group("target"))
            if (
                not destination
                or destination.startswith(("#", "//"))
                or URI_SCHEME.match(destination)
            ):
                continue
            path_part = unquote(destination.split("#", 1)[0].split("?", 1)[0])
            candidate = (file_path.parent / path_part.replace("\\", "/")).resolve()
            line = text.count("\n", 0, match.start()) + 1
            if not candidate.is_relative_to(resolved_root):
                findings.append(f"{display_path}:{line}: local link escapes repository")
            elif not candidate.exists():
                findings.append(f"{display_path}:{line}: missing local link target: {path_part}")
    return findings


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized.startswith("<") or any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def scan_secrets(root: Path, relative_paths: Iterable[Path]) -> list[str]:
    """只报告高置信度 secret，输出中不回显疑似凭据内容。"""

    findings: list[str] = []
    for relative_path in relative_paths:
        file_path = root / relative_path
        display_path = relative_path.as_posix()
        if not file_path.is_file():
            continue
        lower_name = relative_path.name.lower()
        if lower_name in SENSITIVE_FILENAMES or relative_path.suffix.lower() in SENSITIVE_SUFFIXES:
            findings.append(f"{display_path}: sensitive filename must not be tracked")
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in HIGH_CONFIDENCE_SECRETS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{display_path}:{line}: suspected {label}")
        for match in SENSITIVE_ASSIGNMENT.finditer(text):
            if _is_placeholder(match.group(2)):
                continue
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{display_path}:{line}: suspected sensitive assignment")
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
