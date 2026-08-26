from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)

SECTION_ALIASES = {
    "abstract": "abstract",
    "introduction": "introduction",
    "overview": "introduction",
    "background": "introduction",
    "theory": "theory",
    "theoretical background": "theory",
    "theoretical framework": "theory",
    "literature review": "theory",
    "prior research": "theory",
    "hypotheses": "theory",
    "hypothesis development": "theory",
    "research agenda": "theory",
    "methods": "methods",
    "method": "methods",
    "methodology": "methods",
    "data": "methods",
    "sample": "methods",
    "measures": "methods",
    "measurement": "methods",
    "variables": "methods",
    "empirical strategy": "methods",
    "estimation strategy": "methods",
    "research design": "methods",
    "analysis": "methods",
    "results": "results",
    "findings": "results",
    "robustness": "results",
    "robustness checks": "results",
    "endogeneity": "results",
    "discussion": "discussion",
    "implications": "discussion",
    "limitations": "discussion",
    "conclusion": "conclusion",
    "conclusions": "conclusion",
    "concluding remarks": "conclusion",
    "appendix": "appendix",
    "appendices": "appendix",
    "online appendix": "appendix",
    "references": "references",
    "bibliography": "references",
    "notes": "notes",
    "acknowledgments": "notes",
    "acknowledgements": "notes",
}

HEADING_RE = re.compile(
    r"^\s*(?:[IVXLC]+|[A-Z]|\d+)?[.\-:) ]*\s*("
    + "|".join(re.escape(name) for name in sorted(SECTION_ALIASES, key=len, reverse=True))
    + r")\s*$",
    re.IGNORECASE,
)

THEORY_SECTIONS = ("abstract", "introduction", "theory", "discussion", "conclusion", "front_matter")
METHOD_SECTIONS = ("methods", "results", "tables", "appendix")
SKIP_SECTIONS = ("references", "notes")


@dataclass
class PageText:
    page_number: int
    raw_text: str
    clean_text: str
    char_count: int
    is_low_text: bool


@dataclass
class SectionSpan:
    key: str
    title: str
    page_start: int
    page_end: int
    text: str


@dataclass
class ExtractedPaper:
    source_path: Path
    file_hash: str
    title: str | None
    authors: str | None
    doi: str | None
    page_count: int
    pages: list[PageText]
    sections: list[SectionSpan]
    warnings: list[str] = field(default_factory=list)

    def page_map(self) -> dict[int, str]:
        return {page.page_number: page.clean_text or page.raw_text for page in self.pages}

    def packet(self, keys: tuple[str, ...], budget: int | None = None) -> str:
        wanted = set(keys)
        if "front_matter" in wanted and any(section.key == "abstract" for section in self.sections):
            wanted.discard("front_matter")
        selected = [section for section in self.sections if section.key in wanted]
        if not selected:
            selected = [section for section in self.sections if section.key not in SKIP_SECTIONS]
        selected = sorted(selected, key=lambda section: (section.page_start, section.page_end, section.key))
        chunks: list[str] = []
        for section in selected:
            header = f"\n\n## {section.title} (pp. {section.page_start}-{section.page_end})\n"
            body = (section.text or "").strip()
            chunks.append(header + body)
        if chunks:
            return "".join(chunks).strip()
        parts = []
        for page in self.pages:
            body = (page.clean_text or page.raw_text or "").strip()
            parts.append(f"[p.{page.page_number}]\n{body}")
        return "\n\n".join(parts).strip()


def extracted_paper_from_rows(
    source_path: Path,
    record: dict,
    pages: list[dict],
    sections: list[dict],
) -> ExtractedPaper:
    page_objs = [
        PageText(
            page_number=int(row["page_number"]),
            raw_text=row.get("clean_text") or "",
            clean_text=row.get("clean_text") or "",
            char_count=int(row.get("char_count") or 0),
            is_low_text=bool(row.get("is_low_text")),
        )
        for row in pages
    ]
    section_objs = [
        SectionSpan(
            key=row.get("section_key") or "unknown",
            title=row.get("title") or "",
            page_start=int(row.get("page_start") or 1),
            page_end=int(row.get("page_end") or 1),
            text=row.get("text") or "",
        )
        for row in sections
    ]
    warnings: list[str] = []
    raw_warnings = record.get("warnings")
    if raw_warnings:
        try:
            parsed = json.loads(raw_warnings) if isinstance(raw_warnings, str) else raw_warnings
            if isinstance(parsed, list):
                warnings = [str(item) for item in parsed]
        except Exception:
            warnings = [str(raw_warnings)]
    return ExtractedPaper(
        source_path=source_path,
        file_hash=str(record.get("file_hash") or ""),
        title=record.get("title"),
        authors=record.get("authors"),
        doi=record.get("doi"),
        page_count=int(record.get("page_count") or len(page_objs)),
        pages=page_objs,
        sections=section_objs,
        warnings=warnings,
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_pdfs(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*.pdf") if path.is_file())


def dump_table_as_text(table: list[list[str | None]]) -> str:
    lines: list[str] = []
    for row in table or []:
        cells = [" ".join(str(cell or "").split()) for cell in row]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _dump_tables(path: Path) -> list[SectionSpan]:
    import pdfplumber

    chunks: list[str] = []
    first_page: int | None = None
    last_page: int | None = None
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            try:
                tables = page.extract_tables() or []
            except Exception:
                continue
            for table in tables:
                body = dump_table_as_text(table)
                if not body.strip():
                    continue
                chunks.append(f"[p.{index}]\n{body}")
                first_page = first_page or index
                last_page = index
    if not chunks:
        return []
    return [
        SectionSpan(
            key="tables",
            title="Extracted tables",
            page_start=first_page or 1,
            page_end=last_page or first_page or 1,
            text="\n\n".join(chunks),
        )
    ]


def extract_table_spans(path: Path, timeout: float = 20) -> list[SectionSpan]:
    box: queue.Queue[tuple[str, object]] = queue.Queue()

    def worker() -> None:
        try:
            box.put(("ok", _dump_tables(path)))
        except Exception as exc:
            box.put(("err", exc))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return []
    try:
        kind, value = box.get_nowait()
    except queue.Empty:
        return []
    if kind == "err":
        return []
    return list(value or [])


def _extract_pages_pdfplumber(path: Path) -> list[str]:
    import pdfplumber

    texts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            texts.append(page.extract_text() or "")
    return texts


def _extract_pages_pypdf(path: Path) -> list[str]:
    reader = PdfReader(str(path))
    texts = []
    for page in reader.pages:
        texts.append(page.extract_text() or "")
    return texts


def _pdf_metadata(path: Path) -> dict[str, str | None]:
    try:
        meta = PdfReader(str(path)).metadata or {}
    except Exception:
        return {"title": None, "author": None}
    title = getattr(meta, "title", None) or (meta.get("/Title") if hasattr(meta, "get") else None)
    author = getattr(meta, "author", None) or (meta.get("/Author") if hasattr(meta, "get") else None)
    return {"title": title, "author": author}


def detect_running_lines(pages: list[str], min_fraction: float = 0.4) -> set[str]:
    samples: list[str] = []
    for text in pages:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            continue
        if len(lines) <= 4:
            samples.append(lines[0])
            if lines[-1] != lines[0]:
                samples.append(lines[-1])
        else:
            samples.extend(lines[:2])
            samples.extend(lines[-2:])
    if not pages:
        return set()
    threshold = max(2, int(min_fraction * len(pages)))
    counts = Counter(samples)
    junk = set()
    for line, count in counts.items():
        if count >= threshold and len(line) < 90:
            junk.add(line)
        if re.search(r"downloaded from", line, re.IGNORECASE):
            junk.add(line)
    return junk


def clean_page(text: str, junk: set[str]) -> str:
    kept = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in junk:
            continue
        if re.fullmatch(r"\d+", stripped):
            continue
        if re.search(r"downloaded from", stripped, re.IGNORECASE):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def detect_heading(line: str) -> tuple[str, str] | None:
    stripped = re.sub(r"\s+", " ", line).strip()
    match = HEADING_RE.match(stripped)
    if not match:
        lowered = stripped.casefold().strip(" :")
        if lowered in SECTION_ALIASES:
            key = SECTION_ALIASES[lowered]
            return key, stripped
        return None
    key = SECTION_ALIASES[match.group(1).casefold()]
    return key, stripped


def assign_sections(pages: list[PageText]) -> list[SectionSpan]:
    current_key = "front_matter"
    current_title = "Front matter"
    start_page = pages[0].page_number if pages else 1
    buckets: dict[tuple[str, str, int], list[str]] = {}
    order: list[tuple[str, str, int]] = []

    def ensure(key: str, title: str, page: int) -> tuple[str, str, int]:
        token = (key, title, page)
        if token not in buckets:
            buckets[token] = []
            order.append(token)
        return token

    token = ensure(current_key, current_title, start_page)
    for page in pages:
        lines = page.clean_text.splitlines() or [page.clean_text]
        page_chunks: list[str] = [f"[p.{page.page_number}]"]
        for line in lines:
            heading = detect_heading(line)
            if heading:
                key, title = heading
                if key != current_key:
                    if page_chunks:
                        buckets[token].extend(page_chunks)
                        page_chunks = []
                    current_key, current_title = key, title
                    token = ensure(current_key, current_title, page.page_number)
                    continue
            page_chunks.append(line)
        buckets[token].extend(page_chunks)

    spans: list[SectionSpan] = []
    for key, title, page_start in order:
        text = "\n".join(buckets[(key, title, page_start)]).strip()
        page_hits = [int(n) for n in re.findall(r"\[p\.(\d+)\]", text)]
        page_end = max(page_hits) if page_hits else page_start
        spans.append(SectionSpan(key=key, title=title, page_start=page_start, page_end=page_end, text=text))
    return spans


def extract_doi(pages: list[str]) -> str | None:
    blob = "\n".join(pages[:3])
    match = DOI_RE.search(blob)
    return match.group(0).rstrip(".") if match else None


def extract_paper(path: Path, low_text_char_threshold: int = 80) -> ExtractedPaper:
    raw_pages = _extract_pages_pypdf(path)
    letters = sum(len(re.sub(r"\s+", "", page or "")) for page in raw_pages)
    if letters < max(400, low_text_char_threshold * max(len(raw_pages), 1)):
        try:
            plumber_pages = _extract_pages_pdfplumber(path)
            plumber_letters = sum(len(re.sub(r"\s+", "", page or "")) for page in plumber_pages)
            if plumber_letters > letters:
                raw_pages = plumber_pages
        except Exception:
            pass

    junk = detect_running_lines(raw_pages)
    pages: list[PageText] = []
    warnings: list[str] = []
    for index, raw in enumerate(raw_pages, start=1):
        clean = clean_page(raw, junk)
        char_count = len(re.sub(r"\s+", "", clean))
        is_low = char_count < low_text_char_threshold
        if is_low:
            warnings.append(f"page {index} looks low-text or scanned ({char_count} letters)")
        pages.append(
            PageText(
                page_number=index,
                raw_text=raw,
                clean_text=clean or raw,
                char_count=char_count,
                is_low_text=is_low,
            )
        )

    meta = _pdf_metadata(path)
    doi = extract_doi(raw_pages)
    sections = assign_sections(pages) if pages else []
    table_spans = extract_table_spans(path)
    if table_spans:
        sections.extend(table_spans)
    return ExtractedPaper(
        source_path=path,
        file_hash=file_sha256(path),
        title=meta.get("title"),
        authors=meta.get("author"),
        doi=doi,
        page_count=len(pages),
        pages=pages,
        sections=sections,
        warnings=warnings,
    )
