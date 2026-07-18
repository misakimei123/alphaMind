"""R2-03 并发安全的模型 usage、成本与预算账本。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]
NANO_USD_PER_USD = Decimal("1000000000")
NANO_USD_PER_MICRO_USD = Decimal("1000")


class UsageLedgerError(RuntimeError):
    """usage 账本无法可靠读写。"""


class BudgetExceededError(UsageLedgerError):
    """预留下一次调用会超过周期或 UTC 日预算。"""


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return value.astimezone(UTC)


def _usd_to_nano(value: Decimal) -> int:
    converted = value * NANO_USD_PER_USD
    if converted != converted.to_integral_value():
        raise ValueError("USD amount cannot be represented in nano-USD")
    return int(converted)


def _nano_to_usd(value: int) -> str:
    return format(Decimal(value) / NANO_USD_PER_USD, ".9f")


def _price_to_nano_per_token(value: object, *, label: str) -> int:
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{label} is invalid") from None
    if not price.is_finite() or price < 0:
        raise ValueError(f"{label} is invalid")
    converted = price * NANO_USD_PER_MICRO_USD
    if converted != converted.to_integral_value():
        raise ValueError(f"{label} has unsupported precision")
    return int(converted)


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.cached_input_tokens, self.output_tokens) < 0:
            raise ValueError("usage token counts must be non-negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass(frozen=True, slots=True)
class CostPolicy:
    input_nano_usd_per_token: int
    cached_input_nano_usd_per_token: int
    output_nano_usd_per_token: int
    maximum_cost_per_cycle_nano_usd: int
    maximum_cost_per_utc_day_nano_usd: int
    maximum_attempt_cost_nano_usd: int

    @classmethod
    def from_profile(cls, profile: JsonObject) -> CostPolicy:
        cost = profile["cost"]
        request = profile["request"]
        input_rate = _price_to_nano_per_token(
            cost["input_per_million_tokens"], label="input token price"
        )
        cached_rate = _price_to_nano_per_token(
            cost["cached_input_per_million_tokens"], label="cached input token price"
        )
        output_rate = _price_to_nano_per_token(
            cost["output_per_million_tokens"], label="output token price"
        )
        cycle_cap = _usd_to_nano(Decimal(str(cost["maximum_cost_per_cycle"])))
        daily_cap = _usd_to_nano(Decimal(str(cost["maximum_cost_per_utc_day"])))
        maximum_attempt = (
            int(request["max_input_tokens"]) * input_rate
            + int(request["max_output_tokens"]) * output_rate
        )
        if maximum_attempt > cycle_cap:
            raise ValueError("maximum attempt cost exceeds the cycle cap")
        return cls(input_rate, cached_rate, output_rate, cycle_cap, daily_cap, maximum_attempt)

    def usage_cost_nano_usd(self, usage: Usage) -> int:
        uncached = usage.input_tokens - usage.cached_input_tokens
        return (
            uncached * self.input_nano_usd_per_token
            + usage.cached_input_tokens * self.cached_input_nano_usd_per_token
            + usage.output_tokens * self.output_nano_usd_per_token
        )


@dataclass(frozen=True, slots=True)
class UsageSummary:
    attempts: int
    usage: Usage
    accounted_cost_usd: str

    def to_dict(self) -> JsonObject:
        return {
            "attempts": self.attempts,
            "usage": self.usage.to_dict(),
            "accounted_cost_usd": self.accounted_cost_usd,
        }


class UsageLedger:
    """在发请求前用 ``BEGIN IMMEDIATE`` 原子预留预算。"""

    def __init__(self, path: str | Path, policy: CostPolicy) -> None:
        self.path = Path(path).resolve()
        self.policy = policy
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                if str(mode).lower() != "wal":
                    raise UsageLedgerError("AI usage ledger requires SQLite WAL mode")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS ai_usage_schema (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL CHECK (schema_version = 1)
                    );
                    INSERT OR IGNORE INTO ai_usage_schema(singleton, schema_version)
                    VALUES (1, 1);
                    CREATE TABLE IF NOT EXISTS ai_attempts (
                        attempt_id TEXT PRIMARY KEY,
                        cycle_id TEXT NOT NULL,
                        attempt_number INTEGER NOT NULL CHECK (attempt_number BETWEEN 1 AND 2),
                        utc_day TEXT NOT NULL,
                        attempted_at_utc TEXT NOT NULL,
                        settled_at_utc TEXT,
                        status TEXT NOT NULL CHECK (status IN ('RESERVED', 'COMPLETED', 'FAILED')),
                        outcome_code TEXT,
                        input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
                        cached_input_tokens INTEGER NOT NULL DEFAULT 0
                            CHECK (cached_input_tokens >= 0),
                        output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
                        reserved_cost_nano_usd INTEGER NOT NULL
                            CHECK (reserved_cost_nano_usd >= 0),
                        accounted_cost_nano_usd INTEGER NOT NULL
                            CHECK (accounted_cost_nano_usd >= 0),
                        provider_response_id TEXT,
                        provider_request_id TEXT,
                        model_id TEXT,
                        prompt_sha256 TEXT NOT NULL,
                        config_sha256 TEXT NOT NULL,
                        input_sha256 TEXT NOT NULL,
                        UNIQUE(cycle_id, attempt_number)
                    );
                    CREATE INDEX IF NOT EXISTS ai_attempts_utc_day
                        ON ai_attempts(utc_day, attempted_at_utc);
                    """
                )
                version = connection.execute(
                    "SELECT schema_version FROM ai_usage_schema WHERE singleton = 1"
                ).fetchone()
                if version is None or version[0] != 1:
                    raise UsageLedgerError("AI usage ledger schema version is unsupported")
        except UsageLedgerError:
            raise
        except (OSError, sqlite3.Error):
            raise UsageLedgerError("AI usage ledger could not be initialized") from None

    def reserve(
        self,
        *,
        cycle_id: str,
        attempt_number: int,
        attempted_at_utc: datetime,
        prompt_sha256: str,
        config_sha256: str,
        input_sha256: str,
    ) -> str:
        attempted_at = _utc(attempted_at_utc)
        utc_day = attempted_at.date().isoformat()
        attempt_id = f"{cycle_id}:{attempt_number}"
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cycle_total = int(
                    connection.execute(
                        "SELECT COALESCE(SUM(accounted_cost_nano_usd), 0) "
                        "FROM ai_attempts WHERE cycle_id = ?",
                        (cycle_id,),
                    ).fetchone()[0]
                )
                daily_total = int(
                    connection.execute(
                        "SELECT COALESCE(SUM(accounted_cost_nano_usd), 0) "
                        "FROM ai_attempts WHERE utc_day = ?",
                        (utc_day,),
                    ).fetchone()[0]
                )
                reservation = self.policy.maximum_attempt_cost_nano_usd
                if cycle_total + reservation > self.policy.maximum_cost_per_cycle_nano_usd:
                    raise BudgetExceededError("AI cycle cost budget is exhausted")
                if daily_total + reservation > self.policy.maximum_cost_per_utc_day_nano_usd:
                    raise BudgetExceededError("AI UTC-day cost budget is exhausted")
                connection.execute(
                    """
                    INSERT INTO ai_attempts(
                        attempt_id, cycle_id, attempt_number, utc_day, attempted_at_utc,
                        status, reserved_cost_nano_usd, accounted_cost_nano_usd,
                        prompt_sha256, config_sha256, input_sha256
                    ) VALUES (?, ?, ?, ?, ?, 'RESERVED', ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        cycle_id,
                        attempt_number,
                        utc_day,
                        _utc_text(attempted_at),
                        reservation,
                        reservation,
                        prompt_sha256,
                        config_sha256,
                        input_sha256,
                    ),
                )
                connection.execute("COMMIT")
                return attempt_id
        except BudgetExceededError:
            raise
        except sqlite3.IntegrityError:
            raise BudgetExceededError("AI attempt was already reserved for this cycle") from None
        except (OSError, sqlite3.Error):
            raise UsageLedgerError("AI usage budget could not be reserved") from None

    def settle(
        self,
        attempt_id: str,
        *,
        settled_at_utc: datetime,
        status: str,
        outcome_code: str,
        usage: Usage | None,
        charge_reservation: bool,
        provider_response_id: str | None = None,
        provider_request_id: str | None = None,
        model_id: str | None = None,
    ) -> None:
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("settled status is invalid")
        settled_at = _utc(settled_at_utc)
        actual_usage = usage or Usage(0, 0, 0)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT status, reserved_cost_nano_usd FROM ai_attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                if row is None or row["status"] != "RESERVED":
                    raise UsageLedgerError("AI usage attempt is missing or already settled")
                if usage is not None:
                    accounted = self.policy.usage_cost_nano_usd(usage)
                elif charge_reservation:
                    accounted = int(row["reserved_cost_nano_usd"])
                else:
                    accounted = 0
                connection.execute(
                    """
                    UPDATE ai_attempts
                    SET settled_at_utc = ?, status = ?, outcome_code = ?, input_tokens = ?,
                        cached_input_tokens = ?, output_tokens = ?, accounted_cost_nano_usd = ?,
                        provider_response_id = ?, provider_request_id = ?, model_id = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        _utc_text(settled_at),
                        status,
                        outcome_code,
                        actual_usage.input_tokens,
                        actual_usage.cached_input_tokens,
                        actual_usage.output_tokens,
                        accounted,
                        provider_response_id,
                        provider_request_id,
                        model_id,
                        attempt_id,
                    ),
                )
                connection.execute("COMMIT")
        except UsageLedgerError:
            raise
        except (OSError, sqlite3.Error):
            raise UsageLedgerError("AI usage attempt could not be settled") from None

    def cycle_summary(self, cycle_id: str) -> UsageSummary:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS attempts,
                           COALESCE(SUM(input_tokens), 0) AS input_tokens,
                           COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                           COALESCE(SUM(output_tokens), 0) AS output_tokens,
                           COALESCE(SUM(accounted_cost_nano_usd), 0) AS cost
                    FROM ai_attempts WHERE cycle_id = ?
                    """,
                    (cycle_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            raise UsageLedgerError("AI usage summary could not be read") from None
        return UsageSummary(
            attempts=int(row["attempts"]),
            usage=Usage(
                int(row["input_tokens"]),
                int(row["cached_input_tokens"]),
                int(row["output_tokens"]),
            ),
            accounted_cost_usd=_nano_to_usd(int(row["cost"])),
        )
