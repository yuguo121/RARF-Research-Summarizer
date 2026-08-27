from __future__ import annotations

import json
import os
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from rarf_summarizer.dimension_profile import (
    PARALLEL_OPTIONS,
    load_profile,
    normalize_parallel_sessions,
    save_profile,
    schema_from_profile,
)
from rarf_summarizer.formatting import effective_text
from rarf_summarizer.paths import load_settings, update_dotenv
from rarf_summarizer.pipeline import Pipeline
from rarf_summarizer.schema import load_schema
from rarf_summarizer.selection import collect_pdfs, id_root_for, list_directory, windows_drives
from rarf_summarizer.summarizer import paper_id_for


WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_EXTERNAL_BASE_URL = "https://api.deepseek.com"
DEFAULT_EXTERNAL_PRESETS = [
    {"id": "glm-5.3-flash", "label": "智谱 GLM-5.3 Flash", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key_env": "ZHIPU_API_KEY"},
    {"id": "glm-5.3", "label": "智谱 GLM-5.3", "base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key_env": "ZHIPU_API_KEY"},
    {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash", "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY"},
    {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY"},
]


class JobState:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"
        self.logs: list[str] = []
        self.error: str | None = None
        self.result: dict | None = None
        self.action: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "logs": list(self.logs[-300:]),
                "error": self.error,
                "result": self.result,
                "action": self.action,
            }

    def reset(self, action: str) -> None:
        with self.lock:
            self.status = "running"
            self.logs = []
            self.error = None
            self.result = None
            self.action = action

    def log(self, line: str) -> None:
        text = line.rstrip()
        if not text:
            return
        with self.lock:
            self.logs.append(text)

    def finish(self, result: dict | None = None, error: str | None = None) -> None:
        with self.lock:
            self.result = result
            self.error = error
            self.status = "error" if error else "finished"


class _LogTee:
    def __init__(self, job: JobState, original):
        self.job = job
        self.original = original
        self.buf = ""

    def write(self, data: str) -> int:
        self.original.write(data)
        self.buf += data
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.job.log(line)
        return len(data)

    def flush(self) -> None:
        self.original.flush()


def serve(port: int = 8765, open_browser: bool = True, project_root: Path | None = None) -> None:
    pipeline = Pipeline(project_root)
    job = JobState()
    ctx: dict = {"pipeline": pipeline, "job": job}
    handler = _handler_for(ctx)
    class DeskServer(ThreadingHTTPServer):
        allow_reuse_address = False

    server = DeskServer(("127.0.0.1", port), handler)
    ctx["server"] = server
    url = f"http://127.0.0.1:{port}/"
    print(f"RARF desk at {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping RARF desk")
    finally:
        server.server_close()


def _handler_for(ctx: dict):
    pipeline: Pipeline = ctx["pipeline"]
    job: JobState = ctx["job"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

        def do_GET(self) -> None:
            try:
                self._dispatch_get()
            except Exception as exc:
                traceback.print_exc()
                try:
                    self._send_json({"error": str(exc)}, 500)
                except Exception:
                    pass

        def _dispatch_get(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
                return
            if parsed.path == "/api/state":
                self._send_json(_state_payload(pipeline))
                return
            if parsed.path == "/api/browse":
                query = parse_qs(parsed.query)
                raw = unquote((query.get("path") or [""])[0]).strip()
                try:
                    self._send_json(_browse(pipeline, raw))
                except FileNotFoundError as exc:
                    self._send_json({"error": str(exc)}, 404)
                return
            if parsed.path == "/api/job":
                self._send_json(job.snapshot())
                return
            if parsed.path == "/api/papers":
                self._send_json({"papers": pipeline.store.list_papers()})
                return
            if parsed.path == "/api/overview":
                self._send_json(_overview_payload(pipeline))
                return
            self._send_json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            payload = self._read_json()
            if parsed.path == "/api/settings":
                backend = str(payload.get("backend") or "").strip().casefold()
                api_key = str(payload.get("api_key") or "").strip()
                base_url = str(payload.get("base_url") or "").strip()
                model_id = str(payload.get("model_id") or "").strip()
                parallel_sessions = payload.get("parallel_sessions")
                if backend == "external":
                    if not base_url:
                        base_url = str((pipeline.settings.get("external") or {}).get("base_url") or DEFAULT_EXTERNAL_BASE_URL)
                    if not model_id:
                        model_id = str((pipeline.settings.get("external") or {}).get("model_id") or DEFAULT_EXTERNAL_PRESETS[0]["id"])
                if backend in {"local", "external"}:
                    profile = load_profile(pipeline.root)
                    save_profile(
                        pipeline.root,
                        profile.get("enabled"),
                        profile.get("instructions") or {},
                        backend=backend,
                        parallel_sessions=parallel_sessions,
                    )
                env_updates: dict[str, str] = {}
                if api_key:
                    if backend == "external":
                        ext_cfg = pipeline.settings.get("external") or {}
                        presets = ext_cfg.get("presets") or DEFAULT_EXTERNAL_PRESETS
                        preset = next((p for p in presets if str(p.get("id") or "") == model_id), {})
                        key_name = str(preset.get("api_key_env") or ext_cfg.get("api_key_env") or "EXTERNAL_API_KEY")
                    else:
                        key_name = "CURSOR_API_KEY"
                    env_updates[key_name] = api_key
                    os.environ[key_name] = api_key
                if base_url:
                    env_updates["EXTERNAL_BASE_URL"] = base_url
                    os.environ["EXTERNAL_BASE_URL"] = base_url
                if model_id:
                    env_updates["EXTERNAL_MODEL_ID"] = model_id
                    os.environ["EXTERNAL_MODEL_ID"] = model_id
                if env_updates:
                    update_dotenv(pipeline.root, env_updates)
                if base_url or model_id:
                    ext = pipeline.settings.setdefault("external", {})
                    if base_url:
                        ext["base_url"] = base_url
                    if model_id:
                        ext["model_id"] = model_id
                zotero_export = str(payload.get("zotero_export") or "").strip()
                if zotero_export or "zotero_export" in payload:
                    zotero = pipeline.settings.setdefault("zotero", {})
                    zotero["export_path"] = zotero_export
                    pipeline._zotero_rows = None
                    pipeline._zotero_failed = False
                self._send_json({"ok": True, "state": _state_payload(pipeline)})
                return
            if parsed.path == "/api/profile":
                enabled = payload.get("enabled")
                instructions = payload.get("instructions") or {}
                save_profile(pipeline.root, enabled, instructions, backend=payload.get("backend"))
                self._send_json({"ok": True, "profile": load_profile(pipeline.root)})
                return
            if parsed.path == "/api/preview":
                paths = payload.get("paths") or []
                pdfs = collect_pdfs(paths)
                root = id_root_for(pdfs, pipeline.default_folder)
                self._send_json(
                    {
                        "count": len(pdfs),
                        "root": str(root),
                        "files": [pdf.name for pdf in pdfs[:80]],
                    }
                )
                return
            if parsed.path == "/api/run":
                snapshot = job.snapshot()
                if snapshot["status"] == "running":
                    self._send_json({"error": "a job is already running"}, 409)
                    return
                action = payload.get("action") or "extract"
                paths = payload.get("paths") or []
                if not paths:
                    self._send_json({"error": "select a folder or PDF first"}, 400)
                    return
                enabled = payload.get("enabled")
                instructions = payload.get("instructions") or {}
                save_profile(pipeline.root, enabled, instructions, backend=payload.get("backend"))
                thread = threading.Thread(
                    target=_run_job,
                    args=(pipeline, job, action, paths, payload),
                    daemon=True,
                )
                thread.start()
                self._send_json({"ok": True, "status": "running"})
                return
            if parsed.path == "/api/shutdown":
                self._send_json({"ok": True})
                server = ctx.get("server")
                if server is not None:
                    threading.Thread(target=server.shutdown, daemon=True).start()
                return
            if parsed.path == "/api/zotero/import":
                path = str(payload.get("path") or "").strip()
                json_text = str(payload.get("json") or "").strip()
                try:
                    result = pipeline.import_zotero(path=Path(path) if path else None, json_text=json_text or None)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
                self._send_json({"ok": True, **result})
                return
            if parsed.path == "/api/export":
                profile = load_profile(pipeline.root)
                schema = schema_from_profile(pipeline.schema, profile)
                path = pipeline.export(schema=schema)
                self._send_json({"ok": True, "path": str(path)})
                return
            if parsed.path == "/api/open":
                target = payload.get("path") or str(pipeline.output_path)
                _open_path(Path(target))
                self._send_json({"ok": True})
                return
            if parsed.path == "/api/cell":
                paper_id = str(payload.get("paper_id") or "").strip()
                field_id = str(payload.get("field_id") or "").strip()
                text = payload.get("text")
                if not paper_id or not field_id:
                    self._send_json({"error": "paper_id and field_id are required"}, 400)
                    return
                if text is None:
                    self._send_json({"error": "text is required"}, 400)
                    return
                if not pipeline.store.get_paper(paper_id):
                    self._send_json({"error": f"unknown paper {paper_id}"}, 404)
                    return
                pipeline.store.set_human_override(paper_id, field_id, str(text))
                row = pipeline.store.get_field(paper_id, field_id)
                self._send_json({"ok": True, "field": row, "text": effective_text(row)})
                return
            if parsed.path == "/api/resummarize":
                snapshot = job.snapshot()
                if snapshot["status"] == "running":
                    self._send_json({"error": "a job is already running"}, 409)
                    return
                paper_id = str(payload.get("paper_id") or "").strip()
                field_ids = payload.get("field_ids") or []
                if isinstance(field_ids, str):
                    field_ids = [field_ids]
                field_ids = [str(item) for item in field_ids if str(item).strip()]
                if not paper_id or not field_ids:
                    self._send_json({"error": "paper_id and field_ids are required"}, 400)
                    return
                if not pipeline.store.get_paper(paper_id):
                    self._send_json({"error": f"unknown paper {paper_id}"}, 404)
                    return
                if payload.get("backend"):
                    profile = load_profile(pipeline.root)
                    save_profile(
                        pipeline.root,
                        profile.get("enabled"),
                        profile.get("instructions") or {},
                        backend=payload.get("backend"),
                    )
                thread = threading.Thread(
                    target=_run_resummarize,
                    args=(pipeline, job, paper_id, field_ids, payload),
                    daemon=True,
                )
                thread.start()
                self._send_json({"ok": True, "status": "running"})
                return
            self._send_json({"error": "not found"}, 404)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _send_json(self, payload: dict, status: int = 200) -> None:
            blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(blob)

        def _send_file(self, path: Path, content_type: str) -> None:
            if not path.is_file():
                self._send_json({"error": "ui file missing"}, 500)
                return
            blob = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

    return Handler


def _state_payload(pipeline: Pipeline) -> dict:
    base = load_schema(pipeline.root / "config" / "rarf_schema.yaml")
    profile = load_profile(pipeline.root)
    status = _backend_status(pipeline, profile)
    return {
        "default_folder": str(pipeline.default_folder),
        "output_path": str(pipeline.output_path),
        "has_api_key": status["has_api_key"],
        "has_external_key": status["has_external_key"],
        "backend": status["backend"],
        "can_summarize": status["can_summarize"],
        "backend_message": status["backend_message"],
        "backends": status["backends"],
        "external_base_url": status.get("external_base_url", ""),
        "external_model_id": status.get("external_model_id", ""),
        "external_model_label": status.get("external_model_label", ""),
        "external_presets": status.get("external_presets") or DEFAULT_EXTERNAL_PRESETS,
        "parallel_sessions": status.get("parallel_sessions", 5),
        "parallel_options": PARALLEL_OPTIONS,
        "fields": base.as_dict(),
        "profile": profile,
        "drives": windows_drives(),
        "zotero": pipeline.settings.get("zotero") or {},
    }


def _backend_status(pipeline: Pipeline, profile: dict | None = None) -> dict:
    profile = profile or load_profile(pipeline.root)
    backend = str(profile.get("backend") or "local").casefold()
    ext = pipeline.settings.get("external") or {}
    base_url = (os.environ.get("EXTERNAL_BASE_URL") or ext.get("base_url") or "").strip()
    model_id = (os.environ.get("EXTERNAL_MODEL_ID") or ext.get("model_id") or "").strip()
    presets = ext.get("presets") or DEFAULT_EXTERNAL_PRESETS
    preset = next((p for p in presets if str(p.get("id") or "") == model_id), {})
    env_candidates: list[str] = []
    for candidate in (preset.get("api_key_env"), ext.get("api_key_env"), "EXTERNAL_API_KEY"):
        candidate = str(candidate or "").strip()
        if candidate and candidate not in env_candidates:
            env_candidates.append(candidate)
    env_name = env_candidates[0] if env_candidates else "EXTERNAL_API_KEY"
    has_api_key = bool(os.environ.get("CURSOR_API_KEY"))
    has_external_key = any(os.environ.get(name) for name in env_candidates)
    if backend == "external":
        local_host = any(token in base_url.casefold() for token in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))
        can_summarize = bool(base_url) and (has_external_key or local_host)
        missing = []
        if not has_external_key and not local_host:
            missing.append(env_name)
        if not base_url:
            missing.append("external.base_url")
        message = (
            "External API is selected but "
            + " and ".join(missing)
            + (" is missing. Extract still works." if len(missing) == 1 else " are missing. Extract still works.")
            if missing
            else ""
        )
    else:
        can_summarize = has_api_key
        message = "" if has_api_key else "CURSOR_API_KEY is not set. Extract still works."
    preset_label = next((item["label"] for item in presets if item.get("id") == model_id), model_id)
    return {
        "backend": backend,
        "has_api_key": has_api_key,
        "has_external_key": has_external_key,
        "external_base_url": base_url,
        "external_model_id": model_id,
        "external_model_label": preset_label,
        "external_presets": presets,
        "parallel_sessions": normalize_parallel_sessions(profile.get("parallel_sessions")),
        "parallel_options": PARALLEL_OPTIONS,
        "can_summarize": can_summarize,
        "backend_message": message,
        "backends": [
            {"id": "local", "label": "Local Cursor Agent"},
            {"id": "external", "label": "External API"},
        ],
    }


def _json_value(row: dict | None) -> object:
    if not row:
        return None
    raw = row.get("generated_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _overview_payload(pipeline: Pipeline) -> dict:
    profile = load_profile(pipeline.root)
    schema = schema_from_profile(pipeline.schema, profile)
    rows = []
    for paper in pipeline.store.list_papers():
        stored = pipeline.store.fields_for(paper["id"])
        cells = {}
        for spec in schema.fields:
            row = stored.get(spec.id)
            cells[spec.id] = {
                "text": effective_text(row),
                "status": (row or {}).get("status"),
                "confidence": (row or {}).get("confidence"),
                "source": (row or {}).get("source"),
                "value": _json_value(row),
            }
        rows.append(
            {
                "paper_id": paper["id"],
                "title": paper.get("title") or paper.get("relative_path") or paper["id"],
                "status": paper.get("status"),
                "scan_id": paper.get("scan_id"),
                "extracted_at": paper.get("extracted_at"),
                "meta_source": paper.get("meta_source"),
                "year": paper.get("year"),
                "publication": paper.get("publication"),
                "authors": paper.get("authors"),
                "folder": paper.get("folder"),
                "has_pdf": bool(paper.get("source_path")),
                "cells": cells,
            }
        )
    years = sorted({str(row.get("year")) for row in rows if row.get("year")}, reverse=True)
    publications = sorted({str(row.get("publication")) for row in rows if row.get("publication")})
    statuses = sorted({str(row.get("status")) for row in rows if row.get("status")})
    groups = sorted({str(row.get("folder")) for row in rows if row.get("folder")})
    return {
        "fields": schema.as_dict(),
        "rows": rows,
        "facets": {"years": years, "publications": publications, "statuses": statuses, "groups": groups},
    }


def _browse(pipeline: Pipeline, raw: str) -> dict:
    if not raw or raw in {".", "drives"}:
        return {
            "path": "",
            "parent": None,
            "dirs": windows_drives(),
            "files": [],
            "pdf_here": 0,
            "shortcuts": _shortcuts(pipeline),
        }
    listing = list_directory(Path(raw))
    listing["shortcuts"] = _shortcuts(pipeline)
    return listing


def _shortcuts(pipeline: Pipeline) -> list[dict[str, str]]:
    items = [
        {"name": "默认文献夹", "path": str(pipeline.default_folder)},
        {"name": "项目目录", "path": str(pipeline.root)},
    ]
    zotero = pipeline.default_folder.parent.parent if pipeline.default_folder.parent else None
    if zotero and zotero.exists():
        items.insert(1, {"name": "Zotero", "path": str(zotero)})
    return items


def _run_job(pipeline: Pipeline, job: JobState, action: str, paths: list[str], payload: dict) -> None:
    job.reset(action)
    tee = _LogTee(job, sys.stdout)
    previous = sys.stdout
    sys.stdout = tee
    try:
        profile = {
            "enabled": payload.get("enabled"),
            "instructions": payload.get("instructions") or {},
        }
        schema = schema_from_profile(pipeline.schema, profile)
        force = bool(payload.get("force"))
        if action in {"extract", "run"} or action == "summarize":
            # extract always precedes summarize
            ids = pipeline.extract_paths(paths, force=force)
            print(f"extracted {len(ids)} paper(s)")
        else:
            ids = []
        if action in {"summarize", "run"}:
            status = _backend_status(pipeline)
            if not status["can_summarize"]:
                raise ValueError(status["backend_message"] or "summarize backend is not configured")
            if not schema.fields:
                raise ValueError("select at least one dimension")
            ids = pipeline.summarize_paths(paths, force=force, schema=schema)
            print(f"summarized {len(ids)} paper(s)")
            selected_pdfs = collect_pdfs(paths)
            root = id_root_for(selected_pdfs, pipeline.default_folder)
            missing = []
            for pdf in selected_pdfs:
                paper_id = paper_id_for(pdf, root)
                if not pipeline.store.fields_for(paper_id):
                    missing.append(pdf.name)
            if missing:
                print(f"{len(missing)} selected paper(s) have no analysis:")
                for name in missing:
                    print(f"  missing {name}")
        exported = None
        if action == "run" or payload.get("export"):
            exported = str(pipeline.export(schema=schema))
            print(f"exported {exported}")
        job.finish({"ids": ids, "exported": exported, "count": len(ids)})
    except Exception as exc:
        traceback.print_exc()
        job.finish(error=str(exc))
    finally:
        sys.stdout = previous
        if tee.buf.strip():
            job.log(tee.buf)


def _run_resummarize(pipeline: Pipeline, job: JobState, paper_id: str, field_ids: list[str], payload: dict) -> None:
    job.reset("resummarize")
    tee = _LogTee(job, sys.stdout)
    previous = sys.stdout
    sys.stdout = tee
    try:
        profile = load_profile(pipeline.root)
        status = _backend_status(pipeline, profile)
        if not status["can_summarize"]:
            raise ValueError(status["backend_message"] or "summarize backend is not configured")
        schema = schema_from_profile(pipeline.schema, profile)
        extra = str(payload.get("extra_instruction") or "")
        override = payload.get("instruction_override")
        if payload.get("instructions"):
            override = payload.get("instructions")
        updated = pipeline.resummarize_fields(
            paper_id,
            field_ids,
            extra_instruction=extra,
            instruction_override=override,
            schema=schema,
        )
        print(f"replaced {len(updated)} field(s) on {paper_id}")
        job.finish({"paper_id": paper_id, "field_ids": updated})
    except Exception as exc:
        traceback.print_exc()
        job.finish(error=str(exc))
    finally:
        sys.stdout = previous
        if tee.buf.strip():
            job.log(tee.buf)


def _open_path(path: Path) -> None:
    path = Path(path)
    target = path if path.exists() else path.parent
    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
    else:
        webbrowser.open(target.as_uri())


def main() -> None:
    port = 8765
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    serve(port=port)


if __name__ == "__main__":
    main()
