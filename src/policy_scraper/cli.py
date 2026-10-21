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
    args = parser.parse_args()
    lock = None
    try:
        cfg = Config.load(args.config)
        Path(cfg.output).mkdir(parents=True, exist_ok=True)
        lock = FileLock(Path(cfg.output) / ".scraper.lock", timeout=0)
        lock.acquire()
        store = Store(cfg.output)
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
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    main()
