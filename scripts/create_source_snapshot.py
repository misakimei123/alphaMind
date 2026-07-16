"""使用锁定的 Freqtrade 容器创建不可变 Bybit OHLCV source snapshot。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import importlib.metadata
import json
import shlex
import subprocess
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import uuid4

PAIRS = ("BTC/USDT", "ETH/USDT")
TIMEFRAMES = ("4h", "1d")
REQUESTED_START = datetime(2022, 1, 1, tzinfo=UTC)
REQUESTED_END_EXCLUSIVE = datetime(2026, 7, 1, tzinfo=UTC)
TIMEFRAME_DELTA = {"4h": timedelta(hours=4), "1d": timedelta(days=1)}
SOURCE_RELATIVE_ROOT = Path("data/source/bybit_spot")
MANIFEST_RELATIVE_ROOT = Path("data/manifests/source")


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_text_sha256(path: Path) -> str:
    """按仓库 LF 合同计算 UTF-8 文本 hash，兼容既有 Windows CRLF checkout。"""

    text = path.read_bytes().decode("utf-8")
    if "\r" in text.replace("\r\n", ""):
        raise ValueError("text evidence contains unsupported bare carriage returns")
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def snapshot_sha256(partitions: Iterable[Mapping[str, object]]) -> str:
    """按 ADR-0005 的相对路径排序规则计算 snapshot hash。"""

    lines = []
    for partition in partitions:
        lines.append(f"{partition['relative_path']}:{partition['file_sha256']}\n")
    return hashlib.sha256("".join(sorted(lines)).encode()).hexdigest()


def canonical_manifest_sha256(manifest: Mapping[str, object]) -> str:
    """排除自引用字段后，对稳定 UTF-8 JSON 计算 manifest hash。"""

    canonical = copy.deepcopy(dict(manifest))
    immutability = canonical.get("immutability")
    if not isinstance(immutability, dict):
        raise ValueError("manifest.immutability must be an object")
    immutability.pop("manifest_content_sha256", None)
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def json_bytes(document: Mapping[str, object]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def write_new_file(path: Path, content: bytes) -> None:
    """只允许新建证据文件，拒绝覆盖任何已有 manifest 或 metadata。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as destination:
        destination.write(content)


def publish_snapshot(staging_root: Path, final_root: Path) -> None:
    """同一文件系统内发布新 snapshot；目标存在时 fail-closed。"""

    if final_root.exists():
        raise FileExistsError(f"snapshot already exists: {final_root}")
    staging_root.rename(final_root)


def _expected_filename(pair: str, timeframe: str) -> str:
    return f"{pair.replace('/', '_')}-{timeframe}.feather"


def _frame_quality(frame: Any, timeframe: str) -> dict[str, int]:
    pd = importlib.import_module("pandas")
    if "date" not in frame.columns:
        raise RuntimeError("Feather partition is missing the date column")

    dates = pd.to_datetime(frame["date"], utc=True)
    delta = TIMEFRAME_DELTA[timeframe]
    unique_dates = set(dates.tolist())
    expected_count = int((REQUESTED_END_EXCLUSIVE - REQUESTED_START) / delta)
    in_range_unique = {
        value
        for value in unique_dates
        if REQUESTED_START <= value.to_pydatetime() < REQUESTED_END_EXCLUSIVE
    }

    duplicate_count = len(dates) - len(unique_dates)
    non_increasing_count = sum(current <= previous for previous, current in pairwise(dates))
    out_of_range_count = sum(
        not (REQUESTED_START <= value.to_pydatetime() < REQUESTED_END_EXCLUSIVE) for value in dates
    )
    off_grid_count = sum(
        (value.to_pydatetime() - REQUESTED_START) % delta != timedelta(0) for value in dates
    )
    missing_candle_count = max(expected_count - len(in_range_unique), 0)

    return {
        "expected_candle_count": expected_count,
        "duplicate_count": duplicate_count,
        "non_increasing_count": non_increasing_count,
        "out_of_range_count": out_of_range_count,
        "off_grid_count": off_grid_count,
        "missing_candle_count": missing_candle_count,
    }


def inspect_partition(
    path: Path,
    relative_path: str,
    pair: str,
    timeframe: str,
) -> tuple[dict[str, object], dict[str, object]]:
    pd = importlib.import_module("pandas")
    # P1-03 只读取时间戳元数据；OHLC/volume QA 属于 P1-04，且只能读取开发池。
    frame = pd.read_feather(path, columns=["date"])
    if len(frame) == 0:
        raise RuntimeError(f"empty Feather partition: {path}")
    dates = pd.to_datetime(frame["date"], utc=True)
    first = dates.iloc[0].to_pydatetime()
    last = dates.iloc[-1].to_pydatetime()
    quality = _frame_quality(frame, timeframe)
    partition = {
        "pair": pair,
        "timeframe": timeframe,
        "candle_type": "spot",
        "relative_path": relative_path,
        "byte_size": path.stat().st_size,
        "file_sha256": file_sha256(path),
        "first_candle_utc": utc_text(first),
        "last_candle_utc": utc_text(last),
        "candle_count": len(frame),
    }
    return partition, {"pair": pair, "timeframe": timeframe, **quality}


def inspect_download(staging_root: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    downloaded_files = sorted(path for path in staging_root.rglob("*") if path.is_file())
    feather_files = [path for path in downloaded_files if path.suffix == ".feather"]
    if downloaded_files != feather_files:
        unexpected = [
            path.relative_to(staging_root).as_posix()
            for path in downloaded_files
            if path not in feather_files
        ]
        raise RuntimeError(f"unexpected files in source snapshot: {unexpected}")

    by_name = {path.name: path for path in feather_files}
    expected_names = {
        _expected_filename(pair, timeframe) for pair in PAIRS for timeframe in TIMEFRAMES
    }
    if set(by_name) != expected_names:
        raise RuntimeError(
            "downloaded partitions mismatch: "
            f"expected {sorted(expected_names)}, got {sorted(by_name)}"
        )

    partitions: list[dict[str, object]] = []
    quality: list[dict[str, object]] = []
    for pair in PAIRS:
        for timeframe in TIMEFRAMES:
            path = by_name[_expected_filename(pair, timeframe)]
            relative_path = path.relative_to(staging_root).as_posix()
            partition, partition_quality = inspect_partition(path, relative_path, pair, timeframe)
            partitions.append(partition)
            quality.append(partition_quality)
    return partitions, quality


def fetch_exchange_metadata(retrieved_at: datetime) -> dict[str, object]:
    """只调用 Bybit 公开市场接口并保存规范化字段，不保留原始响应。"""

    ccxt = importlib.import_module("ccxt")
    exchange = ccxt.bybit({"enableRateLimit": True})
    network_error = ccxt.NetworkError
    for attempt in range(1, 4):
        try:
            markets = exchange.load_markets(reload=True)
            break
        except network_error:
            if attempt == 3:
                raise
            # 公共接口偶发 TLS/限流错误时有限重试，不改变下载参数或源数据。
            time.sleep(attempt * 2)
    normalized = []
    for pair in PAIRS:
        market = markets.get(pair)
        if not isinstance(market, dict) or market.get("spot") is not True:
            raise RuntimeError(f"Bybit spot market is unavailable: {pair}")
        normalized.append(
            {
                "symbol": market.get("symbol"),
                "id": market.get("id"),
                "base": market.get("base"),
                "quote": market.get("quote"),
                "active": market.get("active"),
                "spot": market.get("spot"),
                "precision": market.get("precision"),
                "limits": market.get("limits"),
            }
        )
    return {
        "schema_version": 1,
        "exchange": "bybit",
        "market_type": "spot",
        "retrieved_at_utc": utc_text(retrieved_at),
        "ccxt_version": importlib.metadata.version("ccxt"),
        "raw_exchange_payload_retained": False,
        "markets": normalized,
    }


def quality_totals(partitions: Iterable[Mapping[str, object]]) -> tuple[int, int]:
    error_keys = (
        "duplicate_count",
        "non_increasing_count",
        "out_of_range_count",
        "off_grid_count",
        "missing_candle_count",
    )
    partition_list = list(partitions)

    def count(partition: Mapping[str, object], key: str) -> int:
        value = partition[key]
        if not isinstance(value, int):
            raise TypeError(f"quality count {key} must be an integer")
        return value

    error_count = sum(count(partition, key) for partition in partition_list for key in error_keys)
    return error_count, 0


def build_quality_markdown(snapshot_id: str, quality: Iterable[Mapping[str, object]]) -> bytes:
    lines = [
        f"# Source structure scan: {snapshot_id}",
        "",
        "该报告只验证原始 Feather 的字节证据、时间戳顺序、时间网格和请求边界，"
        "不读取 OHLC/volume、不创建 clean 数据，也不运行策略或查看收益。"
        "P1-04 负责仅在开发池运行数值质量流水线。",
        "",
        "| Pair | Timeframe | Expected | Missing | Duplicate | Non-increasing | "
        "Off-grid | Out-of-range |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for partition in quality:
        lines.append(
            "| {pair} | {timeframe} | {expected_candle_count} | {missing_candle_count} | "
            "{duplicate_count} | {non_increasing_count} | {off_grid_count} | "
            "{out_of_range_count} |".format(**partition)
        )
    return ("\n".join(lines) + "\n").encode()


def validate_manifest(project_root: Path, manifest: Mapping[str, object]) -> None:
    yaml = importlib.import_module("yaml")
    jsonschema = importlib.import_module("jsonschema")
    schema_path = project_root / "data/schemas/data-manifest.schema.yaml"
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )
    validator.validate(manifest)


def _repo_path(project_root: Path, relative_path: str) -> Path:
    """将清单相对路径限制在项目根目录内，避免校验器读取仓库外文件。"""

    candidate = (project_root / relative_path).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError as error:
        raise ValueError(f"manifest path escapes project root: {relative_path}") from error
    return candidate


def verify_partition_files(
    source_root: Path,
    partitions: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """重新计算分区大小与哈希；任何不一致都拒绝该快照。"""

    verified: list[dict[str, object]] = []
    source_root = source_root.resolve()
    for partition in partitions:
        relative_path = partition.get("relative_path")
        expected_size = partition.get("byte_size")
        expected_hash = partition.get("file_sha256")
        if not isinstance(relative_path, str):
            raise TypeError("partition.relative_path must be a string")
        path = (source_root / relative_path).resolve()
        try:
            path.relative_to(source_root)
        except ValueError as error:
            raise ValueError(f"partition path escapes source root: {relative_path}") from error
        if not path.is_file():
            raise FileNotFoundError(f"partition is missing: {path}")
        actual_size = path.stat().st_size
        actual_hash = file_sha256(path)
        if actual_size != expected_size:
            raise RuntimeError(
                f"partition size mismatch for {relative_path}: "
                f"expected {expected_size}, got {actual_size}"
            )
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"partition hash mismatch for {relative_path}: "
                f"expected {expected_hash}, got {actual_hash}"
            )
        verified.append(
            {
                "relative_path": relative_path,
                "file_sha256": actual_hash,
                "byte_size": actual_size,
            }
        )
    return verified


def verify_snapshot(project_root: Path, manifest_path: Path) -> dict[str, object]:
    """独立重读 manifest 与 source 文件，复核 P1-03 的不可变证据。"""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise TypeError("manifest must be a JSON object")
    validate_manifest(project_root, manifest)

    dataset_id = manifest["dataset_id"]
    storage = manifest["storage"]
    immutability = manifest["immutability"]
    source = manifest["source"]
    quality = manifest["quality"]
    partitions = manifest["partitions"]
    if not all(
        isinstance(value, dict) for value in (storage, immutability, source, quality)
    ) or not isinstance(partitions, list):
        raise TypeError("manifest object structure is invalid")

    recorded_manifest_hash = immutability["manifest_content_sha256"]
    actual_manifest_hash = canonical_manifest_sha256(manifest)
    if recorded_manifest_hash != actual_manifest_hash:
        raise RuntimeError(
            "manifest content hash mismatch: "
            f"expected {recorded_manifest_hash}, got {actual_manifest_hash}"
        )

    source_root = _repo_path(project_root, storage["source_root"])
    verified = verify_partition_files(source_root, partitions)
    actual_snapshot_hash = snapshot_sha256(verified)
    if actual_snapshot_hash != immutability["snapshot_sha256"]:
        raise RuntimeError(
            "snapshot hash mismatch: "
            f"expected {immutability['snapshot_sha256']}, got {actual_snapshot_hash}"
        )

    metadata_path = _repo_path(
        project_root,
        (MANIFEST_RELATIVE_ROOT / f"{dataset_id}.exchange-metadata.json").as_posix(),
    )
    actual_metadata_hash = canonical_text_sha256(metadata_path)
    if actual_metadata_hash != source["exchange_metadata_sha256"]:
        raise RuntimeError(
            "exchange metadata hash mismatch: "
            f"expected {source['exchange_metadata_sha256']}, got {actual_metadata_hash}"
        )
    for report_key in ("report_json_path", "report_markdown_path"):
        report_path = _repo_path(project_root, quality[report_key])
        if not report_path.is_file():
            raise FileNotFoundError(f"quality report is missing: {report_path}")

    access_event_path = manifest_path.with_name(f"{dataset_id}.holdout-access.json")
    holdout_state = manifest["holdout_access"]["state"]
    if access_event_path.is_file():
        access_event = json.loads(access_event_path.read_text(encoding="utf-8"))
        if access_event.get("snapshot_id") != dataset_id:
            raise RuntimeError("holdout access event references another snapshot")
        holdout_state = access_event["resulting_state"]

    return {
        "status": "verified",
        "snapshot_id": dataset_id,
        "partition_count": len(verified),
        "snapshot_sha256": actual_snapshot_hash,
        "manifest_content_sha256": actual_manifest_hash,
        "exchange_metadata_sha256": actual_metadata_hash,
        "holdout_state": holdout_state,
    }


def _download_command(staging_root: Path) -> list[str]:
    return [
        "freqtrade",
        "download-data",
        "--exchange",
        "bybit",
        "--trading-mode",
        "spot",
        "--pairs",
        *PAIRS,
        "--timeframes",
        *TIMEFRAMES,
        "--timerange",
        "20220101-20260701",
        "--data-format-ohlcv",
        "feather",
        "--datadir",
        str(staging_root),
    ]


def create_snapshot(
    project_root: Path,
    resume_staging: Path | None = None,
) -> dict[str, object]:
    source_parent = project_root / SOURCE_RELATIVE_ROOT
    manifest_parent = project_root / MANIFEST_RELATIVE_ROOT
    source_parent.mkdir(parents=True, exist_ok=True)
    manifest_parent.mkdir(parents=True, exist_ok=True)

    if resume_staging is None:
        started_at = datetime.now(UTC).replace(microsecond=0)
        staging_root = source_parent / (f".staging-{started_at:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}")
        staging_root.mkdir()
        command = _download_command(staging_root)
        subprocess.run(command, cwd=project_root, check=True)
    else:
        staging_root = resume_staging.resolve()
        if staging_root.parent != source_parent.resolve() or not staging_root.name.startswith(
            ".staging-"
        ):
            raise ValueError("resume staging path must be a direct .staging-* child of source root")
        if not staging_root.is_dir():
            raise FileNotFoundError(f"resume staging directory is missing: {staging_root}")
        # 恢复流程不再访问 OHLCV 下载接口，只检查已完成的四个分区并发布。
        command = _download_command(staging_root)

    partitions, partition_quality = inspect_download(staging_root)
    completed_at = datetime.now(UTC).replace(microsecond=0)
    snapshot_hash = snapshot_sha256(partitions)
    snapshot_id = f"bybit-spot-ohlcv-{completed_at:%Y%m%dT%H%M%SZ}-{snapshot_hash[:12]}"
    final_root = source_parent / snapshot_id

    metadata = fetch_exchange_metadata(completed_at)
    metadata_content = json_bytes(metadata)
    metadata_hash = hashlib.sha256(metadata_content).hexdigest()
    error_count, warning_count = quality_totals(partition_quality)

    manifest_path = MANIFEST_RELATIVE_ROOT / f"{snapshot_id}.manifest.json"
    metadata_path = MANIFEST_RELATIVE_ROOT / f"{snapshot_id}.exchange-metadata.json"
    quality_json_path = MANIFEST_RELATIVE_ROOT / f"{snapshot_id}.quality.json"
    quality_markdown_path = MANIFEST_RELATIVE_ROOT / f"{snapshot_id}.quality.md"
    manifest: dict[str, object] = {
        "schema_version": 1,
        "dataset_id": snapshot_id,
        "created_at_utc": utc_text(completed_at),
        "source": {
            "exchange": "bybit",
            "market_type": "spot",
            "candle_type": "spot",
            "pairs": list(PAIRS),
            "timeframes": list(TIMEFRAMES),
            "timezone": "UTC",
            "requested_start": utc_text(REQUESTED_START),
            "requested_end_exclusive": utc_text(REQUESTED_END_EXCLUSIVE),
            "downloader": "freqtrade_download_data",
            "freqtrade_version": importlib.metadata.version("freqtrade"),
            "ccxt_version": importlib.metadata.version("ccxt"),
            "storage_format": "feather",
            "command": shlex.join(command),
            "raw_exchange_payload_retained": False,
            "exchange_metadata_sha256": metadata_hash,
        },
        "immutability": {
            "hash_algorithm": "sha256",
            "snapshot_id": snapshot_id,
            "snapshot_sha256": snapshot_hash,
            "manifest_content_sha256": "0" * 64,
            "write_policy": "append_only_new_snapshot",
        },
        "storage": {
            "source_root": (SOURCE_RELATIVE_ROOT / snapshot_id).as_posix(),
            "clean_root": (Path("data/clean") / snapshot_id).as_posix(),
            "feature_root": (Path("data/features") / snapshot_id).as_posix(),
            "final_holdout_root": (Path("data/final_holdout") / snapshot_id).as_posix(),
            "roots_must_be_distinct": True,
        },
        "partitions": partitions,
        "quality": {
            "fill_missing": False,
            "duplicate_action": "reject_partition",
            "missing_action": "reject_partition",
            "invalid_ohlc_action": "reject_partition",
            "negative_volume_action": "reject_partition",
            "zero_volume_action": "warn_and_retain",
            "incomplete_last_candle_action": "drop_and_record",
            "error_count": error_count,
            "warning_count": warning_count,
            "report_json_path": quality_json_path.as_posix(),
            "report_markdown_path": quality_markdown_path.as_posix(),
        },
        "split_contract": {
            "development_start": "2022-01-01T00:00:00Z",
            "development_end_exclusive": "2025-07-01T00:00:00Z",
            "final_holdout_start": "2025-07-01T00:00:00Z",
            "final_holdout_end_exclusive": "2026-07-01T00:00:00Z",
            "final_holdout_left_closed_right_open": True,
            "walk_forward_manifest": "data/manifests/regime-manifest.yaml",
        },
        "holdout_access": {
            "state": "SEALED_UNREAD",
            "access_count": 0,
            "first_accessed_at_utc": None,
            "first_access_commit": None,
            "degraded_reason": None,
        },
    }
    immutability = manifest["immutability"]
    assert isinstance(immutability, dict)
    immutability["manifest_content_sha256"] = canonical_manifest_sha256(manifest)
    validate_manifest(project_root, manifest)

    quality_document: dict[str, object] = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "scan_role": "p1_03_source_structure_only",
        "value_columns_scanned": False,
        "clean_dataset_created": False,
        "strategy_or_returns_evaluated": False,
        "error_count": error_count,
        "warning_count": warning_count,
        "partitions": partition_quality,
    }
    quality_markdown = build_quality_markdown(snapshot_id, partition_quality)

    publish_snapshot(staging_root, final_root)
    write_new_file(project_root / metadata_path, metadata_content)
    write_new_file(project_root / quality_json_path, json_bytes(quality_document))
    write_new_file(project_root / quality_markdown_path, quality_markdown)
    write_new_file(project_root / manifest_path, json_bytes(manifest))
    return {
        "status": "ok",
        "snapshot_id": snapshot_id,
        "snapshot_sha256": snapshot_hash,
        "manifest_path": manifest_path.as_posix(),
        "source_root": (SOURCE_RELATIVE_ROOT / snapshot_id).as_posix(),
        "error_count": error_count,
        "warning_count": warning_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--verify-manifest",
        type=Path,
        help="只读复核已有 manifest、metadata 与 source 文件，不执行下载",
    )
    parser.add_argument(
        "--resume-staging",
        type=Path,
        help="只检查并发布指定的 .staging-* 下载结果，不重复下载 OHLCV",
    )
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    if args.verify_manifest is not None and args.resume_staging is not None:
        parser.error("--verify-manifest and --resume-staging are mutually exclusive")
    if args.verify_manifest is None:
        report = create_snapshot(project_root, args.resume_staging)
    else:
        manifest_path = args.verify_manifest
        if not manifest_path.is_absolute():
            manifest_path = project_root / manifest_path
        report = verify_snapshot(project_root, manifest_path.resolve())
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
