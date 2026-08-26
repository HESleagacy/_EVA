"""Benchmark metrics for comparing canonical DeckIR reports with annotations."""

from __future__ import annotations

from typing import Any


def _payload(report: Any) -> dict[str, Any]:
    if hasattr(report, "to_dict"):
        return report.to_dict()
    return report


def _normalise(value: str) -> str:
    return " ".join(value.split()).casefold()


def _recall(expected: list[Any], actual: list[Any]) -> float:
    if not expected:
        return 1.0
    remaining = [_normalise(str(item)) for item in actual]
    matched = 0
    for item in expected:
        value = _normalise(str(item))
        try:
            index = next(index for index, candidate in enumerate(remaining) if value in candidate)
        except StopIteration:
            continue
        remaining.pop(index)
        matched += 1
    return matched / len(expected)


def _intersection_over_union(first: list[float], second: list[float]) -> float:
    first_x, first_y, first_w, first_h = first
    second_x, second_y, second_w, second_h = second
    first_right, first_bottom = first_x + first_w, first_y + first_h
    second_right, second_bottom = second_x + second_w, second_y + second_h
    intersection_width = max(0.0, min(first_right, second_right) - max(first_x, second_x))
    intersection_height = max(0.0, min(first_bottom, second_bottom) - max(first_y, second_y))
    intersection = intersection_width * intersection_height
    union = first_w * first_h + second_w * second_h - intersection
    return intersection / union if union else 1.0 if first == second else 0.0


def _matching_object(expected: dict[str, Any], actual: list[dict[str, Any]]) -> dict[str, Any] | None:
    if expected.get("id"):
        return next((item for item in actual if item.get("id") == expected["id"]), None)
    candidates = [
        item
        for item in actual
        if item.get("slide_id") == expected.get("slide_id")
        and item.get("type") == expected.get("type")
    ]
    if expected.get("text"):
        expected_text = _normalise(str(expected["text"]))
        candidates = [item for item in candidates if expected_text in _normalise(item.get("text", ""))]
    return candidates[0] if candidates else None


def compute_metrics(report: Any, expected: dict[str, Any]) -> dict[str, float]:
    """Compute reproducible recall and resolution metrics.

    Expected annotations use ``text``, ``objects``, ``relationships``, and
    ``assets`` lists. Object annotations may identify a native object by ID or
    by ``slide_id``/``type``/``text`` and may include a normalized ``bbox``.
    """
    payload = _payload(report)
    actual_slides = payload.get("slides", [])
    actual_objects = payload.get("objects", [])
    actual_relationships = payload.get("relationships", [])
    actual_assets = payload.get("assets", [])

    actual_text = [text for slide in actual_slides for text in slide.get("text", [])]
    actual_text.extend(item.get("text", "") for item in actual_objects if item.get("text"))
    text_recall = _recall(expected.get("text", []), actual_text)

    expected_objects = expected.get("objects", [])
    matched_objects = [_matching_object(item, actual_objects) for item in expected_objects]
    object_recall = sum(item is not None for item in matched_objects) / len(expected_objects) if expected_objects else 1.0
    bbox_pairs = [
        (expected_item["bbox"], actual_item["bbox"])
        for expected_item, actual_item in zip(expected_objects, matched_objects)
        if actual_item is not None and "bbox" in expected_item
    ]
    bbox_accuracy = (
        sum(_intersection_over_union(expected_bbox, actual_bbox) for expected_bbox, actual_bbox in bbox_pairs)
        / len(bbox_pairs)
        if bbox_pairs
        else 1.0
    )

    expected_relationships = expected.get("relationships", [])
    actual_relationship_map = {item.get("id"): item for item in actual_relationships}
    if expected_relationships:
        resolved = sum(
            actual_relationship_map.get(item.get("id"), {}).get("resolved_target") is not None
            for item in expected_relationships
        )
        relationship_resolution = resolved / len(expected_relationships)
    else:
        relationship_resolution = (
            sum(item.get("resolved_target") is not None for item in actual_relationships) / len(actual_relationships)
            if actual_relationships
            else 1.0
        )

    expected_assets = expected.get("assets", [])
    actual_asset_ids = {item.get("id") for item in actual_assets}
    if expected_assets:
        asset_resolution = sum(
            item.get("id") in actual_asset_ids or item.get("part") in {asset.get("part") for asset in actual_assets}
            for item in expected_assets
        ) / len(expected_assets)
    else:
        referenced_assets = [item.get("asset_id") for item in actual_objects if item.get("asset_id")]
        asset_resolution = (
            sum(asset_id in actual_asset_ids for asset_id in referenced_assets) / len(referenced_assets)
            if referenced_assets
            else 1.0
        )

    return {
        "text_recall": text_recall,
        "object_recall": object_recall,
        "bounding_box_accuracy": bbox_accuracy,
        "relationship_resolution": relationship_resolution,
        "asset_resolution": asset_resolution,
    }
