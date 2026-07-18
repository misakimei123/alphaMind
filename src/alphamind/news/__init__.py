"""配置化新闻采集公共接口。"""

from alphamind.news.adapters import (
    AdapterResult,
    BybitAnnouncementAdapter,
    NewsAdapterError,
    RawNewsItem,
    RssAtomAdapter,
    adapter_for,
)
from alphamind.news.collector import (
    NewsCollectionError,
    NewsCollectionResult,
    NewsCollector,
    SourcePollResult,
)
from alphamind.news.http import (
    NewsHttpError,
    NewsHttpRequest,
    NewsTransport,
    default_news_transport,
)
from alphamind.news.state import NewsStateError, NewsStateStore, SourceCursor

__all__ = [
    "AdapterResult",
    "BybitAnnouncementAdapter",
    "NewsAdapterError",
    "NewsCollectionError",
    "NewsCollectionResult",
    "NewsCollector",
    "NewsHttpError",
    "NewsHttpRequest",
    "NewsStateError",
    "NewsStateStore",
    "NewsTransport",
    "RawNewsItem",
    "RssAtomAdapter",
    "SourceCursor",
    "SourcePollResult",
    "adapter_for",
    "default_news_transport",
]
