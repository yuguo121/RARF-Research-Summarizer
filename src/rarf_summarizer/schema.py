from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from rarf_summarizer.paths import CONFIG_DIR, load_yaml

SCHEMA_VERSION = "1.0.0"
PROMPT_VERSION = "1.1.0"


@dataclass(frozen=True)
class FieldSpec:
    id: str
    label: str
    group: str
    session: str
    value_kind: str
    instruction: str


@dataclass(frozen=True)
class GroupSpec:
    id: str
    label: str
    color: str = ""
    soft: str = ""


@dataclass(frozen=True)
class Schema:
    version: str
    prompt_version: str
    fields: tuple[FieldSpec, ...]
    status_values: tuple[str, ...]
    framing_primary_basis: tuple[str, ...]
    framing_secondary_style: tuple[str, ...]
    variable_classes: tuple[str, ...]
    measure_types: tuple[str, ...]
    sessions: dict
    name: str = "Review Form"
    name_short: str = ""
    groups: tuple[GroupSpec, ...] = ()

    def field(self, field_id: str) -> FieldSpec:
        for spec in self.fields:
            if spec.id == field_id:
                return spec
        raise KeyError(field_id)

    def fields_for_session(self, session: str) -> tuple[FieldSpec, ...]:
        return tuple(spec for spec in self.fields if spec.session == session)

    def has(self, field_id: str) -> bool:
        return field_id in self.field_ids

    @property
    def field_ids(self) -> tuple[str, ...]:
        return tuple(spec.id for spec in self.fields)

    def as_dict(self) -> list[dict]:
        return [
            {
                "id": spec.id,
                "label": spec.label,
                "group": spec.group,
                "session": spec.session,
                "value_kind": spec.value_kind,
                "instruction": spec.instruction,
            }
            for spec in self.fields
        ]

    def groups_as_dict(self) -> list[dict]:
        """Groups in declaration order, extended with any group used by fields but not declared."""
        declared = list(self.groups)
        seen = {g.id for g in declared}
        for spec in self.fields:
            if spec.group not in seen:
                seen.add(spec.group)
                declared.append(GroupSpec(id=spec.group, label=spec.group.replace("_", " ").title()))
        return [
            {"id": g.id, "label": g.label, "color": g.color, "soft": g.soft}
            for g in declared
        ]


def load_schema(path: Path | None = None) -> Schema:
    raw = load_yaml(path or CONFIG_DIR / "rarf_schema.yaml")
    fields = tuple(
        FieldSpec(
            id=item["id"],
            label=item["label"],
            group=item["group"],
            session=item["session"],
            value_kind=item["value_kind"],
            instruction=str(item.get("instruction", "")).strip(),
        )
        for item in raw["fields"]
    )
    groups = tuple(
        GroupSpec(
            id=str(item["id"]),
            label=str(item.get("label") or str(item["id"]).replace("_", " ").title()),
            color=str(item.get("color") or ""),
            soft=str(item.get("soft") or ""),
        )
        for item in (raw.get("groups") or [])
    )
    return Schema(
        version=str(raw.get("schema_version", SCHEMA_VERSION)),
        prompt_version=str(raw.get("prompt_version", PROMPT_VERSION)),
        fields=fields,
        status_values=tuple(raw.get("status_values", ())),
        framing_primary_basis=tuple(raw.get("framing_primary_basis", ())),
        framing_secondary_style=tuple(raw.get("framing_secondary_style", ())),
        variable_classes=tuple(raw.get("variable_classes", ())),
        measure_types=tuple(raw.get("measure_types", ())),
        sessions=raw.get("sessions") or {},
        name=str(raw.get("name") or "Review Form"),
        name_short=str(raw.get("name_short") or ""),
        groups=groups,
    )


def apply_profile(
    schema: Schema,
    enabled: list[str] | None = None,
    instructions: dict[str, str] | None = None,
) -> Schema:
    """Return a schema limited to selected fields, with optional instruction edits."""
    allowed = None if enabled is None else {item for item in enabled if item}
    overrides = instructions or {}
    fields = []
    for spec in schema.fields:
        if allowed is not None and spec.id not in allowed:
            continue
        custom = overrides.get(spec.id)
        if custom is not None and str(custom).strip():
            spec = replace(spec, instruction=str(custom).strip())
        fields.append(spec)
    fingerprint = hashlib.sha256(
        "|".join(f"{spec.id}={spec.instruction}" for spec in fields).encode("utf-8")
    ).hexdigest()[:12]
    return Schema(
        version=schema.version,
        prompt_version=f"{schema.prompt_version}+{fingerprint}",
        fields=tuple(fields),
        status_values=schema.status_values,
        framing_primary_basis=schema.framing_primary_basis,
        framing_secondary_style=schema.framing_secondary_style,
        variable_classes=schema.variable_classes,
        measure_types=schema.measure_types,
        sessions=schema.sessions,
        name=schema.name,
        name_short=schema.name_short,
        groups=schema.groups,
    )
