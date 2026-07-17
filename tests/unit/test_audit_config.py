import hashlib
from pathlib import Path

import pytest

from alphamind.audit import load_audit_runtime_config, load_audit_storage_config


def write_config(tmp_path: Path) -> tuple[Path, Path, Path]:
    strategy = tmp_path / "strategy.toml"
    runtime = tmp_path / "runtime.toml"
    strategy.write_text("strategy = 'donchian'\n", encoding="utf-8")
    runtime.write_text("runtime = 'locked'\n", encoding="utf-8")
    config = tmp_path / "audit.toml"
    config.write_text(
        "\n".join(
            (
                "schema_version = 1",
                f'outbox_path = "{(tmp_path / "outbox.sqlite").as_posix()}"',
                f'audit_db_path = "{(tmp_path / "audit.sqlite").as_posix()}"',
                'producer_instance_id = "freqtrade-test"',
                'project_commit_env = "ALPHAMIND_PROJECT_COMMIT"',
                'strategy_id = "donchian_trend"',
                'strategy_version = "0.3.0"',
                f'strategy_config_path = "{strategy.as_posix()}"',
                f'runtime_lock_path = "{runtime.as_posix()}"',
            )
        ),
        encoding="utf-8",
    )
    return config, strategy, runtime


def test_audit_config_hashes_runtime_inputs_and_requires_deployed_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, strategy_path, runtime_path = write_config(tmp_path)
    storage = load_audit_storage_config(config_path)
    assert storage.outbox_path != storage.audit_db_path

    monkeypatch.delenv("ALPHAMIND_PROJECT_COMMIT", raising=False)
    with pytest.raises(ValueError, match="deployed 40-character commit"):
        load_audit_runtime_config(config_path)

    monkeypatch.setenv("ALPHAMIND_PROJECT_COMMIT", "a" * 40)
    runtime = load_audit_runtime_config(config_path)
    assert runtime.provenance.project_commit == "a" * 40
    assert (
        runtime.provenance.strategy_config_sha256
        == hashlib.sha256(strategy_path.read_bytes()).hexdigest()
    )
    assert (
        runtime.provenance.runtime_lock_sha256
        == hashlib.sha256(runtime_path.read_bytes()).hexdigest()
    )


def test_audit_config_rejects_shared_outbox_and_sink_path(tmp_path: Path) -> None:
    config_path, _, _ = write_config(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    outbox_line = next(line for line in text.splitlines() if line.startswith("outbox_path"))
    outbox_path = outbox_line.split("=", 1)[1].strip()
    config_path.write_text(
        "\n".join(
            f"audit_db_path = {outbox_path}" if line.startswith("audit_db_path") else line
            for line in text.splitlines()
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="independent files"):
        load_audit_storage_config(config_path)
