"""SQLite owns run state; content objects are immutable and committed atomically."""

import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import RULE_VERSION
from .config import Config


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Store:
    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in ("objects", "snapshots", "tmp", "runs"):
            (self.root / folder).mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "ledger.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, config_hash TEXT, started TEXT, finished TEXT,
                status TEXT, metadata TEXT);
            CREATE TABLE IF NOT EXISTS urls (
                run TEXT, url TEXT, kind TEXT, state TEXT, decision TEXT,
                reason TEXT, metadata TEXT, artifact TEXT, PRIMARY KEY(run,url));
            CREATE TABLE IF NOT EXISTS edges (
                run TEXT, parent TEXT, child TEXT, label TEXT,
                PRIMARY KEY(run,parent,child));
            CREATE TABLE IF NOT EXISTS attempts (
                run TEXT, url TEXT, at TEXT, status INTEGER, detail TEXT);
            CREATE TABLE IF NOT EXISTS reviews (
                url TEXT PRIMARY KEY, decision TEXT, evidence TEXT, language TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS equivalents (
                run TEXT, source TEXT, alternate TEXT, language TEXT,
                PRIMARY KEY(run,source,alternate));
            CREATE TABLE IF NOT EXISTS latest (
                url TEXT PRIMARY KEY, artifact TEXT, metadata TEXT);
        """)

    def run(self, cfg: Config, run_id: str | None = None) -> str:
        if run_id:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["config_hash"] != cfg.digest:
                raise ValueError("Unknown run or changed configuration; start a new run")
            self.db.execute(
                "UPDATE urls SET state='pending' WHERE run=? AND state IN ('fetching','failed','blocked')",
                (run_id,),
            )
            self.db.commit()
            return run_id
        run_id = uuid.uuid4().hex
        try:
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            revision = None
        from dataclasses import asdict

        metadata = {
            "config": asdict(cfg),
            "rule_version": RULE_VERSION,
            "revision": revision,
            "lock_hash": file_hash(Path("uv.lock")) if Path("uv.lock").exists() else None,
        }
        package = Path(__file__).parent
        metadata["source_hash"] = hashlib.sha256(
            b"".join(f.name.encode() + f.read_bytes() for f in sorted(package.glob("*.py")))
        ).hexdigest()
        self.db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?)",
            (run_id, cfg.digest, now(), None, "running", json.dumps(metadata)),
        )
        self.db.commit()
        return run_id

    def add(
        self,
        run: str,
        url: str,
        kind: str,
        parent: str = "",
        label: str = "",
        metadata: dict | None = None,
        decision: str = "needs_review",
        reason: str = "",
    ):
        self.db.execute(
            "INSERT OR IGNORE INTO urls VALUES (?,?,?,?,?,?,?,?)",
            (run, url, kind, "pending", decision, reason, json.dumps(metadata or {}), None),
        )
        self.db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", (run, parent, url, label))
        self.db.commit()

    def rows(self, run: str) -> list[dict]:
        return [
            dict(r) for r in self.db.execute("SELECT * FROM urls WHERE run=? ORDER BY url", (run,))
        ]

    def update(self, run: str, url: str, **values):
        allowed = {"state", "decision", "reason", "metadata", "artifact", "kind"}
        if not values.keys() <= allowed:
            raise ValueError("Invalid ledger update")
        if "metadata" in values:
            values["metadata"] = json.dumps(values["metadata"])
        self.db.execute(
            f"UPDATE urls SET {','.join(k + '=?' for k in values)} WHERE run=? AND url=?",
            (*values.values(), run, url),
        )
        self.db.commit()

    def attempt(self, run: str, url: str, status: int | None, detail: str):
        self.db.execute(
            "INSERT INTO attempts VALUES (?,?,?,?,?)", (run, url, now(), status, detail)
        )
        self.db.commit()

    def save(self, body: bytes, ext: str, snapshot: bool = False) -> str:
        digest = hashlib.sha256(body).hexdigest()
        relative = f"{'snapshots' if snapshot else 'objects'}/{digest}.{ext}"
        target = self.root / relative
        if not target.exists() or file_hash(target) != digest:
            temporary = self.root / "tmp" / uuid.uuid4().hex
            with temporary.open("wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        return relative

    def valid(self, artifact: str | None) -> bool:
        if not artifact:
            return False
        path = (self.root / artifact).resolve()
        return path.is_relative_to(self.root) and path.is_file() and file_hash(path) == path.stem

    def latest(self, url: str):
        row = self.db.execute("SELECT * FROM latest WHERE url=?", (url,)).fetchone()
        return dict(row) if row and self.valid(row["artifact"]) else None

    def remember(self, url: str, artifact: str, metadata: dict):
        self.db.execute(
            "INSERT OR REPLACE INTO latest VALUES (?,?,?)", (url, artifact, json.dumps(metadata))
        )
        self.db.commit()

    def review(self, url: str):
        row = self.db.execute("SELECT * FROM reviews WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None

    def import_reviews(self, path: str):
        from .rules import normalize

        records = [
            json.loads(line)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for r in records:
            if r["decision"] not in {"include", "exclude"} or not r.get("evidence", "").strip():
                raise ValueError("Every review needs include/exclude and evidence")
            if normalize(r["url"]) != r["url"] or r.get("language") not in {
                None,
                "en",
                "fr",
                "bilingual",
            }:
                raise ValueError("Invalid review URL or language")
        with self.db:
            for r in records:
                self.db.execute(
                    "INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?)",
                    (r["url"], r["decision"], r["evidence"], r.get("language"), now()),
                )

    def audit(self, run: str) -> dict:
        run_row = self.db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
        if not run_row:
            raise ValueError("Unknown run")
        destination = self.root / "runs" / run
        destination.mkdir(exist_ok=True)
        rows = self.rows(run)
        manifest, issues = [], []
        if not rows:
            issues.append({"reason": "Run has no discovered URLs"})
        for row in rows:
            meta = json.loads(row["metadata"])
            if row["state"] == "collected":
                valid = self.valid(row["artifact"])
                record = {
                    **meta,
                    "url": row["url"],
                    "artifact": row["artifact"],
                    "run_id": run,
                    "integrity_ok": valid,
                    "document_id": hashlib.sha256(row["url"].encode()).hexdigest(),
                    "version_id": Path(row["artifact"]).stem,
                }
                manifest.append(record)
                if not valid:
                    issues.append({"url": row["url"], "reason": "artifact integrity failure"})
            if (
                (row["state"] in {"pending", "fetching"} and row["decision"] != "exclude")
                or row["state"] in {"failed", "blocked", "unsupported"}
                or (row["decision"] == "needs_review")
                or meta.get("health_error")
            ):
                issues.append(
                    {
                        "url": row["url"],
                        "state": row["state"],
                        "reason": meta.get("health_error") or row["reason"],
                    }
                )
        exports = {
            "manifest": manifest,
            "candidates": rows,
            "issues": issues,
            "review_required": [r for r in rows if r["decision"] == "needs_review"],
            "excluded": [r for r in rows if r["decision"] == "exclude"],
            "attempts": [
                dict(r) for r in self.db.execute("SELECT * FROM attempts WHERE run=?", (run,))
            ],
            "edges": [dict(r) for r in self.db.execute("SELECT * FROM edges WHERE run=?", (run,))],
        }
        for name, records in exports.items():
            with (destination / f"{name}.jsonl").open("w", encoding="utf-8") as stream:
                for record in records:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        fields = [
            "url",
            "final_url",
            "title",
            "language",
            "category",
            "date",
            "document_status",
            "board",
            "watershed",
            "parent",
            "retrieved_at",
            "sha256",
            "size",
            "artifact",
            "rights_evidence",
            "version_id",
        ]
        with (destination / "inventory.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(manifest)
        status = run_row["status"]
        if status not in {"blocked", "interrupted"}:
            status = "complete_with_gaps" if issues else "complete"
        report = {
            "run_id": run,
            "started": run_row["started"],
            "finished": now(),
            "status": status,
            "documents": len(manifest),
            "issues": len(issues),
            "states": dict(Counter(r["state"] for r in rows)),
            "coverage": {
                key: dict(Counter(str(r.get(key) or "unknown") for r in manifest))
                for key in ("category", "language", "board", "watershed", "seed")
            },
            "provenance": json.loads(run_row["metadata"]),
        }
        referenced = {
            r[0]
            for r in self.db.execute(
                "SELECT artifact FROM urls WHERE artifact IS NOT NULL UNION SELECT artifact FROM latest"
            )
        }
        report["unreferenced_objects"] = [
            p.relative_to(self.root).as_posix()
            for p in (self.root / "objects").iterdir()
            if p.relative_to(self.root).as_posix() not in referenced
        ]
        report["abandoned_temporary_files"] = [
            p.relative_to(self.root).as_posix() for p in (self.root / "tmp").iterdir()
        ]
        report["abandoned_temporary_files"].extend(
            p.relative_to(self.root).as_posix() for p in (destination / "access").glob("download-*")
        )
        (destination / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        self.db.execute("UPDATE runs SET status=?, finished=? WHERE id=?", (status, now(), run))
        self.db.commit()
        return report
