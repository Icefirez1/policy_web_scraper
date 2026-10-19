import pytest

from policy_scraper.rules import classify, inspect_body, language, normalize


def test_normalization_preserves_meaning():
    assert (
        normalize("../Policy?b=2&utm_source=x&a=1#part", "https://ijc.org/en/a")
        == "https://ijc.org/Policy?a=1&b=2"
    )
    assert normalize("javascript:alert(1)") is None
    assert normalize("https://user:password@ijc.org/a") is None


def test_discovery_is_not_corpus_inclusion():
    assert classify("News release: New Report", "Commission", "/news-release").decision == "exclude"
    assert classify("Annual Report", "Board publications").decision == "include"
    assert classify("Report", "unrelated").decision == "needs_review"
    assert classify("Report", "Board; all rights reserved").rights == "unresolved"


def test_file_language_not_inferred_from_parent():
    assert language("document.pdf") is None
    assert language("report-fr.pdf") == "fr"
    assert language("Boundary-ENGFR.pdf") == "bilingual"


def test_format_validation():
    assert (
        inspect_body(b"%PDF-1.4\nexample\n%%EOF", "application/octet-stream", "https://ijc.org/a")
        == "pdf"
    )
    with pytest.raises(ValueError, match="masquerading"):
        inspect_body(b"<html>Error</html>", "text/html", "https://ijc.org/a.pdf")
    with pytest.raises(ValueError, match="end marker"):
        inspect_body(b"%PDF-1.4 broken", "application/pdf", "https://ijc.org/a.pdf")
    with pytest.raises(ValueError, match="challenge"):
        inspect_body(b"<html>Verify you are human</html>", "text/html", "https://ijc.org/a")
