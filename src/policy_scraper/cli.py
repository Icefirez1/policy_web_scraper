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
            cfg.validate_identity(collection=args.command == "collect")
            from scrapy.crawler import CrawlerProcess

            from .crawler import PolicySpider

            run_id = store.run(cfg, args.run_id)
            run_metadata = json.loads(
                store.db.execute("SELECT metadata FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            )
            if (
                args.run_id
                and run_metadata.get("mode") == "preflight"
                and args.command != "preflight"
            ):
                raise ValueError("Preflight runs do not traverse links; start a new discovery run")
            run_metadata["mode"] = args.command
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
                collect=args.command == "collect",
                preflight=args.command == "preflight",
            )
            process.start()
            report = store.audit(run_id)
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
