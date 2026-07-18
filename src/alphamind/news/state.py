"""R2-02 新闻增量 cursor 与跨周期去重状态。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


class NewsStateError(RuntimeError):
    """增量状态缺失一致性，必须 fail closed。"""


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise NewsStateError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise NewsStateError(f"{label} is invalid") from None
    if parsed.utcoffset() != timedelta(0):
        raise NewsStateError(f"{label} is invalid")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SourceCursor:
    last_success_at_utc: datetime | None = None
    high_watermark_utc: datetime | None = None
    high_watermark_ids: tuple[str, ...] = ()
    etag: str | None = None
    last_modified: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "last_success_at_utc": (
                utc_text(self.last_success_at_utc) if self.last_success_at_utc else None
            ),
            "high_watermark_utc": (
                utc_text(self.high_watermark_utc) if self.high_watermark_utc else None
            ),
            "high_watermark_ids": list(self.high_watermark_ids),
            "etag": self.etag,
            "last_modified": self.last_modified,
        }


@dataclass(slots=True)
class NewsState:
    sources: dict[str, SourceCursor]
    seen_fingerprints: dict[str, datetime]

    @classmethod
    def empty(cls) -> NewsState:
        return cls({}, {})

    def cursor(self, source_id: str) -> SourceCursor:
        return self.sources.get(source_id, SourceCursor())

    def prune(self, *, now_utc: datetime, retention: timedelta, maximum_seen: int) -> None:
        cutoff = now_utc - retention
        retained = [
            (fingerprint, seen_at)
            for fingerprint, seen_at in self.seen_fingerprints.items()
            if seen_at >= cutoff
        ]
        retained.sort(key=lambda row: (row[1], row[0]), reverse=True)
        self.seen_fingerprints = dict(retained[:maximum_seen])

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": 1,
            "sources": {key: value.to_dict() for key, value in sorted(self.sources.items())},
            "seen_fingerprints": {
                key: utc_text(value) for key, value in sorted(self.seen_fingerprints.items())
            },
        }


class NewsStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> NewsState:
        if not self.path.exists():
            return NewsState.empty()
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            return self._parse(document)
        except NewsStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise NewsStateError("news cursor state could not be loaded") from None

    @staticmethod
    def _parse(document: object) -> NewsState:
        if not isinstance(document, Mapping) or set(document) != {
            "schema_version",
            "sources",
            "seen_fingerprints",
        }:
            raise NewsStateError("news cursor state fields are invalid")
        if document["schema_version"] != 1:
            raise NewsStateError("news cursor state version is unsupported")
        raw_sources = document["sources"]
        raw_seen = document["seen_fingerprints"]
        if not isinstance(raw_sources, Mapping) or not isinstance(raw_seen, Mapping):
            raise NewsStateError("news cursor state collections are invalid")
        sources: dict[str, SourceCursor] = {}
        for source_id, raw_cursor in raw_sources.items():
            if not isinstance(source_id, str) or not isinstance(raw_cursor, Mapping):
                raise NewsStateError("news source cursor is invalid")
            expected = {
                "last_success_at_utc",
                "high_watermark_utc",
                "high_watermark_ids",
                "etag",
                "last_modified",
            }
            if set(raw_cursor) != expected:
                raise NewsStateError("news source cursor fields are invalid")
            ids = raw_cursor["high_watermark_ids"]
            if not isinstance(ids, list) or any(
                not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item) for item in ids
            ):
                raise NewsStateError("news source cursor identities are invalid")
            if len(ids) != len(set(ids)) or len(ids) > 100:
                raise NewsStateError("news source cursor identities are invalid")
            etag = raw_cursor["etag"]
            last_modified = raw_cursor["last_modified"]
            if etag is not None and not isinstance(etag, str):
                raise NewsStateError("news source cursor ETag is invalid")
            if last_modified is not None and not isinstance(last_modified, str):
                raise NewsStateError("news source cursor Last-Modified is invalid")
            sources[source_id] = SourceCursor(
                last_success_at_utc=(
                    parse_utc(raw_cursor["last_success_at_utc"], label="last_success_at_utc")
                    if raw_cursor["last_success_at_utc"] is not None
                    else None
                ),
                high_watermark_utc=(
                    parse_utc(raw_cursor["high_watermark_utc"], label="high_watermark_utc")
                    if raw_cursor["high_watermark_utc"] is not None
                    else None
                ),
                high_watermark_ids=tuple(ids),
                etag=etag,
                last_modified=last_modified,
            )
        seen: dict[str, datetime] = {}
        for fingerprint, timestamp in raw_seen.items():
            if not isinstance(fingerprint, str) or not re.fullmatch(
                r"[utc]:[a-f0-9]{64}", fingerprint
            ):
                raise NewsStateError("news fingerprint is invalid")
            seen[fingerprint] = parse_utc(timestamp, label="seen_fingerprint timestamp")
        return NewsState(sources, seen)

    def save(self, state: NewsState) -> None:
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8", newline="\n")
            os.replace(temporary, self.path)
        except OSError:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise NewsStateError("news cursor state could not be saved") from None
