from __future__ import annotations

from pathlib import Path

from rarf_summarizer.cursor_runtime import FakeBackend, ModelNotAvailableError, resolve_grok_46_high
from rarf_summarizer.json_util import extract_json_object
from rarf_summarizer.pdf_pipeline import ExtractedPaper, PageText, SectionSpan, file_sha256
from rarf_summarizer.schema import apply_profile, load_schema
from rarf_summarizer.selection import collect_pdfs, id_root_for
from rarf_summarizer.storage import Store
from rarf_summarizer.summarizer import Summarizer, cache_key, paper_id_for
from tests.helpers import QUOTE, json_response, method_payload, reconcile_payload, theory_payload
import pytest


def _extracted(tmp_path: Path) -> ExtractedPaper:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    pages = []
    for index, block in enumerate(
        [
            "Abstract\nWe review the CEO duality literature.",
            "Introduction\n" + QUOTE,
            "Methods\nThis review includes 48 publications from management and finance journals.",
            "Results\nDalton et al. (1998) found no empirical link between CEO duality and firm performance.",
            "Discussion\nFuture research should treat duality as more than a dichotomy.",
        ],
        start=1,
    ):
        pages.append(PageText(index, block, block, len(block), False))
    sections = [
        SectionSpan("abstract", "Abstract", 1, 1, pages[0].clean_text),
        SectionSpan("introduction", "Introduction", 2, 2, pages[1].clean_text),
        SectionSpan("methods", "Methods", 3, 3, pages[2].clean_text),
        SectionSpan("results", "Results", 4, 4, pages[3].clean_text),
        SectionSpan("discussion", "Discussion", 5, 5, pages[4].clean_text),
    ]
    return ExtractedPaper(
        source_path=pdf,
        file_hash="hash-demo",
        title="CEO Duality",
        authors="Krause",
        doi="10.1177/0149206313503013",
        page_count=5,
        pages=pages,
        sections=sections,
    )


def test_cache_key_changes_with_hash_and_versions():
    schema = load_schema()
    a = cache_key("h1", schema, "cursor-grok-4.6-high", "theory")
    b = cache_key("h2", schema, "cursor-grok-4.6-high", "theory")
    c = cache_key("h1", schema, "other-model", "theory")
    assert a != b
    assert a != c
    assert a == cache_key("h1", schema, "cursor-grok-4.6-high", "theory")


def test_summarizer_skips_cached_sessions(tmp_path: Path):
    store = Store(tmp_path / "rarf.sqlite")
    schema = load_schema()
    backend = FakeBackend(
        {
            "theory": json_response(theory_payload()),
            "method": json_response(method_payload()),
            "reconcile": json_response(reconcile_payload()),
        }
    )
    summarizer = Summarizer(store, schema, backend, tmp_path / "work")
    paper = _extracted(tmp_path)
    paper_id = "demo-paper"
    summarizer.summarize_paper(paper_id, paper)
    first_calls = list(backend.calls)
    assert first_calls == ["theory", "method"]
    summarizer.summarize_paper(paper_id, paper)
    assert backend.calls == first_calls
    field = store.get_field(paper_id, "framing")
    assert field and "theory-led" in field["generated_text"]


def test_construct_measure_linking(tmp_path: Path):
    store = Store(tmp_path / "rarf.sqlite")
    schema = load_schema()
    method = method_payload()
    method["measures"] = {
        "status": "present",
        "confidence": 0.9,
        "value": [
            {
                "class": "IV",
                "name": "duality dummy",
                "linked_construct": "CEO duality",
                "operationalization": "1 if CEO is also chair",
                "range": "0-1",
                "type": "binary",
            }
        ],
        "evidence": [],
        "warnings": [],
    }
    backend = FakeBackend(
        {
            "theory": json_response(theory_payload()),
            "method": json_response(method),
        }
    )
    summarizer = Summarizer(store, schema, backend, tmp_path / "work")
    summarizer.summarize_paper("demo-paper", _extracted(tmp_path))
    constructs = store.list_constructs()
    measures = store.list_measures()
    assert constructs
    assert measures[0]["construct_id"] == next(row["construct_id"] for row in constructs if row["name"] == "CEO duality")


def test_startup_versus_run_failure():
    from rarf_summarizer.cursor_runtime import AgentRunError, AgentStartupError, FakeBackend

    backend = FakeBackend({})
    with pytest.raises(AgentRunError):
        backend.run("x", session="theory", work_dir=".")


def test_local_reconcile_skips_llm_when_quotes_match(tmp_path: Path):
    store = Store(tmp_path / "rarf.sqlite")
    schema = load_schema()
    backend = FakeBackend(
        {
            "theory": json_response(theory_payload()),
            "method": json_response(method_payload()),
        }
    )
    summarizer = Summarizer(store, schema, backend, tmp_path / "work")
    summarizer.summarize_paper("demo-paper", _extracted(tmp_path))
    assert backend.calls == ["theory", "method"]
    assert store.get_field("demo-paper", "sample")
    assert store.get_field("demo-paper", "framing")


def test_packet_keeps_theory_methods_results_and_tables_untruncated(tmp_path: Path):
    from rarf_summarizer.pdf_pipeline import METHOD_SECTIONS, THEORY_SECTIONS

    paper = _extracted(tmp_path)
    paper.sections.extend(
        [
            SectionSpan("theory", "Theory", 6, 6, "THEORYBODY " + ("agency framework " * 4000)),
            SectionSpan("tables", "Extracted tables", 4, 4, "[p.4]\nN | Duality | Performance\n48 | 1 | 0.02"),
        ]
    )
    theory = paper.packet(THEORY_SECTIONS, 200)
    method = paper.packet(METHOD_SECTIONS, 200)
    assert "We review the CEO duality literature" in theory
    assert "CEO duality is far too complex" in theory
    assert "THEORYBODY" in theory
    assert "agency framework" in theory
    assert "This review includes 48 publications" in method
    assert "no empirical link between CEO duality" in method
    assert "N | Duality | Performance" in method
    assert "[Section truncated.]" not in theory
    assert "[Section truncated.]" not in method
    assert "[Truncated remaining sections" not in theory
    assert "[Truncated remaining sections" not in method


def test_packet_includes_low_text_pages(tmp_path: Path):
    from rarf_summarizer.pdf_pipeline import THEORY_SECTIONS

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-fake")
    pages = [PageText(1, "RAWSCAN glyphs", "", 3, True), PageText(2, "more glyphs", "", 2, True)]
    paper = ExtractedPaper(
        source_path=pdf,
        file_hash="hash-scan",
        title="Scan",
        authors=None,
        doi=None,
        page_count=2,
        pages=pages,
        sections=[],
        warnings=["page 1 looks low-text or scanned"],
    )
    packet = paper.packet(THEORY_SECTIONS)
    assert "RAWSCAN glyphs" in packet
    assert "more glyphs" in packet


def test_extract_skips_when_hash_unchanged(tmp_path: Path, monkeypatch):
    from rarf_summarizer.pipeline import Pipeline

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake-bytes")
    extracted = _extracted(tmp_path)
    extracted.source_path = pdf
    extracted.file_hash = file_sha256(pdf)
    calls = {"n": 0}

    def fake_extract(path, low_text_char_threshold=80, **kwargs):
        calls["n"] += 1
        return extracted

    monkeypatch.setattr("rarf_summarizer.pipeline.extract_paper", fake_extract)
    pipeline = Pipeline()
    pipeline.store = Store(tmp_path / "rarf.sqlite")
    first = pipeline.extract_one(pdf, tmp_path)
    second = pipeline.extract_one(pdf, tmp_path)
    assert first == second
    assert calls["n"] == 1
    forced = pipeline.extract_one(pdf, tmp_path, force=True)
    assert forced == first
    assert calls["n"] == 2


def test_json_extract_from_fenced_block():
    data = extract_json_object("Sure.\n```json\n{\"a\": 1}\n```\n")
    assert data == {"a": 1}


def test_json_extract_from_preamble_and_trailing_text():
    data = extract_json_object('thinking about files\n{"a": 1, "b": 2}\nnot json')
    assert data == {"a": 1, "b": 2}


def test_json_extract_salvages_first_complete_object():
    data = extract_json_object('{"citation": {"status": "present", "value": "x"}, "extra": 1} leftover {')
    assert data["citation"]["status"] == "present"


def test_paper_id_is_stable(tmp_path: Path):
    pdf = tmp_path / "Duality" / "Krause_2014.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"x")
    assert paper_id_for(pdf, tmp_path) == paper_id_for(pdf, tmp_path)
    assert file_sha256(pdf) == file_sha256(pdf)


def test_theory_only_profile_skips_method_session(tmp_path: Path):
    store = Store(tmp_path / "rarf.sqlite")
    schema = apply_profile(load_schema(), ["citation", "framing", "key_argument"])
    backend = FakeBackend({"theory": json_response(theory_payload())})
    summarizer = Summarizer(store, schema, backend, tmp_path / "work")
    summarizer.summarize_paper("demo-paper", _extracted(tmp_path))
    assert backend.calls == ["theory"]
    assert store.get_field("demo-paper", "citation")
    assert store.get_field("demo-paper", "sample") is None


def test_collect_pdfs_from_file_and_folder(tmp_path: Path):
    folder = tmp_path / "lib"
    nested = folder / "sub"
    nested.mkdir(parents=True)
    a = folder / "a.pdf"
    b = nested / "b.pdf"
    a.write_bytes(b"%PDF-a")
    b.write_bytes(b"%PDF-b")
    (folder / "notes.txt").write_text("nope")
    pdfs = collect_pdfs([a, folder])
    assert {p.name for p in pdfs} == {"a.pdf", "b.pdf"}
    assert id_root_for(pdfs, folder) == folder.resolve()


def test_resummarize_replaces_only_target_field(tmp_path: Path):
    store = Store(tmp_path / "rarf.sqlite")
    schema = load_schema()
    backend = FakeBackend(
        {
            "theory": json_response(theory_payload()),
            "method": json_response(method_payload()),
        }
    )
    summarizer = Summarizer(store, schema, backend, tmp_path / "work")
    paper = _extracted(tmp_path)
    summarizer.summarize_paper("demo-paper", paper)
    original_framing = store.get_field("demo-paper", "framing")["generated_text"]
    store.set_human_override("demo-paper", "citation", "human citation")
    replacement = theory_payload()
    replacement["citation"] = {
        "status": "present",
        "confidence": 0.9,
        "value": "REPLACED CITATION",
        "evidence": [],
        "warnings": [],
    }
    backend.responses["resummarize:theory"] = json_response(replacement)
    updated = summarizer.resummarize_fields(
        "demo-paper",
        paper,
        ["citation"],
        extra_instruction="use the filename",
    )
    assert "citation" in updated
    citation = store.get_field("demo-paper", "citation")
    assert "REPLACED CITATION" in citation["generated_text"]
    assert not citation.get("human_text")
    assert citation["source"] == "generated"
    assert store.get_field("demo-paper", "framing")["generated_text"] == original_framing
    assert store.get_field("demo-paper", "sample")
    assert "resummarize:theory" in backend.calls


def test_parallel_session_caps():
    from rarf_summarizer.dimension_profile import normalize_parallel_sessions, parallel_workers

    assert normalize_parallel_sessions(3) == 5
    assert normalize_parallel_sessions(10) == 10
    assert parallel_workers(5) == 4
    assert parallel_workers(10) == 9
    assert parallel_workers(50) == 49
    assert parallel_workers(100) == 99


def test_summarize_runs_papers_in_parallel(tmp_path: Path, monkeypatch):
    import threading
    import time

    from rarf_summarizer.pipeline import Pipeline

    monkeypatch.setattr(
        "rarf_summarizer.pipeline.load_profile",
        lambda root: {
            "backend": "external",
            "parallel_sessions": 10,
            "enabled": None,
            "instructions": {},
        },
    )
    current = 0
    peak = 0
    lock = threading.Lock()

    class CountingBackend(FakeBackend):
        def run(self, prompt: str, *, session: str, work_dir: str):
            nonlocal current, peak
            with lock:
                current += 1
                peak = max(peak, current)
            time.sleep(0.12)
            try:
                return super().run(prompt, session=session, work_dir=work_dir)
            finally:
                with lock:
                    current -= 1

    folder = tmp_path / "lib"
    folder.mkdir()
    pdfs = []
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        pdf = folder / name
        pdf.write_bytes(b"%PDF-" + name.encode())
        pdfs.append(pdf)

    def fake_extract(path, low_text_char_threshold=80, **kwargs):
        paper = _extracted(tmp_path)
        paper.source_path = path
        paper.file_hash = file_sha256(path)
        paper.title = path.stem
        return paper

    monkeypatch.setattr("rarf_summarizer.pipeline.extract_paper", fake_extract)
    backend = CountingBackend(
        {
            "theory": json_response(theory_payload()),
            "method": json_response(method_payload()),
        }
    )
    pipeline = Pipeline(backend=backend)
    pipeline.store = Store(tmp_path / "rarf.sqlite")
    pipeline.work_dir = tmp_path / "work"
    ids = pipeline.summarize_pdfs(pdfs, folder)
    assert len(ids) == 3
    assert peak > 1
    for paper_id in ids:
        assert pipeline.store.get_field(paper_id, "citation")
        assert pipeline.store.get_paper(paper_id)["status"] == "summarized"


def test_reconcile_failure_still_persists_sessions(tmp_path: Path):
    store = Store(tmp_path / "rarf.sqlite")
    schema = load_schema()
    backend = FakeBackend(
        {
            "theory": json_response(theory_payload()),
            "method": json_response(method_payload()),
        }
    )
    summarizer = Summarizer(store, schema, backend, tmp_path / "work", skip_reconcile_if_clean=False)
    summarizer.summarize_paper("demo-paper", _extracted(tmp_path))
    assert store.get_field("demo-paper", "citation")
    assert store.get_field("demo-paper", "sample")
    assert "reconcile" in backend.calls
    import json as jsonlib
    row = store.get_field("demo-paper", "research_question")
    blob = jsonlib.loads(row["generated_json"])
    assert any("reconcile failed" in w or "no fake response" in w for w in blob.get("warnings", []))


def test_method_failure_persists_theory(tmp_path: Path):
    from rarf_summarizer.cursor_runtime import AgentRunError

    store = Store(tmp_path / "rarf.sqlite")
    schema = load_schema()
    backend = FakeBackend({"theory": json_response(theory_payload())})
    summarizer = Summarizer(store, schema, backend, tmp_path / "work")
    with pytest.raises(AgentRunError):
        summarizer.summarize_paper("demo-paper", _extracted(tmp_path))
    citation = store.get_field("demo-paper", "citation")
    assert citation and citation.get("generated_text")
    sample = store.get_field("demo-paper", "sample")
    assert sample is None or sample.get("status") == "not_reported"
