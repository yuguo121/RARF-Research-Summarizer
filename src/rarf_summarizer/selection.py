from __future__ import annotations

import os
import string
from pathlib import Path

from rarf_summarizer.pdf_pipeline import discover_pdfs


def collect_pdfs(paths: list[str | Path]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            path = path.resolve()
        except OSError:
            continue
        candidates: list[Path] = []
        if path.is_file() and path.suffix.lower() == ".pdf":
            candidates = [path]
        elif path.is_dir():
            candidates = [item.resolve() for item in discover_pdfs(path)]
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)
    return found


def id_root_for(pdfs: list[Path], default_folder: Path) -> Path:
    if not pdfs:
        return Path(default_folder).resolve()
    default = Path(default_folder).resolve()
    resolved = [path.resolve() for path in pdfs]
    if all(_is_relative_to(path, default) for path in resolved):
        return default
    try:
        common = Path(os.path.commonpath([str(path) for path in resolved]))
    except ValueError:
        return resolved[0].parent
    return common.parent if common.is_file() else common


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def windows_drives() -> list[dict[str, str]]:
    if os.name != "nt":
        return [{"name": "/", "path": str(Path("/").resolve())}]
    drives = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:/")
        if drive.exists():
            drives.append({"name": f"{letter}:", "path": str(drive)})
    return drives


def list_directory(path: Path) -> dict:
    path = Path(path).expanduser()
    try:
        path = path.resolve()
    except OSError as exc:
        raise FileNotFoundError(str(exc)) from exc
    if path.is_file():
        path = path.parent
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError(str(path))
    dirs: list[dict] = []
    files: list[dict] = []
    try:
        entries = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
    except PermissionError:
        entries = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir():
                dirs.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "pdf_here": _immediate_pdf_count(entry),
                    }
                )
            elif entry.suffix.lower() == ".pdf":
                files.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "size": entry.stat().st_size,
                    }
                )
        except OSError:
            continue
    parent = None if path.parent == path else str(path.parent)
    return {
        "path": str(path),
        "parent": parent,
        "dirs": dirs,
        "files": files,
        "pdf_here": len(files),
    }


def _immediate_pdf_count(folder: Path) -> int:
    try:
        return sum(1 for item in folder.iterdir() if item.is_file() and item.suffix.lower() == ".pdf")
    except OSError:
        return 0
