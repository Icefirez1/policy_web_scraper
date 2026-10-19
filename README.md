# IJC raw policy document scraper

Collect original PDF, HTML and plain-text governance documents from approved IJC
hosts. Discovery follows public pages more broadly than the final corpus: for
example, a news release can lead to an eligible report. Documents are never sent
to an LLM, cleaned, converted, OCRed, or embedded.

For component responsibilities, workflow diagrams, state transitions, and the
full output/data flow, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Install and configure

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run the
same commands in Windows PowerShell or a Linux shell from this directory:

```sh
uv sync --locked --group dev
uv run policy-scraper --help
```

The project selects Python 3.12 through `.python-version`; `uv.lock` pins the
environment. Python 3.13 is also supported. Dependencies and the Python runtime
require internet access during initial installation.

Edit `config/ijc.yaml`:

- `contact`: your actual project contact email, included in the user agent.
- `collection_basis`: your documented basis for private research collection,
  including an institutional determination or permission reference where relevant.
- `allowed_hosts`: exact approved IJC hostnames. HTTPS and port 443 are required.
- `max_requests`: HTTP budget per invocation, including robots, redirects and
  retries; initially 100 for the pilot. Increase only after inspecting the pilot.
- `output`: local storage directory, relative to the working directory.

Do not treat the IJC site as uniformly public domain. Review its
[published terms](https://www.ijc.org/en/policies) and record the applicable basis.
The software detects some explicit restriction wording; it cannot determine
copyright status, recognize every notice inside PDFs, or provide legal clearance.
Rights uncertainty is not resolved by a relevance approval. Keep the archive
private; redistribution and future model use need separate consideration.

## Workflow

```sh
uv run policy-scraper --config config/ijc.yaml preflight
uv run policy-scraper --config config/ijc.yaml discover
# Inspect data/runs/<run-id>/report.json and review_required.jsonl.
uv run policy-scraper --config config/ijc.yaml review import decisions.jsonl
uv run policy-scraper --config config/ijc.yaml collect --run-id <discovery-run-id>
uv run policy-scraper --config config/ijc.yaml audit --run-id <run-id>
```

Preflight retrieves robots, terms, and seed pages; it does not traverse their
links. Start a **new** discovery run afterward. `collect` without a run ID performs
discovery first and then collects eligible documents. `discover` fetches HTML
and sitemaps but does not schedule known PDF/text attachments. An extensionless
URL may reveal itself as a document only after the HTTP response; it is recorded
for review, not added to the corpus during discovery.

A seed returning a homepage or unexpected title is a coverage gap. Some configured
IJC library links have exhibited this behavior. Inspect their current navigation
and add verified replacement seeds; do not delete failed seeds simply to make
the report appear complete. Both active and archived boards are included.

The first live collection is a pilot with the default 100-request budget. Inspect
every inclusion and a sample of exclusions across foundational documents, active
boards, archived boards, and a working publication listing. If the budget expires
before attachments are reached, resume the same run; each invocation has its own
budget, so record cumulative request counts in `attempts.jsonl`. Do not describe
multiple such invocations as a single 100-request pilot. To enlarge the configured
budget or change scope, start a new run.

## Review decisions and language preference

Review files use one JSON object per line:

```json
{"url":"https://www.ijc.org/example-report","decision":"include","language":"en","evidence":"Board report; English confirmed on the document page; collection covered by the recorded research basis."}
```

Use normalized URLs from `review_required.jsonl`; valid decisions are `include`
and `exclude`. Evidence is mandatory. Language may be `en`, `fr`, `bilingual`, or
null. Import validates the entire file before committing any decisions. Reviews
are reused across runs; revisit them when the source or permissions change.

English HTML counterparts are preferred only when explicitly linked with
`hreflang` and successfully discovered as eligible documents. French pages with
uncertain equivalence and French attachments await review. Approve French-only
documents with evidence that no corresponding English version was found.
Unknown attachment language stays null rather than inheriting the landing page's
language. Bilingual PDFs remain intact. Unsupported Word/Excel/ZIP formats and
external documents are reported rather than converted or fetched.

## Resume, update, and recovery

```sh
# Resume interrupted work with the same configuration:
uv run policy-scraper --config config/ijc.yaml collect --run-id <run-id>
# A later snapshot revisits sources and uses valid HTTP validators:
uv run policy-scraper --config config/ijc.yaml collect
```

SQLite is the authoritative frontier. Resume requeues unfinished and failed work;
new runs revisit sources. A configuration change requires a new run. Conditional
requests are sent only if a prior artifact still passes its SHA-256 check.
Changed documents retain earlier versions. Identical bytes share one content
object, without merging distinct source URLs or titles.

Do not run multiple scraper processes against the same output directory. Back up
the entire output directory with the scraper stopped, including its database.
After an abrupt termination, temporary download files may remain; audits report
them for inspection. They are never treated as complete artifacts. Resuming
redownloads unfinished transfers and reconciles finalized content by hash.

## Outputs

```text
data/
  ledger.sqlite3          # runs, URLs, decisions, edges, attempts, latest versions
  objects/<sha256>.*      # immutable corpus bytes
  snapshots/<sha256>.*    # discovery HTML, terms, sitemaps
  runs/<run-id>/
    access/              # robots evidence and transient streamed downloads
    manifest.jsonl       # one source/version record per collected document
    inventory.csv        # convenient document inventory
    candidates.jsonl     # complete URL ledger, including disposition
    edges.jsonl          # links establishing discovery provenance
    attempts.jsonl       # every actual HTTP attempt
    review_required.jsonl
    excluded.jsonl
    issues.jsonl
    report.json          # coverage, integrity and reproducibility metadata
```

Manifest metadata includes source/final URL, title, language, type, stated date
and status, board/watershed when inferable, parent, retrieval time, HTTP validators,
hash, size, storage path, rights basis, and run/version IDs. Missing values remain
null. Coverage is measured against discovered surfaces, not all historical IJC
records. Run provenance includes the config, code revision when available, source
hash, rule version, and dependency lock hash.

Exit codes: `0` complete without reported gaps; `2` complete with gaps, interrupted,
or blocked; `1` invalid configuration or command failure. An exhausted frontier
does not erase failed listings, unresolved reviews, or inaccessible sources.

## Network behavior

Outbound HTTPS must be permitted by your computer, proxy, institution, and runtime.
The crawler respects standard HTTP proxy and certificate environment settings;
TLS verification is always enabled. Sandbox permission is separate from website
permission. The program does not change firewall settings or rotate proxies.

All requests use a shared limiter (minimum two seconds by default), with robots
checks for each origin and redirect target. Robots `404`/`410` means no published
policy; unresolved robots responses pause the origin. Denials stop access; bounded
retries honor `Retry-After`, with delays exceeding 60 seconds pausing the origin
for a later invocation. Neither access nor avoidance of blocking is guaranteed.

## Tests

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Tests use synthetic fixtures, mocked HTTP failure cases, and a real localhost HTTP
server for complete Scrapy discovery/collection/update cycles. They do not contact
the IJC. A test-only transport maps approved HTTPS fixture URLs to localhost;
production requests retain HTTPS and exact-host enforcement.
