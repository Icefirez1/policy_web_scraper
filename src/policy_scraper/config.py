"""Validated, hashable crawl configuration."""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml


@dataclass
class Config:
    output: str = "data"
    contact: str = ""
    collection_basis: str = ""
    user_agent: str = "IJCPolicyResearch/0.1"
    allowed_hosts: list[str] = field(default_factory=lambda: ["ijc.org", "www.ijc.org"])
    delay_seconds: float = 2
    max_bytes: int = 250 * 1024 * 1024
    timeout_seconds: float = 60
    retries: int = 3
    robots_ttl_seconds: int = 86400
    max_requests: int = 100
    policy_url: str = "https://www.ijc.org/en/policies"
    seeds: list[dict] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "Config":
        value = cls(**yaml.safe_load(Path(path).read_text(encoding="utf-8")))
        if not value.seeds or not value.allowed_hosts:
            raise ValueError("At least one seed and allowed host are required")
        if any(host != "ijc.org" and not host.endswith(".ijc.org") for host in value.allowed_hosts):
            raise ValueError("Only explicitly registered IJC hosts are supported")
        for seed in value.seeds:
            if not all(
                isinstance(seed.get(key), str) and seed[key] for key in ("url", "name", "expected")
            ):
                raise ValueError("Every seed requires a URL, name and expected title")
        for url in [value.policy_url, *(s["url"] for s in value.seeds)]:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in value.allowed_hosts
                or parsed.port not in {None, 443}
                or parsed.username
                or parsed.password
            ):
                raise ValueError(f"Source URL outside the HTTPS host allowlist: {url}")
        if value.delay_seconds < 0 or value.max_bytes <= 0 or value.max_requests <= 0:
            raise ValueError("Invalid delay, byte limit, or request budget")
        if not 0 <= value.retries <= 10 or value.timeout_seconds <= 0:
            raise ValueError("Invalid timeout or retry count")
        if not 0 < value.robots_ttl_seconds <= 86400:
            raise ValueError("Robots TTL must be between 1 second and 24 hours")
        return value

    def validate_identity(self, collection: bool = False) -> None:
        if "@" not in self.contact or "\n" in self.contact or "\r" in self.contact:
            raise ValueError("Set a real contact email in the configuration before network use")
        if not self.user_agent.strip() or any(c in self.user_agent for c in "\r\n"):
            raise ValueError("A valid identifying user agent is required")
        if collection and not self.collection_basis.strip():
            raise ValueError("Document collection_basis before downloading the corpus")

    @property
    def identity(self) -> str:
        return f"{self.user_agent} (+mailto:{self.contact})"

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()
