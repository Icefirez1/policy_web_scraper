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
        if self.path == "/a-denied.pdf":
            self.send_response(401)
            self.end_headers()
            return
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
mode = sys.argv[2] if len(sys.argv) > 2 else "collect"
if mode in {"retry-pause", "retry-404"}:
    marker = root / "retry-run.txt"
    if marker.exists():
        run_id = store.run(cfg, marker.read_text(), known_only=True)
    else:
        run_id = store.run(cfg)
        marker.write_text(run_id)
        store.add(run_id, cfg.policy_url, "policy", decision="exclude")
        policy = store.save(b"<html>Fixture policy</html>", "html", snapshot=True)
        store.update(run_id, cfg.policy_url, state="discovered", artifact=policy)
        store.add(
            run_id,
            "https://ijc.org/a-denied.pdf"
            if mode == "retry-pause"
            else "https://ijc.org/a-missing.pdf",
            "attachment",
            decision="include",
        )
        if mode == "retry-pause":
            for index in range(100):
                store.add(
                    run_id, f"https://ijc.org/b-{index}.pdf", "attachment", decision="include"
                )
        else:
            store.add(
                run_id, "https://ijc.org/files/report-en.pdf", "attachment", decision="include"
            )
elif mode == "prepare-known":
    run_id = store.run(cfg)
    store.add(run_id, cfg.policy_url, "policy", decision="exclude")
    policy = store.save(b"<html>Fixture policy</html>", "html", snapshot=True)
    store.update(run_id, cfg.policy_url, state="discovered", artifact=policy)
    store.add(run_id, "https://ijc.org/saved", "page", decision="include")
    saved = store.save(b"<html>Saved directive</html>", "html", snapshot=True)
    store.update(run_id, "https://ijc.org/saved", state="discovered", artifact=saved)
    store.add(run_id, "https://ijc.org/en/board", "page")
    store.update(run_id, "https://ijc.org/en/board", state="fetching")
    store.add(run_id, "https://ijc.org/files/report-en.pdf", "attachment", decision="include")
    for name, decision in (("excluded", "exclude"), ("uncertain", "needs_review")):
        store.add(run_id, f"https://ijc.org/files/{name}.pdf", "attachment", decision=decision)
    (root / "known-run.txt").write_text(run_id)
elif mode in {"known", "discover"}:
    run_id = store.run(cfg, (root / "known-run.txt").read_text(), known_only=mode == "known")
else:
    run_id = store.run(cfg)
process = CrawlerProcess(settings={"LOG_ENABLED": False})
process.crawl(
    PolicySpider,
    cfg=cfg,
    store=store,
    run_id=run_id,
    collect=mode != "discover",
    known_only=mode in {"prepare-known", "known", "retry-pause", "retry-404"},
)
process.start()
report = store.audit(run_id)
(root / "fixture-result.json").write_text(
    json.dumps({"run": run_id, "report": report, "requests": requests})
)
server.shutdown()
