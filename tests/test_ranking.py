from __future__ import annotations

from pathlib import Path

from pptx_forensics.ranking import (
    BucketThresholds,
    SubmissionResult,
    assign_ranks,
    bucket_for_score,
    discover_sources,
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


def test_ranking_report_groups_problem_statements() -> None:
    result = SubmissionResult(Path("a.pdf"), "team-a", "A", "26168", "Custom PS", "review")

    ranking = render_ranking_markdown([result])

    assert "# Submission Ranking" in ranking
    assert "## Problem Statement `26168`" in ranking
    assert "| REVIEW | team-a | A | REVIEW | N/A | review |" in ranking
