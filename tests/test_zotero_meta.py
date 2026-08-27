from __future__ import annotations

import json

from rarf_summarizer.zotero_meta import load_zotero_export


def test_load_zotero_export_parses_csl_json(tmp_path):
    csl = [
        {
            "id": "chatterji2010",
            "type": "article-journal",
            "title": "How firms respond to being rated",
            "author": [
                {"family": "Chatterji", "given": "Aaron K."},
                {"family": "Toffel", "given": "Michael W."},
            ],
            "container-title": "Strategic Management Journal",
            "issued": {"date-parts": [[2010, 8]]},
            "DOI": "10.1002/smj.840",
            "volume": "31",
            "issue": "9",
            "page": "917-945",
        }
    ]
    path = tmp_path / "export.json"
    path.write_text(json.dumps(csl), encoding="utf-8")
    rows = load_zotero_export(path)
    assert len(rows) == 1
    meta = rows[0]
    assert meta.title == "How firms respond to being rated"
    assert meta.authors == "Aaron K. Chatterji; Michael W. Toffel"
    assert meta.year == "2010"
    assert meta.doi == "10.1002/smj.840"
    assert meta.publication == "Strategic Management Journal"
    assert meta.volume == "31"
    assert meta.issue == "9"
    assert meta.pages == "917-945"
    assert meta.item_key == "chatterji2010"


def test_load_zotero_export_parses_native_json(tmp_path):
    native = [
        {
            "key": "ABCD1234",
            "data": {
                "title": "Some study",
                "creators": [{"firstName": "Jane", "lastName": "Doe"}],
                "date": "2021-03-01",
                "DOI": "10.0000/xyz.123",
                "publicationTitle": "Academy of Management Journal",
                "volume": "64",
                "issue": "2",
                "pages": "100-130",
            },
        }
    ]
    path = tmp_path / "export.json"
    path.write_text(json.dumps(native), encoding="utf-8")
    rows = load_zotero_export(path)
    assert len(rows) == 1
    meta = rows[0]
    assert meta.authors == "Jane Doe"
    assert meta.year == "2021"
    assert meta.publication == "Academy of Management Journal"
    assert meta.pages == "100-130"
    assert meta.item_key == "ABCD1234"
