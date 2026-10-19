"""One conservative HTTP transport for Scrapy and preflight.

Every hop, retry and robots fetch shares a rate limiter. Bodies stream to disk;
Scrapy receives only HTML/XML bodies in memory. Attachments stay on disk.
"""

import email.utils
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from protego import Protego

from .config import Config
from .rules import normalize


class AccessError(Exception):
    """An origin cannot be accessed under the current policy."""


class BudgetExceeded(AccessError):
    pass


@dataclass
class Download:
    url: str
    status: int
    headers: dict
    path: Path
    redirects: list[str]


class Transport:
    def __init__(self, cfg: Config, evidence: Path, attempt=None):
        self.cfg = cfg
        self.evidence = evidence
        evidence.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(
            headers={"User-Agent": cfg.identity},
            timeout=cfg.timeout_seconds,
            follow_redirects=False,
        )
        self.robots: dict[str, tuple[float, Protego, float]] = {}
        self.paused: set[str] = set()
        self.last_request = 0.0
        self.count = 0
        self.attempt = attempt or (lambda *args: None)

    def close(self):
        self.client.close()

    def check_url(self, url: str):
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or p.hostname not in self.cfg.allowed_hosts
            or p.port not in {None, 443}
        ):
            raise AccessError(f"URL outside HTTPS host allowlist: {url}")
        if p.username or p.password:
            raise AccessError("Credentials in URL are prohibited")
        if self.origin(url) in self.paused:
            raise AccessError("Origin paused after access/robots failure")

    @staticmethod
    def origin(url: str) -> str:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def _once(self, url: str, headers: dict, delay: float) -> Download:
        if self.count >= self.cfg.max_requests:
            raise BudgetExceeded("Network request budget exhausted (including robots and retries)")
        time.sleep(max(0, self.last_request + delay - time.monotonic()))
        self.last_request = time.monotonic()
        self.count += 1
        fd, name = tempfile.mkstemp(dir=self.evidence, prefix="download-")
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as target:
                with self.client.stream("GET", url, headers=headers) as response:
                    length = response.headers.get("content-length")
                    if length and int(length) > self.cfg.max_bytes:
                        raise ValueError("Response exceeds configured size limit")
                    size = 0
                    for block in response.iter_bytes():
                        size += len(block)
                        if size > self.cfg.max_bytes:
                            raise ValueError("Decoded response exceeds configured size limit")
                        target.write(block)
                    target.flush()
                    os.fsync(target.fileno())
                    result = Download(
                        str(response.url), response.status_code, dict(response.headers), path, []
                    )
            self.attempt(url, result.status, "HTTP response")
            return result
        except Exception as error:
            path.unlink(missing_ok=True)
            self.attempt(url, None, f"{type(error).__name__}: {error}")
            raise

    @staticmethod
    def retry_delay(value: str | None, attempt: int) -> float:
        if value:
            try:
                return max(0, float(value))
            except ValueError:
                try:
                    return max(
                        0, email.utils.parsedate_to_datetime(value).timestamp() - time.time()
                    )
                except (ValueError, TypeError):
                    pass
        return min(60, 2 ** (attempt + 1))

    def _request(self, url: str, headers: dict, delay: float) -> Download:
        for attempt in range(self.cfg.retries + 1):
            try:
                response = self._once(url, headers, delay)
            except httpx.TransportError:
                if attempt == self.cfg.retries:
                    raise
                time.sleep(self.retry_delay(None, attempt))
                continue
            if response.status in {401, 403}:
                self.paused.add(self.origin(url))
                response.path.unlink(missing_ok=True)
                raise AccessError(f"Access denied: HTTP {response.status}")
            if response.status == 429 or response.status >= 500:
                wait = self.retry_delay(response.headers.get("retry-after"), attempt)
                response.path.unlink(missing_ok=True)
                if attempt == self.cfg.retries or wait > 60:
                    self.paused.add(self.origin(url))
                    raise AccessError(f"Origin paused: HTTP {response.status}; retry after {wait}s")
                time.sleep(wait)
                continue
            return response
        raise AssertionError("Unreachable retry state")

    def robot_policy(self, url: str) -> tuple[Protego, float]:
        origin = self.origin(url)
        cached = self.robots.get(origin)
        if cached and time.time() - cached[0] < self.cfg.robots_ttl_seconds:
            return cached[1], cached[2]
        target = origin + "/robots.txt"
        try:
            # Robots redirects may be followed only within configured origins.
            for _ in range(6):
                self.check_url(target)
                response = self._request(target, {}, self.cfg.delay_seconds)
                if response.status in {301, 302, 303, 307, 308}:
                    response.path.unlink(missing_ok=True)
                    target = normalize(response.headers.get("location", ""), target)
                    if not target:
                        raise AccessError("Invalid robots redirect")
                    continue
                break
            else:
                raise AccessError("Robots redirect limit exceeded")
            data = response.path.read_bytes()
            response.path.unlink(missing_ok=True)
            key = hashlib.sha256(origin.encode()).hexdigest()
            (self.evidence / f"robots-{key}-{int(time.time())}.txt").write_bytes(data)
            (self.evidence / f"robots-{key}.json").write_text(
                json.dumps(
                    {
                        "origin": origin,
                        "url": target,
                        "status": response.status,
                        "at": time.time(),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            if response.status in {404, 410}:
                lines = []
            elif response.status == 200 and not data.lstrip().lower().startswith(
                (b"<!doctype", b"<html")
            ):
                lines = data.decode("utf-8-sig", errors="replace").splitlines()
            else:
                raise AccessError(f"Unusable robots policy: HTTP {response.status}")
            # Scrapy's Protego implements wildcard and longest-match semantics.
            parser = Protego.parse("\n".join(lines))
            agent = self.cfg.user_agent.split("/")[0]
            robot_delay = float(parser.crawl_delay(agent) or 0)
            delay = max(self.cfg.delay_seconds, robot_delay)
            self.robots[origin] = (time.time(), parser, delay)
            return parser, delay
        except Exception:
            self.paused.add(origin)
            raise

    def fetch(self, url: str, headers: dict | None = None) -> Download:
        redirects = []
        for _ in range(11):
            self.check_url(url)
            parser, delay = self.robot_policy(url)
            if not parser.can_fetch(url, self.cfg.user_agent.split("/")[0]):
                raise AccessError("Disallowed by robots.txt")
            response = self._request(url, headers or {}, delay)
            if response.status not in {301, 302, 303, 307, 308}:
                response.redirects = redirects
                return response
            response.path.unlink(missing_ok=True)
            redirects.append(url)
            target = normalize(response.headers.get("location", ""), url)
            if not target or target in redirects:
                raise AccessError("Invalid or looping redirect")
            if self.origin(target) != self.origin(url):
                headers = {}  # Validators belong to the original origin.
            url = target
        raise AccessError("Redirect limit exceeded")
