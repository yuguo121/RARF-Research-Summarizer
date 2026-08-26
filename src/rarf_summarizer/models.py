from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Status = Literal["present", "not_reported", "not_applicable", "unclear"]
PrimaryBasis = Literal["IV-led", "DV-led", "theory-led", "mixed/other"]
SecondaryStyle = Literal["theoretical", "phenomenological", "mixed", "not_reported"]
VariableClass = Literal["DV", "IV", "moderator", "mediator", "control"]
MeasureType = Literal["continuous", "binary", "ordinal", "cardinal"]


class EvidenceItem(BaseModel):
    page: int | None = None
    quote: str = ""

    @field_validator("quote")
    @classmethod
    def strip_quote(cls, value: str) -> str:
        return value.strip()


class Envelope(BaseModel):
    status: Status = "present"
    confidence: float = Field(default=0.5, ge=0, le=1)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    value: Any = None


class FramingValue(BaseModel):
    primary_basis: PrimaryBasis
    secondary_style: SecondaryStyle = "not_reported"
    rationale: str = ""


class ArgumentRecord(BaseModel):
    quote: str
    page: int | None = None
    academic_paraphrase: str = ""
    plain_language: str = ""
    causal_formulation: str | None = None

    @field_validator("quote", "academic_paraphrase", "plain_language")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def quote_required_when_present(self) -> "ArgumentRecord":
        if len(self.quote) < 12:
            raise ValueError("key argument quote must be an exact passage from the paper")
        return self


class ConstructRecord(BaseModel):
    construct_id: str = ""
    class_: VariableClass = Field(alias="class")
    name: str
    nominal_definition: str = ""

    model_config = {"populate_by_name": True}


class MeasureRecord(BaseModel):
    construct_id: str = ""
    class_: VariableClass | Literal["control"] | str = Field(alias="class")
    name: str
    operationalization: str = ""
    range: str = ""
    type: MeasureType | str = "continuous"
    linked_construct: str = ""

    model_config = {"populate_by_name": True}


def slug_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug[:56] or "construct"


def coerce_envelope(raw: Any) -> Envelope:
    if raw is None:
        return Envelope(status="not_reported", value=None)
    if isinstance(raw, str):
        status: Status = "not_reported" if not raw.strip() else "present"
        return Envelope(status=status, value=raw.strip() or None)
    if isinstance(raw, Envelope):
        return raw
    if isinstance(raw, dict):
        payload = dict(raw)
        if "status" not in payload and payload.get("value") in {
            "not_applicable",
            "not_reported",
            "unclear",
        }:
            payload["status"] = payload["value"]
        return Envelope.model_validate(payload)
    return Envelope(status="present", value=raw)
