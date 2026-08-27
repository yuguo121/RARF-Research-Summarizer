from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from rarf_summarizer.cursor_runtime import (
    AgentStartupError,
    CursorSdkBackend,
    ExternalChatBackend,
    ModelNotAvailableError,
    collect_stream_text,
    complete_agent_run,
    make_backend,
    resolve_grok_46_high,
    wait_with_timeout,
)


class DummyVariant:
    def __init__(self, name, params=None):
        self.display_name = name
        self.params = params or []


class DummyModel:
    def __init__(self, model_id, variants=None, parameters=None):
        self.id = model_id
        self.variants = variants or []
        self.parameters = parameters or []


def test_resolves_grok_46_high_from_catalog():
    catalog = [
        DummyModel("composer-2.5"),
        DummyModel(
            "cursor-grok-4.6",
            variants=[DummyVariant("High", params=[{"id": "reasoning", "value": "high"}])],
        ),
    ]
    model_id, params = resolve_grok_46_high(catalog, {"model": {"required_id_substrings": ["grok", "4.6"], "required_label_tokens": ["high"]}})
    assert "grok" in model_id.casefold()
    assert "4.6" in model_id.casefold()
    assert params


def test_prefers_explicit_high_id():
    catalog = [
        DummyModel("cursor-grok-4.6-high"),
        DummyModel("composer-2.5"),
    ]
    model_id, _ = resolve_grok_46_high(catalog, {})
    assert model_id == "cursor-grok-4.6-high"


def test_does_not_fall_back_to_other_families():
    catalog = [DummyModel("composer-2.5"), DummyModel("gpt-5")]
    with pytest.raises(ModelNotAvailableError, match="not in the SDK catalog"):
        resolve_grok_46_high(catalog, {})


class _TextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _AssistantMessage:
    def __init__(self, text: str):
        self.message = SimpleNamespace(content=(_TextBlock(text),))


class _DummyRun:
    def __init__(self, chunks, hang: bool = False, wait_error: bool = False):
        self.id = "run-dummy"
        self.chunks = chunks
        self.hang = hang
        self.wait_error = wait_error
        self.wait_calls = 0

    def messages(self):
        for chunk in self.chunks:
            yield _AssistantMessage(chunk)

    def wait(self):
        self.wait_calls += 1
        if self.hang:
            time.sleep(5)
        if self.wait_error:
            raise RuntimeError("wait failed")
        return SimpleNamespace(status="error", result="", id="wait-id", model=None)


def test_wait_timeout_returns_none():
    run = _DummyRun([], hang=True)
    assert wait_with_timeout(run, 0.05) is None
    assert run.wait_calls == 1


def test_complete_run_uses_streamed_json_without_wait():
    payload = '{"citation": {"status": "present", "value": "x"}, "framing": {"status": "present"}}'
    run = _DummyRun([payload], wait_error=True)
    result = complete_agent_run(run, wait_timeout=0.05)
    assert run.wait_calls == 0
    assert result.status == "finished"
    assert "citation" in result.text


def test_complete_run_survives_wait_error_if_stream_has_json():
    run = _DummyRun(['{"a": 1}'], hang=True)
    result = complete_agent_run(run, wait_timeout=0.05)
    assert result.status == "finished"
    assert '"a"' in result.text


def test_stream_collection_times_out():
    class HangStream:
        id = "run-hang"

        def messages(self):
            while True:
                time.sleep(1)
                yield _AssistantMessage("still thinking")

        def wait(self):
            time.sleep(5)

    started = time.monotonic()
    text = collect_stream_text(HangStream(), timeout=0.2)
    assert time.monotonic() - started < 2
    assert "thinking" in text or text == ""


def _external_settings(model_id="grok-4.6"):
    return {
        "model": {"backend": "external", "required_id_substrings": ["grok", "4.6"]},
        "external": {"base_url": "https://api.example.com/v1", "model_id": model_id},
    }


def test_make_backend_local_uses_cursor_sdk(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-test-key")
    backend = make_backend({"model": {"backend": "local"}})
    assert isinstance(backend, CursorSdkBackend)


def test_make_backend_external_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv("EXTERNAL_API_KEY", raising=False)
    monkeypatch.delenv("EXAMPLE_API_KEY", raising=False)
    with pytest.raises(AgentStartupError, match="EXAMPLE_API_KEY"):
        make_backend(_external_settings(), name="external")


def test_make_backend_external_missing_base_url_fails_closed(monkeypatch):
    monkeypatch.setenv("EXTERNAL_API_KEY", "ext-key")
    monkeypatch.delenv("EXTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("EXTERNAL_MODEL_ID", raising=False)
    settings = _external_settings()
    settings["external"]["base_url"] = ""
    with pytest.raises(AgentStartupError, match="base_url"):
        make_backend(settings, name="external")


def test_make_backend_external_accepts_deepseek_flash(monkeypatch):
    monkeypatch.setenv("EXTERNAL_API_KEY", "ext-key")
    monkeypatch.delenv("EXTERNAL_MODEL_ID", raising=False)
    backend = make_backend(_external_settings("deepseek-v4-flash"), name="external")
    assert isinstance(backend, ExternalChatBackend)
    assert backend.resolve_model() == "deepseek-v4-flash"


def test_make_backend_external_accepts_deepseek_pro(monkeypatch):
    monkeypatch.setenv("EXTERNAL_API_KEY", "ext-key")
    monkeypatch.delenv("EXTERNAL_MODEL_ID", raising=False)
    backend = make_backend(_external_settings("deepseek-v4-pro"), name="external")
    assert backend.resolve_model() == "deepseek-v4-pro"


def test_make_backend_external_prefers_provider_key_env(monkeypatch):
    monkeypatch.delenv("EXTERNAL_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_BASE_URL", raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-key")
    settings = _external_settings("glm-5.3-flash")
    settings["external"]["presets"] = [
        {
            "id": "glm-5.3-flash",
            "label": "GLM",
            "provider": "zhipu",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
        }
    ]
    backend = make_backend(settings, name="external")
    assert isinstance(backend, ExternalChatBackend)
    assert backend.api_key == "zhipu-key"


def test_make_backend_external_provider_base_url_env_override(monkeypatch):
    monkeypatch.delenv("EXTERNAL_API_KEY", raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-key")
    monkeypatch.setenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    settings = _external_settings("glm-5.3-flash")
    settings["external"]["presets"] = [{"id": "glm-5.3-flash", "provider": "zhipu"}]
    backend = make_backend(settings, name="external")
    assert backend.settings["external"]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"


def test_make_backend_external_missing_provider_key_names_vendor_env(monkeypatch):
    monkeypatch.delenv("EXTERNAL_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_BASE_URL", raising=False)
    settings = _external_settings("glm-5.3-flash")
    settings["external"]["presets"] = [{"id": "glm-5.3-flash", "provider": "zhipu"}]
    with pytest.raises(AgentStartupError, match="ZHIPU_API_KEY"):
        make_backend(settings, name="external")


def test_external_backend_salvages_json(monkeypatch, tmp_path):
    monkeypatch.setenv("EXTERNAL_API_KEY", "ext-key")
    monkeypatch.delenv("EXTERNAL_MODEL_ID", raising=False)
    backend = make_backend(_external_settings(), name="external")
    assert isinstance(backend, ExternalChatBackend)

    class DummyResp:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=0):
        return DummyResp({"id": "chatcmpl-1", "choices": [{"message": {"content": '{"citation": {"status": "present"}}'}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = backend.run("prompt", session="theory", work_dir=str(tmp_path))
    assert result.status == "finished"
    assert "citation" in result.text
