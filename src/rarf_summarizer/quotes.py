from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Iterable


_HYPHEN_RE = re.compile(r"(\w)-\s+(\w)")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[“”„‟«»]")


def normalize_text(text: str) -> str:
    text = _PUNCT_RE.sub('"', text or "")
    text = text.replace("\u00ad", "")
    text = _HYPHEN_RE.sub(r"\1\2", text)
    text = _WS_RE.sub(" ", text)
    return text.strip().casefold()


def page_window(pages: dict[int, str], page: int | None, radius: int = 1) -> str:
    if not pages:
        return ""
    if page is None:
        return " ".join(pages[n] for n in sorted(pages))
    numbers = [n for n in range(page - radius, page + radius + 1) if n in pages]
    if not numbers:
        return " ".join(pages[n] for n in sorted(pages))
    return " ".join(pages[n] for n in numbers)


def quote_in_text(quote: str, text: str, threshold: float = 0.92) -> tuple[bool, float]:
    needle = normalize_text(quote)
    haystack = normalize_text(text)
    if not needle or not haystack:
        return False, 0.0
    if needle in haystack:
        return True, 1.0
    # PDF extraction often drops spaces around line breaks; try a compressed form.
    compact_needle = needle.replace(" ", "")
    compact_hay = haystack.replace(" ", "")
    if compact_needle and compact_needle in compact_hay:
        return True, 0.97
    if len(needle) < 24:
        ratio = SequenceMatcher(None, needle, haystack[: min(len(haystack), 800)]).ratio()
        return ratio >= threshold, ratio
    window = max(len(needle) - 8, 24)
    best = 0.0
    step = max(12, window // 6)
    for start in range(0, max(len(haystack) - window, 0) + 1, step):
        chunk = haystack[start : start + len(needle) + 40]
        best = max(best, SequenceMatcher(None, needle, chunk).ratio())
        if best >= threshold:
            return True, best
    return best >= threshold, best


def verify_quote(
    quote: str,
    pages: dict[int, str],
    page: int | None = None,
    threshold: float = 0.92,
) -> dict:
    text = page_window(pages, page)
    matched, score = quote_in_text(quote, text, threshold=threshold)
    if not matched:
        matched, score = quote_in_text(quote, " ".join(pages[n] for n in sorted(pages)), threshold=threshold)
        location = "full_text" if matched else "unmatched"
    else:
        location = f"page_{page}" if page is not None else "window"
    return {
        "matched": matched,
        "score": round(score, 3),
        "location": location,
        "quote": quote,
        "page": page,
    }


def verify_quotes(items: Iterable[dict], pages: dict[int, str]) -> list[dict]:
    results = []
    for item in items:
        results.append(verify_quote(item.get("quote") or "", pages, item.get("page")))
    return results
