"""Deterministic discovery and corpus rules; no model or document text extraction."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid"}
SUPPORTED = {".pdf", ".txt", ".html", ".htm"}
UNSUPPORTED = {".doc", ".docx", ".xls", ".xlsx", ".zip", ".ppt", ".pptx", ".csv"}
ASSETS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".mp4", ".ico"}
CATEGORIES = {
    "treaty": r"treaty|traité",
    "agreement": r"agreement|accord",
    "rule": r"rules of procedure|règles de procédure",
    "guidance": r"guidance|guiding principles|lignes directrices",
    "application": r"application|demande d’approbation",
    "reference": r"reference|renvoi",
    "order": r"order of approval|orders?\b|ordonnance",
    "directive": r"directive|mandate|mandat",
    "plan": r"plan of study|study plan|plan d’étude|plan d'étude",
    "recommendation": r"recommendation|recommandation",
    "report": r"report|rapport",
}
EXCLUDED = re.compile(
    r"news release|communiqué|newsletter|bulletin|meeting minutes|procès.verbal|"
    r"public comments|commentaires du public|biography|biographie|"
    r"gate schedule|outflow update|water levels update|calendar",
    re.I,
)
RESTRICTED = re.compile(
    r"all rights reserved|permission (?:is )?required|reproduction prohibited|"
    r"tous droits réservés|reproduction interdite",
    re.I,
)


def normalize(url: str, base: str = "") -> str | None:
    p = urlsplit(urljoin(base, url))
    if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password:
        return None
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING
    ]
    return urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path or "/", urlencode(sorted(query)), "")
    )


def suffix(url: str) -> str:
    match = re.search(r"\.[a-z0-9]+$", urlsplit(url).path.lower())
    return match.group() if match else ""


def link_kind(url: str) -> str:
    ext = suffix(url)
    if ext in ASSETS:
        return "asset"
    if ext in UNSUPPORTED:
        return "unsupported"
    if ext in {".pdf", ".txt"} or "/sites/default/files/" in url:
        return "attachment"
    return "page"


def language(text: str, html_lang: str | None = None) -> str | None:
    # Never use the parent page's language to identify an attachment.
    if re.search(r"bilingual|bilingue|eng[-_ ]?fr|english.*français", text, re.I):
        return "bilingual"
    if html_lang and html_lang.lower().split("-")[0] in {"en", "fr"}:
        return html_lang.lower().split("-")[0]
    if re.search(r"\bfrançais\b|\bfrench\b|[_-]fr(?:[_.-]|$)", text, re.I):
        return "fr"
    if re.search(r"\benglish\b|[_-]en(?:[_.-]|$)", text, re.I):
        return "en"
    return None


@dataclass
class Decision:
    decision: str
    reason: str
    category: str | None = None
    rights: str = "source_policy"


def classify(title: str, context: str = "", document_type: str = "") -> Decision:
    combined = f"{title} {context} {document_type}"
    if RESTRICTED.search(combined):
        return Decision("needs_review", "explicit restriction", rights="unresolved")
    if EXCLUDED.search(title) or re.search(
        r"news.release|newsletter|/calendar|/events|/maps|/people|/comments", document_type, re.I
    ):
        return Decision("exclude", "non-corpus page; follow discovery links")
    category = next(
        (k for k, pattern in CATEGORIES.items() if re.search(pattern, title, re.I)), None
    )
    # Require independent publication/mandate context in addition to a title keyword.
    substantive = re.search(
        r"publication|report|rapport|mandate|mission|directive|docket|reference|"
        r"/who/|watershed|board|conseil|commission|treaty|traité",
        context + document_type,
        re.I,
    )
    if category and substantive:
        return Decision("include", "document title and source context", category)
    return Decision("needs_review", "insufficient document type/context", category)


def inspect_body(body: bytes, content_type: str, url: str) -> str:
    head = body[:4096].lstrip().lower()
    if not body:
        raise ValueError("empty response")
    if body.startswith(b"%PDF-"):
        if b"%%EOF" not in body[-4096:]:
            raise ValueError("PDF missing end marker")
        return "pdf"
    html = b"<html" in head or b"<!doctype html" in head or "text/html" in content_type
    if html:
        if suffix(url) == ".pdf" or "application/pdf" in content_type:
            raise ValueError("HTML masquerading as PDF")
        if any(x in head for x in (b"just a moment", b"verify you are human", b"access denied")):
            raise ValueError("access challenge/error page")
        headings = re.findall(rb"<(?:title|h1)[^>]*>(.*?)</(?:title|h1)>", head, re.DOTALL)
        if any(
            re.sub(rb"<[^>]+>", b"", heading)
            .strip()
            .startswith(
                (b"404", b"page not found", b"not found", b"403 forbidden", b"500 internal")
            )
            for heading in headings
        ):
            raise ValueError("HTTP 200 error page")
        return "html"
    if "text/plain" in content_type and b"\x00" not in head:
        return "txt"
    raise ValueError("unsupported or mismatched response type")
