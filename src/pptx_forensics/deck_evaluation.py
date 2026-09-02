"""Deck-level evaluation built on top of the compact :mod:`evaluator` IR.

This module is deliberately separate from extraction.  It scores only the
visible evaluator projection and treats model interpretation as optional
evidence.  A missing model response never becomes a guessed score.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from statistics import mean, pstdev
from typing import Any, Mapping, Protocol, Sequence
import urllib.error
import urllib.parse
import urllib.request

from .config import load_dotenv
from .evaluator import EVALUATOR_SCHEMA_VERSION, EvaluatorIR, build_evaluator_ir
from .models import DeckIR
from .vision import VisionImage


EVALUATION_SCHEMA_VERSION = "deck-evaluation-1.0"
RUBRIC_VERSION = "deck-rubric-1.1"
SEMANTIC_SCHEMA_VERSION = "deck-semantic-evaluation-1.0"
SEMANTIC_MODEL = "gemini-2.5-flash"
SCORE_SCALE = 100.0
PROPOSAL_STRENGTH_WEIGHT = 0.70
DECK_QUALITY_WEIGHT = 0.30
MISSING_EVIDENCE_PENALTY = 15.0

SEMANTIC_COMPONENTS = (
    "problem_statement_alignment",
    "solution_clarity",
    "technical_feasibility",
    "innovation",
    "impact",
)
PROPOSAL_COMPONENT_WEIGHTS = {
    "problem_statement_alignment": 0.30,
    "solution_clarity": 0.20,
    "technical_feasibility": 0.20,
    "innovation": 0.15,
    "impact": 0.10,
    "prototype_evidence": 0.05,
}
DECK_COMPONENT_WEIGHTS = {
    "readability": 0.20,
    "layout_consistency": 0.15,
    "visual_hierarchy": 0.15,
    "evidence_visibility": 0.10,
    "content_originality": 0.10,
    "content_structure": 0.10,
    "visual_coverage": 0.10,
    "space_usage": 0.10,
}
PROTOTYPE_TERMS = (
    "demo",
    "prototype",
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "figma.com",
    "youtube.com",
    "youtu.be",
)
GOOD_EVIDENCE_STATUSES = {"verified", "partial"}
IGNORED_EVIDENCE_STATUSES = {"not_requested", "not_applicable"}
SUPPORTED_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
}
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif"}

PARAGRAPH_WORD_THRESHOLD = 45
PARAGRAPH_CHARACTER_THRESHOLD = 240
PARAGRAPH_HEAVY_RATIO = 0.55
VISUAL_TARGET_AREA_RATIO = 0.25
EXCESS_WHITESPACE_THRESHOLD = 0.35
LARGE_EMPTY_REGION_THRESHOLD = 0.20
AMBIGUOUS_TERMS = (
    "advanced",
    "better",
    "comprehensive",
    "cost effective",
    "cost-effective",
    "efficient",
    "fast",
    "improved",
    "intelligent",
    "optimized",
    "quick",
    "real time",
    "real-time",
    "robust",
    "scalable",
    "secure",
    "seamless",
)
_WORD_PATTERN = re.compile(r"[A-Za-z0-9]+(?:['’/-][A-Za-z0-9]+)*")
_URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_SENTENCE_PATTERN = re.compile(r"[.!?](?=\s|$)")
_BULLET_PATTERN = re.compile(r"(?:^|\s)[\u2022\u25aa\u25e6\u25cf\u25c9\u2023*-]\s+")
_SPECIFICITY_PATTERN = re.compile(
    r"(?:\d|%|\b(?:accuracy|baseline|benchmark|demo|github|latency|measure|metric|ms|percent|reference|source|target|test|throughput|validated|users?|devices?|requests?)\b|\b(?:using|via|through|with)\s+[A-Za-z0-9])",
    re.IGNORECASE,
)


class ProblemStatementError(ValueError):
    """Raised when a problem statement does not meet the scoring contract."""


class SemanticRequestError(RuntimeError):
    """Raised when a Gemini semantic request cannot be completed."""


@dataclass(frozen=True)
class ProblemStatement:
    id: str
    title: str
    description: str
    requirements: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "requirements": [dict(item) for item in self.requirements],
        }


class SemanticAdapter(Protocol):
    name: str
    version: str

    def analyze(self, prompt: str, images: Sequence[VisionImage], timeout: float) -> str | dict[str, Any]:
        ...


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float | None:
    result = _number(value)
    if result is None:
        return None
    return max(low, min(high, result))


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _slide_number(slide: Mapping[str, Any], fallback: int) -> int:
    value = _number(slide.get("slide_number", slide.get("number", fallback)))
    return int(value) if value is not None else fallback


def _slides(value: EvaluatorIR | Mapping[str, Any]) -> list[dict[str, Any]]:
    payload = value.to_dict() if isinstance(value, EvaluatorIR) else value
    slides = [dict(item) for item in payload.get("slides", []) if isinstance(item, Mapping)]
    return sorted(slides, key=lambda item: (_slide_number(item, 0), str(item.get("id", ""))))


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, EvaluatorIR):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    return build_evaluator_ir(value).to_dict()


def _unique_numbers(values: Sequence[Any]) -> list[int]:
    result = []
    for value in values:
        number = _number(value)
        if number is not None and number >= 1:
            result.append(int(number))
    return sorted(set(result))


def _unique_strings(values: Sequence[Any]) -> list[str]:
    return sorted({str(value) for value in values if str(value).strip()})


def _missing(values: Sequence[str]) -> list[str]:
    return _unique_strings(values)


def _score_record(
    score: Any,
    confidence: Any,
    evidence_slides: Sequence[Any],
    explanation: str,
    missing_evidence: Sequence[str] = (),
    *,
    weight: float | None = None,
) -> dict[str, Any]:
    numeric_score = _number(score)
    if numeric_score is not None:
        numeric_score = round(max(0.0, min(SCORE_SCALE, numeric_score)), 6)
    numeric_confidence = _clamp(confidence)
    result: dict[str, Any] = {
        "score": numeric_score,
        "confidence": round(numeric_confidence, 6) if numeric_confidence is not None else None,
        "evidence_slides": _unique_numbers(evidence_slides),
        "explanation": _text(explanation),
        "missing_evidence": _missing(missing_evidence),
        "status": "available" if numeric_score is not None else "unavailable",
    }
    if weight is not None:
        result["weight"] = round(weight, 6)
    return result


def _metric_detail(
    value: Any,
    evidence_slides: Sequence[Any],
    explanation: str,
    missing_evidence: Sequence[str] = (),
    **extra: Any,
) -> dict[str, Any]:
    return {
        "value": value,
        "evidence_slides": _unique_numbers(evidence_slides),
        "explanation": _text(explanation),
        "missing_evidence": _missing(missing_evidence),
        **extra,
    }


def _validate_requirement(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ProblemStatementError(f"requirement {index} must be an object")
    requirement_id = _text(item.get("id"))
    description = _text(item.get("description", item.get("text", item.get("title"))))
    weight = _number(item.get("weight"))
    if not requirement_id:
        raise ProblemStatementError(f"requirement {index} is missing id")
    if not description:
        raise ProblemStatementError(f"requirement {requirement_id} is missing description")
    if weight is None or weight < 0:
        raise ProblemStatementError(f"requirement {requirement_id} has an invalid weight")
    return {"id": requirement_id, "description": description, "weight": weight}


def validate_problem_statement(value: Any) -> ProblemStatement:
    """Validate and normalize a weighted problem statement."""
    if isinstance(value, ProblemStatement):
        return value
    if not isinstance(value, Mapping):
        raise ProblemStatementError("problem statement must be a JSON object")
    identifier = _text(value.get("id"))
    title = _text(value.get("title"))
    description = _text(value.get("description"))
    if not identifier:
        raise ProblemStatementError("problem statement is missing id")
    if not title:
        raise ProblemStatementError("problem statement is missing title")
    if not description:
        raise ProblemStatementError("problem statement is missing description")

    raw_requirements = value.get("requirements", value.get("weighted_requirements"))
    if isinstance(raw_requirements, Mapping):
        raw_requirements = [
            {
                "id": key,
                "description": item.get("description", item.get("text", key)) if isinstance(item, Mapping) else key,
                "weight": item.get("weight") if isinstance(item, Mapping) else item,
            }
            for key, item in raw_requirements.items()
        ]
    if not isinstance(raw_requirements, list) or not raw_requirements:
        raise ProblemStatementError("problem statement requires a non-empty requirements list")
    requirements = [_validate_requirement(item, index) for index, item in enumerate(raw_requirements, 1)]
    total_weight = sum(item["weight"] for item in requirements)
    if total_weight <= 0:
        raise ProblemStatementError("problem statement requirement weights must sum to a positive value")
    normalized_items = [{**item, "weight": item["weight"] / total_weight} for item in requirements]
    normalized_items[-1]["weight"] = 1.0 - sum(item["weight"] for item in normalized_items[:-1])
    normalized = tuple(normalized_items)
    return ProblemStatement(identifier, title, description, normalized)


def load_problem_statement(path: str | Path) -> ProblemStatement:
    """Load and validate a problem statement JSON file."""
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProblemStatementError(f"cannot read problem statement: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProblemStatementError(f"problem statement is not valid JSON: {exc}") from exc
    return validate_problem_statement(value)


def load_deck_ir(path: str | Path) -> DeckIR:
    """Load a canonical DeckIR JSON file and validate its authority boundary."""
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read DeckIR: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"DeckIR is not valid JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError("DeckIR must be a JSON object")
    try:
        deck = DeckIR(**dict(value))
        deck.validate()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid DeckIR: {exc}") from exc
    return deck


def _metric_values(slides: Sequence[Mapping[str, Any]], key: str) -> tuple[list[float], list[int], list[int]]:
    values: list[float] = []
    present: list[int] = []
    missing: list[int] = []
    for index, slide in enumerate(slides, 1):
        number = _slide_number(slide, index)
        metrics = slide.get("metrics") if isinstance(slide.get("metrics"), Mapping) else {}
        value = _number(metrics.get(key))
        if value is None:
            missing.append(number)
        else:
            values.append(value)
            present.append(number)
    return values, present, missing


def _words(value: Any) -> list[str]:
    return _WORD_PATTERN.findall(_text(value))


def _ambiguous_terms(value: Any) -> list[str]:
    text = _URL_PATTERN.sub("", _text(value))
    if not text:
        return []
    sentences = [part for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]
    if not sentences:
        sentences = [text]
    found: set[str] = set()
    for term in AMBIGUOUS_TERMS:
        pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        for sentence in sentences:
            if not pattern.search(sentence) or len(_words(sentence)) < 4:
                continue
            if _SPECIFICITY_PATTERN.search(sentence):
                continue
            found.add(term)
            break
    return sorted(found)


def _text_profile(block: Mapping[str, Any]) -> dict[str, Any]:
    text = _text(block.get("text"))
    words = _words(text)
    clean_text = _URL_PATTERN.sub("", text)
    sentence_count = len(_SENTENCE_PATTERN.findall(clean_text))
    label_count = len(re.findall(r"\b[A-Za-z][A-Za-z0-9 /&+()'-]{1,30}:", clean_text))
    bullet_count = len(_BULLET_PATTERN.findall(text))
    paragraph_like = (
        len(words) >= PARAGRAPH_WORD_THRESHOLD
        or len(text) >= PARAGRAPH_CHARACTER_THRESHOLD
        or sentence_count >= 3
    )
    pointer_like = not paragraph_like and (
        len(words) <= 18 or label_count > 0 or bullet_count > 0
    )
    return {
        "character_count": len(text),
        "word_count": len(words),
        "sentence_count": sentence_count,
        "label_count": label_count,
        "bullet_count": bullet_count,
        "paragraph_like": paragraph_like,
        "pointer_like": pointer_like,
        "ambiguous_terms": _ambiguous_terms(text),
        "object_id": block.get("object_id"),
    }


def _body_blocks(slide: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    blocks = slide.get("text_blocks")
    if not isinstance(blocks, list):
        return []
    title = slide.get("title") if isinstance(slide.get("title"), Mapping) else {}
    title_object_id = title.get("object_id")
    return [
        block
        for block in blocks
        if isinstance(block, Mapping)
        and _text(block.get("text"))
        and block.get("object_id") != title_object_id
    ]


def _visible_box_area(slide: Mapping[str, Any]) -> float:
    areas: list[float] = []
    for collection_name in ("text_blocks", "visual_evidence", "tables"):
        collection = slide.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, Mapping):
                continue
            box = item.get("bbox")
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                continue
            try:
                width = max(0.0, min(1.0, float(box[2])))
                height = max(0.0, min(1.0, float(box[3])))
            except (TypeError, ValueError):
                continue
            areas.append(width * height)
    return min(1.0, sum(areas))


def _content_structure_metrics(
    slides: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not slides:
        empty = _metric_detail(None, [], "Content structure cannot be measured without slides.", ["no slides"])
        return {
            "paragraph_content_ratio": None,
            "pointer_content_ratio": None,
            "paragraph_heavy_slide_ratio": None,
            "ambiguous_claim_ratio": None,
            "content_structure_score": None,
        }, {
            "paragraph_content_ratio": empty,
            "pointer_content_ratio": empty,
            "paragraph_heavy_slide_ratio": empty,
            "ambiguous_claim_ratio": empty,
            "content_structure_score": empty,
        }

    total_body_characters = 0
    paragraph_characters = 0
    pointer_characters = 0
    body_block_count = 0
    ambiguous_block_count = 0
    paragraph_heavy_slides = 0
    body_slides = 0
    structure_values: list[float] = []
    structure_slide_numbers: list[int] = []
    paragraph_slides: list[int] = []
    ambiguous_slides: list[int] = []
    slide_profiles: dict[str, dict[str, Any]] = {}

    for index, slide in enumerate(slides, 1):
        number = _slide_number(slide, index)
        profiles = [_text_profile(block) for block in _body_blocks(slide)]
        body_characters = sum(item["character_count"] for item in profiles)
        body_slides += int(bool(body_characters))
        total_body_characters += body_characters
        paragraph_count = sum(item["character_count"] for item in profiles if item["paragraph_like"])
        pointer_count = sum(item["character_count"] for item in profiles if item["pointer_like"])
        paragraph_characters += paragraph_count
        pointer_characters += pointer_count
        body_block_count += len(profiles)
        ambiguous_terms = sorted({term for item in profiles for term in item["ambiguous_terms"]})
        ambiguous_blocks = sum(bool(item["ambiguous_terms"]) for item in profiles)
        ambiguous_block_count += ambiguous_blocks
        if ambiguous_terms:
            ambiguous_slides.append(number)
        paragraph_ratio = paragraph_count / body_characters if body_characters else 0.0
        pointer_ratio = pointer_count / body_characters if body_characters else 0.0
        paragraph_heavy = bool(body_characters and paragraph_ratio >= PARAGRAPH_HEAVY_RATIO)
        if paragraph_heavy:
            paragraph_heavy_slides += 1
            paragraph_slides.append(number)

        if body_characters:
            base_score = 1.0 if paragraph_ratio <= 0.35 else max(0.0, (1.0 - paragraph_ratio) / 0.65)
            score = max(0.0, base_score - min(0.25, ambiguous_blocks * 0.05))
        else:
            has_visual = bool(slide.get("visual_evidence") or slide.get("tables"))
            score = 0.70 if has_visual else 0.0
        structure_values.append(score)
        structure_slide_numbers.append(number)
        slide_profiles[str(number)] = {
            "character_count": body_characters,
            "word_count": sum(item["word_count"] for item in profiles),
            "paragraph_block_count": sum(item["paragraph_like"] for item in profiles),
            "pointer_block_count": sum(item["pointer_like"] for item in profiles),
            "paragraph_character_count": paragraph_count,
            "pointer_character_count": pointer_count,
            "paragraph_ratio": round(paragraph_ratio, 6),
            "pointer_ratio": round(pointer_ratio, 6),
            "ambiguous_block_count": ambiguous_blocks,
            "ambiguous_terms": ambiguous_terms,
            "paragraph_heavy": paragraph_heavy,
            "structure_score": round(score, 6),
        }

    paragraph_ratio = paragraph_characters / total_body_characters if total_body_characters else 0.0
    pointer_ratio = pointer_characters / total_body_characters if total_body_characters else 0.0
    paragraph_heavy_ratio = paragraph_heavy_slides / body_slides if body_slides else 0.0
    ambiguous_ratio = ambiguous_block_count / body_block_count if body_block_count else 0.0
    structure_score = mean(structure_values) if structure_values else None
    common = {
        "slide_profiles": slide_profiles,
        "body_slide_count": body_slides,
        "body_block_count": body_block_count,
        "ambiguous_block_count": ambiguous_block_count,
    }
    metrics = {
        "paragraph_content_ratio": round(paragraph_ratio, 6),
        "pointer_content_ratio": round(pointer_ratio, 6),
        "paragraph_heavy_slide_ratio": round(paragraph_heavy_ratio, 6),
        "ambiguous_claim_ratio": round(ambiguous_ratio, 6),
        "content_structure_score": round(structure_score, 6) if structure_score is not None else None,
    }
    details = {
        "paragraph_content_ratio": _metric_detail(
            metrics["paragraph_content_ratio"],
            paragraph_slides or structure_slide_numbers,
            "Fraction of body-text characters in paragraph-like blocks; long prose is penalized when pointers would be clearer.",
            [],
            slide_values={key: value["paragraph_ratio"] for key, value in slide_profiles.items()},
            **common,
        ),
        "pointer_content_ratio": _metric_detail(
            metrics["pointer_content_ratio"],
            structure_slide_numbers,
            "Fraction of body-text characters in concise, label-like or pointer-like blocks.",
            [],
            slide_values={key: value["pointer_ratio"] for key, value in slide_profiles.items()},
            **common,
        ),
        "paragraph_heavy_slide_ratio": _metric_detail(
            metrics["paragraph_heavy_slide_ratio"],
            paragraph_slides or structure_slide_numbers,
            "Fraction of slides with body text that is at least 55% paragraph-like by character count.",
            [],
            paragraph_heavy_slides=paragraph_slides,
            slide_values={key: value["paragraph_heavy"] for key, value in slide_profiles.items()},
            **common,
        ),
        "ambiguous_claim_ratio": _metric_detail(
            metrics["ambiguous_claim_ratio"],
            ambiguous_slides,
            "Fraction of body-text blocks containing broad claims without a nearby measurable target or supporting mechanism.",
            [],
            slide_values={key: value["ambiguous_block_count"] for key, value in slide_profiles.items()},
            **common,
        ),
        "content_structure_score": _metric_detail(
            metrics["content_structure_score"],
            structure_slide_numbers,
            "Content structure rewards concise pointers, penalizes paragraph-heavy slides, and flags unsupported broad claims.",
            [],
            slide_values={key: value["structure_score"] for key, value in slide_profiles.items()},
            **common,
        ),
    }
    return metrics, details


def _visual_coverage(slides: Sequence[Mapping[str, Any]]) -> tuple[float | None, dict[str, Any]]:
    if not slides:
        return None, _metric_detail(None, [], "Visual coverage cannot be measured without slides.", ["no slides"])
    values: dict[str, float] = {}
    area_values: dict[str, float] = {}
    evidence: list[int] = []
    missing: list[str] = []
    for index, slide in enumerate(slides, 1):
        number = _slide_number(slide, index)
        slide_metrics = slide.get("metrics") if isinstance(slide.get("metrics"), Mapping) else {}
        area = _number(slide_metrics.get("visual_area_ratio"))
        if area is None:
            area = _visible_box_area(slide) if slide.get("visual_evidence") or slide.get("tables") else None
        if area is None:
            missing.append(f"visual coverage is missing on slide {number}")
            continue
        area = max(0.0, min(1.0, area))
        area_values[str(number)] = round(area, 6)
        values[str(number)] = round(min(1.0, area / VISUAL_TARGET_AREA_RATIO), 6)
        evidence.append(number)
    value = round(mean(values.values()), 6) if values else None
    return value, _metric_detail(
        value,
        evidence,
        "Visual coverage is the mean meaningful visual area normalized to a 25% per-slide target; decorative noise is excluded by the evaluator projection.",
        missing,
        slide_values=values,
        visual_area_ratios=area_values,
        target_area_ratio=VISUAL_TARGET_AREA_RATIO,
    )


def _space_usage(slides: Sequence[Mapping[str, Any]]) -> tuple[float | None, dict[str, Any]]:
    if not slides:
        return None, _metric_detail(None, [], "Space usage cannot be measured without slides.", ["no slides"])
    values: dict[str, float] = {}
    whitespace_values: dict[str, float] = {}
    largest_values: dict[str, float] = {}
    balance_values: dict[str, float] = {}
    evidence: list[int] = []
    missing: list[str] = []
    for index, slide in enumerate(slides, 1):
        number = _slide_number(slide, index)
        slide_metrics = slide.get("metrics") if isinstance(slide.get("metrics"), Mapping) else {}
        whitespace = _number(slide_metrics.get("whitespace_area_ratio"))
        if whitespace is None:
            occupied = _number(slide_metrics.get("occupied_area_ratio"))
            if occupied is not None:
                whitespace = 1.0 - max(0.0, min(1.0, occupied))
            elif slide.get("text_blocks") or slide.get("visual_evidence") or slide.get("tables"):
                whitespace = 1.0 - _visible_box_area(slide)
        if whitespace is None:
            missing.append(f"space usage is missing on slide {number}")
            continue
        largest = _number(slide_metrics.get("largest_empty_region_ratio"))
        if largest is None:
            largest = whitespace
        whitespace = max(0.0, min(1.0, whitespace))
        largest = max(0.0, min(1.0, largest))
        excess_whitespace = max(0.0, (whitespace - EXCESS_WHITESPACE_THRESHOLD) / (1.0 - EXCESS_WHITESPACE_THRESHOLD))
        excess_largest = max(0.0, (largest - LARGE_EMPTY_REGION_THRESHOLD) / (1.0 - LARGE_EMPTY_REGION_THRESHOLD))
        score = max(0.0, 1.0 - 0.65 * excess_whitespace - 0.35 * excess_largest)
        values[str(number)] = round(score, 6)
        whitespace_values[str(number)] = round(whitespace, 6)
        largest_values[str(number)] = round(largest, 6)
        balance = _number(slide_metrics.get("whitespace_balance"))
        if balance is not None:
            balance_values[str(number)] = round(balance, 6)
        evidence.append(number)
    value = round(mean(values.values()), 6) if values else None
    return value, _metric_detail(
        value,
        evidence,
        "Space usage penalizes whitespace above 35% and any single empty region above 20%, while allowing normal margins and visual breathing room.",
        missing,
        slide_values=values,
        whitespace_area_ratios=whitespace_values,
        largest_empty_region_ratios=largest_values,
        whitespace_balances=balance_values,
        excess_whitespace_threshold=EXCESS_WHITESPACE_THRESHOLD,
        large_empty_region_threshold=LARGE_EMPTY_REGION_THRESHOLD,
    )


def _mean_metric(slides: Sequence[Mapping[str, Any]], key: str, label: str) -> tuple[float | None, dict[str, Any]]:
    values, present, missing_slides = _metric_values(slides, key)
    missing = [f"{label} is missing on slide {number}" for number in missing_slides]
    value = round(mean(values), 6) if values else None
    if missing_slides:
        explanation = f"Mean {label} from {len(values)} slide-level measurements."
    else:
        explanation = f"Mean {label} across all slides."
    return value, _metric_detail(
        value,
        present,
        explanation,
        missing,
        slide_values={str(number): round(item, 6) for number, item in zip(present, values)},
    )


def _evidence_visibility(slides: Sequence[Mapping[str, Any]]) -> tuple[float | None, dict[str, Any]]:
    if not slides:
        return None, _metric_detail(None, [], "Evidence visibility cannot be measured without slides.", ["no slides"])
    stage_counts: dict[str, dict[str, int]] = {}
    slide_values: dict[str, float] = {}
    evidence_slides: list[int] = []
    missing: list[str] = []
    for index, slide in enumerate(slides, 1):
        number = _slide_number(slide, index)
        visibility = slide.get("evidence_visibility")
        if not isinstance(visibility, Mapping):
            visibility = {}
        considered = [str(value) for value in visibility.values() if str(value) not in IGNORED_EVIDENCE_STATUSES]
        good = [value for value in considered if value in GOOD_EVIDENCE_STATUSES]
        if considered:
            slide_value = len(good) / len(considered)
            evidence_slides.append(number)
            for stage, status in visibility.items():
                bucket = stage_counts.setdefault(str(stage), {"considered": 0, "visible": 0})
                if str(status) not in IGNORED_EVIDENCE_STATUSES:
                    bucket["considered"] += 1
                    bucket["visible"] += int(str(status) in GOOD_EVIDENCE_STATUSES)
        else:
            visible = bool(slide.get("visible_text") or slide.get("text_blocks") or slide.get("visual_evidence") or slide.get("tables"))
            slide_value = 1.0 if visible else 0.0
            if not visible:
                missing.append(f"no visible evidence on slide {number}")
        slide_values[str(number)] = round(slide_value, 6)
    value = round(mean(slide_values.values()), 6) if slide_values else None
    return value, _metric_detail(
        value,
        evidence_slides,
        "Average proportion of requested evidence stages with visible or partial evidence.",
        missing,
        slide_values=slide_values,
        stages=stage_counts,
    )


def _slide_type_coverage(slides: Sequence[Mapping[str, Any]]) -> tuple[float | None, dict[str, Any]]:
    if not slides:
        return None, _metric_detail(None, [], "Slide-type coverage cannot be measured without slides.", ["no slides"])
    known = [slide for slide in slides if _text(slide.get("slide_type")) not in {"", "unknown"}]
    types = sorted({_text(slide.get("slide_type")) for slide in known})
    evidence = [_slide_number(slide, index) for index, slide in enumerate(slides, 1) if slide in known]
    value = round(len(known) / len(slides), 6)
    return value, _metric_detail(
        value,
        evidence,
        "Fraction of slides with a non-unknown evaluator slide type.",
        ["slide type is unknown on at least one slide"] if len(known) != len(slides) else [],
        known_types=types,
        known_slide_count=len(known),
        slide_count=len(slides),
    )


def _duplicate_content(slides: Sequence[Mapping[str, Any]]) -> tuple[float | None, dict[str, Any]]:
    if not slides:
        return None, _metric_detail(None, [], "Duplicate content cannot be measured without slides.", ["no slides"])
    groups: dict[str, list[int]] = {}
    for index, slide in enumerate(slides, 1):
        text = _text(slide.get("visible_text")).casefold()
        if text:
            groups.setdefault(text, []).append(_slide_number(slide, index))
    populated = sum(len(values) for values in groups.values())
    duplicated = sum(len(values) - 1 for values in groups.values() if len(values) > 1)
    value = round(duplicated / populated, 6) if populated else None
    duplicate_groups = [values for values in groups.values() if len(values) > 1]
    evidence = sorted({number for values in duplicate_groups for number in values})
    return value, _metric_detail(
        value,
        evidence,
        "Fraction of non-empty slide text that repeats an earlier slide verbatim after whitespace normalization.",
        ["no non-empty visible slide text"] if not populated else [],
        duplicate_slide_groups=duplicate_groups,
    )


def _prototype_evidence(payload: Mapping[str, Any], slides: Sequence[Mapping[str, Any]]) -> tuple[float | None, dict[str, Any]]:
    deck = payload.get("deck") if isinstance(payload.get("deck"), Mapping) else {}
    links = [item for item in deck.get("links", []) if isinstance(item, Mapping)]
    prototype_links = [
        item
        for item in links
        if any(term in str(item.get("target", "")).casefold() for term in PROTOTYPE_TERMS)
    ]
    evidence_roles = {"screenshot", "evidence_image", "chart", "diagram_candidate"}
    evidence_images = [
        (slide_number, image)
        for index, slide in enumerate(slides, 1)
        for slide_number in [_slide_number(slide, index)]
        for image in (slide.get("visual_evidence", []) if isinstance(slide.get("visual_evidence"), list) else [])
        if isinstance(image, Mapping) and image.get("role") in evidence_roles and image.get("scoring_relevant") is not False
    ]
    has_evidence = bool(prototype_links or evidence_images)
    value = 1.0 if has_evidence else 0.0
    slides_with_evidence = [number for number, _ in evidence_images]
    slides_with_evidence.extend(
        int(number)
        for link in prototype_links
        for number in (link.get("slides", []) if isinstance(link.get("slides"), list) else [])
        if _number(number) is not None
    )
    missing = [] if has_evidence else ["no prototype/demo link or meaningful evidence image was found"]
    return value, _metric_detail(
        value,
        slides_with_evidence,
        "Concrete prototype evidence is present when a demo/repository/prototype link or meaningful evidence image is available.",
        missing,
        external_link_count=len(links),
        prototype_link_count=len(prototype_links),
        evidence_image_count=len(evidence_images),
        prototype_links=[dict(item) for item in prototype_links],
    )


def compute_deterministic_metrics(value: EvaluatorIR | Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Compute evaluator metrics without model interpretation or slide counts as quality scores."""
    payload = _payload(value)
    slides = _slides(payload)
    metrics: dict[str, Any] = {}
    details: dict[str, dict[str, Any]] = {}

    title_values = [
        _slide_number(slide, index)
        for index, slide in enumerate(slides, 1)
        if isinstance(slide.get("title"), Mapping) and _text(slide["title"].get("text"))
    ]
    title_value = round(len(title_values) / len(slides), 6) if slides else None
    metrics["title_coverage"] = title_value
    details["title_coverage"] = _metric_detail(
        title_value,
        title_values,
        "Fraction of slides with a non-empty extracted title candidate.",
        ["no slides"] if not slides else [],
        titled_slide_count=len(title_values),
        slide_count=len(slides),
    )

    for key, label in (
        ("text_density", "text density"),
        ("small_text_ratio", "small-text ratio"),
        ("overlap_ratio", "overlap ratio"),
        ("clipping_rate", "clipping rate"),
        ("occupied_area_ratio", "occupied area ratio"),
        ("whitespace_area_ratio", "whitespace area ratio"),
        ("largest_empty_region_ratio", "largest empty-region ratio"),
        ("visual_area_ratio", "meaningful visual area ratio"),
    ):
        value_for_metric, detail = _mean_metric(slides, key, label)
        metrics[key] = value_for_metric
        details[key] = detail

    structure_metrics, structure_details = _content_structure_metrics(slides)
    metrics.update(structure_metrics)
    details.update(structure_details)

    visual_value, visual_detail = _visual_coverage(slides)
    metrics["visual_coverage"] = visual_value
    details["visual_coverage"] = visual_detail

    space_value, space_detail = _space_usage(slides)
    metrics["space_usage"] = space_value
    details["space_usage"] = space_detail

    density_values, density_slides, density_missing = _metric_values(slides, "text_density")
    density_variation = round(pstdev(density_values), 6) if density_values else None
    metrics["content_density_variation"] = density_variation
    details["content_density_variation"] = _metric_detail(
        density_variation,
        density_slides,
        "Population standard deviation of slide text density.",
        [f"text density is missing on slide {number}" for number in density_missing],
        slide_values={str(number): round(item, 6) for number, item in zip(density_slides, density_values)},
    )

    type_value, type_detail = _slide_type_coverage(slides)
    metrics["slide_type_coverage"] = type_value
    details["slide_type_coverage"] = type_detail

    visibility_value, visibility_detail = _evidence_visibility(slides)
    metrics["evidence_visibility"] = visibility_value
    details["evidence_visibility"] = visibility_detail

    duplicate_value, duplicate_detail = _duplicate_content(slides)
    metrics["duplicate_content"] = duplicate_value
    metrics["duplicate_content_ratio"] = duplicate_value
    details["duplicate_content"] = duplicate_detail

    prototype_value, prototype_detail = _prototype_evidence(payload, slides)
    metrics["link_prototype_evidence"] = prototype_value
    details["link_prototype_evidence"] = prototype_detail
    return metrics, details


def _metric_score(
    metrics: Mapping[str, Any],
    details: Mapping[str, Mapping[str, Any]],
    key: str,
    transform: Any,
    explanation: str,
) -> dict[str, Any]:
    value = _number(metrics.get(key))
    detail = details.get(key, {})
    evidence = detail.get("evidence_slides", []) if isinstance(detail, Mapping) else []
    missing = detail.get("missing_evidence", []) if isinstance(detail, Mapping) else []
    score = transform(value) if value is not None else None
    confidence = 1.0 if value is not None and not missing else 0.75 if value is not None else None
    return _score_record(score, confidence, evidence, explanation, missing)


def _weighted_group(
    name: str,
    component_scores: Mapping[str, Mapping[str, Any]],
    weights: Mapping[str, float],
    explanation: str,
) -> dict[str, Any]:
    components: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    unavailable: list[str] = []
    evidence: list[int] = []
    weighted_confidence = 0.0
    weighted_penalty = 0.0
    for component, weight in weights.items():
        source = dict(component_scores.get(component, _score_record(None, None, [], "No score was produced.", [f"missing component: {component}"])))
        source["weight"] = round(weight, 6)
        components[component] = source
        evidence.extend(source.get("evidence_slides", []))
        source_missing = _missing(source.get("missing_evidence", []))
        source["missing_evidence"] = source_missing
        missing.extend(source_missing)
        if source.get("score") is None:
            unavailable.append(f"{component} score is unavailable")
        elif source.get("confidence") is not None:
            weighted_confidence += weight * float(source["confidence"])
            raw_score = _number(source["score"])
            penalty = min(raw_score, MISSING_EVIDENCE_PENALTY * len(source_missing)) if raw_score is not None else 0.0
            if penalty:
                source["unpenalized_score"] = round(raw_score, 6)
                source["missing_evidence_penalty"] = round(penalty, 6)
                source["score"] = round(raw_score - penalty, 6)
            weighted_penalty += weight * penalty
    if unavailable:
        missing.extend(unavailable)
        group_explanation = f"{explanation} The group remains unavailable until all weighted components have evidence."
        confidence = None
        score = None
    else:
        score = sum(float(components[key]["score"]) * weight for key, weight in weights.items())
        confidence = weighted_confidence
        group_explanation = explanation
    result = {
        **_score_record(score, confidence, evidence, group_explanation, missing),
        "name": name,
        "components": components,
    }
    if score is not None and weighted_penalty:
        result["unpenalized_score"] = round(score + weighted_penalty, 6)
        result["missing_evidence_penalty"] = round(weighted_penalty, 6)
    return result


def _deterministic_scores(
    metrics: Mapping[str, Any],
    details: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    readability = _score_record(
        None,
        None,
        [],
        "Readability combines title coverage, small-text ratio, overlap, and clipping.",
        [],
    )
    required = ("title_coverage", "small_text_ratio", "overlap_ratio", "clipping_rate")
    if all(_number(metrics.get(key)) is not None for key in required):
        score = (
            _number(metrics["title_coverage"]) * 0.35
            + (1 - _number(metrics["small_text_ratio"])) * 0.25
            + (1 - _number(metrics["overlap_ratio"])) * 0.20
            + (1 - _number(metrics["clipping_rate"])) * 0.20
        ) * SCORE_SCALE
        evidence = sorted({slide for key in required for slide in details.get(key, {}).get("evidence_slides", [])})
        missing = [item for key in required for item in details.get(key, {}).get("missing_evidence", [])]
        readability = _score_record(
            score,
            0.75 if missing else 1.0,
            evidence,
            "Readability combines title coverage (35%), inverse small-text ratio (25%), inverse overlap (20%), and inverse clipping (20%).",
            missing,
        )

    layout = _score_record(None, None, [], "Layout consistency combines density variation, overlap, and clipping.", [])
    required = ("content_density_variation", "overlap_ratio", "clipping_rate")
    if all(_number(metrics.get(key)) is not None for key in required):
        density_consistency = 1 - min(1.0, _number(metrics["content_density_variation"]) / 0.20)
        score = (density_consistency * 0.45 + (1 - _number(metrics["overlap_ratio"])) * 0.30 + (1 - _number(metrics["clipping_rate"])) * 0.25) * SCORE_SCALE
        evidence = sorted({slide for key in required for slide in details.get(key, {}).get("evidence_slides", [])})
        missing = [item for key in required for item in details.get(key, {}).get("missing_evidence", [])]
        layout = _score_record(
            score,
            0.75 if missing else 1.0,
            evidence,
            "Layout consistency uses inverse content-density variation (45%), inverse overlap (30%), and inverse clipping (25%).",
            missing,
        )

    hierarchy = _score_record(None, None, [], "Visual hierarchy combines title and slide-type evidence.", [])
    required = ("title_coverage", "slide_type_coverage")
    if all(_number(metrics.get(key)) is not None for key in required):
        score = (_number(metrics["title_coverage"]) * 0.60 + _number(metrics["slide_type_coverage"]) * 0.40) * SCORE_SCALE
        evidence = sorted({slide for key in required for slide in details.get(key, {}).get("evidence_slides", [])})
        missing = [item for key in required for item in details.get(key, {}).get("missing_evidence", [])]
        hierarchy = _score_record(score, 0.75 if missing else 1.0, evidence, "Visual hierarchy combines title coverage (60%) and non-unknown slide-type coverage (40%).", missing)

    visibility = _metric_score(metrics, details, "evidence_visibility", lambda value: value * SCORE_SCALE, "Evidence visibility is scored from visible or partial evidence stages.")
    originality = _metric_score(metrics, details, "duplicate_content", lambda value: (1 - value) * SCORE_SCALE, "Content originality is the inverse of duplicate visible slide text.")
    prototype = _metric_score(metrics, details, "link_prototype_evidence", lambda value: value * SCORE_SCALE, "Prototype evidence is based on concrete links or meaningful evidence images, not counts alone.")
    content_structure = _metric_score(
        metrics,
        details,
        "content_structure_score",
        lambda value: value * SCORE_SCALE,
        "Content structure rewards pointer-friendly text and penalizes paragraph-heavy or broad unsupported claims.",
    )
    visual_coverage = _metric_score(
        metrics,
        details,
        "visual_coverage",
        lambda value: value * SCORE_SCALE,
        "Visual coverage rewards meaningful visuals occupying a useful portion of each slide, not decorative image counts.",
    )
    space_usage = _metric_score(
        metrics,
        details,
        "space_usage",
        lambda value: value * SCORE_SCALE,
        "Space usage penalizes excessive unused slide area and unusually large empty regions.",
    )
    return {
        "readability": readability,
        "layout_consistency": layout,
        "visual_hierarchy": hierarchy,
        "evidence_visibility": visibility,
        "content_originality": originality,
        "content_structure": content_structure,
        "visual_coverage": visual_coverage,
        "space_usage": space_usage,
        "prototype_evidence": prototype,
    }


def _finding(
    category: str,
    title: str,
    detail: str,
    evidence_slides: Sequence[Any] = (),
    *,
    metric: str | None = None,
    value: Any = None,
    score: Any = None,
    confidence: Any = None,
    missing_evidence: Sequence[str] = (),
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "category": category,
        "title": _text(title),
        "detail": _text(detail),
        "evidence_slides": _unique_numbers(evidence_slides),
    }
    if metric is not None:
        result["metric"] = metric
    for key, number in (("value", value), ("score", score), ("confidence", confidence)):
        normalized = _number(number)
        if normalized is not None:
            result[key] = round(normalized, 6)
    missing = _missing(missing_evidence)
    if missing:
        result["missing_evidence"] = missing
    return result


def _build_findings(
    metrics: Mapping[str, Any],
    details: Mapping[str, Mapping[str, Any]],
    deterministic: Mapping[str, Mapping[str, Any]],
    semantic: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    strengths: list[dict[str, Any]] = []
    weaknesses: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []

    structure_value = _number(metrics.get("content_structure_score"))
    structure_detail = details.get("content_structure_score", {})
    structure_profiles = structure_detail.get("slide_profiles", {}) if isinstance(structure_detail, Mapping) else {}
    paragraph_slides = [
        int(number)
        for number, profile in structure_profiles.items()
        if isinstance(profile, Mapping) and profile.get("paragraph_heavy")
    ]
    paragraph_ratio = _number(metrics.get("paragraph_content_ratio"))
    if structure_value is not None:
        if structure_value < 0.60 or paragraph_slides:
            detail = f"{paragraph_ratio:.0%} of body-text characters are in paragraph-like blocks. Convert the cited slides to short pointers and keep explanations in the spoken pitch."
            weaknesses.append(
                _finding(
                    "content_structure",
                    "Paragraph-heavy slides",
                    detail,
                    paragraph_slides or structure_detail.get("evidence_slides", []),
                    metric="paragraph_content_ratio",
                    value=paragraph_ratio,
                    score=structure_value * SCORE_SCALE,
                )
            )
        elif structure_value >= 0.85:
            strengths.append(
                _finding(
                    "content_structure",
                    "Pointer-friendly content",
                    "Body content is predominantly concise, label-like, or pointer-oriented rather than long prose.",
                    structure_detail.get("evidence_slides", []),
                    metric="content_structure_score",
                    value=structure_value,
                    score=structure_value * SCORE_SCALE,
                )
            )
        else:
            ambiguous.append(
                _finding(
                    "content_structure",
                    "Mixed pointer and paragraph structure",
                    "Some content is presentation-friendly, but the current structure does not consistently distinguish speaking points from slide text.",
                    structure_detail.get("evidence_slides", []),
                    metric="content_structure_score",
                    value=structure_value,
                    score=structure_value * SCORE_SCALE,
                )
            )

    for number, profile in structure_profiles.items():
        if not isinstance(profile, Mapping) or not profile.get("ambiguous_terms"):
            continue
        terms = ", ".join(str(item) for item in profile["ambiguous_terms"])
        ambiguous.append(
            _finding(
                "ambiguity",
                "Broad claims need qualification",
                f"Slide {number} uses broad terms ({terms}) without a nearby measurable target or supporting mechanism. Define the claim or add evidence.",
                [number],
                metric="ambiguous_claim_ratio",
                value=metrics.get("ambiguous_claim_ratio"),
            )
        )

    visual_value = _number(metrics.get("visual_coverage"))
    visual_detail = details.get("visual_coverage", {})
    visual_values = visual_detail.get("slide_values", {}) if isinstance(visual_detail, Mapping) else {}
    low_visual_slides = [
        int(number)
        for number, value in visual_values.items()
        if _number(value) is not None and _number(value) < 0.35
    ]
    if visual_value is not None:
        if visual_value < 0.35:
            weaknesses.append(
                _finding(
                    "visual_coverage",
                    "Too little meaningful visual support",
                    "Meaningful visuals occupy a small share of the deck. Replace some text with diagrams, screenshots, charts, or other evidence that explains the claim.",
                    low_visual_slides or visual_detail.get("evidence_slides", []),
                    metric="visual_coverage",
                    value=visual_value,
                    score=visual_value * SCORE_SCALE,
                )
            )
        elif visual_value >= 0.65:
            strengths.append(
                _finding(
                    "visual_coverage",
                    "Meaningful visual support",
                    "Meaningful visuals occupy a useful share of the slides; decorative image counts are not used as a substitute for evidence.",
                    visual_detail.get("evidence_slides", []),
                    metric="visual_coverage",
                    value=visual_value,
                    score=visual_value * SCORE_SCALE,
                )
            )
        else:
            ambiguous.append(
                _finding(
                    "visual_coverage",
                    "Visual support is uneven",
                    "The deck contains visuals, but their coverage varies enough that some claims may still read as text-only.",
                    low_visual_slides or visual_detail.get("evidence_slides", []),
                    metric="visual_coverage",
                    value=visual_value,
                    score=visual_value * SCORE_SCALE,
                )
            )

    space_value = _number(metrics.get("space_usage"))
    space_detail = details.get("space_usage", {})
    space_values = space_detail.get("slide_values", {}) if isinstance(space_detail, Mapping) else {}
    low_space_slides = [
        int(number)
        for number, value in space_values.items()
        if _number(value) is not None and _number(value) < 0.65
    ]
    if space_value is not None:
        if space_value < 0.65 or low_space_slides:
            weaknesses.append(
                _finding(
                    "space_usage",
                    "Excess unused slide area",
                    "The cited slides contain large unused regions or blank space. Rebalance the layout or use the space for visuals and concise supporting points.",
                    low_space_slides or space_detail.get("evidence_slides", []),
                    metric="space_usage",
                    value=space_value,
                    score=space_value * SCORE_SCALE,
                )
            )
        elif space_value >= 0.85:
            strengths.append(
                _finding(
                    "space_usage",
                    "Space is used deliberately",
                    "Slide occupancy stays within the rubric's normal breathing-room range without large unused regions.",
                    space_detail.get("evidence_slides", []),
                    metric="space_usage",
                    value=space_value,
                    score=space_value * SCORE_SCALE,
                )
            )
        else:
            ambiguous.append(
                _finding(
                    "space_usage",
                    "Space usage is uneven",
                    "Most slide area is usable, but some slides leave enough unused space to warrant a layout review.",
                    low_space_slides or space_detail.get("evidence_slides", []),
                    metric="space_usage",
                    value=space_value,
                    score=space_value * SCORE_SCALE,
                )
            )

    readability = deterministic.get("readability", {})
    readability_score = _number(readability.get("score")) if isinstance(readability, Mapping) else None
    if readability_score is not None and readability_score < 60:
        weaknesses.append(
            _finding(
                "readability",
                "Readability risk",
                "The combined title, overlap, clipping, and small-text signals indicate that the cited layout may be difficult to scan.",
                readability.get("evidence_slides", []),
                metric="readability",
                score=readability_score,
            )
        )

    prototype = deterministic.get("prototype_evidence", {})
    prototype_score = _number(prototype.get("score")) if isinstance(prototype, Mapping) else None
    if prototype_score == 0:
        weaknesses.append(
            _finding(
                "prototype_evidence",
                "Prototype evidence is not visible",
                "No concrete prototype/demo link or meaningful evidence image was found in the evaluator-visible deck content.",
                prototype.get("evidence_slides", []),
                metric="link_prototype_evidence",
                value=metrics.get("link_prototype_evidence"),
                score=prototype_score,
                missing_evidence=prototype.get("missing_evidence", []),
            )
        )
    elif prototype_score is not None and prototype_score >= 100:
        strengths.append(
            _finding(
                "prototype_evidence",
                "Concrete prototype evidence",
                "A concrete prototype/demo link or meaningful evidence image is visible in the deck.",
                prototype.get("evidence_slides", []),
                metric="link_prototype_evidence",
                value=metrics.get("link_prototype_evidence"),
                score=prototype_score,
            )
        )

    semantic_status = str(semantic.get("status") or "unavailable")
    semantic_scores = semantic.get("scores") if isinstance(semantic.get("scores"), Mapping) else {}
    if semantic_status == "available":
        for component in SEMANTIC_COMPONENTS:
            record = semantic_scores.get(component)
            if not isinstance(record, Mapping):
                continue
            score = _number(record.get("score"))
            confidence = _number(record.get("confidence"))
            missing = _missing(record.get("missing_evidence", []))
            label = component.replace("_", " ").capitalize()
            if missing or (score is not None and score < 60):
                detail = f"{label} is not fully explained or evidenced."
                if missing:
                    detail += f" Missing: {'; '.join(missing)}."
                weaknesses.append(
                    _finding(
                        "explanation_completeness",
                        f"Incomplete explanation: {label}",
                        detail,
                        record.get("evidence_slides", []),
                        metric=component,
                        score=score,
                        confidence=confidence,
                        missing_evidence=missing,
                    )
                )
            elif score is None:
                ambiguous.append(
                    _finding(
                        "explanation_completeness",
                        f"Explanation not resolved: {label}",
                        f"The evaluator did not produce a usable score for {label}; treat the claim as unresolved until it is supported.",
                        record.get("evidence_slides", []),
                        metric=component,
                        confidence=confidence,
                    )
                )
            elif score < 75 or (confidence is not None and confidence < 0.75):
                ambiguous.append(
                    _finding(
                        "explanation_completeness",
                        f"Clarify: {label}",
                        f"{label} is partially supported but remains unclear or weakly evidenced in the cited slides.",
                        record.get("evidence_slides", []),
                        metric=component,
                        score=score,
                        confidence=confidence,
                    )
                )
            elif score >= 85 and not missing:
                strengths.append(
                    _finding(
                        "explanation_completeness",
                        f"Well-supported: {label}",
                        f"{label} is clearly explained with cited slide evidence and no reported missing-evidence items.",
                        record.get("evidence_slides", []),
                        metric=component,
                        score=score,
                        confidence=confidence,
                    )
                )
    else:
        ambiguous.append(
            _finding(
                "explanation_completeness",
                "Semantic completeness not assessed",
                "A problem statement and validated semantic response were not available, so conceptual completeness and ambiguity remain unresolved rather than being guessed.",
            )
        )

    return {
        "strengths": strengths,
        "weaknesses": weaknesses,
        "ambiguous_points": ambiguous,
    }


def _evaluation_fingerprint(
    ir: EvaluatorIR,
    problem: ProblemStatement | None,
    metrics: Mapping[str, Any],
    metric_details: Mapping[str, Any],
    semantic: Mapping[str, Any],
) -> str:
    semantic_identity = dict(semantic)
    semantic_identity.pop("cache_hit", None)
    return _stable_hash(
        {
            "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
            "rubric_version": RUBRIC_VERSION,
            "evaluator_ir": ir.to_dict(),
            "evaluator_ir_schema_version": ir.schema_version,
            "problem_statement": problem.to_dict() if problem else None,
            "metrics": dict(metrics),
            "metric_details": dict(metric_details),
            "weights": {
                "proposal_strength": PROPOSAL_STRENGTH_WEIGHT,
                "deck_quality": DECK_QUALITY_WEIGHT,
                "proposal_components": PROPOSAL_COMPONENT_WEIGHTS,
                "deck_components": DECK_COMPONENT_WEIGHTS,
            },
            "semantic": semantic_identity,
        }
    )


def _semantic_empty(status: str, reason: str) -> dict[str, Any]:
    scores = {
        key: _score_record(None, None, [], reason, [reason])
        for key in SEMANTIC_COMPONENTS
    }
    return {
        "schema_version": SEMANTIC_SCHEMA_VERSION,
        "model": SEMANTIC_MODEL,
        "status": status,
        "cache_hit": False,
        "scores": scores,
        "error": reason,
    }


def _semantic_schema() -> dict[str, Any]:
    component = {
        "type": "OBJECT",
        "properties": {
            "score": {"type": "NUMBER"},
            "confidence": {"type": "NUMBER"},
            "evidence_slides": {"type": "ARRAY", "items": {"type": "INTEGER"}},
            "explanation": {"type": "STRING"},
            "missing_evidence": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["score", "confidence", "evidence_slides", "explanation", "missing_evidence"],
    }
    return {
        "type": "OBJECT",
        "properties": {
            "schema_version": {"type": "STRING", "enum": [SEMANTIC_SCHEMA_VERSION]},
            "scores": {
                "type": "OBJECT",
                "properties": {key: component for key in SEMANTIC_COMPONENTS},
                "required": list(SEMANTIC_COMPONENTS),
            },
        },
        "required": ["schema_version", "scores"],
    }


def validate_semantic_payload(value: Any, slide_numbers: Sequence[int] = ()) -> tuple[bool, str]:
    """Validate the strict, score-only Gemini response contract."""
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "scores"}:
        return False, "semantic response must contain exactly schema_version and scores"
    if value.get("schema_version") != SEMANTIC_SCHEMA_VERSION:
        return False, "semantic response schema version is unsupported"
    scores = value.get("scores")
    if not isinstance(scores, Mapping) or set(scores) != set(SEMANTIC_COMPONENTS):
        return False, "semantic response scores do not match the required components"
    allowed_slides = set(slide_numbers)
    for component in SEMANTIC_COMPONENTS:
        item = scores[component]
        if not isinstance(item, Mapping) or set(item) != {"score", "confidence", "evidence_slides", "explanation", "missing_evidence"}:
            return False, f"semantic component {component} has an invalid schema"
        score = _number(item.get("score"))
        confidence = _number(item.get("confidence"))
        if score is None or not 0 <= score <= SCORE_SCALE:
            return False, f"semantic component {component} has an invalid score"
        if confidence is None or not 0 <= confidence <= 1:
            return False, f"semantic component {component} has an invalid confidence"
        evidence = item.get("evidence_slides")
        if not isinstance(evidence, list) or any(_number(slide) is None or _number(slide) < 1 for slide in evidence):
            return False, f"semantic component {component} has invalid evidence slides"
        if allowed_slides and any(int(_number(slide)) not in allowed_slides for slide in evidence):
            return False, f"semantic component {component} references an unknown slide"
        if not isinstance(item.get("explanation"), str) or not isinstance(item.get("missing_evidence"), list) or not all(isinstance(item, str) for item in item["missing_evidence"]):
            return False, f"semantic component {component} has invalid evidence text"
    return True, "ok"


class GeminiSemanticAdapter:
    """Minimal standard-library Gemini adapter for semantic score requests."""

    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = SEMANTIC_MODEL,
        endpoint: str = "https://generativelanguage.googleapis.com/v1beta/models",
    ) -> None:
        load_dotenv()
        if model != SEMANTIC_MODEL:
            raise ValueError(f"semantic evaluation only supports {SEMANTIC_MODEL}")
        self.api_key = os.environ.get("GEMINI_API_KEY") if api_key is None else api_key
        self.model = model
        self.version = model
        self.endpoint = endpoint.rstrip("/")

    def analyze(self, prompt: str, images: Sequence[VisionImage], timeout: float) -> str:
        if not self.api_key:
            raise SemanticRequestError("GEMINI_API_KEY is not configured")
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for image in images:
            if image.mime_type not in SUPPORTED_IMAGE_MIMES:
                raise SemanticRequestError(f"unsupported Gemini image MIME type: {image.mime_type}")
            parts.append(
                {
                    "inline_data": {
                        "mime_type": image.mime_type,
                        "data": base64.b64encode(image.data).decode("ascii"),
                    }
                }
            )
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": _semantic_schema(),
            },
        }
        key = urllib.parse.quote(self.api_key, safe="")
        url = f"{self.endpoint}/{urllib.parse.quote(self.model, safe='')}:generateContent?key={key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise SemanticRequestError(f"Gemini HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise SemanticRequestError(f"Gemini request failed: {exc}") from exc
        try:
            candidates = response_payload["candidates"]
            response_parts = candidates[0]["content"]["parts"]
            return next(item["text"] for item in response_parts if isinstance(item, Mapping) and isinstance(item.get("text"), str))
        except (KeyError, IndexError, TypeError, StopIteration) as exc:
            raise SemanticRequestError("Gemini response did not contain semantic JSON text") from exc


def _rendered_images(ir: EvaluatorIR, render_dir: str | Path | None) -> list[VisionImage]:
    if render_dir is None:
        return []
    root = Path(render_dir).expanduser().resolve()
    if not root.is_dir():
        return []
    images: list[VisionImage] = []
    for index, slide in enumerate(_slides(ir), 1):
        number = _slide_number(slide, index)
        references: list[Path] = []
        reference = slide.get("rendered_slide_image")
        if isinstance(reference, Mapping) and reference.get("path"):
            path = Path(str(reference["path"]))
            references.append(path if path.is_absolute() else root / path)
        for stem in (f"slide-{number:02d}", f"slide-{number}", str(number)):
            for suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif"):
                references.append(root / f"{stem}{suffix}")
        selected = next(
            (
                path
                for path in references
                if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
            ),
            None,
        )
        if selected is None:
            continue
        try:
            data = selected.read_bytes()
        except OSError:
            continue
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".heic": "image/heic",
            ".heif": "image/heif",
        }.get(selected.suffix.lower())
        if mime:
            images.append(VisionImage(f"slide:{number}", mime, data))
    return images


def _semantic_prompt(ir: EvaluatorIR, problem: ProblemStatement | None) -> str:
    slides = []
    for slide in _slides(ir):
        slides.append(
            {
                "slide_number": slide.get("slide_number"),
                "title": slide.get("title"),
                "visible_text": slide.get("visible_text", ""),
                "visual_evidence": [
                    {
                        "role": item.get("role"),
                        "ocr_text": item.get("ocr_text"),
                        "content_status": item.get("content_status"),
                    }
                    for item in slide.get("visual_evidence", [])
                    if isinstance(item, Mapping) and item.get("scoring_relevant") is not False
                ],
                "tables": slide.get("tables", []),
                "links": slide.get("links", []),
            }
        )
    context = {"problem_statement": problem.to_dict() if problem else None, "slides": slides}
    return (
        "Evaluate the proposal using only the supplied presentation evidence. "
        "Return JSON matching the requested schema exactly. Score each component from 0 to 100. "
        "Set confidence as a required decimal from 0.0 to 1.0, never as a percentage from 0 to 100. "
        "Always provide a numeric confidence even when evidence is weak or missing. "
        "Cite only slide numbers that support the component. If evidence is absent, list it in "
        "missing_evidence and do not infer it from presentation polish. Do not return nodes, edges, "
        "rankings, or any fields outside the schema.\n\n"
        + json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _cache_root(cache_dir: str | Path | None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).expanduser().resolve()
    return Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "pptx-forensics" / "evaluation"


def _semantic_cache_key(prompt: str, images: Sequence[VisionImage]) -> str:
    digest = hashlib.sha256()
    digest.update(SEMANTIC_SCHEMA_VERSION.encode("utf-8"))
    digest.update(SEMANTIC_MODEL.encode("utf-8"))
    digest.update(prompt.encode("utf-8"))
    for image in images:
        digest.update(image.label.encode("utf-8"))
        digest.update(image.mime_type.encode("utf-8"))
        digest.update(image.data)
    return digest.hexdigest()


def _read_semantic_cache(path: Path, content_hash: str, slide_numbers: Sequence[int]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, Mapping) or value.get("content_hash") != content_hash:
        return None
    response = value.get("response")
    valid, _ = validate_semantic_payload(response, slide_numbers)
    return dict(response) if valid else None


def _write_semantic_cache(path: Path, content_hash: str, response: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"content_hash": content_hash, "response": response}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def evaluate_semantics(
    ir: EvaluatorIR,
    problem: ProblemStatement,
    *,
    render_dir: str | Path | None = None,
    adapter: SemanticAdapter | None = None,
    api_key: str | None = None,
    cache_dir: str | Path | None = None,
    timeout: float = 30.0,
    skip: bool = False,
    fresh: bool = False,
) -> dict[str, Any]:
    """Run optional Gemini semantic scoring with strict validation and caching."""
    if skip:
        return _semantic_empty("not_requested", "semantic evaluation was disabled")
    images = _rendered_images(ir, render_dir)
    prompt = _semantic_prompt(ir, problem)
    content_hash = _semantic_cache_key(prompt, images)
    cache_path = _cache_root(cache_dir) / f"{content_hash}.json"
    cached = None
    if not fresh:
        cached = _read_semantic_cache(
            cache_path,
            content_hash,
            [_slide_number(slide, index) for index, slide in enumerate(_slides(ir), 1)],
        )
    if cached is not None:
        return {
            "schema_version": SEMANTIC_SCHEMA_VERSION,
            "model": SEMANTIC_MODEL,
            "status": "available",
            "cache_hit": True,
            "content_hash": content_hash,
            "scores": {
                key: _score_record(
                    cached["scores"][key]["score"],
                    cached["scores"][key]["confidence"],
                    cached["scores"][key]["evidence_slides"],
                    cached["scores"][key]["explanation"],
                    cached["scores"][key]["missing_evidence"],
                )
                for key in SEMANTIC_COMPONENTS
            },
        }

    semantic_adapter = adapter or GeminiSemanticAdapter(api_key=api_key)
    if isinstance(semantic_adapter, GeminiSemanticAdapter) and not semantic_adapter.api_key:
        result = _semantic_empty("unavailable", "GEMINI_API_KEY is not configured")
        result["content_hash"] = content_hash
        return result
    try:
        raw = semantic_adapter.analyze(prompt, images, timeout)
        response = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        result = _semantic_empty("failed", str(exc))
        result["content_hash"] = content_hash
        return result
    valid, error = validate_semantic_payload(
        response,
        [_slide_number(slide, index) for index, slide in enumerate(_slides(ir), 1)],
    )
    if not valid:
        result = _semantic_empty("failed", error)
        result["content_hash"] = content_hash
        return result
    _write_semantic_cache(cache_path, content_hash, response)
    return {
        "schema_version": SEMANTIC_SCHEMA_VERSION,
        "model": SEMANTIC_MODEL,
        "status": "available",
        "cache_hit": False,
        "content_hash": content_hash,
        "scores": {
            key: _score_record(
                response["scores"][key]["score"],
                response["scores"][key]["confidence"],
                response["scores"][key]["evidence_slides"],
                response["scores"][key]["explanation"],
                response["scores"][key]["missing_evidence"],
            )
            for key in SEMANTIC_COMPONENTS
        },
    }


def evaluate_deck(
    value: EvaluatorIR | DeckIR | Mapping[str, Any],
    problem: ProblemStatement | Mapping[str, Any] | str | Path | None = None,
    *,
    deck_only: bool = False,
    render_dir: str | Path | None = None,
    semantic_adapter: SemanticAdapter | None = None,
    api_key: str | None = None,
    semantic_cache_dir: str | Path | None = None,
    semantic_timeout: float = 30.0,
    skip_semantic: bool = False,
    fresh_semantic: bool = False,
) -> dict[str, Any]:
    """Evaluate a deck from canonical DeckIR or compact EvaluatorIR data."""
    if isinstance(value, EvaluatorIR):
        ir = value
    elif isinstance(value, Mapping) and value.get("schema_version") == EVALUATOR_SCHEMA_VERSION and {"deck", "slides"} <= set(value):
        ir = EvaluatorIR(
            deck=dict(value.get("deck", {})) if isinstance(value.get("deck"), Mapping) else {},
            slides=[dict(item) for item in value.get("slides", []) if isinstance(item, Mapping)],
            schema_version=str(value["schema_version"]),
        )
    else:
        ir = build_evaluator_ir(value)
    metrics, metric_details = compute_deterministic_metrics(ir)
    deterministic = _deterministic_scores(metrics, metric_details)

    problem_statement: ProblemStatement | None = None
    if not deck_only:
        if problem is None:
            raise ProblemStatementError("problem statement is required unless --deck-only is used")
        problem_statement = load_problem_statement(problem) if isinstance(problem, (str, Path)) else validate_problem_statement(problem)
    elif problem is not None:
        problem_statement = load_problem_statement(problem) if isinstance(problem, (str, Path)) else validate_problem_statement(problem)

    if deck_only:
        semantic = _semantic_empty("not_requested", "deck-only evaluation does not run semantic proposal scoring")
    elif problem_statement is not None:
        semantic = evaluate_semantics(
            ir,
            problem_statement,
            render_dir=render_dir,
            adapter=semantic_adapter,
            api_key=api_key,
            cache_dir=semantic_cache_dir,
            timeout=semantic_timeout,
            skip=skip_semantic,
            fresh=fresh_semantic,
        )
    else:
        semantic = _semantic_empty("unavailable", "problem statement is unavailable")

    proposal_components = {
        key: dict(semantic["scores"].get(key, _score_record(None, None, [], "Semantic score unavailable.", [key])))
        for key in SEMANTIC_COMPONENTS
    }
    proposal_components["prototype_evidence"] = deterministic["prototype_evidence"]
    proposal = _weighted_group(
        "proposal_strength",
        proposal_components,
        PROPOSAL_COMPONENT_WEIGHTS,
        "Proposal strength combines problem alignment, solution clarity, feasibility, innovation, impact, and concrete prototype evidence.",
    )
    deck_quality = _weighted_group(
        "deck_quality",
        {key: deterministic[key] for key in DECK_COMPONENT_WEIGHTS},
        DECK_COMPONENT_WEIGHTS,
        "Deck quality combines readability, layout consistency, visual hierarchy, evidence visibility, content originality, pointer-friendly content structure, visual coverage, and space usage.",
    )
    final_missing = []
    if proposal["score"] is None:
        final_missing.append("proposal_strength score is unavailable")
    if deck_quality["score"] is None:
        final_missing.append("deck_quality score is unavailable")
    if final_missing:
        final_explanation = "Final score is unavailable while a weighted score group lacks evidence."
        final_score = None
        final_confidence = None
    else:
        final_explanation = "Final score = 0.70 * proposal_strength + 0.30 * deck_quality."
        final_score = PROPOSAL_STRENGTH_WEIGHT * proposal["score"] + DECK_QUALITY_WEIGHT * deck_quality["score"]
        final_confidence = PROPOSAL_STRENGTH_WEIGHT * proposal["confidence"] + DECK_QUALITY_WEIGHT * deck_quality["confidence"]
    final = _score_record(
        final_score,
        final_confidence,
        [*proposal.get("evidence_slides", []), *deck_quality.get("evidence_slides", [])],
        final_explanation,
        [*final_missing, *proposal.get("missing_evidence", []), *deck_quality.get("missing_evidence", [])],
    )
    findings = _build_findings(metrics, metric_details, deterministic, semantic)
    evaluation_fingerprint = _evaluation_fingerprint(ir, problem_statement, metrics, metric_details, semantic)
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "evaluation_fingerprint": evaluation_fingerprint,
        "score_scale": SCORE_SCALE,
        "weights": {
            "proposal_strength": PROPOSAL_STRENGTH_WEIGHT,
            "deck_quality": DECK_QUALITY_WEIGHT,
        },
        "evaluator_ir_schema_version": ir.schema_version,
        "deck": dict(ir.deck),
        "problem_statement": problem_statement.to_dict() if problem_statement else None,
        "metrics": metrics,
        "metric_details": metric_details,
        "semantic": semantic,
        "findings": findings,
        "scores": {
            "proposal_strength": proposal,
            "deck_quality": deck_quality,
            "final": final,
        },
        "final_score": final["score"],
    }


def evaluate_deck_file(
    deck_ir_path: str | Path,
    problem_path: str | Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Evaluate canonical DeckIR JSON from disk."""
    deck = load_deck_ir(deck_ir_path)
    problem = load_problem_statement(problem_path) if problem_path is not None else None
    return evaluate_deck(deck, problem, **kwargs)
