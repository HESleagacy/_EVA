from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pptx_forensics import extract_pptx

from test_extractor import _feature_package


REMOVED_KEYS = {
    "sha256",
    "source_sha256",
    "raw_style",
    "resolved_style",
    "geometry",
    "bbox_emu",
    "transform_chain",
    "xml_path",
    "xml_part",
    "evidence_refs",
    "failure_class",
    "failure_classes",
    "missing_evidence",
    "flow_candidate",
    "words",
    "paragraph",
    "metadata",
    "model",
    "usage",
    "estimated_cost_usd",
    "request_seconds",
    "cache_hit",
    "engine",
    "engine_version",
    "adapter",
}


def _assert_compact(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in REMOVED_KEYS
            assert not key.endswith(("_sha256", "_emu"))
            _assert_compact(item)
    elif isinstance(value, list):
        for item in value:
            _assert_compact(item)


def test_semantic_json_keeps_content_and_removes_pipeline_details(tmp_path: Path) -> None:
    source = tmp_path / "feature.pptx"
    _feature_package(source)

    report = extract_pptx(source)
    payload = report.to_semantic_dict()

    _assert_compact(payload)
    assert set(payload) == {
        "schema_version",
        "deck",
        "summary",
        "slides",
        "objects",
        "assets",
        "relationships",
        "visual_regions",
        "diagrams",
        "ocr",
        "vision",
        "comments",
        "warnings",
    }
    chart = next(item for item in payload["objects"] if item["type"] == "chart")
    assert chart["text"] == ""
    assert chart["chart_data"]["series"][0]["values"] == [
        {"index": "0", "value": "10"},
        {"index": "1", "value": "20"},
    ]
    assert payload["diagrams"][0]["edges"][0]["semantic_status"] == "uncertain"
    assert payload["slides"][0]["flow"] == {"direction": "unknown", "present": True}
    assert json.loads(report.to_json()) == payload


def test_markdown_output_is_descriptive_and_written_with_evidence(tmp_path: Path) -> None:
    source = tmp_path / "feature.pptx"
    evidence = tmp_path / "evidence"
    _feature_package(source)

    report = extract_pptx(source, evidence)
    markdown = report.to_markdown()

    assert markdown.startswith("# Parsed Presentation\n")
    assert "## Slides" in markdown
    assert "Chart contents" in markdown
    assert "SmartArt contents" in markdown
    assert "## Relationships" in markdown
    assert "flow_candidate" not in markdown
    assert "raw_style" not in markdown
    assert sorted(path.name for path in evidence.iterdir()) == ["original.pptx", "parts"]
