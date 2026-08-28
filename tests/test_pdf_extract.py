from __future__ import annotations

import os
from pathlib import Path

import pytest

from rarf_summarizer.pdf_pipeline import extract_paper

# Point RARF_EXEMPLAR_PDF at a real review-article PDF to enable the extraction test.
_exemplar = os.environ.get("RARF_EXEMPLAR_PDF", "")
KRAUSE = Path(_exemplar) if _exemplar else Path("__missing__")


@pytest.mark.skipif(not KRAUSE.exists(), reason="exemplar PDF is not available")
def test_extract_exemplar_review_article():
    paper = extract_paper(KRAUSE)
    assert paper.page_count >= 20
    assert paper.doi and paper.doi.startswith("10.1177/")
    blob = " ".join(page.clean_text for page in paper.pages[:4])
    assert "CEO duality" in blob
    packet = paper.packet(("abstract", "introduction", "theory", "discussion", "conclusion"), 20000)
    assert "duality" in packet.casefold()


@pytest.mark.skipif(os.environ.get("RARF_LIVE_TEST") != "1", reason="opt-in live Cursor SDK test")
def test_live_summarize_requires_api_key():
    assert os.environ.get("CURSOR_API_KEY"), "CURSOR_API_KEY is required for the live test"
    from rarf_summarizer.pipeline import Pipeline

    pipeline = Pipeline()
    folder = KRAUSE.parent.parent
    ids = pipeline.summarize_folder(folder, force=True, limit=1)
    assert ids
    path = pipeline.export()
    assert path.exists()
