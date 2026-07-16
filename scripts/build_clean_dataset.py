"""P1-04：从不可变 source snapshot 构建仅含开发池的 clean 数据与质量证据。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from alphamind.research.data_quality import validate_partition
from scripts.create_source_snapshot import (
    file_sha256,
    json_bytes,
    publish_snapshot,
    snapshot_sha256,
    utc_text,
    verify_snapshot,
    write_new_file,
)

VALUE_COLUMNS = ("date", "open", "high", "low", "close", "volume")
CLEAN_RELATIVE_ROOT = Path("data/clean")
QUALITY_RELATIVE_ROOT = Path("data/manifests/quality")
QUALITY_RULESET = "p1-04-v1"


def _repo_path(project_root: Path, relative_path: str) -> Path:
    candidate = (project_root / relative_path).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError as error:
        raise ValueError(f"path escapes project root: {relative_path}") from error
    return candidate


def _canonical_report_sha256(report: dict[str, object]) -> str:
    canonical = copy.deepcopy(report)
    canonical.pop("report_content_sha256", None)
    canonical.pop("report_markdown_sha256", None)
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _required_str(document: dict[str, Any], key: str) -> str:
    value = document[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _required_int(document: dict[str, Any], key: str) -> int:
    value = document[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{key} must be an integer")
    return value


def _quality_count(document: dict[str, object], key: str) -> int:
    value = document[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"partition quality {key} must be an integer")
    return value


def _quality_status(error_count: int, warning_count: int) -> str:
    if error_count:
        return "REJECTED"
    if warning_count:
        return "ACCEPTED_WITH_WARNINGS"
    return "ACCEPTED"


def _utc_boundary(document: dict[str, Any], key: str) -> datetime:
    value = _required_str(document, key)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{key} must be an explicit UTC timestamp")
    return parsed.astimezone(UTC)


def _development_bounds(
    source_manifest: dict[str, Any],
    holdout_state: object,
) -> tuple[datetime, datetime]:
    """按 holdout 状态确定可读取边界；未知状态一律拒绝继续。"""

    split_contract = source_manifest["split_contract"]
    source = source_manifest["source"]
    if not isinstance(split_contract, dict) or not isinstance(source, dict):
        raise TypeError("source manifest split_contract/source must be objects")

    start = _utc_boundary(split_contract, "development_start")
    if holdout_state == "SEALED_UNREAD":
        end = _utc_boundary(split_contract, "development_end_exclusive")
    elif holdout_state == "DEGRADED_TO_DEVELOPMENT":
        # 严格降级后原 holdout 已成为开发数据，但仍必须排除请求右边界之后的附带 candle。
        end = _utc_boundary(source, "requested_end_exclusive")
    else:
        raise RuntimeError(f"unsupported holdout state for data quality: {holdout_state}")
    if start >= end:
        raise ValueError("development interval must be non-empty")
    return start, end


def _load_development_frame(path: Path, start: datetime, end: datetime) -> Any:
    """在 Arrow scanner 层限定当前开发池，禁止载入边界外数值列。"""

    pa = importlib.import_module("pyarrow")
    ds = importlib.import_module("pyarrow.dataset")
    dataset = ds.dataset(str(path), format="ipc")
    predicate = (ds.field("date") >= pa.scalar(start)) & (ds.field("date") < pa.scalar(end))
    table = dataset.to_table(columns=list(VALUE_COLUMNS), filter=predicate)
    return table.to_pandas().reset_index(drop=True)


def _quality_markdown(report: dict[str, object]) -> bytes:
    partitions = report["partitions"]
    if not isinstance(partitions, list):
        raise TypeError("report.partitions must be a list")
    lines = [
        f"# Data quality report: {report['dataset_id']}",
        "",
        f"- Status: `{report['status']}`",
        f"- Source snapshot: `{report['source_snapshot_id']}`",
        f"- Scope: `{report['development_start']}` to `{report['development_end_exclusive']}`",
        f"- Errors: `{report['error_count']}`",
        f"- Warnings: `{report['warning_count']}`",
        f"- Clean published: `{str(report['clean_published']).lower()}`",
        f"- Report SHA-256: `{report['report_content_sha256']}`",
        "",
        "质量流水线不填补、不插值、不去重、不重排 source。ERROR 阻止 clean 发布；"
        "零成交量和固定阈值跳变只作为 WARN 保留。",
        "",
        "| Pair | Timeframe | Rows | Expected | Errors | Warnings | Result |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for partition in partitions:
        if not isinstance(partition, dict):
            raise TypeError("report partition must be an object")
        lines.append(
            "| {pair} | {timeframe} | {input_row_count} | {expected_candle_count} | "
            "{error_count} | {warning_count} | {status} |".format(**partition)
        )
        counts = partition.get("counts_by_code")
        if isinstance(counts, dict) and counts:
            rendered = ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
            lines.append(f"| ↳ issue counts |  |  |  |  |  | `{rendered}` |")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _quality_output_paths(dataset_id: str) -> tuple[Path, Path]:
    root = QUALITY_RELATIVE_ROOT / dataset_id
    return root / "report.json", root / "report.md"


def verify_clean_report(project_root: Path, report_path: Path) -> dict[str, object]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise TypeError("quality report must be a JSON object")
    recorded_report_hash = _required_str(report, "report_content_sha256")
    actual_report_hash = _canonical_report_sha256(report)
    if recorded_report_hash != actual_report_hash:
        raise RuntimeError(
            "quality report hash mismatch: "
            f"expected {recorded_report_hash}, got {actual_report_hash}"
        )

    recorded_report_path = _repo_path(project_root, _required_str(report, "report_json_path"))
    if recorded_report_path != report_path.resolve():
        raise RuntimeError("quality report path does not match its recorded path")

    markdown_path = _repo_path(project_root, _required_str(report, "report_markdown_path"))
    actual_markdown_hash = file_sha256(markdown_path)
    recorded_markdown_hash = _required_str(report, "report_markdown_sha256")
    if actual_markdown_hash != recorded_markdown_hash:
        raise RuntimeError(
            "quality Markdown hash mismatch: "
            f"expected {recorded_markdown_hash}, got {actual_markdown_hash}"
        )

    source_manifest = _repo_path(project_root, _required_str(report, "source_manifest_path"))
    source_verification = verify_snapshot(project_root, source_manifest)
    clean_published = report["clean_published"]
    if not isinstance(clean_published, bool):
        raise TypeError("clean_published must be a boolean")
    downstream_allowed = report["downstream_experiment_allowed"]
    if not isinstance(downstream_allowed, bool):
        raise TypeError("downstream_experiment_allowed must be a boolean")
    error_count = _required_int(report, "error_count")
    if clean_published != downstream_allowed or clean_published != (error_count == 0):
        raise RuntimeError("quality gate publication state is inconsistent")

    clean_hash: str | None = None
    clean_partitions = report["clean_partitions"]
    if not isinstance(clean_partitions, list):
        raise TypeError("clean_partitions must be a list")
    if clean_published:
        clean_root_text = report["clean_root"]
        if not isinstance(clean_root_text, str):
            raise TypeError("published clean report must provide clean_root")
        clean_root = _repo_path(project_root, clean_root_text)
        verified_partitions: list[dict[str, object]] = []
        for partition in clean_partitions:
            if not isinstance(partition, dict):
                raise TypeError("clean partition must be an object")
            relative_path = _required_str(partition, "relative_path")
            path = (clean_root / relative_path).resolve()
            try:
                path.relative_to(clean_root)
            except ValueError as error:
                raise ValueError(f"clean partition escapes clean root: {relative_path}") from error
            actual_size = path.stat().st_size
            expected_size = _required_int(partition, "byte_size")
            actual_file_hash = file_sha256(path)
            expected_file_hash = _required_str(partition, "file_sha256")
            if actual_size != expected_size or actual_file_hash != expected_file_hash:
                raise RuntimeError(f"clean partition evidence mismatch: {relative_path}")
            verified_partitions.append(
                {
                    "relative_path": relative_path,
                    "file_sha256": actual_file_hash,
                }
            )
        clean_hash = snapshot_sha256(verified_partitions)
        if clean_hash != report["clean_snapshot_sha256"]:
            raise RuntimeError("clean snapshot hash mismatch")
    elif clean_partitions or report["clean_root"] is not None:
        raise RuntimeError("rejected report must not reference published clean data")

    return {
        "status": "verified",
        "dataset_id": report["dataset_id"],
        "quality_status": report["status"],
        "source_verification_status": source_verification["status"],
        "holdout_state": source_verification["holdout_state"],
        "clean_published": clean_published,
        "downstream_experiment_allowed": downstream_allowed,
        "clean_snapshot_sha256": clean_hash,
        "report_content_sha256": actual_report_hash,
        "report_markdown_sha256": actual_markdown_hash,
    }


def build_clean_dataset(project_root: Path, source_manifest_path: Path) -> dict[str, object]:
    source_verification = verify_snapshot(project_root, source_manifest_path)
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(source_manifest, dict):
        raise TypeError("source manifest must be a JSON object")

    source_snapshot_id = source_manifest["dataset_id"]
    immutability = source_manifest["immutability"]
    storage = source_manifest["storage"]
    partitions = source_manifest["partitions"]
    if not isinstance(source_snapshot_id, str):
        raise TypeError("source dataset_id must be a string")
    if not isinstance(immutability, dict) or not isinstance(storage, dict):
        raise TypeError("source manifest immutability/storage must be objects")
    if not isinstance(partitions, list):
        raise TypeError("source manifest partitions must be a list")

    development_start, development_end_exclusive = _development_bounds(
        source_manifest,
        source_verification["holdout_state"],
    )

    source_snapshot_hash = immutability["snapshot_sha256"]
    if not isinstance(source_snapshot_hash, str):
        raise TypeError("source snapshot hash must be a string")
    dataset_id = f"bybit-spot-development-{source_snapshot_hash[:12]}-{QUALITY_RULESET}"
    clean_root = project_root / CLEAN_RELATIVE_ROOT / dataset_id
    quality_root = project_root / QUALITY_RELATIVE_ROOT / dataset_id
    if clean_root.exists() or quality_root.exists():
        raise FileExistsError(f"clean dataset or quality evidence already exists: {dataset_id}")

    source_root = _repo_path(project_root, _required_str(storage, "source_root"))
    frames: list[tuple[dict[str, Any], Any]] = []
    partition_reports: list[dict[str, object]] = []
    for partition in partitions:
        if not isinstance(partition, dict):
            raise TypeError("source partition must be an object")
        pair = _required_str(partition, "pair")
        timeframe = _required_str(partition, "timeframe")
        relative_path = _required_str(partition, "relative_path")
        source_path = (source_root / relative_path).resolve()
        try:
            source_path.relative_to(source_root)
        except ValueError as error:
            raise ValueError(f"source partition escapes source root: {relative_path}") from error

        frame = _load_development_frame(
            source_path,
            development_start,
            development_end_exclusive,
        )
        rows = frame.to_dict(orient="records")
        quality = validate_partition(
            rows,
            timeframe=timeframe,
            interval_start=development_start,
            interval_end_exclusive=development_end_exclusive,
        )
        quality.update(
            {
                "pair": pair,
                "source_relative_path": relative_path,
                "source_file_sha256": _required_str(partition, "file_sha256"),
                "source_total_row_count": _required_int(partition, "candle_count"),
                "rows_excluded_outside_development_pool": (
                    _required_int(partition, "candle_count") - len(frame)
                ),
            }
        )
        partition_reports.append(quality)
        frames.append((partition, frame))

    error_count = sum(_quality_count(partition, "error_count") for partition in partition_reports)
    warning_count = sum(
        _quality_count(partition, "warning_count") for partition in partition_reports
    )
    accepted = error_count == 0
    clean_partitions: list[dict[str, object]] = []
    clean_hash: str | None = None

    staging_root = project_root / CLEAN_RELATIVE_ROOT / (f".staging-{dataset_id}-{uuid4().hex[:8]}")
    evidence_staging = staging_root / "evidence"
    data_staging = staging_root / "data"
    evidence_staging.mkdir(parents=True)

    if accepted:
        data_staging.mkdir()
        for partition, frame in frames:
            pair = _required_str(partition, "pair")
            timeframe = _required_str(partition, "timeframe")
            relative_path = _required_str(partition, "relative_path")
            clean_frame = frame.rename(columns={"date": "timestamp"}).copy()
            clean_frame["source_status"] = "observed"
            clean_frame = clean_frame[
                ["timestamp", "open", "high", "low", "close", "volume", "source_status"]
            ]
            output_path = data_staging / relative_path
            output_path.parent.mkdir(parents=True, exist_ok=True)
            clean_frame.to_feather(output_path, compression="zstd", compression_level=3)
            timestamps = importlib.import_module("pandas").to_datetime(
                clean_frame["timestamp"],
                utc=True,
            )
            clean_partitions.append(
                {
                    "pair": pair,
                    "timeframe": timeframe,
                    "relative_path": relative_path,
                    "byte_size": output_path.stat().st_size,
                    "file_sha256": file_sha256(output_path),
                    "candle_count": len(clean_frame),
                    "first_candle_utc": utc_text(timestamps.iloc[0].to_pydatetime()),
                    "last_candle_utc": utc_text(timestamps.iloc[-1].to_pydatetime()),
                }
            )
        clean_hash = snapshot_sha256(clean_partitions)

    report_json_path, report_markdown_path = _quality_output_paths(dataset_id)
    report: dict[str, object] = {
        "schema_version": 1,
        "ruleset": QUALITY_RULESET,
        "dataset_id": dataset_id,
        "source_snapshot_id": source_snapshot_id,
        "source_manifest_path": source_manifest_path.relative_to(project_root).as_posix(),
        "source_snapshot_sha256": source_snapshot_hash,
        "source_manifest_content_sha256": immutability["manifest_content_sha256"],
        "source_verification_status": source_verification["status"],
        "holdout_state": source_verification["holdout_state"],
        "development_start": utc_text(development_start),
        "development_end_exclusive": utc_text(development_end_exclusive),
        "value_columns_scope": "development_pool_only",
        "fill_missing": False,
        "deduplicate": False,
        "reorder": False,
        "interpolate": False,
        "error_action": "reject_clean_publication",
        "warning_action": "retain_and_report",
        "warning_acceptance_reasons": {
            "zero_volume": "保留交易所原始观测并进入后续流动性诊断",
            "abnormal_close_jump": "固定阈值诊断信号，不改变价格且不据此选择参数",
        },
        "status": _quality_status(error_count, warning_count),
        "error_count": error_count,
        "warning_count": warning_count,
        "clean_published": accepted,
        "downstream_experiment_allowed": accepted,
        "clean_root": (CLEAN_RELATIVE_ROOT / dataset_id).as_posix() if accepted else None,
        "clean_snapshot_sha256": clean_hash,
        "clean_partitions": clean_partitions,
        "partitions": partition_reports,
        "report_json_path": report_json_path.as_posix(),
        "report_markdown_path": report_markdown_path.as_posix(),
        "report_content_sha256": "0" * 64,
        "report_markdown_sha256": "0" * 64,
    }
    report["report_content_sha256"] = _canonical_report_sha256(report)
    quality_markdown = _quality_markdown(report)
    report["report_markdown_sha256"] = hashlib.sha256(quality_markdown).hexdigest()
    write_new_file(evidence_staging / "report.json", json_bytes(report))
    write_new_file(evidence_staging / "report.md", quality_markdown)

    if accepted:
        publish_snapshot(data_staging, clean_root)
    quality_root.parent.mkdir(parents=True, exist_ok=True)
    publish_snapshot(evidence_staging, quality_root)
    staging_root.rmdir()
    return {
        "status": report["status"],
        "dataset_id": dataset_id,
        "error_count": error_count,
        "warning_count": warning_count,
        "clean_published": accepted,
        "clean_snapshot_sha256": clean_hash,
        "report_content_sha256": report["report_content_sha256"],
        "report_json_path": report_json_path.as_posix(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--source-manifest", type=Path)
    mode.add_argument("--verify-report", type=Path)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    if args.verify_report is not None:
        report_path = args.verify_report
        if not report_path.is_absolute():
            report_path = project_root / report_path
        result = verify_clean_report(project_root, report_path.resolve())
    else:
        source_manifest = args.source_manifest
        assert source_manifest is not None
        if not source_manifest.is_absolute():
            source_manifest = project_root / source_manifest
        result = build_clean_dataset(project_root, source_manifest.resolve())
    print(json.dumps(result, sort_keys=True))
    return 2 if result["status"] == "REJECTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
