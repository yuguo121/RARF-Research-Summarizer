from __future__ import annotations

from rarf_summarizer.app import WEB_DIR, _backend_status, _overview_payload
from rarf_summarizer.dimension_profile import load_profile, schema_from_profile
from rarf_summarizer.pipeline import Pipeline
from rarf_summarizer.storage import Store


def test_desk_html_has_backend_picker_and_cell_drawer():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="settingsBtn"' in html
    assert 'id="modeLocal"' in html
    assert 'id="modeExternal"' in html
    assert 'id="modelCards"' in html
    assert "glm-5.3-flash" in html
    assert "deepseek-v4-flash" in html
    assert "deepseek-v4-pro" in html
    assert "open.bigmodel.cn" in html
    assert 'id="fgParallel"' in html
    assert "&lt;5" in html
    assert "&lt;100" in html
    assert 'id="overview"' in html
    assert "保存人工稿" in html
    assert "按此提问词重跑并替换" in html
    assert "id=\"drawer\"" in html
    assert "全选当前 PDF" in html


def test_backend_status_external_missing_key_disables_summarize(monkeypatch):
    pipeline = Pipeline()
    monkeypatch.delenv("EXTERNAL_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("EXTERNAL_MODEL_ID", raising=False)
    status = _backend_status(pipeline, {"backend": "external", "enabled": [], "instructions": {}})
    assert status["backend"] == "external"
    assert status["can_summarize"] is False
    model_id = status["external_model_id"]
    presets = status["external_presets"]
    expected_env = next(
        (f"{str(p.get('provider')).upper()}_API_KEY" for p in presets if p.get("id") == model_id and p.get("provider")),
        "EXTERNAL_API_KEY",
    )
    assert expected_env in status["backend_message"]


def test_parallel_workers_respects_provider_cap(monkeypatch):
    pipeline = Pipeline()
    pipeline.settings = {
        "external": {
            "model_id": "glm-5.3-flash",
            "presets": [{"id": "glm-5.3-flash", "provider": "zhipu", "max_parallel": 10}],
        }
    }
    monkeypatch.setattr(
        "rarf_summarizer.pipeline.load_profile",
        lambda root: {"backend": "external", "parallel_sessions": 100},
    )
    assert pipeline._parallel_workers() == 10
    pipeline.settings["external"]["presets"] = [{"id": "glm-5.3-flash", "provider": "zhipu"}]
    assert pipeline._parallel_workers() == 99


def test_overview_uses_effective_text(tmp_path):
    pipeline = Pipeline()
    pipeline.store = Store(tmp_path / "rarf.sqlite")
    field_id = schema_from_profile(pipeline.schema, load_profile(pipeline.root)).field_ids[0]
    pipeline.store.upsert_paper(
        {
            "id": "p1",
            "source_path": str(tmp_path / "a.pdf"),
            "relative_path": "a.pdf",
            "folder": ".",
            "file_hash": "h",
            "title": "Demo paper",
            "authors": None,
            "year": None,
            "doi": None,
            "page_count": 1,
            "warnings": "[]",
            "extracted_at": None,
            "status": "summarized",
        }
    )
    pipeline.store.upsert_field(
        "p1",
        field_id,
        {"status": "present", "confidence": 0.8, "generated_text": "generated cite", "generated_json": "{}"},
    )
    pipeline.store.set_human_override("p1", field_id, "human cite")
    data = _overview_payload(pipeline)
    assert data["rows"]
    enabled_ids = [field["id"] for field in data["fields"]]
    assert field_id in enabled_ids
    cell = data["rows"][0]["cells"][field_id]
    assert cell["text"] == "human cite"
    assert cell["source"] == "human"
