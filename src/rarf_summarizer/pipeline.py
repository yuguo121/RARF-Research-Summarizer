from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rarf_summarizer.cursor_runtime import make_backend
from rarf_summarizer.dimension_profile import load_profile, parallel_workers
from rarf_summarizer.excel_export import export_workbook
from rarf_summarizer.excel_import import sync_back
from rarf_summarizer.paths import load_settings, resolve_under_root
from rarf_summarizer.pdf_pipeline import (
    discover_pdfs,
    extract_paper,
    extracted_paper_from_rows,
    file_sha256,
)
from rarf_summarizer.provenance import new_scan_id
from rarf_summarizer.migrate import migrate_content_ids
from rarf_summarizer.schema import Schema, apply_profile, load_schema
from rarf_summarizer.selection import collect_pdfs, id_root_for
from rarf_summarizer.storage import Store, utc_now
from rarf_summarizer.summarizer import Summarizer, paper_id_for, paper_id_for_zotero
from rarf_summarizer.zotero_meta import (
    ZoteroMeta,
    fetch_zotero_api,
    load_zotero_export,
    match_zotero_meta,
)


def _folder_label(pdf: Path, folder: Path) -> str:
    try:
        parent = pdf.parent.resolve().relative_to(folder.resolve())
    except ValueError:
        return pdf.parent.name
    return str(parent) if str(parent) != "." else "."


def _year_from_name(name: str) -> str | None:
    match = re.search(r"(19|20)\d{2}", name)
    return match.group(0) if match else None


class Pipeline:
    def __init__(self, project_root: Path | None = None, backend=None):
        self.settings = load_settings(project_root)
        self.root = Path(self.settings["_project_root"])
        self.schema = load_schema(self.root / "config" / "rarf_schema.yaml")
        self.store = Store(resolve_under_root(self.root, self.settings["sqlite_path"]))
        self.output_path = resolve_under_root(self.root, self.settings["output_path"])
        self.work_dir = resolve_under_root(self.root, self.settings.get("work_dir", "data/work"))
        self.default_folder = Path(self.settings["default_folder"])
        self.backend = backend
        self.packet_budget = int(self.settings.get("packet_char_budget") or 0)
        self.packet_warn_chars = int(self.settings.get("packet_warn_chars") or 0)
        self.low_text = int(self.settings.get("low_text_char_threshold", 80))
        self.skip_reconcile_if_clean = bool(self.settings.get("skip_reconcile_if_clean", True))
        self._zotero_rows: list[ZoteroMeta] | None = None
        self._zotero_failed = False
        moved = migrate_content_ids(self.store)
        if moved:
            print(f"merged/re-keyed {moved} duplicate paper row(s) by content hash")

    def active_backend_name(self) -> str:
        profile = load_profile(self.root)
        name = profile.get("backend") or (self.settings.get("model") or {}).get("backend") or "local"
        return str(name).strip().casefold()

    def _backend(self):
        return make_backend(self.settings, name=self.active_backend_name(), injected=self.backend)

    def _zotero_metadata(self) -> list[ZoteroMeta]:
        if self._zotero_rows is not None or self._zotero_failed:
            return self._zotero_rows or []
        import os

        zotero_cfg = self.settings.get("zotero") or {}
        rows: list[ZoteroMeta] = []
        export_path = zotero_cfg.get("export_path")
        if export_path:
            candidate = Path(str(export_path)).expanduser()
            if candidate.is_file():
                try:
                    rows = load_zotero_export(candidate)
                    print(f"loaded {len(rows)} Zotero item(s) from {candidate.name}")
                except Exception as exc:
                    print(f"could not parse Zotero export {candidate}: {exc}")
        if not rows and zotero_cfg.get("use_api"):
            library_id = str(zotero_cfg.get("library_id") or os.environ.get("ZOTERO_LIBRARY_ID") or "").strip()
            if library_id:
                try:
                    rows = fetch_zotero_api(
                        library_id,
                        api_key=os.environ.get(str(zotero_cfg.get("api_key_env") or "ZOTERO_API_KEY")),
                        library_type=str(zotero_cfg.get("library_type") or "user"),
                    )
                    print(f"loaded {len(rows)} Zotero item(s) via API")
                except Exception as exc:
                    print(f"Zotero API unavailable: {exc}")
        if not rows:
            self._zotero_failed = True
        self._zotero_rows = rows
        return rows

    def _zotero_for(self, pdf: Path) -> ZoteroMeta | None:
        rows = self._zotero_metadata()
        if not rows:
            return None
        return match_zotero_meta(pdf, rows)

    def _summarizer(self, backend, schema: Schema | None = None) -> Summarizer:
        return Summarizer(
            self.store,
            schema or self.schema,
            backend,
            self.work_dir,
            packet_char_budget=self.packet_budget,
            skip_reconcile_if_clean=self.skip_reconcile_if_clean,
            packet_warn_chars=self.packet_warn_chars,
        )

    def extract_folder(self, folder: Path | None = None, force: bool = False) -> list[str]:
        folder = Path(folder or self.default_folder)
        pdfs = discover_pdfs(folder)
        return self.extract_pdfs(pdfs, folder, force=force, scan_id=new_scan_id(pdfs), scan_source=str(folder))

    def extract_paths(self, paths: list[str | Path], force: bool = False, folder: Path | None = None) -> list[str]:
        pdfs = collect_pdfs(paths)
        root = Path(folder) if folder else id_root_for(pdfs, self.default_folder)
        return self.extract_pdfs(pdfs, root, force=force, scan_id=new_scan_id(pdfs), scan_source="selection")

    def extract_pdfs(
        self,
        pdfs: list[Path],
        folder: Path,
        force: bool = False,
        scan_id: str | None = None,
        scan_source: str | None = None,
    ) -> list[str]:
        scan_id = scan_id or new_scan_id(pdfs)
        return [self.extract_one(pdf, folder, force=force, scan_id=scan_id, scan_source=scan_source) for pdf in pdfs]

    def extract_one(
        self,
        pdf: Path,
        folder: Path,
        force: bool = False,
        scan_id: str | None = None,
        scan_source: str | None = None,
    ) -> str:
        pdf = Path(pdf)
        current_hash = file_sha256(pdf)
        zmeta = self._zotero_for(pdf)
        paper_id = paper_id_for(pdf, file_hash=current_hash)
        if zmeta is not None:
            library_row = self.store.find_by_zotero_key(zmeta.item_key or "") or (
                self.store.find_by_doi(zmeta.doi or "") if zmeta.doi else None
            )
            if library_row and str(library_row["id"]).startswith("zotero:"):
                # Attach the PDF to the existing Zotero library row instead of creating a new one.
                self.store.change_paper_id(library_row["id"], paper_id)
        record = self.store.get_paper(paper_id)
        if (
            not force
            and record
            and record.get("file_hash") == current_hash
            and self.store.list_pages(paper_id)
        ):
            print(f"skipping extract for {pdf.name} (unchanged hash)")
            return paper_id
        extracted = extract_paper(pdf, low_text_char_threshold=self.low_text, zotero_meta=zmeta)
        try:
            relative = str(pdf.resolve().relative_to(folder.resolve()))
        except ValueError:
            relative = pdf.name
        prior_status = (record or {}).get("status") or "extracted"
        if prior_status == "library":
            prior_status = "extracted"
        self.store.upsert_paper(
            {
                "id": paper_id,
                "source_path": str(pdf),
                "relative_path": relative,
                "folder": _folder_label(pdf, folder),
                "file_hash": extracted.file_hash,
                "title": extracted.title or pdf.stem,
                "authors": extracted.authors,
                "year": extracted.year or _year_from_name(pdf.name),
                "doi": extracted.doi,
                "publication": extracted.publication,
                "volume": extracted.volume,
                "issue": extracted.issue,
                "pages": extracted.pages_range,
                "zotero_key": extracted.zotero_key,
                "meta_source": extracted.meta_source,
                "citation": extracted.citation,
                "page_count": extracted.page_count,
                "warnings": json.dumps(extracted.warnings, ensure_ascii=False),
                "extracted_at": utc_now(),
                "scan_id": scan_id,
                "scan_source": scan_source,
                "status": prior_status if prior_status in {"summarized", "partial"} else "extracted",
            }
        )
        self.store.replace_pages(
            paper_id,
            [
                {
                    "paper_id": paper_id,
                    "page_number": page.page_number,
                    "clean_text": page.clean_text or page.raw_text,
                    "char_count": page.char_count,
                    "is_low_text": int(page.is_low_text),
                }
                for page in extracted.pages
            ],
        )
        self.store.replace_sections(
            paper_id,
            [
                {
                    "paper_id": paper_id,
                    "section_key": section.key,
                    "title": section.title,
                    "page_start": section.page_start,
                    "page_end": section.page_end,
                    "text": section.text,
                }
                for section in extracted.sections
            ],
        )
        return paper_id

    def summarize_folder(
        self,
        folder: Path | None = None,
        force: bool = False,
        limit: int | None = None,
        name_contains: str | None = None,
        schema: Schema | None = None,
    ) -> list[str]:
        folder = Path(folder or self.default_folder)
        return self.summarize_pdfs(
            discover_pdfs(folder),
            folder,
            force=force,
            limit=limit,
            name_contains=name_contains,
            schema=schema,
        )

    def summarize_paths(
        self,
        paths: list[str | Path],
        force: bool = False,
        limit: int | None = None,
        schema: Schema | None = None,
        folder: Path | None = None,
    ) -> list[str]:
        pdfs = collect_pdfs(paths)
        root = Path(folder) if folder else id_root_for(pdfs, self.default_folder)
        return self.summarize_pdfs(pdfs, root, force=force, limit=limit, schema=schema)

    def _parallel_workers(self) -> int:
        if self.active_backend_name() != "external":
            return 1
        profile = load_profile(self.root)
        cap = profile.get("parallel_sessions") or (self.settings.get("external") or {}).get("parallel_sessions") or 5
        workers = parallel_workers(cap)
        provider_cap = self._provider_parallel_cap()
        if provider_cap and workers > provider_cap:
            print(f"parallel sessions capped at {provider_cap} by the active provider's limit (requested <{cap})")
            workers = provider_cap
        return workers

    def _provider_parallel_cap(self) -> int | None:
        """Per-provider concurrency ceiling from the active preset (or external.max_parallel)."""
        ext = self.settings.get("external") or {}
        presets = ext.get("presets") or []
        model_id = str(ext.get("model_id") or "").strip()
        preset = next((p for p in presets if str(p.get("id") or "") == model_id), {})
        raw = preset.get("max_parallel", ext.get("max_parallel"))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _missing_field_ids(self, paper_id: str, schema: Schema) -> list[str]:
        """Fields with no usable text yet (neither generated nor human)."""
        stored = self.store.fields_for(paper_id)
        missing: list[str] = []
        for spec in schema.fields:
            row = stored.get(spec.id)
            text = ((row or {}).get("human_text") or (row or {}).get("generated_text") or "").strip()
            if not text:
                missing.append(spec.id)
        return missing

    def summarize_pdfs(
        self,
        pdfs: list[Path],
        folder: Path,
        force: bool = False,
        limit: int | None = None,
        name_contains: str | None = None,
        schema: Schema | None = None,
    ) -> list[str]:
        created = self.backend is None
        backend = self._backend()
        active_schema = schema or self.schema
        try:
            jobs: list[tuple[Path, str, list[str]]] = []
            skipped: list[str] = []
            seen_hashes: set[str] = set()
            for pdf in pdfs:
                if name_contains and name_contains.casefold() not in pdf.name.casefold():
                    continue
                if limit is not None and len(jobs) >= limit:
                    break
                paper_id = self.extract_one(pdf, folder)
                record = self.store.get_paper(paper_id) or {}
                file_hash = record.get("file_hash") or file_sha256(pdf)
                if file_hash in seen_hashes:
                    skipped.append(pdf.name)
                    print(f"skipping duplicate PDF in this run: {pdf.name}")
                    continue
                seen_hashes.add(file_hash)
                missing = self._missing_field_ids(paper_id, active_schema)
                if not force and not missing:
                    skipped.append(pdf.name)
                    print(f"skipping {pdf.name} (all {len(active_schema.fields)} fields filled)")
                    continue
                jobs.append((pdf, paper_id, missing))
            if skipped:
                print(f"skipped {len(skipped)} already-complete paper(s)")
            workers = min(self._parallel_workers(), max(1, len(jobs)))
            print(f"queued {len(jobs)} paper(s) for summarize" + (f" with {workers} parallel sessions" if workers > 1 else ""))
            for pdf, _paper_id, missing in jobs:
                note = "full" if len(missing) == len(active_schema.fields) else f"{len(missing)} missing cell(s)"
                print(f"  queue {pdf.name} ({note})")
            done: list[str] = []
            errors: list[str] = []

            def _one(pdf: Path, paper_id: str, missing: list[str]) -> str:
                try:
                    extracted = self._load_extracted(paper_id, pdf, folder)
                    target = active_schema if force else apply_profile(active_schema, missing)
                    print(f"summarizing {pdf.name} as {paper_id} ({len(target.fields)} field(s))")
                    self._summarizer(backend, target).summarize_paper(paper_id, extracted, force=force)
                    self.store.set_paper_status(paper_id, "summarized")
                    return paper_id
                except Exception:
                    self.store.set_paper_status(paper_id, "error")
                    raise

            if workers == 1:
                for pdf, paper_id, missing in jobs:
                    done.append(_one(pdf, paper_id, missing))
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_one, pdf, paper_id, missing): paper_id for pdf, paper_id, missing in jobs}
                    for future in as_completed(futures):
                        paper_id = futures[future]
                        try:
                            done.append(future.result())
                        except Exception as exc:
                            errors.append(f"{paper_id}: {exc}")
                            print(f"error summarizing {paper_id}: {exc}")
            if errors and not done:
                raise RuntimeError("all parallel summarize jobs failed:\n" + "\n".join(errors))
            if errors:
                print(f"{len(errors)} paper(s) failed:\n" + "\n".join(errors))
            return done
        finally:
            if created:
                close = getattr(backend, "close", None)
                if callable(close):
                    close()

    def resummarize_fields(
        self,
        paper_id: str,
        field_ids: list[str],
        extra_instruction: str = "",
        instruction_override: str | dict[str, str] | None = None,
        schema: Schema | None = None,
    ) -> list[str]:
        record = self.store.get_paper(paper_id)
        if not record:
            raise RuntimeError(f"paper {paper_id} is not in the store")
        pdf = Path(record["source_path"])
        pages = self.store.list_pages(paper_id)
        sections = self.store.list_sections(paper_id)
        if not pages:
            raise RuntimeError(f"paper {paper_id} has no extracted pages")
        extracted = extracted_paper_from_rows(pdf, record, pages, sections)
        created = self.backend is None
        backend = self._backend()
        try:
            summarizer = self._summarizer(backend, schema)
            overrides: dict[str, str] = {}
            if isinstance(instruction_override, dict):
                overrides = {str(key): str(value) for key, value in instruction_override.items() if str(value).strip()}
            elif instruction_override:
                for field_id in field_ids:
                    overrides[field_id] = str(instruction_override)
            updated = summarizer.resummarize_fields(
                paper_id,
                extracted,
                field_ids,
                extra_instruction=extra_instruction,
                instruction_overrides=overrides or None,
            )
            return list(updated)
        finally:
            if created:
                close = getattr(backend, "close", None)
                if callable(close):
                    close()

    def export(self, path: Path | None = None, schema: Schema | None = None) -> Path:
        return export_workbook(self.store, schema or self.schema, path or self.output_path)

    def sync_back(self, path: Path | None = None) -> int:
        return sync_back(self.store, self.schema, path or self.output_path)

    def import_zotero(self, path: Path | None = None, json_text: str | None = None) -> dict:
        """Import Zotero items as library rows (metadata only, no PDF extraction)."""
        if json_text:
            import tempfile

            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
                handle.write(json_text)
                temp_path = Path(handle.name)
            try:
                rows = load_zotero_export(temp_path)
            finally:
                temp_path.unlink(missing_ok=True)
        elif path:
            rows = load_zotero_export(Path(path))
        else:
            raise ValueError("provide a Zotero export path or JSON text")
        if not rows:
            return {"imported": 0, "merged": 0}
        self._zotero_rows = rows
        self._zotero_failed = False
        scan_id = new_scan_id()
        imported = 0
        merged = 0
        for meta in rows:
            pid = paper_id_for_zotero(meta)
            existing = self.store.get_paper(pid)
            if not existing and meta.doi:
                existing = self.store.find_by_doi(meta.doi)
            if existing:
                if str(existing["id"]).startswith("zotero:") or not existing.get("source_path"):
                    target = existing["id"]
                else:
                    # PDF already extracted: fold the library row into it.
                    target = existing["id"]
                    merged += 1
                record = dict(existing)
                record.update(
                    {
                        "title": meta.title or existing.get("title"),
                        "authors": meta.authors or existing.get("authors"),
                        "year": meta.year or existing.get("year"),
                        "doi": meta.doi or existing.get("doi"),
                        "publication": meta.publication or existing.get("publication"),
                        "volume": meta.volume or existing.get("volume"),
                        "issue": meta.issue or existing.get("issue"),
                        "pages": meta.pages or existing.get("pages"),
                        "zotero_key": meta.item_key or existing.get("zotero_key"),
                        "meta_source": meta.source,
                        "citation": meta.citation() or existing.get("citation"),
                        "scan_id": scan_id,
                        "scan_source": "zotero-import",
                    }
                )
                self.store.upsert_paper(record)
                if target != pid and self.store.get_paper(pid):
                    self.store.merge_paper_into(pid, target)
            else:
                self.store.upsert_paper(
                    {
                        "id": pid,
                        "source_path": "",
                        "relative_path": meta.title or pid,
                        "folder": "Zotero",
                        "file_hash": f"zotero:{meta.item_key or meta.doi or pid}",
                        "title": meta.title,
                        "authors": meta.authors,
                        "year": meta.year,
                        "doi": meta.doi,
                        "publication": meta.publication,
                        "volume": meta.volume,
                        "issue": meta.issue,
                        "pages": meta.pages,
                        "zotero_key": meta.item_key,
                        "meta_source": meta.source,
                        "citation": meta.citation(),
                        "page_count": 0,
                        "warnings": "[]",
                        "extracted_at": utc_now(),
                        "scan_id": scan_id,
                        "scan_source": "zotero-import",
                        "status": "library",
                    }
                )
                imported += 1
        return {"imported": imported, "merged": merged, "total": len(rows)}

    def run(
        self,
        folder: Path | None = None,
        force: bool = False,
        limit: int | None = None,
        name_contains: str | None = None,
    ) -> Path:
        folder = Path(folder or self.default_folder)
        self.extract_folder(folder)
        self.summarize_folder(folder, force=force, limit=limit, name_contains=name_contains)
        return self.export()

    def _load_extracted(self, paper_id: str, pdf: Path, folder: Path):
        record = self.store.get_paper(paper_id)
        if not record:
            raise RuntimeError(f"paper {paper_id} is not in the store")
        pages = self.store.list_pages(paper_id)
        sections = self.store.list_sections(paper_id)
        if not pages:
            paper_id = self.extract_one(pdf, folder, force=True)
            record = self.store.get_paper(paper_id)
            pages = self.store.list_pages(paper_id)
            sections = self.store.list_sections(paper_id)
        return extracted_paper_from_rows(pdf, record or {}, pages, sections)
