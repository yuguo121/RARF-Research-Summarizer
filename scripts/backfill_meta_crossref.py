"""Backfill journal metadata (publication, authors, year, volume, issue, pages)
for stored papers that have a DOI, using the CrossRef API.

Usage:  python scripts/backfill_meta_crossref.py [--db data/rarf.sqlite] [--mailto you@example.com]
Only empty fields are filled; existing values are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request


def crossref_work(doi: str, mailto: str, timeout: float = 20) -> dict | None:
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi) + "?mailto=" + urllib.parse.quote(mailto)
    req = urllib.request.Request(url, headers={"User-Agent": f"rarf-summarizer (mailto:{mailto})"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("message") or {}
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/rarf.sqlite")
    parser.add_argument("--mailto", default="rarf-desk@localhost")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, doi, publication, authors, year, volume, issue, pages FROM papers "
        "WHERE doi IS NOT NULL AND doi <> '' AND (publication IS NULL OR publication = '')"
    ).fetchall()
    print(f"{len(rows)} papers missing journal metadata; querying CrossRef…")
    updated = failed = 0
    for index, row in enumerate(rows, 1):
        msg = crossref_work(row["doi"], args.mailto)
        if not msg:
            failed += 1
            print(f"[{index}/{len(rows)}] FAIL {row['doi']}")
            continue
        container = msg.get("container-title") or []
        authors = "; ".join(
            " ".join(p for p in [a.get("given"), a.get("family")] if p).strip()
            for a in msg.get("author") or []
        )
        issued = msg.get("issued", {}).get("date-parts") or [[None]]
        updates = {
            "publication": container[0] if container else None,
            "authors": authors or None,
            "year": str(issued[0][0]) if issued[0][0] else None,
            "volume": msg.get("volume"),
            "issue": msg.get("issue"),
            "pages": msg.get("page"),
        }
        sets, params = [], []
        for col, val in updates.items():
            if val and not row[col]:
                sets.append(f"{col} = ?")
                params.append(str(val))
        if sets:
            params.append(row["id"])
            conn.execute(f"UPDATE papers SET {', '.join(sets)} WHERE id = ?", params)
            updated += 1
        if index % 25 == 0:
            conn.commit()
            print(f"[{index}/{len(rows)}] updated={updated} failed={failed}")
        time.sleep(0.05)
    conn.commit()
    print(f"done: updated={updated} failed={failed} of {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
