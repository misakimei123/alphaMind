"""R2-02 配置化新闻抓取、标准化、增量 cursor、去重与资产关联。"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, ParserRejectedMarkup

from alphamind.config import EffectiveConfig
from alphamind.decision import ContractValidationError, DecisionContractBinder
from alphamind.news.adapters import NewsAdapterError, RawNewsItem, adapter_for
from alphamind.news.http import (
    NewsHttpError,
    NewsHttpRequest,
    NewsTransport,
    default_news_transport,
)
from alphamind.news.state import NewsState, NewsStateStore, SourceCursor

JsonObject = dict[str, Any]
TRACKING_PARAMETERS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})
TAG_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


class NewsCollectionError(RuntimeError):
    """整个新闻周期无法安全建立。"""


@dataclass(frozen=True, slots=True)
class SourcePollResult:
    source_id: str
    status: str
    accepted_items: int
    duplicate_items: int
    rejected_items: int
    error_code: str | None = None

    def to_dict(self) -> JsonObject:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "accepted_items": self.accepted_items,
            "duplicate_items": self.duplicate_items,
            "rejected_items": self.rejected_items,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class NewsCollectionResult:
    fetched_at_utc: datetime
    items: tuple[JsonObject, ...]
    sources: tuple[SourcePollResult, ...]
    healthy_source_count: int
    risk_increase_news_available: bool

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": 1,
            "fetched_at_utc": _utc_text(self.fetched_at_utc),
            "healthy_source_count": self.healthy_source_count,
            "risk_increase_news_available": self.risk_increase_news_available,
            "sources": [source.to_dict() for source in self.sources],
            "items": list(self.items),
        }


def _normalized_text(value: str, maximum: int) -> str:
    try:
        document = BeautifulSoup(value, "html.parser")
        for active in document(["script", "style", "iframe", "object", "svg"]):
            active.decompose()
    except (ParserRejectedMarkup, ValueError):
        raise NewsCollectionError("external news text could not be normalized") from None
    normalized = " ".join(document.get_text(" ", strip=True).split())
    if not normalized:
        raise NewsCollectionError("external news text is empty after normalization")
    return normalized[:maximum]


def _canonical_https_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise NewsCollectionError("external news URL is invalid") from None
    if parsed.scheme.lower() != "https" or not hostname or parsed.username or parsed.password:
        raise NewsCollectionError("external news URL must be credential-free HTTPS")
    netloc = hostname.lower()
    if port is not None:
        netloc = f"{netloc}:{port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    path = parsed.path or "/"
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _news_id(source_id: str, raw: RawNewsItem, canonical_url: str) -> str:
    timestamp = raw.published_at_utc.strftime("%Y%m%dT%H%M%SZ")
    suffix = _sha256(f"{source_id}\n{raw.source_identity}\n{canonical_url}")[:12]
    return f"news-{timestamp}-{suffix}"


def _normalized_tags(values: tuple[str, ...]) -> list[str]:
    tags: list[str] = []
    for value in values:
        normalized = re.sub(r"[^a-z0-9_-]+", "_", value.strip().lower()).strip("_-")
        if len(normalized) < 2:
            continue
        normalized = normalized[:63]
        if TAG_PATTERN.fullmatch(normalized) and normalized not in tags:
            tags.append(normalized)
        if len(tags) == 20:
            break
    return tags


def _category(raw: RawNewsItem, allowed: tuple[str, ...]) -> str:
    text = " ".join((raw.category_hint or "", raw.title, *raw.tags)).lower()
    candidates: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("delisting", ("delist", "remove trading pair")),
        ("listing", ("new_crypto", "new listing", "lists ", "listing")),
        ("security_incident", ("hack", "exploit", "security incident", "breach")),
        ("regulation", ("sec ", "regulat", "charges ", "enforcement", "lawsuit")),
        ("protocol_update", ("upgrade", "hard fork", "protocol update")),
        ("tokenomics", ("airdrop", "token burn", "unlock")),
        ("macro", ("interest rate", "inflation", "cpi", "federal reserve")),
        ("market_structure", ("maintenance", "trading rules", "market structure")),
    )
    for category, keywords in candidates:
        if category in allowed and any(keyword in text for keyword in keywords):
            return category
    for fallback in ("other", "market_structure", "regulation"):
        if fallback in allowed:
            return fallback
    return allowed[0]


def _assets(
    raw: RawNewsItem,
    instrument_ids: tuple[str, ...],
    default: tuple[str, ...],
) -> list[str]:
    haystack = " ".join((raw.title, raw.summary, raw.canonical_url, *raw.tags))
    matched = [
        instrument_id
        for instrument_id in instrument_ids
        if re.search(rf"(?<![A-Z0-9]){re.escape(instrument_id)}(?![A-Z0-9])", haystack, re.I)
    ]
    return matched or list(default)


def _is_incremental(raw: RawNewsItem, cursor: SourceCursor) -> bool:
    identity = _sha256(raw.source_identity)
    if cursor.high_watermark_utc is None:
        return True
    if raw.published_at_utc > cursor.high_watermark_utc:
        return True
    return (
        raw.published_at_utc == cursor.high_watermark_utc
        and identity not in cursor.high_watermark_ids
    )


def _advanced_cursor(
    cursor: SourceCursor,
    records: tuple[RawNewsItem, ...],
    *,
    now_utc: datetime,
    etag: str | None,
    last_modified: str | None,
) -> SourceCursor:
    watermark = cursor.high_watermark_utc
    identities = set(cursor.high_watermark_ids)
    for record in records:
        if watermark is None or record.published_at_utc > watermark:
            watermark = record.published_at_utc
            identities = {_sha256(record.source_identity)}
        elif record.published_at_utc == watermark:
            identities.add(_sha256(record.source_identity))
    return SourceCursor(
        last_success_at_utc=now_utc,
        high_watermark_utc=watermark,
        high_watermark_ids=tuple(sorted(identities))[:100],
        etag=etag,
        last_modified=last_modified,
    )


class NewsCollector:
    """按 source priority 轮询；单源失败隔离，状态损坏则整个周期 fail closed。"""

    def __init__(
        self,
        effective: EffectiveConfig,
        *,
        state_store: NewsStateStore,
        transport: NewsTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.effective = effective
        self.state_store = state_store
        self.transport = transport or default_news_transport
        self.environ = dict(os.environ if environ is None else environ)
        self.binder = DecisionContractBinder(effective)
        self.registry = effective.instrument_registry

    def collect(
        self,
        *,
        now_utc: datetime | None = None,
        force: bool = False,
    ) -> NewsCollectionResult:
        now = now_utc or datetime.now(UTC)
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("now_utc must use UTC")
        state = self.state_store.load()
        source_results: list[SourcePollResult] = []
        accepted: list[JsonObject] = []
        sources = sorted(
            (source for source in self.effective.news_sources["sources"] if source["enabled"]),
            key=lambda source: int(source["priority"]),
        )
        for source in sources:
            source_id = str(source["source_id"])
            cursor = state.cursor(source_id)
            if (
                not force
                and cursor.last_success_at_utc is not None
                and now - cursor.last_success_at_utc
                < timedelta(seconds=int(source["poll_interval_seconds"]))
            ):
                source_results.append(SourcePollResult(source_id, "not_due", 0, 0, 0))
                continue
            result = self._poll_source(source, cursor, state, accepted, now)
            source_results.append(result)

        maximum_age = max(
            (int(source["maximum_item_age_hours"]) for source in sources),
            default=int(self.effective.runtime["scheduler"]["news_lookback_hours"]),
        )
        state.prune(now_utc=now, retention=timedelta(hours=maximum_age * 2), maximum_seen=10_000)
        self.state_store.save(state)
        accepted.sort(key=lambda item: (item["published_at_utc"], item["news_id"]), reverse=True)
        cycle_limit = int(self.effective.news_sources["normalization"]["cycle_text_max_characters"])
        bounded: list[JsonObject] = []
        used_characters = 0
        for item in accepted:
            item_characters = len(str(item["title"])) + len(str(item["summary"]))
            if used_characters + item_characters > cycle_limit:
                continue
            bounded.append(item)
            used_characters += item_characters
        healthy = sum(
            result.status in {"ok", "not_modified", "not_due"} for result in source_results
        )
        minimum = int(
            self.effective.news_sources["availability_policy"][
                "minimum_healthy_sources_for_risk_increase"
            ]
        )
        return NewsCollectionResult(
            fetched_at_utc=now,
            items=tuple(bounded),
            sources=tuple(source_results),
            healthy_source_count=healthy,
            risk_increase_news_available=healthy >= minimum,
        )

    def _poll_source(
        self,
        source: JsonObject,
        cursor: SourceCursor,
        state: NewsState,
        accepted: list[JsonObject],
        now: datetime,
    ) -> SourcePollResult:
        source_id = str(source["source_id"])
        user_agent = self.environ.get(str(source["user_agent_env"]), "").strip()
        if not user_agent:
            return SourcePollResult(source_id, "failed", 0, 0, 0, "user_agent_missing")
        try:
            response = self.transport(
                NewsHttpRequest(
                    endpoint=str(source["endpoint"]),
                    params=source["request_params"],
                    timeout_seconds=int(source["request_timeout_seconds"]),
                    maximum_response_bytes=int(source["maximum_response_bytes"]),
                    user_agent=user_agent,
                    etag=cursor.etag,
                    last_modified=cursor.last_modified,
                )
            )
            if response.status_code == 304:
                state.sources[source_id] = SourceCursor(
                    last_success_at_utc=now,
                    high_watermark_utc=cursor.high_watermark_utc,
                    high_watermark_ids=cursor.high_watermark_ids,
                    etag=cursor.etag,
                    last_modified=cursor.last_modified,
                )
                return SourcePollResult(source_id, "not_modified", 0, 0, 0)
            if response.status_code != 200:
                raise NewsHttpError("news source response status is invalid")
            expected_content_type = (
                "application/json"
                if source["adapter"] == "bybit_announcements_v5"
                else {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}
            )
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if isinstance(expected_content_type, str):
                content_type_valid = content_type == expected_content_type
            else:
                content_type_valid = content_type in expected_content_type
            if not content_type_valid:
                raise NewsHttpError("news source response Content-Type does not match adapter")
            if len(response.content) > int(source["maximum_response_bytes"]):
                raise NewsHttpError("news source response exceeds the configured byte limit")
            parsed = adapter_for(str(source["adapter"])).parse(response.content)
        except (NewsHttpError, NewsAdapterError, OSError):
            return SourcePollResult(source_id, "failed", 0, 0, 0, "source_unavailable")

        duplicates = 0
        rejected = parsed.rejected_records
        source_accepted = 0
        maximum_items = int(source["maximum_items_per_poll"])
        selected_records = tuple(
            sorted(
                parsed.items,
                key=lambda item: (item.published_at_utc, item.source_identity),
                reverse=True,
            )[:maximum_items]
        )
        future_limit = now + timedelta(
            seconds=int(self.effective.news_sources["normalization"]["future_clock_skew_seconds"])
        )
        for raw in selected_records:
            if not _is_incremental(raw, cursor):
                duplicates += 1
                continue
            if raw.published_at_utc > future_limit or now - raw.published_at_utc > timedelta(
                hours=int(source["maximum_item_age_hours"])
            ):
                rejected += 1
                continue
            try:
                item, fingerprints = self._normalize(source, raw, now)
            except (NewsCollectionError, ContractValidationError):
                rejected += 1
                continue
            if any(fingerprint in state.seen_fingerprints for fingerprint in fingerprints):
                duplicates += 1
                continue
            for fingerprint in fingerprints:
                state.seen_fingerprints[fingerprint] = now
            accepted.append(item)
            source_accepted += 1

        has_future_record = any(
            record.published_at_utc > future_limit for record in selected_records
        )
        state.sources[source_id] = _advanced_cursor(
            cursor,
            tuple(record for record in selected_records if record.published_at_utc <= future_limit),
            now_utc=now,
            etag=None if has_future_record else response.headers.get("ETag"),
            last_modified=(None if has_future_record else response.headers.get("Last-Modified")),
        )
        return SourcePollResult(source_id, "ok", source_accepted, duplicates, rejected)

    def _normalize(
        self,
        source: JsonObject,
        raw: RawNewsItem,
        now: datetime,
    ) -> tuple[JsonObject, tuple[str, str, str]]:
        normalization = self.effective.news_sources["normalization"]
        title = _normalized_text(raw.title, int(normalization["title_max_characters"]))
        summary = _normalized_text(raw.summary, int(normalization["summary_max_characters"]))
        maximum_combined = int(normalization["item_text_max_characters"])
        if len(title) + len(summary) > maximum_combined:
            summary = summary[: max(1, maximum_combined - len(title))]
        canonical_url = _canonical_https_url(raw.canonical_url)
        title_hash = _sha256(title)
        content_hash = _sha256(summary)
        instrument_ids = tuple(
            item.instrument_id for item in self.registry.instruments if item.enabled
        )
        allowed_categories = tuple(str(item) for item in source["categories"])
        item: JsonObject = {
            "schema_version": 1,
            "news_id": _news_id(str(source["source_id"]), raw, canonical_url),
            "source": {
                "source_id": source["source_id"],
                "display_name": source["display_name"],
                "source_type": source["source_type"],
                "trust_tier": source["trust_tier"],
            },
            "published_at_utc": _utc_text(raw.published_at_utc),
            "fetched_at_utc": _utc_text(now),
            "title": title,
            "canonical_url": canonical_url,
            "summary": summary,
            "assets": _assets(raw, instrument_ids, tuple(source["default_assets"])),
            "category": _category(raw, allowed_categories),
            "language": source["language"],
            "title_sha256": title_hash,
            "content_sha256": content_hash,
            "untrusted_external_content": True,
        }
        tags = _normalized_tags(raw.tags)
        if tags:
            item["tags"] = tags
        self.binder.bind_news_item(item, as_of_utc=now)
        return item, (
            f"u:{_sha256(canonical_url)}",
            f"t:{title_hash}",
            f"c:{content_hash}",
        )
