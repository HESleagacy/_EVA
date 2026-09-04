"""Native PDF extraction into the shared DeckIR contract.

The PDF adapter reads PDF objects directly with pypdf. It does not rasterize
the document or convert it to OOXML. Text, image XObjects, vector paths, link
annotations, page boxes, and document security metadata remain native facts;
OCR, rendering, and vision are separate optional stages.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from io import BytesIO
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .extractor import ExtractionError
from .models import (
    DECKIR_SCHEMA,
    DECKIR_SCHEMA_VERSION,
    DeckIR,
    ExtractionReport,
    MediaRecord,
    PartRecord,
    RelationshipRecord,
    SlideRecord,
)

PDF_NATIVE_SCHEMA = "pdf-native@1"
PDF_PARSER_VERSION = "0.1.0"
MAX_PDF_BYTES = 256 * 1024 * 1024
MAX_PDF_PAGES = 10_000
MAX_PDF_OBJECTS = 500_000
_PDF_HEADER = re.compile(rb"^%PDF-(\d+\.\d+)")
_PAGE_BOX_KEYS = ("/MediaBox", "/CropBox", "/BleedBox", "/TrimBox", "/ArtBox")
_PATH_PAINT_OPERATORS = frozenset({"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"})
_IMAGE_MIMES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",
    b"II*\x00": "image/tiff",
    b"MM\x00*": "image/tiff",
    b"\x00\x00\x00\x0cjP  \r\n\x87\n": "image/jp2",
}
_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/tiff": "tiff",
    "image/jp2": "jp2",
}
Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _round(value: float, digits: int = 12) -> float:
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded


def _number(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _integer(value: Any, default: int | None = None) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return default
    return int(number)


def _name(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[1:] if text.startswith("/") else text


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _resolve(value: Any) -> Any:
    getter = getattr(value, "get_object", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return value


def _object_ref(value: Any) -> dict[str, Any] | None:
    candidate = value
    if not hasattr(candidate, "idnum"):
        candidate = getattr(value, "indirect_reference", None)
    if candidate is None or not hasattr(candidate, "idnum"):
        return None
    object_number = _integer(getattr(candidate, "idnum", None))
    generation = _integer(getattr(candidate, "generation", None), 0)
    if object_number is None or generation is None or object_number < 0 or generation < 0:
        return None
    return {
        "object": object_number,
        "generation": generation,
        "reference": f"{object_number} {generation} R",
    }


def _reference_key(value: Any) -> str | None:
    reference = _object_ref(value)
    return reference.get("reference") if reference else None


def _pdf_value(value: Any, depth: int = 0) -> Any:
    """Convert small PDF values to bounded JSON-safe diagnostic values."""
    if depth > 8:
        return "<depth-limit>"
    reference = _object_ref(value)
    if reference is not None:
        return reference
    resolved = _resolve(value)
    if resolved is None:
        return None
    if isinstance(resolved, bool):
        return resolved
    if isinstance(resolved, (int, float)):
        number = _number(resolved)
        return _round(number) if number is not None else None
    if isinstance(resolved, bytes):
        return {"size": len(resolved), "sha256": _sha256(resolved)}
    if isinstance(resolved, str):
        return str(resolved)
    if isinstance(resolved, Mapping):
        result: dict[str, Any] = {}
        for key, item in sorted(resolved.items(), key=lambda pair: str(pair[0])):
            result[str(key)] = _pdf_value(item, depth + 1)
        return result
    if isinstance(resolved, (list, tuple)):
        return [_pdf_value(item, depth + 1) for item in resolved]
    return _text(resolved)


def _matrix(values: Iterable[Any]) -> Matrix | None:
    values = list(values)
    if len(values) < 6:
        return None
    numbers = [_number(item) for item in values[:6]]
    if any(item is None for item in numbers):
        return None
    return tuple(float(item) for item in numbers)  # type: ignore[arg-type,return-value]


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


def _bbox(points: Iterable[tuple[float, float]]) -> list[float] | None:
    points = list(points)
    if not points or any(not math.isfinite(value) for point in points for value in point):
        return None
    xs, ys = zip(*points)
    left, right = min(xs), max(xs)
    bottom, top = min(ys), max(ys)
    return [_round(left), _round(bottom), _round(right - left), _round(top - bottom)]


def _array_box(value: Any) -> list[float] | None:
    resolved = _resolve(value)
    if isinstance(resolved, Mapping):
        return None
    try:
        values = list(resolved or [])
    except (TypeError, ValueError):
        return None
    if len(values) < 4:
        return None
    numbers = [_number(item) for item in values[:4]]
    if any(item is None for item in numbers):
        return None
    x0, y0, x1, y1 = (float(item) for item in numbers)  # type: ignore[arg-type]
    left, right = sorted((x0, x1))
    bottom, top = sorted((y0, y1))
    width, height = right - left, top - bottom
    if not all(math.isfinite(item) for item in (left, bottom, width, height)):
        return None
    if right <= left or top <= bottom:
        return [_round(left), _round(bottom), _round(max(0.0, width)), _round(max(0.0, height))]
    return [_round(left), _round(bottom), _round(width), _round(height)]


def _point_from_box(box: list[float]) -> list[tuple[float, float]]:
    x, y, width, height = box
    return [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]


def _display_point(point: tuple[float, float], geometry: Mapping[str, Any]) -> tuple[float, float]:
    crop = geometry["crop_box"]
    crop_x, crop_y, width, height = (float(item) for item in crop)
    x, y = point[0] - crop_x, point[1] - crop_y
    rotation = int(geometry["rotation"]) % 360
    if rotation == 270:
        display = (y, x)
    elif rotation == 180:
        display = (width - x, y)
    elif rotation == 90:
        display = (height - y, width - x)
    else:
        display = (x, height - y)
    unit = float(geometry.get("user_unit", 1.0))
    return display[0] * unit, display[1] * unit


def _coordinate_box(raw_box: list[float], geometry: Mapping[str, Any]) -> dict[str, Any]:
    display_points = [_display_point(point, geometry) for point in _point_from_box(raw_box)]
    display_box = _bbox(display_points) or [0.0, 0.0, 0.0, 0.0]
    display_width, display_height = (float(item) for item in geometry["display_size_points"])
    normalized = [
        _round(display_box[0] / display_width) if display_width else 0.0,
        _round(display_box[1] / display_height) if display_height else 0.0,
        _round(display_box[2] / display_width) if display_width else 0.0,
        _round(display_box[3] / display_height) if display_height else 0.0,
    ]
    return {
        "bbox_pdf": raw_box,
        "bbox_points": display_box,
        "bbox": normalized,
    }


def _page_geometry(page: Any, page_number: int) -> dict[str, Any]:
    boxes: dict[str, list[float]] = {}
    for key in _PAGE_BOX_KEYS:
        box = _array_box(page.get(key))
        if box is not None:
            boxes[key[1:].casefold()] = box
    media_box = boxes.get("mediabox")
    if media_box is None or media_box[2] <= 0 or media_box[3] <= 0:
        raise ExtractionError(f"PDF page {page_number} has no valid MediaBox")
    crop_box = boxes.get("cropbox") or media_box
    if crop_box[2] <= 0 or crop_box[3] <= 0:
        raise ExtractionError(f"PDF page {page_number} has no valid CropBox")
    rotation = _integer(page.get("/Rotate"), 0) or 0
    rotation %= 360
    if rotation not in {0, 90, 180, 270}:
        rotation = (round(rotation / 90) * 90) % 360
    user_unit = _number(page.get("/UserUnit"), 1.0) or 1.0
    if user_unit <= 0:
        user_unit = 1.0
    width, height = crop_box[2], crop_box[3]
    display_size = [height, width] if rotation in {90, 270} else [width, height]
    return {
        "page_number": page_number,
        "object_ref": _object_ref(page),
        "boxes": boxes,
        "media_box": media_box,
        "crop_box": crop_box,
        "rotation": rotation,
        "user_unit": _round(user_unit),
        "display_size_points": [_round(display_size[0] * user_unit), _round(display_size[1] * user_unit)],
        "coordinate_system": {
            "native": "pdf_user_space_bottom_left",
            "output": "normalized_top_left",
        },
    }


def _font_name(font_dict: Any) -> str | None:
    font = _resolve(font_dict)
    if not isinstance(font, Mapping):
        return None
    value = font.get("/BaseFont")
    return _name(value)


def _font_width_units(text: str, font_dict: Any) -> float:
    """Estimate text advance while retaining the native transform separately."""
    font = _resolve(font_dict)
    widths = _resolve(font.get("/Widths")) if isinstance(font, Mapping) else None
    first_char = _integer(font.get("/FirstChar"), 0) if isinstance(font, Mapping) else 0
    if isinstance(widths, (list, tuple)) and first_char is not None:
        values: list[float] = []
        for character in text:
            if character in "\r\n":
                continue
            index = ord(character) - first_char
            width = _number(widths[index]) if 0 <= index < len(widths) else None
            values.append(width if width is not None and width >= 0 else 500.0)
        if values:
            return max(sum(values) / 1000.0, 0.25)
    visible = sum(4 if character == "\t" else 1 for character in text if character not in "\r\n")
    return max(visible * 0.5, 0.25)


def _text_spans(page: Any, page_number: int, geometry: Mapping[str, Any], warnings: list[str]) -> tuple[str, list[dict[str, Any]]]:
    spans: list[dict[str, Any]] = []

    def visitor_text(text: Any, cm: Any, tm: Any, font_dict: Any, font_size: Any) -> None:
        value = _text(text)
        if not value or not value.strip():
            return
        cm_matrix = _matrix(cm) or IDENTITY
        tm_matrix = _matrix(tm) or IDENTITY
        combined = _matrix_multiply(cm_matrix, tm_matrix)
        size = _number(font_size, 12.0) or 12.0
        if size <= 0:
            size = 12.0
        advance = _font_width_units(value, font_dict) * size
        local_height = size
        raw_points = [
            _matrix_apply(combined, (0.0, -local_height * 0.2)),
            _matrix_apply(combined, (advance, -local_height * 0.2)),
            _matrix_apply(combined, (advance, local_height * 0.8)),
            _matrix_apply(combined, (0.0, local_height * 0.8)),
        ]
        raw_box = _bbox(raw_points)
        if raw_box is None:
            warnings.append(f"PDF page {page_number} produced a text span without a finite bbox")
            return
        coordinates = _coordinate_box(raw_box, geometry)
        spans.append(
            {
                "text": value,
                "font": _font_name(font_dict),
                "font_size": _round(size),
                "text_matrix": [_round(item) for item in tm_matrix],
                "ctm": [_round(item) for item in cm_matrix],
                "text_transform": [_round(item) for item in combined],
                "bbox_pdf": coordinates["bbox_pdf"],
                "bbox_points": coordinates["bbox_points"],
                "bbox": coordinates["bbox"],
                "bbox_basis": "estimated_text_advance",
            }
        )

    try:
        extracted = page.extract_text(visitor_text=visitor_text)
    except Exception as exc:
        warnings.append(f"PDF page {page_number} text extraction failed: {exc}")
        try:
            extracted = page.extract_text() or ""
        except Exception as fallback_exc:
            warnings.append(f"PDF page {page_number} fallback text extraction failed: {fallback_exc}")
            extracted = ""
    return _text(extracted), spans


def _resource_xobjects(page: Any) -> dict[str, Any]:
    resources = _resolve(page.get("/Resources"))
    if not isinstance(resources, Mapping):
        return {}
    xobjects = _resolve(resources.get("/XObject"))
    if not isinstance(xobjects, Mapping):
        return {}
    return {str(key).lstrip("/"): value for key, value in xobjects.items()}


def _image_mime(data: bytes, xobject: Any) -> str:
    for signature, mime in _IMAGE_MIMES.items():
        if data.startswith(signature):
            return mime
    filters = _filter_names(xobject.get("/Filter") if isinstance(xobject, Mapping) else None)
    if "JPXDecode" in filters:
        return "image/jp2"
    if "DCTDecode" in filters:
        return "image/jpeg"
    if "JBIG2Decode" in filters:
        return "image/jbig2"
    return "application/octet-stream"


def _filter_names(value: Any) -> list[str]:
    resolved = _resolve(value)
    if isinstance(resolved, (list, tuple)):
        return [item for item in (_name(entry) for entry in resolved) if item]
    item = _name(resolved)
    return [item] if item else []


def _image_records(page: Any, page_number: int, warnings: list[str]) -> dict[str, dict[str, Any]]:
    xobjects = _resource_xobjects(page)
    records: dict[str, dict[str, Any]] = {}
    for resource_name, resource in sorted(xobjects.items()):
        xobject = _resolve(resource)
        if not isinstance(xobject, Mapping) or _name(xobject.get("/Subtype")) != "Image":
            continue
        data = b""
        if callable(getattr(xobject, "get_data", None)):
            try:
                # Read the image stream directly. page.images may decode and
                # re-encode JPX data, which loses the source representation.
                data = bytes(xobject.get_data())
            except Exception as exc:
                warnings.append(f"PDF page {page_number} image {resource_name} extraction failed: {exc}")
                data = b""
        width = _integer(xobject.get("/Width"), 0) if isinstance(xobject, Mapping) else 0
        height = _integer(xobject.get("/Height"), 0) if isinstance(xobject, Mapping) else 0
        object_ref = _object_ref(resource)
        reference_key = object_ref["reference"] if object_ref else f"page-{page_number}:{resource_name}"
        mime = _image_mime(data, xobject)
        records[resource_name] = {
            "resource_name": resource_name,
            "object_ref": object_ref,
            "reference_key": reference_key,
            "data": data,
            "content_type": mime,
            "width": width or None,
            "height": height or None,
            "filters": _filter_names(xobject.get("/Filter") if isinstance(xobject, Mapping) else None),
            "color_space": _pdf_value(xobject.get("/ColorSpace")) if isinstance(xobject, Mapping) else None,
            "bits_per_component": _integer(xobject.get("/BitsPerComponent"), None) if isinstance(xobject, Mapping) else None,
            "has_soft_mask": bool(xobject.get("/SMask")) if isinstance(xobject, Mapping) else False,
        }
    return records


def _content_records(
    page: Any,
    reader: Any,
    page_number: int,
    geometry: Mapping[str, Any],
    image_names: set[str],
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Return image placements, painted paths, and deterministic operator counts."""
    try:
        from pypdf.generic import ContentStream

        contents = page.get_contents()
        if contents is None:
            return [], [], {}
        operations = ContentStream(contents, reader).operations
    except Exception as exc:
        warnings.append(f"PDF page {page_number} content stream parsing failed: {exc}")
        return [], [], {}

    xobjects = _resource_xobjects(page)
    operator_counts: Counter[str] = Counter()
    image_occurrences: list[dict[str, Any]] = []
    vector_records: list[dict[str, Any]] = []
    graphics_stack: list[Matrix] = []
    current_matrix = IDENTITY
    path_points: list[tuple[float, float]] = []
    path_segments: list[dict[str, Any]] = []
    current_point: tuple[float, float] | None = None
    subpath_start: tuple[float, float] | None = None

    def transformed_point(x: float, y: float) -> tuple[float, float]:
        return _matrix_apply(current_matrix, (x, y))

    def append_segment(operator: str, raw_values: list[float], points: list[tuple[float, float]]) -> None:
        path_points.extend(points)
        path_segments.append(
            {
                "operator": operator,
                "operands": [_round(item) for item in raw_values],
                "points_pdf": [[_round(x), _round(y)] for x, y in points],
                "ctm": [_round(item) for item in current_matrix],
            }
        )

    def clear_path() -> None:
        nonlocal current_point, subpath_start
        path_points.clear()
        path_segments.clear()
        current_point = None
        subpath_start = None

    for operator_index, (raw_operands, raw_operator) in enumerate(operations):
        operator = _text(raw_operator)
        operator_counts[operator] += 1
        operands = list(raw_operands)
        if operator == "q":
            graphics_stack.append(current_matrix)
        elif operator == "Q":
            if graphics_stack:
                current_matrix = graphics_stack.pop()
            else:
                warnings.append(f"PDF page {page_number} has an unmatched Q graphics operator")
        elif operator == "cm":
            matrix = _matrix(operands)
            if matrix is None:
                warnings.append(f"PDF page {page_number} has an invalid cm operator")
            else:
                current_matrix = _matrix_multiply(current_matrix, matrix)
        elif operator in {"m", "l"} and len(operands) >= 2:
            values = [_number(item) for item in operands[:2]]
            if all(item is not None for item in values):
                point = transformed_point(float(values[0]), float(values[1]))  # type: ignore[arg-type]
                append_segment(operator, [float(values[0]), float(values[1])], [point])
                current_point = point
                if operator == "m":
                    subpath_start = point
        elif operator == "c" and len(operands) >= 6:
            values = [_number(item) for item in operands[:6]]
            if all(item is not None for item in values):
                numbers = [float(item) for item in values]  # type: ignore[arg-type]
                points = [transformed_point(numbers[index], numbers[index + 1]) for index in range(0, 6, 2)]
                append_segment(operator, numbers, points)
                current_point = points[-1]
        elif operator in {"v", "y"} and len(operands) >= 4:
            values = [_number(item) for item in operands[:4]]
            if all(item is not None for item in values):
                numbers = [float(item) for item in values]  # type: ignore[arg-type]
                if operator == "v" and current_point is not None:
                    points = [current_point, transformed_point(numbers[0], numbers[1]), transformed_point(numbers[2], numbers[3])]
                else:
                    points = [transformed_point(numbers[0], numbers[1]), transformed_point(numbers[2], numbers[3])]
                append_segment(operator, numbers, points)
                current_point = points[-1]
        elif operator == "re" and len(operands) >= 4:
            values = [_number(item) for item in operands[:4]]
            if all(item is not None for item in values):
                x, y, width, height = (float(item) for item in values)  # type: ignore[arg-type]
                points = [
                    transformed_point(x, y),
                    transformed_point(x + width, y),
                    transformed_point(x + width, y + height),
                    transformed_point(x, y + height),
                ]
                append_segment(operator, [x, y, width, height], points)
                current_point = points[-1]
                subpath_start = points[0]
        elif operator == "h":
            if current_point is not None and subpath_start is not None:
                path_points.append(subpath_start)
                path_segments.append(
                    {
                        "operator": operator,
                        "operands": [],
                        "points_pdf": [[_round(subpath_start[0]), _round(subpath_start[1])]],
                        "ctm": [_round(item) for item in current_matrix],
                    }
                )
                current_point = subpath_start
        elif operator in _PATH_PAINT_OPERATORS:
            raw_box = _bbox(path_points)
            if raw_box is not None:
                vector_records.append(
                    {
                        "operator": operator,
                        "operator_index": operator_index,
                        "segments": list(path_segments),
                        "bbox_pdf": raw_box,
                    }
                )
            clear_path()
        elif operator == "n":
            clear_path()
        elif operator == "Do" and operands:
            resource_name = _name(operands[0])
            xobject = _resolve(xobjects.get(resource_name or ""))
            if resource_name in image_names and isinstance(xobject, Mapping) and _name(xobject.get("/Subtype")) == "Image":
                raw_box = _bbox(
                    [
                        _matrix_apply(current_matrix, (0.0, 0.0)),
                        _matrix_apply(current_matrix, (1.0, 0.0)),
                        _matrix_apply(current_matrix, (1.0, 1.0)),
                        _matrix_apply(current_matrix, (0.0, 1.0)),
                    ]
                )
                if raw_box is not None:
                    image_occurrences.append(
                        {
                            "resource_name": resource_name,
                            "operator_index": operator_index,
                            "ctm": [_round(item) for item in current_matrix],
                            **_coordinate_box(raw_box, geometry),
                        }
                    )
    return image_occurrences, vector_records, {key: operator_counts[key] for key in sorted(operator_counts)}


def _annotation_records(
    page: Any,
    page_number: int,
    slide_id: str,
    geometry: Mapping[str, Any],
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[RelationshipRecord], list[dict[str, Any]]]:
    try:
        annotations = list(_resolve(page.get("/Annots")) or [])
    except Exception as exc:
        warnings.append(f"PDF page {page_number} annotation enumeration failed: {exc}")
        return [], [], [], []
    objects: list[dict[str, Any]] = []
    hyperlinks: list[dict[str, Any]] = []
    relationships: list[RelationshipRecord] = []
    comments: list[dict[str, Any]] = []
    source_part = f"pdf:page-{page_number:02d}"
    for annotation_index, raw_annotation in enumerate(annotations, 1):
        annotation = _resolve(raw_annotation)
        if not isinstance(annotation, Mapping):
            warnings.append(f"PDF page {page_number} annotation {annotation_index} is not a dictionary")
            continue
        subtype = _name(annotation.get("/Subtype")) or "Unknown"
        raw_box = _array_box(annotation.get("/Rect")) or [0.0, 0.0, 0.0, 0.0]
        coordinates = _coordinate_box(raw_box, geometry)
        annotation_id = f"{slide_id}-annotation-{annotation_index:04d}"
        relationship_id = f"annotation-{annotation_index:04d}"
        action = _resolve(annotation.get("/A"))
        action_type = _name(action.get("/S")) if isinstance(action, Mapping) else None
        uri = _text(action.get("/URI")) if isinstance(action, Mapping) and action_type == "URI" else ""
        destination = annotation.get("/Dest")
        if destination is None and isinstance(action, Mapping):
            destination = action.get("/D")
        destination_value = _pdf_value(destination) if destination is not None else None
        destination_text = (
            json.dumps(destination_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if destination_value is not None
            else ""
        )
        target = uri or destination_text
        relationship_type = "link" if subtype == "Link" else f"annotation/{subtype.casefold()}"
        relationship = None
        if subtype == "Link" or target:
            relationship = RelationshipRecord(
                source_part=source_part,
                relationship_id=relationship_id,
                relationship_type=relationship_type,
                target=target,
                target_mode="external" if uri else "internal" if target else None,
                resolved_target=None,
            )
            relationships.append(relationship)
        detail = {
            "annotation_id": relationship_id,
            "subtype": subtype,
            "object_ref": _object_ref(raw_annotation) or _object_ref(annotation),
            "rect_pdf": raw_box,
            "bbox_points": coordinates["bbox_points"],
            "bbox": coordinates["bbox"],
            "action": action_type,
            "target": target or None,
            "destination": destination_value,
            "javascript": action_type == "JavaScript",
        }
        if annotation.get("/Contents") is not None:
            detail["contents"] = _text(annotation.get("/Contents"))
        if annotation.get("/T") is not None:
            detail["title"] = _text(annotation.get("/T"))
        object_relationships = [f"{source_part}:{relationship_id}"] if relationship else []
        objects.append(
            {
                "id": annotation_id,
                "bbox": coordinates["bbox"],
                "text": _text(annotation.get("/Contents")),
                "shape_type": f"PDF_{subtype.upper()}",
                "native_pdf": detail,
                "relationships": object_relationships,
            }
        )
        if subtype == "Link":
            hyperlinks.append(
                {
                    "kind": "link",
                    "annotation_id": relationship_id,
                    "target": uri or None,
                    "resolved_target": None,
                    "action": action_type,
                    "destination": destination_value,
                    "rect_pdf": raw_box,
                    "bbox": coordinates["bbox"],
                }
            )
        if subtype in {"Text", "FreeText", "Stamp", "Caret", "FileAttachment", "Highlight", "Underline", "Squiggly", "StrikeOut"}:
            comment_text = _text(annotation.get("/Contents"))
            if comment_text:
                comments.append(
                    {
                        "page": page_number,
                        "subtype": subtype,
                        "author": _text(annotation.get("/T")) or None,
                        "text": comment_text,
                        "bbox": coordinates["bbox"],
                    }
                )
    return objects, hyperlinks, relationships, comments


def _outline_items(root: Any, reader: Any) -> list[dict[str, Any]]:
    outlines_ref = root.get("/Outlines") if isinstance(root, Mapping) else None
    reference = _object_ref(outlines_ref)
    if reference is None or not _reader_has_object(reader, reference):
        return []
    outlines = _resolve(outlines_ref)
    if not isinstance(outlines, Mapping):
        return []
    result: list[dict[str, Any]] = []
    visited: set[str] = set()

    def walk(first: Any, level: int) -> None:
        current = first
        while current is not None:
            current_ref = _reference_key(current)
            if current_ref and current_ref in visited:
                return
            if current_ref:
                visited.add(current_ref)
            item = _resolve(current)
            if not isinstance(item, Mapping):
                return
            action = _resolve(item.get("/A"))
            destination = item.get("/Dest")
            action_type = _name(action.get("/S")) if isinstance(action, Mapping) else None
            if destination is None and isinstance(action, Mapping):
                destination = action.get("/D")
            result.append(
                {
                    "title": _text(item.get("/Title")),
                    "level": level,
                    "destination": _pdf_value(destination) if destination is not None else None,
                    "action": action_type,
                    "target": _text(action.get("/URI")) if isinstance(action, Mapping) and action_type == "URI" else None,
                    "object_ref": _object_ref(current),
                }
            )
            child = item.get("/First")
            child_ref = _object_ref(child)
            if child_ref is not None and _reader_has_object(reader, child_ref):
                walk(child, level + 1)
            current = item.get("/Next")

    first = outlines.get("/First")
    if _object_ref(first) is not None and _reader_has_object(reader, _object_ref(first) or {}):
        walk(first, 0)
    return result


def _reader_has_object(reader: Any, reference: Mapping[str, Any]) -> bool:
    object_number = reference.get("object")
    generation = reference.get("generation")
    xref = getattr(reader, "xref", {})
    if not isinstance(object_number, int) or not isinstance(generation, int) or not isinstance(xref, Mapping):
        return True
    generation_objects = xref.get(generation)
    return isinstance(generation_objects, Mapping) and object_number in generation_objects


def _page_labels(root: Any, reader: Any, page_count: int) -> list[dict[str, Any]]:
    labels_root_ref = root.get("/PageLabels") if isinstance(root, Mapping) else None
    reference = _object_ref(labels_root_ref)
    if reference is not None and not _reader_has_object(reader, reference):
        return []
    labels_root = _resolve(labels_root_ref)
    if not isinstance(labels_root, Mapping):
        return []
    entries: list[tuple[int, Mapping[str, Any]]] = []

    def collect(node: Any) -> None:
        resolved = _resolve(node)
        if not isinstance(resolved, Mapping):
            return
        nums = _resolve(resolved.get("/Nums"))
        if isinstance(nums, (list, tuple)):
            values = list(nums)
            for index in range(0, len(values) - 1, 2):
                page_index = _integer(values[index])
                item = _resolve(values[index + 1])
                if page_index is not None and isinstance(item, Mapping) and 0 <= page_index < page_count:
                    entries.append((page_index, item))
        kids = _resolve(resolved.get("/Kids"))
        if isinstance(kids, (list, tuple)):
            for child in kids:
                collect(child)

    collect(labels_root)
    entries.sort(key=lambda item: item[0])
    labels: list[dict[str, Any]] = []
    for page_index in range(page_count):
        matching = [item for item in entries if item[0] <= page_index]
        raw = matching[-1][1] if matching else {}
        start_index = matching[-1][0] if matching else 0
        style = _name(raw.get("/S"))
        start = _integer(raw.get("/St"), 1) or 1
        value = start + page_index - start_index
        prefix = _text(raw.get("/P"))
        labels.append(
            {
                "page": page_index + 1,
                "label": prefix + _page_label_number(value, style),
                "style": style,
                "prefix": prefix,
                "start": start,
            }
        )
    return labels


def _page_label_number(value: int, style: str | None) -> str:
    if style == "A":
        return _alpha_number(value, uppercase=True)
    if style == "a":
        return _alpha_number(value, uppercase=False)
    if style == "R":
        return _roman_number(value).upper()
    if style == "r":
        return _roman_number(value).lower()
    return str(value) if style in {"D", None} else ""


def _alpha_number(value: int, uppercase: bool) -> str:
    if value <= 0:
        return ""
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr((65 if uppercase else 97) + remainder) + result
    return result


def _roman_number(value: int) -> str:
    if value <= 0:
        return ""
    result = []
    for number, numeral in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        count, value = divmod(value, number)
        result.append(numeral * count)
    return "".join(result)


def _embedded_files(root: Any) -> list[dict[str, Any]]:
    names = _resolve(root.get("/Names")) if isinstance(root, Mapping) else None
    embedded_root = _resolve(names.get("/EmbeddedFiles")) if isinstance(names, Mapping) else None
    if not isinstance(embedded_root, Mapping):
        return []
    pairs: list[tuple[str, Any]] = []

    def collect(node: Any) -> None:
        resolved = _resolve(node)
        if not isinstance(resolved, Mapping):
            return
        values = _resolve(resolved.get("/Names"))
        if isinstance(values, (list, tuple)):
            values = list(values)
            for index in range(0, len(values) - 1, 2):
                pairs.append((_text(values[index]), values[index + 1]))
        kids = _resolve(resolved.get("/Kids"))
        if isinstance(kids, (list, tuple)):
            for child in kids:
                collect(child)

    collect(embedded_root)
    result: list[dict[str, Any]] = []
    for name, raw_file_spec in pairs:
        file_spec = _resolve(raw_file_spec)
        if not isinstance(file_spec, Mapping):
            continue
        display_name = name or _text(file_spec.get("/UF")) or _text(file_spec.get("/F")) or "attachment"
        embedded = _resolve(file_spec.get("/EF"))
        stream = _resolve(embedded.get("/F")) if isinstance(embedded, Mapping) else None
        data = b""
        if stream is not None and callable(getattr(stream, "get_data", None)):
            try:
                data = bytes(stream.get_data())
            except Exception:
                data = b""
        result.append(
            {
                "name": display_name,
                "object_ref": _object_ref(raw_file_spec),
                "size": len(data),
                "sha256": _sha256(data) if data else None,
                "data": data,
            }
        )
    return result


def _security_features(root: Any, pages: Iterable[Any]) -> dict[str, Any]:
    features = {
        "javascript": False,
        "automatic_actions": False,
        "launch_actions": False,
        "external_actions": False,
        "embedded_files": False,
        "rich_media": False,
        "forms": False,
        "signatures": False,
        "signature_count": 0,
        "execution_disabled": True,
    }
    visited: set[str] = set()

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 20:
            return
        reference = _object_ref(value)
        if reference is not None:
            key = reference["reference"]
            if key in visited:
                return
            visited.add(key)
        resolved = _resolve(value)
        if isinstance(resolved, Mapping):
            keys = {_name(key) or "" for key in resolved}
            if keys & {"JS", "JavaScript"}:
                features["javascript"] = True
            if "AA" in keys:
                features["automatic_actions"] = True
            if keys & {"EmbeddedFiles", "EmbeddedFile", "EF"}:
                features["embedded_files"] = True
            if "RichMedia" in keys:
                features["rich_media"] = True
            if keys & {"AcroForm", "FT", "Fields", "Widget"}:
                features["forms"] = True
            if "ByteRange" in keys or _name(resolved.get("/Type")) == "Sig":
                features["signatures"] = True
                features["signature_count"] += _name(resolved.get("/Type")) == "Sig"
            action_type = _name(resolved.get("/S"))
            if action_type == "JavaScript":
                features["javascript"] = True
            if action_type == "Launch":
                features["launch_actions"] = True
            if action_type in {"URI", "GoToR", "SubmitForm", "ImportData"}:
                features["external_actions"] = True
            # Structural page links are scanned separately; avoid malformed
            # parent/tree references and content streams here.
            for key, child in resolved.items():
                if _name(key) in {"Parent", "Pages", "Page", "Contents"}:
                    continue
                walk(child, depth + 1)
        elif isinstance(resolved, (list, tuple)):
            for child in resolved:
                walk(child, depth + 1)

    walk(root)
    for page in pages:
        walk(page)
    return features


def _dependency_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution in ("pypdf", "Pillow"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "not-installed"
    return versions


def _native_source(page_number: int | None = None, object_ref: Mapping[str, Any] | None = None, kind: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "layer": "native_pdf",
        "schema": PDF_NATIVE_SCHEMA,
        "confidence": 1.0,
    }
    if page_number is not None:
        result["pdf_page"] = page_number
    if object_ref is not None:
        result["pdf_object"] = dict(object_ref)
    if kind:
        result["pdf_object_type"] = kind
    return result


def _object_record(
    object_id: str,
    slide_id: str,
    object_type: str,
    shape_type: str,
    coordinates: Mapping[str, Any],
    page_number: int,
    text: str = "",
    *,
    style: Mapping[str, Any] | None = None,
    native_pdf: Mapping[str, Any] | None = None,
    relationships: Iterable[str] = (),
    z_order: int = 0,
) -> dict[str, Any]:
    style_value = dict(style or {})
    result: dict[str, Any] = {
        "id": object_id,
        "slide_id": slide_id,
        "parent_id": None,
        "type": object_type,
        "shape_type": shape_type,
        "bbox": list(coordinates["bbox"]),
        "z_order": z_order,
        "text": text,
        "style": style_value,
        "geometry": {
            "coordinate_space": "pdf_user_space_bottom_left",
            "bbox_pdf": list(coordinates["bbox_pdf"]),
            "bbox_points": list(coordinates["bbox_points"]),
        },
        "raw_style": dict(style_value),
        "resolved_style": dict(style_value),
        "inherited_from": [],
        "semantic_status": "extracted",
        "relationships": list(relationships),
        "source": _native_source(page_number, None, object_type),
    }
    if native_pdf is not None:
        result["native_pdf"] = dict(native_pdf)
        result["source"] = _native_source(page_number, native_pdf.get("object_ref"), object_type)
    return result


def _asset_extension(content_type: str) -> str:
    return _MIME_EXTENSIONS.get(content_type, "bin")


def _add_asset(
    record: Mapping[str, Any],
    page_number: int,
    assets: list[dict[str, Any]],
    media: list[MediaRecord],
    asset_bytes_by_id: dict[str, bytes],
    asset_by_key: dict[str, str],
) -> str:
    key = str(record["reference_key"])
    existing = asset_by_key.get(key)
    if existing is not None:
        return existing
    asset_id = f"asset-{len(assets) + 1:04d}"
    content_type = str(record.get("content_type") or "application/octet-stream")
    part = f"pdf/assets/{asset_id}.{_asset_extension(content_type)}"
    data = bytes(record.get("data") or b"")
    object_ref = record.get("object_ref") if isinstance(record.get("object_ref"), Mapping) else None
    native = {
        "kind": record.get("kind", "image_xobject"),
        "page": page_number,
        "object_ref": dict(object_ref) if object_ref else None,
        "resource_name": record.get("resource_name"),
        "width": record.get("width"),
        "height": record.get("height"),
        "filters": list(record.get("filters") or []),
        "color_space": record.get("color_space"),
        "bits_per_component": record.get("bits_per_component"),
        "has_soft_mask": bool(record.get("has_soft_mask", False)),
        "extracted_bytes": bool(data),
    }
    asset = {
        "id": asset_id,
        "type": "embedded" if native["kind"] == "attachment" else "media",
        "part": part,
        "content_type": content_type,
        "size": len(data),
        "sha256": _sha256(data) if data else None,
        "native_pdf": native,
        "source": {
            "layer": "native_pdf",
            "schema": PDF_NATIVE_SCHEMA,
            "pdf_page": page_number,
            "pdf_object": dict(object_ref) if object_ref else None,
            "confidence": 1.0,
        },
    }
    assets.append(asset)
    asset_by_key[key] = asset_id
    if data:
        asset_bytes_by_id[asset_id] = data
    if asset["type"] == "media":
        media.append(MediaRecord(part=part, content_type=content_type, size=len(data), sha256=asset["sha256"] or ""))
    return asset_id


def _read_pdf(source: str | Path, max_bytes: int) -> tuple[Path, bytes, str]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ExtractionError("PDF size limit must be a positive integer")
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise ExtractionError(f"PDF file does not exist: {source_path}")
    try:
        size = source_path.stat().st_size
        if size > max_bytes:
            raise ExtractionError(f"PDF file exceeds the configured size limit: {size} > {max_bytes} bytes")
        data = source_path.read_bytes()
    except ExtractionError:
        raise
    except OSError as exc:
        raise ExtractionError(f"Could not read PDF file: {source_path}: {exc}") from exc
    match = _PDF_HEADER.match(data)
    if match is None:
        raise ExtractionError(f"Not a PDF file: {source_path}")
    return source_path, data, match.group(1).decode("ascii")


def extract_pdf(
    source: str | Path,
    evidence_dir: str | Path | None = None,
    *,
    include_visual_evidence: bool = True,
    include_native_diagrams: bool = True,
    password: str | None = None,
    max_bytes: int = MAX_PDF_BYTES,
    max_pages: int = MAX_PDF_PAGES,
    max_objects: int = MAX_PDF_OBJECTS,
) -> ExtractionReport:
    """Extract a PDF without executing actions or replacing native content."""
    for name, value in (("max_pages", max_pages), ("max_objects", max_objects)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ExtractionError(f"PDF {name} limit must be a positive integer")
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs
        raise ExtractionError("PDF support requires the pypdf package") from exc

    source_path, source_bytes, header_version = _read_pdf(source, max_bytes)
    warnings: list[str] = []
    try:
        reader = PdfReader(BytesIO(source_bytes), strict=False)
    except Exception as exc:
        raise ExtractionError(f"Could not parse PDF file: {source_path}: {exc}") from exc

    encrypted = bool(getattr(reader, "is_encrypted", False))
    decrypted = not encrypted
    if encrypted:
        if password is None:
            raise ExtractionError("Encrypted PDF requires an explicit password")
        try:
            decrypted = bool(reader.decrypt(password))
        except Exception as exc:
            raise ExtractionError(f"Could not decrypt PDF file: {exc}") from exc
        if not decrypted:
            raise ExtractionError("The supplied PDF password was rejected")
        warnings.append("PDF was decrypted with an explicit password; the password is not retained")

    try:
        pages = reader.pages
        page_count = len(pages)
    except Exception as exc:
        raise ExtractionError(f"Could not enumerate PDF pages: {exc}") from exc
    if page_count < 1:
        raise ExtractionError("PDF has no pages")
    if page_count > max_pages:
        raise ExtractionError(f"PDF page count exceeds the configured limit: {page_count} > {max_pages}")

    try:
        trailer = reader.trailer
        root_ref = trailer.get("/Root") if isinstance(trailer, Mapping) else None
        root = _resolve(root_ref)
    except Exception as exc:
        raise ExtractionError(f"Could not read PDF catalog: {exc}") from exc
    if not isinstance(root, Mapping):
        raise ExtractionError("PDF has no readable catalog")

    metadata: dict[str, Any] = {}
    try:
        raw_metadata = reader.metadata
        if isinstance(raw_metadata, Mapping):
            metadata = {str(key): _text(value) for key, value in raw_metadata.items() if value is not None}
    except Exception as exc:
        warnings.append(f"PDF metadata could not be read: {exc}")

    security = _security_features(root, pages)
    security.update(
        {
            "encrypted": encrypted,
            "decrypted": decrypted,
            "password_supplied": password is not None,
            "header_version": header_version,
        }
    )
    if security["javascript"] or security["launch_actions"] or security["external_actions"]:
        warnings.append("PDF actions were recorded as inert metadata and were never executed")
    if security["signatures"]:
        warnings.append("PDF signatures were detected but not cryptographically verified")

    try:
        page_labels = _page_labels(root, reader, page_count)
    except Exception as exc:
        warnings.append(f"PDF page labels could not be read: {exc}")
        page_labels = []
    try:
        outlines = _outline_items(root, reader)
    except Exception as exc:
        warnings.append(f"PDF outlines could not be read: {exc}")
        outlines = []
    try:
        attachments = _embedded_files(root)
    except Exception as exc:
        warnings.append(f"PDF embedded files could not be read: {exc}")
        attachments = []
    if attachments:
        security["embedded_files"] = True

    assets: list[dict[str, Any]] = []
    media: list[MediaRecord] = []
    asset_bytes_by_id: dict[str, bytes] = {}
    asset_by_key: dict[str, str] = {}
    relationships: list[RelationshipRecord] = []
    comments: list[dict[str, Any]] = []
    slides: list[SlideRecord] = []
    canonical_slides: list[dict[str, Any]] = []
    canonical_objects: list[dict[str, Any]] = []
    page_native_records: list[dict[str, Any]] = []
    page_sizes: list[list[float]] = []

    for page_index in range(page_count):
        page_number = page_index + 1
        try:
            page = pages[page_index]
            geometry = _page_geometry(page, page_number)
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError(f"Could not read PDF page {page_number}: {exc}") from exc
        slide_id = f"slide-{page_number:02d}"
        page_sizes.append(list(geometry["display_size_points"]))
        full_text, spans = _text_spans(page, page_number, geometry, warnings)
        image_records = _image_records(page, page_number, warnings)
        image_occurrences, vector_records, operator_counts = _content_records(
            page,
            reader,
            page_number,
            geometry,
            set(image_records),
            warnings,
        )
        occurrences_by_resource: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for occurrence in image_occurrences:
            occurrences_by_resource[occurrence["resource_name"]].append(occurrence)

        page_objects: list[dict[str, Any]] = []
        object_order = 0
        for span_index, span in enumerate(spans, 1):
            object_order += 1
            coordinates = {key: span[key] for key in ("bbox", "bbox_pdf", "bbox_points")}
            style = {
                key: value
                for key, value in {
                    "font_family": span.get("font"),
                    "font_size_pt": span.get("font_size"),
                }.items()
                if value not in (None, "")
            }
            native = {
                "kind": "text_span",
                "page": page_number,
                "text": span["text"],
                "font": span.get("font"),
                "font_size": span.get("font_size"),
                "text_matrix": span.get("text_matrix"),
                "ctm": span.get("ctm"),
                "text_transform": span.get("text_transform"),
                "bbox_pdf": span.get("bbox_pdf"),
                "bbox_basis": span.get("bbox_basis"),
                "object_ref": None,
            }
            item = _object_record(
                f"{slide_id}-text-{span_index:04d}",
                slide_id,
                "text",
                "PDF_TEXT_SPAN",
                coordinates,
                page_number,
                span["text"],
                style=style,
                native_pdf=native,
                z_order=object_order,
            )
            page_objects.append(item)

        for resource_name, image_record in sorted(image_records.items()):
            asset_id = _add_asset(
                image_record,
                page_number,
                assets,
                media,
                asset_bytes_by_id,
                asset_by_key,
            )
            image_record["asset_id"] = asset_id
            occurrences = occurrences_by_resource.get(resource_name) or [None]
            for occurrence_index, occurrence in enumerate(occurrences, 1):
                object_order += 1
                if occurrence is None:
                    coordinates = {
                        "bbox": [0.0, 0.0, 0.0, 0.0],
                        "bbox_pdf": [0.0, 0.0, 0.0, 0.0],
                        "bbox_points": [0.0, 0.0, 0.0, 0.0],
                    }
                    displayed = False
                    placement = None
                else:
                    coordinates = occurrence
                    displayed = True
                    placement = occurrence
                native = {
                    "kind": "image_xobject",
                    "page": page_number,
                    "resource_name": resource_name,
                    "object_ref": image_record.get("object_ref"),
                    "displayed": displayed,
                    "placement": placement,
                    "width": image_record.get("width"),
                    "height": image_record.get("height"),
                    "filters": image_record.get("filters", []),
                    "color_space": image_record.get("color_space"),
                    "bits_per_component": image_record.get("bits_per_component"),
                    "has_soft_mask": image_record.get("has_soft_mask", False),
                }
                item = _object_record(
                    f"{slide_id}-image-{len([obj for obj in page_objects if obj.get('type') == 'image']) + 1:04d}",
                    slide_id,
                    "image",
                    "PDF_IMAGE_XOBJECT",
                    coordinates,
                    page_number,
                    style={"rotation_degrees": 0.0},
                    native_pdf=native,
                    z_order=object_order,
                )
                item["asset_id"] = asset_id
                page_objects.append(item)

        for vector_index, vector in enumerate(vector_records, 1):
            object_order += 1
            coordinates = _coordinate_box(vector["bbox_pdf"], geometry)
            native = {
                "kind": "content_path",
                "page": page_number,
                "operator": vector["operator"],
                "operator_index": vector["operator_index"],
                "segments": vector["segments"],
                "bbox_pdf": vector["bbox_pdf"],
                "object_ref": None,
            }
            page_objects.append(
                _object_record(
                    f"{slide_id}-vector-{vector_index:04d}",
                    slide_id,
                    "vector",
                    f"PDF_PATH_{vector['operator']}",
                    coordinates,
                    page_number,
                    native_pdf=native,
                    z_order=object_order,
                )
            )

        annotation_objects, hyperlinks, page_relationships, page_comments = _annotation_records(
            page,
            page_number,
            slide_id,
            geometry,
            warnings,
        )
        for annotation in annotation_objects:
            object_order += 1
            native = annotation.get("native_pdf", {})
            coordinates = {
                "bbox": annotation.get("bbox", [0.0, 0.0, 0.0, 0.0]),
                "bbox_pdf": native.get("rect_pdf", [0.0, 0.0, 0.0, 0.0]),
                "bbox_points": native.get("bbox_points", [0.0, 0.0, 0.0, 0.0]),
            }
            page_objects.append(
                _object_record(
                    annotation["id"],
                    slide_id,
                    "annotation",
                    annotation["shape_type"],
                    coordinates,
                    page_number,
                    annotation.get("text", ""),
                    native_pdf=native,
                    relationships=annotation.get("relationships", []),
                    z_order=object_order,
                )
            )
        relationships.extend(page_relationships)
        comments.extend(page_comments)

        if len(canonical_objects) + len(page_objects) > max_objects:
            raise ExtractionError(f"PDF object count exceeds the configured limit: > {max_objects}")
        canonical_objects.extend(page_objects)
        page_native = {
            **geometry,
            "text": {
                "extraction": "pypdf",
                "text": full_text,
                "line_count": len(full_text.splitlines()),
                "span_count": len(spans),
            },
            "content": {
                "operator_counts": operator_counts,
                "vector_path_count": len(vector_records),
                "image_placement_count": len(image_occurrences),
            },
            "annotations": [
                {
                    "kind": item.get("shape_type"),
                    "bbox": item.get("bbox"),
                    "relationship_ids": item.get("relationships", []),
                }
                for item in annotation_objects
            ],
            "page_label": page_labels[page_index] if page_index < len(page_labels) else None,
        }
        page_native_records.append(page_native)
        slide_part = f"pdf:page-{page_number:02d}"
        text_lines = full_text.splitlines() if full_text else []
        slides.append(
            SlideRecord(
                number=page_number,
                part=slide_part,
                layout_part=None,
                master_part=None,
                theme_part=None,
                text=text_lines,
                hyperlinks=hyperlinks,
            )
        )
        canonical_slides.append(
            {
                "id": slide_id,
                "number": page_number,
                "part": slide_part,
                "layout_part": None,
                "master_part": None,
                "theme_part": None,
                "text": text_lines,
                "notes": [],
                "hyperlinks": hyperlinks,
                "alt_text": [],
                "animations": [],
                "object_ids": [item["id"] for item in page_objects],
                "slide_reading_order": "unknown",
                "diagram_flow_direction": "unknown",
                "flow_present": None,
                "flow_presence_basis": "undetermined",
                "visual_region_ids": [],
                "visual_evidence_visibility": {
                    "native": "verified",
                    "rendered": "not_requested",
                    "ocr": "not_requested",
                    "vision": "not_requested",
                },
                "source": _native_source(page_number, geometry.get("object_ref"), "page"),
                "native_pdf": page_native,
            }
        )

    attachment_metadata: list[dict[str, Any]] = []
    for attachment in attachments:
        record = {
            **attachment,
            "kind": "attachment",
            "reference_key": attachment.get("object_ref", {}).get("reference") if isinstance(attachment.get("object_ref"), Mapping) else f"attachment:{attachment.get('name')}",
            "content_type": "application/octet-stream",
            "width": None,
            "height": None,
            "filters": [],
            "color_space": None,
            "bits_per_component": None,
            "has_soft_mask": False,
            "resource_name": attachment.get("name"),
        }
        asset_id = _add_asset(record, 0, assets, media, asset_bytes_by_id, asset_by_key)
        attachment_metadata.append(
            {
                "name": attachment.get("name"),
                "asset_id": asset_id,
                "size": attachment.get("size", 0),
                "sha256": attachment.get("sha256"),
                "object_ref": attachment.get("object_ref"),
            }
        )

    canonical_relationships = [
        {
            "id": f"{relation.source_part}:{relation.relationship_id}",
            "source_part": relation.source_part,
            "relationship_id": relation.relationship_id,
            "relationship_type": relation.relationship_type,
            "target": relation.target,
            "target_mode": relation.target_mode,
            "resolved_target": relation.resolved_target,
            "source": _native_source(
                _page_number_from_part(relation.source_part),
                None,
                "annotation",
            ),
        }
        for relation in relationships
    ]
    canonical_part = PartRecord(
        name="document.pdf",
        content_type="application/pdf",
        size=len(source_bytes),
        compressed_size=len(source_bytes),
        crc32="",
        sha256=_sha256(source_bytes),
        is_xml=False,
    )
    first_size = page_sizes[0]
    pdf_metadata = {
        "schema": PDF_NATIVE_SCHEMA,
        "version": header_version,
        "header": f"%PDF-{header_version}",
        "metadata": metadata,
        "security": security,
        "page_count": page_count,
        "page_sizes_points": page_sizes,
        "page_labels": page_labels,
        "outlines": outlines,
        "attachments": attachment_metadata,
        "catalog": {
            "object_ref": _object_ref(root_ref),
            "keys": sorted(str(key) for key in root.keys()),
        },
        "page_records": page_native_records,
    }
    canonical_assets = assets
    canonical = DeckIR(
        deck={
            "id": "deck",
            "schema": DECKIR_SCHEMA,
            "schema_version": DECKIR_SCHEMA_VERSION,
            "name": source_path.name,
            "source": source_path.name,
            "source_sha256": _sha256(source_bytes),
            "source_format": "pdf",
            "slide_count": page_count,
            "page_count": page_count,
            "slide_size_emu": [_round(first_size[0] * 12_700), _round(first_size[1] * 12_700)],
            "page_size_points": first_size,
            "slide_aspect_ratio": _round(first_size[0] / first_size[1], 12) if first_size[1] else None,
            "package_part_count": 1,
            "pdf": pdf_metadata,
        },
        slides=canonical_slides,
        objects=canonical_objects,
        assets=canonical_assets,
        relationships=canonical_relationships,
        rendered_evidence=[],
        ocr_evidence=[],
        vision_evidence=[],
        warnings=warnings,
        provenance={
            "schema": DECKIR_SCHEMA,
            "schema_version": DECKIR_SCHEMA_VERSION,
            "native_layer": "native_pdf",
            "source_format": "pdf",
            "parser_version": PDF_PARSER_VERSION,
            "adapter": "pypdf",
            "dependencies": _dependency_versions(),
            "source_name": source_path.name,
            "source_sha256": _sha256(source_bytes),
            "package_parts": [
                {
                    "name": canonical_part.name,
                    "content_type": canonical_part.content_type,
                    "size": canonical_part.size,
                    "sha256": canonical_part.sha256,
                }
            ],
            "authority": {
                "native_pdf": "authoritative",
                "native_ooxml": "not_applicable",
                "rendered_cv": "derived",
                "ocr": "derived",
                "vision_model": "probabilistic",
            },
            "security": security,
        },
    )
    if include_visual_evidence:
        from .visual import add_native_visual_evidence

        bytes_by_part = {
            asset["part"]: asset_bytes_by_id[asset["id"]]
            for asset in canonical.assets
            if asset.get("id") in asset_bytes_by_id
        }
        add_native_visual_evidence(canonical, bytes_by_part)
    # PDF vector paths are already retained as native objects. The OOXML
    # SmartArt/chart reconstruction branch is intentionally not applied.
    canonical.validate()

    report = ExtractionReport(
        source=str(source_path),
        source_sha256=_sha256(source_bytes),
        parser_version=PDF_PARSER_VERSION,
        dependencies=_dependency_versions(),
        package_parts=[canonical_part],
        relationships=relationships,
        slides=slides,
        media=media,
        comments=comments,
        convenience={
            "adapter": "pdf",
            "pdf": pdf_metadata,
            "page_sizes_points": page_sizes,
        },
        warnings=warnings,
        canonical=canonical,
        asset_bytes_by_id=asset_bytes_by_id,
    )
    if evidence_dir is not None:
        destination = Path(evidence_dir).expanduser().resolve()
        try:
            (destination / "parts").mkdir(parents=True, exist_ok=True)
            (destination / "assets").mkdir(exist_ok=True)
            (destination / "pages").mkdir(exist_ok=True)
            (destination / "original.pdf").write_bytes(source_bytes)
            (destination / "parts" / canonical_part.name).write_bytes(source_bytes)
            for asset in canonical.assets:
                data = asset_bytes_by_id.get(asset["id"])
                if data is not None:
                    (destination / asset["part"]).parent.mkdir(parents=True, exist_ok=True)
                    (destination / asset["part"]).write_bytes(data)
            for page_record in page_native_records:
                page_number = page_record["page_number"]
                (destination / "pages" / f"page-{page_number:02d}.json").write_text(
                    json.dumps(page_record, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8",
                )
        except (OSError, TypeError, ValueError) as exc:
            raise ExtractionError(f"Could not write PDF evidence bundle: {destination}: {exc}") from exc
        report.evidence_dir = str(destination)
    return report


def _page_number_from_part(value: str) -> int | None:
    match = re.search(r"page-(\d+)$", value)
    return int(match.group(1)) if match else None
