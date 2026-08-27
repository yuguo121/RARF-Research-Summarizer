from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from rarf_summarizer.cursor_runtime import AgentBackend, AgentRunError, AgentStartupError
from rarf_summarizer.formatting import format_field, parse_field_value
from rarf_summarizer.json_util import JsonExtractError, extract_json_object
from rarf_summarizer.models import Envelope, slug_id
from rarf_summarizer.pdf_pipeline import METHOD_SECTIONS, THEORY_SECTIONS, ExtractedPaper, file_sha256
from rarf_summarizer.quotes import verify_quote
from rarf_summarizer.schema import Schema, apply_profile
from rarf_summarizer.storage import Store, utc_now


JSON_CONTRACT = """
Return ONLY a JSON object. No markdown commentary outside JSON.
Each field key must be the field id provided below.
Each field value must be an object:
{
  "status": "present" | "not_reported" | "not_applicable" | "unclear",
  "confidence": 0.0-1.0,
  "value": <field-specific>,
  "evidence": [{"page": <int>, "quote": "<exact substring from the packet>"}],
  "warnings": []
}
Use not_applicable when the paper type makes the field meaningless (e.g. sample size in a purely theoretical review).
Use not_reported when the field could apply but the paper does not provide it.
Do not invent hypotheses, samples, or measures that are not in the packet.
"""


def cache_key(file_hash: str, schema: Schema, model: str, session: str) -> str:
    blob = "|".join([file_hash, schema.version, schema.prompt_version, model, session])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def paper_id_for(path: Path, root: Path | None = None, file_hash: str | None = None) -> str:
    """Content-based id: the same PDF maps to the same row regardless of where it was selected from."""
    digest_src = file_hash or file_sha256(path)
    digest = hashlib.sha1(digest_src.encode("utf-8")).hexdigest()[:10]
    stem = Path(path).stem[:80]
    return f"{digest}:{stem}"


def paper_id_for_zotero(meta) -> str:
    """Stable id for a Zotero library item that may not have a local PDF yet."""
    key = (meta.doi or meta.item_key or meta.title or "unknown").casefold()
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9]+", " ", meta.title or "")[:60].strip()
    return f"zotero:{digest}:{slug}"


def _field_block(schema: Schema, session: str) -> str:
    lines = []
    for spec in schema.fields_for_session(session):
        lines.append(f"- {spec.id} ({spec.label}) [{spec.value_kind}]: {spec.instruction}")
    return "\n".join(lines)


def theory_prompt(schema: Schema, packet: str, paper: ExtractedPaper) -> str:
    extras: list[str] = []
    if schema.has("framing"):
        extras.append(
            """
Framing.value MUST be:
{"primary_basis": "IV-led"|"DV-led"|"theory-led"|"mixed/other",
 "secondary_style": "theoretical"|"phenomenological"|"mixed"|"not_reported",
 "rationale": "<short rationale>"}
Decide whether the paper is organized around an IV, a DV, or theory per se.
"""
        )
    if schema.has("key_argument"):
        extras.append(
            """
key_argument.value MUST be a list of argument objects:
{"quote": "<exact words from the packet>", "page": <int>,
 "academic_paraphrase": "...", "plain_language": "...",
 "causal_formulation": "..." or null}
There may be several arguments. Each needs an exact quote plus the rephrasings.
"""
        )
    if schema.has("key_variables"):
        extras.append(
            """
key_variables.value MUST be a list:
{"class": "DV"|"IV"|"moderator"|"mediator", "name": "...", "nominal_definition": "..."}
Conceptual definitions only.
"""
        )
    return _wrap_prompt("theory", schema, packet, paper, "\n".join(extras))


def method_prompt(schema: Schema, packet: str, paper: ExtractedPaper) -> str:
    extra = ""
    if schema.has("measures"):
        extra = """
measures.value MUST be a list:
{"class": "DV"|"IV"|"moderator"|"mediator"|"control",
 "name": "...", "linked_construct": "<conceptual name>",
 "operationalization": "...", "range": "...",
 "type": "continuous"|"binary"|"ordinal"|"cardinal"}
Empirical operationalizations only. Do not repeat nominal definitions unless they are how the variable is measured.
"""
    return _wrap_prompt("method", schema, packet, paper, extra)


def reconcile_prompt(schema: Schema, theory: dict, method: dict, qa_notes: list[str]) -> str:
    return f"""
You are reconciling two structured RARF extractions of the same paper.
Merge them into one JSON object covering ALL of these field ids:
{', '.join(schema.field_ids)}

Rules:
- Prefer exact quotes already verified. If a quote is flagged, replace it with an exact packet substring or keep the field but add a warning.
- Link measures to constructs by construct_id / linked_construct name.
- Do not drop a present field because the other session omitted it.
- Keep framing.primary_basis as IV-led, DV-led, theory-led, or mixed/other.
- Keep every key argument quote verbatim; retain academic, plain, and causal rephrasings.
- Preserve not_applicable vs not_reported distinctions.

QA notes:
{json.dumps(qa_notes, ensure_ascii=False, indent=2)}

Theory JSON:
{json.dumps(theory, ensure_ascii=False)}

Method JSON:
{json.dumps(method, ensure_ascii=False)}

{JSON_CONTRACT}
"""


def repair_prompt(field_id: str, issue: str, envelope: dict, page_excerpts: str) -> str:
    return f"""
Repair only this RARF field: {field_id}
Issue: {issue}
Current JSON: {json.dumps(envelope, ensure_ascii=False)}

Page excerpts:
{page_excerpts}

Return a JSON object with a single key "{field_id}" whose value is a corrected field object
(status, confidence, value, evidence). Quotes must be exact substrings of the excerpts.
{JSON_CONTRACT}
"""


def _wrap_prompt(session: str, schema: Schema, packet: str, paper: ExtractedPaper, extra: str) -> str:
    meta = [
        f"Source file: {paper.source_path.name}",
        f"PDF path: {paper.source_path}",
        f"PDF title: {paper.title or 'unknown'}",
        f"PDF author: {paper.authors or 'unknown'}",
        f"DOI: {paper.doi or 'unknown'}",
        f"Pages: {paper.page_count}",
    ]
    if paper.citation:
        meta.append(f"Citation (from {paper.meta_source or 'metadata'}): {paper.citation}")
    return f"""
You are filling a Research Article Review Form (RARF) for one academic paper.
Session: {session}
{chr(10).join(meta)}

Fields for this session:
{_field_block(schema, session)}

{extra}

The packet is tagged with [p.N] markers. Use those numbers as page citations.
This message already contains the complete extracted packet. Do not search the filesystem unless a tool is explicitly available. Do not invent prose that is missing from a sparse or scanned extract.
Start your reply with `{{` and return only one JSON object.

Packet:
{packet}

{JSON_CONTRACT}
""".strip()


def cell_prompt(
    schema: Schema,
    packet: str,
    paper: ExtractedPaper,
    field_ids: list[str],
    extra_instruction: str,
    current_values: dict[str, Any],
) -> str:
    extra = ""
    if extra_instruction.strip():
        extra = f"Additional notes for this rerun:\n{extra_instruction.strip()}\n"
    extra += (
        "Replace only the listed field(s). Return JSON with those field ids as keys.\n"
        "Current values to replace:\n"
        + json.dumps(current_values, ensure_ascii=False, indent=2)
    )
    session = schema.fields[0].session if schema.fields else "theory"
    return _wrap_prompt(session, schema, packet, paper, extra)


def normalize_session_payload(schema: Schema, payload: dict[str, Any], session: str | None = None) -> dict[str, Envelope]:
    fields = schema.fields_for_session(session) if session else schema.fields
    parsed: dict[str, Envelope] = {}
    for spec in fields:
        raw = payload.get(spec.id)
        if raw is None:
            # tolerate label keys
            raw = payload.get(spec.label)
        try:
            parsed[spec.id] = parse_field_value(spec.value_kind, raw)
        except Exception as exc:
            parsed[spec.id] = Envelope(
                status="unclear",
                confidence=0.1,
                value=raw,
                warnings=[f"validation error: {exc}"],
            )
    return parsed


def envelopes_to_json(envelopes: dict[str, Envelope]) -> dict[str, Any]:
    return {key: value.model_dump() for key, value in envelopes.items()}


def merge_session_envelopes(
    schema: Schema,
    theory: dict[str, Envelope],
    method: dict[str, Envelope],
) -> dict[str, Envelope]:
    merged: dict[str, Envelope] = {}
    for spec in schema.fields:
        source = theory if spec.session == "theory" else method
        fallback = method if spec.session == "theory" else theory
        envelope = source.get(spec.id) or fallback.get(spec.id)
        if envelope is None:
            merged[spec.id] = Envelope(status="not_reported")
        else:
            merged[spec.id] = envelope.model_copy(deep=True)
    return merged


def _needs_llm_reconcile(qa_notes: list[str]) -> bool:
    return any("quote not found" in note or "quote mismatch" in note for note in qa_notes)


class Summarizer:
    def __init__(
        self,
        store: Store,
        schema: Schema,
        backend: AgentBackend,
        work_dir: Path,
        packet_char_budget: int = 0,
        skip_reconcile_if_clean: bool = True,
        packet_warn_chars: int = 100000,
    ):
        self.store = store
        self.schema = schema
        self.backend = backend
        self.work_dir = work_dir
        self.packet_char_budget = packet_char_budget
        self.packet_warn_chars = int(packet_warn_chars or packet_char_budget or 0)
        self.skip_reconcile_if_clean = skip_reconcile_if_clean
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def _session_packet(self, extracted: ExtractedPaper, session: str) -> str:
        keys = THEORY_SECTIONS if session == "theory" else METHOD_SECTIONS
        packet = extracted.packet(keys)
        if self.packet_warn_chars and len(packet) >= self.packet_warn_chars:
            print(f"warning: {session} packet is {len(packet)} characters")
        return packet

    def summarize_paper(self, paper_id: str, extracted: ExtractedPaper, force: bool = False) -> dict[str, Envelope]:
        if not self.schema.fields:
            raise ValueError("no RARF dimensions selected")
        model = self.backend.resolve_model()
        pages = extracted.page_map()
        theory: dict[str, Envelope] = {}
        method: dict[str, Envelope] = {}
        try:
            if self.schema.fields_for_session("theory"):
                theory_packet = self._session_packet(extracted, "theory")
                theory = self._run_session(
                    paper_id,
                    "theory",
                    theory_prompt(self.schema, theory_packet, extracted),
                    extracted,
                    model,
                    force,
                    session_fields="theory",
                )
            if self.schema.fields_for_session("method"):
                method_packet = self._session_packet(extracted, "method")
                method = self._run_session(
                    paper_id,
                    "method",
                    method_prompt(self.schema, method_packet, extracted),
                    extracted,
                    model,
                    force,
                    session_fields="method",
                )
            if theory and method:
                try:
                    reconciled = self._reconcile(paper_id, extracted, theory, method, pages, model, force)
                except (AgentStartupError, AgentRunError, JsonExtractError) as exc:
                    print(f"reconcile failed for {paper_id}; keeping theory/method merge ({exc})")
                    reconciled = merge_session_envelopes(self.schema, theory, method)
                    note = f"reconcile failed: {exc}"
                    for envelope in reconciled.values():
                        envelope.warnings.append(note)
            else:
                reconciled = merge_session_envelopes(self.schema, theory, method)
            if self.schema.has("key_argument"):
                reconciled = self._repair_if_needed(paper_id, extracted, reconciled, pages, model)
            self._apply_metadata(paper_id, extracted, reconciled)
            self._persist(paper_id, reconciled, pages)
            return reconciled
        except Exception:
            self._persist_partial(paper_id, theory, method, pages)
            raise

    def resummarize_fields(
        self,
        paper_id: str,
        extracted: ExtractedPaper,
        field_ids: list[str],
        extra_instruction: str = "",
        instruction_overrides: dict[str, str] | None = None,
    ) -> dict[str, Envelope]:
        wanted = [fid for fid in field_ids if fid in self.schema.field_ids]
        if not wanted:
            raise ValueError("no matching RARF fields to resummarize")
        model = self.backend.resolve_model()
        pages = extracted.page_map()
        updated: dict[str, Envelope] = {}
        by_session: dict[str, list[str]] = {"theory": [], "method": []}
        for field_id in wanted:
            by_session[self.schema.field(field_id).session].append(field_id)
        for session, ids in by_session.items():
            if not ids:
                continue
            subset = apply_profile(self.schema, ids, instruction_overrides)
            packet = self._session_packet(extracted, session)
            current = {}
            for field_id in ids:
                row = self.store.get_field(paper_id, field_id) or {}
                current[field_id] = row.get("human_text") or row.get("generated_text") or ""
            prompt = cell_prompt(subset, packet, extracted, ids, extra_instruction, current)
            parsed = self._run_session(
                paper_id,
                f"resummarize:{session}",
                prompt,
                extracted,
                model,
                force=True,
                session_fields=session,
            )
            for field_id in ids:
                if field_id in parsed:
                    updated[field_id] = parsed[field_id]
        self._persist(paper_id, updated, pages, only_ids=list(updated), clear_human=True)
        return updated

    def _reconcile(
        self,
        paper_id: str,
        extracted: ExtractedPaper,
        theory: dict[str, Envelope],
        method: dict[str, Envelope],
        pages: dict[int, str],
        model: str,
        force: bool,
    ) -> dict[str, Envelope]:
        key = cache_key(extracted.file_hash, self.schema, model, "reconcile")
        snapshot_path = self.work_dir / f"{paper_id.replace(':', '_')}_reconcile.json"
        if not force and self.store.cached_run(paper_id, "reconcile", key) and snapshot_path.exists():
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            return normalize_session_payload(self.schema, payload, None)
        qa_notes = self._quote_issues(theory, pages) + self._quote_issues(method, pages)
        if self.skip_reconcile_if_clean and not _needs_llm_reconcile(qa_notes):
            merged = merge_session_envelopes(self.schema, theory, method)
            self._record_local_session(paper_id, "reconcile", key, model, envelopes_to_json(merged), snapshot_path)
            return merged
        merged_prompt = reconcile_prompt(
            self.schema,
            envelopes_to_json(theory),
            envelopes_to_json(method),
            qa_notes,
        )
        return self._run_session(
            paper_id,
            "reconcile",
            merged_prompt,
            extracted,
            model,
            force,
            session_fields=None,
        )

    def _record_local_session(
        self,
        paper_id: str,
        session: str,
        key: str,
        model: str,
        payload: dict[str, Any],
        snapshot_path: Path,
    ) -> None:
        snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.add_run(
            {
                "paper_id": paper_id,
                "session_type": session,
                "cache_key": key,
                "model": model,
                "schema_version": self.schema.version,
                "prompt_version": self.schema.prompt_version,
                "status": "finished",
                "started_at": utc_now(),
                "finished_at": utc_now(),
                "error": None,
            }
        )

    def _run_session(
        self,
        paper_id: str,
        session: str,
        prompt: str,
        extracted: ExtractedPaper,
        model: str,
        force: bool,
        session_fields: str | None,
    ) -> dict[str, Envelope]:
        key = cache_key(extracted.file_hash, self.schema, model, session)
        snapshot_path = self.work_dir / f"{paper_id.replace(':', '_')}_{session}.json"
        if not force and self.store.cached_run(paper_id, session, key) and snapshot_path.exists():
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            return normalize_session_payload(self.schema, payload, session_fields)
        print(f"running {session} for {paper_id}")
        run_pk = self.store.add_run(
            {
                "paper_id": paper_id,
                "session_type": session,
                "cache_key": key,
                "model": model,
                "schema_version": self.schema.version,
                "prompt_version": self.schema.prompt_version,
                "status": "running",
                "started_at": utc_now(),
            }
        )
        try:
            result = self.backend.run(prompt, session=session, work_dir=str(self.work_dir))
            payload = extract_json_object(result.text)
            parsed = normalize_session_payload(self.schema, payload, session_fields)
            self.store.update_run(
                run_pk,
                status="finished",
                finished_at=utc_now(),
                agent_id=result.agent_id,
                run_id=result.run_id,
                model=result.model or model,
                error=None,
            )
            snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return parsed
        except (AgentStartupError, AgentRunError, JsonExtractError) as exc:
            self.store.update_run(run_pk, status="error", finished_at=utc_now(), error=str(exc))
            raise

    def _quote_issues(self, envelopes: dict[str, Envelope], pages: dict[int, str]) -> list[str]:
        notes = []
        for field_id, envelope in envelopes.items():
            if field_id == "key_argument":
                for item in envelope.value or []:
                    quote = item.get("quote") if isinstance(item, dict) else ""
                    page = item.get("page") if isinstance(item, dict) else None
                    check = verify_quote(quote, pages, page)
                    if not check["matched"]:
                        notes.append(f"{field_id}: quote not found (page={page}): {quote[:180]}")
            for evidence in envelope.evidence:
                if evidence.quote:
                    check = verify_quote(evidence.quote, pages, evidence.page)
                    if not check["matched"]:
                        notes.append(f"{field_id} evidence quote not found (page={evidence.page})")
            notes.extend(f"{field_id}: {warning}" for warning in envelope.warnings)
        return notes

    def _repair_if_needed(
        self,
        paper_id: str,
        extracted: ExtractedPaper,
        envelopes: dict[str, Envelope],
        pages: dict[int, str],
        model: str,
    ) -> dict[str, Envelope]:
        issues = self._quote_issues(envelopes, pages)
        key_arg_mismatch = [note for note in issues if note.startswith("key_argument:")]
        if not key_arg_mismatch or "key_argument" not in envelopes:
            return envelopes
        field_id = "key_argument"
        envelope = envelopes[field_id]
        excerpts = _nearby_pages(pages, envelope)
        prompt = repair_prompt(field_id, "; ".join(key_arg_mismatch), envelope.model_dump(), excerpts)
        try:
            result = self.backend.run(prompt, session=f"repair:{field_id}", work_dir=str(self.work_dir))
            payload = extract_json_object(result.text)
            raw = payload.get(field_id, payload)
            spec = self.schema.field(field_id)
            envelopes[field_id] = parse_field_value(spec.value_kind, raw)
            self.store.add_run(
                {
                    "paper_id": paper_id,
                    "session_type": f"repair:{field_id}",
                    "cache_key": cache_key(extracted.file_hash, self.schema, model, f"repair:{field_id}"),
                    "agent_id": result.agent_id,
                    "run_id": result.run_id,
                    "model": result.model or model,
                    "schema_version": self.schema.version,
                    "prompt_version": self.schema.prompt_version,
                    "status": "finished",
                    "started_at": utc_now(),
                    "finished_at": utc_now(),
                }
            )
        except Exception as exc:
            envelope.warnings.append(f"repair failed: {exc}")
        return envelopes

    def _apply_metadata(
        self,
        paper_id: str,
        extracted: ExtractedPaper,
        envelopes: dict[str, Envelope],
    ) -> None:
        if not self.schema.has("citation"):
            return
        existing = self.store.get_field(paper_id, "citation") or {}
        human = (existing.get("human_text") or "").strip()
        if human:
            return
        citation = (extracted.citation or "").strip()
        if not citation:
            parts = []
            if extracted.authors:
                parts.append(extracted.authors.rstrip("."))
            if extracted.year:
                parts.append(f"({extracted.year}).")
            if extracted.title:
                parts.append(extracted.title.rstrip("."))
            if extracted.doi:
                parts.append(f"https://doi.org/{extracted.doi}")
            citation = " ".join(parts)
        if not citation:
            return
        source = extracted.meta_source or "pdf"
        envelopes["citation"] = Envelope(
            status="present",
            confidence=0.99 if source in {"export", "api"} else 0.7,
            value=citation,
            warnings=[f"metadata from {source}"],
        )

    def _persist_partial(
        self,
        paper_id: str,
        theory: dict[str, Envelope],
        method: dict[str, Envelope],
        pages: dict[int, str],
    ) -> None:
        if not theory and not method:
            return
        merged = merge_session_envelopes(self.schema, theory, method)
        filled = [item for item in merged.values() if item.status != "not_reported" or item.value]
        if not filled:
            return
        try:
            self._persist(paper_id, merged, pages)
            print(f"saved partial results for {paper_id} after a later session failed")
        except Exception as exc:
            print(f"could not save partial results for {paper_id}: {exc}")

    def _persist(
        self,
        paper_id: str,
        envelopes: dict[str, Envelope],
        pages: dict[int, str],
        only_ids: list[str] | None = None,
        clear_human: bool = False,
    ) -> None:
        evidence_rows: list[dict[str, Any]] = []
        warning_rows: list[tuple[str, str]] = []
        constructs: list[dict[str, Any]] = []
        measures: list[dict[str, Any]] = []
        target_ids = list(only_ids) if only_ids is not None else list(self.schema.field_ids)
        target_set = set(target_ids)

        if "key_variables" in envelopes and "key_variables" in target_set:
            for item in envelopes["key_variables"].value or []:
                if not isinstance(item, dict):
                    continue
                cid = item.get("construct_id") or slug_id(item.get("name") or "construct")
                item["construct_id"] = cid
                constructs.append(
                    {
                        "paper_id": paper_id,
                        "construct_id": cid,
                        "class": item.get("class"),
                        "name": item.get("name"),
                        "nominal_definition": item.get("nominal_definition"),
                    }
                )
        construct_by_name = {row["name"].casefold(): row["construct_id"] for row in constructs if row.get("name")}
        if not construct_by_name:
            construct_by_name = {
                row["name"].casefold(): row["construct_id"]
                for row in self.store.list_constructs()
                if row.get("paper_id") == paper_id and row.get("name")
            }
        if "measures" in envelopes and "measures" in target_set:
            for item in envelopes["measures"].value or []:
                if not isinstance(item, dict):
                    continue
                linked = item.get("linked_construct") or item.get("name") or ""
                cid = item.get("construct_id") or construct_by_name.get(linked.casefold()) or slug_id(linked)
                item["construct_id"] = cid
                item["linked_construct"] = linked
                measures.append(
                    {
                        "paper_id": paper_id,
                        "construct_id": cid,
                        "class": item.get("class"),
                        "name": item.get("name"),
                        "operationalization": item.get("operationalization"),
                        "range": item.get("range"),
                        "type": item.get("type"),
                        "linked_construct": linked,
                    }
                )

        for spec in self.schema.fields:
            if spec.id not in target_set:
                continue
            envelope = envelopes.get(spec.id)
            if envelope is None:
                if only_ids is not None:
                    continue
                envelope = Envelope(status="not_reported")
            text = format_field(spec.value_kind, envelope)
            self.store.upsert_field(
                paper_id,
                spec.id,
                {
                    "status": envelope.status,
                    "confidence": envelope.confidence,
                    "generated_text": text,
                    "generated_json": json.dumps(envelope.model_dump(), ensure_ascii=False),
                },
                clear_human=clear_human,
            )
            if spec.id == "key_argument":
                for item in envelope.value or []:
                    if not isinstance(item, dict):
                        continue
                    check = verify_quote(item.get("quote") or "", pages, item.get("page"))
                    if not check["matched"]:
                        warning_rows.append((spec.id, f"quote mismatch: {item.get('quote', '')[:160]}"))
                        envelope.warnings.append("quote mismatch")
                    evidence_rows.append(
                        {
                            "field_id": spec.id,
                            "quote": item.get("quote"),
                            "page": item.get("page"),
                            "matched": check["matched"],
                            "score": check["score"],
                            "location": check["location"],
                            "extra_json": {
                                "academic_paraphrase": item.get("academic_paraphrase"),
                                "plain_language": item.get("plain_language"),
                                "causal_formulation": item.get("causal_formulation"),
                            },
                        }
                    )
            for evidence in envelope.evidence:
                check = verify_quote(evidence.quote, pages, evidence.page) if evidence.quote else {
                    "matched": True,
                    "score": 1,
                    "location": "empty",
                }
                if evidence.quote and not check["matched"]:
                    warning_rows.append((spec.id, f"evidence quote mismatch p.{evidence.page}"))
                evidence_rows.append(
                    {
                        "field_id": spec.id,
                        "quote": evidence.quote,
                        "page": evidence.page,
                        "matched": check["matched"],
                        "score": check["score"],
                        "location": check["location"],
                    }
                )
            for warning in envelope.warnings:
                warning_rows.append((spec.id, warning))

        self.store.replace_evidence(paper_id, evidence_rows, field_ids=target_ids)
        if "key_variables" in target_set and self.schema.has("key_variables") and "key_variables" in envelopes:
            self.store.replace_constructs(paper_id, constructs)
        if "measures" in target_set and self.schema.has("measures") and "measures" in envelopes:
            self.store.replace_measures(paper_id, measures)
        self.store.replace_warnings(paper_id, warning_rows, field_ids=target_ids)


def _nearby_pages(pages: dict[int, str], envelope: Envelope, radius: int = 1) -> str:
    wanted: set[int] = set()
    for evidence in envelope.evidence:
        if evidence.page:
            wanted.update(range(evidence.page - radius, evidence.page + radius + 1))
    if isinstance(envelope.value, list):
        for item in envelope.value:
            if isinstance(item, dict) and item.get("page"):
                wanted.update(range(item["page"] - radius, item["page"] + radius + 1))
    if not wanted:
        wanted = set(list(pages)[:4])
    parts = []
    for number in sorted(n for n in wanted if n in pages):
        parts.append(f"[p.{number}]\n{pages[number]}")
    return "\n\n".join(parts)[:20000]
