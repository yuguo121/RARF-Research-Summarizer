from __future__ import annotations

from pathlib import Path

import yaml

from rarf_summarizer.paths import configured_schema_path
from rarf_summarizer.schema import Schema, apply_profile, load_schema

PARALLEL_CAPS = (5, 10, 50, 100)
PARALLEL_OPTIONS = [{"id": cap, "label": f"<{cap}"} for cap in PARALLEL_CAPS]


def normalize_parallel_sessions(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 5
    for cap in PARALLEL_CAPS:
        if number <= cap:
            return cap
    return PARALLEL_CAPS[-1]


def parallel_workers(cap: int) -> int:
    """Exclusive upper bound: <5 means at most 4 concurrent sessions."""
    return max(1, normalize_parallel_sessions(cap) - 1)


def profile_path(project_root: Path) -> Path:
    return Path(project_root) / "data" / "dimension_profile.yaml"


def load_profile(project_root: Path) -> dict:
    path = profile_path(project_root)
    if not path.is_file():
        schema = load_schema(configured_schema_path(Path(project_root)))
        return {
            "enabled": list(schema.field_ids),
            "instructions": {},
            "backend": "local",
            "parallel_sessions": 5,
        }
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    enabled = raw.get("enabled")
    instructions = raw.get("instructions") or {}
    if not isinstance(instructions, dict):
        instructions = {}
    backend = str(raw.get("backend") or "local").strip().casefold()
    if backend not in {"local", "external"}:
        backend = "local"
    return {
        "enabled": list(enabled) if enabled else None,
        "instructions": {str(key): str(value) for key, value in instructions.items()},
        "backend": backend,
        "parallel_sessions": normalize_parallel_sessions(raw.get("parallel_sessions")),
    }


def save_profile(
    project_root: Path,
    enabled: list[str] | None,
    instructions: dict[str, str] | None,
    backend: str | None = None,
    parallel_sessions: int | None = None,
) -> Path:
    path = profile_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    current: dict = {}
    if path.is_file():
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    chosen = backend if backend is not None else current.get("backend") or "local"
    chosen = str(chosen).strip().casefold()
    if chosen not in {"local", "external"}:
        chosen = "local"
    payload = {
        "enabled": list(enabled) if enabled is not None else None,
        "instructions": instructions or {},
        "backend": chosen,
        "parallel_sessions": normalize_parallel_sessions(
            parallel_sessions if parallel_sessions is not None else current.get("parallel_sessions")
        ),
    }
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def schema_from_profile(base: Schema, profile: dict) -> Schema:
    return apply_profile(base, profile.get("enabled"), profile.get("instructions"))
