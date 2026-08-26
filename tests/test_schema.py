from __future__ import annotations

from rarf_summarizer.formatting import parse_field_value
from rarf_summarizer.models import FramingValue
from rarf_summarizer.schema import apply_profile, load_schema
import pytest


def test_apply_profile_filters_fields_and_overrides_instruction():
    schema = load_schema()
    slim = apply_profile(schema, ["citation", "findings"], {"citation": "Use the filename if DOI is missing."})
    assert slim.field_ids == ("citation", "findings")
    assert "filename" in slim.field("citation").instruction
    assert slim.prompt_version != schema.prompt_version


def test_schema_has_twenty_three_fields():
    schema = load_schema()
    assert len(schema.fields) == 23
    assert schema.field_ids[0] == "citation"
    assert schema.field_ids[-1] == "most_interesting"


def test_framing_classification_values():
    envelope = parse_field_value(
        "framing",
        {
            "status": "present",
            "value": {
                "primary_basis": "IV-led",
                "secondary_style": "theoretical",
                "rationale": "The paper is organized around CEO duality as the focal practice.",
            },
        },
    )
    assert isinstance(envelope.value, FramingValue)
    assert envelope.value.primary_basis == "IV-led"


def test_framing_rejects_unknown_basis():
    with pytest.raises(Exception):
        parse_field_value(
            "framing",
            {"status": "present", "value": {"primary_basis": "method-led", "rationale": "nope"}},
        )
