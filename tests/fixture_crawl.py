"""Subprocess fixture: actual local HTTP server and full Scrapy lifecycle.

Only tests rewrite approved HTTPS requests to localhost. Production Transport
continues to require approved HTTPS origins.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from scrapy.crawler import CrawlerProcess

from policy_scraper.config import Config
from policy_scraper.crawler import PolicySpider
from policy_scraper.store import Store
from policy_scraper.transport import Transport

root = Path(sys.argv[1])
requests = []
fixtures = Path(__file__).parent / "fixtures"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        requests.append({"path": self.path, "validator": self.headers.get("If-None-Match")})
        pages = {
            "/robots.txt": (
                "text/plain",
                b"User-agent: *\nDisallow: /private/\nSitemap: https://ijc.org/sitemap.xml",
            ),
            "/policy": (
                "text/html",
                b"<html><h1>Collection policy</h1><p>Fixture research permission.</p></html>",
            ),
            "/en/board": ("text/html", (fixtures / "board.html").read_bytes()),
            "/en/news": ("text/html", (fixtures / "news.html").read_bytes()),
            "/en/catalog?page=1": (
                "text/html",
                b'<html lang="en"><main><h1>Publications</h1><a href="/en/catalog?page=2">Next</a></main></html>',
            ),
            "/en/catalog?page=2": (
                "text/html",
                b'<html lang="en"><main><h1>Publications</h1><a href="/en/catalog?page=2">Next</a></main></html>',
            ),
            "/fr/directive": (
                "text/html",
                b'<html lang="fr"><head><link rel="alternate" hreflang="en" href="/en/board"></head><main><h1>Directive</h1><p>Commission mandate</p></main></html>',
            ),
            "/files/report-en.pdf": ("application/pdf", b"%PDF-1.4\nfixture policy\n%%EOF"),
            "/sitemap.xml": (
                "application/xml",
                b"<urlset><url><loc>https://ijc.org/en/news</loc></url></urlset>",
            ),
        }
        if self.headers.get("If-None-Match") == '"v1"' and self.path in pages:
            self.send_response(304)
            self.end_headers()
            return
        if self.path not in pages:
            self.send_response(404)
            self.end_headers()
            return
        mime, body = pages[self.path]
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"v1"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


class LocalTransport(httpx.BaseTransport):
    def __init__(self):
        self.inner = httpx.HTTPTransport()

    def handle_request(self, request):
        local = httpx.Request(
            request.method,
            request.url.copy_with(scheme="http", host="127.0.0.1", port=server.server_port),
            headers=request.headers,
        )
        return self.inner.handle_request(local)

    def close(self):
        self.inner.close()


original_init = Transport.__init__


def fixture_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    self.client.close()
    self.client = httpx.Client(transport=LocalTransport())


Transport.__init__ = fixture_init
cfg = Config(
    output=str(root),
    contact="fixture@example.org",
    collection_basis="Local test fixtures",
    delay_seconds=0,
    policy_url="https://ijc.org/policy",
    seeds=[{"url": "https://ijc.org/en/board", "name": "board", "expected": "Directive"}],
)
store = Store(cfg.output)
run_id = store.run(cfg)
process = CrawlerProcess(settings={"LOG_ENABLED": False})
process.crawl(PolicySpider, cfg=cfg, store=store, run_id=run_id, collect=True)
process.start()
report = store.audit(run_id)
(root / "fixture-result.json").write_text(
    json.dumps({"run": run_id, "report": report, "requests": requests})
)
server.shutdown()
