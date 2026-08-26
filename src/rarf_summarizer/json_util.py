from __future__ import annotations

import json
import re
from typing import Any


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*)", re.DOTALL | re.IGNORECASE)


class JsonExtractError(ValueError):
    pass


def try_extract_json_object(text: str) -> dict[str, Any] | None:
    try:
        return extract_json_object(text)
    except JsonExtractError:
        return None


def first_complete_object(text: str) -> dict[str, Any] | None:
    """Parse the first complete JSON object, ignoring trailing text.

    Used while streaming: success means the outer object has closed.
    """
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) and obj else None


def extract_json_object(text: str) -> dict[str, Any]:
    if not text or not str(text).strip():
        raise JsonExtractError("empty model response")
    blob = str(text)
    fenced = _FENCE_RE.search(blob)
    if fenced:
        inner = re.sub(r"\s*```\s*$", "", fenced.group(1))
        parsed = _decode_from_braces(inner)
        if parsed is not None:
            return parsed
    parsed = _decode_from_braces(blob)
    if parsed is not None:
        return parsed
    raise JsonExtractError("could not parse JSON object from model response")


def _decode_from_braces(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    starts = [index for index, char in enumerate(text) if char == "{"]
    for start in starts[:30]:
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj:
            return obj
    if not starts:
        return None
    start = starts[0]
    closes = [index for index, char in enumerate(text) if char == "}" and index > start]
    for end in reversed(closes[-80:]):
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj:
            return obj
    return None
