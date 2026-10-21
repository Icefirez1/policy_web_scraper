import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from scrapy.http import HtmlResponse
from twisted.python.failure import Failure

from policy_scraper.cli import main
from policy_scraper.config import Config
from policy_scraper.crawler import PolicySpider
from policy_scraper.store import Store
from policy_scraper.transport import BudgetExceeded


def prepared(tmp_path):
    cfg = Config(output=str(tmp_path))
    store = Store(cfg.output)
    run = store.run(cfg)
    store.add(run, cfg.policy_url, "policy", decision="exclude")
    artifact = store.save(b"<html>Policy</html>", "html", snapshot=True)
    store.update(run, cfg.policy_url, state="discovered", artifact=artifact)
    return cfg, store, run


def test_known_resume_preserves_discovery_and_completed_documents(tmp_path):
    cfg, store, run = prepared(tmp_path)
    for kind in ("page", "sitemap", "attachment"):
        for state in ("fetching", "failed", "blocked", "pending", "collected"):
            url = f"https://ijc.org/{kind}/{state}"
            store.add(run, url, kind)
            store.update(run, url, state=state)
    before = store.rows(run)
    edges = list(store.db.execute("SELECT * FROM edges"))
    store.run(cfg, run, known_only=True)
    for old, new in zip(before, store.rows(run)):
        expected = dict(old)
        if old["kind"] == "attachment" and old["state"] in {"fetching", "failed", "blocked"}:
            expected["state"] = "pending"
        assert new == expected
    assert list(store.db.execute("SELECT * FROM edges")) == edges


@pytest.mark.parametrize("problem", ["missing_id", "unknown", "config", "preflight"])
def test_known_validation_does_not_change_urls(tmp_path, problem):
    cfg, store, run = prepared(tmp_path)
    store.add(run, "https://ijc.org/file.pdf", "attachment", decision="include")
    store.update(run, "https://ijc.org/file.pdf", state="fetching")
    if problem == "config":
        cfg.delay_seconds += 1
    elif problem == "preflight":
        store.db.execute("UPDATE runs SET metadata=? WHERE id=?", ('{"mode":"preflight"}', run))
        store.db.commit()
    before = store.rows(run)
    selected = None if problem == "missing_id" else "unknown" if problem == "unknown" else run
    with pytest.raises(ValueError):
        store.run(cfg, selected, known_only=True)
    assert store.rows(run) == before


def test_known_start_promotes_html_and_preserves_unfinished_reviewed_page(tmp_path):
    cfg, store, run = prepared(tmp_path)
    for name, state in (("saved", "discovered"), ("unfinished", "fetching")):
        url = f"https://ijc.org/{name}"
        store.add(run, url, "page", decision="include")
        artifact = store.save(b"<html>Directive</html>", "html", snapshot=True)
        store.update(run, url, state=state, artifact=artifact)
    before = next(r for r in store.rows(run) if r["url"].endswith("unfinished"))
    spider = PolicySpider(cfg, store, run, collect=True, known_only=True)

    async def start():
        return [request async for request in spider.start()]

    assert asyncio.run(start()) == []
    assert next(r for r in store.rows(run) if r["url"].endswith("unfinished")) == before
    saved = next(r for r in store.rows(run) if r["url"].endswith("saved"))
    assert saved["state"] == "collected"
    assert saved["artifact"].startswith("objects/")
    assert store.valid(saved["artifact"])


@pytest.mark.parametrize("state", ["fetching", "failed", "discovered"])
def test_known_fetches_missing_policy_before_attachments(tmp_path, state):
    cfg, store, run = prepared(tmp_path)
    store.update(run, cfg.policy_url, state=state, artifact=None)
    store.add(run, "https://ijc.org/queued", "page")
    store.add(run, "https://ijc.org/report.pdf", "attachment", decision="include")
    queued = next(r for r in store.rows(run) if r["url"].endswith("queued"))
    store.run(cfg, run, known_only=True)
    spider = PolicySpider(cfg, store, run, collect=True, known_only=True)

    async def start():
        return [request async for request in spider.start()]

    requests = asyncio.run(start())
    assert [request.url for request in requests] == [cfg.policy_url]
    assert not spider.collection_started
    path = tmp_path / "response"
    body = b'<html><h1>Policy</h1><a href="/new-page">Link</a></html>'
    path.write_bytes(body)
    requests[0].meta["download_path"] = str(path)
    response = HtmlResponse(
        cfg.policy_url, body=body, request=requests[0], headers={"Content-Type": "text/html"}
    )
    assert spider.parse(response) == []
    assert [r.url for r in spider.collection_requests()] == ["https://ijc.org/report.pdf"]
    assert next(r for r in store.rows(run) if r["url"].endswith("queued")) == queued
    assert not any(r["url"].endswith("new-page") for r in store.rows(run))


def test_known_failed_policy_blocks_collection(tmp_path):
    cfg, store, run = prepared(tmp_path)
    store.update(run, cfg.policy_url, state="failed", artifact=None)
    store.add(run, "https://ijc.org/report.pdf", "attachment", decision="include")
    spider = PolicySpider(cfg, store, run, collect=True, known_only=True)
    assert spider.collection_requests() == []
    assert spider.halted


def test_known_collection_then_discovery_then_collection(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}

    def invoke(mode):
        result = subprocess.run(
            [sys.executable, "tests/fixture_crawl.py", str(tmp_path), mode],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads((tmp_path / "fixture-result.json").read_text())

    first = invoke("prepare-known")
    assert first["report"]["documents"] == 2
    assert {r["path"] for r in first["requests"]} == {"/robots.txt", "/files/report-en.pdf"}
    store = Store(str(tmp_path))
    rows = store.rows(first["run"])
    assert next(r for r in rows if r["url"].endswith("/en/board"))["state"] == "pending"
    edges = [tuple(r) for r in store.db.execute("SELECT * FROM edges")]
    second = invoke("known")
    assert second["requests"] == []
    assert store.rows(first["run"]) == rows
    assert [tuple(r) for r in store.db.execute("SELECT * FROM edges")] == edges
    discovery = invoke("discover")
    assert discovery["run"] == first["run"]
    assert len(store.rows(first["run"])) > len(rows)
    final = invoke("known")
    assert final["report"]["documents"] > first["report"]["documents"]
    assert final["run"] == first["run"]


@pytest.mark.parametrize("interrupted", [False, True])
def test_known_retry_after_budget_or_interruption(tmp_path, interrupted):
    cfg, store, run = prepared(tmp_path)
    url = "https://ijc.org/report.pdf"
    store.add(run, url, "attachment", decision="include")
    store.add(run, "https://ijc.org/saved", "page", decision="include")
    snapshot = store.save(b"<html>Directive</html>", "html", snapshot=True)
    store.update(run, "https://ijc.org/saved", state="discovered", artifact=snapshot)
    spider = PolicySpider(cfg, store, run, collect=True, known_only=True)
    requests = spider.collection_requests()
    assert len(requests) == 1
    if not interrupted:
        failure = Failure(BudgetExceeded("Network request budget exhausted"))
        failure.request = requests[0]
        closed = []
        spider.crawler = SimpleNamespace(
            engine=SimpleNamespace(close_spider=lambda *args: closed.append(args))
        )
        spider.failed(failure)
        assert closed and spider.halted
    spider.closed_run("shutdown" if interrupted else "request_budget")
    assert store.audit(run)["documents"] == 1
    store.run(cfg, run, known_only=True)
    resumed = PolicySpider(cfg, store, run, collect=True, known_only=True)
    assert [r.url for r in resumed.collection_requests()] == [url]
    assert sum(r["state"] == "collected" for r in store.rows(run)) == 1


def test_reviews_and_invalid_attachment_response(tmp_path):
    cfg, store, run = prepared(tmp_path)
    include = "https://ijc.org/approved.pdf"
    exclude = "https://ijc.org/excluded.pdf"
    store.add(run, include, "attachment")
    store.add(run, exclude, "attachment", decision="include")
    reviews = tmp_path / "reviews.jsonl"
    reviews.write_text(
        "\n".join(
            json.dumps(
                {
                    "url": url,
                    "decision": decision,
                    "evidence": "Human review",
                    "language": "en",
                }
            )
            for url, decision in ((include, "include"), (exclude, "exclude"))
        )
    )
    store.import_reviews(str(reviews))
    spider = PolicySpider(cfg, store, run, collect=True, known_only=True)
    requests = spider.collection_requests()
    assert [r.url for r in requests] == [include]
    path = tmp_path / "response"
    body = b"<html><h1>Landing page</h1></html>"
    path.write_bytes(body)
    requests[0].meta["download_path"] = str(path)
    response = HtmlResponse(
        include, body=body, request=requests[0], headers={"Content-Type": "text/html"}
    )
    assert spider.parse(response) == []
    assert next(r for r in store.rows(run) if r["url"] == include)["state"] == "failed"
    assert store.audit(run)["documents"] == 0


def test_collected_integrity_problem_is_audited(tmp_path):
    cfg, store, run = prepared(tmp_path)
    url = "https://ijc.org/broken.pdf"
    store.add(run, url, "attachment", decision="include")
    store.update(run, url, state="collected", artifact="objects/missing.pdf")
    spider = PolicySpider(cfg, store, run, collect=True, known_only=True)
    assert spider.collection_requests() == []
    report = store.audit(run)
    assert report["status"] == "complete_with_gaps"
    assert "artifact integrity failure" in (tmp_path / "runs" / run / "issues.jsonl").read_text()


def test_cli_known_requires_run_id(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["policy-scraper", "collect-known"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


@pytest.mark.parametrize("unfinished", [False, True])
def test_cli_known_summary_exit_and_provenance(tmp_path, monkeypatch, capsys, unfinished):
    from dataclasses import asdict

    cfg = Config(
        output=str(tmp_path / "data"),
        contact="fixture@example.org",
        collection_basis="Test fixtures",
        seeds=[{"url": "https://ijc.org/saved", "name": "saved", "expected": "Directive"}],
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(asdict(cfg)))
    cfg = Config.load(str(config_path))
    store = Store(cfg.output)
    run = store.run(cfg)
    original = json.loads(store.db.execute("SELECT metadata FROM runs").fetchone()[0])
    original["mode"] = "discover"
    store.db.execute("UPDATE runs SET metadata=?", (json.dumps(original),))
    store.db.commit()
    store.add(run, cfg.policy_url, "policy", decision="exclude")
    policy = store.save(b"<html>Policy</html>", "html", snapshot=True)
    store.update(run, cfg.policy_url, state="discovered", artifact=policy)
    store.add(run, "https://ijc.org/saved", "page", decision="include")
    store.update(run, "https://ijc.org/saved", state="discovered", artifact=policy)
    if unfinished:
        store.add(run, "https://ijc.org/queued", "page")

    class LocalProcess:
        def crawl(self, cls, **kwargs):
            self.spider = cls(**kwargs)

        def start(self):
            assert self.spider.collection_requests() == []
            self.spider.closed_run("finished")

    monkeypatch.setattr("scrapy.crawler.CrawlerProcess", LocalProcess)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "policy-scraper",
            "--config",
            str(config_path),
            "collect-known",
            "--run-id",
            run,
        ],
    )
    if unfinished:
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2
    else:
        main()
    output = capsys.readouterr().out
    assert "Documents added: 1; total collected: 1" in output
    assert f"unfinished discovery URLs: {int(unfinished)}" in output
    metadata = json.loads(store.db.execute("SELECT metadata FROM runs").fetchone()[0])
    assert metadata == {
        **original,
        "last_command": "collect-known",
        "stop_reason": "no_eligible_work" if unfinished else "finished",
    }
