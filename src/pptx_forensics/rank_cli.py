"""Command-line interface for batch submission ranking."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any

from .config import load_dotenv
from .deck_evaluation import MISSING_EVIDENCE_PENALTY, ProblemStatementError, load_problem_statement
from .problem_scraper import DEFAULT_SIH_PROBLEM_URL, OfficialProblemScraper
from .ranking import (
    BucketThresholds,
    discover_sources,
    load_manifest,
    normalize_ps_id,
    rank_submissions,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Process local document submissions sequentially and rank them by problem statement"
    )
    parser.add_argument("sources", nargs="*", type=Path, help="specific supported files; defaults to the submissions folder")
    parser.add_argument("--manifest", type=Path, help="path to submissions TSV/CSV manifest file")
    parser.add_argument(
        "--input-dir",
        action="append",
        type=Path,
        help="directory containing submissions; defaults to ./submissions and may be repeated",
    )
    problem_group = parser.add_mutually_exclusive_group()
    problem_group.add_argument("--problem", type=Path, help="one weighted problem JSON applied to every submission")
    problem_group.add_argument("--problem-dir", type=Path, help="directory of weighted problem JSON files keyed by ID")
    parser.add_argument(
        "--sih-url",
        default=DEFAULT_SIH_PROBLEM_URL,
        help="official problem-statement page used when --problem/--problem-dir is omitted",
    )
    parser.add_argument(
        "--problem-cache-dir",
        type=Path,
        default=Path(".cache/problem-statements"),
        help="local cache for the deterministic official problem-page scrape",
    )
    parser.add_argument("--sih-timeout", type=float, default=20.0, help="official problem-page request timeout in seconds")
    parser.add_argument("--refresh-problems", action="store_true", help="refresh the official problem-page cache")
    parser.add_argument("--ps-id", help="override the PS ID for every submission")
    parser.add_argument("--output-dir", type=Path, default=Path("evidence"), help="local Markdown output directory")
    parser.add_argument("--no-recursive", action="store_true", help="do not scan input directories recursively")
    parser.add_argument("--native-only", action="store_true", help="skip rendering, OCR, diagrams, and vision")
    parser.add_argument("--skip-render", action="store_true", help="skip slide/page rendering")
    parser.add_argument("--skip-ocr", action="store_true", help="skip OCR")
    parser.add_argument("--skip-diagrams", action="store_true", help="skip diagram reconstruction")
    parser.add_argument("--skip-vision", action="store_true", help="skip Gemini vision")
    parser.add_argument("--skip-semantic", action="store_true", help="skip Gemini semantic proposal and rendered-quality scoring")
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
        help="deprecated compatibility option; rubric 2 scores missing evidence within the affected criterion",
    )
    parser.add_argument("--excellent-threshold", type=float, default=85.0)
    parser.add_argument("--strong-threshold", type=float, default=70.0)
    parser.add_argument("--promising-threshold", type=float, default=55.0)
    parser.add_argument("--quiet", action="store_true", help="suppress per-submission progress messages")
    args = parser.parse_args(argv)

    try:
        load_dotenv()
        manifest_path = args.manifest
        if manifest_path is None and not args.sources and not args.input_dir:
            for candidate in (Path("submissions.tsv"), Path("/opt/application/submissions.tsv")):
                if candidate.is_file():
                    manifest_path = candidate
                    break

        manifest_entries = load_manifest(manifest_path) if manifest_path else None
        if manifest_entries is None:
            input_dirs = args.input_dir
            if input_dirs is None and not args.sources:
                input_dirs = [Path("submissions")]
            sources = discover_sources(
                args.sources,
                input_dirs or (),
                recursive=not args.no_recursive,
            )
        else:
            sources = ()

        if not os.environ.get("GEMINI_API_KEY") and not args.skip_semantic:
            print("Note: gemini key unavailable (GEMINI_API_KEY not configured in environment)", file=sys.stderr)

        problem = load_problem_statement(args.problem) if args.problem else None
        if args.problem_dir:
            problem_resolver = _problem_resolver(args.problem_dir)
        elif problem is None:
            problem_resolver = OfficialProblemScraper(
                args.sih_url,
                cache_dir=args.problem_cache_dir,
                timeout=args.sih_timeout,
                refresh=args.refresh_problems,
            ).resolve
        else:
            problem_resolver = None
        thresholds = BucketThresholds(
            excellent=args.excellent_threshold,
            strong=args.strong_threshold,
            promising=args.promising_threshold,
        )
        results = rank_submissions(
            sources,
            args.output_dir,
            manifest=manifest_entries,
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
            progress=None if args.quiet else _progress,
        )
    except (OSError, ProblemStatementError, ValueError) as exc:
        parser.error(str(exc))
    scored = sum(result.final_score is not None for result in results)
    failed = sum(result.status == "failed" for result in results)
    print(f"Evaluated {len(results)} submissions sequentially; {scored} scored; {failed} failed; Markdown output: {args.output_dir}")
    return 0


def _progress(index: int, total: int, source: Path) -> None:
    print(f"[{index}/{total}] Processing {source.name}", file=sys.stderr)


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
