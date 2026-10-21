import httpx
import pytest

from policy_scraper.config import Config
from policy_scraper.transport import AccessError, BudgetExceeded, TemporaryFailure, Transport


def transport(tmp_path, handler, **options):
    cfg = Config(contact="test@example.org", delay_seconds=0, retries=0, **options)
    value = Transport(cfg, tmp_path)
    value.client.close()
    value.client = httpx.Client(transport=httpx.MockTransport(handler))
    return value


def test_robots_and_redirect_destination_checked(tmp_path):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private")
        return httpx.Response(302, headers={"Location": "/private"})

    value = transport(tmp_path, handle)
    with pytest.raises(AccessError, match="Disallowed"):
        value.fetch("https://ijc.org/start")
    assert "https://ijc.org/private" not in calls


@pytest.mark.parametrize("status", [404, 410])
def test_missing_robots_allowed(tmp_path, status):
    value = transport(
        tmp_path,
        lambda req: httpx.Response(status if req.url.path == "/robots.txt" else 200, text="body"),
    )
    assert value.fetch("https://ijc.org/a").status == 200


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_robots_failure_pauses_origin(tmp_path, status):
    value = transport(tmp_path, lambda req: httpx.Response(status))
    with pytest.raises(AccessError):
        value.fetch("https://ijc.org/a")
    assert "https://ijc.org" in value.paused


def test_external_redirect_never_requested(tmp_path):
    calls = []

    def handle(req):
        calls.append(str(req.url))
        return (
            httpx.Response(404)
            if req.url.path == "/robots.txt"
            else httpx.Response(302, headers={"Location": "https://evil.test/file.pdf"})
        )

    value = transport(tmp_path, handle)
    with pytest.raises(AccessError, match="allowlist"):
        value.fetch("https://ijc.org/a")
    assert not any("evil.test" in url for url in calls)


def test_size_and_budget(tmp_path):
    value = transport(
        tmp_path,
        lambda req: (
            httpx.Response(404)
            if req.url.path == "/robots.txt"
            else httpx.Response(200, content=b"12345")
        ),
        max_bytes=4,
    )
    with pytest.raises(ValueError, match="size limit"):
        value.fetch("https://ijc.org/a")
    assert not list(tmp_path.glob("download-*"))
    value.cfg.max_requests = value.count
    with pytest.raises(BudgetExceeded):
        value.fetch("https://ijc.org/a")


def test_retry_after_and_robots_wildcards(tmp_path):
    value = transport(
        tmp_path,
        lambda req: httpx.Response(200, text="User-agent: *\nDisallow: /*.pdf$\nAllow: /ok.pdf"),
    )
    parser, _ = value.robot_policy("https://ijc.org/a")
    assert not parser.can_fetch("https://ijc.org/no.pdf", "IJCPolicyResearch")
    assert parser.can_fetch("https://ijc.org/ok.pdf", "IJCPolicyResearch")
    assert value.retry_delay("120", 0) == 120


def test_long_retry_after_pauses_without_early_retry(tmp_path):
    value = transport(
        tmp_path,
        lambda req: (
            httpx.Response(404)
            if req.url.path == "/robots.txt"
            else httpx.Response(429, headers={"Retry-After": "3600"})
        ),
    )
    with pytest.raises(AccessError, match="3600"):
        value.fetch("https://ijc.org/a")
    assert value.count == 2


def test_broken_stream_never_becomes_an_artifact(tmp_path):
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b"%PDF-1.4 partial"
            raise httpx.ReadError("interrupted")

    value = transport(
        tmp_path,
        lambda req: (
            httpx.Response(404)
            if req.url.path == "/robots.txt"
            else httpx.Response(200, stream=Broken())
        ),
    )
    with pytest.raises(TemporaryFailure):
        value.fetch("https://ijc.org/a.pdf")
    assert not list(tmp_path.glob("download-*"))


def test_robots_refresh_can_disallow_previously_allowed_url(tmp_path):
    count = 0

    def handle(req):
        nonlocal count
        count += 1
        return httpx.Response(200, text="User-agent: *\nDisallow: " + ("" if count == 1 else "/"))

    value = transport(tmp_path, handle)
    value.robot_policy("https://ijc.org/a")
    _, parser, delay = value.robots["https://ijc.org"]
    value.robots["https://ijc.org"] = (0, parser, delay)
    with pytest.raises(AccessError, match="Disallowed"):
        value.fetch("https://ijc.org/a")
