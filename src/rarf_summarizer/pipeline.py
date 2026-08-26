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
from rarf_summarizer.schema import Schema, load_schema
from rarf_summarizer.selection import collect_pdfs, id_root_for
from rarf_summarizer.storage import Store, utc_now
from rarf_summarizer.summarizer import Summarizer, paper_id_for


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

    def active_backend_name(self) -> str:
        profile = load_profile(self.root)
        name = profile.get("backend") or (self.settings.get("model") or {}).get("backend") or "local"
        return str(name).strip().casefold()

    def _backend(self):
        return make_backend(self.settings, name=self.active_backend_name(), injected=self.backend)

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
        return self.extract_pdfs(discover_pdfs(folder), folder, force=force)

    def extract_paths(self, paths: list[str | Path], force: bool = False, folder: Path | None = None) -> list[str]:
        pdfs = collect_pdfs(paths)
        root = Path(folder) if folder else id_root_for(pdfs, self.default_folder)
        return self.extract_pdfs(pdfs, root, force=force)

    def extract_pdfs(self, pdfs: list[Path], folder: Path, force: bool = False) -> list[str]:
        return [self.extract_one(pdf, folder, force=force) for pdf in pdfs]

    def extract_one(self, pdf: Path, folder: Path, force: bool = False) -> str:
        paper_id = paper_id_for(pdf, folder)
        record = self.store.get_paper(paper_id)
        current_hash = file_sha256(pdf)
        if (
            not force
            and record
            and record.get("file_hash") == current_hash
            and self.store.list_pages(paper_id)
        ):
            print(f"skipping extract for {pdf.name} (unchanged hash)")
            return paper_id
        extracted = extract_paper(pdf, low_text_char_threshold=self.low_text)
        try:
            relative = str(pdf.resolve().relative_to(folder.resolve()))
        except ValueError:
            relative = pdf.name
        paper_id = paper_id_for(pdf, folder)
        self.store.upsert_paper(
            {
                "id": paper_id,
                "source_path": str(pdf),
                "relative_path": relative,
                "folder": _folder_label(pdf, folder),
                "file_hash": extracted.file_hash,
                "title": extracted.title or pdf.stem,
                "authors": extracted.authors,
                "year": _year_from_name(pdf.name),
                "doi": extracted.doi,
                "page_count": extracted.page_count,
                "warnings": json.dumps(extracted.warnings, ensure_ascii=False),
                "extracted_at": utc_now(),
                "status": "extracted",
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
        return parallel_workers(cap)

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
        try:
            summarizer = self._summarizer(backend, schema)
            jobs: list[tuple[Path, str]] = []
            for pdf in pdfs:
                if name_contains and name_contains.casefold() not in pdf.name.casefold():
                    continue
                if limit is not None and len(jobs) >= limit:
                    break
                paper_id = self.extract_one(pdf, folder)
                jobs.append((pdf, paper_id))
            workers = min(self._parallel_workers(), max(1, len(jobs)))
            if workers > 1:
                print(f"summarizing {len(jobs)} paper(s) with {workers} parallel sessions")
            done: list[str] = []
            errors: list[str] = []

            def _one(pdf: Path, paper_id: str) -> str:
                extracted = self._load_extracted(paper_id, pdf, folder)
                print(f"summarizing {pdf.name} as {paper_id}")
                summarizer.summarize_paper(paper_id, extracted, force=force)
                record = self.store.get_paper(paper_id)
                if record:
                    record["status"] = "summarized"
                    self.store.upsert_paper(record)
                return paper_id

            if workers == 1:
                for pdf, paper_id in jobs:
                    done.append(_one(pdf, paper_id))
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_one, pdf, paper_id): paper_id for pdf, paper_id in jobs}
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
