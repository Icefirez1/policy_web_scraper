"""Back up a stopped output directory, validate migration, and optionally apply it."""

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from filelock import FileLock

from policy_scraper.store import Store, file_hash


def fingerprint(db):
    result = {}
    for table in ("runs", "urls", "edges", "attempts", "reviews", "equivalents", "latest"):
        digest = hashlib.sha256()
        for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            digest.update(json.dumps(tuple(row), ensure_ascii=True).encode())
        result[table] = digest.hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="data")
    parser.add_argument("--backup", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root, backup = Path(args.output).resolve(), Path(args.backup).resolve()
    if backup == root or backup.is_relative_to(root) or backup.exists():
        raise ValueError("Choose a new backup directory outside the output directory")
    with FileLock(root / ".scraper.lock", timeout=0):
        shutil.copytree(
            root,
            backup,
            ignore=shutil.ignore_patterns(
                ".scraper.lock", "ledger.sqlite3", "ledger.sqlite3-wal", "ledger.sqlite3-shm"
            ),
        )
        with sqlite3.connect(root / "ledger.sqlite3") as source:
            before = fingerprint(source)
            with sqlite3.connect(backup / "ledger.sqlite3") as destination:
                source.backup(destination)
        with tempfile.TemporaryDirectory(prefix="retry-migration-") as scratch:
            shutil.copy2(backup / "ledger.sqlite3", Path(scratch) / "ledger.sqlite3")
            for _ in range(2):
                migrated = Store(scratch)
                assert fingerprint(migrated.db) == before, (
                    "Migration changed original ledger records"
                )
                assert migrated.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                migrated.db.close()
        checked = 0
        for path in (backup / "objects").iterdir():
            assert file_hash(path) == path.stem, f"Invalid backup artifact: {path}"
            checked += 1
        if args.apply:
            migrated = Store(str(root))
            assert fingerprint(migrated.db) == before
            pauses = [dict(r) for r in migrated.db.execute("SELECT * FROM origin_pauses")]
            migrated.db.close()
        else:
            pauses = []
        print(
            json.dumps(
                {
                    "backup": str(backup),
                    "objects_verified": checked,
                    "migration_validated_twice": True,
                    "applied": args.apply,
                    "origin_pauses": pauses,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
