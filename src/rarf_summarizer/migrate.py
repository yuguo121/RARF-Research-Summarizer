from __future__ import annotations

from pathlib import Path

from rarf_summarizer.storage import Store
from rarf_summarizer.summarizer import paper_id_for

_STATUS_RANK = {"summarized": 0, "error": 1, "partial": 2, "extracted": 3, "library": 4}


def migrate_content_ids(store: Store) -> int:
    """One-time migration: merge duplicate papers (same file_hash) and re-key by content hash."""
    if store.get_meta("ids_content_hash") == "1":
        return 0
    papers = store.list_papers()
    by_hash: dict[str, list[dict]] = {}
    for paper in papers:
        file_hash = paper.get("file_hash") or ""
        if file_hash.startswith("zotero:"):
            continue
        by_hash.setdefault(file_hash, []).append(paper)
    moved = 0
    for file_hash, group in by_hash.items():
        if not file_hash:
            continue
        group.sort(
            key=lambda p: (
                _STATUS_RANK.get(str(p.get("status")), 5),
                -len(store.fields_for(p["id"])),
                str(p.get("extracted_at") or ""),
            )
        )
        keeper = group[0]
        new_id = paper_id_for(
            Path(str(keeper.get("source_path") or keeper.get("title") or "paper")),
            file_hash=file_hash,
        )
        if keeper["id"] != new_id:
            store.change_paper_id(keeper["id"], new_id)
            moved += 1
        for loser in group[1:]:
            store.merge_paper_into(loser["id"], new_id)
            moved += 1
    store.set_meta("ids_content_hash", "1")
    return moved
