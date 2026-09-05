from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pptx_forensics.ranking import (
    BucketThresholds,
    ManifestEntry,
    SubmissionResult,
    assign_ranks,
    bucket_for_score,
    discover_sources,
    extension_for_url,
    infer_ps_id,
    load_manifest,
    render_evaluation_markdown,
    render_ranking_markdown,
)


def test_discover_sources_finds_only_supported_submission_types(tmp_path: Path) -> None:
    root = tmp_path / "submissions"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "one.pdf").write_bytes(b"pdf")
    (nested / "two.PPTX").write_bytes(b"pptx")
    (root / "notes.txt").write_text("ignore", encoding="utf-8")

    assert discover_sources(input_dirs=[root]) == [
        (nested / "two.PPTX").resolve(),
        (root / "one.pdf").resolve(),
    ]


def test_infer_ps_id_reads_official_id_tokens_from_document_text() -> None:
    report = SimpleNamespace(slides=[SimpleNamespace(text=["SIH 2025; official reference: SIH26168"])])

    assert infer_ps_id(report, "team-submission.pdf") == "26168"


def test_bucket_thresholds_and_rank_order_are_deterministic() -> None:
    thresholds = BucketThresholds(excellent=90, strong=75, promising=60)
    assert bucket_for_score(90, thresholds) == "A - Excellent"
    assert bucket_for_score(75, thresholds) == "B - Strong"
    assert bucket_for_score(60, thresholds) == "C - Promising"
    assert bucket_for_score(None, thresholds) == "REVIEW"

    results = [
        SubmissionResult(Path("a.pdf"), "team-a", "A", "26168", "PS", "scored", final_score=80, proposal_score=70, deck_quality_score=90),
        SubmissionResult(Path("b.pdf"), "team-b", "B", "26168", "PS", "scored", final_score=90, proposal_score=70, deck_quality_score=80),
        SubmissionResult(Path("c.pdf"), "team-c", "C", "26168", "PS", "review"),
    ]

    assign_ranks(results, thresholds)

    assert [(item.submission_id, item.rank, item.bucket) for item in results] == [
        ("team-a", 2, "B - Strong"),
        ("team-b", 1, "A - Excellent"),
        ("team-c", None, "REVIEW"),
    ]


def test_final_reports_are_narrative_only() -> None:
    result = SubmissionResult(
        Path("submission.pdf"),
        "team-submission",
        "Team",
        "26168",
        "Custom navigation PS",
        "scored",
        bucket="A - Excellent",
        rank=1,
        evaluation={
            "findings": {
                "strengths": [{"title": "Clear approach", "detail": "The approach is explained.", "evidence_slides": [2]}],
                "weaknesses": [],
                "ambiguous_points": [],
            }
        },
        evidence={"Native objects": 10},
    )

    report = render_evaluation_markdown(result)

    assert "## Overall Result" not in report
    assert "## Component Scores" not in report
    assert "Clear approach" in report
    assert "Native objects" in report


def test_final_reports_render_reviewer_critique_when_available() -> None:
    result = SubmissionResult(
        Path("submission.pdf"),
        "team-submission",
        "Team",
        "26168",
        "Custom navigation PS",
        "scored",
        evaluation={
            "review": {
                "area_reviews": [
                    {
                        "area": area,
                        "rating": 8,
                        "reason": "Evidence is present.",
                        "evidence_slides": [1],
                        "missing_evidence": [],
                    }
                    for area in (
                        "fit_to_problem",
                        "technical_approach",
                        "validation_presented",
                        "differentiation",
                        "presentation",
                    )
                ],
                "strengths": [{"point": "Coherent approach.", "evidence_slides": [1]}],
                "risks": [{"title": "Validation gap", "detail": "More trials needed.", "evidence_slides": [1]}],
                "next_evidence": ["Show an unseen-device trial."],
                "decision": "needs_revision",
                "limitations": ["External links were not inspected."],
            }
        },
    )

    report = render_evaluation_markdown(result)

    assert "## Area Review" in report
    assert "## What Prevents a Higher Score" in report
    assert "## Judging Decision" in report
    assert "External links were not inspected." in report


def test_ranking_report_groups_problem_statements() -> None:
    result = SubmissionResult(Path("a.pdf"), "team-a", "A", "26168", "Custom PS", "review")

    ranking = render_ranking_markdown([result])

    assert "# Submission Ranking" in ranking
    assert "## Problem Statement `26168`" in ranking
    assert "| REVIEW | team-a | A | REVIEW | N/A | review |" in ranking


def test_load_manifest_and_extension_inference(tmp_path: Path) -> None:
    manifest_file = tmp_path / "submissions.tsv"
    manifest_file.write_text(
        "teamName\tppt\tdemo\n"
        "Alpha\thttps://example.com/files/doc.pdf\thttps://demo.test/1\n"
        "Beta\thttps://example.com/files/deck.pptx?token=xyz\t\n",
        encoding="utf-8",
    )
    entries = load_manifest(manifest_file)
    assert len(entries) == 2
    assert entries[0].team_name == "Alpha"
    assert entries[0].url == "https://example.com/files/doc.pdf"
    assert extension_for_url(entries[0].url) == ".pdf"
    assert entries[1].team_name == "Beta"
    assert extension_for_url(entries[1].url) == ".pptx"
