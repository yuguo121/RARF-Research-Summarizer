from __future__ import annotations

from rarf_summarizer.pdf_pipeline import PageText, assign_sections, clean_page, detect_heading, detect_running_lines, dump_table_as_text


def test_heading_detection_and_section_routing():
    pages = [
        PageText(1, "Abstract\nWe review duality.\n", "Abstract\nWe review duality.", 20, False),
        PageText(2, "Introduction\nWhy duality?\n", "Introduction\nWhy duality?", 18, False),
        PageText(3, "Methods\nWe searched journals.\n", "Methods\nWe searched journals.", 22, False),
        PageText(4, "Results\nNo performance effect.\n", "Results\nNo performance effect.", 24, False),
        PageText(5, "Discussion\nMore nuance is needed.\n", "Discussion\nMore nuance is needed.", 24, False),
        PageText(6, "References\nDalton 1998.\n", "References\nDalton 1998.", 16, False),
    ]
    spans = assign_sections(pages)
    keys = [span.key for span in spans]
    assert "abstract" in keys
    assert "introduction" in keys
    assert "methods" in keys
    assert "results" in keys
    assert "discussion" in keys
    methods = next(span for span in spans if span.key == "methods")
    assert "searched journals" in methods.text
    assert methods.page_start == 3


def test_running_headers_are_stripped():
    pages = [
        "Journal of Management\nReal paragraph one.\nDownloaded from jom.sagepub.com",
        "Journal of Management\nReal paragraph two.\nDownloaded from jom.sagepub.com",
        "Journal of Management\nReal paragraph three.\nDownloaded from jom.sagepub.com",
    ]
    junk = detect_running_lines(pages)
    cleaned = clean_page(pages[0], junk)
    assert "Journal of Management" not in cleaned
    assert "Real paragraph one." in cleaned
    assert "Downloaded" not in cleaned


def test_detect_heading_aliases():
    assert detect_heading("Hypothesis Development")[0] == "theory"
    assert detect_heading("Empirical Strategy")[0] == "methods"
    assert detect_heading("This sentence mentions methods in passing.") is None


def test_dump_table_as_text_joins_cells():
    blob = dump_table_as_text([["N", "Duality"], ["48", "1 if CEO is chair"]])
    assert "N | Duality" in blob
    assert "48 | 1 if CEO is chair" in blob
