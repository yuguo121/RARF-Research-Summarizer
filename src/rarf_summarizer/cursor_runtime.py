from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rarf_summarizer.json_util import first_complete_object, try_extract_json_object

PREFERRED_ID_CANDIDATES = (
    "cursor-grok-4.6-high",
    "grok-4.6-high",
    "cursor-grok-4.6-high-fast",
    "grok-4.6",
)


def _patch_windows_bridge_discovery() -> None:
    """cursor-sdk uses selectors on a pipe, which fails on Windows (WinError 10038)."""
    if sys.platform != "win32":
        return
    try:
        from cursor_sdk._bridge import READY_LINE_PREFIX, parse_discovery_line
        from cursor_sdk.errors import CursorSDKError
        import cursor_sdk._bridge as bridge_mod
    except Exception:
        return

    def _read_discovery(process, timeout: float):
        if process.stderr is None:
            raise CursorSDKError("Bridge process stderr is unavailable")
        lines: list[str] = []
        pending: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            try:
                for line in process.stderr:
                    pending.put(line)
                    if line.startswith(READY_LINE_PREFIX):
                        return
            except Exception:
                pass
            finally:
                pending.put(None)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                line = pending.get(timeout=min(0.2, remaining))
            except queue.Empty:
                if process.poll() is not None and not thread.is_alive():
                    break
                continue
            if line is None:
                break
            lines.append(line)
            discovery = parse_discovery_line(line)
            if discovery is not None:
                return discovery
        if process.poll() is not None:
            raise CursorSDKError(
                f"Bridge exited before discovery with status {process.poll()}: " + "".join(lines)
            )
        raise CursorSDKError("Timed out waiting for bridge discovery")

    bridge_mod._read_discovery = _read_discovery


_patch_windows_bridge_discovery()


class ModelNotAvailableError(RuntimeError):
    pass


class AgentStartupError(RuntimeError):
    pass


class AgentRunError(RuntimeError):
    pass


@dataclass
class AgentRunResult:
    text: str
    status: str
    agent_id: str | None = None
    run_id: str | None = None
    model: str | None = None


class AgentBackend(Protocol):
    def resolve_model(self) -> str: ...

    def run(self, prompt: str, *, session: str, work_dir: str) -> AgentRunResult: ...


def wait_with_timeout(run: Any, timeout: float) -> Any | None:
    box: queue.Queue[tuple[str, Any]] = queue.Queue()

    def worker() -> None:
        try:
            box.put(("ok", run.wait()))
        except Exception as exc:
            box.put(("err", exc))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None
    try:
        kind, value = box.get_nowait()
    except queue.Empty:
        return None
    if kind == "err":
        raise value
    return value


def _block_text(block: Any) -> str:
    if block is None:
        return ""
    if isinstance(block, str):
        return block
    if getattr(block, "type", None) == "text":
        return str(getattr(block, "text", "") or "")
    text = getattr(block, "text", None)
    if text:
        return str(text)
    if isinstance(block, dict):
        if block.get("type") == "text" or "text" in block:
            return str(block.get("text") or "")
    return ""


def message_text(message: Any) -> str:
    inner = getattr(message, "message", None)
    if inner is None:
        inner = message
    content = getattr(inner, "content", None)
    if content is None and isinstance(message, dict):
        nested = message.get("message")
        if isinstance(nested, dict):
            content = nested.get("content")
        content = content if content is not None else message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(_block_text(block) for block in content)


def collect_stream_text(run: Any, timeout: float = 720) -> str:
    iterator = getattr(run, "messages", None)
    if not callable(iterator):
        return ""
    parts: list[str] = []
    pending: queue.Queue[tuple[str, Any]] = queue.Queue()

    def reader() -> None:
        try:
            for message in iterator():
                pending.put(("msg", message))
            pending.put(("done", None))
        except Exception as exc:
            pending.put(("err", exc))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + max(0.05, timeout)
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            kind, value = pending.get(timeout=min(0.4, max(0.05, remaining)))
        except queue.Empty:
            continue
        if kind == "done":
            break
        if kind == "err":
            break
        chunk = message_text(value)
        if not chunk:
            continue
        parts.append(chunk)
        if first_complete_object("".join(parts)) is not None:
            break
    return "".join(parts)


def complete_agent_run(
    run: Any,
    *,
    wait_timeout: float = 90,
    stream_timeout: float = 720,
) -> AgentRunResult:
    streamed = collect_stream_text(run, timeout=stream_timeout)
    wait_result = None
    wait_error: Exception | None = None
    if try_extract_json_object(streamed) is None:
        try:
            wait_result = wait_with_timeout(run, wait_timeout)
        except Exception as exc:
            wait_error = exc
            wait_result = None
    text = streamed
    status = "finished"
    run_id = getattr(run, "id", None)
    model_used = None
    if wait_result is not None:
        status = str(getattr(wait_result, "status", None) or status)
        run_id = getattr(wait_result, "id", None) or run_id
        model_used = getattr(getattr(wait_result, "model", None), "id", None)
        waited_text = getattr(wait_result, "result", None) or ""
        if waited_text:
            text = str(waited_text)
        elif not text:
            text_fn = getattr(run, "text", None)
            if callable(text_fn):
                try:
                    text = text_fn() or text
                except Exception:
                    pass
    elif wait_error is not None and not text:
        text = str(wait_error)
        status = "error"
    elif wait_result is None and try_extract_json_object(streamed) is None and not streamed:
        status = "timeout"
    if try_extract_json_object(text) is not None:
        status = "finished"
    return AgentRunResult(
        text=str(text or ""),
        status=status,
        run_id=str(run_id) if run_id else None,
        model=str(model_used) if model_used else None,
    )


def _model_id(item: Any) -> str:
    return str(getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else "") or "")


def _model_variants(item: Any) -> list[Any]:
    variants = getattr(item, "variants", None)
    if variants is None and isinstance(item, dict):
        variants = item.get("variants") or []
    return list(variants or [])


def _variant_label(variant: Any) -> str:
    for attr in ("display_name", "name", "id", "label"):
        value = getattr(variant, attr, None)
        if value:
            return str(value)
    if isinstance(variant, dict):
        return str(variant.get("display_name") or variant.get("name") or variant.get("id") or "")
    return str(variant)


def _variant_params(variant: Any) -> list[Any]:
    params = getattr(variant, "params", None)
    if params is None and isinstance(variant, dict):
        params = variant.get("params")
    return list(params or [])


def resolve_grok_46_high(catalog: list[Any], settings: dict[str, Any]) -> tuple[str, list[Any]]:
    """Return (model_id, params) for Cursor Grok 4.6 High. Never silently fall back."""
    model_cfg = settings.get("model") or {}
    required_substrings = [s.casefold() for s in model_cfg.get("required_id_substrings") or ["grok", "4.6"]]
    required_tokens = [s.casefold() for s in model_cfg.get("required_label_tokens") or ["high"]]

    catalog_ids = [_model_id(item) for item in catalog]
    grok_models = [
        item
        for item in catalog
        if all(token in _model_id(item).casefold() for token in required_substrings)
    ]
    if not grok_models:
        raise ModelNotAvailableError(
            "Cursor Grok 4.6 High is not in the SDK catalog. "
            f"Available model IDs: {', '.join(catalog_ids) or '(none)'}"
        )

    for candidate_id in PREFERRED_ID_CANDIDATES:
        for item in grok_models:
            if _model_id(item).casefold() == candidate_id.casefold():
                params = _high_params(item, required_tokens)
                if params or "high" in candidate_id.casefold():
                    return _model_id(item), params

    scored: list[tuple[int, str, list[Any]]] = []
    for item in grok_models:
        model_id = _model_id(item)
        blob = model_id.casefold()
        variants = _model_variants(item)
        for variant in variants:
            blob += " " + _variant_label(variant).casefold()
        params = _high_params(item, required_tokens)
        if params:
            blob += " high"
        score = sum(blob.count(token) for token in required_tokens) + ("high" in model_id.casefold())
        if all(token in blob for token in required_tokens) or params:
            scored.append((int(score), model_id, params))

    if not scored:
        raise ModelNotAvailableError(
            "Found Grok 4.6 in the catalog, but no High effort/preset. "
            f"Grok IDs: {', '.join(_model_id(item) for item in grok_models)}"
        )
    scored.sort(reverse=True)
    return scored[0][1], scored[0][2]


def _high_params(item: Any, required_tokens: list[str]) -> list[Any]:
    for variant in _model_variants(item):
        label = _variant_label(variant).casefold()
        if all(token in label for token in required_tokens):
            return _variant_params(variant)
    parameters = getattr(item, "parameters", None) or (item.get("parameters") if isinstance(item, dict) else None) or []
    params = []
    for parameter in parameters:
        param_id = str(getattr(parameter, "id", None) or (parameter.get("id") if isinstance(parameter, dict) else "") or "")
        values = getattr(parameter, "values", None) or (parameter.get("values") if isinstance(parameter, dict) else None) or []
        labels = " ".join(
            str(getattr(value, "value", None) or getattr(value, "display_name", None) or value).casefold()
            for value in values
        )
        if "effort" in param_id.casefold() or "reasoning" in param_id.casefold():
            if any(token in labels for token in required_tokens) or "high" in labels:
                try:
                    from cursor_sdk import ModelParameterValue

                    params.append(ModelParameterValue(id=param_id, value="high"))
                except Exception:
                    params.append({"id": param_id, "value": "high"})
    return params


class CursorSdkBackend:
    def __init__(self, settings: dict[str, Any], api_key: str | None = None):
        self.settings = settings
        self.api_key = api_key
        self._resolved: tuple[str, list[Any]] | None = None
        self._client = None

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            client.close()

    def _ensure_client(self, work_dir: str):
        if self._client is not None:
            return self._client
        from cursor_sdk import Client

        self._client = Client.launch_bridge(workspace=work_dir)
        return self._client

    def resolve_model(self) -> str:
        model_id, _ = self._selection()
        return model_id

    def _selection(self) -> tuple[str, list[Any]]:
        if self._resolved:
            return self._resolved
        try:
            root = str(self.settings.get("_project_root") or ".")
            client = self._ensure_client(root)
            catalog = list(client.models.list(api_key=self.api_key))
        except Exception as exc:
            raise AgentStartupError(f"failed to list Cursor models: {exc}") from exc
        self._resolved = resolve_grok_46_high(catalog, self.settings)
        return self._resolved

    def run(self, prompt: str, *, session: str, work_dir: str) -> AgentRunResult:
        try:
            from cursor_sdk import Agent, AgentOptions, LocalAgentOptions, ModelSelection
        except ImportError as exc:
            raise AgentStartupError("cursor-sdk is not installed") from exc

        model_id, params = self._selection()
        try:
            from cursor_sdk import ModelParameterValue

            converted = []
            for param in params or []:
                if isinstance(param, dict):
                    converted.append(ModelParameterValue(id=str(param["id"]), value=str(param["value"])))
                else:
                    converted.append(param)
            model = ModelSelection(id=model_id, params=converted)
        except TypeError:
            model = model_id
        tools = (self.settings.get("cursor") or {}).get("tools")
        if tools is None:
            tools = []
        options = AgentOptions(
            api_key=self.api_key,
            model=model,
            local=LocalAgentOptions(cwd=work_dir),
            tools=list(tools),
        )
        wait_timeout = float(
            (self.settings.get("cursor") or {}).get("wait_timeout_seconds")
            or self.settings.get("wait_timeout_seconds")
            or 90
        )
        stream_timeout = float(
            (self.settings.get("cursor") or {}).get("stream_timeout_seconds")
            or self.settings.get("stream_timeout_seconds")
            or 720
        )
        try:
            client = self._ensure_client(work_dir)
            with Agent.create(options, client=client) as agent:
                run = agent.send(prompt)
                completed = complete_agent_run(
                    run,
                    wait_timeout=wait_timeout,
                    stream_timeout=stream_timeout,
                )
                log_path = Path(work_dir) / f"{session.replace(':', '_')}.run.log"
                try:
                    log_path.write_text((completed.text or "")[:200000], encoding="utf-8")
                except Exception:
                    pass
                agent_id = getattr(agent, "agent_id", None)
                run_id = completed.run_id or getattr(run, "id", None)
                model_used = completed.model or model_id
                if try_extract_json_object(completed.text) is None:
                    raise AgentRunError(
                        f"{session} run failed: {run_id} {(completed.text or '')[:2000]}".strip()
                    )
                return AgentRunResult(
                    text=str(completed.text),
                    status="finished",
                    agent_id=str(agent_id) if agent_id else None,
                    run_id=str(run_id) if run_id else None,
                    model=str(model_used),
                )
        except AgentRunError:
            raise
        except Exception as exc:
            name = type(exc).__name__
            if "CursorAgentError" in name or exc.__class__.__name__ == "CursorAgentError":
                raise AgentStartupError(f"{session} did not start: {exc}") from exc
            try:
                from cursor_sdk import CursorAgentError as _CursorAgentError

                if isinstance(exc, _CursorAgentError):
                    raise AgentStartupError(f"{session} did not start: {exc}") from exc
            except ImportError:
                pass
            raise AgentStartupError(f"{session} failed to execute: {exc}") from exc


class FakeBackend:
    def __init__(self, responses: dict[str, str], model: str = "cursor-grok-4.6-high"):
        self.responses = responses
        self.model = model
        self.calls: list[str] = []

    def resolve_model(self) -> str:
        return self.model

    def run(self, prompt: str, *, session: str, work_dir: str) -> AgentRunResult:
        self.calls.append(session)
        if session not in self.responses:
            raise AgentRunError(f"no fake response for session {session}")
        return AgentRunResult(
            text=self.responses[session],
            status="finished",
            agent_id="agent-fake",
            run_id=f"run-{session}",
            model=self.model,
        )


class ExternalChatBackend:
    """OpenAI-compatible chat/completions. Used only when the Desk selects External API."""

    def __init__(self, settings: dict[str, Any], api_key: str):
        self.settings = settings
        self.api_key = api_key

    def resolve_model(self) -> str:
        ext = self.settings.get("external") or {}
        model_id = str(ext.get("model_id") or "").strip()
        if not model_id:
            raise AgentStartupError("external.model_id is not set")
        return model_id

    def run(self, prompt: str, *, session: str, work_dir: str) -> AgentRunResult:
        import json
        import urllib.error
        import urllib.request

        ext = self.settings.get("external") or {}
        base = str(ext.get("base_url") or "").rstrip("/")
        if not base:
            raise AgentStartupError("external.base_url is not set")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        model_id = self.resolve_model()
        presets = ext.get("presets") or []
        preset = next((p for p in presets if str(p.get("id") or "") == model_id), {})
        temperature = preset.get("temperature", ext.get("temperature"))
        top_p = preset.get("top_p", ext.get("top_p"))
        body: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(temperature) if temperature is not None else 0,
        }
        if top_p is not None:
            body["top_p"] = float(top_p)
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        timeout = float(self.settings.get("stream_timeout_seconds") or 720)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise AgentRunError(f"{session} external API HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise AgentStartupError(f"{session} external API failed: {exc}") from exc
        choices = body.get("choices") or []
        message = (choices[0].get("message") if choices else {}) or {}
        text = message.get("content") or body.get("text") or ""
        log_path = Path(work_dir) / f"{session.replace(':', '_')}.run.log"
        try:
            Path(work_dir).mkdir(parents=True, exist_ok=True)
            log_path.write_text(str(text)[:200000], encoding="utf-8")
        except Exception:
            pass
        if try_extract_json_object(str(text)) is None:
            raise AgentRunError(f"{session} external API returned no JSON object")
        return AgentRunResult(
            text=str(text),
            status="finished",
            agent_id="external",
            run_id=str(body.get("id") or f"ext-{session}"),
            model=model_id,
        )


def _is_local_base(base_url: str) -> bool:
    host = base_url.casefold()
    return any(token in host for token in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))


def make_backend(settings: dict[str, Any], *, name: str | None = None, injected=None):
    if injected is not None:
        return injected
    import os

    merged = dict(settings)
    ext = dict(merged.get("external") or {})
    if os.environ.get("EXTERNAL_BASE_URL"):
        ext["base_url"] = os.environ["EXTERNAL_BASE_URL"].strip()
    if os.environ.get("EXTERNAL_MODEL_ID"):
        ext["model_id"] = os.environ["EXTERNAL_MODEL_ID"].strip()
    merged["external"] = ext
    kind = (name or (merged.get("model") or {}).get("backend") or "local").casefold()
    if kind == "external":
        env_name = str(ext.get("api_key_env") or "EXTERNAL_API_KEY")
        api_key = os.environ.get(env_name) or os.environ.get("EXTERNAL_API_KEY")
        base_url = str(ext.get("base_url") or "").strip()
        if not base_url:
            raise AgentStartupError("external.base_url is not set")
        if not api_key and not _is_local_base(base_url):
            raise AgentStartupError(f"{env_name} is not set")
        backend = ExternalChatBackend(merged, api_key or "")
        backend.resolve_model()
        return backend
    api_key = os.environ.get("CURSOR_API_KEY")
    if not api_key:
        raise AgentStartupError("CURSOR_API_KEY is not set")
    return CursorSdkBackend(merged, api_key=api_key)
