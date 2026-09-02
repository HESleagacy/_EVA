"""Compact scoring projection built from the authoritative DeckIR.

DeckIR is intentionally rich because it is an audit record.  EvaluatorIR is
the opposite: it contains only the visible, scoring-relevant facts and small
references back to the evidence bundle.  In particular, it never exposes the
relationship graph, presenter notes, object styles, OCR word arrays, or raster
diagram graphs to deterministic scoring.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
import math
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit


EVALUATOR_SCHEMA_VERSION = "1.0"
LOW_OCR_CONFIDENCE = 0.65
NOISE_ROLES = {"logo", "decorative", "decorative_image", "template"}
ROLE_NAMES = {
    "diagram": "diagram_candidate",
    "screenshot": "screenshot",
    "chart": "chart",
    "evidence_image": "evidence_image",
    "logo": "logo",
    "decorative": "decorative",
    "decorative_image": "decorative",
    "template": "decorative",
    "unknown": "unknown",
}


@dataclass(frozen=True)
class EvaluatorIR:
    """The small, scoring-facing interchange representation."""

    deck: dict[str, Any]
    slides: list[dict[str, Any]]
    schema_version: str = EVALUATOR_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "deck": self.deck,
            "slides": self.slides,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _canonical(value: Any) -> Any:
    if getattr(value, "canonical", None) is not None:
        return value.canonical
    return value


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _round(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 6)


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in result) or result[2] < 0 or result[3] < 0:
        return None
    return result  # type: ignore[return-value]


def _bbox_list(value: tuple[float, float, float, float] | None) -> list[float] | None:
    return [round(item, 12) for item in value] if value is not None else None


def _right(value: tuple[float, float, float, float]) -> float:
    return value[0] + value[2]


def _bottom(value: tuple[float, float, float, float]) -> float:
    return value[1] + value[3]


def _union_bbox(boxes: Iterable[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    values = list(boxes)
    if not values:
        return None
    left = min(item[0] for item in values)
    top = min(item[1] for item in values)
    right = max(_right(item) for item in values)
    bottom = max(_bottom(item) for item in values)
    return left, top, right - left, bottom - top


def _area(value: tuple[float, float, float, float] | None) -> float:
    return max(0.0, value[2]) * max(0.0, value[3]) if value else 0.0


def _clip_box(value: tuple[float, float, float, float]) -> tuple[float, float, float, float] | None:
    left = max(0.0, value[0])
    top = max(0.0, value[1])
    right = min(1.0, _right(value))
    bottom = min(1.0, _bottom(value))
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def _union_area(boxes: Iterable[tuple[float, float, float, float]]) -> float:
    """Return the clipped union area for normalized slide boxes."""
    clipped = [item for box in boxes if (item := _clip_box(box)) is not None]
    if not clipped:
        return 0.0
    x_values = sorted({value for box in clipped for value in (box[0], _right(box))})
    area = 0.0
    for left, right in zip(x_values, x_values[1:]):
        if right <= left:
            continue
        intervals = sorted(
            (box[1], _bottom(box))
            for box in clipped
            if box[0] <= left < _right(box)
        )
        covered = 0.0
        current_start = current_end = None
        for start, end in intervals:
            if current_start is None:
                current_start, current_end = start, end
            elif start > current_end:
                covered += current_end - current_start
                current_start, current_end = start, end
            else:
                current_end = max(current_end, end)
        if current_start is not None:
            covered += current_end - current_start
        area += (right - left) * covered
    return max(0.0, min(1.0, area))


def _facts(deck: Any, slide_id: str, fact_type: str | None = None) -> list[dict[str, Any]]:
    records = []
    for record in _get(deck, "rendered_evidence", []) or []:
        if not isinstance(record, Mapping) or record.get("slide_id") != slide_id:
            continue
        value = record.get("value")
        if not isinstance(value, Mapping):
            continue
        if fact_type is None or value.get("type") == fact_type:
            records.append(dict(record))
    return records


def _fact_value(deck: Any, slide_id: str, fact_type: str) -> dict[str, Any]:
    record = next(iter(_facts(deck, slide_id, fact_type)), None)
    value = record.get("value") if isinstance(record, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def _objects(deck: Any, slide_id: str) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in (_get(deck, "objects", []) or [])
        if isinstance(item, Mapping) and item.get("slide_id") == slide_id
    ]


def _excluded_object_ids(deck: Any, slide_id: str) -> set[str]:
    excluded: set[str] = set()
    objects = {
        str(item.get("id")): item
        for item in (_get(deck, "objects", []) or [])
        if isinstance(item, Mapping) and item.get("id")
    }
    occupancy = _fact_value(deck, slide_id, "slide_occupancy")
    for item in occupancy.get("excluded_objects", []):
        if isinstance(item, Mapping) and item.get("id"):
            object_item = objects.get(str(item["id"]), {})
            reasons = set(item.get("reasons", []))
            # A large visible text block can contain a brand term without being
            # slide chrome. Badge-only exclusions are
            # safe for images, but not for native text.
            if object_item.get("type") == "text" and reasons <= {"badge"}:
                continue
            excluded.add(str(item["id"]))
    exclusions = _fact_value(deck, slide_id, "visual_exclusions")
    for item_id in exclusions.get("objects", []):
        object_item = objects.get(str(item_id), {})
        exclusion = next(
            (
                item
                for item in exclusions.get("excluded_objects", [])
                if isinstance(item, Mapping) and str(item.get("id")) == str(item_id)
            ),
            {},
        )
        if object_item.get("type") == "text" and set(exclusion.get("reasons", [])) <= {"badge"}:
            continue
        if item_id:
            excluded.add(str(item_id))
    return excluded


def _text_blocks(objects: list[dict[str, Any]], excluded: set[str]) -> list[dict[str, Any]]:
    blocks = []
    for item in objects:
        if item.get("id") in excluded or item.get("type") != "text":
            continue
        text = str(item.get("text") or "").strip()
        box = _bbox(item.get("bbox"))
        if not text or box is None or _area(box) <= 0:
            continue
        blocks.append(
            {
                "text": text,
                "bbox": _bbox_list(box),
                "object_id": item.get("id"),
            }
        )
    return sorted(
        blocks,
        key=lambda item: (
            (item["bbox"][1], item["bbox"][0]) if item.get("bbox") else (0.0, 0.0),
            str(item.get("object_id", "")),
        ),
    )


def _font_sizes(item: Mapping[str, Any]) -> list[float]:
    resolved = item.get("resolved_style") if isinstance(item.get("resolved_style"), Mapping) else {}
    values = resolved.get("run_font_sizes_pt")
    if not isinstance(values, list):
        values = [resolved.get("font_size_pt")]
    result = []
    for value in values:
        number = _round(value)
        if number is not None and number > 0:
            result.append(number)
    return result


def _font_family(item: Mapping[str, Any]) -> str | None:
    resolved = item.get("resolved_style") if isinstance(item.get("resolved_style"), Mapping) else {}
    value = resolved.get("font_family")
    return str(value) if value else None


def _font_aggregates(objects: list[dict[str, Any]], excluded: set[str]) -> dict[str, Any]:
    sizes: list[float] = []
    families: Counter[str] = Counter()
    for item in objects:
        if item.get("id") in excluded or item.get("type") != "text" or not item.get("text"):
            continue
        item_sizes = _font_sizes(item)
        sizes.extend(item_sizes)
        family = _font_family(item)
        if family:
            families[family] += max(1, len(str(item.get("text") or "")))
    dominant_size = None
    if sizes:
        counts = Counter(sizes)
        dominant_size = min((value for value, count in counts.items() if count == max(counts.values())), default=None)
    family_counts = {key: families[key] for key in sorted(families)}
    return {
        "font_size": {
            "dominant_pt": dominant_size,
            "minimum_pt": min(sizes) if sizes else None,
            "maximum_pt": max(sizes) if sizes else None,
            "unique_count": len(set(sizes)),
        },
        "font_family": {
            "dominant": families.most_common(1)[0][0] if families else None,
            "counts": family_counts,
            "unique_count": len(families),
        },
    }


def _title(deck: Any, slide_id: str, objects: list[dict[str, Any]]) -> dict[str, Any] | None:
    record = next(iter(_facts(deck, slide_id, "slide_title_candidate")), None)
    value = record.get("value") if isinstance(record, Mapping) else None
    if not isinstance(value, Mapping) or not value.get("selected_object_id"):
        return None
    object_id = str(value["selected_object_id"])
    item = next((item for item in objects if item.get("id") == object_id), None)
    if item is None:
        return None
    box = _bbox(item.get("bbox"))
    confidence = record.get("confidence") if isinstance(record, Mapping) else None
    result = {
        "object_id": object_id,
        "text": str(value.get("selected_text") or item.get("text") or ""),
        "bbox": _bbox_list(box),
        "content_status": "extracted",
        "classification_confidence": _round(confidence),
    }
    return result


def _role_records(deck: Any, slide_id: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_object: dict[str, dict[str, Any]] = {}
    by_asset: dict[str, dict[str, Any]] = {}
    for record in _facts(deck, slide_id, "image_role_candidate"):
        value = record.get("value", {})
        if not isinstance(value, Mapping):
            continue
        item = dict(value)
        if record.get("confidence") is not None and item.get("confidence") is None:
            item["confidence"] = record.get("confidence")
        object_id = item.get("object") or record.get("object_id")
        asset_id = item.get("asset_id")
        if object_id:
            by_object[str(object_id)] = item
        if asset_id:
            by_asset.setdefault(str(asset_id), item)
    return by_object, by_asset


def _image_details(deck: Any, slide_id: str, asset_id: str) -> dict[str, Any]:
    for record in _facts(deck, slide_id, "image_asset_analysis"):
        value = record.get("value", {})
        if isinstance(value, Mapping) and value.get("asset_id") == asset_id:
            return dict(value)
    return {}


def _ocr_by_asset(deck: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in _get(deck, "ocr_evidence", []) or []:
        if not isinstance(record, Mapping):
            continue
        value = record.get("value")
        if isinstance(value, Mapping) and value.get("asset_id"):
            result[str(value["asset_id"])] = dict(record)
    return result


def _candidate_by_asset(deck: Any, slide_id: str, asset_id: str) -> dict[str, Any] | None:
    for record in _facts(deck, slide_id, "diagram_candidate"):
        value = record.get("value")
        if isinstance(value, Mapping) and value.get("asset_id") == asset_id:
            return dict(record)
    return None


def _asset_role(value: Any) -> str:
    return ROLE_NAMES.get(str(value or "unknown"), "unknown")


def _meaningful(role: str, box: tuple[float, float, float, float] | None) -> bool:
    if role in NOISE_ROLES:
        return False
    if role != "unknown":
        return True
    if box is None:
        return False
    return _area(box) >= 0.04 or not (box[0] <= 0.08 or box[1] <= 0.08 or _right(box) >= 0.92 or _bottom(box) >= 0.92)


def _image_records(
    deck: Any,
    slide_id: str,
    objects: list[dict[str, Any]],
    excluded: set[str],
    ocr_by_asset: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_object_role, by_asset_role = _role_records(deck, slide_id)
    assets = {
        str(item.get("id")): dict(item)
        for item in (_get(deck, "assets", []) or [])
        if isinstance(item, Mapping) and item.get("id")
    }
    grouped: dict[str, list[tuple[dict[str, Any], tuple[float, float, float, float]]]] = defaultdict(list)
    for item in objects:
        if item.get("id") in excluded or item.get("type") != "image" or not item.get("asset_id"):
            continue
        box = _bbox(item.get("bbox"))
        if box is not None and _area(box) > 0:
            grouped[str(item["asset_id"])].append((item, box))

    records: list[dict[str, Any]] = []
    for asset_id in sorted(grouped):
        instances = grouped[asset_id]
        boxes = [box for _, box in instances]
        object_ids = [str(item["id"]) for item, _ in instances]
        role_item = by_object_role.get(object_ids[0], by_asset_role.get(asset_id, {}))
        raw_role = str(role_item.get("image_role") or role_item.get("role") or "unknown")
        role = _asset_role(raw_role)
        image_box = _union_bbox(boxes)
        asset = assets.get(asset_id, {})
        details = _image_details(deck, slide_id, asset_id)
        ocr_record = ocr_by_asset.get(asset_id)
        ocr_value = ocr_record.get("value", {}) if isinstance(ocr_record, Mapping) else {}
        if not isinstance(ocr_value, Mapping):
            ocr_value = {}
        ocr_confidence = _round(ocr_record.get("confidence")) if isinstance(ocr_record, Mapping) else None
        ocr_status = ocr_record.get("status") if isinstance(ocr_record, Mapping) else None
        meaningful = _meaningful(role, image_box)
        useful_ocr = (
            meaningful
            and isinstance(ocr_value.get("text"), str)
            and bool(ocr_value.get("text", "").strip())
            and ocr_status == "verified"
            and ocr_confidence is not None
            and ocr_confidence >= LOW_OCR_CONFIDENCE
        )
        candidate = _candidate_by_asset(deck, slide_id, asset_id)
        needs_review = role == "diagram_candidate" or bool(candidate)
        if _meaningful(role, image_box) and _area(image_box) >= 0.25:
            needs_review = True
        content_status = "extracted" if meaningful and useful_ocr else "low_confidence"
        if not meaningful:
            content_status = "excluded_noise"
        image = {
            "asset_id": asset_id,
            "role": role,
            "bbox": _bbox_list(image_box),
            "dimensions": details.get("image_size"),
            "content_type": asset.get("content_type"),
            "sha256": asset.get("sha256"),
            "ocr_text": str(ocr_value.get("text")) if useful_ocr else None,
            "ocr_confidence": ocr_confidence,
            "classification_confidence": _round(role_item.get("confidence")),
            "interpretation_confidence": None,
            "content_status": content_status,
            "needs_vision_review": needs_review,
            "scoring_relevant": meaningful,
            "raw_evidence_ref": {
                "object_ids": object_ids,
                "asset_id": asset_id,
                "ocr_id": ocr_record.get("id") if isinstance(ocr_record, Mapping) else None,
                "diagram_candidate_id": candidate.get("id") if candidate else None,
            },
        }
        if not meaningful:
            continue
        records.append(image)
    return records


def _external_target(link: Any) -> str | None:
    if not isinstance(link, Mapping):
        return None
    target = link.get("resolved_target") or link.get("target")
    if not isinstance(target, str) or not target:
        return None
    parsed = urlsplit(target)
    if parsed.scheme.lower() not in {"http", "https", "mailto", "ftp"} and not target.startswith("//"):
        return None
    return target


def _slide_links(slide: Mapping[str, Any]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str | None], dict[str, Any]] = {}
    for link in slide.get("hyperlinks", []) or []:
        target = _external_target(link)
        if target is None:
            continue
        kind = str(link.get("kind") or "external") if isinstance(link, Mapping) else "external"
        action = link.get("action") if isinstance(link, Mapping) and link.get("action") else None
        unique.setdefault((target, action), {"target": target, "kind": kind, **({"action": action} if action else {})})
    return [unique[key] for key in sorted(unique)]


def _global_links(slides: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str | None], dict[str, Any]] = {}
    slide_numbers: dict[tuple[str, str | None], list[int]] = defaultdict(list)
    for slide in slides:
        for link in _slide_links(slide):
            key = (str(link["target"]), link.get("action"))
            unique.setdefault(key, {key_name: link[key_name] for key_name in ("target", "kind", "action") if key_name in link})
            if slide.get("number") is not None:
                slide_numbers[key].append(int(slide["number"]))
    result = []
    for key in sorted(unique):
        item = dict(unique[key])
        item["slides"] = sorted(set(slide_numbers[key]))
        result.append(item)
    return result


def _slide_type(text: str, images: list[Mapping[str, Any]]) -> str:
    value = text.casefold()
    for name, terms in (
        ("architecture", ("architecture", "system design", "component")),
        ("process", ("workflow", "process", "pipeline", "steps")),
        ("results", ("result", "output", "evaluation", "performance")),
        ("comparison", ("comparison", "versus", " vs ")),
    ):
        if any(term in value for term in terms):
            return name
    if any(item.get("role") == "diagram_candidate" for item in images):
        return "architecture"
    return "content"


def _slide_metrics(
    deck: Any,
    slide_id: str,
    objects: list[dict[str, Any]],
    excluded: set[str],
    images: list[Mapping[str, Any]],
) -> dict[str, Any]:
    occupancy = _fact_value(deck, slide_id, "slide_occupancy")
    density = _fact_value(deck, slide_id, "text_density")
    whitespace = _fact_value(deck, slide_id, "whitespace_balance")
    largest_empty = _fact_value(deck, slide_id, "largest_empty_region")
    overlaps = _facts(deck, slide_id, "object_overlap")
    text_density = _round(density.get("text_union_area_ratio"))
    if text_density is None:
        text_density = _round(occupancy.get("occupied_area_ratio")) or 0.0
    occupied_area_ratio = _round(occupancy.get("occupied_area_ratio"))
    if occupied_area_ratio is None:
        occupied_area_ratio = _round(
            _union_area(
                box
                for item in objects
                if item.get("id") not in excluded and (box := _bbox(item.get("bbox"))) is not None
            )
        )
    whitespace_area_ratio = _round(whitespace.get("whitespace_area_ratio"))
    if whitespace_area_ratio is None and occupied_area_ratio is not None:
        whitespace_area_ratio = round(max(0.0, min(1.0, 1.0 - occupied_area_ratio)), 6)
    largest_empty_region_ratio = _round(largest_empty.get("area_ratio"))
    whitespace_balance = _round(whitespace.get("balance"))
    overlap_area = sum(
        float(record.get("value", {}).get("intersection_area", 0.0))
        for record in overlaps
        if isinstance(record.get("value"), Mapping)
    )
    occupied = max(float(occupied_area_ratio or 0.0), 1e-9)
    overlap_score = min(1.0, overlap_area / occupied)
    sizes = _font_aggregates(objects, excluded)
    text_items = [item for item in _text_blocks(objects, excluded)]
    characters = sum(len(item["text"]) for item in text_items)
    small_text_ratio = 0.0
    sized_items = [item for item in objects if item.get("id") not in excluded and item.get("type") == "text" and item.get("text")]
    if sized_items:
        small_text_ratio = sum(any(size < 10 for size in _font_sizes(item)) for item in sized_items) / len(sized_items)
    clipping = len(_facts(deck, slide_id, "clipping_overflow"))
    clipping_rate = clipping / max(1, len(objects) - len(excluded))
    visual_boxes = [
        box
        for image in images
        if (box := _bbox(image.get("bbox"))) is not None
    ]
    visual_boxes.extend(
        box
        for item in objects
        if item.get("id") not in excluded
        and item.get("type") in {"chart", "smartart", "table"}
        and (box := _bbox(item.get("bbox"))) is not None
    )
    visual_area_ratio = _round(_union_area(visual_boxes)) or 0.0
    readability = 10.0
    readability -= min(4.0, text_density * 4.0)
    readability -= min(3.0, overlap_score * 6.0)
    readability -= min(1.5, small_text_ratio * 1.5)
    readability -= min(1.0, clipping * 0.25)
    return {
        "text_density": round(max(0.0, min(1.0, text_density)), 6),
        "occupied_area_ratio": occupied_area_ratio,
        "whitespace_area_ratio": whitespace_area_ratio,
        "largest_empty_region_ratio": largest_empty_region_ratio,
        "whitespace_balance": whitespace_balance,
        "visual_area_ratio": visual_area_ratio,
        "visual_object_count": len(visual_boxes),
        "overlap_score": round(max(0.0, min(1.0, overlap_score)), 6),
        "overlap_ratio": round(max(0.0, min(1.0, overlap_score)), 6),
        "small_text_ratio": round(max(0.0, min(1.0, small_text_ratio)), 6),
        "clipping_count": clipping,
        "clipping_rate": round(max(0.0, min(1.0, clipping_rate)), 6),
        "readability_score": round(max(0.0, min(10.0, readability)), 2),
        "text_object_count": len(text_items),
        "character_count": characters,
        "image_count": len(images),
        "meaningful_image_count": sum(bool(item.get("scoring_relevant")) for item in images),
        **sizes,
    }


def _rendered_reference(deck: Any, slide_id: str) -> dict[str, Any] | None:
    for record in _facts(deck, slide_id, "rendered_slide"):
        value = record.get("value", {})
        if not isinstance(value, Mapping):
            continue
        return {
            "evidence_ref": record.get("id"),
            "path": value.get("path"),
            "format": value.get("format"),
            "status": record.get("status"),
        }
    return None


def _tables(objects: list[dict[str, Any]], excluded: set[str]) -> list[dict[str, Any]]:
    result = []
    for item in objects:
        if item.get("id") in excluded or item.get("type") != "table":
            continue
        data = item.get("table_data") if isinstance(item.get("table_data"), Mapping) else {}
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        result.append(
            {
                "object_id": item.get("id"),
                "bbox": _bbox(item.get("bbox")),
                "rows": rows,
                "raw_evidence_ref": {"object_id": item.get("id")},
            }
        )
    for item in result:
        item["bbox"] = _bbox_list(item["bbox"])
    return result


def build_evaluator_ir(value: Any) -> EvaluatorIR:
    """Build the scoring projection without exposing DeckIR internals."""
    report = value if getattr(value, "canonical", None) is not None else None
    deck = _canonical(value)
    if deck is None:
        return EvaluatorIR(deck={"slide_count": 0, "links": [], "media": [], "global_metrics": {}}, slides=[])

    slides = [item for item in (_get(deck, "slides", []) or []) if isinstance(item, Mapping)]
    ocr_by_asset = _ocr_by_asset(deck)
    evaluator_slides: list[dict[str, Any]] = []
    for slide in sorted(slides, key=lambda item: (item.get("number", 0), str(item.get("id", "")))):
        slide_id = str(slide.get("id") or "")
        objects = _objects(deck, slide_id)
        excluded = _excluded_object_ids(deck, slide_id)
        text_blocks = _text_blocks(objects, excluded)
        images = _image_records(deck, slide_id, objects, excluded, ocr_by_asset)
        visible_text = "\n".join(item["text"] for item in text_blocks)
        slide_links = _slide_links(slide)
        slide_size = _get(deck, "deck", {}).get("slide_size_emu", [None, None])
        if not isinstance(slide_size, (list, tuple)):
            slide_size = [None, None]
        dimensions = {
            "width": slide_size[0] if len(slide_size) > 0 else None,
            "height": slide_size[1] if len(slide_size) > 1 else None,
            "unit": "emu",
        }
        raw_evidence_ref = {
            "slide_id": slide_id,
            "object_ids": [str(item.get("id")) for item in objects if item.get("id")],
        }
        if report is not None and getattr(report, "evidence_dir", None):
            raw_evidence_ref["evidence_dir"] = report.evidence_dir
        evaluator_slides.append(
            {
                "slide_number": slide.get("number"),
                "dimensions": dimensions,
                "rendered_slide_image": _rendered_reference(deck, slide_id),
                "visible_text": visible_text,
                "text_blocks": text_blocks,
                "title": _title(deck, slide_id, objects),
                "slide_type": _slide_type(visible_text, images),
                "evidence_visibility": dict(slide.get("visual_evidence_visibility", {}))
                if isinstance(slide.get("visual_evidence_visibility"), Mapping)
                else {},
                "metrics": _slide_metrics(deck, slide_id, objects, excluded, images),
                "visual_evidence": images,
                "tables": _tables(objects, excluded),
                "links": slide_links,
                "raw_evidence_ref": raw_evidence_ref,
            }
        )

    referenced_assets = {
        str(item.get("asset_id"))
        for slide in evaluator_slides
        for item in slide.get("visual_evidence", [])
        if item.get("asset_id")
    }
    media: list[dict[str, Any]] = []
    seen_media: set[str] = set()
    for asset in (_get(deck, "assets", []) or []):
        if (
            not isinstance(asset, Mapping)
            or asset.get("type") != "media"
            or not asset.get("id")
            or str(asset.get("id")) not in referenced_assets
        ):
            continue
        asset_id = str(asset["id"])
        if asset_id in seen_media:
            continue
        seen_media.add(asset_id)
        detail = next(
            (
                record.get("value", {})
                for record in (_get(deck, "rendered_evidence", []) or [])
                if isinstance(record, Mapping)
                and isinstance(record.get("value"), Mapping)
                and record["value"].get("type") == "image_asset_analysis"
                and record["value"].get("asset_id") == asset_id
            ),
            {},
        )
        media.append(
            {
                "asset_id": asset_id,
                "name": PurePosixPath(str(asset.get("part") or "")).name,
                "content_type": asset.get("content_type"),
                "dimensions": detail.get("image_size") if isinstance(detail, Mapping) else None,
                "sha256": asset.get("sha256"),
                "raw_evidence_ref": {"asset_id": asset_id},
            }
        )

    deck_meta = _get(deck, "deck", {}) or {}
    global_links = _global_links(slides)
    global_metrics = {
        "text_object_count": sum(slide["metrics"]["text_object_count"] for slide in evaluator_slides),
        "character_count": sum(slide["metrics"]["character_count"] for slide in evaluator_slides),
        "image_count": sum(slide["metrics"]["image_count"] for slide in evaluator_slides),
        "meaningful_image_count": sum(slide["metrics"]["meaningful_image_count"] for slide in evaluator_slides),
        "external_link_count": len(global_links),
        "media_count": len(media),
    }
    deck_size = deck_meta.get("slide_size_emu", [None, None])
    if not isinstance(deck_size, (list, tuple)):
        deck_size = [None, None]
    evaluator_deck = {
        "slide_count": len(evaluator_slides),
        "dimensions": {
            "width": deck_size[0] if len(deck_size) > 0 else None,
            "height": deck_size[1] if len(deck_size) > 1 else None,
            "unit": "emu",
        },
        "links": global_links,
        "media": media,
        "global_metrics": global_metrics,
    }
    if report is not None and getattr(report, "evidence_dir", None):
        evaluator_deck["raw_evidence_ref"] = {"evidence_dir": report.evidence_dir}
    return EvaluatorIR(
        deck=evaluator_deck,
        slides=evaluator_slides,
    )


def evaluator_dict(value: Any) -> dict[str, Any]:
    """Return the compact evaluator payload for a report or DeckIR."""
    if isinstance(value, EvaluatorIR):
        return value.to_dict()
    return build_evaluator_ir(value).to_dict()


def evaluator_json(value: Any) -> str:
    if isinstance(value, EvaluatorIR):
        return value.to_json()
    return build_evaluator_ir(value).to_json()
