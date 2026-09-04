"""Command-line interface for batch submission ranking."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

from .deck_evaluation import MISSING_EVIDENCE_PENALTY, ProblemStatementError, load_problem_statement
from .ranking import (
    BucketThresholds,
    discover_sources,
    normalize_ps_id,
    rank_submissions,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate and rank PPTX/PDF submissions by problem statement"
    )
    parser.add_argument("sources", nargs="*", type=Path, help="PPTX/PDF files to evaluate")
    parser.add_argument(
        "--input-dir",
        action="append",
        type=Path,
        help="directory containing submissions; repeat for multiple directories",
    )
    problem_group = parser.add_mutually_exclusive_group(required=True)
    problem_group.add_argument("--problem", type=Path, help="one weighted PS JSON applied to every submission")
    problem_group.add_argument("--problem-dir", type=Path, help="directory of weighted PS JSON files keyed by PS ID")
    parser.add_argument("--ps-id", help="override the PS ID for every submission")
    parser.add_argument("--output-dir", type=Path, required=True, help="directory for Markdown-only output")
    parser.add_argument("--no-recursive", action="store_true", help="do not scan input directories recursively")
    parser.add_argument("--native-only", action="store_true", help="skip rendering, OCR, diagrams, and vision")
    parser.add_argument("--skip-render", action="store_true", help="skip slide/page rendering")
    parser.add_argument("--skip-ocr", action="store_true", help="skip OCR")
    parser.add_argument("--skip-diagrams", action="store_true", help="skip diagram reconstruction")
    parser.add_argument("--skip-vision", action="store_true", help="skip Gemini vision")
    parser.add_argument("--skip-semantic", action="store_true", help="skip Gemini semantic proposal scoring")
    parser.add_argument("--aurochs-root", type=Path, help="Aurochs checkout for PPTX rendering")
    parser.add_argument("--pdf-render-dpi", type=int, default=144)
    parser.add_argument("--render-cache-dir", type=Path)
    parser.add_argument("--ocr-cache-dir", type=Path)
    parser.add_argument("--diagram-ocr-cache-dir", type=Path)
    parser.add_argument("--vision-cache-dir", type=Path)
    parser.add_argument("--vision-ocr-cache-dir", type=Path)
    parser.add_argument("--semantic-cache-dir", type=Path)
    parser.add_argument("--vision-model", default=None)
    parser.add_argument("--vision-timeout", type=float, default=30.0)
    parser.add_argument("--vision-retries", type=int, default=2)
    parser.add_argument("--vision-thinking-budget", type=int, default=1024)
    parser.add_argument("--vision-max-output-tokens", type=int, default=16384)
    parser.add_argument("--vision-concurrency", type=int, default=2)
    parser.add_argument("--vision-include-noise", action="store_true")
    parser.add_argument("--semantic-timeout", type=float, default=30.0)
    parser.add_argument("--fresh-semantic", action="store_true")
    parser.add_argument(
        "--missing-evidence-penalty",
        type=float,
        default=MISSING_EVIDENCE_PENALTY,
        help="points deducted per distinct missing-evidence item",
    )
    parser.add_argument("--excellent-threshold", type=float, default=85.0)
    parser.add_argument("--strong-threshold", type=float, default=70.0)
    parser.add_argument("--promising-threshold", type=float, default=55.0)
    args = parser.parse_args(argv)

    try:
        sources = discover_sources(
            args.sources,
            args.input_dir or (),
            recursive=not args.no_recursive,
        )
        problem = load_problem_statement(args.problem) if args.problem else None
        problem_resolver = _problem_resolver(args.problem_dir) if args.problem_dir else None
        thresholds = BucketThresholds(
            excellent=args.excellent_threshold,
            strong=args.strong_threshold,
            promising=args.promising_threshold,
        )
        results = rank_submissions(
            sources,
            args.output_dir,
            problem=problem,
            problem_resolver=problem_resolver,
            ps_id=args.ps_id,
            native_only=args.native_only,
            skip_render=args.skip_render,
            skip_ocr=args.skip_ocr,
            skip_diagrams=args.skip_diagrams,
            skip_vision=args.skip_vision,
            skip_semantic=args.skip_semantic,
            aurochs_root=args.aurochs_root,
            pdf_render_dpi=args.pdf_render_dpi,
            render_cache_dir=args.render_cache_dir,
            ocr_cache_dir=args.ocr_cache_dir,
            diagram_ocr_cache_dir=args.diagram_ocr_cache_dir,
            vision_cache_dir=args.vision_cache_dir,
            vision_ocr_cache_dir=args.vision_ocr_cache_dir,
            semantic_cache_dir=args.semantic_cache_dir,
            vision_model=args.vision_model,
            vision_timeout=args.vision_timeout,
            vision_retries=args.vision_retries,
            vision_thinking_budget=args.vision_thinking_budget,
            vision_max_output_tokens=args.vision_max_output_tokens,
            vision_concurrency=args.vision_concurrency,
            vision_include_noise=args.vision_include_noise,
            semantic_timeout=args.semantic_timeout,
            fresh_semantic=args.fresh_semantic,
            missing_evidence_penalty=args.missing_evidence_penalty,
            thresholds=thresholds,
        )
    except (OSError, ProblemStatementError, ValueError) as exc:
        parser.error(str(exc))
    scored = sum(result.final_score is not None for result in results)
    print(f"Evaluated {len(results)} submissions; {scored} scored; Markdown output: {args.output_dir}")
    return 0


def _problem_resolver(problem_dir: Path):
    root = problem_dir.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"problem directory does not exist: {root}")
    problems: dict[str, Any] = {}
    for path in sorted(root.glob("*.json")):
        problem = load_problem_statement(path)
        for key in {normalize_ps_id(problem.id), normalize_ps_id(path.stem)}:
            existing = problems.get(key)
            if existing is not None and existing.to_dict() != problem.to_dict():
                raise ValueError(f"multiple problem statements map to PS ID {key}")
            problems[key] = problem
    if not problems:
        raise ValueError(f"no problem JSON files found in: {root}")

    def resolve(ps_id: str):
        problem = problems.get(normalize_ps_id(ps_id))
        if problem is None:
            raise ProblemStatementError(f"no problem statement found for PS ID {ps_id}")
        return problem

    return resolve


if __name__ == "__main__":
    sys.exit(main())
