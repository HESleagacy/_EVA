"""Command-line interface for fetching one official problem statement."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .problem_scraper import DEFAULT_SIH_PROBLEM_URL, OfficialProblemScraper, ProblemScrapeError, normalize_problem_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch one official problem statement into local JSON")
    parser.add_argument("ps_id", help="problem statement ID found in the submission")
    parser.add_argument("--url", default=DEFAULT_SIH_PROBLEM_URL, help="official problem-statement page")
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/problem-statements"))
    parser.add_argument("--output", type=Path, help="JSON destination; defaults to problems/<ps-id>.json")
    parser.add_argument("--refresh", action="store_true", help="ignore the local cache and fetch the page again")
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    try:
        problem = OfficialProblemScraper(
            args.url,
            cache_dir=args.cache_dir,
            timeout=args.timeout,
            refresh=args.refresh,
        ).resolve(args.ps_id)
    except (OSError, ProblemScrapeError, ValueError) as exc:
        parser.error(str(exc))

    output = args.output or Path("problems") / f"{normalize_problem_id(problem.id)}.json"
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(problem.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Fetched PS {problem.id}: {problem.title}")
    print(f"Saved: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
