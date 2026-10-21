import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from policy_scraper.config import Config
from policy_scraper.rules import local_file_link
from policy_scraper.store import Store
from policy_scraper.transport import (
    BudgetExceeded,
    OriginPaused,
    PermanentHTTPError,
    TemporaryFailure,
    Transport,
)


def setup(tmp_path, handler, **options):
    cfg = Config(output=str(tmp_path), contact="test@example.org", delay_seconds=0, **options)
    store = Store(cfg.output)
    run = store.run(cfg)
    url = "https://ijc.org/report.pdf"
    store.add(run, url, "attachment", decision="include")
    value = Transport(cfg, tmp_path / "access")
    value.client.close()
    value.client = httpx.Client(transport=httpx.MockTransport(handler))
    value.ledger, value.run_id = store, run
    return cfg, store, run, url, value


def test_pause_survives_restart_and_requires_explicit_resume(tmp_path):
    cfg, store, run, url, transport = setup(
        tmp_path, lambda r: httpx.Response(404 if r.url.path == "/robots.txt" else 403)
    )
    with pytest.raises(OriginPaused):
        transport.fetch(url)
    store.db.close()
    reopened = Store(cfg.output)
    assert not reopened.eligible(run, url)
    reopened.recovery("retry-reset", url, run)
    assert not reopened.eligible(run, url)
    reopened.recovery("origin-resume", "https://ijc.org")
    assert reopened.eligible(run, url)


@pytest.mark.parametrize("status", [404, 410])
def test_permanent_response_and_reset(tmp_path, status):
    cfg, store, run, url, transport = setup(tmp_path, lambda r: httpx.Response(status))
    with pytest.raises(PermanentHTTPError) as failure:
        transport.fetch(url)
    store.set_retry(run, url, code="unavailable", status=failure.value.status, terminal=1)
    store.update(run, url, state="failed")
    store.run(cfg, run, known_only=True)
    assert store.rows(run)[0]["state"] == "failed"
    store.recovery("retry-reset", url, run)
    assert store.eligible(run, url)


def test_deadline_and_retry_limit_survive_new_transport(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(429, headers={"Retry-After": "3600"})
        )

    cfg, store, run, url, transport = setup(tmp_path, handle, retries=0)
    with pytest.raises(OriginPaused):
        transport.fetch(url)
    assert store.retry(run, url)["failures"] == 1
    assert store.retry(run, url)["next_retry"] == 4600
    assert not store.eligible(run, url)
    clock[0] = 4601
    assert not store.eligible(run, url)  # Exhaustion survives the cooldown.
    with pytest.raises(TemporaryFailure):
        transport._request(url, {}, 0)
    assert len(calls) == 2


def test_retry_success_resets_failures_and_budget_does_not_increment(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda wait: clock.__setitem__(0, clock[0] + wait))
    responses = iter([503, 200])

    def handle(request):
        return httpx.Response(404 if request.url.path == "/robots.txt" else next(responses))

    cfg, store, run, url, transport = setup(tmp_path, handle)
    transport.fetch(url).path.unlink()
    assert store.retry(run, url)["failures"] == 0
    store.set_retry(run, url, code="network", failures=1, next_retry=clock[0] - 1)
    transport.count = cfg.max_requests
    with pytest.raises(BudgetExceeded):
        transport.fetch(url)
    assert store.retry(run, url)["failures"] == 1


def test_redirect_pause_records_actual_origin(tmp_path):
    def handle(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "ijc.org":
            return httpx.Response(302, headers={"Location": "https://www.ijc.org/target.pdf"})
        return httpx.Response(403)

    cfg, store, run, url, transport = setup(tmp_path, handle)
    with pytest.raises(OriginPaused) as failure:
        transport.fetch(url)
    assert failure.value.url == "https://www.ijc.org/target.pdf"
    assert store.paused("https://www.ijc.org")
    assert not store.paused("https://ijc.org")


@pytest.mark.parametrize(
    "url",
    [
        r"C:\Users\a.pdf",
        r"\\server\file.pdf",
        "https://ijc.org/C%3A%2FUsers/a.pdf",
        "https://ijc.org/U:/Communications/a",
    ],
)
def test_malformed_paths(url):
    assert local_file_link(url)


def test_legacy_migration_is_idempotent(tmp_path):
    cfg, store, run, url, transport = setup(tmp_path, lambda r: httpx.Response(200))
    artifact = store.save(b"original", "txt")
    store.update(run, url, state="failed", reason="HTTP 404")
    store.db.execute("DELETE FROM schema_migrations")
    store.db.commit()
    store.db.close()
    for _ in range(2):
        reopened = Store(cfg.output)
        assert reopened.retry(run, url)["terminal"]
        assert reopened.valid(artifact)
        assert (
            reopened.db.execute("SELECT config_hash FROM runs WHERE id=?", (run,)).fetchone()[0]
            == cfg.digest
        )
        reopened.db.close()
    assert (
        sqlite3.connect(tmp_path / "ledger.sqlite3")
        .execute("SELECT COUNT(*) FROM retry_state")
        .fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("mode", ["retry-pause", "retry-404"])
def test_localhost_retry_lifecycle(tmp_path, mode):
    def invoke():
        result = subprocess.run(
            [sys.executable, "tests/fixture_crawl.py", str(tmp_path), mode],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": str(Path("src").resolve())},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads((tmp_path / "fixture-result.json").read_text())

    first = invoke()
    store = Store(str(tmp_path))
    if mode == "retry-pause":
        assert len(first["requests"]) == 2  # robots and the triggering denial only
        assert sum(r["state"] == "pending" for r in store.rows(first["run"])) == 100
        assert first["report"]["provenance"]["stop_reason"] == "origin_paused"
    else:
        assert first["report"]["documents"] == 1
        assert first["report"]["retry_progress"]["unavailable"] == 1
    second = invoke()
    assert second["requests"] == []
    assert second["report"]["documents"] == first["report"]["documents"]
