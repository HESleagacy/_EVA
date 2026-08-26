"""Lightweight, deterministic visual evidence derived from native geometry.

The native OOXML object boxes are the authority for this module.  SVG is only
used by :func:`rendered_geometry_evidence` when a caller explicitly asks for a
rendered slide, and image facts inspect the original package asset directly.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import math
import re
from statistics import median
from typing import Any, Iterable, Mapping

from defusedxml import ElementTree as SafeET

from .models import DeckIR


VISUAL_SCHEMA_VERSION = "visual-geometry-v2"
ALIGNMENT_TOLERANCE = 0.005
ALIGNMENT_MISMATCH_TOLERANCE = 0.05
GEOMETRY_MISMATCH_TOLERANCE = 0.005
MAX_WHITESPACE_REGIONS = 64

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_SVG_NUMBER = re.compile(_NUMBER)
_SVG_TRANSFORM = re.compile(r"([A-Za-z]+)\s*\(([^)]*)\)")
_SVG_PATH_TOKEN = re.compile(rf"[AaCcHhLlMmQqSsTtVvZz]|{_NUMBER}")

Box = tuple[float, float, float, float]
Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_FOOTER_PLACEHOLDERS = {"ftr", "footer", "hdr", "header", "dt", "date", "sldnum", "slidenum", "slide_number"}
_BADGE_TERMS = {"badge", "logo", "watermark", "hackathon", "sih", "team id", "team name", "point blank"}
_TITLE_NOISE_TERMS = {"template", "placeholder", "watermark", "footer", "header", "logo", "badge"}
_PAGE_NUMBER = re.compile(r"^(?:(?:page|slide)\s*)?\d+(?:\s*(?:/|of)\s*\d+)?$")
_ROLE_KEYWORDS = {
    "diagram": ("diagram", "flow", "process", "pipeline", "architecture", "workflow", "schematic"),
    "screenshot": ("screenshot", "screen shot", "screen", "terminal", "browser", "desktop", "ui"),
    "chart": ("chart", "graph", "plot", "histogram", "bar", "line graph", "pie"),
    "evidence_image": ("evidence", "result", "output", "capture", "proof", "error", "log", "trace"),
    "logo": ("logo", "watermark", "brand", "hackathon", "sih", "point blank"),
    "decorative_image": ("decorative", "decoration", "ornament", "pattern", "background", "icon"),
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _round(value: float | int) -> float:
    rounded = round(float(value), 12)
    return 0.0 if rounded == 0 else rounded


def _box(values: Any) -> Box | None:
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return None
    try:
        left, top, width, height = (float(value) for value in values)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (left, top, width, height)):
        return None
    if width < 0 or height < 0:
        return None
    return left, top, width, height


def _box_list(values: Box | Iterable[float]) -> list[float]:
    return [_round(value) for value in values]


def _right(box: Box) -> float:
    return box[0] + box[2]


def _bottom(box: Box) -> float:
    return box[1] + box[3]


def _area(box: Box) -> float:
    return max(0.0, box[2]) * max(0.0, box[3])


def _normalised_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _object_label(item: dict[str, Any]) -> str:
    style = item.get("style") if isinstance(item.get("style"), dict) else {}
    resolved = item.get("resolved_style") if isinstance(item.get("resolved_style"), dict) else {}
    values = [
        item.get("name", ""),
        item.get("text", ""),
        style.get("placeholder_type", ""),
        resolved.get("placeholder_type", ""),
    ]
    return _normalised_text(" ".join(str(value) for value in values if value))


def _placeholder_type(item: dict[str, Any]) -> str:
    for key in ("style", "resolved_style"):
        style = item.get(key)
        if isinstance(style, dict) and style.get("placeholder_type"):
            return _normalised_text(style["placeholder_type"])
    return ""


def _near_edge(box: Box, tolerance: float = 0.08) -> bool:
    return box[0] <= tolerance or box[1] <= tolerance or _right(box) >= 1.0 - tolerance or _bottom(box) >= 1.0 - tolerance


def _is_page_number(item: dict[str, Any]) -> bool:
    text = _normalised_text(item.get("text"))
    if not text:
        return False
    return bool(_PAGE_NUMBER.fullmatch(text))


def _visual_exclusion_reasons(
    slides: Iterable[dict[str, Any]],
    objects_by_slide: Mapping[str, list[dict[str, Any]]],
) -> dict[str, list[str]]:
    """Identify slide chrome before calculating occupied-area signals."""
    slide_list = list(slides)
    all_objects = [item for objects in objects_by_slide.values() for item in objects if item.get("id")]
    repeated_text = Counter(
        _normalised_text(item.get("text"))
        for item in all_objects
        if _normalised_text(item.get("text"))
    )
    repeated_threshold = max(3, math.ceil(len(slide_list) * 0.6)) if len(slide_list) > 1 else 3
    reasons_by_id: dict[str, list[str]] = {}
    for item in all_objects:
        item_id = item["id"]
        box = _box(item.get("bbox"))
        if box is None:
            continue
        label = _object_label(item)
        name = _normalised_text(item.get("name"))
        text = _normalised_text(item.get("text"))
        placeholder = _placeholder_type(item)
        reasons: list[str] = []
        xml_part = str(item.get("source", {}).get("xml_part", "")).casefold() if isinstance(item.get("source"), dict) else ""
        if item.get("type") in {"background", "master"} or "slidemaster" in xml_part or "slidelayout" in xml_part:
            reasons.append("master")
        if "background" in name or "backdrop" in name or (
            item.get("type") == "shape" and not text and _area(box) >= 0.92 and item.get("z_order", 0) <= 2
        ):
            reasons.append("background")
        if placeholder in _FOOTER_PLACEHOLDERS:
            reasons.append("footer")
        if _is_page_number(item):
            reasons.append("page_number")
        if text and repeated_text[text] >= 2 and box[1] >= 0.80:
            reasons.append("repeated_footer")
        if any(term in label for term in _BADGE_TERMS) and (_near_edge(box) or _area(box) <= 0.05):
            reasons.append("badge")
        if text and repeated_text[text] >= repeated_threshold and (
            box[1] <= 0.18 or box[1] >= 0.80 or any(term in name for term in _TITLE_NOISE_TERMS)
        ):
            reasons.append("template_noise")
        if reasons:
            reasons_by_id[item_id] = sorted(set(reasons))
    return reasons_by_id


def _axis_overlap_ratio(first: Box, second: Box, axis: str) -> float:
    if axis == "x":
        overlap = max(0.0, min(_right(first), _right(second)) - max(first[0], second[0]))
        denominator = min(first[2], second[2])
    else:
        overlap = max(0.0, min(_bottom(first), _bottom(second)) - max(first[1], second[1]))
        denominator = min(first[3], second[3])
    return overlap / denominator if denominator > 0 else 0.0


def _rotation_degrees(item: dict[str, Any]) -> float | None:
    for style_key in ("style", "resolved_style"):
        style = item.get(style_key)
        if not isinstance(style, dict) or style.get("rotation_degrees") is None:
            continue
        try:
            value = float(style["rotation_degrees"])
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return _round(value)
    return None


def _intersection(first: Box, second: Box) -> tuple[Box, float] | None:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(_right(first), _right(second))
    bottom = min(_bottom(first), _bottom(second))
    if right <= left or bottom <= top:
        return None
    result = (left, top, right - left, bottom - top)
    return result, _area(result)


def _intersection_box(first: Box, second: Box) -> Box | None:
    overlap = _intersection(first, second)
    return overlap[0] if overlap is not None else None


def _clip(box: Box) -> Box | None:
    left = max(0.0, box[0])
    top = max(0.0, box[1])
    right = min(1.0, _right(box))
    bottom = min(1.0, _bottom(box))
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def _union_bbox(boxes: Iterable[Box]) -> Box:
    values = list(boxes)
    if not values:
        return 0.0, 0.0, 0.0, 0.0
    left = min(item[0] for item in values)
    top = min(item[1] for item in values)
    right = max(_right(item) for item in values)
    bottom = max(_bottom(item) for item in values)
    return left, top, right - left, bottom - top


def _merged_intervals(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    merged: list[list[float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(item[0], item[1]) for item in merged]


def _union_area(boxes: Iterable[Box]) -> float:
    """Return the exact axis-aligned union area using a small sweep line."""
    values = [item for box in boxes if (item := _clip(box)) is not None]
    if not values:
        return 0.0
    events: dict[float, list[tuple[int, Box]]] = defaultdict(list)
    x_values: set[float] = set()
    for index, item in enumerate(values):
        events[item[0]].append((index, item))
        events[_right(item)].append((index, item))
        x_values.update((item[0], _right(item)))
    active: dict[int, Box] = {}
    total = 0.0
    ordered_x = sorted(x_values)
    for index, x in enumerate(ordered_x[:-1]):
        for item_index, item in events.get(x, []):
            if item[0] == x:
                active[item_index] = item
            else:
                active.pop(item_index, None)
        next_x = ordered_x[index + 1]
        if next_x <= x or not active:
            continue
        y_union = _merged_intervals((_item[1], _bottom(_item)) for _item in active.values())
        total += (next_x - x) * sum(end - start for start, end in y_union)
    return _round(total)


def _empty_regions(boxes: list[Box]) -> list[Box]:
    """Return deterministic empty rectangles from the occupied x/y sweep."""
    clipped = [item for box in boxes if (item := _clip(box)) is not None]
    if not clipped:
        return [(0.0, 0.0, 1.0, 1.0)]
    x_values = sorted({0.0, 1.0, *(item[0] for item in clipped), *(_right(item) for item in clipped)})
    active_regions: dict[tuple[float, float], Box] = {}
    completed: list[Box] = []
    for left, right in zip(x_values, x_values[1:]):
        if right <= left:
            continue
        midpoint = (left + right) / 2.0
        occupied = _merged_intervals(
            (item[1], _bottom(item))
            for item in clipped
            if item[0] <= midpoint < _right(item)
        )
        empty: list[tuple[float, float]] = []
        cursor = 0.0
        for start, end in occupied:
            if start > cursor:
                empty.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < 1.0:
            empty.append((cursor, 1.0))
        current_keys = set(empty)
        for key in list(active_regions):
            if key not in current_keys or _right(active_regions[key]) != left:
                completed.append(active_regions.pop(key))
        for top, bottom in empty:
            previous = active_regions.get((top, bottom))
            if previous is None:
                active_regions[(top, bottom)] = (left, top, right - left, bottom - top)
            else:
                active_regions[(top, bottom)] = (previous[0], top, right - previous[0], bottom - top)
    completed.extend(active_regions.values())
    unique: dict[tuple[float, float, float, float], Box] = {}
    for item in completed:
        if _area(item) > 0:
            unique[tuple(_round(value) for value in item)] = item
    ordered = sorted(unique.values(), key=lambda item: (-_area(item), item[1], item[0], item[2], item[3]))
    return ordered[:MAX_WHITESPACE_REGIONS]


def _whitespace_balance(boxes: list[Box]) -> dict[str, float]:
    halves = {
        "left": (0.0, 0.0, 0.5, 1.0),
        "right": (0.5, 0.0, 0.5, 1.0),
        "top": (0.0, 0.0, 1.0, 0.5),
        "bottom": (0.0, 0.5, 1.0, 0.5),
    }
    whitespace: dict[str, float] = {}
    for name, half in halves.items():
        half_box = half
        occupied = _union_area(
            overlap
            for box in boxes
            if (overlap := _intersection_box(box, half_box)) is not None
        )
        whitespace[name] = _round(max(0.0, _area(half_box) - occupied))
    horizontal_total = whitespace["left"] + whitespace["right"]
    vertical_total = whitespace["top"] + whitespace["bottom"]
    horizontal_balance = 1.0 - abs(whitespace["left"] - whitespace["right"]) / horizontal_total if horizontal_total else 1.0
    vertical_balance = 1.0 - abs(whitespace["top"] - whitespace["bottom"]) / vertical_total if vertical_total else 1.0
    return {
        **whitespace,
        "horizontal_balance": _round(horizontal_balance),
        "vertical_balance": _round(vertical_balance),
        "balance": _round((horizontal_balance + vertical_balance) / 2.0),
        "whitespace_area_ratio": _round(1.0 - _union_area(boxes)),
    }


def _layout_objects(
    objects: Iterable[dict[str, Any]],
    excluded_ids: set[str] | None = None,
) -> list[tuple[dict[str, Any], Box]]:
    result: list[tuple[dict[str, Any], Box]] = []
    for item in sorted(objects, key=lambda value: (value.get("z_order", 0), value.get("id", ""))):
        if item.get("type") == "group":
            continue
        if excluded_ids and item.get("id") in excluded_ids:
            continue
        item_box = _box(item.get("bbox"))
        if item_box is not None:
            result.append((item, item_box))
    return result


def _peer_signature(item: dict[str, Any], box: Box) -> tuple[Any, ...]:
    return (
        item.get("type", "unknown"),
        item.get("shape_type"),
        _round(box[2]),
        _round(box[3]),
        _rotation_degrees(item),
    )


def _fact(
    fact_type: str,
    slide_id: str,
    value: dict[str, Any],
    bbox: Box | Iterable[float] = (0.0, 0.0, 1.0, 1.0),
    *,
    object_id: str | None = None,
    source: str = "native_geometry",
    status: str = "verified",
    confidence: float | None = 1.0,
) -> dict[str, Any]:
    fact_source = "native_ooxml" if source.startswith("native_") else source
    fact = {
        "type": fact_type,
        **value,
        "source": fact_source,
        "method": source,
        "status": status,
    }
    return {
        "slide_id": slide_id,
        "object_id": object_id,
        "bbox": _box_list(bbox),
        "value": fact,
        "confidence": confidence,
        "source": {
            "layer": "rendered_cv",
            "method": source,
            "authority": "native_ooxml" if source.startswith("native_") else "derived",
            "schema": VISUAL_SCHEMA_VERSION,
            "status": status,
        },
    }


def _add_facts(deck: DeckIR, facts: Iterable[dict[str, Any]], prefix: str = "visual") -> list[dict[str, Any]]:
    added: list[dict[str, Any]] = []
    counters: dict[str, int] = defaultdict(int)
    existing = {item.get("id"): item for item in deck.rendered_evidence}
    for item in facts:
        fact_type = item["value"]["type"]
        counters[fact_type] += 1
        record_id = f"{prefix}-{item['slide_id']}-{fact_type}-{counters[fact_type]:04d}"
        record = {
            "id": record_id,
            **item,
            "status": item["value"].get("status", "unverified"),
            "evidence_refs": _fact_evidence_refs(item),
        }
        if record_id in existing:
            added.append(existing[record_id])
            continue
        deck.add_evidence("rendered_evidence", record)
        existing[record_id] = record
        added.append(record)
    return added


def _fact_evidence_refs(item: dict[str, Any]) -> list[dict[str, Any]]:
    value = item.get("value", {}) if isinstance(item.get("value"), dict) else {}
    refs: list[dict[str, Any]] = []

    def add(value_id: Any, kind: str) -> None:
        if isinstance(value_id, str) and value_id:
            refs.append({"id": value_id, "kind": kind})

    add(item.get("object_id"), "native_object")
    add(value.get("object"), "native_object")
    add(value.get("selected_object_id"), "native_object")
    add(value.get("parent"), "native_object")
    add(value.get("asset_id"), "native_asset")
    objects = value.get("objects")
    if isinstance(objects, list):
        for object_id in objects:
            add(object_id, "native_object")
    children = value.get("children")
    if isinstance(children, list):
        for object_id in children:
            add(object_id, "native_object")
    evidence_sources = value.get("evidence_sources")
    if isinstance(evidence_sources, list):
        for reference in evidence_sources:
            if isinstance(reference, dict):
                add(reference.get("id"), str(reference.get("kind") or "evidence"))
    if not refs:
        add(item.get("slide_id"), "native_slide")
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for ref in refs:
        unique[(ref["id"], ref["kind"])] = ref
    return list(unique.values())


def _slide_reading_order(positioned: list[tuple[dict[str, Any], Box]]) -> str:
    text_boxes = [(item, box) for item, box in positioned if item.get("text")]
    if len(text_boxes) < 2:
        return "unknown"
    heights = [box[3] for _, box in text_boxes if box[3] > 0]
    widths = [box[2] for _, box in text_boxes if box[2] > 0]
    row_tolerance = max(0.04, (median(heights) if heights else 0.0) * 1.5)
    column_tolerance = max(0.04, (median(widths) if widths else 0.0) * 1.5)
    top_values = [box[1] for _, box in text_boxes]
    left_values = [box[0] for _, box in text_boxes]
    if max(top_values) - min(top_values) <= row_tolerance:
        return "left_to_right"
    if max(left_values) - min(left_values) <= column_tolerance:
        return "top_to_bottom"
    return "unknown"


def _visible_character_count(value: Any) -> int:
    return sum(not character.isspace() for character in str(value or ""))


def _title_selection(
    positioned: list[tuple[dict[str, Any], Box]],
    exclusion_reasons: Mapping[str, list[str]],
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    excluded_candidates: list[dict[str, Any]] = []
    for item, box in positioned:
        text = str(item.get("text", "")).strip()
        if not text or item.get("type") != "text":
            continue
        reasons = exclusion_reasons.get(item.get("id", ""), [])
        if reasons:
            excluded_candidates.append({"object": item["id"], "text": text, "reasons": reasons})
            continue
        placeholder = _placeholder_type(item)
        font_sizes = _font_sizes(item)
        font_size = max(font_sizes) if font_sizes else 0.0
        if placeholder not in {"title", "ctrtitle", "subtitle"} and box[1] > 0.38:
            continue
        score = (
            (100.0 if placeholder in {"title", "ctrtitle"} else 0.0)
            + max(0.0, 32.0 - box[1] * 64.0)
            + min(font_size, 48.0) / 4.0
            + min(_visible_character_count(text), 80) / 16.0
        )
        candidates.append(
            {
                "object": item["id"],
                "text": text,
                "bbox": _box_list(box),
                "font_size_pt": _round(font_size) if font_size else None,
                "placeholder_type": placeholder or None,
                "score": _round(score),
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["bbox"][1], item["bbox"][0], item["object"]))
    selected = candidates[0] if candidates else None
    return {
        "selected_object_id": selected["object"] if selected else None,
        "selected_text": selected["text"] if selected else None,
        "candidate_count": len(candidates),
        "candidates": candidates[:8],
        "excluded_candidates": excluded_candidates[:8],
        "selection_basis": ["title_placeholder", "top_position", "font_size", "visible_character_count"],
    }


def _font_observations(items: Iterable[dict[str, Any]]) -> list[tuple[float, float]]:
    observations: list[tuple[float, float]] = []
    for item in items:
        sizes = _font_sizes(item)
        characters = _visible_character_count(item.get("text"))
        if not sizes or characters <= 0:
            continue
        weight = characters / len(sizes)
        observations.extend((size, weight) for size in sizes)
    return observations


def classify_image_role(
    item: dict[str, Any],
    metadata: Mapping[str, Any] | None = None,
    occurrences: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a deterministic role candidate from native metadata and geometry."""
    metadata = dict(metadata or {})
    occurrences = list(occurrences or [item])
    label = _object_label(item)
    asset_part = _normalised_text(metadata.get("part") or item.get("asset_id"))
    context = f"{label} {asset_part}".strip()
    roles = ("diagram", "screenshot", "chart", "evidence_image", "logo", "decorative_image", "unknown")
    scores = {role: 0.0 for role in roles}
    reasons: list[str] = []
    for role, keywords in _ROLE_KEYWORDS.items():
        if role not in scores:
            continue
        matches = [
            keyword
            for keyword in keywords
            if re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", context)
        ]
        if matches:
            scores[role] = max(scores[role], 0.85)
            reasons.append(f"keyword:{role}:{matches[0]}")

    box = _box(item.get("bbox"))
    area = _area(box) if box else 0.0
    corner = _near_edge(box) if box else False
    repeated = len({value.get("slide_id") for value in occurrences if value.get("slide_id")})
    if repeated >= 2 and area <= 0.05 and corner:
        scores["logo"] = max(scores["logo"], 0.9)
        scores["decorative_image"] = max(scores["decorative_image"], 0.7)
        reasons.append("repeated_small_edge_asset")
    if metadata.get("has_alpha") is True and area <= 0.05 and corner:
        scores["logo"] = max(scores["logo"], 0.65)
        scores["decorative_image"] = max(scores["decorative_image"], 0.6)
        reasons.append("transparent_edge_asset")
    if area >= 0.65:
        scores["screenshot"] = max(scores["screenshot"], 0.35)
        scores["evidence_image"] = max(scores["evidence_image"], 0.3)
        reasons.append("large_displayed_asset")
    if area <= 0.03 and corner and not context:
        scores["decorative_image"] = max(scores["decorative_image"], 0.55)
        reasons.append("small_edge_asset")

    best_score = max(scores.values())
    if best_score <= 0:
        role = "unknown"
        confidence = None
        reasons.append("insufficient_native_role_evidence")
    else:
        priority = {role: index for index, role in enumerate(roles)}
        role = min((candidate for candidate, score in scores.items() if score == best_score), key=priority.get)
        confidence = _round(min(best_score, 0.95))
    scores["unknown"] = _round(max(0.0, 1.0 - max(scores.values())))
    return {
        "image_role": role,
        "role": role,
        "role_scores": {key: _round(value) for key, value in scores.items()},
        "role_evidence": sorted(set(reasons)),
        "status": "partial" if role != "unknown" else "unverified",
        "confidence": confidence,
    }


def _add_visual_regions(
    deck: DeckIR,
    objects_by_slide: Mapping[str, list[dict[str, Any]]],
    exclusion_reasons: Mapping[str, list[str]] | None = None,
) -> None:
    """Create compact slide-level regions without duplicating every object box."""
    for slide in sorted(deck.slides, key=lambda item: (item.get("number", 0), item.get("id", ""))):
        slide_id = slide["id"]
        positioned = _layout_objects(objects_by_slide.get(slide_id, []), set(exclusion_reasons or {}))
        slide["slide_reading_order"] = _slide_reading_order(positioned)
        slide["visual_region_ids"] = []
        boxes = [item_box for _, item_box in positioned if _area(item_box) > 0]
        if not boxes:
            continue
        region_id = f"visual-region-{slide_id}-content"
        refs = [{"id": item["id"], "kind": "native_object"} for item, _ in positioned if item.get("id")]
        region = {
            "id": region_id,
            "slide_id": slide_id,
            "object_id": None,
            "bbox": _box_list(_union_bbox(boxes)),
            "value": {
                "type": "visual_region",
                "region_kind": "content",
                "coordinate_space": "slide_normalized",
                "source": "native_ooxml",
                "status": "verified",
            },
            "status": "verified",
            "confidence": 1.0,
            "evidence_refs": refs or [{"id": slide_id, "kind": "native_slide"}],
            "source": {
                "layer": "rendered_cv",
                "method": "native_visual_region",
                "authority": "native_ooxml",
                "schema": VISUAL_SCHEMA_VERSION,
                "status": "verified",
            },
        }
        existing_index = next((index for index, item in enumerate(deck.visual_regions) if item.get("id") == region_id), None)
        if existing_index is None:
            deck.add_evidence("visual_regions", region)
        else:
            deck.visual_regions[existing_index] = region
        slide["visual_region_ids"] = [region_id]



def _font_sizes(item: dict[str, Any]) -> list[float]:
    resolved = item.get("resolved_style") if isinstance(item.get("resolved_style"), dict) else {}
    run_sizes = resolved.get("run_font_sizes_pt")
    if isinstance(run_sizes, list):
        result: list[float] = []
        for value in run_sizes:
            try:
                result.append(_round(float(value)))
            except (TypeError, ValueError):
                continue
        if result:
            return result
    raw_style = item.get("raw_style") if isinstance(item.get("raw_style"), dict) else {}
    raw_runs = raw_style.get("runs", [])
    sizes: list[float] = []
    for run in raw_runs if isinstance(raw_runs, list) else []:
        raw_size = run.get("font_size_pt") if isinstance(run, dict) else None
        try:
            if raw_size is not None:
                sizes.append(_round(float(raw_size) / 100.0))
        except (TypeError, ValueError):
            continue
    if sizes:
        return sizes
    resolved_size = resolved.get("font_size_pt")
    try:
        return [_round(float(resolved_size))] if resolved_size is not None else []
    except (TypeError, ValueError):
        return []


def _color_values(item: dict[str, Any]) -> list[tuple[str, str]]:
    style = item.get("resolved_style") if isinstance(item.get("resolved_style"), dict) else {}
    candidates = (
        ("fill", style.get("fill")),
        ("line", style.get("line", {}).get("fill") if isinstance(style.get("line"), dict) else None),
        ("font", style.get("font_color")),
    )
    values: list[tuple[str, str]] = []
    run_colors = style.get("run_font_colors")
    if isinstance(run_colors, list):
        for color in run_colors:
            if isinstance(color, dict) and color.get("color"):
                values.append(("font", str(color["color"]).upper()))
        if run_colors:
            candidates = tuple(item for item in candidates if item[0] != "font")
    for role, color in candidates:
        if not isinstance(color, dict) or not color.get("color"):
            continue
        values.append((role, str(color["color"]).upper()))
    return values


def _slide_native_facts(
    slide: dict[str, Any],
    objects: list[dict[str, Any]],
    assets_by_id: Mapping[str, dict[str, Any]],
    asset_bytes: Mapping[str, bytes],
    slide_aspect_ratio: float = 1.0,
    exclusion_reasons: Mapping[str, list[str]] | None = None,
    image_occurrences: Mapping[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    slide_id = slide["id"]
    exclusion_reasons = exclusion_reasons or {}
    image_occurrences = image_occurrences or {}
    all_positioned = _layout_objects(objects)
    positioned = _layout_objects(objects, set(exclusion_reasons))
    boxes = [item[1] for item in positioned]
    area_boxes = [box for box in boxes if _area(box) > 0]
    excluded_positioned = [item for item in all_positioned if item[0].get("id") in exclusion_reasons]
    excluded_boxes = [item_box for _, item_box in excluded_positioned if _area(item_box) > 0]
    clipped = [clipped_box for box in area_boxes if (clipped_box := _clip(box)) is not None]
    content_bbox = _union_bbox(clipped)
    occupied_area = _union_area(area_boxes)
    connector_ids = [item["id"] for item in objects if item.get("type") == "connector" and item.get("id")]
    exclusion_summary = [
        {
            "id": item[0]["id"],
            "reasons": exclusion_reasons[item[0]["id"]],
            "bbox": _box_list(item[1]),
        }
        for item in excluded_positioned
    ]
    occupancy = _fact(
        "slide_occupancy",
        slide_id,
        {
            "coordinate_space": "slide_normalized",
            "occupied_area_ratio": _round(occupied_area),
            "occupied_bbox": _box_list(content_bbox),
            "occupied_bbox_ratio": _round(_area(content_bbox)),
            "object_count": len(positioned),
            "area_object_count": len(area_boxes),
            "total_object_count": len(all_positioned),
            "included_object_count": len(positioned),
            "excluded_object_count": len(excluded_positioned),
            "excluded_area_ratio": _round(_union_area(excluded_boxes)),
            "excluded_objects": exclusion_summary,
            "native_connector_count": len(connector_ids),
        },
        status="partial" if excluded_positioned else "verified",
        confidence=0.5 if excluded_positioned else 1.0,
    )
    facts: list[dict[str, Any]] = [occupancy]

    if excluded_positioned:
        facts.append(
            _fact(
                "visual_exclusions",
                slide_id,
                {
                    "objects": [item[0]["id"] for item in excluded_positioned],
                    "excluded_objects": exclusion_summary,
                    "basis": "deterministic_native_chrome_heuristics",
                    "coordinate_space": "slide_normalized",
                },
                _union_bbox(excluded_boxes),
                status="partial",
                confidence=0.5,
            )
        )

    empty_regions = _empty_regions(area_boxes)
    for region in empty_regions:
        facts.append(
            _fact(
                "whitespace_region",
                slide_id,
                {
                    "coordinate_space": "slide_normalized",
                    "area_ratio": _round(_area(region)),
                    "region_kind": "empty_axis_aligned_region",
                },
                region,
            )
        )
    largest_empty = empty_regions[0] if empty_regions else (0.0, 0.0, 1.0, 1.0)
    facts.append(
        _fact(
            "largest_empty_region",
            slide_id,
            {
                "coordinate_space": "slide_normalized",
                "region": _box_list(largest_empty),
                "area_ratio": _round(_area(largest_empty)),
                "region_kind": "largest_empty_axis_aligned_region",
            },
            largest_empty,
        )
    )
    balance = _whitespace_balance(area_boxes)
    facts.append(
        _fact(
            "whitespace_balance",
            slide_id,
            {
                "coordinate_space": "slide_normalized",
                **balance,
            },
        )
    )

    connector_refs = [
        {"id": item["id"], "kind": "native_object", "source": item.get("source")}
        for item in objects
        if item.get("type") == "connector" and item.get("id")
    ]
    connector_ids = [item["id"] for item in objects if item.get("type") == "connector" and item.get("id")]
    flow_candidate = True if connector_ids else None
    facts.append(
        _fact(
            "native_connector_count",
            slide_id,
            {
                "native_connector_count": len(connector_ids),
                "count": len(connector_ids),
                "objects": connector_ids,
                "evidence_sources": connector_refs or [{"id": slide_id, "kind": "native_slide"}],
                "coordinate_space": "slide_normalized",
            },
            status="verified",
            confidence=1.0,
        )
    )
    facts.append(
        _fact(
            "flow_candidate",
            slide_id,
            {
                "flow_candidate": flow_candidate,
                "native_connector_count": len(connector_ids),
                "evidence_sources": connector_refs or [{"id": slide_id, "kind": "native_slide"}],
                "basis": "native_connector" if connector_ids else "no_native_connectors_not_evidence_of_absence",
                "coordinate_space": "slide_normalized",
            },
            status="partial" if connector_ids else "unverified",
            confidence=0.5 if connector_ids else None,
        )
    )

    title_selection = _title_selection(all_positioned, exclusion_reasons)
    selected_candidate = next(
        (item for item in title_selection["candidates"] if item.get("object") == title_selection.get("selected_object_id")),
        None,
    )
    title_objects = [item["object"] for item in title_selection["candidates"]]
    title_objects.extend(item["object"] for item in title_selection["excluded_candidates"])
    title_status = "unverified"
    title_confidence = None
    if selected_candidate is not None:
        title_status = "verified" if selected_candidate.get("placeholder_type") in {"title", "ctrtitle"} else "partial"
        title_confidence = 1.0 if title_status == "verified" else 0.5
    title_bbox = _box(selected_candidate.get("bbox")) if selected_candidate else None
    facts.append(
        _fact(
            "slide_title_candidate",
            slide_id,
            {
                **title_selection,
                "objects": title_objects,
                "coordinate_space": "slide_normalized",
            },
            title_bbox or (0.0, 0.0, 1.0, 1.0),
            status=title_status,
            confidence=title_confidence,
        )
    )

    if area_boxes:
        raw_margins = {
            "left": _round(min(item[0] for item in area_boxes)),
            "top": _round(min(item[1] for item in area_boxes)),
            "right": _round(1.0 - max(_right(item) for item in area_boxes)),
            "bottom": _round(1.0 - max(_bottom(item) for item in area_boxes)),
        }
        margins = {
            key: _round(max(0.0, value))
            for key, value in raw_margins.items()
        }
    else:
        raw_margins = {key: 1.0 for key in ("left", "top", "right", "bottom")}
        margins = dict(raw_margins)
    facts.append(
        _fact(
            "margins",
            slide_id,
            {
                "coordinate_space": "slide_normalized",
                "margins": margins,
                "raw_margins": raw_margins,
                "content_bbox": _box_list(content_bbox),
                "object_count": len(positioned),
            },
            content_bbox if clipped else (0.0, 0.0, 1.0, 1.0),
        )
    )

    for index, (first, first_box) in enumerate(positioned):
        for second, second_box in positioned[index + 1 :]:
            overlap = _intersection(first_box, second_box)
            if overlap is None:
                continue
            intersection_box, intersection_area = overlap
            first_area, second_area = _area(first_box), _area(second_box)
            smaller_area = min(first_area, second_area)
            union = first_area + second_area - intersection_area
            facts.append(
                _fact(
                    "object_overlap",
                    slide_id,
                    {
                        "objects": [first["id"], second["id"]],
                        "intersection_area": _round(intersection_area),
                        "overlap_ratio": _round(intersection_area / smaller_area) if smaller_area else 0.0,
                        "intersection_over_union": _round(intersection_area / union) if union else 0.0,
                        "z_order": [first.get("z_order"), second.get("z_order")],
                        "coordinate_space": "slide_normalized",
                    },
                    intersection_box,
                )
            )

    alignment_groups: dict[tuple[str, ...], dict[str, Any]] = {}
    edge_specs = (
        ("left", lambda item: item[1][0], "x"),
        ("right", lambda item: _right(item[1]), "x"),
        ("center_x", lambda item: item[1][0] + item[1][2] / 2.0, "x"),
        ("top", lambda item: item[1][1], "y"),
        ("bottom", lambda item: _bottom(item[1]), "y"),
        ("center_y", lambda item: item[1][1] + item[1][3] / 2.0, "y"),
    )
    for edge, coordinate, axis in edge_specs:
        ordered = sorted(positioned, key=lambda item: (coordinate(item), item[0]["id"]))
        cluster: list[tuple[dict[str, Any], Box]] = []
        cluster_value: float | None = None
        for item in ordered + [(None, None)]:  # type: ignore[list-item]
            if item[0] is not None:
                current = coordinate(item)  # type: ignore[arg-type]
                if cluster and cluster_value is not None and abs(current - cluster_value) > ALIGNMENT_TOLERANCE:
                    ids = tuple(sorted(value[0]["id"] for value in cluster))
                    if len(ids) >= 2:
                        entry = alignment_groups.setdefault(ids, {"edges": set(), "axis": set(), "items": cluster})
                        entry["edges"].add(edge)
                        entry["axis"].add(axis)
                    cluster = []
                if not cluster:
                    cluster_value = current
                cluster.append(item)  # type: ignore[arg-type]
            elif cluster:
                ids = tuple(sorted(value[0]["id"] for value in cluster))
                if len(ids) >= 2:
                    entry = alignment_groups.setdefault(ids, {"edges": set(), "axis": set(), "items": cluster})
                    entry["edges"].add(edge)
                    entry["axis"].add(axis)
                cluster = []
    for ids in sorted(alignment_groups):
        entry = alignment_groups[ids]
        item_map = {item[0]["id"]: item for item in positioned}
        union_box = _union_bbox(item_map[item_id][1] for item_id in ids)
        facts.append(
            _fact(
                "alignment",
                slide_id,
                {
                    "objects": list(ids),
                    "edges": sorted(entry["edges"]),
                    "axes": sorted(entry["axis"]),
                    "tolerance": ALIGNMENT_TOLERANCE,
                    "coordinate_space": "slide_normalized",
                },
                union_box,
            )
        )
        facts.append(
            _fact(
                "alignment_peer_group",
                slide_id,
                {
                    "objects": list(ids),
                    "peer_group_type": "alignment",
                    "edges": sorted(entry["edges"]),
                    "axes": sorted(entry["axis"]),
                    "tolerance": ALIGNMENT_TOLERANCE,
                    "coordinate_space": "slide_normalized",
                },
                union_box,
                status="partial",
                confidence=0.5,
            )
        )

    mismatch_specs = (
        ("left", lambda value: value[0], "x", "y"),
        ("right", lambda value: _right(value), "x", "y"),
        ("center_x", lambda value: value[0] + value[2] / 2.0, "x", "y"),
        ("top", lambda value: value[1], "y", "x"),
        ("bottom", lambda value: _bottom(value), "y", "x"),
        ("center_y", lambda value: value[1] + value[3] / 2.0, "y", "x"),
    )
    for index, (first, first_box) in enumerate(positioned):
        for second, second_box in positioned[index + 1 :]:
            pair_mismatches: list[tuple[float, str, str]] = []
            for edge, coordinate, axis, orthogonal_axis in mismatch_specs:
                if _axis_overlap_ratio(first_box, second_box, orthogonal_axis) < 0.75:
                    continue
                distance = abs(coordinate(first_box) - coordinate(second_box))
                if not ALIGNMENT_TOLERANCE < distance <= ALIGNMENT_MISMATCH_TOLERANCE:
                    continue
                pair_mismatches.append((distance, edge, axis))
            if pair_mismatches:
                distance, edge, axis = min(pair_mismatches, key=lambda item: (item[0], item[1], item[2]))
                facts.append(
                    _fact(
                        "alignment_mismatch",
                        slide_id,
                        {
                            "objects": [first["id"], second["id"]],
                            "edge": edge,
                            "axis": axis,
                            "distance": _round(distance),
                            "tolerance": ALIGNMENT_TOLERANCE,
                            "mismatch_radius": ALIGNMENT_MISMATCH_TOLERANCE,
                            "coordinate_space": "slide_normalized",
                        },
                        _union_bbox((first_box, second_box)),
                    )
                )

    for axis, coordinate, start, end, orthogonal, direction in (
        (
            "horizontal",
            lambda item: item[1][1] + item[1][3] / 2.0,
            lambda item: item[1][0],
            lambda item: _right(item[1]),
            "y",
            "left_to_right",
        ),
        (
            "vertical",
            lambda item: item[1][0] + item[1][2] / 2.0,
            lambda item: item[1][1],
            lambda item: _bottom(item[1]),
            "x",
            "top_to_bottom",
        ),
    ):
        ordered = sorted(positioned, key=lambda item: (coordinate(item), item[0]["id"]))
        clusters: list[list[tuple[dict[str, Any], Box]]] = []
        for item in ordered:
            if not clusters or abs(coordinate(item) - coordinate(clusters[-1][0])) > ALIGNMENT_TOLERANCE * 2:
                clusters.append([item])
            else:
                clusters[-1].append(item)
        seen_spacing: set[tuple[str, ...]] = set()
        for cluster in clusters:
            along = sorted(cluster, key=lambda item: (start(item), item[0]["id"]))
            if len(along) < 3:
                continue
            windows = [along] if len(along) == 3 else [along[index : index + 3] for index in range(len(along) - 2)]
            for window in windows:
                gaps = [_round(start(window[index + 1]) - end(window[index])) for index in range(len(window) - 1)]
                if any(gap < -ALIGNMENT_TOLERANCE for gap in gaps):
                    continue
                spacing = sum(gaps) / len(gaps)
                if spacing <= ALIGNMENT_TOLERANCE:
                    continue
                if max(gaps) - min(gaps) > max(ALIGNMENT_TOLERANCE, abs(spacing) * 0.05):
                    continue
                ids = tuple(item[0]["id"] for item in window)
                if ids in seen_spacing:
                    continue
                seen_spacing.add(ids)
                facts.append(
                    _fact(
                        "equal_spacing",
                        slide_id,
                        {
                            "objects": list(ids),
                            "axis": axis,
                            "direction": direction,
                            "gaps": gaps,
                            "spacing": _round(spacing),
                            "spacing_variance": _round(sum((gap - spacing) ** 2 for gap in gaps) / len(gaps)),
                            "orthogonal_axis": orthogonal,
                            "tolerance": ALIGNMENT_TOLERANCE,
                            "coordinate_space": "slide_normalized",
                        },
                        _union_bbox(item[1] for item in window),
                    )
                )
                facts.append(
                    _fact(
                        "spacing_peer_group",
                        slide_id,
                        {
                            "objects": list(ids),
                            "peer_group_type": "spacing",
                            "axis": axis,
                            "direction": direction,
                            "gaps": gaps,
                            "spacing": _round(spacing),
                            "spacing_variance": _round(sum((gap - spacing) ** 2 for gap in gaps) / len(gaps)),
                            "tolerance": ALIGNMENT_TOLERANCE,
                            "coordinate_space": "slide_normalized",
                        },
                        _union_bbox(item[1] for item in window),
                        status="partial",
                        confidence=0.5,
                    )
                )

    for item, item_box in positioned:
        visible = _clip(item_box)
        visible_area = _area(visible) if visible is not None else 0.0
        total_area = _area(item_box)
        edges = []
        if item_box[0] < 0:
            edges.append("left")
        if item_box[1] < 0:
            edges.append("top")
        if _right(item_box) > 1:
            edges.append("right")
        if _bottom(item_box) > 1:
            edges.append("bottom")
        if not edges:
            continue
        facts.append(
            _fact(
                "clipping_overflow",
                slide_id,
                {
                    "object": item["id"],
                    "clipped_edges": edges,
                    "outside_area_ratio": _round(max(0.0, 1.0 - visible_area / total_area)) if total_area else 1.0,
                    "visible_area_ratio": _round(visible_area / total_area) if total_area else 0.0,
                    "coordinate_space": "slide_normalized",
                },
                item_box,
                object_id=item["id"],
            )
        )

    rotations = []
    for item, item_box in positioned:
        rotation = _rotation_degrees(item)
        if rotation is None:
            continue
        rotations.append({"object": item["id"], "rotation_degrees": rotation})
        if rotation == 0:
            continue
        facts.append(
            _fact(
                "rotation",
                slide_id,
                {
                    "object": item["id"],
                    "rotation_degrees": rotation,
                    "coordinate_space": "slide_normalized",
                },
                item_box,
                object_id=item["id"],
            )
        )
    facts.append(
        _fact(
            "rotation_distribution",
            slide_id,
            {
                "rotations": rotations,
                "rotated_object_count": sum(item["rotation_degrees"] != 0 for item in rotations),
                "known_rotation_count": len(rotations),
                "coordinate_space": "slide_normalized",
            }
        )
    )

    objects_by_id = {item["id"]: item for item in objects if item.get("id")}
    for group in (item for item in objects if item.get("type") == "group" and item.get("id")):
        children = sorted(
            item["id"]
            for item in objects
            if item.get("parent_id") == group["id"] and item.get("id")
        )
        if not children:
            continue
        group_box = _box(group.get("bbox"))
        child_boxes = [_box(objects_by_id[child].get("bbox")) for child in children]
        hierarchy_box = group_box or _union_bbox(item for item in child_boxes if item is not None)
        facts.append(
            _fact(
                "shape_hierarchy_candidate",
                slide_id,
                {
                    "parent": group["id"],
                    "children": children,
                    "objects": [group["id"], *children],
                    "relation": "native_parent_child",
                    "coordinate_space": "slide_normalized",
                },
                hierarchy_box,
                object_id=group["id"],
            )
        )

    peer_groups: dict[tuple[Any, ...], list[tuple[dict[str, Any], Box]]] = defaultdict(list)
    for item, item_box in positioned:
        peer_groups[_peer_signature(item, item_box)].append((item, item_box))
    for signature, members in sorted(peer_groups.items(), key=lambda item: repr(item[0])):
        if len(members) < 3:
            continue
        member_ids = sorted(item[0]["id"] for item in members)
        facts.append(
            _fact(
                "shape_peer_group",
                slide_id,
                {
                    "objects": member_ids,
                    "count": len(member_ids),
                    "signature": list(signature),
                    "basis": ["type", "shape_type", "width", "height", "rotation"],
                    "coordinate_space": "slide_normalized",
                },
                _union_bbox(item[1] for item in members),
                status="partial",
                confidence=0.5,
            )
        )

    text_items = [item for item, _ in positioned if item.get("text")]
    font_observations = _font_observations(text_items)
    font_values = [size for size, _ in font_observations]
    histogram = Counter(font_values)
    character_weights: dict[float, float] = defaultdict(float)
    for size, weight in font_observations:
        character_weights[size] += weight
    visible_font_characters = sum(character_weights.values())
    weighted_mean = (
        sum(size * weight for size, weight in font_observations) / visible_font_characters
        if visible_font_characters
        else None
    )
    weighted_median = None
    if visible_font_characters:
        running_weight = 0.0
        for size in sorted(character_weights):
            running_weight += character_weights[size]
            if running_weight >= visible_font_characters / 2.0:
                weighted_median = size
                break
    facts.append(
        _fact(
            "font_size_distribution",
            slide_id,
            {
                "font_sizes_pt": sorted(font_values),
                "histogram": [
                    {
                        "size_pt": size,
                        "count": histogram[size],
                        "visible_character_count": _round(character_weights[size]),
                    }
                    for size in sorted(histogram)
                ],
                "count": len(font_values),
                "weighting": "visible_character_count",
                "visible_character_count": _round(visible_font_characters),
                "known_text_object_count": sum(bool(_font_sizes(item)) for item in text_items),
                "unknown_text_object_count": sum(not _font_sizes(item) for item in text_items),
                "minimum_pt": _round(min(font_values)) if font_values else None,
                "maximum_pt": _round(max(font_values)) if font_values else None,
                "mean_pt": _round(sum(font_values) / len(font_values)) if font_values else None,
                "median_pt": _round(float(median(font_values))) if font_values else None,
                "weighted_mean_pt": _round(weighted_mean) if weighted_mean is not None else None,
                "weighted_median_pt": _round(weighted_median) if weighted_median is not None else None,
            },
            source="native_style",
        )
    )
    dominant_size = max(character_weights, key=lambda size: (character_weights[size], -size)) if character_weights else None
    facts.append(
        _fact(
            "font_consistency",
            slide_id,
            {
                "weighting": "visible_character_count",
                "visible_character_count": _round(visible_font_characters),
                "dominant_size_pt": dominant_size,
                "dominant_character_ratio": _round(character_weights[dominant_size] / visible_font_characters) if dominant_size is not None and visible_font_characters else None,
                "weighted_size_variance": _round(
                    sum(weight * (size - weighted_mean) ** 2 for size, weight in font_observations) / visible_font_characters
                )
                if weighted_mean is not None and visible_font_characters
                else None,
            },
            source="native_style",
        )
    )

    color_counts: Counter[str] = Counter()
    color_roles: dict[str, Counter[str]] = defaultdict(Counter)
    for item, _ in positioned:
        for role, color in _color_values(item):
            color_counts[color] += 1
            color_roles[color][role] += 1
    total_colors = sum(color_counts.values())
    palette = [
        {
            "color": color,
            "count": color_counts[color],
            "roles": {role: color_roles[color][role] for role in sorted(color_roles[color])},
        }
        for color in sorted(color_counts, key=lambda value: (-color_counts[value], value))
    ]
    facts.append(
        _fact(
            "color_consistency",
            slide_id,
            {
                "palette": palette,
                "unique_color_count": len(palette),
                "sample_count": total_colors,
                "dominant_color": palette[0]["color"] if palette else None,
                "dominant_ratio": _round(palette[0]["count"] / total_colors) if palette else None,
            },
            source="native_style",
        )
    )

    type_counts = Counter(item.get("type", "unknown") for item, _ in positioned)
    area_sum = sum(_area(item_box) for _, item_box in positioned)
    facts.append(
        _fact(
            "shape_density",
            slide_id,
            {
                "shape_count": len(positioned),
                "type_counts": {key: type_counts[key] for key in sorted(type_counts)},
                "non_text_shape_count": sum(value for key, value in type_counts.items() if key != "text"),
                "box_area_sum_ratio": _round(area_sum),
                "union_area_ratio": _round(occupied_area),
                "objects_per_occupied_area": _round(len(positioned) / occupied_area) if occupied_area else None,
            },
        )
    )

    text_boxes = [item_box for item, item_box in positioned if item.get("text") and _area(item_box) > 0]
    text_chars = sum(len(str(item.get("text", ""))) for item, _ in positioned)
    text_words = sum(len(str(item.get("text", "")).split()) for item, _ in positioned)
    text_area = _union_area(text_boxes)
    facts.append(
        _fact(
            "text_density",
            slide_id,
            {
                "text_object_count": len(text_items),
                "character_count": text_chars,
                "word_count": text_words,
                "text_union_area_ratio": _round(text_area),
                "characters_per_slide_area": text_chars,
                "characters_per_text_area": _round(text_chars / text_area) if text_area else None,
            },
            source="native_geometry",
        )
    )

    asset_metadata: dict[str, dict[str, Any]] = {}
    for item, item_box in all_positioned:
        if item.get("type") != "image" or not item.get("asset_id"):
            continue
        asset_id = item["asset_id"]
        asset = assets_by_id.get(asset_id, {})
        part = asset.get("part")
        data = asset_bytes.get(part) if part else None
        if asset_id not in asset_metadata:
            asset_metadata[asset_id] = _image_metadata(data, asset.get("content_type"), asset.get("sha256"))
        metadata = asset_metadata[asset_id]
        role_info = classify_image_role(item, {**metadata, "part": part}, image_occurrences.get(asset_id, [item]))
        display_ratio = item_box[2] * slide_aspect_ratio / item_box[3] if item_box[3] else None
        source_ratio = metadata.get("aspect_ratio")
        crop = item.get("geometry", {}).get("crop") if isinstance(item.get("geometry"), dict) else None
        crop_adjusted_ratio = source_ratio
        if source_ratio and isinstance(crop, dict):
            crop_width = 1.0 - float(crop.get("left", 0.0)) - float(crop.get("right", 0.0))
            crop_height = 1.0 - float(crop.get("top", 0.0)) - float(crop.get("bottom", 0.0))
            if crop_width > 0 and crop_height > 0:
                crop_adjusted_ratio = source_ratio * crop_width / crop_height
        facts.append(
            _fact(
                "image_asset_analysis",
                slide_id,
                {
                    **metadata,
                    "asset_id": asset_id,
                    "display_bbox": _box_list(item_box),
                    "slide_aspect_ratio": _round(slide_aspect_ratio),
                    "display_aspect_ratio": _round(display_ratio) if display_ratio else None,
                    "aspect_ratio_delta": _round(abs(source_ratio - display_ratio)) if source_ratio and display_ratio else None,
                    "crop_adjusted_aspect_ratio": _round(crop_adjusted_ratio) if crop_adjusted_ratio else None,
                    "crop_adjusted_aspect_ratio_delta": _round(abs(crop_adjusted_ratio - display_ratio)) if crop_adjusted_ratio and display_ratio else None,
                    "crop": crop,
                    "rotation_degrees": item.get("style", {}).get("rotation_degrees") if isinstance(item.get("style"), dict) else None,
                    "analysis_target": "original_asset",
                    "image_role": role_info["image_role"],
                    "image_role_confidence": role_info["confidence"],
                },
                item_box,
                object_id=item["id"],
                source="native_asset",
            )
        )
        facts.append(
            _fact(
                "image_role_candidate",
                slide_id,
                {
                    "asset_id": asset_id,
                    "object": item["id"],
                    "image_role": role_info["image_role"],
                    "role": role_info["role"],
                    "role_scores": role_info["role_scores"],
                    "role_evidence": role_info["role_evidence"],
                    "coordinate_space": "slide_normalized",
                },
                item_box,
                object_id=item["id"],
                source="native_asset",
                status=role_info["status"],
                confidence=role_info["confidence"],
            )
        )
    return facts


def add_native_visual_evidence(
    deck: DeckIR,
    asset_bytes_by_part: Mapping[str, bytes] | None = None,
) -> list[dict[str, Any]]:
    """Append native visual facts to ``deck.rendered_evidence``.

    The output is intentionally evidence rather than a quality score.  All
    calculations use normalized native object boxes and resolved native style.
    """
    asset_bytes = asset_bytes_by_part or {}
    objects_by_slide: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in deck.objects:
        objects_by_slide[item["slide_id"]].append(item)
    exclusion_reasons = _visual_exclusion_reasons(deck.slides, objects_by_slide)
    image_occurrences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in deck.objects:
        if item.get("type") == "image" and item.get("asset_id"):
            image_occurrences[item["asset_id"]].append(item)
    _add_visual_regions(deck, objects_by_slide, exclusion_reasons)
    assets_by_id = {item["id"]: item for item in deck.assets}
    try:
        slide_aspect_ratio = float(deck.deck.get("slide_aspect_ratio", 1.0))
    except (TypeError, ValueError):
        slide_aspect_ratio = 1.0
    facts: list[dict[str, Any]] = []
    for slide in sorted(deck.slides, key=lambda item: (item.get("number", 0), item.get("id", ""))):
        facts.extend(
            _slide_native_facts(
                slide,
                objects_by_slide[slide["id"]],
                assets_by_id,
                asset_bytes,
                slide_aspect_ratio,
                exclusion_reasons,
                image_occurrences,
            )
        )
    return _add_facts(deck, facts)


def _matrix_multiply(outer: Matrix, inner: Matrix) -> Matrix:
    oa, ob, oc, od, oe, of = outer
    ia, ib, ic, id_, ie, iff = inner
    return (
        oa * ia + oc * ib,
        ob * ia + od * ib,
        oa * ic + oc * id_,
        ob * ic + od * id_,
        oa * ie + oc * iff + oe,
        ob * ie + od * iff + of,
    )


def _matrix_apply(matrix: Matrix, point: tuple[float, float]) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    x, y = point
    return a * x + c * y + e, b * x + d * y + f


def _svg_transform(value: str | None) -> Matrix:
    matrix = IDENTITY
    if not value:
        return matrix
    for name, raw_args in _SVG_TRANSFORM.findall(value):
        args = [float(item) for item in _SVG_NUMBER.findall(raw_args)]
        operation: Matrix = IDENTITY
        lowered = name.lower()
        if lowered == "translate" and args:
            operation = (1.0, 0.0, 0.0, 1.0, args[0], args[1] if len(args) > 1 else 0.0)
        elif lowered == "scale" and args:
            operation = (args[0], 0.0, 0.0, args[1] if len(args) > 1 else args[0], 0.0, 0.0)
        elif lowered == "matrix" and len(args) >= 6:
            operation = tuple(args[:6])  # type: ignore[assignment]
        elif lowered == "rotate" and args:
            angle = math.radians(args[0])
            cosine, sine = math.cos(angle), math.sin(angle)
            rotation = (cosine, sine, -sine, cosine, 0.0, 0.0)
            if len(args) >= 3:
                cx, cy = args[1], args[2]
                operation = _matrix_multiply(
                    (1.0, 0.0, 0.0, 1.0, cx, cy),
                    _matrix_multiply(rotation, (1.0, 0.0, 0.0, 1.0, -cx, -cy)),
                )
            else:
                operation = rotation
        matrix = _matrix_multiply(matrix, operation)
    return matrix


def _svg_shape_box(element: Any) -> Box | None:
    name = _local_name(element.tag)
    if name in {"rect", "image"}:
        try:
            left = float(element.get("x", 0))
            top = float(element.get("y", 0))
            width = float(element.get("width", 0))
            height = float(element.get("height", 0))
            return _box((left, top, width, height))
        except (TypeError, ValueError):
            return None
    if name == "line":
        try:
            x1, y1 = float(element.get("x1", 0)), float(element.get("y1", 0))
            x2, y2 = float(element.get("x2", 0)), float(element.get("y2", 0))
        except (TypeError, ValueError):
            return None
        return _box((min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)))
    if name in {"polygon", "polyline"}:
        values = [float(item) for item in _SVG_NUMBER.findall(element.get("points", ""))]
        points = list(zip(values[::2], values[1::2]))
        if not points:
            return None
        return _box((min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points) - min(x for x, _ in points), max(y for _, y in points) - min(y for _, y in points)))
    if name == "path":
        tokens = _SVG_PATH_TOKEN.findall(element.get("d", ""))
        points: list[tuple[float, float]] = []
        current = (0.0, 0.0)
        start = current
        command = ""
        index = 0
        arities = {"M": 2, "L": 2, "T": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "A": 7}
        while index < len(tokens):
            if tokens[index].isalpha():
                command = tokens[index]
                index += 1
            if not command:
                break
            upper = command.upper()
            if upper == "Z":
                current = start
                points.append(current)
                command = ""
                continue
            arity = arities.get(upper)
            if arity is None or index + arity > len(tokens) or any(token.isalpha() for token in tokens[index : index + arity]):
                break
            values = [float(token) for token in tokens[index : index + arity]]
            index += arity
            relative = command.islower()
            if upper in {"M", "L", "T"}:
                point = (values[0] + current[0], values[1] + current[1]) if relative else (values[0], values[1])
                current = point
                points.append(point)
                if upper == "M":
                    start = point
                    command = "l" if relative else "L"
            elif upper == "H":
                current = (values[0] + current[0], current[1]) if relative else (values[0], current[1])
                points.append(current)
            elif upper == "V":
                current = (current[0], values[0] + current[1]) if relative else (current[0], values[0])
                points.append(current)
            elif upper in {"C", "S", "Q"}:
                pairs = list(zip(values[::2], values[1::2]))
                if relative:
                    pairs = [(x + current[0], y + current[1]) for x, y in pairs]
                points.extend(pairs)
                current = pairs[-1]
            elif upper == "A":
                endpoint = (values[5] + current[0], values[6] + current[1]) if relative else (values[5], values[6])
                points.append(endpoint)
                current = endpoint
        if not points:
            return None
        return _box((min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points) - min(x for x, _ in points), max(y for _, y in points) - min(y for _, y in points)))
    if name == "text":
        try:
            left = float(element.get("x", 0))
            baseline = float(element.get("y", 0))
            size = float(str(element.get("font-size", "0")).replace("px", ""))
        except (TypeError, ValueError):
            return None
        text = "".join(element.itertext())
        width = max(size * 0.5 * len(text), size * 0.5 if text else 0.0)
        return _box((left, baseline - size * 0.8, width, size))
    return None


def _transformed_box(box: Box, matrix: Matrix) -> Box:
    points = [_matrix_apply(matrix, point) for point in ((box[0], box[1]), (_right(box), box[1]), (_right(box), _bottom(box)), (box[0], _bottom(box)))]
    left = min(point[0] for point in points)
    top = min(point[1] for point in points)
    right = max(point[0] for point in points)
    bottom = max(point[1] for point in points)
    return left, top, right - left, bottom - top


def _svg_group_bbox(group: Any) -> Box | None:
    boxes: list[Box] = []

    def visit(element: Any, parent_matrix: Matrix) -> None:
        if _local_name(element.tag) == "defs":
            return
        matrix = _matrix_multiply(parent_matrix, _svg_transform(element.get("transform")))
        shape = _svg_shape_box(element)
        if shape is not None:
            boxes.append(_transformed_box(shape, matrix))
        for child in list(element):
            visit(child, matrix)

    visit(group, IDENTITY)
    return _union_bbox(boxes) if boxes else None


def _svg_object_bboxes(data: bytes) -> tuple[dict[str, Box], tuple[float, float, float, float] | None]:
    try:
        root = SafeET.fromstring(data)
    except Exception:
        return {}, None
    view_values = [float(item) for item in _SVG_NUMBER.findall(root.get("viewBox", ""))]
    if len(view_values) == 4 and view_values[2] and view_values[3]:
        view_box = tuple(view_values)  # type: ignore[assignment]
    else:
        try:
            width = float(str(root.get("width", "0")).replace("px", ""))
            height = float(str(root.get("height", "0")).replace("px", ""))
        except (TypeError, ValueError):
            return {}, None
        view_box = (0.0, 0.0, width, height)
    if view_box[2] == 0 or view_box[3] == 0:
        return {}, view_box
    result: dict[str, Box] = {}
    for element in root.iter():
        object_id = element.get("data-ooxml-id")
        if not object_id:
            continue
        bbox = _svg_group_bbox(element)
        if bbox is None:
            continue
        result[str(object_id)] = (
            (bbox[0] - view_box[0]) / view_box[2],
            (bbox[1] - view_box[1]) / view_box[3],
            bbox[2] / view_box[2],
            bbox[3] / view_box[3],
        )
    return result, view_box


def rendered_geometry_evidence(deck: DeckIR, slide_number: int, data: bytes) -> list[dict[str, Any]]:
    """Compare selected-slide SVG object boxes with native boxes.

    This function is intentionally separate from native extraction.  It is
    called only after an explicit Aurochs render and never affects baseline
    extraction or requires a renderer to be installed.
    """
    slide_id = f"slide-{slide_number:02d}"
    rendered, view_box = _svg_object_bboxes(data)
    native = {
        str(item.get("native_id")): item
        for item in deck.objects
        if item.get("slide_id") == slide_id
        and item.get("native_id") is not None
        and item.get("type") != "group"
    }
    facts: list[dict[str, Any]] = []
    mismatch_count = 0
    bbox_mismatch_count = 0
    missing_count = 0
    matched_count = 0
    missing_ids: list[str] = []
    for native_id in sorted(native):
        item = native[native_id]
        native_box = _box(item.get("bbox"))
        rendered_box = rendered.get(native_id)
        if native_box is None:
            continue
        if rendered_box is None:
            missing_ids.append(native_id)
            missing_count += 1
            mismatch_count += 1
            facts.append(
                _fact(
                    "native_rendered_geometry_mismatch",
                    slide_id,
                    {
                        "objects": [item["id"]],
                        "native_id": native_id,
                        "native_bbox": _box_list(native_box),
                        "rendered_bbox": None,
                        "delta": None,
                        "max_delta": None,
                        "reason": "missing_rendered_object",
                        "coordinate_space": "slide_normalized",
                    },
                    native_box,
                    object_id=item["id"],
                    source="aurochs_svg",
                )
            )
            continue
        matched_count += 1
        delta = [_round(abs(first - second)) for first, second in zip(native_box, rendered_box)]
        maximum = max(delta)
        if maximum <= GEOMETRY_MISMATCH_TOLERANCE:
            continue
        mismatch_count += 1
        bbox_mismatch_count += 1
        facts.append(
            _fact(
                "native_rendered_geometry_mismatch",
                slide_id,
                {
                    "objects": [item["id"]],
                    "native_id": native_id,
                    "native_bbox": _box_list(native_box),
                    "rendered_bbox": _box_list(rendered_box),
                    "delta": delta,
                    "max_delta": maximum,
                    "tolerance": GEOMETRY_MISMATCH_TOLERANCE,
                    "reason": "bbox_delta_exceeds_tolerance",
                    "coordinate_space": "slide_normalized",
                },
                _union_bbox((native_box, rendered_box)),
                object_id=item["id"],
                source="aurochs_svg",
            )
        )
    facts.append(
        _fact(
            "native_rendered_geometry_verification",
            slide_id,
            {
                "matched_object_count": matched_count,
                "rendered_object_count": len(rendered),
                "mismatch_count": mismatch_count,
                "bbox_mismatch_count": bbox_mismatch_count,
                "missing_rendered_object_count": missing_count,
                "coverage_ratio": _round(matched_count / len(native)) if native else 0.0,
                "missing_native_ids": missing_ids,
                "tolerance": GEOMETRY_MISMATCH_TOLERANCE,
                "view_box": [_round(value) for value in view_box] if view_box else None,
                "coordinate_space": "slide_normalized",
            },
            source="aurochs_svg",
            status=("unverified" if not native else "partial" if missing_count else "verified"),
            confidence=1.0 if matched_count and not missing_count else None,
        )
    )
    return _add_facts(deck, facts, prefix="rendered")


def _png_metadata(data: bytes) -> dict[str, Any] | None:
    if len(data) < 26 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    color_type = data[25]
    return {
        "format": "png",
        "image_size": [width, height],
        "aspect_ratio": _round(width / height) if height else None,
        "has_alpha": color_type in {4, 6},
        "decode_status": "header_verified",
    }


def _gif_metadata(data: bytes) -> dict[str, Any] | None:
    if len(data) < 10 or data[:6] not in {b"GIF87a", b"GIF89a"}:
        return None
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    return {
        "format": "gif",
        "image_size": [width, height],
        "aspect_ratio": _round(width / height) if height else None,
        "has_alpha": data[:6] == b"GIF89a" and b"\x21\xF9\x04" in data,
        "decode_status": "header_verified",
    }


def _jpeg_metadata(data: bytes) -> dict[str, Any] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    position = 2
    sof_markers = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
    while position + 3 < len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0xD8, 0xD9}:
            continue
        if position + 2 > len(data):
            break
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > len(data):
            break
        if marker in sof_markers and length >= 8:
            height = int.from_bytes(data[position + 3 : position + 5], "big")
            width = int.from_bytes(data[position + 5 : position + 7], "big")
            channels = data[position + 7]
            return {
                "format": "jpeg",
                "image_size": [width, height],
                "aspect_ratio": _round(width / height) if height else None,
                "has_alpha": False,
                "channels": channels,
                "decode_status": "header_verified",
            }
        position += length
    return None


def _webp_metadata(data: bytes) -> dict[str, Any] | None:
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    kind = data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        alpha = bool(data[20] & 0x10)
    elif kind == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = 1 + (bits & 0x3FFF)
        height = 1 + ((bits >> 14) & 0x3FFF)
        alpha = True
    elif kind == b"VP8" and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        alpha = False
    else:
        return None
    return {
        "format": "webp",
        "image_size": [width, height],
        "aspect_ratio": _round(width / height) if height else None,
        "has_alpha": alpha,
        "decode_status": "header_verified",
    }


def _bmp_metadata(data: bytes) -> dict[str, Any] | None:
    if len(data) < 30 or data[:2] != b"BM":
        return None
    width = int.from_bytes(data[18:22], "little", signed=True)
    height = abs(int.from_bytes(data[22:26], "little", signed=True))
    bits_per_pixel = int.from_bytes(data[28:30], "little")
    width = abs(width)
    return {
        "format": "bmp",
        "image_size": [width, height],
        "aspect_ratio": _round(width / height) if height else None,
        "has_alpha": bits_per_pixel == 32,
        "bits_per_pixel": bits_per_pixel,
        "decode_status": "header_verified",
    }


def _svg_metadata(data: bytes) -> dict[str, Any] | None:
    if b"<svg" not in data[:512].lower():
        return None
    try:
        root = SafeET.fromstring(data)
    except Exception:
        return None
    values = [float(item) for item in _SVG_NUMBER.findall(root.get("viewBox", ""))]
    if len(values) == 4 and values[2] and values[3]:
        width, height = values[2], values[3]
    else:
        try:
            width = float(str(root.get("width", "0")).replace("px", ""))
            height = float(str(root.get("height", "0")).replace("px", ""))
        except (TypeError, ValueError):
            return None
    return {
        "format": "svg",
        "image_size": [_round(width), _round(height)],
        "aspect_ratio": _round(width / height) if height else None,
        "has_alpha": None,
        "decode_status": "header_verified",
    }


def _image_metadata(data: bytes | None, content_type: str | None, asset_sha256: str | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "content_type": content_type,
        "asset_sha256": asset_sha256,
        "byte_length": len(data) if data is not None else None,
    }
    parsed = None
    if data is not None:
        for parser in (_png_metadata, _jpeg_metadata, _gif_metadata, _webp_metadata, _bmp_metadata, _svg_metadata):
            parsed = parser(data)
            if parsed is not None:
                break
    if parsed is None:
        metadata.update({"format": None, "image_size": None, "aspect_ratio": None, "has_alpha": None, "decode_status": "header_unavailable"})
    else:
        metadata.update(parsed)
    return metadata
