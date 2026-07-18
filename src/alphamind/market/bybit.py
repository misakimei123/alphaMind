"""无认证、只读的 Bybit V5 instruments-info 客户端。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

JsonObject = dict[str, Any]
OFFICIAL_BASE_URLS = frozenset(
    {
        "https://api.bybit.com",
        "https://api-testnet.bybit.com",
        "https://api-demo.bybit.com",
    }
)


class BybitMarketDataError(RuntimeError):
    """公共市场响应无法安全使用。"""


@dataclass(frozen=True, slots=True)
class BybitFetchResult:
    fetched_at_utc: datetime
    base_url: str
    endpoint: str
    records: Mapping[str, tuple[JsonObject, ...]]
    page_sha256: Mapping[str, tuple[str, ...]]
    server_time_milliseconds: tuple[int, ...]


def _default_transport(
    url: str,
    timeout_seconds: int,
    maximum_response_bytes: int,
    *,
    http_transport: httpx.BaseTransport | None = None,
) -> bytes:
    try:
        with (
            httpx.Client(
                timeout=timeout_seconds,
                follow_redirects=False,
                transport=http_transport,
            ) as client,
            client.stream(
                "GET",
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "alphaMind/0.1 market-capability",
                },
            ) as response,
        ):
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type != "application/json":
                raise BybitMarketDataError("Bybit response Content-Type must be application/json")
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None:
                try:
                    if int(declared_length) > maximum_response_bytes:
                        raise BybitMarketDataError(
                            "Bybit response exceeds the configured byte limit"
                        )
                except ValueError:
                    raise BybitMarketDataError("Bybit Content-Length is invalid") from None
            payload = bytearray()
            for chunk in response.iter_bytes():
                payload.extend(chunk)
                if len(payload) > maximum_response_bytes:
                    raise BybitMarketDataError("Bybit response exceeds the configured byte limit")
    except BybitMarketDataError:
        raise
    except httpx.HTTPError:
        raise BybitMarketDataError("Bybit instruments-info request failed") from None
    return bytes(payload)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


class BybitInstrumentClient:
    ENDPOINT = "/v5/market/instruments-info"

    def __init__(
        self,
        *,
        base_url: str = "https://api.bybit.com",
        timeout_seconds: int = 20,
        maximum_response_bytes: int = 8_000_000,
        maximum_pages: int = 10,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized = base_url.rstrip("/")
        if normalized not in OFFICIAL_BASE_URLS:
            raise ValueError("Bybit base_url must be an approved official HTTPS endpoint")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 1 and 60")
        if not 1024 <= maximum_response_bytes <= 20_000_000:
            raise ValueError("maximum_response_bytes is outside the safe range")
        if not 1 <= maximum_pages <= 20:
            raise ValueError("maximum_pages must be between 1 and 20")
        self.base_url = normalized
        self.timeout_seconds = timeout_seconds
        self.maximum_response_bytes = maximum_response_bytes
        self.maximum_pages = maximum_pages
        self._http_transport = http_transport

    def _request_page(self, category: str, cursor: str | None) -> JsonObject:
        params: dict[str, str | int] = {"category": category}
        if category == "linear":
            params["limit"] = 1000
            if cursor:
                params["cursor"] = cursor
        url = f"{self.base_url}{self.ENDPOINT}?{urlencode(params)}"
        try:
            payload = _default_transport(
                url,
                self.timeout_seconds,
                self.maximum_response_bytes,
                http_transport=self._http_transport,
            )
            decoded = json.loads(payload.decode("utf-8"))
        except BybitMarketDataError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            raise BybitMarketDataError("Bybit instruments-info request failed") from None
        if not isinstance(decoded, dict):
            raise BybitMarketDataError("Bybit instruments-info response must be an object")
        return decoded

    @staticmethod
    def _validate_page(page: JsonObject, category: str) -> tuple[list[JsonObject], str, int]:
        if page.get("retCode") != 0 or page.get("retMsg") != "OK":
            raise BybitMarketDataError("Bybit instruments-info returned a non-success code")
        result = page.get("result")
        if not isinstance(result, dict) or result.get("category") != category:
            raise BybitMarketDataError("Bybit instruments-info category does not match request")
        raw_records = result.get("list")
        if not isinstance(raw_records, list) or any(
            not isinstance(row, dict) for row in raw_records
        ):
            raise BybitMarketDataError("Bybit instruments-info list is invalid")
        cursor = result.get("nextPageCursor", "")
        if not isinstance(cursor, str):
            raise BybitMarketDataError("Bybit instruments-info cursor is invalid")
        server_time = page.get("time")
        if type(server_time) is not int or server_time <= 0:
            raise BybitMarketDataError("Bybit instruments-info server time is invalid")
        return raw_records, cursor, server_time

    def fetch(self, *, fetched_at_utc: datetime | None = None) -> BybitFetchResult:
        fetched_at = fetched_at_utc or datetime.now(UTC)
        if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
            raise ValueError("fetched_at_utc must be timezone-aware")
        fetched_at = fetched_at.astimezone(UTC)

        records: dict[str, tuple[JsonObject, ...]] = {}
        page_hashes: dict[str, tuple[str, ...]] = {}
        server_times: list[int] = []
        for category in ("spot", "linear"):
            category_records: list[JsonObject] = []
            category_hashes: list[str] = []
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for _ in range(self.maximum_pages):
                page = self._request_page(category, cursor)
                rows, next_cursor, server_time = self._validate_page(page, category)
                category_records.extend(rows)
                category_hashes.append(_canonical_sha256(page))
                server_times.append(server_time)
                if category == "spot":
                    if next_cursor:
                        raise BybitMarketDataError("spot instruments-info must not paginate")
                    break
                if not next_cursor:
                    break
                if next_cursor in seen_cursors:
                    raise BybitMarketDataError("linear instruments-info cursor repeated")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            else:
                raise BybitMarketDataError("linear instruments-info exceeded maximum pages")
            records[category] = tuple(category_records)
            page_hashes[category] = tuple(category_hashes)

        return BybitFetchResult(
            fetched_at_utc=fetched_at,
            base_url=self.base_url,
            endpoint=self.ENDPOINT,
            records=records,
            page_sha256=page_hashes,
            server_time_milliseconds=tuple(server_times),
        )
