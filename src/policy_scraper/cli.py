"""Command line interface; use one process per crawl cycle."""

import argparse
import json
from pathlib import Path

from filelock import FileLock, Timeout

from .config import Config
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Collect original IJC policy documents")
    parser.add_argument("--config", default="config/ijc.yaml")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "discover", "collect", "audit"):
        sub = commands.add_parser(command)
        sub.add_argument("--run-id")
    known = commands.add_parser("collect-known", help="Collect known documents without discovery")
    known.add_argument("--run-id", required=True)
    review = commands.add_parser("review")
    review.add_argument("action", choices=["import"])
    review.add_argument("file")
    reset = commands.add_parser("retry-reset")
    reset.add_argument("--run-id", required=True)
    reset.add_argument("--url", required=True)
    resume = commands.add_parser("origin-resume")
    resume.add_argument("--origin", required=True)
    args = parser.parse_args()
    lock = None
    store = None
    try:
        cfg = Config.load(args.config)
        Path(cfg.output).mkdir(parents=True, exist_ok=True)
        lock = FileLock(Path(cfg.output) / ".scraper.lock", timeout=0)
        lock.acquire()
        store = Store(cfg.output)
        if args.command in {"retry-reset", "origin-resume"}:
            if args.command == "origin-resume":
                from urllib.parse import urlsplit

                origin = urlsplit(args.origin)
                if (
                    origin.scheme != "https"
                    or origin.hostname not in cfg.allowed_hosts
                    or origin.netloc != origin.hostname
                    or origin.path
                    or origin.query
                    or origin.fragment
                ):
                    raise ValueError("Use an exact approved HTTPS origin without a path")
                store.recovery(args.command, args.origin)
            else:
                store.recovery(args.command, args.url, args.run_id)
            print("Recovery action recorded; no network requests made.")
            return
        if args.command == "review":
            store.import_reviews(args.file)
            print("Review decisions imported.")
            return
        if args.command == "audit":
            if not args.run_id:
                raise ValueError("audit requires --run-id")
            report = store.audit(args.run_id)
        else:
            known_only = args.command == "collect-known"
            cfg.validate_identity(collection=args.command in {"collect", "collect-known"})
            from scrapy.crawler import CrawlerProcess

            from .crawler import PolicySpider

            run_id = store.run(cfg, args.run_id, known_only=known_only)
            before = sum(r["state"] == "collected" for r in store.rows(run_id))
            attempts_before = store.db.execute(
                "SELECT COUNT(*) FROM attempts WHERE run=?", (run_id,)
            ).fetchone()[0]
            summary_path = store.root / "runs" / run_id / "invocation.json"
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(
                json.dumps({"continue_batch": False, "stop_reason": "running"}), encoding="utf-8"
            )
            run_metadata = json.loads(
                store.db.execute("SELECT metadata FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            )
            if (
                args.run_id
                and run_metadata.get("mode") == "preflight"
                and args.command != "preflight"
            ):
                raise ValueError("Preflight runs do not traverse links; start a new discovery run")
            if not known_only:
                run_metadata["mode"] = args.command
            run_metadata["last_command"] = args.command
            run_metadata.pop("stop_reason", None)
            run_metadata.pop("scheduled_requests", None)
            store.db.execute(
                "UPDATE runs SET metadata=?,status='running' WHERE id=?",
                (json.dumps(run_metadata), run_id),
            )
            store.db.commit()
            print(f"Run ID: {run_id}", flush=True)
            process = CrawlerProcess()
            process.crawl(
                PolicySpider,
                cfg=cfg,
                store=store,
                run_id=run_id,
                collect=args.command in {"collect", "collect-known"},
                known_only=known_only,
                preflight=args.command == "preflight",
            )
            process.start()
            report = store.audit(run_id)
            attempts_added = (
                store.db.execute("SELECT COUNT(*) FROM attempts WHERE run=?", (run_id,)).fetchone()[
                    0
                ]
                - attempts_before
            )
            stop_reason = report["provenance"].get("stop_reason", "unknown")
            summary = {
                "documents_added": report["documents"] - before,
                "http_attempts": attempts_added,
                "scheduled_requests": report["provenance"].get("scheduled_requests", 0),
                "stop_reason": stop_reason,
                "continue_batch": stop_reason == "request_budget" and attempts_added > 0,
                **report["retry_progress"],
            }
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(json.dumps(summary, indent=2))
            if known_only:
                remaining = sum(
                    r["kind"] in {"page", "policy", "sitemap"}
                    and r["state"] in {"pending", "fetching", "failed", "blocked"}
                    for r in store.rows(run_id)
                )
                print(
                    f"Documents added: {report['documents'] - before}; "
                    f"total collected: {report['documents']}; "
                    f"unfinished discovery URLs: {remaining}."
                )
                print("Unfinished discovery can leave this run complete_with_gaps.")
        print(json.dumps(report, indent=2))
        if report["status"] != "complete":
            raise SystemExit(2)
    except Timeout:
        parser.exit(1, "Error: Another scraper process is using this output directory\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"Error: {error}\n")
    finally:
        if store is not None:
            store.db.close()
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    main()
