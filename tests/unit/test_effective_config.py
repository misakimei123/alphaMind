from __future__ import annotations

import json
import shutil
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from alphamind.config import ConfigError, load_effective_config
from alphamind.config.cli import main

PROJECT_ROOT = Path(__file__).parents[2]


def _load_yaml(path: Path) -> dict[str, object]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _write_yaml(path: Path, document: dict[str, object]) -> None:
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _copy_configuration_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "configs", root / "configs")
    shutil.copytree(PROJECT_ROOT / "data" / "schemas", root / "data" / "schemas")
    shutil.copytree(PROJECT_ROOT / "prompts", root / "prompts")
    return root


def test_repository_configuration_loads_as_deterministic_safe_snapshot() -> None:
    first = load_effective_config(PROJECT_ROOT, environ={})
    second = load_effective_config(PROJECT_ROOT, environ={})

    assert first.effective_sha256 == second.effective_sha256
    assert len(first.effective_sha256) == 64
    assert first.applied_overrides == ()
    assert [row["id"] for row in first.instruments["instruments"]] == [
        "BTC",
        "ETH",
        "SOL",
        "HYPE",
    ]
    assert [source["source_id"] for source in first.news_sources["sources"]] == [
        "bybit_announcements",
        "sec_press_releases",
        "coindesk_rss",
    ]
    assert first.ai_profile["model"]["id"] == "gpt-5.6-terra"
    assert first.runtime["approval"]["store_path"] == "user_data/state/proposals.sqlite"
    assert first.execution_ready is True
    assert [item.required for item in first.runtime_dependencies] == [True, True]
    assert [item.exists for item in first.runtime_dependencies] == [True, True]
    assert first.warnings == ()
    assert "freqtrade_spot_merged" in first.source_sha256
    assert "freqtrade_futures_merged" in first.source_sha256

    safe = first.to_safe_dict()
    profile = safe["configuration"]["ai_profile"]
    assert profile["provider"]["api_key_env"] == "OPENAI_API_KEY"
    assert profile["request"]["max_input_tokens"] == 24000
    assert profile["cost"]["input_per_million_tokens"] == "2.50"
    assert profile["audit"]["record_api_key"] is False

    profile_with_credentials = deepcopy(first.ai_profile)
    profile_with_credentials["provider"]["api_key"] = "actual-api-key"
    profile_with_credentials["provider"]["bot_token"] = "actual-bot-token"
    redacted_profile = replace(first, ai_profile=profile_with_credentials).to_safe_dict()[
        "configuration"
    ]["ai_profile"]
    assert redacted_profile["provider"]["api_key"] == "<redacted>"
    assert redacted_profile["provider"]["bot_token"] == "<redacted>"


def test_whitelisted_environment_overrides_are_typed_and_hashed() -> None:
    baseline = load_effective_config(PROJECT_ROOT, environ={})
    effective = load_effective_config(
        PROJECT_ROOT,
        environ={
            "ALPHAMIND_ENVIRONMENT": "demo",
            "ALPHAMIND_DECISION_CYCLE_MINUTES": "60",
            "ALPHAMIND_FUTURES_ENABLED": "false",
            "OPENAI_API_KEY": "must-not-be-read",
            "ALPHAMIND_TELEGRAM_BOT_TOKEN": "must-not-be-read-either",
        },
    )

    assert effective.runtime["environment"] == "demo"
    assert effective.runtime["scheduler"]["decision_cycle_minutes"] == 60
    assert effective.runtime["execution"]["futures"]["enabled"] is False
    assert effective.execution_ready is True
    assert effective.warnings == ()
    assert effective.applied_overrides == (
        "ALPHAMIND_DECISION_CYCLE_MINUTES",
        "ALPHAMIND_ENVIRONMENT",
        "ALPHAMIND_FUTURES_ENABLED",
    )
    assert effective.effective_sha256 != baseline.effective_sha256
    serialized = json.dumps(effective.to_safe_dict(), ensure_ascii=False)
    assert "must-not-be-read" not in serialized


def test_ai_profile_override_selects_versioned_deepseek_profile_without_reading_key() -> None:
    effective = load_effective_config(
        PROJECT_ROOT,
        environ={
            "ALPHAMIND_AI_PROFILE_PATH": ("configs/alphamind/ai-profile.deepseek-test.yaml"),
            "DEEPSEEK_API_KEY": "must-not-be-read",  # pragma: allowlist secret
        },
    )

    assert effective.applied_overrides == ("ALPHAMIND_AI_PROFILE_PATH",)
    assert effective.ai_profile["provider"] == {
        "id": "deepseek",
        "api": "chat_completions",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",  # pragma: allowlist secret
    }
    assert effective.ai_profile["model"]["id"] == "deepseek-v4-flash"
    assert "must-not-be-read" not in json.dumps(effective.to_safe_dict())


def test_leverage_override_requires_matching_capability_refresh() -> None:
    with pytest.raises(ConfigError, match="global leverage does not match"):
        load_effective_config(
            PROJECT_ROOT,
            environ={"ALPHAMIND_GLOBAL_MAX_LEVERAGE": "2.5"},
        )


@pytest.mark.parametrize(
    ("name", "raw_value"),
    [
        ("ALPHAMIND_DECISION_CYCLE_MINUTES", "invalid-sensitive-value"),
        ("ALPHAMIND_FUTURES_ENABLED", "invalid-sensitive-value"),
        ("ALPHAMIND_GLOBAL_MAX_LEVERAGE", "invalid-sensitive-value"),
    ],
)
def test_invalid_environment_override_is_rejected_without_echoing_value(
    name: str,
    raw_value: str,
) -> None:
    with pytest.raises(ConfigError) as raised:
        load_effective_config(PROJECT_ROOT, environ={name: raw_value})

    assert name in str(raised.value)
    assert raw_value not in str(raised.value)


def test_cross_file_timing_rules_run_after_environment_overrides() -> None:
    with pytest.raises(ConfigError, match="approval ttl must not exceed"):
        load_effective_config(
            PROJECT_ROOT,
            environ={"ALPHAMIND_DECISION_CYCLE_MINUTES": "5"},
        )

    with pytest.raises(ConfigError, match="AI request timeout must be shorter"):
        load_effective_config(
            PROJECT_ROOT,
            environ={"ALPHAMIND_CYCLE_TIMEOUT_SECONDS": "80"},
        )


def test_runtime_config_must_stay_inside_project_root(tmp_path: Path) -> None:
    outside = tmp_path / "runtime.yaml"
    outside.write_text("schema_version: 2\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="must stay inside the project root"):
        load_effective_config(PROJECT_ROOT, outside, environ={})


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("scheduler", "state_db_path", "../outside.sqlite", "scheduler state DB"),
        ("scheduler", "snapshot_directory", "../outside", "scheduler snapshot directory"),
        ("risk", "snapshot_path", "../outside.json", "risk snapshot"),
        ("approval", "store_path", "../outside.sqlite", "proposal store"),
    ],
)
def test_runtime_state_paths_must_stay_inside_project_root(
    tmp_path: Path,
    section: str,
    field: str,
    value: str,
    message: str,
) -> None:
    root = _copy_configuration_project(tmp_path)
    runtime_path = root / "configs" / "alphamind" / "runtime.example.yaml"
    runtime = _load_yaml(runtime_path)
    target = runtime[section]
    assert isinstance(target, dict)
    target[field] = value
    _write_yaml(runtime_path, runtime)

    with pytest.raises(ConfigError, match=message):
        load_effective_config(root, environ={})


def test_prompt_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    root = _copy_configuration_project(tmp_path)
    prompt = root / "prompts" / "ai" / "trade-decision-v2.md"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="prompt sha256 does not match"):
        load_effective_config(root, environ={})


def test_business_identifiers_and_priorities_must_be_unique(tmp_path: Path) -> None:
    root = _copy_configuration_project(tmp_path)
    instruments_path = root / "configs" / "alphamind" / "instruments.example.yaml"
    instruments = _load_yaml(instruments_path)
    rows = instruments["instruments"]
    assert isinstance(rows, list)
    assert isinstance(rows[1], dict)
    rows[1]["id"] = "BTC"
    rows[1]["spot"]["pair"] = "BTC/USDT"
    rows[1]["futures"]["pair"] = "BTC/USDT:USDT"
    rows[1]["futures"]["max_leverage"] = "1.5"
    _write_yaml(instruments_path, instruments)

    with pytest.raises(ConfigError, match="instrument ids must be unique"):
        load_effective_config(root, environ={})

    root = _copy_configuration_project(tmp_path / "news")
    sources_path = root / "configs" / "alphamind" / "news-sources.example.yaml"
    sources = _load_yaml(sources_path)
    source_rows = sources["sources"]
    assert isinstance(source_rows, list)
    assert isinstance(source_rows[1], dict)
    source_rows[1]["priority"] = 10
    _write_yaml(sources_path, sources)

    with pytest.raises(ConfigError, match="news source priorities must be unique"):
        load_effective_config(root, environ={})


@pytest.mark.parametrize(
    ("spot_key", "futures_key", "message"),
    [
        ("runtime_db_path", "runtime_db_path", "databases must be distinct"),
        ("bot_identity", "bot_identity", "bot identities must be distinct"),
        ("api_key_env", "api_key_env", "credential environment names"),
    ],
)
def test_spot_and_futures_runtime_isolation_fails_closed(
    tmp_path: Path,
    spot_key: str,
    futures_key: str,
    message: str,
) -> None:
    root = _copy_configuration_project(tmp_path)
    runtime_path = root / "configs" / "alphamind" / "runtime.example.yaml"
    runtime = _load_yaml(runtime_path)
    execution = runtime["execution"]
    assert isinstance(execution, dict)
    spot = execution["spot"]
    futures = execution["futures"]
    assert isinstance(spot, dict)
    assert isinstance(futures, dict)
    futures[futures_key] = spot[spot_key]
    _write_yaml(runtime_path, runtime)

    with pytest.raises(ConfigError, match=message):
        load_effective_config(root, environ={})


def test_futures_generated_pairlist_drift_fails_closed(tmp_path: Path) -> None:
    root = _copy_configuration_project(tmp_path)
    pairlist_path = root / "configs" / "freqtrade" / "futures-instruments.generated.json"
    pairlist = json.loads(pairlist_path.read_text(encoding="utf-8"))
    pairlist["exchange"]["pair_whitelist"].pop()
    pairlist_path.write_text(json.dumps(pairlist), encoding="utf-8")

    with pytest.raises(ConfigError, match="pair whitelist"):
        load_effective_config(root, environ={})


def test_freqtrade_dependency_changes_effective_configuration_hash(tmp_path: Path) -> None:
    root = _copy_configuration_project(tmp_path)
    before = load_effective_config(root, environ={})
    common_path = root / "configs" / "freqtrade" / "common.json"
    common = json.loads(common_path.read_text(encoding="utf-8"))
    common["cancel_open_orders_on_exit"] = True
    common_path.write_text(json.dumps(common), encoding="utf-8")
    after = load_effective_config(root, environ={})

    assert before.effective_sha256 != after.effective_sha256
    assert (
        before.source_sha256["freqtrade_spot_merged"]
        != after.source_sha256["freqtrade_spot_merged"]
    )
    assert (
        before.source_sha256["freqtrade_futures_merged"]
        != after.source_sha256["freqtrade_futures_merged"]
    )


def test_schema_validation_error_does_not_echo_unknown_value(tmp_path: Path) -> None:
    root = _copy_configuration_project(tmp_path)
    runtime_path = root / "configs" / "alphamind" / "runtime.example.yaml"
    runtime = _load_yaml(runtime_path)
    runtime["unexpected_secret"] = "do-not-echo-this-value"
    _write_yaml(runtime_path, runtime)

    with pytest.raises(ConfigError) as raised:
        load_effective_config(root, environ={})

    message = str(raised.value)
    assert "failed schema validation" in message
    assert "do-not-echo-this-value" not in message


def test_show_effective_config_cli_is_safe_and_reports_execution_readiness(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = {
        "OPENAI_API_KEY": "cli-openai-secret",
        "ALPHAMIND_TELEGRAM_BOT_TOKEN": "cli-telegram-secret",
    }
    status = main(["--project-root", str(PROJECT_ROOT)], environ=environ)
    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert status == 0
    assert captured.err == ""
    assert output["status"] == "ok"
    assert output["execution_ready"] is True
    assert "cli-openai-secret" not in captured.out
    assert "cli-telegram-secret" not in captured.out

    status = main(
        ["--project-root", str(PROJECT_ROOT), "--require-execution-ready"],
        environ=environ,
    )
    assert status == 0
