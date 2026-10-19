import json
import os
import subprocess
import sys
from pathlib import Path


def test_local_server_collection_and_incremental_cycle(tmp_path):
    runner = Path(__file__).parent / "fixture_crawl.py"
    env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}

    def run():
        result = subprocess.run(
            [sys.executable, str(runner), str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads((tmp_path / "fixture-result.json").read_text())

    first = run()
    manifest = [
        json.loads(line)
        for line in (tmp_path / "runs" / first["run"] / "manifest.jsonl").read_text().splitlines()
    ]
    urls = {r["url"] for r in manifest}
    assert "https://ijc.org/files/report-en.pdf" in urls, (
        tmp_path / "runs" / first["run"] / "candidates.jsonl"
    ).read_text()
    assert "https://ijc.org/en/board" in urls
    assert "https://ijc.org/en/news" not in urls
    assert "https://ijc.org/fr/directive" not in urls
    assert not any("/private/" in r["path"] for r in first["requests"])
    assert all(r["integrity_ok"] for r in manifest)
    original_blobs = {p.name for p in (tmp_path / "objects").iterdir()}
    second = run()
    assert second["run"] != first["run"]
    assert any(r["validator"] for r in second["requests"])
    assert original_blobs == {p.name for p in (tmp_path / "objects").iterdir()}
    assert second["report"]["documents"] == first["report"]["documents"]
