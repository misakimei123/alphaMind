from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import jsonschema
import pytest
import yaml

from alphamind.config import MarketKind, load_effective_config, load_instrument_registry
from alphamind.market import (
    BybitInstrumentClient,
    BybitKlineClient,
    CapabilityError,
    build_market_capability_snapshot,
    load_market_capability_snapshot,
    parse_market_capability_snapshot,
)
from alphamind.market.bybit import BybitMarketDataError
from scripts.refresh_market_capabilities import main as refresh_main

PROJECT_ROOT = Path(__file__).parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "bybit"
REGISTRY = load_instrument_registry(
    PROJECT_ROOT / "configs" / "alphamind" / "instruments.example.yaml"
)
SNAPSHOT_PATH = PROJECT_ROOT / "configs" / "alphamind" / "market-capabilities.snapshot.json"
FETCHED_AT = datetime(2026, 7, 18, 10, tzinfo=UTC)


def _fixture_client() -> BybitInstrumentClient:
    queues = {
        "spot": [FIXTURE_ROOT / "instruments-spot-page1.json"],
        "linear": [
            FIXTURE_ROOT / "instruments-linear-page1.json",
            FIXTURE_ROOT / "instruments-linear-page2.json",
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        category = request.url.params["category"]
        return httpx.Response(
            200,
            content=queues[category].pop(0).read_bytes(),
            headers={"Content-Type": "application/json"},
        )

    return BybitInstrumentClient(http_transport=httpx.MockTransport(handler))


def test_checked_in_snapshot_is_valid_and_covers_all_registry_markets() -> None:
    document = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    schema = yaml.safe_load(
        (PROJECT_ROOT / "data/schemas/market-capability-snapshot.schema.yaml").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    ).validate(document)
    snapshot = load_market_capability_snapshot(SNAPSHOT_PATH, registry=REGISTRY)

    assert snapshot.available_pairs(MarketKind.SPOT) == REGISTRY.enabled_pairs(MarketKind.SPOT)
    assert snapshot.available_pairs(MarketKind.FUTURES) == REGISTRY.enabled_pairs(
        MarketKind.FUTURES
    )
    hype = snapshot.capability_for_pair("HYPE/USDT:USDT", MarketKind.FUTURES)
    assert hype is not None
    assert hype.exchange_max_leverage == Decimal("75")
    assert hype.configured_max_leverage == Decimal("1")
    assert hype.effective_max_leverage == Decimal("1")
    assert hype.funding_interval_minutes == 480
    assert document["summary"]["unavailable_enabled_markets"] == []


def test_client_handles_spot_no_pagination_and_linear_cursor_pages() -> None:
    fetched = _fixture_client().fetch(fetched_at_utc=FETCHED_AT)
    snapshot = build_market_capability_snapshot(
        REGISTRY,
        fetched,
        global_max_leverage=Decimal("3"),
    )

    assert len(fetched.page_sha256["spot"]) == 1
    assert len(fetched.page_sha256["linear"]) == 2
    assert snapshot.available_pairs(MarketKind.SPOT) == ("BTC/USDT",)
    assert snapshot.available_pairs(MarketKind.FUTURES) == (
        "BTC/USDT:USDT",
        "SOL/USDT:USDT",
    )
    missing_eth = snapshot.capability_for_pair("ETH/USDT", MarketKind.SPOT)
    assert missing_eth is not None
    assert missing_eth.unavailable_reason == "NOT_RETURNED_AS_TRADING"
    parsed = parse_market_capability_snapshot(snapshot.to_dict(), registry=REGISTRY)
    assert parsed.snapshot_id == snapshot.snapshot_id


def test_default_market_transport_uses_httpx_mock_transport() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        category = request.url.params["category"]
        if category == "spot":
            fixture = "instruments-spot-page1.json"
        elif request.url.params.get("cursor"):
            fixture = "instruments-linear-page2.json"
        else:
            fixture = "instruments-linear-page1.json"
        return httpx.Response(
            200,
            content=(FIXTURE_ROOT / fixture).read_bytes(),
            headers={"Content-Type": "application/json"},
        )

    fetched = BybitInstrumentClient(http_transport=httpx.MockTransport(handler)).fetch(
        fetched_at_utc=FETCHED_AT
    )

    assert len(fetched.records["spot"]) == 1
    assert len(fetched.records["linear"]) == 2
    assert [request.url.params["category"] for request in calls] == [
        "spot",
        "linear",
        "linear",
    ]


def test_kline_client_returns_only_completed_candles_in_chronological_order() -> None:
    as_of = datetime(2026, 7, 18, 12, 5, tzinfo=UTC)
    starts = [
        datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        datetime(2026, 7, 18, 11, 30, tzinfo=UTC),
        datetime(2026, 7, 18, 11, 0, tzinfo=UTC),
    ]
    payload = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "spot",
            "symbol": "BTCUSDT",
            "list": [
                [str(int(start.timestamp() * 1000)), "100", "102", "99", "101", "10", "0"]
                for start in starts
            ],
        },
        "time": 1784376300000,
    }
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    result = BybitKlineClient(http_transport=httpx.MockTransport(handler)).fetch(
        pair="BTC/USDT",
        category="spot",
        timeframe="30m",
        as_of_utc=as_of,
        limit=60,
    )

    assert [candle.started_at_utc for candle in result.candles] == list(reversed(starts[1:]))
    assert all(candle.completed_at_utc <= as_of for candle in result.candles)
    assert result.category == "spot"
    assert len(result.response_sha256) == 64
    assert dict(calls[0].url.params) == {
        "category": "spot",
        "symbol": "BTCUSDT",
        "interval": "30",
        "end": str(int(as_of.timestamp() * 1000)),
        "limit": "60",
    }


def test_kline_client_rejects_identity_duplicates_and_invalid_ohlcv() -> None:
    as_of = datetime(2026, 7, 18, 12, 5, tzinfo=UTC)
    start = str(int(datetime(2026, 7, 18, 11, 30, tzinfo=UTC).timestamp() * 1000))

    def client_with_rows(rows: list[list[str]], *, symbol: str = "BTCUSDT") -> BybitKlineClient:
        payload = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"category": "spot", "symbol": symbol, "list": rows},
            "time": 1784376300000,
        }
        return BybitKlineClient(
            http_transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    content=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
            )
        )

    valid = [start, "100", "102", "99", "101", "10", "0"]
    with pytest.raises(BybitMarketDataError, match="identity"):
        client_with_rows([valid], symbol="ETHUSDT").fetch(
            pair="BTC/USDT", category="spot", timeframe="30m", as_of_utc=as_of
        )
    with pytest.raises(BybitMarketDataError, match="duplicated"):
        client_with_rows([valid, valid]).fetch(
            pair="BTC/USDT", category="spot", timeframe="30m", as_of_utc=as_of
        )
    invalid = [start, "100", "98", "99", "101", "10", "0"]
    with pytest.raises(BybitMarketDataError, match="OHLCV"):
        client_with_rows([invalid]).fetch(
            pair="BTC/USDT", category="spot", timeframe="30m", as_of_utc=as_of
        )


def test_malformed_single_market_disables_only_that_market() -> None:
    fetched = _fixture_client().fetch(fetched_at_utc=FETCHED_AT)
    records = {category: list(rows) for category, rows in fetched.records.items()}
    sol = deepcopy(records["linear"][1])
    sol["lotSizeFilter"]["qtyStep"] = "invalid"
    records["linear"][1] = sol
    malformed = replace(
        fetched,
        records={category: tuple(rows) for category, rows in records.items()},
    )

    snapshot = build_market_capability_snapshot(
        REGISTRY,
        malformed,
        global_max_leverage=Decimal("3"),
    )

    btc = snapshot.capability_for_pair("BTC/USDT:USDT", MarketKind.FUTURES)
    sol_capability = snapshot.capability_for_pair("SOL/USDT:USDT", MarketKind.FUTURES)
    assert btc is not None and btc.available
    assert sol_capability is not None and not sol_capability.available
    assert sol_capability.unavailable_reason == "INVALID_MARKET_RECORD"


def test_request_level_failure_publishes_no_usable_result() -> None:
    def failed_transport(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"retCode":10001,"retMsg":"bad","result":{},"time":1784370000000}',
            headers={"Content-Type": "application/json"},
        )

    with pytest.raises(BybitMarketDataError, match="non-success"):
        BybitInstrumentClient(http_transport=httpx.MockTransport(failed_transport)).fetch(
            fetched_at_utc=FETCHED_AT
        )


def test_client_rejects_unapproved_endpoint_and_repeated_linear_cursor() -> None:
    with pytest.raises(ValueError, match="official HTTPS endpoint"):
        BybitInstrumentClient(base_url="https://api.bybit.com.attacker.example")

    spot_payload = (FIXTURE_ROOT / "instruments-spot-page1.json").read_bytes()
    repeated_linear_payload = (FIXTURE_ROOT / "instruments-linear-page1.json").read_bytes()

    def repeated_cursor_transport(request: httpx.Request) -> httpx.Response:
        payload = (
            repeated_linear_payload if request.url.params["category"] == "linear" else spot_payload
        )
        return httpx.Response(
            200,
            content=payload,
            headers={"Content-Type": "application/json"},
        )

    with pytest.raises(BybitMarketDataError, match="cursor repeated"):
        BybitInstrumentClient(http_transport=httpx.MockTransport(repeated_cursor_transport)).fetch(
            fetched_at_utc=FETCHED_AT
        )


def test_snapshot_freshness_registry_hash_and_effective_leverage_fail_closed() -> None:
    snapshot = load_market_capability_snapshot(SNAPSHOT_PATH, registry=REGISTRY)
    with pytest.raises(CapabilityError, match="stale"):
        load_market_capability_snapshot(
            SNAPSHOT_PATH,
            registry=REGISTRY,
            now_utc=snapshot.fetched_at_utc + timedelta(hours=25),
            maximum_age=timedelta(hours=24),
        )

    document = snapshot.to_dict()
    document["instruments"][3]["futures"]["effective_max_leverage"] = "2"
    with pytest.raises(CapabilityError, match="effective futures leverage"):
        parse_market_capability_snapshot(document, registry=REGISTRY)

    mismatched = replace(REGISTRY, source_sha256="0" * 64)
    with pytest.raises(CapabilityError, match="does not match"):
        load_market_capability_snapshot(SNAPSHOT_PATH, registry=mismatched)


def test_snapshot_rejects_inconsistent_response_evidence_and_registry_leverage() -> None:
    snapshot = load_market_capability_snapshot(SNAPSHOT_PATH, registry=REGISTRY)
    document = snapshot.to_dict()
    document["source"]["response_sha256"] = "0" * 64
    with pytest.raises(CapabilityError, match="response evidence hash"):
        parse_market_capability_snapshot(document, registry=REGISTRY)

    document = snapshot.to_dict()
    document["instruments"][0]["futures"]["configured_max_leverage"] = "1"
    document["instruments"][0]["futures"]["effective_max_leverage"] = "1"
    with pytest.raises(CapabilityError, match="leverage does not match"):
        parse_market_capability_snapshot(document, registry=REGISTRY)


def test_effective_config_binds_capability_snapshot_and_offline_check_command() -> None:
    effective = load_effective_config(PROJECT_ROOT, environ={})

    assert effective.market_capability_snapshot.snapshot_id.startswith("capability-")
    assert effective.source_sha256["market_capabilities"]
    assert (
        refresh_main(
            [
                "--project-root",
                str(PROJECT_ROOT),
                "--check",
                "--maximum-age-hours",
                "876000",
            ]
        )
        == 0
    )
