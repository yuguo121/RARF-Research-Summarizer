from __future__ import annotations

from rarf_summarizer.quotes import normalize_text, verify_quote


def test_verbatim_quote_matches_page():
    pages = {
        2: "CEO duality is far too complex to be considered dichotomously, with dual CEOs viewed as wielding unchecked power."
    }
    quote = "CEO duality is far too complex to be considered dichotomously"
    result = verify_quote(quote, pages, page=2)
    assert result["matched"] is True
    assert result["score"] >= 0.92


def test_hyphenation_and_whitespace_are_normalized():
    assert "doubleedged" in normalize_text("double-\nedged sword").replace(" ", "")
    pages = {1: "This is a double-\nedged sword in governance research."}
    result = verify_quote("This is a double-edged sword in governance research.", pages, page=1)
    assert result["matched"] is True


def test_paraphrase_does_not_count_as_quote():
    pages = {1: "Boards should remain independent from management to limit entrenchment."}
    result = verify_quote("Independence always raises profits in every industry.", pages, page=1)
    assert result["matched"] is False
