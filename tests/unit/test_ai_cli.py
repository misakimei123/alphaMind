from __future__ import annotations

import json
from pathlib import Path

from alphamind.ai.cli import main

PROJECT_ROOT = Path(__file__).parents[2]


def test_check_validates_provider_without_network_or_key(capsys: object) -> None:
    exit_code = main(
        ["--project-root", str(PROJECT_ROOT), "--check"],
        environ={},
    )

    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert exit_code == 0
    assert output["status"] == "configuration_valid"
    assert output["network_request_sent"] is False
    assert output["provider"]["api_key_configured"] is False
    assert output["structured_output"]["strict"] is True
    assert output["request"] == {
        "background": False,
        "maximum_attempts": 2,
        "store": False,
        "timeout_seconds": 90,
        "tools_enabled": False,
    }


def test_check_never_prints_configured_key(capsys: object) -> None:
    secret = "secret-value-that-must-not-be-printed"

    exit_code = main(
        ["--project-root", str(PROJECT_ROOT), "--check", "--pretty"],
        environ={"OPENAI_API_KEY": secret},
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 0
    assert secret not in captured.out
    assert secret not in captured.err
    assert json.loads(captured.out)["provider"]["api_key_configured"] is True


def test_check_reports_deepseek_chat_contract_without_printing_key(capsys: object) -> None:
    secret = "deepseek-secret-that-must-not-be-printed"  # pragma: allowlist secret
    exit_code = main(
        ["--project-root", str(PROJECT_ROOT), "--check"],
        environ={
            "ALPHAMIND_AI_PROFILE_PATH": ("configs/alphamind/ai-profile.deepseek-test.yaml"),
            "DEEPSEEK_API_KEY": secret,
        },
    )

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    output = json.loads(captured.out)
    assert exit_code == 0
    assert secret not in captured.out
    assert secret not in captured.err
    assert output["provider"]["endpoint"] == "https://api.deepseek.com/chat/completions"
    assert output["model"]["id"] == "deepseek-v4-flash"
    assert output["model"]["thinking"] == "disabled"
    assert output["structured_output"]["strict"] is True
    assert output["structured_output"]["provider_schema_enforced"] is False


def test_context_is_required_outside_check(capsys: object) -> None:
    exit_code = main(["--project-root", str(PROJECT_ROOT)], environ={})

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert exit_code == 2
    assert json.loads(captured.err) == {
        "error": "--context is required unless --check is used",
        "status": "invalid",
    }
