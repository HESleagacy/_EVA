"""Evidence-first diagram reconstruction for native and raster diagrams.

Native diagrams use the parser's shape and connector facts.  Raster diagrams
use the original image asset, optional asset-scoped OCR, and small deterministic
pixel heuristics.  Neither branch makes a quality claim or invokes a model.
"""

from __future__ import annotations

from collections import defaultdict
from io import BytesIO
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import zipfile

from .models import DeckIR, ExtractionReport
from .ocr import OcrAdapter, run_ocr


DIAGRAM_SCHEMA_VERSION = "diagram-reconstruction-v2"
NODE_TYPES = {"shape", "text", "image", "table", "chart", "smartart"}
NATIVE_PROXIMITY_TOLERANCE = 0.08
RASTER_MAX_DIMENSION = 512
RASTER_DARK_THRESHOLD = 200
RASTER_MIN_SEGMENT = 0.04
RASTER_MAX_SEGMENTS = 128
RASTER_MAX_CONTOURS = 128
RASTER_ENDPOINT_TOLERANCE = 0.06

Box = tuple[float, float, float, float]
Point = tuple[float, float]


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
    if width < 0 or height < 0 or not all(math.isfinite(value) for value in (left, top, width, height)):
        return None
    return left, top, width, height


def _box_list(value: Box | Iterable[float] | None) -> list[float] | None:
    return [_round(item) for item in value] if value is not None else None


def _right(value: Box) -> float:
    return value[0] + value[2]


def _bottom(value: Box) -> float:
    return value[1] + value[3]


def _area(value: Box) -> float:
    return max(0.0, value[2]) * max(0.0, value[3])


def _center(value: Box) -> Point:
    return value[0] + value[2] / 2.0, value[1] + value[3] / 2.0


def _distance_to_box(point: Point, value: Box) -> float:
    x, y = point
    dx = max(value[0] - x, 0.0, x - _right(value))
    dy = max(value[1] - y, 0.0, y - _bottom(value))
    return math.hypot(dx, dy)


def _intersection_over_union(first: Box, second: Box) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(_right(first), _right(second))
    bottom = min(_bottom(first), _bottom(second))
    overlap = max(0.0, right - left) * max(0.0, bottom - top)
    union = _area(first) + _area(second) - overlap
    return overlap / union if union else 0.0


def _slide_objects(deck: DeckIR, slide_id: str) -> list[dict[str, Any]]:
    return sorted(
        [item for item in deck.objects if item.get("slide_id") == slide_id and item.get("id")],
        key=lambda item: (item.get("z_order", 0), item.get("id", "")),
    )


def _slide_size(deck: DeckIR) -> tuple[float, float]:
    values = deck.deck.get("slide_size_emu", [1.0, 1.0])
    try:
        width, height = float(values[0]), float(values[1])
        if width > 0 and height > 0:
            return width, height
    except (IndexError, TypeError, ValueError):
        pass
    return 1.0, 1.0


def _normalized_point(value: Any, width: float, height: float) -> Point | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        x, y = float(value[0]) / width, float(value[1]) / height
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        return _round(x), _round(y)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _native_ref(item: dict[str, Any], kind: str = "native_object", **extra: Any) -> dict[str, Any]:
    result = {"id": item["id"], "kind": kind, "source": item.get("source")}
    result.update(extra)
    return result


def _evidence_ref(record: dict[str, Any], kind: str, **extra: Any) -> dict[str, Any]:
    result = {"id": record["id"], "kind": kind, "source": record.get("source")}
    result.update(extra)
    return result


def _append_diagram_record(
    deck: DeckIR,
    *,
    record_id: str,
    slide_id: str,
    object_id: str | None,
    bbox: Box | Iterable[float],
    value: dict[str, Any],
    method: str,
    status: str,
    confidence: float | None,
) -> dict[str, Any]:
    value_source = "native_ooxml" if method.startswith("native_") else method
    evidence_refs = [ref for ref in value.get("evidence_refs", []) if isinstance(ref, dict)]
    if object_id:
        evidence_refs.append({"id": object_id, "kind": "native_object"})
    if not evidence_refs:
        evidence_refs.append({"id": slide_id, "kind": "native_slide"})
    unique_refs = {(ref.get("id"), ref.get("kind")): ref for ref in evidence_refs if ref.get("id")}
    record = {
        "id": record_id,
        "slide_id": slide_id,
        "object_id": object_id,
        "bbox": [_round(item) for item in bbox],
        "value": {
            "type": value.get("type", "diagram_evidence"),
            **{key: item for key, item in value.items() if key != "type"},
            "source": value_source,
            "status": status,
        },
        "status": status,
        "confidence": confidence,
        "evidence_refs": list(unique_refs.values()),
        "source": {
            "layer": "rendered_cv",
            "method": method,
            "schema": DIAGRAM_SCHEMA_VERSION,
            "status": status,
        },
    }
    existing_index = next((index for index, item in enumerate(deck.rendered_evidence) if item.get("id") == record_id), None)
    if existing_index is not None:
        deck.rendered_evidence[existing_index] = record
        return record
    deck.add_evidence("rendered_evidence", record)
    return record


def _group_path(objects_by_id: Mapping[str, dict[str, Any]], object_id: str) -> list[str]:
    path: list[str] = []
    current = objects_by_id.get(object_id, {}).get("parent_id")
    visited: set[str] = set()
    while current and current not in visited:
        visited.add(current)
        if objects_by_id.get(current, {}).get("type") == "group":
            path.append(current)
        current = objects_by_id.get(current, {}).get("parent_id")
    return list(reversed(path))


def _native_endpoint(
    connection: Any,
    point: Point | None,
    objects_by_native_id: Mapping[str, dict[str, Any]],
    ambiguous_native_ids: set[str],
    nodes_by_object_id: Mapping[str, dict[str, Any]],
    node_items: list[tuple[dict[str, Any], Box]],
) -> tuple[str | None, str, float | None, dict[str, Any] | None]:
    connection_id = connection.get("id") if isinstance(connection, dict) else None
    if connection_id is not None and str(connection_id) not in ambiguous_native_ids:
        item = objects_by_native_id.get(str(connection_id))
        if item is not None and item["id"] in nodes_by_object_id:
            if point is None:
                return nodes_by_object_id[item["id"]]["id"], "verified", None, item
            item_box = _box(nodes_by_object_id[item["id"]].get("bbox"))
            distance = _distance_to_box(point, item_box) if item_box is not None else None
            if distance is not None and distance > NATIVE_PROXIMITY_TOLERANCE:
                return nodes_by_object_id[item["id"]]["id"], "partial", _round(distance), item
            return nodes_by_object_id[item["id"]]["id"], "verified", _round(distance or 0.0), item
    if point is None:
        return None, "unverified", None, None
    candidates = sorted(
        (
            _distance_to_box(point, item_box),
            item.get("z_order", 0),
            item["id"],
            item,
        )
        for item, item_box in node_items
    )
    if not candidates or candidates[0][0] > NATIVE_PROXIMITY_TOLERANCE:
        return None, "unverified", candidates[0][0] if candidates else None, None
    distance, _, _, item = candidates[0]
    return nodes_by_object_id[item["id"]]["id"], "partial", _round(distance), item


def _diagram_flow_direction(edges: list[dict[str, Any]], nodes: Mapping[str, dict[str, Any]]) -> str:
    directions: list[str] = []
    for edge in edges:
        if edge.get("status") != "verified":
            continue
        source, target = nodes.get(edge.get("source")), nodes.get(edge.get("target"))
        if not source or not target:
            continue
        source_center, target_center = _center(_box(source["bbox"]) or (0, 0, 0, 0)), _center(_box(target["bbox"]) or (0, 0, 0, 0))
        if abs(source_center[0] - target_center[0]) >= abs(source_center[1] - target_center[1]):
            directions.append("left_to_right" if source_center[0] < target_center[0] else "right_to_left")
        else:
            directions.append("top_to_bottom" if source_center[1] < target_center[1] else "bottom_to_top")
    if not directions:
        return "unknown"
    counts = {direction: directions.count(direction) for direction in set(directions)}
    winner = max(counts, key=counts.get)
    return winner if list(counts.values()).count(counts[winner]) == 1 else "unknown"


def _raster_flow_direction(edges: list[dict[str, Any]], nodes: Mapping[str, dict[str, Any]]) -> str:
    directions: list[str] = []
    for edge in edges:
        source, target = nodes.get(edge.get("source")), nodes.get(edge.get("target"))
        if not source or not target:
            continue
        source_center = _center(_box(source.get("bbox")) or (0, 0, 0, 0))
        target_center = _center(_box(target.get("bbox")) or (0, 0, 0, 0))
        if abs(source_center[0] - target_center[0]) >= abs(source_center[1] - target_center[1]):
            directions.append("left_to_right" if source_center[0] < target_center[0] else "right_to_left")
        else:
            directions.append("top_to_bottom" if source_center[1] < target_center[1] else "bottom_to_top")
    if not directions:
        return "unknown"
    counts = {direction: directions.count(direction) for direction in set(directions)}
    winner = max(counts, key=counts.get)
    return winner if list(counts.values()).count(counts[winner]) == 1 else "unknown"


def _native_graph(deck: DeckIR, slide: dict[str, Any]) -> dict[str, Any] | None:
    slide_id = slide["id"]
    objects = _slide_objects(deck, slide_id)
    connectors = [item for item in objects if item.get("type") == "connector"]
    if not connectors:
        return None
    objects_by_id = {item["id"]: item for item in objects}
    native_id_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in objects:
        if item.get("native_id") is not None:
            native_id_items[str(item["native_id"])].append(item)
    ambiguous_native_ids = {key for key, values in native_id_items.items() if len(values) > 1}
    objects_by_native_id = {key: values[0] for key, values in native_id_items.items() if len(values) == 1}
    node_items = [
        (item, item_box)
        for item in objects
        if item.get("type") in NODE_TYPES
        and (item_box := _box(item.get("bbox"))) is not None
        and item.get("type") != "group"
    ]
    nodes: list[dict[str, Any]] = []
    nodes_by_object_id: dict[str, dict[str, Any]] = {}
    for ordinal, (item, item_box) in enumerate(node_items, 1):
        node_id = f"{slide_id}-diagram-node-{ordinal:02d}"
        node = {
            "id": node_id,
            "object_id": item["id"],
            "native_id": item.get("native_id"),
            "type": item.get("type"),
            "bbox": _box_list(item_box),
            "text": item.get("text", ""),
            "status": "verified",
            "group_path": _group_path(objects_by_id, item["id"]),
            "evidence_refs": [_native_ref(item)],
        }
        nodes.append(node)
        nodes_by_object_id[item["id"]] = node

    groups = []
    group_id_by_object_id: dict[str, str] = {}
    for ordinal, item in enumerate((item for item in objects if item.get("type") == "group"), 1):
        group_id = f"{slide_id}-diagram-group-{ordinal:02d}"
        group_id_by_object_id[item["id"]] = group_id
        groups.append(
            {
                "id": group_id,
                "object_id": item["id"],
                "native_id": item.get("native_id"),
                "parent_id": group_id_by_object_id.get(item.get("parent_id")),
                "children": [],
                "status": "verified",
                "evidence_refs": [_native_ref(item)],
            }
        )
    for node in nodes:
        node["group_path"] = [group_id_by_object_id.get(value, value) for value in node["group_path"]]
    for group in groups:
        object_id = group["object_id"]
        parent = objects_by_id.get(object_id, {}).get("parent_id")
        parent_id = group_id_by_object_id.get(parent)
        group["parent_id"] = parent_id
        if parent_id:
            parent_group = next(item for item in groups if item["id"] == parent_id)
            parent_group["children"].append(group["id"])
        for node in nodes:
            if node["group_path"] and node["group_path"][-1] == group["id"]:
                group["children"].append(node["id"])

    width, height = _slide_size(deck)
    edges: list[dict[str, Any]] = []
    spatial_proximity: list[dict[str, Any]] = []
    for ordinal, connector in enumerate(connectors, 1):
        geometry = connector.get("geometry") if isinstance(connector.get("geometry"), dict) else {}
        connector_data = geometry.get("connector") if isinstance(geometry.get("connector"), dict) else {}
        start = _normalized_point(connector_data.get("start_emu"), width, height)
        end = _normalized_point(connector_data.get("end_emu"), width, height)
        start_connection = connector_data.get("start_connection")
        end_connection = connector_data.get("end_connection")
        start_node, start_status, start_distance, start_item = _native_endpoint(
            start_connection,
            start,
            objects_by_native_id,
            ambiguous_native_ids,
            nodes_by_object_id,
            node_items,
        )
        end_node, end_status, end_distance, end_item = _native_endpoint(
            end_connection,
            end,
            objects_by_native_id,
            ambiguous_native_ids,
            nodes_by_object_id,
            node_items,
        )
        if start_status == "partial" and start_node:
            spatial_proximity.append(
                {
                    "edge_id": f"{slide_id}-diagram-edge-{ordinal:02d}",
                    "endpoint": "start",
                    "node_id": start_node,
                    "distance": start_distance,
                    "status": "partial",
                    "evidence_refs": [_native_ref(start_item, "spatial_proximity")] if start_item else [],
                }
            )
        if end_status == "partial" and end_node:
            spatial_proximity.append(
                {
                    "edge_id": f"{slide_id}-diagram-edge-{ordinal:02d}",
                    "endpoint": "end",
                    "node_id": end_node,
                    "distance": end_distance,
                    "status": "partial",
                    "evidence_refs": [_native_ref(end_item, "spatial_proximity")] if end_item else [],
                }
            )
        endpoint_status = "verified" if start_status == end_status == "verified" else "partial" if start_node or end_node else "unverified"
        edge_status = endpoint_status
        evidence_refs = [_native_ref(connector)]
        if start_item:
            evidence_refs.append(_native_ref(start_item, "connector_endpoint"))
        if end_item and (not start_item or end_item["id"] != start_item["id"]):
            evidence_refs.append(_native_ref(end_item, "connector_endpoint"))
        edge = {
            "id": f"{slide_id}-diagram-edge-{ordinal:02d}",
            "connector_id": connector["id"],
            "source": start_node,
            "target": end_node,
            "endpoints": {
                "start": {
                    "point": list(start) if start else None,
                    "connection": start_connection,
                    "node_id": start_node,
                    "status": start_status,
                },
                "end": {
                    "point": list(end) if end else None,
                    "connection": end_connection,
                    "node_id": end_node,
                    "status": end_status,
                },
            },
            "arrowheads": {
                "start": connector_data.get("begin_arrow"),
                "end": connector_data.get("end_arrow"),
            },
            "status": edge_status,
            "evidence_refs": evidence_refs,
        }
        edges.append(edge)

    node_map = {item["id"]: item for item in nodes}
    node_recovery = 1.0 if nodes else 0.0
    edge_verification = sum(edge["status"] == "verified" for edge in edges) / len(edges) if edges else 0.0
    if not nodes:
        status = "failed"
    elif edges and all(edge["status"] == "verified" for edge in edges):
        status = "verified"
    elif any(edge["status"] == "partial" for edge in edges):
        status = "partial"
    else:
        status = "unverified"
    if ambiguous_native_ids and status == "verified":
        status = "partial"
    return {
        "type": "diagram_graph",
        "branch": "native",
        "status": status,
        "nodes": nodes,
        "edges": edges,
        "groups": groups,
        "spatial_proximity": spatial_proximity,
        "node_recovery": _round(node_recovery),
        "edge_verification": _round(edge_verification),
        "diagram_flow_direction": _diagram_flow_direction(edges, node_map),
        "flow_present": True,
        "flow_presence_basis": "native_connector",
        "evidence_refs": [ref for item in nodes + edges + groups for ref in item.get("evidence_refs", [])],
        "missing_evidence": ([] if status == "verified" else ["connector_endpoint_verification"])
        + (["ambiguous_native_ids"] if ambiguous_native_ids else []),
    }


def add_native_diagram_evidence(deck: DeckIR) -> list[dict[str, Any]]:
    """Append native shape/connector graph candidates for connector-bearing slides."""
    records: list[dict[str, Any]] = []
    for slide in sorted(deck.slides, key=lambda item: (item.get("number", 0), item.get("id", ""))):
        graph = _native_graph(deck, slide)
        if graph is None:
            continue
        slide_id = slide["id"]
        connector = next((item for item in _slide_objects(deck, slide_id) if item.get("type") == "connector"), None)
        record = _append_diagram_record(
            deck,
            record_id=f"diagram-native-{slide_id}",
            slide_id=slide_id,
            object_id=connector.get("id") if connector else None,
            bbox=(0.0, 0.0, 1.0, 1.0),
            value=graph,
            method="native_diagram",
            status=graph["status"],
            confidence=1.0 if graph["status"] == "verified" else None if graph["status"] == "failed" else 0.5,
        )
        slide["diagram_flow_direction"] = graph["diagram_flow_direction"]
        slide["flow_present"] = graph["flow_present"]
        slide["flow_presence_basis"] = graph["flow_presence_basis"]
        records.append(record)
    return records


def _load_raster(data: bytes) -> tuple[Any, int, int, str | None]:
    try:
        from PIL import Image
    except ImportError:
        return None, 0, 0, "Pillow is not installed"
    try:
        with Image.open(BytesIO(data)) as opened:
            image = opened.convert("L")
            if max(image.size) > RASTER_MAX_DIMENSION:
                scale = RASTER_MAX_DIMENSION / max(image.size)
                image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
            return image, image.width, image.height, None
    except Exception as exc:
        return None, 0, 0, f"image decode failed: {exc}"


def _dark_runs(values: list[int], width: int, height: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    horizontal: list[dict[str, Any]] = []
    vertical: list[dict[str, Any]] = []
    min_horizontal = max(6, round(width * RASTER_MIN_SEGMENT))
    min_vertical = max(6, round(height * RASTER_MIN_SEGMENT))
    for y in range(height):
        index = 0
        while index < width:
            if values[y * width + index] > RASTER_DARK_THRESHOLD:
                index += 1
                continue
            start = index
            while index < width and values[y * width + index] <= RASTER_DARK_THRESHOLD:
                index += 1
            if index - start >= min_horizontal:
                horizontal.append({"orientation": "horizontal", "bbox_px": [start, y, index - start, 1]})
    for x in range(width):
        index = 0
        while index < height:
            if values[index * width + x] > RASTER_DARK_THRESHOLD:
                index += 1
                continue
            start = index
            while index < height and values[index * width + x] <= RASTER_DARK_THRESHOLD:
                index += 1
            if index - start >= min_vertical:
                vertical.append({"orientation": "vertical", "bbox_px": [x, start, 1, index - start]})

    def merge(items: list[dict[str, Any]], orientation: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        ordered_items = sorted(items, key=lambda value: (-value["bbox_px"][2] * value["bbox_px"][3], value["bbox_px"][1], value["bbox_px"][0]))
        for item in ordered_items:
            left, top, width_value, height_value = item["bbox_px"]
            right, bottom = left + width_value, top + height_value
            matched = None
            for candidate in result:
                c_left, c_top, c_width, c_height = candidate["bbox_px"]
                c_right, c_bottom = c_left + c_width, c_top + c_height
                if orientation == "horizontal":
                    overlap = max(0, min(right, c_right) - max(left, c_left))
                    close = abs((top + bottom) / 2 - (c_top + c_bottom) / 2) <= max(3, height * 0.02)
                    enough = overlap / max(1, min(width_value, c_width)) >= 0.5
                else:
                    overlap = max(0, min(bottom, c_bottom) - max(top, c_top))
                    close = abs((left + right) / 2 - (c_left + c_right) / 2) <= max(3, width * 0.02)
                    enough = overlap / max(1, min(height_value, c_height)) >= 0.5
                if close and enough:
                    matched = candidate
                    break
            if matched is None:
                if len(result) >= RASTER_MAX_SEGMENTS:
                    continue
                result.append({"orientation": orientation, "bbox_px": [left, top, width_value, height_value]})
            else:
                c_left, c_top, c_width, c_height = matched["bbox_px"]
                new_left, new_top = min(left, c_left), min(top, c_top)
                new_right, new_bottom = max(right, c_left + c_width), max(bottom, c_top + c_height)
                matched["bbox_px"] = [new_left, new_top, new_right - new_left, new_bottom - new_top]
        return sorted(result, key=lambda value: (value["bbox_px"][1], value["bbox_px"][0]))

    return merge(horizontal, "horizontal"), merge(vertical, "vertical")


def _detect_contours(values: list[int], width: int, height: int) -> list[dict[str, Any]]:
    """Return connected dark-pixel components as conservative contour candidates."""
    if width <= 0 or height <= 0:
        return []
    visited = bytearray(width * height)
    minimum_area = max(4, round(width * height * 0.00002))
    candidates: list[dict[str, Any]] = []
    for start_index, value in enumerate(values):
        if value > RASTER_DARK_THRESHOLD or visited[start_index]:
            continue
        visited[start_index] = 1
        stack = [start_index]
        count = 0
        left = right = start_index % width
        top = bottom = start_index // width
        while stack:
            index = stack.pop()
            x, y = index % width, index // width
            count += 1
            left, right = min(left, x), max(right, x)
            top, bottom = min(top, y), max(bottom, y)
            for neighbor in (index - 1, index + 1, index - width, index + width):
                if neighbor < 0 or neighbor >= len(values) or visited[neighbor]:
                    continue
                neighbor_x = neighbor % width
                if abs(neighbor_x - x) > 1:
                    continue
                if values[neighbor] <= RASTER_DARK_THRESHOLD:
                    visited[neighbor] = 1
                    stack.append(neighbor)
        if count < minimum_area:
            continue
        candidates.append(
            {
                "bbox": [
                    _round(left / width),
                    _round(top / height),
                    _round((right - left + 1) / width),
                    _round((bottom - top + 1) / height),
                ],
                "pixel_area": count,
                "area_ratio": _round(count / (width * height)),
                "status": "partial",
            }
        )
    ordered = sorted(
        candidates,
        key=lambda item: (
            -item["pixel_area"],
            item["bbox"][1],
            item["bbox"][0],
            item["bbox"][2],
            item["bbox"][3],
        ),
    )[:RASTER_MAX_CONTOURS]
    for index, item in enumerate(ordered, 1):
        item["id"] = index
    return ordered


def _segment_value(segment: dict[str, Any], width: int, height: int, index: int) -> dict[str, Any]:
    left, top, segment_width, segment_height = segment["bbox_px"]
    if segment["orientation"] == "horizontal":
        start, end = (left / width, (top + segment_height / 2) / height), ((left + segment_width) / width, (top + segment_height / 2) / height)
    else:
        start, end = ((left + segment_width / 2) / width, top / height), ((left + segment_width / 2) / width, (top + segment_height) / height)
    return {
        "id": index,
        "orientation": segment["orientation"],
        "bbox": [_round(left / width), _round(top / height), _round(segment_width / width), _round(segment_height / height)],
        "start": [_round(start[0]), _round(start[1])],
        "end": [_round(end[0]), _round(end[1])],
        "status": "partial",
    }


def _detect_boxes(lines: list[dict[str, Any]]) -> list[Box]:
    horizontal = [item for item in lines if item["orientation"] == "horizontal"]
    vertical = [item for item in lines if item["orientation"] == "vertical"]
    boxes: list[Box] = []
    box_keys: set[tuple[float, float, float, float]] = set()
    for top_index, top_line in enumerate(horizontal):
        for bottom_line in horizontal[top_index + 1 :]:
            top = top_line["bbox"][1]
            bottom = bottom_line["bbox"][1] + bottom_line["bbox"][3]
            if bottom - top < 0.04:
                continue
            left = max(top_line["bbox"][0], bottom_line["bbox"][0])
            right = min(top_line["bbox"][0] + top_line["bbox"][2], bottom_line["bbox"][0] + bottom_line["bbox"][2])
            if right - left < 0.04:
                continue
            tolerance = 0.04
            left_lines = [
                item
                for item in vertical
                if abs(item["bbox"][0] - left) <= tolerance
                and item["bbox"][1] <= top + tolerance
                and item["bbox"][1] + item["bbox"][3] >= bottom - tolerance
            ]
            right_lines = [
                item
                for item in vertical
                if abs(item["bbox"][0] + item["bbox"][2] - right) <= tolerance
                and item["bbox"][1] <= top + tolerance
                and item["bbox"][1] + item["bbox"][3] >= bottom - tolerance
            ]
            for left_line in left_lines:
                for right_line in right_lines:
                    x_left = left_line["bbox"][0]
                    x_right = right_line["bbox"][0] + right_line["bbox"][2]
                    candidate = (x_left, top, x_right - x_left, bottom - top)
                    if x_right - x_left < 0.04:
                        continue
                    key = tuple(_round(value) for value in candidate)
                    if key not in box_keys:
                        box_keys.add(key)
                        boxes.append(candidate)
    return sorted(boxes, key=lambda item: (item[1], item[0], item[2], item[3]))[:64]


def _is_box_boundary(line: dict[str, Any], boxes: list[Box]) -> bool:
    left, top, width, height = line["bbox"]
    right, bottom = left + width, top + height
    for box in boxes:
        if line["orientation"] == "horizontal":
            on_edge = abs(top - box[1]) < 0.025 or abs(top - _bottom(box)) < 0.025
            overlap = max(0.0, min(right, _right(box)) - max(left, box[0]))
            if on_edge and overlap >= min(width, box[2]) * 0.75:
                return True
        else:
            on_edge = abs(left - box[0]) < 0.025 or abs(left - _right(box)) < 0.025
            overlap = max(0.0, min(bottom, _bottom(box)) - max(top, box[1]))
            if on_edge and overlap >= min(height, box[3]) * 0.75:
                return True
    return False


def _detect_arrows(values: list[int], width: int, height: int, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find short diagonal dark runs near detected line endpoints."""
    arrows: list[dict[str, Any]] = []
    min_run = max(4, round(min(width, height) * 0.015))
    for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
        for y in range(height):
            for x in range(width):
                previous_x, previous_y = x - dx, y - dy
                if 0 <= previous_x < width and 0 <= previous_y < height and values[previous_y * width + previous_x] <= RASTER_DARK_THRESHOLD:
                    continue
                points: list[tuple[int, int]] = []
                offset = 0
                while True:
                    px, py = x + dx * offset, y + dy * offset
                    if not (0 <= px < width and 0 <= py < height) or values[py * width + px] > RASTER_DARK_THRESHOLD:
                        break
                    points.append((px, py))
                    offset += 1
                if len(points) < min_run:
                    continue
                left = min(px for px, _ in points) / width
                top = min(py for _, py in points) / height
                right = max(px for px, _ in points) / width
                bottom = max(py for _, py in points) / height
                center = ((left + right) / 2, (top + bottom) / 2)
                near_line = any(
                    min(math.dist(center, tuple(line["start"])), math.dist(center, tuple(line["end"]))) < 0.08
                    for line in lines
                )
                if near_line:
                    direction = (
                        "down_right" if dx > 0 and dy > 0 else
                        "up_right" if dx > 0 else
                        "down_left" if dy > 0 else
                        "up_left"
                    )
                    candidate = {"bbox": [_round(left), _round(top), _round(right - left), _round(bottom - top)], "direction": direction, "status": "partial"}
                    if not any(_intersection_over_union(tuple(candidate["bbox"]), tuple(item["bbox"])) > 0.5 for item in arrows):
                        arrows.append(candidate)
                if len(arrows) >= 64:
                    return arrows
    return arrows


def _mask_ocr_regions(values: list[int], width: int, height: int, lines: list[dict[str, Any]]) -> tuple[list[int], int]:
    """Remove OCR text regions from line/arrow scans without changing source pixels."""
    masked = list(values)
    changed = 0
    padding = max(1, round(min(width, height) * 0.004))
    for line in lines:
        box = _box(line.get("bbox"))
        if box is None:
            continue
        left, top, box_width, box_height = box
        x_start = max(0, round(left * width) - padding)
        y_start = max(0, round(top * height) - padding)
        x_end = min(width, round((left + box_width) * width) + padding)
        y_end = min(height, round((top + box_height) * height) + padding)
        for y in range(y_start, y_end):
            row = y * width
            for x in range(x_start, x_end):
                index = row + x
                if masked[index] != 255:
                    masked[index] = 255
                    changed += 1
    return masked, changed


def _ocr_status(record: dict[str, Any] | None) -> str:
    if record is None:
        return "unverified"
    status = record.get("status")
    if status is None:
        value = record.get("value") if isinstance(record.get("value"), dict) else {}
        status = value.get("status")
    if status == "verified":
        return "verified"
    if status == "failed":
        return "failed"
    return "unverified"


def _raster_graph(
    deck: DeckIR,
    slide_id: str,
    asset: dict[str, Any],
    image_data: bytes | None,
    ocr_record: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    asset_id = asset["id"]
    object_item = next((item for item in _slide_objects(deck, slide_id) if item.get("asset_id") == asset_id), None)
    object_id = object_item.get("id") if object_item else None
    object_bbox = _box(object_item.get("bbox")) if object_item else (0.0, 0.0, 1.0, 1.0)
    ocr_state = _ocr_status(ocr_record)
    asset_visual_record = next(
        (
            item
            for item in deck.rendered_evidence
            if isinstance(item.get("value"), dict)
            and item["value"].get("type") == "image_asset_analysis"
            and item["value"].get("asset_id") == asset_id
            and item.get("slide_id") == slide_id
            and (object_id is None or item.get("object_id") == object_id)
        ),
        None,
    )
    asset_evidence_refs = [_evidence_ref(asset_visual_record, "image_asset_analysis")] if asset_visual_record else []
    display_geometry = object_item.get("geometry") if object_item and isinstance(object_item.get("geometry"), dict) else {}
    display_style = object_item.get("style") if object_item and isinstance(object_item.get("style"), dict) else {}
    ocr_value = ocr_record.get("value") if ocr_record and isinstance(ocr_record.get("value"), dict) else {}
    ocr_lines = [item for item in ocr_value.get("lines", []) if isinstance(item, dict)]
    image, width, height, load_error = _load_raster(image_data or b"") if image_data is not None else (None, 0, 0, "image asset is unavailable")
    detection_refs: list[dict[str, Any]] = []
    if image is None:
        line_record = _append_diagram_record(
            deck,
            record_id=f"diagram-raster-lines-{slide_id}-{asset_id}",
            slide_id=slide_id,
            object_id=object_id,
            bbox=object_bbox or (0.0, 0.0, 1.0, 1.0),
            value={"type": "raster_line_detection", "asset_id": asset_id, "coordinate_space": "asset_normalized", "lines": [], "image_size": None, "error": load_error},
            method="raster_line_detection",
            status="failed",
            confidence=None,
        )
        detection_refs.append(_evidence_ref(line_record, "raster_line_detection"))
        graph = {
            "type": "diagram_graph",
            "branch": "raster",
            "asset_id": asset_id,
            "status": "failed",
            "nodes": [],
            "edges": [],
            "groups": [],
            "spatial_proximity": [],
            "node_recovery": 0.0,
            "edge_verification": 0.0,
            "diagram_flow_direction": "unknown",
            "flow_present": None,
            "flow_presence_basis": "undetermined",
            "evidence_refs": asset_evidence_refs + detection_refs + ([_evidence_ref(ocr_record, "ocr_evidence")] if ocr_record else []),
            "missing_evidence": ["image_decode"],
            "ocr_status": ocr_state,
            "coordinate_space": "asset_normalized",
            "display_bbox": _box_list(object_bbox),
            "display_crop": display_geometry.get("crop"),
            "display_rotation_degrees": display_style.get("rotation_degrees"),
        }
        return [_append_diagram_record(deck, record_id=f"diagram-raster-{slide_id}-{asset_id}", slide_id=slide_id, object_id=object_id, bbox=object_bbox or (0.0, 0.0, 1.0, 1.0), value=graph, method="raster_diagram", status="failed", confidence=None)]

    values = list(image.getdata())
    scan_values, masked_pixel_count = _mask_ocr_regions(values, width, height, ocr_lines)
    horizontal, vertical = _dark_runs(scan_values, width, height)
    line_values = [_segment_value(item, width, height, index) for index, item in enumerate(horizontal + vertical)]
    line_status = "partial" if line_values else "unverified"
    line_record = _append_diagram_record(
        deck,
        record_id=f"diagram-raster-lines-{slide_id}-{asset_id}",
        slide_id=slide_id,
        object_id=object_id,
        bbox=object_bbox or (0.0, 0.0, 1.0, 1.0),
        value={"type": "raster_line_detection", "asset_id": asset_id, "coordinate_space": "asset_normalized", "lines": line_values, "image_size": [width, height], "ocr_masked_pixel_count": masked_pixel_count},
        method="raster_line_detection",
        status=line_status,
        confidence=0.5 if line_values else None,
    )
    detection_refs.append(_evidence_ref(line_record, "raster_line_detection"))
    contour_candidates = _detect_contours(values, width, height)
    contour_record = _append_diagram_record(
        deck,
        record_id=f"diagram-raster-contours-{slide_id}-{asset_id}",
        slide_id=slide_id,
        object_id=object_id,
        bbox=object_bbox or (0.0, 0.0, 1.0, 1.0),
        value={
            "type": "raster_contour_detection",
            "asset_id": asset_id,
            "coordinate_space": "asset_normalized",
            "contours": contour_candidates,
            "image_size": [width, height],
        },
        method="raster_contour_detection",
        status="partial" if contour_candidates else "unverified",
        confidence=0.5 if contour_candidates else None,
    )
    detection_refs.append(_evidence_ref(contour_record, "raster_contour_detection"))
    box_candidates = _detect_boxes(line_values)
    box_record = _append_diagram_record(
        deck,
        record_id=f"diagram-raster-boxes-{slide_id}-{asset_id}",
        slide_id=slide_id,
        object_id=object_id,
        bbox=object_bbox or (0.0, 0.0, 1.0, 1.0),
        value={"type": "raster_box_detection", "asset_id": asset_id, "coordinate_space": "asset_normalized", "boxes": [_box_list(item) for item in box_candidates], "line_evidence_ref": line_record["id"], "contour_evidence_ref": contour_record["id"]},
        method="raster_box_detection",
        status="partial" if box_candidates else "unverified",
        confidence=0.5 if box_candidates else None,
    )
    detection_refs.append(_evidence_ref(box_record, "raster_box_detection"))
    arrow_candidates = _detect_arrows(scan_values, width, height, line_values)
    arrow_record = _append_diagram_record(
        deck,
        record_id=f"diagram-raster-arrows-{slide_id}-{asset_id}",
        slide_id=slide_id,
        object_id=object_id,
        bbox=object_bbox or (0.0, 0.0, 1.0, 1.0),
        value={"type": "raster_arrow_detection", "asset_id": asset_id, "coordinate_space": "asset_normalized", "arrows": arrow_candidates, "line_evidence_ref": line_record["id"]},
        method="raster_arrow_detection",
        status="partial" if arrow_candidates else "unverified",
        confidence=0.5 if arrow_candidates else None,
    )
    detection_refs.append(_evidence_ref(arrow_record, "raster_arrow_detection"))
    line_ref, contour_ref, box_ref, arrow_ref = detection_refs

    node_candidates: list[dict[str, Any]] = []
    assigned_lines: set[int] = set()
    box_matches: list[tuple[Box, list[tuple[int, dict[str, Any]]]]] = []
    for candidate in sorted(box_candidates, key=lambda item: (_area(item), item[1], item[0], item[2], item[3])):
        matches: list[tuple[int, dict[str, Any]]] = []
        for line_index, line in enumerate(ocr_lines):
            line_box = _box(line.get("bbox"))
            if line_box is not None and (_intersection_over_union(candidate, line_box) > 0 or _distance_to_box(_center(line_box), candidate) < 0.04):
                matches.append((line_index, line))
        if ocr_state == "verified" and not matches:
            continue
        box_matches.append((candidate, matches))

    for candidate, matches in box_matches:
        new_matches = [(line_index, line) for line_index, line in matches if line_index not in assigned_lines]
        if ocr_state == "verified" and not new_matches:
            continue
        assigned_lines.update(line_index for line_index, _ in new_matches)
        refs = [box_ref]
        if ocr_record:
            refs.append(_evidence_ref(ocr_record, "ocr_evidence", line_count=len(new_matches)))
        node_candidates.append({"bbox": _box_list(candidate), "text": " ".join(item.get("text", "") for _, item in new_matches), "status": "partial" if ocr_state == "verified" else "unverified", "evidence_refs": refs})
    direct_ocr_nodes = 0
    for line_index, line in enumerate(ocr_lines):
        if line_index in assigned_lines:
            continue
        line_box = _box(line.get("bbox"))
        if line_box is None:
            continue
        direct_ocr_nodes += 1
        node_candidates.append({"bbox": _box_list(line_box), "text": line.get("text", ""), "status": "partial" if ocr_state == "verified" else "unverified", "evidence_refs": [_evidence_ref(ocr_record, "ocr_evidence", line_index=line_index)] if ocr_record else []})
    if not node_candidates:
        fallback_candidates = box_candidates or [tuple(item["bbox"]) for item in contour_candidates]
        fallback_ref = box_ref if box_candidates else contour_ref
        node_candidates = [{"bbox": _box_list(candidate), "text": "", "status": "unverified", "evidence_refs": [fallback_ref]} for candidate in fallback_candidates]

    nodes = []
    for index, candidate in enumerate(node_candidates, 1):
        nodes.append({"id": f"{slide_id}-{asset_id}-diagram-node-{index:02d}", "bbox": candidate["bbox"], "text": candidate["text"], "status": candidate["status"], "coordinate_space": "asset_normalized", "evidence_refs": candidate["evidence_refs"] or detection_refs[:1]})
    edge_candidates = []
    node_boxes = [(node, _box(node["bbox"])) for node in nodes]
    unresolved_edge_candidates = 0
    for index, line in enumerate(line_values):
        if _is_box_boundary(line, box_candidates):
            continue
        start, end = tuple(line["start"]), tuple(line["end"])
        start_matches = sorted((_distance_to_box(start, item_box), node["id"], node) for node, item_box in node_boxes if item_box is not None)
        end_matches = sorted((_distance_to_box(end, item_box), node["id"], node) for node, item_box in node_boxes if item_box is not None)
        start_node = next((item for distance, _, item in start_matches if distance <= RASTER_ENDPOINT_TOLERANCE), None)
        end_node = next((item for distance, _, item in end_matches if distance <= RASTER_ENDPOINT_TOLERANCE and (start_node is None or item["id"] != start_node["id"])), None)
        if start_node is None or end_node is None:
            unresolved_edge_candidates += 1
            continue
        arrow_matches = [arrow for arrow in arrow_candidates if min(math.dist(tuple(arrow["bbox"][:2]), start), math.dist(tuple(arrow["bbox"][:2]), end)) < 0.12]
        # Pixel detections are candidate evidence only.  They cannot verify an
        # edge until a stronger source confirms the endpoint and arrow geometry.
        status = "partial" if arrow_matches else "unverified"
        refs = [line_ref, box_ref]
        if arrow_matches:
            refs.append(arrow_ref)
        refs.extend(start_node["evidence_refs"])
        refs.extend(end_node["evidence_refs"])
        edge_candidates.append({"id": f"{slide_id}-{asset_id}-diagram-edge-{index + 1:02d}", "source": start_node["id"] if start_node else None, "target": end_node["id"] if end_node else None, "line": line, "arrow": arrow_matches[0] if arrow_matches else None, "status": status, "coordinate_space": "asset_normalized", "evidence_refs": refs})

    ocr_line_count = len(ocr_lines)
    node_recovery = (len(assigned_lines) + direct_ocr_nodes) / ocr_line_count if ocr_line_count else 0.0
    edge_verification = sum(item["status"] == "verified" for item in edge_candidates) / len(edge_candidates) if edge_candidates else 0.0
    missing_evidence = []
    if ocr_state != "verified":
        missing_evidence.append("ocr")
    if not line_values:
        missing_evidence.append("line_detection")
    if not contour_candidates:
        missing_evidence.append("contour_detection")
    if not box_candidates:
        missing_evidence.append("box_detection")
    if not arrow_candidates:
        missing_evidence.append("arrow_detection")
    if not edge_candidates:
        missing_evidence.append("edge_candidates")
    if unresolved_edge_candidates:
        missing_evidence.append("edge_endpoint_verification")
    if edge_candidates and edge_verification == 0.0:
        missing_evidence.append("edge_verification")
    if not nodes:
        status = "unverified"
    elif ocr_state != "verified" or node_recovery <= 0.0 or not edge_candidates or edge_verification <= 0.0:
        status = "unverified"
    elif all(item["status"] == "verified" for item in edge_candidates) and ocr_state == "verified":
        status = "verified"
    else:
        status = "partial"
    graph = {
        "type": "diagram_graph",
        "branch": "raster",
        "asset_id": asset_id,
        "status": status,
        "nodes": nodes,
        "edges": edge_candidates,
        "groups": [],
        "spatial_proximity": [],
        "node_recovery": _round(node_recovery),
        "ocr_line_coverage": _round(node_recovery),
        "edge_verification": _round(edge_verification),
        "diagram_flow_direction": _raster_flow_direction(edge_candidates, {node["id"]: node for node in nodes}),
        "flow_present": True if edge_candidates else None,
        "flow_presence_basis": "raster_edge_candidate" if edge_candidates else "undetermined",
        "ocr_status": ocr_state,
        "evidence_refs": asset_evidence_refs + detection_refs + ([_evidence_ref(ocr_record, "ocr_evidence")] if ocr_record else []),
        "missing_evidence": missing_evidence,
        "coordinate_space": "asset_normalized",
        "display_bbox": _box_list(object_bbox),
        "display_crop": display_geometry.get("crop"),
        "display_rotation_degrees": display_style.get("rotation_degrees"),
        "unresolved_edge_candidates": unresolved_edge_candidates,
        "analysis_target": "original_asset",
    }
    return [_append_diagram_record(deck, record_id=f"diagram-raster-{slide_id}-{asset_id}", slide_id=slide_id, object_id=object_id, bbox=object_bbox or (0.0, 0.0, 1.0, 1.0), value=graph, method="raster_diagram", status=status, confidence=_round(node_recovery) if status != "failed" else None)]


def reconstruct_raster_diagrams(
    report: ExtractionReport,
    source: str | Path,
    *,
    slides: Sequence[int] | None = None,
    asset_ids: Sequence[str] | None = None,
    adapter: OcrAdapter | None = None,
    ocr_cache_dir: str | Path | None = None,
    min_dimension: int = 0,
    run_ocr_stage: bool = True,
    skip_ocr: bool = False,
) -> list[dict[str, Any]]:
    """Build candidate graphs for selected image assets.

    OCR is explicit and asset-scoped.  If OCR or image decoding is unavailable,
    the resulting graph records retain that missing evidence and cannot become
    verified.
    """
    if report.canonical is None:
        return []
    if run_ocr_stage and not skip_ocr:
        run_ocr(
            report,
            source,
            slides=slides,
            asset_ids=asset_ids,
            adapter=adapter,
            cache_dir=ocr_cache_dir,
            min_dimension=min_dimension,
            skip=skip_ocr,
        )
    allowed_slides = {f"slide-{number:02d}" for number in slides} if slides else None
    wanted_assets = set(asset_ids or [])
    image_objects = [
        item
        for item in report.canonical.objects
        if item.get("type") == "image"
        and (allowed_slides is None or item.get("slide_id") in allowed_slides)
        and (not wanted_assets or item.get("asset_id") in wanted_assets)
        and item.get("asset_id")
    ]
    first_instance: dict[tuple[str, str], dict[str, Any]] = {}
    for item in image_objects:
        first_instance.setdefault((item["slide_id"], item["asset_id"]), item)
    assets = {item["id"]: item for item in report.canonical.assets if item.get("id")}
    ocr_by_asset: dict[str, dict[str, Any]] = {}
    for item in report.canonical.ocr_evidence:
        value = item.get("value") if isinstance(item.get("value"), dict) else {}
        asset_id = value.get("asset_id")
        if asset_id:
            ocr_by_asset[asset_id] = item
    results: list[dict[str, Any]] = []
    with zipfile.ZipFile(Path(source).expanduser().resolve()) as archive:
        for (slide_id, asset_id), instance in sorted(first_instance.items()):
            asset = assets.get(asset_id)
            if asset is None:
                continue
            try:
                image_data = archive.read(asset["part"])
            except (KeyError, OSError, RuntimeError):
                image_data = None
            raster_records = _raster_graph(report.canonical, slide_id, asset, image_data, ocr_by_asset.get(asset_id))
            results.extend(raster_records)
            slide_record = next((item for item in report.canonical.slides if item.get("id") == slide_id), None)
            if slide_record is not None and raster_records:
                graph = raster_records[0].get("value", {})
                if graph.get("flow_present") is True:
                    slide_record["flow_present"] = True
                    slide_record["flow_presence_basis"] = graph.get("flow_presence_basis", "raster_edge_candidate")
                    if graph.get("diagram_flow_direction") != "unknown":
                        slide_record["diagram_flow_direction"] = graph["diagram_flow_direction"]
    return results


def reconstruct_diagrams(
    report: ExtractionReport,
    source: str | Path | None = None,
    *,
    slides: Sequence[int] | None = None,
    asset_ids: Sequence[str] | None = None,
    adapter: OcrAdapter | None = None,
    ocr_cache_dir: str | Path | None = None,
    min_dimension: int = 0,
    run_ocr_stage: bool = True,
    skip_ocr: bool = False,
) -> list[dict[str, Any]]:
    """Run native reconstruction and optionally the explicit raster branch."""
    records = add_native_diagram_evidence(report.canonical) if report.canonical is not None else []
    if source is not None:
        records.extend(
            reconstruct_raster_diagrams(
                report,
                source,
                slides=slides,
                asset_ids=asset_ids,
                adapter=adapter,
                ocr_cache_dir=ocr_cache_dir,
                min_dimension=min_dimension,
                run_ocr_stage=run_ocr_stage,
                skip_ocr=skip_ocr,
            )
        )
    return records
