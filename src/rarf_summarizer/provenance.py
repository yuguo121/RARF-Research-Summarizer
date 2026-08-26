from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path


def new_scan_id(paths: list[Path] | None = None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = ""
    if paths:
        blob = "|".join(str(path) for path in paths[:50])
        digest = "-" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:6]
    return f"scan-{stamp}{digest}"
