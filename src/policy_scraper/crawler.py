"""Scrapy discovery with durable frontier and a streaming HTTP download handler."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import scrapy
from scrapy import signals
from scrapy.exceptions import DontCloseSpider
from scrapy.http import HtmlResponse, Response, XmlResponse
from twisted.internet.threads import deferToThread

from .rules import classify, inspect_body, language, link_kind, normalize
from .store import Store, now
from .transport import AccessError, BudgetExceeded, Transport


class DownloadHandler:
    def __init__(self, crawler):
        self.crawler = crawler
        self.transport = None

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def download_request(self, request, spider=None):
        return deferToThread(self._download, request)

    def _download(self, request):
        spider = self.crawler.spider
        if self.transport is None:
            # Dedicated connection for the single download worker.
            self.transport = Transport(
                spider.cfg, spider.store.root / "runs" / spider.run_id / "access"
            )
        ledger = Store(spider.cfg.output)
        self.transport.attempt = lambda url, status, detail: ledger.attempt(
            spider.run_id, url, status, detail
        )
        try:
            result = self.transport.fetch(
                request.url,
                {k.decode(): b", ".join(v).decode() for k, v in request.headers.items()},
            )
        finally:
            ledger.db.close()
        request.meta["download_path"] = str(result.path)
        request.meta["redirect_chain"] = result.redirects
        ctype = result.headers.get("content-type", "")
        cls = HtmlResponse if "html" in ctype else XmlResponse if "xml" in ctype else Response
        body = result.path.read_bytes() if cls != Response else b""
        return cls(
            result.url, status=result.status, headers=result.headers, body=body, request=request
        )

    def close(self):
        if self.transport:
            self.transport.close()


class PolicySpider(scrapy.Spider):
    name = "ijc_policy"
    custom_settings = {
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_HANDLERS": {
            "https": "policy_scraper.crawler.DownloadHandler",
            "http": "policy_scraper.crawler.DownloadHandler",
        },
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy.downloadermiddlewares.redirect.RedirectMiddleware": None,
            "scrapy.downloadermiddlewares.redirect.MetaRefreshMiddleware": None,
            "scrapy.downloadermiddlewares.retry.RetryMiddleware": None,
            "scrapy.downloadermiddlewares.httpcompression.HttpCompressionMiddleware": None,
            "scrapy.downloadermiddlewares.cookies.CookiesMiddleware": None,
        },
        "ROBOTSTXT_OBEY": False,  # Enforced on *every hop* by Transport instead.
        "TELNETCONSOLE_ENABLED": False,
        "HTTPERROR_ALLOW_ALL": True,
        "LOG_LEVEL": "INFO",
    }

    def __init__(self, cfg, store, run_id, collect=False, preflight=False, **kwargs):
        super().__init__(**kwargs)
        self.cfg, self.store, self.run_id = cfg, store, run_id
        self.collect_mode, self.preflight = collect, preflight
        self.halted = False
        self.collection_started = False
        self.seeds = {normalize(s["url"]): s for s in cfg.seeds}
        self.page_hashes = {}

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider.idle, signal=signals.spider_idle)
        crawler.signals.connect(spider.closed_run, signal=signals.spider_closed)
        crawler.signals.connect(spider.callback_error, signal=signals.spider_error)
        return spider

    def callback_error(self, failure, response, spider):
        self.store.update(
            self.run_id,
            response.meta["source_url"],
            state="failed",
            reason=f"Callback error: {failure.value}",
        )

    async def start(self):
        self.store.add(self.run_id, self.cfg.policy_url, "policy", metadata={"seed": "policy"})
        for url, seed in self.seeds.items():
            self.store.add(self.run_id, url, "page", metadata={"seed": seed["name"]})
        for row in self.store.rows(self.run_id):
            if row["kind"] in {"page", "policy", "sitemap"} and row["state"] == "pending":
                yield self.request(row["url"])

    def request(self, url):
        self.store.update(self.run_id, url, state="fetching")
        headers = {}
        latest = self.store.latest(url)
        if latest:
            meta = json.loads(latest["metadata"])
            for key, header in (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")):
                if meta.get(key):
                    headers[header] = meta[key]
        return scrapy.Request(
            url,
            callback=self.parse,
            errback=self.failed,
            headers=headers,
            dont_filter=True,
            meta={"source_url": url},
        )

    def row(self, url):
        return dict(
            self.store.db.execute(
                "SELECT * FROM urls WHERE run=? AND url=?", (self.run_id, url)
            ).fetchone()
        )

    def failed(self, failure):
        url = failure.request.meta["source_url"]
        blocked = failure.check(AccessError)
        self.store.update(
            self.run_id, url, state="blocked" if blocked else "failed", reason=str(failure.value)
        )
        if failure.check(BudgetExceeded):
            self.halted = True
            self.crawler.engine.close_spider(self, "request_budget")

    def parse(self, response):
        source = response.meta["source_url"]
        row = self.row(source)
        path = Path(response.meta["download_path"])
        try:
            return list(self.process(response, source, row, path))
        except (ValueError, OSError) as error:
            self.store.update(self.run_id, source, state="failed", reason=str(error))
            return []
        finally:
            path.unlink(missing_ok=True)

    def process(self, response, source, row, path):
        http_status = response.status
        if response.status == 304:
            latest = self.store.latest(source)
            if not latest:
                raise ValueError("304 without a verified local artifact; retry in a new run")
            cached = json.loads(latest["metadata"])
            body = (self.store.root / latest["artifact"]).read_bytes()
            mime = cached.get("mime", "")
            if "html" in mime:
                response = HtmlResponse(
                    response.url,
                    body=body,
                    encoding=cached.get("encoding", "utf-8"),
                    headers={
                        "Content-Type": mime,
                        "ETag": cached.get("etag") or "",
                        "Last-Modified": cached.get("last_modified") or "",
                    },
                    request=response.request,
                )
        elif response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        else:
            body = path.read_bytes() if row["kind"] != "attachment" else None
            mime = response.headers.get(b"Content-Type", b"").decode()
        metadata = json.loads(row["metadata"])
        metadata.update(
            {
                "final_url": response.url,
                "retrieved_at": now(),
                "mime": mime,
                "redirect_chain": response.meta.get("redirect_chain", []),
                "etag": response.headers.get(b"ETag", b"").decode()
                or (cached.get("etag") if response.status == 304 else None),
                "last_modified": response.headers.get(b"Last-Modified", b"").decode()
                or (cached.get("last_modified") if response.status == 304 else None),
                "rights_evidence": metadata.get("rights_evidence")
                or self.cfg.collection_basis
                or None,
                "http_status": http_status,
            }
        )
        if row["kind"] == "sitemap":
            xml = scrapy.Selector(body=body, type="xml")
            xml.remove_namespaces()
            for loc in xml.css("loc::text").getall():
                yield from self.discover_link(
                    loc, source, "sitemap", metadata, sitemap=bool(xml.css("sitemapindex"))
                )
            artifact = self.store.save(body, "xml", snapshot=True)
            self.store.update(
                self.run_id,
                source,
                state="discovered",
                decision="exclude",
                reason="discovery sitemap",
                metadata=metadata,
                artifact=artifact,
            )
            return
        if row["kind"] == "attachment":
            # Validate only boundaries; transfer the streamed file without loading the whole PDF.
            with path.open("rb") as stream:
                head = stream.read(4096)
                stream.seek(max(0, path.stat().st_size - 4096))
                tail = stream.read()
            if response.status == 304:
                head, tail = body[:4096], body[-4096:]
            ext = inspect_body(head + tail, mime, source)
            if ext == "html":
                raise ValueError(
                    "Expected attachment returned HTML; inspect landing page separately"
                )
            metadata["size"] = len(body) if body is not None else path.stat().st_size
            if body is not None:
                artifact = self.store.save(body, ext)
            else:
                from .store import file_hash

                digest = file_hash(path)
                artifact = f"objects/{digest}.{ext}"
                target = self.store.root / artifact
                if not target.exists() or file_hash(target) != digest:
                    path.replace(target)
            metadata["sha256"] = Path(artifact).stem
            self.store.update(
                self.run_id, source, state="collected", metadata=metadata, artifact=artifact
            )
            self.store.remember(source, artifact, metadata)
            return
        ext = inspect_body(body, mime, response.url)
        if ext != "html":
            # An extensionless endpoint may be a document; don't archive it during discovery.
            self.store.update(
                self.run_id,
                source,
                kind="attachment",
                state="pending",
                metadata=metadata,
                reason="extensionless document; needs review",
            )
            return
        if not isinstance(response, HtmlResponse):
            response = HtmlResponse(
                response.url, body=body, encoding="utf-8", request=response.request
            )
        title = response.css("h1::text").get() or response.css("title::text").get() or ""
        main = response.css("main, article, .region-content")
        text = " ".join(
            (main or response)
            .xpath(
                ".//text()[not(ancestor::footer or ancestor::nav or ancestor::script or ancestor::style)]"
            )
            .getall()
        )
        metadata.update(
            {
                "title": title.strip(),
                "language": language(title, response.css("html::attr(lang)").get()),
                "date": response.css("time::attr(datetime)").get(),
                "document_status": next(
                    (s for s in ("draft", "amended", "superseded") if s in title.lower()), None
                ),
                "board": metadata.get("board"),
                "watershed": metadata.get("watershed"),
                "canonical": response.css('link[rel="canonical"]::attr(href)').get(),
                "encoding": response.encoding,
                "size": len(body),
            }
        )
        seed = self.seeds.get(source)
        if seed and (
            seed.get("expected", "").lower() not in title.lower()
            or (
                urlsplit(response.url).path.rstrip("/") in {"", "/en", "/fr"}
                and urlsplit(source).path.rstrip("/") not in {"", "/en", "/fr"}
            )
        ):
            metadata["health_error"] = "Seed title/redirect mismatch"
        if (
            seed
            and seed.get("listing")
            and not response.css(".views-row, article, .view-content a")
        ):
            metadata["health_error"] = "Expected listing structure missing"
        digest = hashlib.sha256(body).hexdigest()
        repeated = digest in self.page_hashes and "page=" in source
        self.page_hashes[digest] = source
        if repeated:
            metadata["health_error"] = "Repeated pagination content"
        decision = classify(title, text[:3000], source)
        if (seed and seed.get("listing")) or title.strip().lower() in {
            "publications",
            "reports",
            "completed boards",
            "boards, studies, and committee",
        }:
            decision.decision, decision.reason = "exclude", "discovery directory"
        # Generic footer copyright is deliberately not considered document-specific evidence.
        for alternate in response.css('link[rel="alternate"][hreflang]'):
            alt_url = normalize(alternate.attrib.get("href", ""), source)
            alt_lang = alternate.attrib.get("hreflang", "").split("-")[0]
            if alt_url and alt_url != source and alt_lang in {"en", "fr"}:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO equivalents VALUES (?,?,?,?)",
                    (self.run_id, source, alt_url, alt_lang),
                )
        self.store.db.commit()
        decision_language = metadata["language"]
        if decision.decision == "include" and decision_language == "fr":
            decision.decision, decision.reason = (
                "needs_review",
                "French equivalence requires review",
            )
        review = self.store.review(source)
        if review:
            decision.decision, decision.reason = review["decision"], review["evidence"]
            metadata["rights_evidence"] = review["evidence"]
            metadata["language"] = review["language"] or metadata["language"]
        metadata["category"] = decision.category
        metadata["rights_status"] = decision.rights
        metadata["corpus_decision"] = decision.decision
        artifact = self.store.save(body, "html", snapshot=True)
        metadata["sha256"] = digest
        if row["kind"] == "policy":
            decision.decision, decision.reason = "exclude", "source terms snapshot"
        self.store.update(
            self.run_id,
            source,
            state="discovered",
            decision=decision.decision,
            reason=decision.reason,
            metadata=metadata,
            artifact=artifact,
        )
        self.store.remember(source, artifact, metadata)
        if self.preflight or repeated or row["kind"] == "policy":
            return
        for anchor in response.css("a[href], link[rel='alternate'][href]"):
            href = anchor.attrib["href"]
            label = " ".join(anchor.css("::text").getall()).strip()
            child_meta = dict(metadata)
            if anchor.attrib.get("hreflang"):
                child_meta["alternate_language"] = anchor.attrib["hreflang"]
            yield from self.discover_link(href, source, label, child_meta)
        # Sitemaps are discovered from saved robots evidence, never guessed file names.
        evidence = self.store.root / "runs" / self.run_id / "access"
        for robots in evidence.glob("robots-*.txt"):
            for sitemap in re.findall(
                r"(?im)^sitemap:\s*(\S+)", robots.read_text(errors="replace")
            ):
                yield from self.discover_link(sitemap, source, "sitemap", metadata, sitemap=True)

    def discover_link(self, href, parent, label, parent_meta, sitemap=False):
        url = normalize(href, parent)
        if not url:
            return
        kind = "sitemap" if sitemap else link_kind(url)
        if kind == "asset":
            return
        split = urlsplit(url)
        external = split.hostname not in self.cfg.allowed_hosts or split.scheme != "https"
        excluded = any(split.path.startswith(prefix) for prefix in self.cfg.exclude_paths)
        # Only real page-number pagination, never combinatorial facet/search URLs.
        from urllib.parse import parse_qsl

        facet = (
            bool(split.query)
            and any(k not in {"page"} for k, _ in parse_qsl(split.query))
            and kind == "page"
        )
        context = f"{parent_meta.get('title', '')} {parent}"
        decision = classify(label, context, "attachment" if kind == "attachment" else parent)
        if (
            kind == "attachment"
            and decision.reason == "insufficient document type/context"
            and parent_meta.get("corpus_decision") == "include"
            and parent_meta.get("category")
        ):
            decision.decision = "include"
            decision.category = parent_meta["category"]
            decision.reason = "Attachment explicitly linked by an eligible document page"
        meta = {
            "title": label or None,
            "parent": parent,
            "seed": parent_meta.get("seed"),
            "category": decision.category,
            "language": language(label + " " + split.path),
            "board": parent_meta.get("board"),
            "watershed": parent_meta.get("watershed"),
            "date": None,
            "document_status": None,
        }
        if "/watersheds/" in urlsplit(parent).path:
            meta["watershed"] = parent_meta.get("title")
        if "board" in (parent_meta.get("title") or "").lower():
            meta["board"] = parent_meta.get("title")
        if kind == "attachment" and meta["language"] == "fr" and decision.decision == "include":
            decision.decision, decision.reason = (
                "needs_review",
                "Attachment language/equivalence unresolved",
            )
        if kind == "attachment" and parent_meta.get("rights_status") == "unresolved":
            decision.decision, decision.reason = (
                "needs_review",
                "Source document has explicit restrictions",
            )
            meta["rights_status"] = "unresolved"
        existing = self.store.db.execute(
            "SELECT 1 FROM urls WHERE run=? AND url=?", (self.run_id, url)
        ).fetchone()
        self.store.add(
            self.run_id, url, kind, parent, label, meta, decision.decision, decision.reason
        )
        if existing:
            return
        if external or excluded or facet or kind == "unsupported":
            reason = (
                "external link"
                if external
                else "unsupported format"
                if kind == "unsupported"
                else "excluded discovery path/query"
            )
            self.store.update(
                self.run_id,
                url,
                state="unsupported" if external or kind == "unsupported" else "excluded",
                decision="exclude",
                reason=reason,
            )
            return
        if kind in {"page", "sitemap"}:
            yield self.request(url)

    def idle(self):
        if self.halted or not self.collect_mode or self.collection_started:
            return
        policy = self.row(self.cfg.policy_url)
        if policy["state"] != "discovered":
            self.halted = True
            return
        self.collection_started = True
        scheduled = False
        for row in self.store.rows(self.run_id):
            if row["kind"] == "policy":
                continue
            meta = json.loads(row["metadata"])
            review = self.store.review(row["url"])
            decision = review["decision"] if review else row["decision"]
            if not review and meta.get("language") == "fr":
                equivalents = self.store.db.execute(
                    "SELECT alternate FROM equivalents WHERE run=? AND source=? AND language='en'",
                    (self.run_id, row["url"]),
                ).fetchall()
                for alternate in equivalents:
                    match = self.store.db.execute(
                        "SELECT decision,state,metadata FROM urls WHERE run=? AND url=?",
                        (self.run_id, alternate["alternate"]),
                    ).fetchone()
                    if (
                        match
                        and match["decision"] == "include"
                        and match["state"] in {"discovered", "collected"}
                    ):
                        decision = "exclude"
                        self.store.update(
                            self.run_id,
                            row["url"],
                            decision="exclude",
                            reason="Published English equivalent available",
                        )
                        break
            if review:
                meta["rights_evidence"] = review["evidence"]
                meta["language"] = review["language"] or meta.get("language")
                self.store.update(
                    self.run_id,
                    row["url"],
                    decision=decision,
                    reason=review["evidence"],
                    metadata=meta,
                )
            if decision != "include" or meta.get("health_error"):
                continue
            if row["state"] == "discovered" and self.store.valid(row["artifact"]):
                artifact = self.store.save((self.store.root / row["artifact"]).read_bytes(), "html")
                self.store.update(self.run_id, row["url"], state="collected", artifact=artifact)
            elif row["kind"] == "attachment" and row["state"] == "pending":
                self.crawler.engine.crawl(self.request(row["url"]))
                scheduled = True
        if scheduled:
            raise DontCloseSpider

    def closed_run(self, reason):
        status = "blocked" if self.halted else "interrupted" if reason != "finished" else "finished"
        self.store.db.execute("UPDATE runs SET status=? WHERE id=?", (status, self.run_id))
        self.store.db.commit()
