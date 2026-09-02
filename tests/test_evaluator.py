from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pptx_forensics import (
    DECK_QUALITY_WEIGHT,
    EVALUATOR_SCHEMA_VERSION,
    PROPOSAL_STRENGTH_WEIGHT,
    RUBRIC_VERSION,
    SEMANTIC_COMPONENTS,
    compute_deterministic_metrics,
    evaluate_deck,
    extract_pptx,
    reconstruct_raster_diagrams,
    validate_problem_statement,
)
from pptx_forensics.evaluate_cli import main as evaluate_cli_main

from test_extractor import _feature_package


FORBIDDEN_KEYS = {
    "relationships",
    "notes",
    "alt_text",
    "animations",
    "raw_style",
    "resolved_style",
    "geometry",
    "words",
    "lines",
    "nodes",
    "edges",
    "rendered_evidence",
    "ocr_evidence",
    "vision_evidence",
    "provenance",
    "warnings",
    "metadata",
}


def _assert_evaluator_boundary(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in FORBIDDEN_KEYS
            _assert_evaluator_boundary(item)
    elif isinstance(value, list):
        for item in value:
            _assert_evaluator_boundary(item)


def test_evaluator_ir_is_compact_and_deduplicates_external_links(tmp_path: Path) -> None:
    source = tmp_path / "evaluator.pptx"
    _feature_package(source)
    report = extract_pptx(source)
    report.canonical.slides[0]["hyperlinks"] = [
        {"target": "https://example.test", "kind": "external"},
        {"resolved_target": "https://example.test", "kind": "external"},
        {"target": "#slide-2", "kind": "internal"},
    ]
    image = next(item for item in report.canonical.objects if item.get("type") == "image")
    report.canonical.objects.append({**image, "id": "duplicate-image", "bbox": [0.72, 0.01, 0.2, 0.2]})

    payload = report.to_evaluator_dict()

    assert payload["schema_version"] == EVALUATOR_SCHEMA_VERSION
    assert set(payload) == {"schema_version", "deck", "slides"}
    assert payload["deck"]["links"] == [
        {"kind": "external", "slides": [1], "target": "https://example.test"}
    ]
    assert payload["slides"][0]["visible_text"]
    assert payload["slides"][0]["text_blocks"]
    assert payload["slides"][0]["metrics"]["meaningful_image_count"] == 1
    assert len(payload["slides"][0]["visual_evidence"]) == 1
    assert len(payload["deck"]["media"]) == 1
    _assert_evaluator_boundary(payload)
    assert json.loads(report.to_evaluator_json()) == payload


def test_raster_candidates_are_referenced_but_never_projected_as_graphs(tmp_path: Path) -> None:
    source = tmp_path / "raster-candidate.pptx"
    _feature_package(source)
    report = extract_pptx(source)
    role_record = next(
        record
        for record in report.canonical.rendered_evidence
        if record.get("value", {}).get("type") == "image_role_candidate"
        and record["value"].get("asset_id") == "asset-0002"
    )
    role_record["value"]["image_role"] = "diagram"
    role_record["value"]["role"] = "diagram"

    records = reconstruct_raster_diagrams(
        report,
        source,
        slides=[1],
        asset_ids=["asset-0002"],
        run_ocr_stage=False,
        skip_ocr=True,
    )
    assert records

    payload = report.to_evaluator_dict()
    image = next(item for item in payload["slides"][0]["visual_evidence"] if item["asset_id"] == "asset-0002")
    assert image["role"] == "diagram_candidate"
    assert image["needs_vision_review"] is True
    assert image["raw_evidence_ref"]["diagram_candidate_id"] is None
    assert "nodes" not in image
    assert "edges" not in image
    _assert_evaluator_boundary(payload)


def test_logo_assets_are_excluded_from_meaningful_evaluator_evidence(tmp_path: Path) -> None:
    source = tmp_path / "logo-gate.pptx"
    _feature_package(source)
    report = extract_pptx(source)
    role_record = next(
        record
        for record in report.canonical.rendered_evidence
        if record.get("value", {}).get("type") == "image_role_candidate"
        and record["value"].get("asset_id") == "asset-0002"
    )
    role_record["value"]["image_role"] = "logo"

    payload = report.to_evaluator_dict()
    assert payload["slides"][0]["visual_evidence"] == []
    assert payload["deck"]["media"] == []


PROBLEM = {
    "id": "PS-1",
    "title": "Make systems safer",
    "description": "Provide a practical system hardening solution.",
    "requirements": [
        {"id": "r1", "description": "Explain the threat", "weight": 2},
        {"id": "r2", "description": "Show the solution", "weight": 1},
    ],
}


def _semantic_response(score: float = 80.0) -> dict[str, Any]:
    return {
        "schema_version": "deck-semantic-evaluation-1.0",
        "scores": {
            key: {
                "score": score,
                "confidence": 0.9,
                "evidence_slides": [1],
                "explanation": f"Evidence supports {key}.",
                "missing_evidence": [],
            }
            for key in SEMANTIC_COMPONENTS
        },
    }


def test_problem_statement_weights_are_normalized() -> None:
    problem = validate_problem_statement(PROBLEM)

    assert problem.id == "PS-1"
    assert sum(item["weight"] for item in problem.requirements) == 1.0
    assert problem.requirements[0]["weight"] > problem.requirements[1]["weight"]


def test_deck_evaluation_aggregates_groups_and_final_score(tmp_path: Path) -> None:
    source = tmp_path / "score.pptx"
    _feature_package(source)
    report = extract_pptx(source)

    class Adapter:
        name = "mock"
        version = "mock-1"

        def analyze(self, prompt: str, images: list[Any], timeout: float) -> dict[str, Any]:
            assert "Make systems safer" in prompt
            assert not images
            return _semantic_response()

    result = evaluate_deck(report, PROBLEM, semantic_adapter=Adapter(), semantic_cache_dir=tmp_path / "cache")
    proposal = result["scores"]["proposal_strength"]
    deck_quality = result["scores"]["deck_quality"]

    assert set(result["metrics"]) >= {
        "title_coverage",
        "text_density",
        "small_text_ratio",
        "overlap_ratio",
        "clipping_rate",
        "content_density_variation",
        "slide_type_coverage",
        "evidence_visibility",
        "duplicate_content",
        "link_prototype_evidence",
        "paragraph_content_ratio",
        "pointer_content_ratio",
        "paragraph_heavy_slide_ratio",
        "ambiguous_claim_ratio",
        "visual_coverage",
        "whitespace_area_ratio",
        "largest_empty_region_ratio",
        "space_usage",
    }
    assert proposal["score"] == 76.0
    assert result["final_score"] == round(
        PROPOSAL_STRENGTH_WEIGHT * proposal["score"] + DECK_QUALITY_WEIGHT * deck_quality["score"],
        6,
    )
    assert result["rubric_version"] == RUBRIC_VERSION
    assert len(result["evaluation_fingerprint"]) == 64
    for group in result["scores"].values():
        assert {"score", "confidence", "evidence_slides", "explanation", "missing_evidence"} <= set(group)


def test_missing_evidence_penalizes_component_and_weighted_scores(tmp_path: Path) -> None:
    source = tmp_path / "missing-evidence.pptx"
    _feature_package(source)
    report = extract_pptx(source)

    class Adapter:
        name = "mock"
        version = "mock-1"

        def analyze(self, prompt: str, images: list[Any], timeout: float) -> dict[str, Any]:
            response = _semantic_response()
            response["scores"]["problem_statement_alignment"]["score"] = 85.0
            response["scores"]["problem_statement_alignment"]["missing_evidence"] = [
                "missing integration proof",
                "missing standards proof",
            ]
            return response

    result = evaluate_deck(report, PROBLEM, semantic_adapter=Adapter(), semantic_cache_dir=tmp_path / "cache")
    alignment = result["scores"]["proposal_strength"]["components"]["problem_statement_alignment"]

    assert alignment["unpenalized_score"] == 85.0
    assert alignment["missing_evidence_penalty"] == 30.0
    assert alignment["score"] == 55.0
    assert result["scores"]["proposal_strength"]["missing_evidence_penalty"] == 9.0
    assert result["scores"]["proposal_strength"]["score"] == 68.5
    assert any(
        item["category"] == "explanation_completeness"
        for item in result["findings"]["weaknesses"]
    )


def test_content_visual_and_space_rubric_signals_produce_findings(tmp_path: Path) -> None:
    source = tmp_path / "rubric-signals.pptx"
    _feature_package(source)
    report = extract_pptx(source)
    payload = report.to_evaluator_dict()
    slide = payload["slides"][0]
    slide["title"] = {"object_id": "title", "text": "Title"}
    slide["text_blocks"] = [
        {"object_id": "title", "text": "Title", "bbox": [0.1, 0.05, 0.8, 0.1]},
        {
            "object_id": "body",
            "text": "Robust scalable platform with better outcomes. "
            + "This long explanation repeats implementation details without concise pointers. " * 8,
            "bbox": [0.1, 0.2, 0.8, 0.5],
        },
    ]
    slide["visual_evidence"] = []
    slide["tables"] = []
    slide["metrics"].update(
        {
            "occupied_area_ratio": 0.0,
            "whitespace_area_ratio": 1.0,
            "largest_empty_region_ratio": 1.0,
            "whitespace_balance": 1.0,
            "visual_area_ratio": 0.0,
        }
    )

    metrics, details = compute_deterministic_metrics(payload)
    assert metrics["paragraph_content_ratio"] == 1.0
    assert metrics["pointer_content_ratio"] == 0.0
    assert metrics["paragraph_heavy_slide_ratio"] == 1.0
    assert metrics["visual_coverage"] == 0.0
    assert metrics["space_usage"] == 0.0
    assert details["content_structure_score"]["slide_profiles"]["1"]["paragraph_heavy"] is True

    result = evaluate_deck(payload, deck_only=True)
    weakness_categories = {item["category"] for item in result["findings"]["weaknesses"]}
    assert {"content_structure", "visual_coverage", "space_usage"} <= weakness_categories
    assert result["findings"]["ambiguous_points"]


def test_deck_evaluation_accepts_serialized_evaluator_ir(tmp_path: Path) -> None:
    source = tmp_path / "serialized.pptx"
    _feature_package(source)
    report = extract_pptx(source)
    evaluator_payload = report.to_evaluator_dict()

    result = evaluate_deck(evaluator_payload, deck_only=True)

    assert result["deck"]["slide_count"] == evaluator_payload["deck"]["slide_count"]
    assert result["metrics"]["title_coverage"] == 1.0


def test_repeated_evaluation_is_stable_and_current_input_changes_fingerprint(tmp_path: Path) -> None:
    source = tmp_path / "repeatable.pptx"
    _feature_package(source)
    report = extract_pptx(source)

    first = evaluate_deck(report, deck_only=True)
    second = evaluate_deck(report, deck_only=True)

    assert first["scores"]["deck_quality"]["score"] == second["scores"]["deck_quality"]["score"]
    assert first["evaluation_fingerprint"] == second["evaluation_fingerprint"]

    changed_payload = report.to_evaluator_dict()
    changed_payload["slides"][0]["metrics"].update(
        {
            "visual_area_ratio": 0.0,
            "whitespace_area_ratio": 1.0,
            "largest_empty_region_ratio": 1.0,
        }
    )
    changed = evaluate_deck(changed_payload, deck_only=True)

    assert changed["scores"]["deck_quality"]["score"] != first["scores"]["deck_quality"]["score"]
    assert changed["evaluation_fingerprint"] != first["evaluation_fingerprint"]


def test_semantic_evaluation_is_cacheable_and_mockable(tmp_path: Path) -> None:
    source = tmp_path / "cache.pptx"
    _feature_package(source)
    report = extract_pptx(source)
    cache = tmp_path / "semantic-cache"

    class FirstAdapter:
        name = "mock"
        version = "mock-1"

        def __init__(self) -> None:
            self.calls = 0

        def analyze(self, prompt: str, images: list[Any], timeout: float) -> dict[str, Any]:
            self.calls += 1
            return _semantic_response(70.0)

    first_adapter = FirstAdapter()
    first = evaluate_deck(report, PROBLEM, semantic_adapter=first_adapter, semantic_cache_dir=cache)

    class FailingAdapter:
        name = "failing"
        version = "failing-1"

        def analyze(self, prompt: str, images: list[Any], timeout: float) -> dict[str, Any]:
            raise AssertionError("cache should prevent a second request")

    second = evaluate_deck(report, PROBLEM, semantic_adapter=FailingAdapter(), semantic_cache_dir=cache)

    assert first_adapter.calls == 1
    assert first["semantic"]["cache_hit"] is False
    assert second["semantic"]["cache_hit"] is True
    assert second["scores"]["proposal_strength"]["score"] == first["scores"]["proposal_strength"]["score"]


def test_fresh_semantic_bypasses_cache_for_a_new_model_score(tmp_path: Path) -> None:
    source = tmp_path / "fresh-cache.pptx"
    _feature_package(source)
    report = extract_pptx(source)
    cache = tmp_path / "fresh-semantic-cache"

    class Adapter:
        name = "mock"
        version = "mock-1"

        def __init__(self) -> None:
            self.calls = 0

        def analyze(self, prompt: str, images: list[Any], timeout: float) -> dict[str, Any]:
            self.calls += 1
            return _semantic_response(60.0 + self.calls)

    adapter = Adapter()
    first = evaluate_deck(
        report,
        PROBLEM,
        semantic_adapter=adapter,
        semantic_cache_dir=cache,
        fresh_semantic=True,
    )
    second = evaluate_deck(
        report,
        PROBLEM,
        semantic_adapter=adapter,
        semantic_cache_dir=cache,
        fresh_semantic=True,
    )

    assert adapter.calls == 2
    assert first["semantic"]["cache_hit"] is False
    assert second["semantic"]["cache_hit"] is False
    assert first["semantic"]["scores"]["impact"]["score"] != second["semantic"]["scores"]["impact"]["score"]
    assert first["evaluation_fingerprint"] != second["evaluation_fingerprint"]


def test_deck_only_keeps_semantic_scores_unavailable(tmp_path: Path, monkeypatch: Any) -> None:
    source = tmp_path / "deck-only.pptx"
    _feature_package(source)
    report = extract_pptx(source)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    result = evaluate_deck(report, deck_only=True, api_key=None)

    assert result["problem_statement"] is None
    assert result["semantic"]["status"] == "not_requested"
    assert result["scores"]["deck_quality"]["score"] is not None
    assert result["scores"]["proposal_strength"]["score"] is None
    assert result["final_score"] is None
    assert all(result["semantic"]["scores"][key]["score"] is None for key in SEMANTIC_COMPONENTS)


def test_missing_gemini_key_does_not_guess_semantic_scores(tmp_path: Path, monkeypatch: Any) -> None:
    source = tmp_path / "no-key.pptx"
    _feature_package(source)
    report = extract_pptx(source)
    monkeypatch.setenv("GEMINI_API_KEY", "")

    result = evaluate_deck(report, PROBLEM, semantic_cache_dir=tmp_path / "cache")

    assert result["semantic"]["status"] == "unavailable"
    assert result["semantic"]["error"] == "GEMINI_API_KEY is not configured"
    assert result["scores"]["proposal_strength"]["score"] is None
    assert result["final_score"] is None


def test_evaluate_deck_cli_loads_canonical_deckir(capsys: Any, tmp_path: Path) -> None:
    source = tmp_path / "cli.pptx"
    _feature_package(source)
    report = extract_pptx(source)
    deck_path = tmp_path / "deck-ir.json"
    deck_path.write_text(json.dumps(report.to_dict()), encoding="utf-8")

    assert evaluate_cli_main(["--deck-ir", str(deck_path), "--deck-only"]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["schema_version"] == "deck-evaluation-1.0"
    assert output["scores"]["deck_quality"]["score"] is not None
    assert output["scores"]["proposal_strength"]["score"] is None
