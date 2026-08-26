from __future__ import annotations

import json
from typing import Any

from rarf_summarizer.models import (
    ArgumentRecord,
    ConstructRecord,
    Envelope,
    FramingValue,
    MeasureRecord,
    coerce_envelope,
    slug_id,
)


def effective_text(field_row: dict[str, Any] | None) -> str:
    if not field_row:
        return ""
    return (field_row.get("human_text") or field_row.get("generated_text") or "").strip()


def format_framing(value: Any) -> str:
    data = value
    if isinstance(value, Envelope):
        data = value.value
    if isinstance(data, FramingValue):
        data = data.model_dump()
    if not isinstance(data, dict):
        return str(data or "")
    lines = [
        f"Primary: {data.get('primary_basis') or 'unclear'}",
        f"Style: {data.get('secondary_style') or 'not_reported'}",
    ]
    if data.get("rationale"):
        lines.append(f"Rationale: {data['rationale']}")
    return "\n".join(lines)


def format_arguments(value: Any) -> str:
    try:
        records = _argument_records(value)
    except Exception:
        return str(value or "")
    blocks = []
    for index, record in enumerate(records, start=1):
        page = f" (p.{record.page})" if record.page else ""
        parts = [f"[{index}] Quote{page}: \"{record.quote}\""]
        if record.academic_paraphrase:
            parts.append(f"Academic: {record.academic_paraphrase}")
        if record.plain_language:
            parts.append(f"Plain: {record.plain_language}")
        if record.causal_formulation:
            parts.append(f"Causal/logical: {record.causal_formulation}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def format_constructs(value: Any) -> str:
    records = _construct_records(value)
    lines = []
    for record in records:
        definition = f": {record.nominal_definition}" if record.nominal_definition else ""
        lines.append(f"{record.class_} — {record.name}{definition}")
    return "\n".join(lines)


def format_measures(value: Any) -> str:
    records = _measure_records(value)
    lines = []
    for record in records:
        linked = record.linked_construct or record.construct_id
        bits = [f"{record.class_} — {record.name}"]
        if linked:
            bits.append(f"construct: {linked}")
        if record.operationalization:
            bits.append(record.operationalization)
        extras = []
        if record.range:
            extras.append(f"range {record.range}")
        if record.type:
            extras.append(str(record.type))
        if extras:
            bits.append("(" + "; ".join(extras) + ")")
        lines.append(" | ".join(bits))
    return "\n".join(lines)


def format_field(kind: str, envelope: Envelope) -> str:
    if envelope.status in {"not_reported", "not_applicable", "unclear"} and not envelope.value:
        return envelope.status
    value = envelope.value
    if kind == "framing":
        return format_framing(envelope)
    if kind == "arguments":
        return format_arguments(value)
    if kind == "constructs":
        return format_constructs(value)
    if kind == "measures":
        return format_measures(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value or envelope.status)


def parse_field_value(kind: str, raw: Any) -> Envelope:
    envelope = coerce_envelope(raw)
    if envelope.status != "present":
        return envelope
    if kind == "framing":
        payload = envelope.value if isinstance(envelope.value, dict) else raw
        if isinstance(payload, dict) and "primary_basis" not in payload and "value" in payload:
            payload = payload["value"]
        envelope.value = FramingValue.model_validate(payload)
    elif kind == "arguments":
        envelope.value = [item.model_dump() for item in _argument_records(envelope.value or raw)]
    elif kind == "constructs":
        records = _construct_records(envelope.value or raw)
        envelope.value = [item.model_dump(by_alias=True) for item in records]
    elif kind == "measures":
        records = _measure_records(envelope.value or raw)
        envelope.value = [item.model_dump(by_alias=True) for item in records]
    elif envelope.value is None and isinstance(raw, dict) and "value" not in raw:
        # Bare string-like objects already handled; keep dicts as value.
        envelope.value = raw.get("text") or raw
    return envelope


def _argument_records(value: Any) -> list[ArgumentRecord]:
    if isinstance(value, dict) and "arguments" in value:
        value = value["arguments"]
    if isinstance(value, dict) and "quote" in value:
        value = [value]
    if not isinstance(value, list):
        return []
    return [ArgumentRecord.model_validate(item) for item in value]


def _construct_records(value: Any) -> list[ConstructRecord]:
    if isinstance(value, dict) and "constructs" in value:
        value = value["constructs"]
    if not isinstance(value, list):
        return []
    records = []
    for item in value:
        record = ConstructRecord.model_validate(item)
        if not record.construct_id:
            record.construct_id = slug_id(record.name)
        records.append(record)
    return records


def _measure_records(value: Any) -> list[MeasureRecord]:
    if isinstance(value, dict) and "measures" in value:
        value = value["measures"]
    if not isinstance(value, list):
        return []
    records = []
    for item in value:
        record = MeasureRecord.model_validate(item)
        linked = record.linked_construct or record.name
        if not record.construct_id:
            record.construct_id = slug_id(linked)
        if not record.linked_construct:
            record.linked_construct = linked
        records.append(record)
    return records
