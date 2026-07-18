"""只读新闻源适配器：Bybit V5 announcements 与 RSS/Atom。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import feedparser  # type: ignore[import-untyped]


class NewsAdapterError(ValueError):
    """源响应无法按已配置适配器安全解析。"""


@dataclass(frozen=True, slots=True)
class RawNewsItem:
    """尚未经过 Registry 绑定和文本清洗的源记录。"""

    source_identity: str
    published_at_utc: datetime
    title: str
    canonical_url: str
    summary: str
    category_hint: str | None
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdapterResult:
    items: tuple[RawNewsItem, ...]
    rejected_records: int


class NewsAdapter(Protocol):
    def parse(self, payload: bytes) -> AdapterResult: ...


def _utc_from_milliseconds(value: object) -> datetime:
    if type(value) is not int or value <= 0:
        raise NewsAdapterError("announcement timestamp is invalid")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise NewsAdapterError("announcement timestamp is invalid") from None


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NewsAdapterError(f"{label} is missing")
    return value


class BybitAnnouncementAdapter:
    """解析官方 ``GET /v5/announcements/index`` 响应。"""

    def parse(self, payload: bytes) -> AdapterResult:
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise NewsAdapterError("Bybit announcement response is not UTF-8 JSON") from None
        if not isinstance(decoded, dict):
            raise NewsAdapterError("Bybit announcement response must be an object")
        if decoded.get("retCode") != 0 or decoded.get("retMsg") != "OK":
            raise NewsAdapterError("Bybit announcement response returned a non-success code")
        result = decoded.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("list"), list):
            raise NewsAdapterError("Bybit announcement result list is invalid")

        parsed: list[RawNewsItem] = []
        rejected = 0
        for record in result["list"]:
            try:
                parsed.append(self._parse_record(record))
            except NewsAdapterError:
                rejected += 1
        return AdapterResult(tuple(parsed), rejected)

    @staticmethod
    def _parse_record(record: object) -> RawNewsItem:
        if not isinstance(record, Mapping):
            raise NewsAdapterError("announcement record must be an object")
        title = _required_text(record.get("title"), "announcement title")
        url = _required_text(record.get("url"), "announcement URL")
        description = _required_text(
            record.get("description") or title,
            "announcement description",
        )
        published_raw = record.get("publishTime", record.get("dateTimestamp"))
        published = _utc_from_milliseconds(published_raw)
        raw_type = record.get("type")
        type_key: str | None = None
        if isinstance(raw_type, Mapping) and isinstance(raw_type.get("key"), str):
            type_key = str(raw_type["key"])
        raw_tags = record.get("tags", [])
        if not isinstance(raw_tags, list) or any(not isinstance(tag, str) for tag in raw_tags):
            raise NewsAdapterError("announcement tags are invalid")
        identity = str(record.get("id") or url)
        return RawNewsItem(
            source_identity=identity,
            published_at_utc=published,
            title=title,
            canonical_url=url,
            summary=description,
            category_hint=type_key,
            tags=tuple(raw_tags),
        )


def _feed_text(entry: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = entry.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _feed_timestamp(entry: Mapping[str, Any]) -> datetime:
    value = entry.get("published_parsed") or entry.get("updated_parsed")
    if not isinstance(value, tuple) or len(value) < 6:
        raise NewsAdapterError("feed item timestamp is invalid")
    try:
        parts = [int(value[index]) for index in range(6)]
        return datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], tzinfo=UTC)
    except (TypeError, ValueError, OverflowError):
        raise NewsAdapterError("feed item timestamp is invalid") from None


def _feed_tags(entry: Mapping[str, Any]) -> tuple[str, ...]:
    raw_tags = entry.get("tags", [])
    if not isinstance(raw_tags, list):
        raise NewsAdapterError("feed item tags are invalid")
    tags: list[str] = []
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, Mapping):
            raise NewsAdapterError("feed item tags are invalid")
        term = raw_tag.get("term")
        if isinstance(term, str) and term.strip():
            tags.append(term)
    return tuple(tags)


class RssAtomAdapter:
    """使用 Universal Feed Parser 统一解析 RSS/Atom。"""

    def parse(self, payload: bytes) -> AdapterResult:
        lowered = payload.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise NewsAdapterError("XML declarations with DTD or entities are not allowed")
        parsed_feed = feedparser.parse(payload)
        if parsed_feed.bozo:
            raise NewsAdapterError("feed response is not valid RSS/Atom")
        if not str(parsed_feed.version).startswith(("rss", "atom")):
            raise NewsAdapterError("feed root must be RSS or Atom")

        parsed: list[RawNewsItem] = []
        rejected = 0
        for record in parsed_feed.entries:
            try:
                parsed.append(self._parse_entry(record))
            except NewsAdapterError:
                rejected += 1
        return AdapterResult(tuple(parsed), rejected)

    @staticmethod
    def _parse_entry(entry: Mapping[str, Any]) -> RawNewsItem:
        title = _required_text(_feed_text(entry, "title"), "feed item title")
        link = _required_text(_feed_text(entry, "link"), "feed item link")
        summary = _required_text(
            _feed_text(entry, "summary", "description") or title,
            "feed item summary",
        )
        identity = _feed_text(entry, "id", "guid") or link
        tags = _feed_tags(entry)
        return RawNewsItem(
            source_identity=identity,
            published_at_utc=_feed_timestamp(entry),
            title=title,
            canonical_url=link,
            summary=summary,
            category_hint=tags[0] if tags else None,
            tags=tags,
        )


def adapter_for(name: str) -> NewsAdapter:
    if name == "bybit_announcements_v5":
        return BybitAnnouncementAdapter()
    if name == "rss_atom":
        return RssAtomAdapter()
    raise NewsAdapterError("configured news adapter is unsupported")
