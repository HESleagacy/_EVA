"""Batch evaluation and problem-statement ranking for PPTX and PDF submissions."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
import urllib.parse
import urllib.request

from .deck_evaluation import (
    DECK_COMPONENT_POINTS,
    MISSING_EVIDENCE_PENALTY,
    PROPOSAL_COMPONENT_POINTS,
    ProblemStatement,
    ProblemStatementError,
    evaluate_deck,
    load_problem_statement,
    validate_problem_statement,
)
from .diagrams import reconstruct_raster_diagrams
from .extractor import extract_document
from .ocr import run_ocr
from .problem_scraper import normalize_problem_id
from .render import render_selected_pdf_pages, render_selected_slides
from .vision import DEFAULT_MAX_OUTPUT_TOKENS, run_selective_vision


SUPPORTED_INPUT_SUFFIXES = frozenset({".pdf", ".pptx"})
LEGACY_PPT_SUFFIX = ".ppt"
RANKING_SCHEMA_VERSION = "submission-ranking-1.0"
_PS_ID_PATTERNS = (
    re.compile(r"\bs\s*i\s*h\s*[-_:#]?\s*(\d(?:[\s_-]*\d){4,7})\b", re.IGNORECASE),
    re.compile(
        r"problem\s+statement\s+(?:id|number|no)\s*[:#\-–—]?\s*([A-Za-z0-9][A-Za-z0-9_-]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bps\s+(?:id|number|no)\s*[:#\-–—]?\s*([A-Za-z0-9][A-Za-z0-9_-]*)",
        re.IGNORECASE,
    ),
)
_FILENAME_PS_PATTERN = re.compile(
    r"(?:^|[-_ ()])(?:ps|problem[-_ ]?statement)[-_ ]*([0-9][A-Za-z0-9_-]*)",
    re.IGNORECASE,
)
_TEAM_PATTERN = re.compile(r"team\s+name\s*[:#\-–—]?\s*([^\r\n]+)", re.IGNORECASE)
_REVIEW_AREA_LABELS = {
    "fit_to_problem": "Fit to the stated problem",
    "technical_approach": "Technical approach",
    "validation_presented": "Validation presented",
    "differentiation": "Differentiation",
    "presentation": "Presentation",
}


@dataclass(frozen=True)
class BucketThresholds:
    """Absolute score thresholds used consistently within every PS group."""

    excellent: float = 85.0
    strong: float = 70.0
    promising: float = 55.0

    def __post_init__(self) -> None:
        values = (self.excellent, self.strong, self.promising)
        if any(not isinstance(value, (int, float)) or isinstance(value, bool) for value in values):
            raise ValueError("bucket thresholds must be numeric")
        if not 0 <= self.promising <= self.strong <= self.excellent <= 100:
            raise ValueError("bucket thresholds must satisfy 0 <= promising <= strong <= excellent <= 100")


@dataclass(frozen=True)
class ManifestEntry:
    """Entry loaded from a submissions manifest file."""

    team_name: str
    url: str
    demo_url: str = ""


def load_manifest(path: str | Path) -> list[ManifestEntry]:
    """Parse a TSV or CSV manifest file containing teamName and ppt URL."""
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")
    entries: list[ManifestEntry] = []
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    header_seen = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else [item.strip() for item in line.split(",")]
        if not header_seen:
            first = parts[0].strip().lower()
            if "team" in first or "ppt" in first or "url" in first:
                header_seen = True
                continue
            header_seen = True
        team = parts[0].strip()
        url = parts[1].strip() if len(parts) > 1 else ""
        demo = parts[2].strip() if len(parts) > 2 else ""
        if url:
            entries.append(ManifestEntry(team_name=team or "Unknown", url=url, demo_url=demo))
    return entries


def extension_for_url(url: str) -> str:
    """Infer the downloaded presentation extension from a submission URL."""
    parsed_path = urllib.parse.urlsplit(url).path
    suffix = Path(parsed_path).suffix.lower()
    if suffix in SUPPORTED_INPUT_SUFFIXES or suffix == LEGACY_PPT_SUFFIX:
def convert_legacy_ppt(source: Path, destination_dir: Path) -> Path:
    """Convert a legacy binary PowerPoint file to OOXML for extraction."""
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if executable is None:
        raise RuntimeError("legacy .ppt input requires libreoffice or soffice for conversion")
    destination_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [executable, "--headless", "--convert-to", "pptx", "--outdir", str(destination_dir), str(source)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "conversion failed").strip()
        raise RuntimeError(f"legacy .ppt conversion failed: {detail}") from exc
    converted = destination_dir / f"{source.stem}.pptx"
    if not converted.is_file():
        raise RuntimeError("legacy .ppt conversion did not produce a .pptx file")
    return converted


        return suffix
    name = Path(parsed_path).name.lower()
    if ".pptx" in name:
        return ".pptx"
    return ".pdf"


def download_submission_file(url: str, destination: Path, timeout: float = 60.0) -> None:
    """Stream a remote presentation file to local disk."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; DocumentForensics/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        with open(destination, "wb") as output_file:
            while chunk := response.read(65536):
                output_file.write(chunk)


@dataclass
class SubmissionResult:
    """Internal result used to write a submission report and PS leaderboard."""

    source: Path
    submission_id: str
    team: str
    ps_id: str
    problem_title: str
    status: str
    bucket: str = "REVIEW"
    rank: int | None = None
    final_score: float | None = None
    proposal_score: float | None = None
    deck_quality_score: float | None = None
    evaluation: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    error: str | None = None
    report_path: Path | None = None


def normalize_ps_id(value: Any) -> str:
    """Normalize PS labels so ``PS-26168`` and ``26168`` group together."""
    return normalize_problem_id(value)


def safe_slug(value: Any, fallback: str = "submission") -> str:
    """Create a filesystem-safe, stable name for a report directory."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-.")
    return slug if slug not in {"", ".", ".."} else fallback


def discover_sources(
    paths: Sequence[str | Path] = (),
    input_dirs: Sequence[str | Path] = (),
    *,
    recursive: bool = True,
) -> list[Path]:
    """Discover supported PPTX/PDF files from explicit paths and directories."""
    candidates: list[Path] = []
    for raw in [*paths, *input_dirs]:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"submission path does not exist: {path}")
        if path.is_file():
            if path.suffix.casefold() not in SUPPORTED_INPUT_SUFFIXES:
                raise ValueError(f"unsupported submission type: {path}")
            candidates.append(path)
            continue
        iterator: Iterable[Path] = path.rglob("*") if recursive else path.glob("*")
        candidates.extend(
            item.resolve()
            for item in iterator
            if item.is_file() and item.suffix.casefold() in SUPPORTED_INPUT_SUFFIXES
        )
    unique = {item for item in candidates}
    if not unique:
        raise ValueError("no .pptx or .pdf submissions were found")
    return sorted(unique, key=lambda item: str(item).casefold())


def infer_ps_id(report: Any, source: str | Path) -> str:
    """Infer a PS identifier from native slide text, then from the filename."""
    text = _report_text(report)
    for pattern in _PS_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group(1)
            if pattern is _PS_ID_PATTERNS[0]:
                value = re.sub(r"[\s_-]+", "", value)
            return normalize_ps_id(value)
    filename_match = _FILENAME_PS_PATTERN.search(Path(source).stem)
    if filename_match:
        return normalize_ps_id(filename_match.group(1))
    return "UNASSIGNED"


def infer_team(report: Any, source: str | Path) -> str:
    """Infer a team label from native slide text with a filename fallback."""
    text = _report_text(report)
    match = _TEAM_PATTERN.search(text)
    if match:
        value = re.sub(r"^[\s•●▪*-]+", "", match.group(1)).strip()
        if value:
            return value
    return Path(source).stem


def bucket_for_score(score: float | None, thresholds: BucketThresholds = BucketThresholds()) -> str:
    """Return a stable absolute bucket; unavailable scores remain human review."""
    if score is None:
        return "REVIEW"
    if score >= thresholds.excellent:
        return "A - Excellent"
    if score >= thresholds.strong:
        return "B - Strong"
    if score >= thresholds.promising:
        return "C - Promising"
    return "D - Needs work"


def assign_ranks(results: Sequence[SubmissionResult], thresholds: BucketThresholds = BucketThresholds()) -> list[SubmissionResult]:
    """Assign ranks only to scored submissions, grouped by normalized PS ID."""
    grouped: dict[str, list[SubmissionResult]] = {}
    for result in results:
        result.bucket = bucket_for_score(result.final_score, thresholds)
        grouped.setdefault(result.ps_id, []).append(result)
    for group in grouped.values():
        scored = sorted(
            (item for item in group if item.final_score is not None and item.status != "failed"),
            key=lambda item: (
                -float(item.final_score or 0.0),
                -float(item.proposal_score or 0.0),
                -float(item.deck_quality_score or 0.0),
                item.submission_id.casefold(),
            ),
        )
        for rank, item in enumerate(scored, 1):
            item.rank = rank
    return list(results)


def render_evaluation_markdown(result: SubmissionResult) -> str:
    """Render a concise, evidence-backed scorecard for one submission."""
    lines = [
        "# Deck Evaluation Report",
        "",
        "## Submission",
        "",
        f"- Source: `{result.source}`",
        f"- Submission: `{result.submission_id}`",
        f"- Team: `{result.team}`",
        f"- Problem statement: `{result.ps_id}`",
        f"- Problem title: {result.problem_title or 'Unavailable'}",
        f"- Rank: `{result.rank if result.rank is not None else 'REVIEW'}`",
        f"- Bucket: `{result.bucket}`",
        f"- Status: `{result.status}`",
        "",
    ]
    if result.error:
        lines.extend(["## Processing Status", "", f"- Error: {_one_line(result.error)}", ""])
        return "\n".join(lines)

    evaluation = result.evaluation or {}
    scores = evaluation.get("scores") if isinstance(evaluation, Mapping) else None
    lines.extend(_scorecard_sections(evaluation, scores if isinstance(scores, Mapping) else {}))

    findings = evaluation.get("findings", {}) if isinstance(evaluation, Mapping) else {}
    if not isinstance(findings, Mapping):
        findings = {}
    lines.extend(_finding_section("Top Strengths", findings.get("strengths", []), limit=3))
    lines.extend(
        _finding_section(
            "Main Deductions",
            [*findings.get("weaknesses", []), *findings.get("ambiguous_points", [])],
            limit=3,
        )
    )
    if isinstance(evaluation, Mapping):
        quality_penalty = evaluation.get("quality_penalty")
        if isinstance(quality_penalty, Mapping):
            lines.extend(
                [
                    "## Additional Quality Penalty",
                    "",
                    f"- Points: `{_display_number(quality_penalty.get('points'), '0')}`",
                    f"- Basis: {_one_line(quality_penalty.get('reason', 'Not assessed.'))}",
                    "",
                ]
            )
        lines.extend(_verdict_section(result, evaluation, findings))
    lines.extend(["## Evidence Coverage", ""])
    for label, value in (result.evidence or {}).items():
        lines.append(f"- {label}: `{value}`")
    if not result.evidence:
        lines.append("- No evidence summary was produced.")
    lines.append("")
    return "\n".join(lines)


def _scorecard_sections(evaluation: Mapping[str, Any], scores: Mapping[str, Any]) -> list[str]:
    lines = ["## Scorecard", ""]
    final = scores.get("final") if isinstance(scores.get("final"), Mapping) else {}
    proposal = scores.get("proposal_strength") if isinstance(scores.get("proposal_strength"), Mapping) else {}
    deck = scores.get("deck_quality") if isinstance(scores.get("deck_quality"), Mapping) else {}
    final_score = _finite_value(final.get("score"))
    proposal_points = _finite_value(proposal.get("points"))
    deck_points = _finite_value(deck.get("points"))
    if final_score is None:
        lines.append("- Final score: `REVIEW` (a complete weighted score was not available)")
    else:
        lines.append(
            f"- Final score: `{final_score:.2f}/100` "
            f"(proposal `{proposal_points:.2f}/70` + deck `{deck_points:.2f}/30`)"
        )
    lines.append("")
    lines.extend(["| Metric | Score | Points | Basis |", "| --- | ---: | ---: | --- |"])
    point_maps = (
        (proposal, PROPOSAL_COMPONENT_POINTS),
        (deck, DECK_COMPONENT_POINTS),
    )
    for group, point_map in point_maps:
        components = group.get("components") if isinstance(group.get("components"), Mapping) else {}
        for key, maximum in point_map.items():
            record = components.get(key) if isinstance(components.get(key), Mapping) else {}
            score = _finite_value(record.get("score"))
            points = score * maximum / 100.0 if score is not None else None
            basis = record.get("explanation") or "Not assessed."
            lines.append(
                f"| {_table_text(key.replace('_', ' ').capitalize())} | "
                f"{_display_number(score, 'REVIEW')}/100 | {_display_number(points, 'N/A')}/{maximum:g} | "
                f"{_table_text(_brief(basis))} |"
            )
    penalty = evaluation.get("quality_penalty")
    if isinstance(penalty, Mapping):
        penalty_points = _finite_value(penalty.get("points"))
        if penalty_points:
            lines.append(f"| Additional deck-quality penalty | - | -{penalty_points:g} | {_table_text(_brief(penalty.get('reason', '')))} |")
    lines.append("")
    return lines


def _verdict_section(
    result: SubmissionResult,
    evaluation: Mapping[str, Any],
    findings: Mapping[str, Any],
) -> list[str]:
    final = evaluation.get("scores", {}).get("final", {}) if isinstance(evaluation.get("scores"), Mapping) else {}
    score = _finite_value(final.get("score")) if isinstance(final, Mapping) else None
    deductions = [
        item
        for item in [*findings.get("weaknesses", []), *findings.get("ambiguous_points", [])]
        if isinstance(item, Mapping)
    ]
    if score is None:
        sentence = "The submission remains in REVIEW because a complete numeric score was unavailable."
    elif deductions:
        first = _one_line(deductions[0].get("detail", ""))
        sentence = f"The submission scored {score:.2f}/100; the main limiting factor is {first}."
    else:
        sentence = f"The submission scored {score:.2f}/100 with no material deduction identified in the supplied evidence."
    return ["## Verdict", "", f"- {sentence}", ""]


def render_ranking_markdown(results: Sequence[SubmissionResult]) -> str:
    """Render a PS-grouped leaderboard without exposing intermediate JSON."""
    lines = ["# Submission Ranking", "", f"- Ranking schema: `{RANKING_SCHEMA_VERSION}`", ""]
    grouped: dict[str, list[SubmissionResult]] = {}
    for result in results:
        grouped.setdefault(result.ps_id, []).append(result)
    for ps_id in sorted(grouped):
        group = sorted(
            grouped[ps_id],
            key=lambda item: (
                item.rank is None,
                item.rank if item.rank is not None else 10**9,
                item.submission_id.casefold(),
            ),
        )
        title = next((item.problem_title for item in group if item.problem_title), "Unavailable")
        lines.extend(
            [
                f"## Problem Statement `{ps_id}`",
                "",
                f"- Title: {title}",
                "",
                "| Rank | Submission | Team | Bucket | Score | Status |",
                "| ---: | --- | --- | --- | ---: | --- |",
            ]
        )
        for item in group:
            rank = str(item.rank) if item.rank is not None else "REVIEW"
            score = f"{item.final_score:.2f}" if item.final_score is not None else "N/A"
            lines.append(
                f"| {rank} | {_table_text(item.submission_id)} | {_table_text(item.team)} | "
                f"{_table_text(item.bucket)} | {score} | {_table_text(item.status)} |"
            )
        lines.append("")
    if not results:
        lines.append("No submissions were evaluated.\n")
    return "\n".join(lines)


def rank_submissions(
    sources: Sequence[str | Path] = (),
    output_dir: str | Path = Path("evidence"),
    *,
    manifest: Sequence[ManifestEntry] | None = None,
    problem: ProblemStatement | Mapping[str, Any] | str | Path | None = None,
    problem_resolver: Callable[[str], ProblemStatement] | None = None,
    ps_id: str | None = None,
    native_only: bool = False,
    skip_render: bool = False,
    skip_ocr: bool = False,
    skip_diagrams: bool = False,
    skip_vision: bool = False,
    skip_semantic: bool = False,
    aurochs_root: str | Path | None = None,
    pdf_render_dpi: int = 144,
    render_cache_dir: str | Path | None = None,
    ocr_cache_dir: str | Path | None = None,
    diagram_ocr_cache_dir: str | Path | None = None,
    vision_cache_dir: str | Path | None = None,
    vision_ocr_cache_dir: str | Path | None = None,
    semantic_cache_dir: str | Path | None = None,
    vision_model: str | None = None,
    vision_timeout: float = 30.0,
    vision_retries: int = 2,
    vision_thinking_budget: int = 1024,
    vision_max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    vision_concurrency: int = 2,
    vision_include_noise: bool = False,
    semantic_timeout: float = 30.0,
    fresh_semantic: bool = False,
    missing_evidence_penalty: float = MISSING_EVIDENCE_PENALTY,
    thresholds: BucketThresholds = BucketThresholds(),
    progress: Callable[[int, int, Path], None] | None = None,
) -> list[SubmissionResult]:
    """Evaluate, rank, bucket, and write Markdown-only submission outputs."""
    if problem is not None and problem_resolver is not None:
        raise ValueError("provide either problem or problem_resolver, not both")
    single_problem = _coerce_problem(problem) if problem is not None else None
    if single_problem is None and problem_resolver is None:
        raise ProblemStatementError("a problem statement or problem resolver is required")
    if not sources and not manifest:
        raise ValueError("at least one submission or manifest entry is required")
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[SubmissionResult] = []

    if manifest is not None:
        total = len(manifest)
        for index, entry in enumerate(manifest, 1):
                    source_file = convert_legacy_ppt(temp_file, Path(dl_dir) / "converted") if ext == LEGACY_PPT_SUFFIX else temp_file
            if progress is not None:
                progress(index, total, Path(entry.team_name))
            ext = extension_for_url(entry.url)
            with tempfile.TemporaryDirectory(prefix="submission-dl-") as dl_dir:
                temp_file = Path(dl_dir) / f"{safe_slug(entry.team_name)}{ext}"
                try:
                    download_submission_file(entry.url, temp_file, timeout=60.0)
                    result = _process_submission(
                        source_file,
                        team_override=entry.team_name,
                        single_problem=single_problem,
                        problem_resolver=problem_resolver,
                        forced_ps_id=ps_id,
                        native_only=native_only,
                        skip_render=skip_render,
                        skip_ocr=skip_ocr,
                        skip_diagrams=skip_diagrams,
                        skip_vision=skip_vision,
                        skip_semantic=skip_semantic,
                        aurochs_root=aurochs_root,
                        pdf_render_dpi=pdf_render_dpi,
                        render_cache_dir=render_cache_dir,
                        ocr_cache_dir=ocr_cache_dir,
                        diagram_ocr_cache_dir=diagram_ocr_cache_dir,
                        vision_cache_dir=vision_cache_dir,
                        vision_ocr_cache_dir=vision_ocr_cache_dir,
                        semantic_cache_dir=semantic_cache_dir,
                        vision_model=vision_model,
                        vision_timeout=vision_timeout,
                        vision_retries=vision_retries,
                        vision_thinking_budget=vision_thinking_budget,
                        vision_max_output_tokens=vision_max_output_tokens,
                        vision_concurrency=vision_concurrency,
                        vision_include_noise=vision_include_noise,
                        semantic_timeout=semantic_timeout,
                        fresh_semantic=fresh_semantic,
                        missing_evidence_penalty=missing_evidence_penalty,
                    )
                except Exception as exc:
                    result = SubmissionResult(
                        source=Path(entry.url),
                        submission_id=f"{safe_slug(entry.team_name)}-download-failed",
                        team=entry.team_name,
                        ps_id=normalize_ps_id(ps_id) if ps_id else (normalize_ps_id(single_problem.id) if single_problem else "UNASSIGNED"),
                        problem_title=single_problem.title if single_problem else "Unavailable",
                        status="failed",
                        error=f"Download or extraction failed: {exc}",
                    )
            # Temporary downloaded presentation file in dl_dir is automatically deleted here (Option A)
            report_dir = output_root / safe_slug(result.ps_id, "UNASSIGNED") / safe_slug(result.submission_id)
            report_dir.mkdir(parents=True, exist_ok=True)
            result.report_path = report_dir / "report.md"
            result.report_path.write_text(render_evaluation_markdown(result), encoding="utf-8")
            results.append(result)
    else:
        total = len(sources)
        for index, raw_source in enumerate(sources, 1):
            source = Path(raw_source).expanduser().resolve()
            if progress is not None:
                progress(index, total, source)
            result = _process_submission(
                source,
                single_problem=single_problem,
                problem_resolver=problem_resolver,
                forced_ps_id=ps_id,
                native_only=native_only,
                skip_render=skip_render,
                skip_ocr=skip_ocr,
                skip_diagrams=skip_diagrams,
                skip_vision=skip_vision,
                skip_semantic=skip_semantic,
                aurochs_root=aurochs_root,
                pdf_render_dpi=pdf_render_dpi,
                render_cache_dir=render_cache_dir,
                ocr_cache_dir=ocr_cache_dir,
                diagram_ocr_cache_dir=diagram_ocr_cache_dir,
                vision_cache_dir=vision_cache_dir,
                vision_ocr_cache_dir=vision_ocr_cache_dir,
                semantic_cache_dir=semantic_cache_dir,
                vision_model=vision_model,
                vision_timeout=vision_timeout,
                vision_retries=vision_retries,
                vision_thinking_budget=vision_thinking_budget,
                vision_max_output_tokens=vision_max_output_tokens,
                vision_concurrency=vision_concurrency,
                vision_include_noise=vision_include_noise,
                semantic_timeout=semantic_timeout,
                fresh_semantic=fresh_semantic,
                missing_evidence_penalty=missing_evidence_penalty,
            )
            report_dir = output_root / safe_slug(result.ps_id, "UNASSIGNED") / safe_slug(result.submission_id)
            report_dir.mkdir(parents=True, exist_ok=True)
            result.report_path = report_dir / "report.md"
            result.report_path.write_text(render_evaluation_markdown(result), encoding="utf-8")
            results.append(result)

    assign_ranks(results, thresholds)
    for result in results:
        if result.report_path:
            result.report_path.write_text(render_evaluation_markdown(result), encoding="utf-8")
    (output_root / "ranking.md").write_text(render_ranking_markdown(results), encoding="utf-8")
    return results


def _process_submission(
    source: Path,
    *,
    team_override: str | None = None,
    single_problem: ProblemStatement | None,
    problem_resolver: Callable[[str], ProblemStatement] | None,
    forced_ps_id: str | None,
    native_only: bool,
    skip_render: bool,
    skip_ocr: bool,
    skip_diagrams: bool,
    skip_vision: bool,
    skip_semantic: bool,
    aurochs_root: str | Path | None,
    pdf_render_dpi: int,
    render_cache_dir: str | Path | None,
    ocr_cache_dir: str | Path | None,
    diagram_ocr_cache_dir: str | Path | None,
    vision_cache_dir: str | Path | None,
    vision_ocr_cache_dir: str | Path | None,
    semantic_cache_dir: str | Path | None,
    vision_model: str | None,
    vision_timeout: float,
    vision_retries: int,
    vision_thinking_budget: int,
    vision_max_output_tokens: int,
    vision_concurrency: int,
    vision_include_noise: bool,
    semantic_timeout: float,
    fresh_semantic: bool,
    missing_evidence_penalty: float,
) -> SubmissionResult:
    fallback_id = f"{safe_slug(team_override or source.stem)}-{hashlib.sha256(str(source).encode()).hexdigest()[:8]}"
    team = team_override or source.stem
    ps_key = normalize_ps_id(forced_ps_id) if forced_ps_id else normalize_ps_id(single_problem.id) if single_problem else "UNASSIGNED"
    problem_title = single_problem.title if single_problem else "Unavailable"
    try:
        with tempfile.TemporaryDirectory(prefix="pptx-forensics-ranking-") as temporary:
            evidence_dir = Path(temporary) / "evidence"
            report = extract_document(
                source,
                evidence_dir,
                include_visual_evidence=not native_only,
                include_native_diagrams=not native_only,
            )
            team = team_override or infer_team(report, source)
            if not single_problem:
                ps_key = normalize_ps_id(forced_ps_id or infer_ps_id(report, source))
                if problem_resolver is None:
                    raise ProblemStatementError("no problem resolver is configured")
                selected_problem = problem_resolver(ps_key)
            else:
                selected_problem = single_problem
            problem_title = selected_problem.title
            if selected_problem is not None and getattr(report, "canonical", None) is not None:
                report.canonical.deck["problem_statement"] = selected_problem.to_dict()
            slides = _slide_numbers(report)
            rendered_dir = evidence_dir / "rendered"
            if not native_only and not skip_render and slides:
                _safe_stage(
                    report,
                    "rendering",
                    lambda: _render(report, source, evidence_dir, slides, aurochs_root, pdf_render_dpi, render_cache_dir),
                )
            if not native_only and not skip_ocr:
                _safe_stage(report, "OCR", lambda: run_ocr(report, source, slides=slides, cache_dir=ocr_cache_dir))
            if not native_only and not skip_diagrams:
                _safe_stage(
                    report,
                    "diagram reconstruction",
                    lambda: reconstruct_raster_diagrams(
                        report,
                        source,
                        slides=slides,
                        ocr_cache_dir=diagram_ocr_cache_dir or ocr_cache_dir,
                        run_ocr_stage=not skip_ocr,
                        skip_ocr=skip_ocr,
                    ),
                )
            if not native_only and not skip_vision:
                _safe_stage(
                    report,
                    "vision",
                    lambda: run_selective_vision(
                        report,
                        source,
                        slides=slides,
                        model=vision_model or "gemini-2.5-flash",
                        cache_dir=vision_cache_dir,
                        ocr_cache_dir=vision_ocr_cache_dir or ocr_cache_dir or diagram_ocr_cache_dir,
                        rendered_dir=rendered_dir,
                        run_ocr_stage=not skip_ocr,
                        skip_ocr=skip_ocr,
                        include_noise=vision_include_noise,
                        timeout=vision_timeout,
                        retries=vision_retries,
                        thinking_budget=vision_thinking_budget,
                        max_output_tokens=vision_max_output_tokens,
                        max_concurrency=vision_concurrency,
                    ),
                )
            evaluation = evaluate_deck(
                report,
                selected_problem,
                render_dir=rendered_dir,
                semantic_cache_dir=semantic_cache_dir,
                semantic_timeout=semantic_timeout,
                skip_semantic=skip_semantic,
                fresh_semantic=fresh_semantic,
                missing_evidence_penalty=missing_evidence_penalty,
            )
            final_score = _score(evaluation, ("final_score",))
            proposal_score = _score(evaluation, ("scores", "proposal_strength", "score"))
            deck_quality_score = _score(evaluation, ("scores", "deck_quality", "score"))
            evidence = _evidence_summary(report, evaluation)
            evidence["Missing-evidence policy"] = "Criterion-scoped; no blanket per-item deduction"
            digest = report.source_sha256[:8]
            submission_id = f"{safe_slug(team)}-{digest}" if team_override else f"{safe_slug(team)}-{safe_slug(source.stem)}-{digest}"
            return SubmissionResult(
                source=source,
                submission_id=submission_id,
                team=team,
                ps_id=ps_key,
                problem_title=problem_title,
                status="scored" if final_score is not None else "review",
                final_score=final_score,
                proposal_score=proposal_score,
                deck_quality_score=deck_quality_score,
                evaluation=evaluation,
                evidence=evidence,
            )
    except Exception as exc:
        return SubmissionResult(
            source=source,
            submission_id=fallback_id,
            team=team,
            ps_id=ps_key,
            problem_title=problem_title,
            status="failed",
            error=str(exc),
        )


def _coerce_problem(value: ProblemStatement | Mapping[str, Any] | str | Path | None) -> ProblemStatement | None:
    if value is None:
        return None
    if isinstance(value, (str, Path)):
        return load_problem_statement(value)
    return validate_problem_statement(value)


def _report_text(report: Any) -> str:
    canonical = getattr(report, "canonical", None)
    if canonical is not None:
        values = [item.get("text", "") for item in canonical.objects if isinstance(item, Mapping) and item.get("type") == "text"]
        if values:
            return "\n".join(str(value) for value in values)
    slides = getattr(report, "slides", [])
    return "\n".join(str(value) for slide in slides for value in getattr(slide, "text", []))


def _slide_numbers(report: Any) -> list[int]:
    canonical = getattr(report, "canonical", None)
    if canonical is None:
        return []
    return sorted(
        int(item["number"])
        for item in canonical.slides
        if isinstance(item, Mapping) and isinstance(item.get("number"), int) and not isinstance(item.get("number"), bool)
    )


def _render(
    report: Any,
    source: Path,
    evidence_dir: Path,
    slides: Sequence[int],
    aurochs_root: str | Path | None,
    pdf_render_dpi: int,
    render_cache_dir: str | Path | None,
) -> None:
    if getattr(report, "convenience", {}).get("adapter") == "pdf":
        render_selected_pdf_pages(
            report,
            source,
            evidence_dir,
            slides,
            cache_dir=render_cache_dir,
            dpi=pdf_render_dpi,
        )
    else:
        render_selected_slides(
            report,
            source,
            evidence_dir,
            slides,
            renderer_root=aurochs_root,
            cache_dir=render_cache_dir,
        )


def _safe_stage(report: Any, name: str, operation: Callable[[], Any]) -> None:
    try:
        operation()
    except Exception as exc:
        warnings = getattr(report, "warnings", None)
        if isinstance(warnings, list):
            warnings.append(f"Batch {name} failed: {exc}")


def _score(value: Mapping[str, Any], path: Sequence[str]) -> float | None:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    try:
        number = float(current)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _evidence_summary(report: Any, evaluation: Mapping[str, Any]) -> dict[str, Any]:
    canonical = getattr(report, "canonical", None)
    if canonical is None:
        return {}
    rendered = sum(
        1
        for item in canonical.rendered_evidence
        if isinstance(item, Mapping) and isinstance(item.get("value"), Mapping) and item["value"].get("type") == "rendered_slide"
    )
    diagrams = sum(
        1
        for item in canonical.rendered_evidence
        if "diagram" in str(item.get("value", {}).get("type", "")).casefold()
    )
    evidence_dict = {
        "Native objects": len(canonical.objects),
        "Native assets": len(canonical.assets),
        "Rendered slides": rendered,
        "OCR records": len(canonical.ocr_evidence),
        "Diagram records": diagrams,
        "Vision records": len(canonical.vision_evidence),
        "External links": len(evaluation.get("deck", {}).get("links", [])) if isinstance(evaluation.get("deck"), Mapping) else 0,
        "Warnings": len(getattr(report, "warnings", [])),
    }
    if not os.environ.get("GEMINI_API_KEY"):
        evidence_dict["Gemini status"] = "gemini key unavailable"
    return evidence_dict


def _finding_section(title: str, findings: Any, *, limit: int | None = None) -> list[str]:
    lines = [f"## {title}", ""]
    if not isinstance(findings, list) or not findings:
        lines.append("- None reported.")
        lines.append("")
        return lines
    rendered = 0
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        label = _one_line(finding.get("title", "Finding"))
        detail = _one_line(finding.get("detail", ""))
        evidence = finding.get("evidence_slides")
        suffix = f"; slides: {', '.join(str(item) for item in evidence)}" if isinstance(evidence, list) and evidence else ""
        lines.append(f"- **{label}**: {detail}{suffix}")
        rendered += 1
        if limit is not None and rendered >= limit:
            break
    lines.append("")
    return lines


def _review_sections(review: Mapping[str, Any]) -> list[str]:
    areas = review.get("area_reviews", [])
    ratings = [float(item["rating"]) for item in areas if isinstance(item, Mapping) and _finite_number(item.get("rating"))]
    overall = sum(ratings) / len(ratings) if ratings else None
    lines = ["## Reviewer Assessment", ""]
    if overall is not None:
        lines.append(f"- Overall reviewer rating: `{overall:.1f}/10`")
    lines.append(f"- Decision: `{_one_line(review.get('decision', 'needs_revision'))}`")
    lines.extend(["", "## Area Review", "", "| Area | Rating | Reason |", "| --- | ---: | --- |"])
    for item in areas:
        if not isinstance(item, Mapping):
            continue
        area = _REVIEW_AREA_LABELS.get(str(item.get("area")), str(item.get("area", "Area")))
        rating = item.get("rating", "N/A")
        reason = _one_line(item.get("reason", ""))
        missing = item.get("missing_evidence")
        if isinstance(missing, list) and missing:
            reason += f" Missing: {_one_line('; '.join(str(value) for value in missing))}."
        lines.append(f"| {_table_text(area)} | {rating}/10 | {reason} |")
    lines.append("")
    lines.extend(_review_point_section("What Earns the Score", review.get("strengths", []), "point"))
    lines.extend(_review_point_section("What Prevents a Higher Score", review.get("risks", []), "risk"))
    lines.extend(["## Judging Decision", ""])
    next_evidence = review.get("next_evidence", [])
    if isinstance(next_evidence, list) and next_evidence:
        for item in next_evidence:
            lines.append(f"- {_one_line(item)}")
    else:
        lines.append("- No additional evidence request was produced.")
    lines.append("")
    limitations = review.get("limitations", [])
    if isinstance(limitations, list) and limitations:
        lines.extend(["## Evaluation Limits", ""])
        for item in limitations:
            lines.append(f"- {_one_line(item)}")
        lines.append("")
    return lines


def _review_point_section(title: str, items: Any, kind: str) -> list[str]:
    lines = [f"## {title}", ""]
    if not isinstance(items, list) or not items:
        lines.append("- None reported.")
        lines.append("")
        return lines
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if kind == "point":
            text = _one_line(item.get("point", ""))
        else:
            text = f"**{_one_line(item.get('title', 'Risk'))}**: {_one_line(item.get('detail', ''))}"
        evidence = item.get("evidence_slides")
        suffix = f"; slides: {', '.join(str(value) for value in evidence)}" if isinstance(evidence, list) and evidence else ""
        lines.append(f"- {text}{suffix}")
    lines.append("")
    return lines


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split()).replace("|", "\\|")


def _brief(value: Any, limit: int = 180) -> str:
    text = _one_line(value)
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _table_text(value: Any) -> str:
    return _one_line(value)


def _finite_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and abs(number) != float("inf")


def _finite_value(value: Any) -> float | None:
    return float(value) if _finite_number(value) else None


def _display_number(value: Any, fallback: str) -> str:
    number = _finite_value(value)
    return f"{number:.2f}" if number is not None else fallback
