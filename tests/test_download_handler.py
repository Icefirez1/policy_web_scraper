import asyncio
import inspect
from unittest.mock import Mock, patch

import pytest
import scrapy
from scrapy.http import Response
from twisted.internet.defer import fail, succeed

from policy_scraper.crawler import DownloadHandler


def test_download_request_uses_threaded_download():
    assert inspect.iscoroutinefunction(DownloadHandler.download_request)
    assert list(inspect.signature(DownloadHandler.download_request).parameters) == [
        "self",
        "request",
    ]
    handler = DownloadHandler(Mock())
    request = scrapy.Request("https://ijc.org/report")
    response = Response(request.url, request=request)
    with patch("policy_scraper.crawler.deferToThread", return_value=succeed(response)) as worker:
        assert asyncio.run(handler.download_request(request)) is response
    worker.assert_called_once_with(handler._download, request)


def test_download_request_propagates_thread_failure():
    handler = DownloadHandler(Mock())
    request = scrapy.Request("https://ijc.org/report")
    with patch(
        "policy_scraper.crawler.deferToThread", return_value=fail(ValueError("fetch failed"))
    ):
        with pytest.raises(ValueError, match="fetch failed"):
            asyncio.run(handler.download_request(request))


def test_close_with_and_without_transport():
    handler = DownloadHandler(Mock())
    assert inspect.iscoroutinefunction(handler.close)
    asyncio.run(handler.close())
    handler.transport = Mock()
    asyncio.run(handler.close())
    handler.transport.close.assert_called_once_with()
