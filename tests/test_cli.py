import json
import sys

import pytest
import yaml

from policy_scraper.cli import main
from policy_scraper.config import Config


def test_default_seed_registry_loads():
    cfg = Config.load("config/ijc.yaml")
    assert cfg.max_requests == 100
    assert {s["name"] for s in cfg.seeds} >= {
        "active_boards",
        "archived_boards",
        "iwi_publications",
    }


def test_preflight_requires_contact_before_network(tmp_path, monkeypatch, capsys):
    cfg = yaml.safe_load(open("config/ijc.yaml", encoding="utf-8"))
    cfg["contact"] = ""
    cfg["output"] = str(tmp_path / "data")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(sys, "argv", ["policy-scraper", "--config", str(path), "preflight"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "contact" in capsys.readouterr().err


def test_review_command_and_audit(tmp_path, monkeypatch, capsys):
    cfg = yaml.safe_load(open("config/ijc.yaml", encoding="utf-8"))
    cfg["output"] = str(tmp_path / "data")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    review = tmp_path / "review.jsonl"
    review.write_text(
        json.dumps(
            {
                "url": "https://ijc.org/a",
                "decision": "include",
                "language": "fr",
                "evidence": "French-only board directive; source permission verified",
            }
        )
    )
    monkeypatch.setattr(
        sys, "argv", ["policy-scraper", "--config", str(path), "review", "import", str(review)]
    )
    main()
    assert "imported" in capsys.readouterr().out


def test_config_rejects_http_seed(tmp_path):
    cfg = yaml.safe_load(open("config/ijc.yaml", encoding="utf-8"))
    cfg["seeds"][0]["url"] = "http://ijc.org/a"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match="allowlist"):
        Config.load(str(path))
