from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from alphamind.config import load_effective_config
from alphamind.news import (
    BybitAnnouncementAdapter,
    NewsCollector,
    NewsHttpError,
    NewsHttpRequest,
    NewsStateError,
    NewsStateStore,
    RssAtomAdapter,
    default_news_transport,
)

PROJECT_ROOT = Path(__file__).parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "news"
NOW = datetime(2026, 7, 18, 13, 0, tzinfo=UTC)


def _payload(name: str) -> bytes:
    return (FIXTURE_ROOT / name).read_bytes()


def _response(
    status_code: int,
    body: bytes,
    content_type: str | None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> httpx.Response:
    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = content_type
    if etag:
        headers["ETag"] = etag
    if last_modified:
        headers["Last-Modified"] = last_modified
    return httpx.Response(status_code, content=body, headers=headers)


class FixtureTransport:
    def __init__(self) -> None:
        self.requests: list[NewsHttpRequest] = []
        self.not_modified = False

    def __call__(self, request: NewsHttpRequest) -> httpx.Response:
        self.requests.append(request)
        if self.not_modified and request.etag:
            return _response(304, b"", None, request.etag, request.last_modified)
        if "announcements" in request.endpoint:
            return _response(
                200,
                _payload("bybit-announcements.json"),
                "application/json",
                '"bybit-v1"',
                "Sat, 18 Jul 2026 12:45:00 GMT",
            )
        if "sec.gov" in request.endpoint:
            return _response(
                200,
                _payload("press-releases.rss.xml"),
                "application/rss+xml",
                '"sec-v1"',
                None,
            )
        return _response(
            200,
            _payload("coindesk.atom.xml"),
            "application/atom+xml",
            '"coindesk-v1"',
            None,
        )


def test_default_news_transport_uses_httpx_and_enforces_redirect_origin() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/feed":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(
            200,
            content=_payload("press-releases.rss.xml"),
            headers={
                "Content-Type": "application/rss+xml; charset=utf-8",
                "ETag": '"next"',
            },
        )

    response = default_news_transport(
        NewsHttpRequest(
            endpoint="https://news.example/feed",
            params={"limit": 2},
            timeout_seconds=5,
            maximum_response_bytes=100_000,
            user_agent="alphaMind-test",
            etag='"before"',
        ),
        http_transport=httpx.MockTransport(handler),
    )

    assert response.status_code == 200
    assert response.headers["etag"] == '"next"'
    assert response.content == _payload("press-releases.rss.xml")
    assert [request.url.path for request in calls] == ["/feed", "/final"]
    assert calls[0].url.params["limit"] == "2"
    assert calls[0].headers["if-none-match"] == '"before"'

    def cross_origin(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://attacker.example/feed"})

    with pytest.raises(NewsHttpError, match="different origin"):
        default_news_transport(
            NewsHttpRequest(
                endpoint="https://news.example/feed",
                params={},
                timeout_seconds=5,
                maximum_response_bytes=100_000,
                user_agent="alphaMind-test",
            ),
            http_transport=httpx.MockTransport(cross_origin),
        )


def _collector(tmp_path: Path, transport: FixtureTransport) -> NewsCollector:
    effective = load_effective_config(PROJECT_ROOT, environ={})
    return NewsCollector(
        effective,
        state_store=NewsStateStore(tmp_path / "news-cursors.json"),
        transport=transport,
        environ={"ALPHAMIND_NEWS_USER_AGENT": "alphaMind-test contact@example.invalid"},
    )


def test_adapters_parse_bybit_rss_and_atom_without_network() -> None:
    bybit = BybitAnnouncementAdapter().parse(_payload("bybit-announcements.json"))
    rss = RssAtomAdapter().parse(_payload("press-releases.rss.xml"))
    atom = RssAtomAdapter().parse(_payload("coindesk.atom.xml"))

    assert len(bybit.items) == 2
    assert bybit.rejected_records == 1
    assert bybit.items[0].category_hint == "new_crypto"
    assert len(rss.items) == 1 and rss.rejected_records == 1
    assert rss.items[0].published_at_utc == datetime(2026, 7, 18, 12, 30, tzinfo=UTC)
    assert atom.items[0].source_identity == "tag:coindesk.com,2026:hype-update"


def test_collector_normalizes_binds_assets_and_persists_incremental_state(tmp_path: Path) -> None:
    transport = FixtureTransport()
    collector = _collector(tmp_path, transport)

    result = collector.collect(now_utc=NOW)

    assert result.healthy_source_count == 3
    assert result.risk_increase_news_available
    assert len(result.items) == 4
    by_title = {item["title"]: item for item in result.items}
    sol = by_title["New Listing: SOL/USDT Trading Opens"]
    assert sol["assets"] == ["SOL"]
    assert sol["category"] == "listing"
    assert sol["canonical_url"] == "https://announcements.bybit.com/en-US/article/sol-listing"
    assert "ignore me" not in sol["summary"]
    assert sol["untrusted_external_content"] is True
    assert by_title["SEC announces digital asset market roundtable"]["assets"] == ["ETH"]
    assert by_title["HYPE protocol upgrade reaches mainnet"]["category"] == "protocol_update"

    state = json.loads((tmp_path / "news-cursors.json").read_text(encoding="utf-8"))
    assert state["schema_version"] == 1
    assert state["sources"]["bybit_announcements"]["etag"] == '"bybit-v1"'
    assert all(
        len(identity) == 64
        for cursor in state["sources"].values()
        for identity in cursor["high_watermark_ids"]
    )
    assert len(state["seen_fingerprints"]) == 12

    transport.not_modified = True
    second = collector.collect(now_utc=datetime(2026, 7, 18, 13, 31, tzinfo=UTC))
    assert second.items == ()
    assert all(source.status == "not_modified" for source in second.sources)
    assert all(request.etag for request in transport.requests[-3:])


def test_incremental_cursor_deduplicates_identical_payload_without_http_validators(
    tmp_path: Path,
) -> None:
    transport = FixtureTransport()
    collector = _collector(tmp_path, transport)
    collector.collect(now_utc=NOW)

    second = collector.collect(
        now_utc=datetime(2026, 7, 18, 13, 1, tzinfo=UTC),
        force=True,
    )

    assert second.items == ()
    assert sum(source.duplicate_items for source in second.sources) == 4


def test_not_due_sources_remain_healthy_and_do_not_issue_requests(tmp_path: Path) -> None:
    transport = FixtureTransport()
    collector = _collector(tmp_path, transport)
    collector.collect(now_utc=NOW)

    second = collector.collect(now_utc=datetime(2026, 7, 18, 13, 5, tzinfo=UTC))

    assert second.items == ()
    assert second.healthy_source_count == 3
    assert second.risk_increase_news_available
    assert all(source.status == "not_due" for source in second.sources)
    assert len(transport.requests) == 3


def test_single_source_failure_is_isolated_and_disables_risk_increase_only_when_all_fail(
    tmp_path: Path,
) -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})

    def partially_failed(request: NewsHttpRequest) -> httpx.Response:
        if "announcements" in request.endpoint:
            raise OSError("fixture failure")
        if "sec.gov" in request.endpoint:
            return _response(200, _payload("press-releases.rss.xml"), "application/rss+xml")
        return _response(200, _payload("coindesk.atom.xml"), "application/atom+xml")

    collector = NewsCollector(
        effective,
        state_store=NewsStateStore(tmp_path / "state.json"),
        transport=partially_failed,
        environ={"ALPHAMIND_NEWS_USER_AGENT": "test-agent"},
    )
    result = collector.collect(now_utc=NOW)
    assert result.healthy_source_count == 2
    assert result.risk_increase_news_available
    assert len(result.items) == 2
    assert result.sources[0].error_code == "source_unavailable"


def test_missing_user_agent_marks_sources_failed_without_leaking_environment(
    tmp_path: Path,
) -> None:
    result = NewsCollector(
        load_effective_config(PROJECT_ROOT, environ={}),
        state_store=NewsStateStore(tmp_path / "state.json"),
        transport=FixtureTransport(),
        environ={},
    ).collect(now_utc=NOW)

    assert result.items == ()
    assert result.healthy_source_count == 0
    assert not result.risk_increase_news_available
    assert {source.error_code for source in result.sources} == {"user_agent_missing"}


def test_wrong_content_type_future_items_and_unsafe_xml_fail_closed(tmp_path: Path) -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})

    def wrong_content_type(request: NewsHttpRequest) -> httpx.Response:
        return _response(200, b"{}", "text/html")

    result = NewsCollector(
        effective,
        state_store=NewsStateStore(tmp_path / "state.json"),
        transport=wrong_content_type,
        environ={"ALPHAMIND_NEWS_USER_AGENT": "test-agent"},
    ).collect(now_utc=NOW)
    assert result.healthy_source_count == 0
    assert all(source.error_code == "source_unavailable" for source in result.sources)

    with pytest.raises(ValueError, match="DTD or entities"):
        RssAtomAdapter().parse(b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss>&x;</rss>')


def test_corrupt_cursor_state_and_non_utc_clock_are_rejected(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"schema_version":999}', encoding="utf-8")
    collector = NewsCollector(
        load_effective_config(PROJECT_ROOT, environ={}),
        state_store=NewsStateStore(state_path),
        transport=FixtureTransport(),
        environ={"ALPHAMIND_NEWS_USER_AGENT": "test-agent"},
    )
    with pytest.raises(NewsStateError, match="fields are invalid"):
        collector.collect(now_utc=NOW)
    with pytest.raises(ValueError, match="must use UTC"):
        collector.collect(now_utc=datetime(2026, 7, 18, 13, 0))
