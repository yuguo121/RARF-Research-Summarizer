from __future__ import annotations

import re
from pathlib import Path

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_dotenv(project_root: Path | None = None) -> None:
    import os

    path = (project_root or PROJECT_ROOT) / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def update_dotenv(project_root: Path, updates: dict[str, str]) -> Path:
    """Write or update key=value pairs in .env, preserving existing lines."""
    path = Path(project_root) / ".env"
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def load_settings(project_root: Path | None = None) -> dict:
    root = project_root or PROJECT_ROOT
    load_dotenv(root)
    data = load_yaml(root / "config" / "settings.yaml")
    data["_project_root"] = root
    return data


def resolve_under_root(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (root / path).resolve()


def provider_slug(base_url: str) -> str:
    """Derive a provider slug from an API base URL for env var naming (slug_API_KEY / slug_BASE_URL)."""
    host = re.sub(r"^https?://", "", str(base_url or "").strip()).split("/")[0].split(":")[0]
    parts = [p for p in host.split(".") if p]
    if parts and parts[0] in {"api", "open", "www", "platform", "gateway"} and len(parts) > 1:
        parts = parts[1:]
    while len(parts) > 1 and parts[-1] in {"com", "cn", "net", "org", "io", "ai", "dev", "co", "ac"}:
        parts = parts[:-1]
    slug = re.sub(r"[^A-Za-z0-9]+", "_", parts[0] if parts else "").strip("_").lower()
    if not slug or slug.replace("_", "").isdigit():
        return "local"
    return slug
