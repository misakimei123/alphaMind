"""校验 AI provider，或用已绑定的 DecisionContext 发起一次只读决策请求。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from alphamind.ai import (
    CostPolicy,
    OpenAICompatibleProvider,
    ProviderClient,
    UsageLedger,
    UsageLedgerError,
    build_provider,
)
from alphamind.config import ConfigError, load_effective_config
from alphamind.decision import ContractValidationError, DecisionContractBinder

JsonObject = dict[str, Any]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--runtime-config",
        type=Path,
        default=Path("configs/alphamind/runtime.example.yaml"),
    )
    parser.add_argument(
        "--context",
        type=Path,
        help="DecisionContext JSON/YAML；省略时必须使用 --check",
    )
    parser.add_argument(
        "--usage-db",
        type=Path,
        default=Path("user_data/state/ai-usage.sqlite"),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验配置、Prompt 和 provider schema，不发起网络请求",
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def _under_root(project_root: Path, path: Path, *, label: str) -> Path:
    candidate = path if path.is_absolute() else project_root / path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError:
        raise ValueError(f"{label} must stay inside the project root") from None
    return resolved


def _document(path: Path) -> JsonObject:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ValueError("DecisionContext could not be loaded") from None
    if not isinstance(value, dict):
        raise ValueError("DecisionContext must be an object")
    return value


def _schema_sha256(schema: JsonObject) -> str:
    canonical = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_output(
    provider: OpenAICompatibleProvider,
    *,
    key_configured: bool,
) -> JsonObject:
    profile = provider.profile
    endpoint_suffix = (
        "responses" if profile["provider"]["api"] == "responses" else "chat/completions"
    )
    return {
        "status": "configuration_valid",
        "network_request_sent": False,
        "provider": {
            "api": profile["provider"]["api"],
            "endpoint": f"{profile['provider']['base_url']}/{endpoint_suffix}",
            "api_key_env": profile["provider"]["api_key_env"],
            "api_key_configured": key_configured,
        },
        "model": {
            "id": profile["model"]["id"],
            "reasoning_effort": profile["model"]["reasoning_effort"],
            "verbosity": profile["model"]["verbosity"],
            "thinking": profile["model"]["thinking"],
        },
        "structured_output": {
            "name": profile["structured_output"]["schema_name"],
            "strict": True,
            "provider_schema_enforced": profile["structured_output"]["provider_schema_enforced"],
            "provider_schema_sha256": _schema_sha256(provider.schema),
        },
        "request": {
            "timeout_seconds": profile["request"]["timeout_seconds"],
            "maximum_attempts": profile["retry"]["maximum_attempts"],
            "store": False,
            "background": False,
            "tools_enabled": False,
        },
        "prompt_sha256": profile["prompt"]["sha256"],
        "config_sha256": provider.effective.effective_sha256,
    }


def _print(value: JsonObject, *, pretty: bool, stream: Any = None) -> None:
    print(
        json.dumps(value, ensure_ascii=False, indent=2 if pretty else None, sort_keys=True),
        file=stream,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    now_utc: datetime | None = None,
    client: ProviderClient | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    project_root = args.project_root.resolve()
    selected_environ = dict(os.environ if environ is None else environ)
    try:
        effective = load_effective_config(
            project_root,
            args.runtime_config,
            environ=selected_environ,
        )
        key_env = str(effective.ai_profile["provider"]["api_key_env"])
        key_configured = bool(selected_environ.get(key_env, "").strip())
        if args.check:
            with TemporaryDirectory(prefix="alphamind-ai-check-") as temporary:
                ledger = UsageLedger(
                    Path(temporary) / "usage.sqlite",
                    CostPolicy.from_profile(effective.ai_profile),
                )
                provider = OpenAICompatibleProvider(
                    effective,
                    usage_ledger=ledger,
                    environ=selected_environ,
                )
                output = _check_output(provider, key_configured=key_configured)
            _print(output, pretty=args.pretty)
            return 0

        if args.context is None:
            raise ValueError("--context is required unless --check is used")
        usage_path = _under_root(project_root, args.usage_db, label="AI usage DB path")
        current = now_utc or datetime.now(UTC)
        context = DecisionContractBinder(effective).bind_context(
            _document(args.context.resolve()),
            now_utc=current,
        )
        provider = build_provider(
            effective,
            usage_db_path=usage_path,
            client=client,
            environ=selected_environ,
            sleep=sleep or time.sleep,
        )
        result = provider.decide(context, now_utc=current)
    except ContractValidationError as error:
        _print(
            {"status": "invalid", "error": error.to_dict()}, pretty=args.pretty, stream=sys.stderr
        )
        return 2
    except (ConfigError, UsageLedgerError, ValueError) as error:
        _print(
            {"status": "invalid", "error": str(error)},
            pretty=args.pretty,
            stream=sys.stderr,
        )
        return 2

    _print(result.to_safe_dict(), pretty=args.pretty)
    return 0 if result.status == "SUCCESS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
