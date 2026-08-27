from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
YEAR_RE = re.compile(r"(19|20)\d{2}")


@dataclass
class ZoteroMeta:
    title: str | None = None
    authors: str | None = None
    year: str | None = None
    doi: str | None = None
    publication: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    item_key: str | None = None
    source: str = "none"

    def citation(self) -> str | None:
        parts: list[str] = []
        if self.authors:
            parts.append(self.authors.rstrip("."))
        if self.year:
            parts.append(f"({self.year}).")
        if self.title:
            parts.append(self.title.rstrip("."))
        venue = self.publication or ""
        if self.volume:
            venue += f", {self.volume}"
            if self.issue:
                venue += f"({self.issue})"
        if self.pages:
            venue += f", {self.pages}"
        if venue:
            parts.append(venue.strip().lstrip(",").strip() + ".")
        if self.doi:
            parts.append(f"https://doi.org/{self.doi}")
        return " ".join(part for part in parts if part).strip() or None


def _clean_doi(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    match = DOI_RE.search(text)
    return match.group(0).rstrip(".") if match else None


def _clean_year(value: Any) -> str | None:
    if not value:
        return None
    match = YEAR_RE.search(str(value))
    return match.group(0) if match else None


def _authors_from_creators(creators: Iterable[dict] | None) -> str | None:
    names: list[str] = []
    for creator in creators or []:
        name = creator.get("name") or " ".join(
            part
            for part in [
                creator.get("firstName") or creator.get("given"),
                creator.get("lastName") or creator.get("family"),
            ]
            if part
        ).strip()
        if name:
            names.append(name)
    return "; ".join(names) or None


def _csl_issued_year(value: Any) -> str | None:
    """CSL JSON dates: {"issued": {"date-parts": [[2010, 5]]}}."""
    if isinstance(value, dict):
        parts = value.get("date-parts") or []
        if parts and parts[0]:
            return _clean_year(parts[0][0])
        return None
    return _clean_year(value)


def _authors_from_rdf(value: Any) -> str | None:
    if value is None:
        return None
    items = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name") or item.get("label") or item.get("value")
            if name:
                names.append(str(name))
        elif item:
            names.append(str(item))
    return "; ".join(names) or None


def _meta_from_data(data: dict, source: str, item_key: str | None = None) -> ZoteroMeta:
    return ZoteroMeta(
        title=data.get("title") or None,
        authors=(
            _authors_from_creators(data.get("creators"))
            or _authors_from_creators(data.get("author"))
            or _authors_from_rdf(data.get("authors"))
        ),
        year=_clean_year(data.get("date") or data.get("year")) or _csl_issued_year(data.get("issued")),
        doi=_clean_doi(data.get("DOI") or data.get("doi")),
        publication=data.get("publicationTitle") or data.get("container-title") or data.get("journal") or None,
        volume=str(data.get("volume")) if data.get("volume") else None,
        issue=str(data.get("issue")) if data.get("issue") else None,
        pages=str(data.get("pages") or data.get("page") or "") or None,
        item_key=item_key or data.get("key") or data.get("id") or None,
        source=source,
    )


def load_zotero_export(path: Path) -> list[ZoteroMeta]:
    """Parse a Zotero export file (JSON / CSL JSON / RDF-ish) into metadata rows."""
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _parse_rdf(text)
    rows: list[ZoteroMeta] = []
    items = data.get("items") if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = list(items.values())
    for item in items or []:
        if not isinstance(item, dict):
            continue
        payload = item.get("data") if isinstance(item.get("data"), dict) else item
        meta = _meta_from_data(payload, "export", item.get("key"))
        if meta.title or meta.doi:
            rows.append(meta)
    return rows


def _parse_rdf(text: str) -> list[ZoteroMeta]:
    rows: list[ZoteroMeta] = []
    for block in re.findall(r"<bib:Article[\s\S]*?</bib:Article>", text):
        def grab(tag: str) -> str | None:
            match = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", block)
            return re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else None

        meta = ZoteroMeta(
            title=grab("dc:title"),
            authors=_authors_from_rdf(re.findall(r"<foaf:surname>([\s\S]*?)</foaf:surname>", block)),
            year=_clean_year(grab("dc:date")),
            doi=_clean_doi(grab("dc:identifier")),
            publication=grab("prism:publicationName"),
            volume=grab("prism:volume"),
            pages=grab("bib:pages"),
            source="export",
        )
        if meta.title or meta.doi:
            rows.append(meta)
    return rows


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()


def _tokens(text: str | None) -> set[str]:
    return {token for token in _norm(text).split() if len(token) > 2}


def match_zotero_meta(pdf: Path, rows: list[ZoteroMeta]) -> ZoteroMeta | None:
    """Best-effort match of a PDF filename against exported Zotero items."""
    stem_tokens = _tokens(pdf.stem)
    if not stem_tokens:
        return None
    year = _clean_year(pdf.stem)
    best: tuple[float, ZoteroMeta] | None = None
    for row in rows:
        title_tokens = _tokens(row.title)
        if not title_tokens:
            continue
        overlap = len(stem_tokens & title_tokens) / max(1, len(title_tokens))
        author_hit = 0.0
        if row.authors:
            author_hit = 0.25 if any(
                token in stem_tokens for token in _tokens(row.authors.replace(";", " "))
            ) else 0.0
        year_hit = 0.15 if year and row.year == year else 0.0
        score = overlap + author_hit + year_hit
        if best is None or score > best[0]:
            best = (score, row)
    if best and best[0] >= 0.45:
        return best[1]
    return None


def fetch_zotero_api(
    library_id: str,
    *,
    api_key: str | None = None,
    library_type: str = "user",
    limit: int = 100,
    timeout: float = 20,
) -> list[ZoteroMeta]:
    """Pull top-level items from the Zotero Web API (no local DB access)."""
    base = f"https://api.zotero.org/{library_type}s/{library_id}/items/top"
    params = {"format": "json", "limit": str(limit), "start": "0"}
    headers = {"Zotero-API-Version": "3"}
    if api_key:
        headers["Zotero-API-Key"] = api_key
    rows: list[ZoteroMeta] = []
    while True:
        url = base + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            total = int(response.headers.get("Total-Results") or len(payload))
        for item in payload:
            data = item.get("data") if isinstance(item, dict) else None
            if isinstance(data, dict):
                meta = _meta_from_data(data, "api", item.get("key"))
                if meta.title or meta.doi:
                    rows.append(meta)
        if len(rows) >= total or not payload:
            break
        params["start"] = str(int(params["start"]) + len(payload))
    return rows


def merge_meta(base: ZoteroMeta, extra: ZoteroMeta | None) -> ZoteroMeta:
    if extra is None:
        return base
    for attr in ("title", "authors", "year", "doi", "publication", "volume", "issue", "pages", "item_key"):
        value = getattr(extra, attr)
        if value and not getattr(base, attr):
            setattr(base, attr, value)
    if extra.source != "none":
        base.source = extra.source
    return base
