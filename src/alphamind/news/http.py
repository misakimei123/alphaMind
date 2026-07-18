"""带 Content-Type、响应大小、重定向边界的只读新闻 HTTP 传输。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class NewsHttpError(RuntimeError):
    """HTTP 响应不满足只读新闻边界。"""


@dataclass(frozen=True, slots=True)
class NewsHttpRequest:
    endpoint: str
    params: Mapping[str, str | int]
    timeout_seconds: int
    maximum_response_bytes: int
    user_agent: str
    etag: str | None = None
    last_modified: str | None = None


NewsTransport = Callable[[NewsHttpRequest], httpx.Response]


def _same_origin(expected: str, actual: str) -> bool:
    expected_url = urlsplit(expected)
    actual_url = urlsplit(actual)
    return (
        expected_url.scheme == actual_url.scheme == "https"
        and expected_url.hostname == actual_url.hostname
        and expected_url.port == actual_url.port
    )


def default_news_transport(
    request: NewsHttpRequest,
    *,
    http_transport: httpx.BaseTransport | None = None,
) -> httpx.Response:
    headers = {
        "Accept": (
            "application/json, application/rss+xml, application/atom+xml, application/xml, text/xml"
        ),
        "Accept-Encoding": "identity",
        "User-Agent": request.user_agent,
    }
    if request.etag:
        headers["If-None-Match"] = request.etag
    if request.last_modified:
        headers["If-Modified-Since"] = request.last_modified

    def reject_cross_origin_redirect(response: httpx.Response) -> None:
        if not response.is_redirect:
            return
        location = response.headers.get("Location")
        if not location:
            raise NewsHttpError("news source redirect is missing Location")
        target = response.url.join(location)
        if not _same_origin(request.endpoint, str(target)):
            raise NewsHttpError("news source redirected to a different origin")

    try:
        with (
            httpx.Client(
                timeout=request.timeout_seconds,
                follow_redirects=True,
                event_hooks={"response": [reject_cross_origin_redirect]},
                transport=http_transport,
            ) as client,
            client.stream(
                "GET",
                request.endpoint,
                params=request.params,
                headers=headers,
            ) as response,
        ):
            if response.status_code == 304:
                response_headers = dict(response.headers)
                if request.etag and "etag" not in response.headers:
                    response_headers["ETag"] = request.etag
                if request.last_modified and "last-modified" not in response.headers:
                    response_headers["Last-Modified"] = request.last_modified
                return httpx.Response(
                    304,
                    headers=response_headers,
                    request=response.request,
                )
            if response.status_code != 200:
                raise NewsHttpError("news source request returned a non-success status")
            if not _same_origin(request.endpoint, str(response.url)):
                raise NewsHttpError("news source redirected to a different origin")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            allowed = {
                "application/json",
                "application/rss+xml",
                "application/atom+xml",
                "application/xml",
                "text/xml",
            }
            if content_type not in allowed:
                raise NewsHttpError("news source response Content-Type is not allowed")
            declared_length = response.headers.get("Content-Length")
            try:
                if (
                    declared_length is not None
                    and int(declared_length) > request.maximum_response_bytes
                ):
                    raise NewsHttpError("news source response exceeds the configured byte limit")
            except ValueError:
                raise NewsHttpError("news source Content-Length is invalid") from None
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > request.maximum_response_bytes:
                    raise NewsHttpError("news source response exceeds the configured byte limit")
            return httpx.Response(
                200,
                content=bytes(body),
                headers=response.headers,
                request=response.request,
            )
    except NewsHttpError:
        raise
    except httpx.HTTPError:
        raise NewsHttpError("news source request failed") from None
