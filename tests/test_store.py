import json

import pytest

from policy_scraper.config import Config
from policy_scraper.store import Store


def test_resume_incremental_and_integrity(tmp_path):
    cfg = Config(output=str(tmp_path))
    store = Store(cfg.output)
    run = store.run(cfg)
    store.add(run, "https://ijc.org/a", "page")
    store.update(run, "https://ijc.org/a", state="fetching")
    assert store.run(cfg, run) == run
    assert store.rows(run)[0]["state"] == "pending"
    artifact = store.save(b"original", "txt")
    assert artifact == store.save(b"original", "txt")
    store.remember("https://ijc.org/a", artifact, {"etag": "v1"})
    assert store.latest("https://ijc.org/a")
    second = store.run(cfg)
    store.add(second, "https://ijc.org/a", "page")
    assert store.rows(second)[0]["state"] == "pending"
    assert store.save(b"changed", "txt") != artifact
    (store.root / artifact).write_bytes(b"corrupt")
    assert store.latest("https://ijc.org/a") is None
    cfg.delay_seconds = 3
    with pytest.raises(ValueError, match="changed configuration"):
        store.run(cfg, run)


def test_review_import_is_atomic_and_requires_evidence(tmp_path):
    store = Store(str(tmp_path / "data"))
    path = tmp_path / "review.jsonl"
    path.write_text(json.dumps({"url": "https://ijc.org/a", "decision": "include", "evidence": ""}))
    with pytest.raises(ValueError):
        store.import_reviews(str(path))
    assert not store.review("https://ijc.org/a")


def test_audit_reports_integrity_and_gaps(tmp_path):
    cfg = Config(output=str(tmp_path))
    store = Store(cfg.output)
    run = store.run(cfg)
    store.add(run, "https://ijc.org/a", "attachment", decision="include")
    artifact = store.save(b"original", "txt")
    store.update(run, "https://ijc.org/a", state="collected", artifact=artifact)
    assert store.audit(run)["status"] == "complete"
    (store.root / artifact).unlink()
    assert store.audit(run)["status"] == "complete_with_gaps"
