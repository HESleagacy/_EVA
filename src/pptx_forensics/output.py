"""Compact semantic output and human-readable Markdown reporting.

The extraction pipeline needs more context than a downstream consumer does.
This module is the boundary between those two concerns: the extractor can keep
implementation details while the public report contains document semantics
only.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import PurePosixPath
import re
from typing import Any, Mapping


SEMANTIC_SCHEMA_VERSION = "1.0"

_DROP_KEYS = frozenset(
    {
        "adapter",
        "asset_images",
        "asset_sha256",
        "authority",
        "bbox_emu",
        "cache_hit",
        "cache_key",
        "content_hash",
        "dependencies",
        "engine",
        "engine_status",
        "engine_version",
        "estimated_cost_usd",
        "evidence_grounding",
        "evidence_reconciliation",
        "evidence_refs",
        "failure_class",
        "failure_classes",
        "geometry",
        "image_hashes",
        "inherited_from",
        "max_concurrency",
        "max_output_tokens",
        "metadata",
        "missing_evidence",
        "model",
        "model_status",
        "native_id",
        "native_text_used",
        "package_part_count",
        "package_parts",
        "paragraph",
        "part",
        "prompt_version",
        "raw_style",
        "request_seconds",
        "response_sha256",
        "resolved_style",
        "sanitization",
        "selection_reasons",
        "sha256",
        "source_part",
        "source_sha256",
        "target_mode",
        "thinking_budget",
        "token_count",
        "transform_chain",
        "transform_source",
        "usage",
        "words",
        "xml_path",
        "xml_part",
    }
)
_DROP_TYPES = {
    "raster_arrow_detection",
    "raster_box_detection",
    "raster_contour_detection",
    "raster_line_detection",
}
_DROP_SUFFIXES = ("_emu", "_sha256")
_SLIDE_PART = re.compile(r"(?:^|/)slide(\d+)\.xml$")


def _status(value: Any) -> str:
    """Normalize pipeline states to the three public semantic states."""
    if isinstance(value, str) and value in {"verified", "extracted", "ok"}:
        return "verified"
    if isinstance(value, str) and value in {"failed", "error", "unsupported"}:
        return "failed"
    return "uncertain"


def _confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 1:
        return None
    return round(float(value), 6)


def _bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        result = [round(float(item), 12) for item in value]
    except (TypeError, ValueError):
        return None
    return result


def _source_label(value: Any) -> str | None:
    if isinstance(value, str):
        layer = value
    elif isinstance(value, Mapping):
        layer = str(value.get("layer") or value.get("source") or "")
    else:
        return None
    return {
        "native_ooxml": "native",
        "native": "native",
        "rendered_cv": "derived",
        "ocr": "ocr",
        "vision_model": "vision",
        "vision": "vision",
    }.get(layer, layer or None)


def _drop_key(key: str) -> bool:
    lowered = key.casefold()
    return (
        key in _DROP_KEYS
        or lowered.endswith(_DROP_SUFFIXES)
        or lowered in {"flow_candidate", "geometry_emu", "slide_size_emu"}
    )


def _clean(value: Any, *, keep_confidence: bool = False) -> Any:
    """Remove debug-only fields from a nested semantic value."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if _drop_key(key):
                continue
            if key == "confidence" and not keep_confidence:
                continue
            if key == "source":
                label = _source_label(raw_value)
                if label is not None:
                    result[key] = label
                continue
            if key == "status":
                result["semantic_status"] = _status(raw_value)
                continue
            if key == "semantic_status":
                result[key] = _status(raw_value)
                continue
            result[key] = _clean(raw_value, keep_confidence=False)
        return result
    if isinstance(value, list):
        return [_clean(item, keep_confidence=False) for item in value]
    if isinstance(value, tuple):
        return [_clean(item, keep_confidence=False) for item in value]
    return value


def _object_style(item: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only style properties that help a reader understand the object."""
    direct = item.get("style") if isinstance(item.get("style"), Mapping) else {}
    resolved = item.get("resolved_style") if isinstance(item.get("resolved_style"), Mapping) else {}
    style: dict[str, Any] = {}
    for key in ("placeholder_type", "rotation_degrees"):
        value = direct.get(key, resolved.get(key))
        if value is not None and value != "":
            style[key] = value
    for key in (
        "font_size_pt",
        "font_family",
        "bold",
        "italic",
        "underline",
        "strike",
    ):
        value = resolved.get(key)
        if value is not None:
            style[key] = value

    def color(value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        compact = {}
        if value.get("color"):
            compact["color"] = value["color"]
        if value.get("opacity") is not None:
            compact["opacity"] = value["opacity"]
        return compact or None

    font_color = color(resolved.get("font_color"))
    if font_color:
        style["font_color"] = font_color
    fill = color(resolved.get("fill"))
    if fill:
        style["fill"] = fill
    return style


def _compact_lines(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    lines: list[dict[str, Any]] = []
    for line in value:
        if not isinstance(line, Mapping):
            continue
        item: dict[str, Any] = {"text": str(line.get("text") or "")}
        box = _bbox(line.get("bbox"))
        if box is not None:
            item["bbox"] = box
        lines.append(item)
    return lines


def _compact_object_data(item: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("table_data", "chart_data", "smartart_data"):
        value = item.get(key)
        if isinstance(value, Mapping):
            result[key] = _clean(value)
    embedded = item.get("embedded_object")
    if isinstance(embedded, Mapping):
        result["embedded_object"] = {
            key: _clean(embedded.get(key))
            for key in ("prog_id", "show_as_icon", "preview_asset_id")
            if embedded.get(key) is not None
        }
    return result


def _image_roles(deck: Any) -> tuple[dict[str, str], dict[str, str]]:
    by_asset: dict[str, str] = {}
    by_object: dict[str, str] = {}
    for record in getattr(deck, "rendered_evidence", []):
        value = record.get("value") if isinstance(record, Mapping) else None
        if not isinstance(value, Mapping) or value.get("type") != "image_role_candidate":
            continue
        role = value.get("image_role") or value.get("role")
        if not isinstance(role, str) or not role:
            continue
        asset_id = value.get("asset_id")
        object_id = value.get("object") or record.get("object_id")
        if isinstance(asset_id, str):
            by_asset.setdefault(asset_id, role)
        if isinstance(object_id, str):
            by_object.setdefault(object_id, role)
    return by_asset, by_object


def _image_details(deck: Any) -> dict[str, dict[str, Any]]:
    details_by_asset: dict[str, dict[str, Any]] = {}
    meaningful = (
        "format",
        "image_size",
        "aspect_ratio",
        "has_alpha",
        "channels",
        "crop",
        "rotation_degrees",
        "decode_status",
    )
    for record in getattr(deck, "rendered_evidence", []):
        value = record.get("value") if isinstance(record, Mapping) else None
        if not isinstance(value, Mapping) or value.get("type") != "image_asset_analysis":
            continue
        asset_id = value.get("asset_id")
        if not isinstance(asset_id, str):
            continue
        details = {
            key: _clean(value[key])
            for key in meaningful
            if value.get(key) is not None
        }
        if details:
            details_by_asset.setdefault(asset_id, details)
    return details_by_asset


def _semantic_object(item: Mapping[str, Any], roles_by_object: Mapping[str, str]) -> dict[str, Any]:
    object_id = item.get("id")
    result: dict[str, Any] = {
        "id": object_id,
        "slide_id": item.get("slide_id"),
        "parent_id": item.get("parent_id"),
        "type": item.get("type", "unknown"),
        "shape_type": item.get("shape_type"),
        "bbox": _bbox(item.get("bbox")),
        "z_order": item.get("z_order"),
        "text": str(item.get("text") or ""),
        "style": _object_style(item),
        "semantic_status": _status(item.get("semantic_status")),
        "relationships": [str(value) for value in item.get("relationships", []) if value],
        "source": "native",
        "confidence": 1.0,
    }
    if item.get("name"):
        result["name"] = str(item["name"])
    if item.get("asset_id"):
        result["asset_id"] = str(item["asset_id"])
    if isinstance(object_id, str) and object_id in roles_by_object:
        result["image_role"] = roles_by_object[object_id]
    result.update(_compact_object_data(item))
    return result


def _semantic_slide(
    slide: Mapping[str, Any],
    objects: list[Mapping[str, Any]],
    diagrams: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, Any]:
    slide_id = str(slide.get("id") or "")
    result: dict[str, Any] = {
        "id": slide_id,
        "number": slide.get("number"),
        "text": [str(value) for value in slide.get("text", [])],
        "notes": [str(value) for value in slide.get("notes", [])],
        "object_ids": [str(item.get("id")) for item in objects if item.get("id")],
        "reading_order": slide.get("slide_reading_order", "unknown"),
        "source": "native",
    }
    links = []
    for link in slide.get("hyperlinks", []):
        if not isinstance(link, Mapping):
            continue
        target = link.get("resolved_target") or link.get("target")
        links.append(
            {
                key: value
                for key, value in {
                    "kind": link.get("kind"),
                    "target": target,
                    "action": link.get("action"),
                }.items()
                if value not in (None, "")
            }
        )
    if links:
        result["links"] = links
    alt_text = []
    for item in slide.get("alt_text", []):
        if not isinstance(item, Mapping):
            continue
        compact = {
            key: str(item[key])
            for key in ("name", "descr", "title")
            if item.get(key)
        }
        if compact:
            alt_text.append(compact)
    if alt_text:
        result["alt_text"] = alt_text
    animation_elements = [
        str(item.get("element"))
        for item in slide.get("animations", [])
        if isinstance(item, Mapping) and item.get("element")
    ]
    if animation_elements:
        result["animations"] = animation_elements

    graph_values = diagrams.get(slide_id, [])
    if graph_values:
        present = any(item.get("flow", {}).get("present") is True for item in graph_values)
        directions = [
            item.get("flow", {}).get("direction")
            for item in graph_values
            if item.get("flow", {}).get("direction") not in {None, "none", "unknown"}
        ]
        result["flow"] = {
            "present": present,
            "direction": directions[0] if directions else "unknown" if present else "none",
        }
    else:
        result["flow"] = {"present": False, "direction": "none"}
    return result


def _relationship_source(part: Any) -> str:
    value = str(part or "")
    match = _SLIDE_PART.search(value)
    if match:
        return f"slide-{int(match.group(1)):02d}"
    if value.endswith("presentation.xml"):
        return "presentation"
    if not value:
        return "package"
    return PurePosixPath(value).stem or "package"


def _relationship_type(value: Any) -> str:
    text = str(value or "")
    return text.rstrip("/").rsplit("/", 1)[-1] or "relationship"


def _semantic_relationship(item: Any) -> dict[str, Any]:
    source_part = getattr(item, "source_part", None)
    relationship_id = getattr(item, "relationship_id", None)
    relationship_type = getattr(item, "relationship_type", None)
    target = getattr(item, "target", None)
    resolved_target = getattr(item, "resolved_target", None)
    if isinstance(item, Mapping):
        source_part = item.get("source_part", item.get("source"))
        relationship_id = item.get("id") or item.get("relationship_id")
        relationship_type = item.get("relationship_type", item.get("type"))
        target = item.get("target")
        resolved_target = item.get("resolved_target")
    if relationship_id and source_part and ":" not in str(relationship_id):
        relationship_id = f"{source_part}:{relationship_id}"
    return {
        "id": relationship_id,
        "source": _relationship_source(source_part),
        "type": _relationship_type(relationship_type),
        "target": resolved_target or target,
    }


def _semantic_asset(
    item: Mapping[str, Any],
    roles_by_asset: Mapping[str, str],
    details_by_asset: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    asset_id = str(item.get("id") or "")
    result: dict[str, Any] = {
        "id": asset_id,
        "type": item.get("type", "asset"),
        "content_type": item.get("content_type"),
        "source": "native",
        "confidence": 1.0,
    }
    part = str(item.get("part") or "")
    if part:
        result["name"] = PurePosixPath(part).name
    if asset_id in roles_by_asset:
        result["image_role"] = roles_by_asset[asset_id]
    if asset_id in details_by_asset:
        result["image"] = dict(details_by_asset[asset_id])
    return result


def _node_status(node: Mapping[str, Any]) -> str:
    return _status(node.get("semantic_status", node.get("status")))


def _diagram_direction(source: Mapping[str, Any] | None, target: Mapping[str, Any] | None) -> str:
    first, second = _bbox(source.get("bbox")) if source else None, _bbox(target.get("bbox")) if target else None
    if first is None or second is None:
        return "unknown"
    first_center = (first[0] + first[2] / 2, first[1] + first[3] / 2)
    second_center = (second[0] + second[2] / 2, second[1] + second[3] / 2)
    if abs(first_center[0] - second_center[0]) >= abs(first_center[1] - second_center[1]):
        return "left_to_right" if first_center[0] < second_center[0] else "right_to_left"
    return "top_to_bottom" if first_center[1] < second_center[1] else "bottom_to_top"


def _arrowheads(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for side in ("start", "end"):
        arrow = value.get(side)
        if isinstance(arrow, Mapping) and arrow.get("type"):
            result[side] = {"type": arrow["type"]}
    return result or None


def _semantic_diagram(record: Mapping[str, Any]) -> dict[str, Any] | None:
    value = record.get("value") if isinstance(record.get("value"), Mapping) else {}
    if value.get("type") != "diagram_graph":
        return None
    raw_nodes = [item for item in value.get("nodes", []) if isinstance(item, Mapping)]
    nodes: list[dict[str, Any]] = []
    for node in raw_nodes:
        item: dict[str, Any] = {
            "id": node.get("id"),
            "bbox": _bbox(node.get("bbox")),
            "text": str(node.get("text") or node.get("label") or ""),
            "semantic_status": _node_status(node),
        }
        for key in ("object_id", "type", "group_path", "coordinate_space"):
            if node.get(key) not in (None, [], ""):
                item[key] = _clean(node[key])
        nodes.append(item)
    node_by_id = {str(node.get("id")): node for node in nodes if node.get("id")}
    edges: list[dict[str, Any]] = []
    for edge in value.get("edges", []):
        if not isinstance(edge, Mapping):
            continue
        source = edge.get("source")
        target = edge.get("target")
        item: dict[str, Any] = {
            "id": edge.get("id"),
            "source": source,
            "target": target,
            "direction": edge.get("direction") or _diagram_direction(
                node_by_id.get(str(source)), node_by_id.get(str(target))
            ),
            "semantic_status": _node_status(edge),
        }
        if edge.get("label"):
            item["label"] = str(edge["label"])
        arrows = _arrowheads(edge.get("arrowheads"))
        if arrows:
            item["arrowheads"] = arrows
        edges.append(item)
    groups: list[dict[str, Any]] = []
    for group in value.get("groups", []):
        if not isinstance(group, Mapping):
            continue
        groups.append(
            {
                key: _clean(group[key])
                for key in ("id", "parent_id", "children")
                if group.get(key) not in (None, [])
            }
            | {"semantic_status": _node_status(group)}
        )
    original_present = value.get("flow_present")
    present = bool(original_present) if original_present is not None else bool(edges)
    direction = value.get("diagram_flow_direction")
    if direction in {None, "", "unknown"}:
        directions = [edge["direction"] for edge in edges if edge.get("direction") not in {None, "unknown"}]
        direction = directions[0] if directions else "unknown" if present else "none"
    branch = value.get("branch", "native")
    if branch == "raster":
        diagram_id = f"diagram-{record.get('slide_id')}-{value.get('asset_id')}"
    else:
        diagram_id = f"diagram-{record.get('slide_id')}"
    result: dict[str, Any] = {
        "id": diagram_id,
        "slide_id": record.get("slide_id"),
        "object_id": record.get("object_id"),
        "bbox": _bbox(record.get("bbox")),
        "type": "diagram",
        "branch": branch,
        "nodes": nodes,
        "edges": edges,
        "flow": {"present": present, "direction": direction},
        "semantic_status": _status(record.get("status", value.get("status"))),
        "source": "raster" if value.get("branch") == "raster" else "native",
    }
    if value.get("asset_id"):
        result["asset_id"] = value["asset_id"]
    if groups:
        result["groups"] = groups
    confidence = _confidence(record.get("confidence"))
    if confidence is not None:
        result["confidence"] = confidence
    return result


def _semantic_ocr(record: Mapping[str, Any]) -> dict[str, Any] | None:
    value = record.get("value") if isinstance(record.get("value"), Mapping) else {}
    asset_id = value.get("asset_id")
    if not asset_id:
        return None
    result: dict[str, Any] = {
        "id": record.get("id"),
        "slide_id": record.get("slide_id"),
        "object_id": record.get("object_id"),
        "asset_id": asset_id,
        "bbox": _bbox(record.get("bbox")),
        "text": str(value.get("text") or ""),
        "semantic_status": _status(record.get("status", value.get("status"))),
        "source": "ocr",
    }
    lines = _compact_lines(value.get("lines"))
    if lines:
        result["lines"] = lines
    image_size = value.get("image_size")
    if isinstance(image_size, list) and len(image_size) == 2:
        result["image_size"] = image_size
    if value.get("error"):
        result["error"] = str(value["error"])
    confidence = _confidence(record.get("confidence"))
    if confidence is not None:
        result["confidence"] = confidence
    return result


def _semantic_vision(record: Mapping[str, Any]) -> dict[str, Any] | None:
    value = record.get("value") if isinstance(record.get("value"), Mapping) else {}
    analysis = value.get("analysis") if isinstance(value.get("analysis"), Mapping) else {}
    result: dict[str, Any] = {
        "id": record.get("id"),
        "slide_id": record.get("slide_id"),
        "target": value.get("target"),
        "bbox": _bbox(record.get("bbox")),
        "semantic_status": _status(record.get("status", value.get("status"))),
        "source": "vision",
    }
    for key in ("summary", "image_role", "slide_reading_order"):
        if analysis.get(key) not in (None, ""):
            result[key] = analysis[key]
    present = analysis.get("flow_present")
    direction = analysis.get("diagram_flow_direction", "unknown")
    if present is not None or direction not in {None, "", "unknown"}:
        result["flow"] = {
            "present": bool(present) if present is not None else False,
            "direction": direction if direction not in {None, ""} else "unknown",
        }
    nodes = []
    for node in analysis.get("nodes", []):
        if not isinstance(node, Mapping):
            continue
        nodes.append(
            {
                "id": node.get("id"),
                "label": str(node.get("label") or ""),
                "bbox": _bbox(node.get("bbox")),
                "semantic_status": _node_status(node),
            }
        )
    if nodes:
        result["nodes"] = nodes
    edges = []
    for edge in analysis.get("edges", []):
        if not isinstance(edge, Mapping):
            continue
        item = {
            "source": edge.get("source"),
            "target": edge.get("target"),
            "direction": edge.get("direction", "unknown"),
            "semantic_status": _node_status(edge),
        }
        if edge.get("label"):
            item["label"] = str(edge["label"])
        edges.append(item)
    if edges:
        result["edges"] = edges
    observations = []
    for observation in analysis.get("observations", []):
        if not isinstance(observation, Mapping):
            continue
        observations.append(
            {
                key: _clean(observation[key])
                for key in ("type", "objects", "description")
                if observation.get(key) not in (None, "")
            }
        )
    if observations:
        result["observations"] = observations
    if value.get("error"):
        result["error"] = str(value["error"])
    confidence = _confidence(record.get("confidence"))
    if confidence is not None:
        result["confidence"] = confidence
    return result


def _semantic_region(record: Mapping[str, Any]) -> dict[str, Any] | None:
    value = record.get("value") if isinstance(record.get("value"), Mapping) else {}
    if not value:
        return None
    result = {
        "id": record.get("id"),
        "slide_id": record.get("slide_id"),
        "bbox": _bbox(record.get("bbox")),
        "region_kind": value.get("region_kind", "content"),
        "semantic_status": _status(record.get("status", value.get("status"))),
        "source": "layout",
    }
    confidence = _confidence(record.get("confidence"))
    if confidence is not None:
        result["confidence"] = confidence
    return result


def _semantic_comments(report: Any) -> list[dict[str, str]]:
    comments: list[dict[str, str]] = []
    for comment in getattr(report, "comments", []):
        if not isinstance(comment, Mapping) or not comment.get("text"):
            continue
        item = {"text": str(comment["text"])}
        if comment.get("author_id") not in (None, ""):
            item["author_id"] = str(comment["author_id"])
        comments.append(item)
    return comments


def semantic_dict(value: Any) -> dict[str, Any]:
    """Return the compact, consumer-facing representation of a DeckIR object."""
    report = value if getattr(value, "canonical", None) is not None else None
    deck = report.canonical if report is not None else value
    objects = [item for item in getattr(deck, "objects", []) if isinstance(item, Mapping)]
    roles_by_asset, roles_by_object = _image_roles(deck)
    details_by_asset = _image_details(deck)
    semantic_diagrams = []
    for record in getattr(deck, "rendered_evidence", []):
        if not isinstance(record, Mapping):
            continue
        value = record.get("value") if isinstance(record.get("value"), Mapping) else {}
        if value.get("type") in _DROP_TYPES:
            continue
        diagram = _semantic_diagram(record)
        if diagram is not None:
            semantic_diagrams.append(diagram)
    semantic_diagrams.sort(key=lambda item: (str(item.get("slide_id")), str(item.get("id"))))
    diagrams_by_slide: dict[str, list[Mapping[str, Any]]] = {}
    for diagram in semantic_diagrams:
        diagrams_by_slide.setdefault(str(diagram.get("slide_id")), []).append(diagram)

    slide_objects: dict[str, list[Mapping[str, Any]]] = {}
    for item in objects:
        slide_id = item.get("slide_id")
        if slide_id:
            slide_objects.setdefault(str(slide_id), []).append(item)
    slides = [
        _semantic_slide(slide, slide_objects.get(str(slide.get("id")), []), diagrams_by_slide)
        for slide in sorted(getattr(deck, "slides", []), key=lambda item: (item.get("number", 0), item.get("id", "")))
        if isinstance(slide, Mapping)
    ]

    semantic_ocr = [
        item
        for record in getattr(deck, "ocr_evidence", [])
        if isinstance(record, Mapping)
        if (item := _semantic_ocr(record)) is not None
    ]
    semantic_vision = [
        item
        for record in getattr(deck, "vision_evidence", [])
        if isinstance(record, Mapping)
        if (item := _semantic_vision(record)) is not None
    ]
    semantic_regions = [
        item
        for record in getattr(deck, "visual_regions", [])
        if isinstance(record, Mapping)
        if (item := _semantic_region(record)) is not None
    ]
    semantic_objects = [_semantic_object(item, roles_by_object) for item in objects]
    semantic_assets = [
        _semantic_asset(item, roles_by_asset, details_by_asset)
        for item in getattr(deck, "assets", [])
        if isinstance(item, Mapping)
    ]
    semantic_relationships = [_semantic_relationship(item) for item in getattr(deck, "relationships", [])]
    type_counts = Counter(str(item.get("type", "unknown")) for item in semantic_objects)
    payload = {
        "schema_version": SEMANTIC_SCHEMA_VERSION,
        "deck": {
            "id": "deck",
            "name": "Presentation",
            "source": "presentation",
            "slide_count": len(slides),
            "slide_aspect_ratio": getattr(deck, "deck", {}).get("slide_aspect_ratio"),
        },
        "summary": {
            "slides": len(slides),
            "objects": len(semantic_objects),
            "object_types": {key: type_counts[key] for key in sorted(type_counts)},
            "assets": len(semantic_assets),
            "relationships": len(semantic_relationships),
            "diagrams": len(semantic_diagrams),
            "ocr_records": len(semantic_ocr),
            "vision_records": len(semantic_vision),
        },
        "slides": slides,
        "objects": semantic_objects,
        "assets": semantic_assets,
        "relationships": semantic_relationships,
        "visual_regions": semantic_regions,
        "diagrams": semantic_diagrams,
        "ocr": semantic_ocr,
        "vision": semantic_vision,
        "warnings": [str(value) for value in getattr(deck, "warnings", [])],
    }
    comments = _semantic_comments(report) if report is not None else []
    payload["comments"] = comments
    return payload


def _inline(value: Any) -> str:
    text = str(value if value is not None else "none")
    return text.replace("`", "'").replace("|", "\\|").replace("\n", " ").strip() or "(empty)"


def _json_inline(value: Any) -> str:
    return _inline(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _text_block(lines: list[str], label: str, text: Any) -> None:
    lines.append(f"**{label}**")
    lines.append("")
    lines.append("```text")
    content = str(text or "")
    lines.extend((content or "(empty)").replace("```", "''' ").splitlines())
    lines.append("```")


def _table(lines: list[str], rows: Any) -> None:
    if not isinstance(rows, list) or not rows:
        lines.append("(empty table)")
        return
    width = max((len(row) for row in rows if isinstance(row, list)), default=0)
    if not width:
        lines.append("(empty table)")
        return
    lines.append("| " + " | ".join(f"Column {index}" for index in range(1, width + 1)) + " |")
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    for row in rows:
        values = row if isinstance(row, list) else [row]
        values = [*values, *([""] * (width - len(values)))]
        lines.append("| " + " | ".join(_inline(value) for value in values[:width]) + " |")


def _append_object_details(lines: list[str], item: Mapping[str, Any], ocr_by_asset: Mapping[str, Mapping[str, Any]]) -> None:
    lines.append(f"##### {_inline(item.get('name') or item.get('id'))}")
    lines.append(f"- ID: `{_inline(item.get('id'))}`")
    lines.append(f"- Type: `{_inline(item.get('type'))}`" + (f" (`{_inline(item.get('shape_type'))}`)" if item.get("shape_type") else ""))
    lines.append(f"- Bounding box: `{_json_inline(item.get('bbox'))}` in normalized slide coordinates")
    lines.append(f"- Semantic status: `{_inline(item.get('semantic_status'))}`")
    if item.get("confidence") is not None:
        lines.append(f"- Confidence: `{_inline(item.get('confidence'))}`")
    if item.get("parent_id"):
        lines.append(f"- Parent object: `{_inline(item.get('parent_id'))}`")
    if item.get("asset_id"):
        lines.append(f"- Asset: `{_inline(item.get('asset_id'))}`")
    if item.get("image_role"):
        lines.append(f"- Image role: `{_inline(item.get('image_role'))}`")
    _text_block(lines, "Text", item.get("text"))
    if item.get("style"):
        lines.append(f"- Meaningful style: `{_json_inline(item['style'])}`")
    if item.get("relationships"):
        lines.append("- Relationships: " + ", ".join(f"`{_inline(value)}`" for value in item["relationships"]))
    table_data = item.get("table_data")
    if isinstance(table_data, Mapping):
        lines.append("**Table contents**")
        lines.append("")
        _table(lines, table_data.get("rows"))
    chart_data = item.get("chart_data")
    if isinstance(chart_data, Mapping):
        lines.append("**Chart contents**")
        lines.append("")
        for key in ("type", "title", "legend"):
            if chart_data.get(key):
                lines.append(f"- {key.replace('_', ' ').title()}: {_inline(chart_data[key])}")
        for axis in chart_data.get("axes", []):
            if isinstance(axis, Mapping):
                lines.append(f"- Axis `{_inline(axis.get('type'))}`: {_inline(axis.get('title') or '(untitled)')}")
        for series in chart_data.get("series", []):
            if not isinstance(series, Mapping):
                continue
            lines.append(f"- Series `{_inline(series.get('title') or '(untitled)')}`:")
            categories = [item.get("value") for item in series.get("categories", []) if isinstance(item, Mapping)]
            values = [item.get("value") for item in series.get("values", []) if isinstance(item, Mapping)]
            lines.append(f"  - Categories: `{_json_inline(categories)}`")
            lines.append(f"  - Values: `{_json_inline(values)}`")
    smartart_data = item.get("smartart_data")
    if isinstance(smartart_data, Mapping):
        lines.append("**SmartArt contents**")
        lines.append("")
        for node in smartart_data.get("nodes", []):
            if isinstance(node, Mapping):
                lines.append(f"- Node `{_inline(node.get('id'))}`: {_inline(node.get('text'))}")
        for connection in smartart_data.get("connections", []):
            if isinstance(connection, Mapping):
                lines.append(f"- Connection: `{_inline(connection.get('source'))}` -> `{_inline(connection.get('target'))}`")
    embedded = item.get("embedded_object")
    if embedded:
        lines.append(f"- Embedded object details: `{_json_inline(embedded)}`")
    ocr = ocr_by_asset.get(str(item.get("asset_id")))
    if ocr and ocr.get("text"):
        lines.append("**OCR text for this image**")
        lines.append("")
        lines.append("```text")
        lines.extend(str(ocr["text"]).replace("```", "''' ").splitlines())
        lines.append("```")
        if ocr.get("confidence") is not None:
            lines.append(f"OCR confidence: `{_inline(ocr['confidence'])}`")


def _append_diagram(lines: list[str], diagram: Mapping[str, Any]) -> None:
    lines.append(f"#### Diagram `{_inline(diagram.get('id'))}`")
    lines.append(f"- Slide: `{_inline(diagram.get('slide_id'))}`")
    if diagram.get("asset_id"):
        lines.append(f"- Asset: `{_inline(diagram.get('asset_id'))}`")
    lines.append(f"- Branch: `{_inline(diagram.get('branch'))}`")
    lines.append(f"- Bounding box: `{_json_inline(diagram.get('bbox'))}` in normalized slide coordinates")
    lines.append(f"- Semantic status: `{_inline(diagram.get('semantic_status'))}`")
    if diagram.get("confidence") is not None:
        lines.append(f"- Confidence: `{_inline(diagram.get('confidence'))}`")
    flow = diagram.get("flow", {})
    lines.append(f"- Flow: `{_inline(flow.get('direction'))}`, present=`{_inline(flow.get('present'))}`")
    lines.append("**Nodes**")
    lines.append("")
    for node in diagram.get("nodes", []):
        if not isinstance(node, Mapping):
            continue
        lines.append(
            f"- `{_inline(node.get('id'))}`: {_inline(node.get('text'))}; "
            f"bbox=`{_json_inline(node.get('bbox'))}`; status=`{_inline(node.get('semantic_status'))}`"
        )
    if not diagram.get("nodes"):
        lines.append("(none)")
    lines.append("**Edges**")
    lines.append("")
    for edge in diagram.get("edges", []):
        if not isinstance(edge, Mapping):
            continue
        label = f"; label={_inline(edge['label'])}" if edge.get("label") else ""
        lines.append(
            f"- `{_inline(edge.get('source'))}` -> `{_inline(edge.get('target'))}`; "
            f"direction=`{_inline(edge.get('direction'))}`; status=`{_inline(edge.get('semantic_status'))}`{label}"
        )
    if not diagram.get("edges"):
        lines.append("(none)")


def render_markdown(deck: Any) -> str:
    """Render a semantic DeckIR payload as a descriptive Markdown document."""
    payload = semantic_dict(deck)
    lines = [
        "# Parsed Presentation",
        "",
        "This document lists the content and structure parsed from the presentation.",
        "",
        "## Parsed Summary",
        "",
    ]
    summary = payload["summary"]
    lines.extend(
        [
            f"- Slides parsed: `{summary['slides']}`",
            f"- Objects parsed: `{summary['objects']}`",
            f"- Assets parsed: `{summary['assets']}`",
            f"- Relationships parsed: `{summary['relationships']}`",
            f"- Diagrams parsed: `{summary['diagrams']}`",
            f"- OCR records: `{summary['ocr_records']}`",
            f"- Vision descriptions: `{summary['vision_records']}`",
            f"- Object types: `{_json_inline(summary['object_types'])}`",
        ]
    )
    if payload["deck"].get("slide_aspect_ratio") is not None:
        lines.append(f"- Slide aspect ratio: `{_inline(payload['deck']['slide_aspect_ratio'])}`")

    objects_by_slide: dict[str, list[Mapping[str, Any]]] = {}
    for item in payload["objects"]:
        objects_by_slide.setdefault(str(item.get("slide_id")), []).append(item)
    ocr_by_asset = {str(item.get("asset_id")): item for item in payload["ocr"] if item.get("asset_id")}
    diagrams_by_slide: dict[str, list[Mapping[str, Any]]] = {}
    for item in payload["diagrams"]:
        diagrams_by_slide.setdefault(str(item.get("slide_id")), []).append(item)
    vision_by_slide: dict[str, list[Mapping[str, Any]]] = {}
    for item in payload["vision"]:
        vision_by_slide.setdefault(str(item.get("slide_id")), []).append(item)

    lines.extend(["", "## Slides", ""])
    for slide in payload["slides"]:
        lines.append(f"### Slide {slide.get('number')}: `{_inline(slide.get('id'))}`")
        lines.append(f"- Reading order: `{_inline(slide.get('reading_order'))}`")
        flow = slide.get("flow", {})
        lines.append(f"- Flow: `{_inline(flow.get('direction'))}`, present=`{_inline(flow.get('present'))}`")
        if slide.get("notes"):
            _text_block(lines, "Presenter notes", "\n".join(slide["notes"]))
        if slide.get("text"):
            _text_block(lines, "Text parsed directly from the slide", "\n".join(slide["text"]))
        if slide.get("links"):
            lines.append("**Links**")
            lines.append("")
            for link in slide["links"]:
                lines.append(f"- `{_inline(link.get('kind'))}` -> {_inline(link.get('target') or link.get('action'))}")
        if slide.get("alt_text"):
            lines.append("**Alt text**")
            lines.append("")
            for item in slide["alt_text"]:
                lines.append(f"- {_json_inline(item)}")
        if slide.get("animations"):
            lines.append("- Animation elements parsed: " + ", ".join(f"`{_inline(value)}`" for value in slide["animations"]))
        lines.append("")
        lines.append("#### Slide objects")
        lines.append("")
        slide_items = objects_by_slide.get(str(slide.get("id")), [])
        if slide_items:
            for item in slide_items:
                _append_object_details(lines, item, ocr_by_asset)
                lines.append("")
        else:
            lines.append("(none)")
        for diagram in diagrams_by_slide.get(str(slide.get("id")), []):
            _append_diagram(lines, diagram)
            lines.append("")
        for vision in vision_by_slide.get(str(slide.get("id")), []):
            lines.append(f"#### Vision description `{_inline(vision.get('id'))}`")
            lines.append(f"- Semantic status: `{_inline(vision.get('semantic_status'))}`")
            if vision.get("confidence") is not None:
                lines.append(f"- Confidence: `{_inline(vision.get('confidence'))}`")
            if vision.get("image_role"):
                lines.append(f"- Image role: `{_inline(vision.get('image_role'))}`")
            if vision.get("slide_reading_order"):
                lines.append(f"- Reading order: `{_inline(vision.get('slide_reading_order'))}`")
            if vision.get("flow"):
                lines.append(
                    f"- Flow: `{_inline(vision['flow'].get('direction'))}`, "
                    f"present=`{_inline(vision['flow'].get('present'))}`"
                )
            if vision.get("summary"):
                _text_block(lines, "Description", vision["summary"])
            if vision.get("nodes"):
                lines.append("**Vision nodes**")
                lines.append("")
                for node in vision["nodes"]:
                    lines.append(
                        f"- `{_inline(node.get('id'))}`: {_inline(node.get('label'))}; "
                        f"bbox=`{_json_inline(node.get('bbox'))}`; status=`{_inline(node.get('semantic_status'))}`"
                    )
            if vision.get("edges"):
                lines.append("**Vision edges**")
                lines.append("")
                for edge in vision["edges"]:
                    label = f"; label={_inline(edge['label'])}" if edge.get("label") else ""
                    lines.append(
                        f"- `{_inline(edge.get('source'))}` -> `{_inline(edge.get('target'))}`; "
                        f"direction=`{_inline(edge.get('direction'))}`; status=`{_inline(edge.get('semantic_status'))}`{label}"
                    )
            if vision.get("observations"):
                lines.append("**Observed details**")
                lines.append("")
                for observation in vision["observations"]:
                    lines.append(f"- {_json_inline(observation)}")
            lines.append("")

    lines.extend(["## Assets", ""])
    if payload["assets"]:
        lines.extend(
            [
                "| ID | Type | Name | Content type | Image role | Image details |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for asset in payload["assets"]:
            lines.append(
                "| "
                + " | ".join(
                    _inline(asset.get(key))
                    for key in ("id", "type", "name", "content_type", "image_role", "image")
                )
                + " |"
            )
    else:
        lines.append("(none)")

    if payload["ocr"]:
        lines.extend(["", "## OCR Text", ""])
        for item in payload["ocr"]:
            lines.append(f"### `{_inline(item.get('asset_id'))}` on `{_inline(item.get('slide_id'))}`")
            lines.append(f"- Semantic status: `{_inline(item.get('semantic_status'))}`")
            if item.get("confidence") is not None:
                lines.append(f"- Confidence: `{_inline(item.get('confidence'))}`")
            _text_block(lines, "Extracted text", item.get("text"))
            if item.get("lines"):
                lines.append("**Extracted lines and positions**")
                lines.append("")
                for line in item["lines"]:
                    lines.append(f"- `{_json_inline(line.get('bbox'))}`: {_inline(line.get('text'))}")
            lines.append("")

    if payload["relationships"]:
        lines.extend(["## Relationships", ""])
        for relationship in payload["relationships"]:
            lines.append(
                f"- `{_inline(relationship.get('id'))}`: "
                f"{_inline(relationship.get('source'))} -> `{_inline(relationship.get('type'))}` -> "
                f"{_inline(relationship.get('target'))}"
            )

    if payload.get("comments"):
        lines.extend(["", "## Comments", ""])
        for comment in payload["comments"]:
            author = f" (author `{_inline(comment['author_id'])}`)" if comment.get("author_id") else ""
            lines.append(f"- Comment{author}:")
            lines.append("")
            lines.append("```text")
            lines.extend(str(comment["text"]).replace("```", "''' ").splitlines())
            lines.append("```")

    if payload["visual_regions"]:
        lines.extend(["", "## Parsed Layout Regions", ""])
        for region in payload["visual_regions"]:
            lines.append(
                f"- `{_inline(region.get('id'))}` on `{_inline(region.get('slide_id'))}`: "
                f"bbox=`{_json_inline(region.get('bbox'))}`; kind=`{_inline(region.get('region_kind'))}`"
            )

    if payload["warnings"]:
        lines.extend(["", "## Parser Notes", ""])
        lines.extend(f"- {value}" for value in payload["warnings"])
    return "\n".join(lines).rstrip() + "\n"


def render_semantic_json(deck: Any) -> str:
    """Serialize the compact semantic payload for a JSON consumer."""
    return json.dumps(semantic_dict(deck), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
