import json

import scrapy
from scrapy.http import HtmlResponse

from policy_scraper.config import Config
from policy_scraper.crawler import PolicySpider
from policy_scraper.store import Store


def page(tmp_path, body, final_url="https://ijc.org/report", seed=None):
    cfg = Config(output=str(tmp_path), seeds=[seed] if seed else [])
    store = Store(cfg.output)
    run = store.run(cfg)
    source = seed["url"] if seed else final_url
    store.add(run, source, "page")
    spider = PolicySpider(cfg, store, run, preflight=True)
    path = tmp_path / "response"
    path.write_bytes(body)
    request = scrapy.Request(source, meta={"source_url": source, "download_path": str(path)})
    response = HtmlResponse(
        final_url, body=body, headers={"Content-Type": "text/html"}, request=request
    )
    spider.parse(response)
    return store.rows(run)[0]


def test_homepage_redirect_is_not_a_healthy_seed(tmp_path):
    row = page(
        tmp_path,
        b"<html><h1>Home</h1></html>",
        final_url="https://ijc.org/en",
        seed={"url": "https://ijc.org/library", "name": "library", "expected": "Reports"},
    )
    assert json.loads(row["metadata"])["health_error"] == "Seed title/redirect mismatch"


def test_generic_footer_does_not_restrict_document(tmp_path):
    row = page(
        tmp_path,
        b"<html lang='en'><h1>Board Directive</h1><p>Commission mandate</p>"
        b"<footer>Copyright IJC. All rights reserved.</footer></html>",
    )
    assert row["decision"] == "include"


def test_document_specific_restriction_requires_review(tmp_path):
    row = page(
        tmp_path,
        b"<html><main><h1>Board Directive</h1><p>Commission mandate. "
        b"All rights reserved.</p></main></html>",
    )
    assert row["decision"] == "needs_review"
    assert json.loads(row["metadata"])["rights_status"] == "unresolved"


def test_french_without_proven_equivalent_needs_review(tmp_path):
    row = page(
        tmp_path,
        b"<html lang='fr'><main><h1>Directive</h1><p>Commission mandate.</p></main></html>",
    )
    assert row["decision"] == "needs_review"


def test_200_error_page_rejected(tmp_path):
    row = page(tmp_path, b"<html><h1>404 Not Found</h1></html>")
    assert row["state"] == "failed"


def test_empty_listing_flagged(tmp_path):
    row = page(
        tmp_path,
        b"<html><h1>Publications</h1></html>",
        seed={
            "url": "https://ijc.org/report",
            "name": "library",
            "expected": "Publications",
            "listing": True,
        },
    )
    assert json.loads(row["metadata"])["health_error"] == "Expected listing structure missing"


def test_generic_pdf_label_inherits_proven_document_context(tmp_path):
    cfg = Config(output=str(tmp_path))
    store = Store(cfg.output)
    run = store.run(cfg)
    spider = PolicySpider(cfg, store, run)
    list(
        spider.discover_link(
            "/files/ID123.pdf",
            "https://ijc.org/treaty",
            "PDF",
            {"title": "Boundary Waters Treaty", "category": "treaty", "corpus_decision": "include"},
        )
    )
    row = store.rows(run)[0]
    assert row["decision"] == "include"
    assert json.loads(row["metadata"])["category"] == "treaty"
