"""Command-line interface for deck evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .deck_evaluation import (
    MISSING_EVIDENCE_PENALTY,
    ProblemStatementError,
    evaluate_deck_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate a canonical DeckIR presentation")
    parser.add_argument("--deck-ir", required=True, type=Path, help="canonical DeckIR JSON input")
    parser.add_argument("--problem", type=Path, help="weighted problem statement JSON input")
    parser.add_argument("--render-dir", type=Path, help="directory containing rendered slide images")
    parser.add_argument("--deck-only", action="store_true", help="skip problem alignment and semantic proposal scoring")
    parser.add_argument("--semantic-cache-dir", type=Path, help="cache directory for validated Gemini responses")
    parser.add_argument("--skip-semantic", action="store_true", help="do not make a Gemini semantic request")
    parser.add_argument(
        "--fresh-semantic",
        action="store_true",
        help="bypass the semantic cache and request a fresh model score",
    )
    parser.add_argument("--semantic-timeout", type=float, default=30.0, help="Gemini timeout in seconds")
    parser.add_argument(
        "--missing-evidence-penalty",
        type=float,
        default=MISSING_EVIDENCE_PENALTY,
        help="points deducted per missing-evidence item, capped at the component score",
    )
    parser.add_argument("--output", type=Path, help="write evaluation JSON to this path instead of stdout")
    args = parser.parse_args(argv)
    if args.problem is None and not args.deck_only:
        parser.error("--problem is required unless --deck-only is used")
    try:
        result = evaluate_deck_file(
            args.deck_ir,
            args.problem,
            deck_only=args.deck_only,
            render_dir=args.render_dir,
            semantic_cache_dir=args.semantic_cache_dir,
            semantic_timeout=args.semantic_timeout,
            skip_semantic=args.skip_semantic,
            fresh_semantic=args.fresh_semantic,
            missing_evidence_penalty=args.missing_evidence_penalty,
        )
    except (OSError, ProblemStatementError, ValueError) as exc:
        parser.error(str(exc))
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(output, end="")
    else:
        destination = args.output.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(output, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
