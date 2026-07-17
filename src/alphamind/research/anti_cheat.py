"""P2-06 可独立测试的反作弊合同。"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar


@dataclass(frozen=True)
class SourceFinding:
    """保存静态扫描命中的规则和精确源码位置。"""

    rule: str
    line: int
    column: int


def _negative_number(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
    )


def _attribute_chain(node: ast.AST) -> tuple[str, ...]:
    names: list[str] = []
    current = node
    while isinstance(current, (ast.Call, ast.Attribute)):
        if isinstance(current, ast.Call):
            current = current.func
            continue
        names.append(current.attr)
        current = current.value
    return tuple(reversed(names))


def _axis_is_row_wise(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "axis" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value in (1, "columns")
    return False


class _StrategySourceVisitor(ast.NodeVisitor):
    """只扫描 strategy 源码；报告器自身允许使用位置索引。"""

    _AGGREGATIONS: ClassVar[set[str]] = {
        "max",
        "mean",
        "median",
        "min",
        "quantile",
        "rank",
        "std",
        "var",
    }
    _SAFE_WINDOWS: ClassVar[set[str]] = {"ewm", "expanding", "rolling"}
    _FORBIDDEN_CALLS: ClassVar[dict[str, str]] = {
        "backfill": "backward_fill",
        "bfill": "backward_fill",
        "fit_transform": "full_sample_transform",
    }

    def __init__(self) -> None:
        self.findings: list[SourceFinding] = []

    def _add(self, node: ast.AST, rule: str) -> None:
        self.findings.append(
            SourceFinding(
                rule=rule,
                line=getattr(node, "lineno", 0),
                column=getattr(node, "col_offset", 0),
            )
        )

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.value, ast.Attribute) and node.value.attr in {"iat", "iloc"}:
            self._add(node, "absolute_positional_dataframe_access")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        chain = _attribute_chain(node.func)
        name = chain[-1] if chain else ""
        if name == "shift":
            periods = node.args[0] if node.args else None
            for keyword in node.keywords:
                if keyword.arg == "periods":
                    periods = keyword.value
            if periods is not None and _negative_number(periods):
                self._add(node, "negative_shift")
        if name in self._FORBIDDEN_CALLS:
            self._add(node, self._FORBIDDEN_CALLS[name])
        if name in self._AGGREGATIONS:
            uses_window = any(part in self._SAFE_WINDOWS for part in chain[:-1])
            if not uses_window and not _axis_is_row_wise(node):
                self._add(node, "full_sample_aggregation")
        for keyword in node.keywords:
            if (
                keyword.arg == "center"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                self._add(node, "centered_window")
        self.generic_visit(node)


def scan_strategy_source(source: str) -> tuple[SourceFinding, ...]:
    """拒绝已知未来访问和全样本归一化模式。"""

    tree = ast.parse(source)
    visitor = _StrategySourceVisitor()
    visitor.visit(tree)
    return tuple(sorted(visitor.findings, key=lambda item: (item.line, item.column, item.rule)))


class DatasetAccessGuard:
    """把研究读取限制在显式批准的数据根目录内。"""

    def __init__(self, project_root: Path, allowed_roots: Iterable[Path]) -> None:
        self.project_root = project_root.resolve()
        self.allowed_roots = tuple(path.resolve() for path in allowed_roots)
        if not self.allowed_roots:
            raise ValueError("at least one allowed dataset root is required")

    def approve(self, candidate: Path) -> Path:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.project_root)
        except ValueError as error:
            raise ValueError(f"dataset path escapes project root: {candidate}") from error
        if not any(_is_relative_to(resolved, root) for root in self.allowed_roots):
            raise PermissionError(f"dataset path is outside approved roots: {candidate}")
        return resolved


def _is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value}")
    return parsed.astimezone(UTC)


def validate_signal_execution_separation(
    trades: Sequence[Mapping[str, Any]], expected_interval: timedelta
) -> dict[str, int]:
    """所有入场必须严格发生在 signal candle 的下一根 candle。"""

    if expected_interval <= timedelta(0):
        raise ValueError("expected_interval must be positive")
    if not trades:
        raise ValueError("at least one trade is required")
    for index, trade in enumerate(trades):
        signal_value = trade.get("entry_signal_timestamp")
        entry_value = trade.get("entry_timestamp")
        if not isinstance(signal_value, str) or not isinstance(entry_value, str):
            raise TypeError(f"trade {index} is missing entry timestamps")
        delta = parse_utc(entry_value) - parse_utc(signal_value)
        if delta != expected_interval:
            raise RuntimeError(
                f"trade {index} entered after {delta}, expected exactly {expected_interval}"
            )
    return {"checked_trade_count": len(trades), "timing_mismatch_count": 0}


def validate_registry_selection(
    registry: Mapping[str, Any], summary: Mapping[str, Any]
) -> dict[str, int | str]:
    """拒绝未登记 trial、提前选择或伪造独立评审结果。"""

    entries = registry.get("entries")
    trials = summary.get("trials")
    selection = summary.get("selection")
    if (
        not isinstance(entries, list)
        or not isinstance(trials, list)
        or not isinstance(selection, dict)
    ):
        raise TypeError("registry or summary structure is invalid")
    registered = {entry.get("experiment_id") for entry in entries if isinstance(entry, dict)}
    reported = {trial.get("experiment_id") for trial in trials if isinstance(trial, dict)}
    if None in registered or None in reported or registered != reported:
        raise RuntimeError("walk-forward summary contains an unregistered or missing experiment")
    if len(registered) != len(entries) or len(reported) != len(trials):
        raise RuntimeError("duplicate experiment id detected")
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("registry entry must be an object")
        if entry.get("review_result") != "PENDING":
            raise RuntimeError("P2-06 cannot rewrite independent review state")
    if selection.get("parameter_selection") is not None:
        raise RuntimeError("parameter selection occurred before independent review")
    return {
        "registered_trial_count": len(registered),
        "reported_trial_count": len(reported),
        "review_state": "PENDING",
    }


def validate_timestamp_boundary(
    timestamps: Sequence[datetime], end_exclusive: datetime
) -> dict[str, str | int]:
    """确保反作弊输入没有越过冻结的 development 边界。"""

    if not timestamps:
        raise ValueError("dataset timestamps cannot be empty")
    normalized = [value.astimezone(UTC) for value in timestamps]
    if any(value >= end_exclusive for value in normalized):
        raise RuntimeError("dataset crosses the development end boundary")
    return {
        "candle_count": len(normalized),
        "first_timestamp": normalized[0].isoformat().replace("+00:00", "Z"),
        "last_timestamp": normalized[-1].isoformat().replace("+00:00", "Z"),
    }
