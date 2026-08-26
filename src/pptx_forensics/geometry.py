"""Native geometry extraction for PresentationML shapes and groups."""

from __future__ import annotations

from typing import Any


Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(root: Any | None, name: str) -> Any | None:
    if root is None:
        return None
    return next((item for item in list(root) if _local_name(item.tag) == name), None)


def _descendant(root: Any | None, name: str) -> Any | None:
    if root is None:
        return None
    return next((item for item in root.iter() if _local_name(item.tag) == name), None)


def _value(element: Any | None, attribute: str, default: float = 0.0) -> float:
    if element is None or element.get(attribute) is None:
        return default
    try:
        return float(element.get(attribute))
    except (TypeError, ValueError):
        return default


def _bool(element: Any | None, attribute: str) -> bool:
    return bool(element is not None and element.get(attribute) in {"1", "true", "on"})


def _multiply(outer: Matrix, inner: Matrix) -> Matrix:
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


def _apply(matrix: Matrix, point: tuple[float, float]) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    x, y = point
    return a * x + c * y + e, b * x + d * y + f


def _translation(x: float, y: float) -> Matrix:
    return (1.0, 0.0, 0.0, 1.0, x, y)


def _scale(x: float, y: float) -> Matrix:
    return (x, 0.0, 0.0, y, 0.0, 0.0)


def _rotation(degrees: float) -> Matrix:
    import math

    radians = math.radians(degrees)
    cosine, sine = math.cos(radians), math.sin(radians)
    return (cosine, sine, -sine, cosine, 0.0, 0.0)


def _transform(element: Any | None) -> Any | None:
    if element is None:
        return None
    direct = _child(element, "xfrm")
    if direct is not None:
        return direct
    for name in ("spPr", "grpSpPr"):
        properties = _child(element, name)
        direct = _child(properties, "xfrm")
        if direct is not None:
            return direct
    return None


def _transform_values(element: Any | None) -> dict[str, Any] | None:
    xfrm = _transform(element)
    if xfrm is None:
        return None
    offset = _child(xfrm, "off")
    extent = _child(xfrm, "ext")
    values: dict[str, Any] = {
        "offset_emu": [_value(offset, "x"), _value(offset, "y")],
        "extent_emu": [_value(extent, "cx"), _value(extent, "cy")],
        "rotation_degrees": round(_value(xfrm, "rot") / 60_000.0, 12),
        "flip_h": _bool(xfrm, "flipH"),
        "flip_v": _bool(xfrm, "flipV"),
    }
    # PresentationML stores group child offsets and extents as attributes on
    # separate elements.
    child_offset_element = _child(xfrm, "chOff")
    child_extent_element = _child(xfrm, "chExt")
    if child_offset_element is not None:
        values["child_offset_emu"] = [_value(child_offset_element, "x"), _value(child_offset_element, "y")]
    if child_extent_element is not None:
        values["child_extent_emu"] = [_value(child_extent_element, "cx"), _value(child_extent_element, "cy")]
    return values


def _box_matrix(values: dict[str, Any]) -> Matrix:
    x, y = values["offset_emu"]
    width, height = values["extent_emu"]
    center_x, center_y = x + width / 2.0, y + height / 2.0
    reflection = _scale(-1.0 if values["flip_h"] else 1.0, -1.0 if values["flip_v"] else 1.0)
    return _multiply(
        _translation(center_x, center_y),
        _multiply(
            _rotation(values["rotation_degrees"]),
            _multiply(reflection, _multiply(_translation(-center_x, -center_y), _multiply(_translation(x, y), _scale(width, height)))),
        ),
    )


def _group_matrix(values: dict[str, Any]) -> Matrix:
    x, y = values["offset_emu"]
    width, height = values["extent_emu"]
    child_offset = values.get("child_offset_emu", [0.0, 0.0])
    child_extent = values.get("child_extent_emu", [width, height])
    scale_x = width / child_extent[0] if child_extent[0] else 1.0
    scale_y = height / child_extent[1] if child_extent[1] else 1.0
    mapping = _multiply(_translation(x, y), _multiply(_scale(scale_x, scale_y), _translation(-child_offset[0], -child_offset[1])))
    center_x, center_y = x + width / 2.0, y + height / 2.0
    reflection = _scale(-1.0 if values["flip_h"] else 1.0, -1.0 if values["flip_v"] else 1.0)
    return _multiply(
        _translation(center_x, center_y),
        _multiply(
            _rotation(values["rotation_degrees"]),
            _multiply(reflection, _multiply(_translation(-center_x, -center_y), mapping)),
        ),
    )


def child_transform_matrix(element: Any, parent_matrix: Matrix = IDENTITY) -> Matrix:
    values = _transform_values(element)
    if values is None:
        return parent_matrix
    return _multiply(parent_matrix, _group_matrix(values))


def _round_point(point: tuple[float, float]) -> list[float]:
    return [round(point[0], 6), round(point[1], 6)]


def _bbox(matrix: Matrix) -> list[float]:
    points = [_apply(matrix, point) for point in ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))]
    left = min(point[0] for point in points)
    top = min(point[1] for point in points)
    right = max(point[0] for point in points)
    bottom = max(point[1] for point in points)
    return [left, top, right - left, bottom - top]


def _crop(element: Any) -> dict[str, float] | None:
    source = _descendant(element, "srcRect")
    if source is None:
        return None
    attributes = {"left": "l", "top": "t", "right": "r", "bottom": "b"}
    return {key: round(_value(source, attribute) / 100_000.0, 6) for key, attribute in attributes.items()}


def _placeholder(element: Any) -> dict[str, str] | None:
    placeholder = _descendant(element, "ph")
    if placeholder is None:
        return None
    result = {}
    if placeholder.get("type") is not None:
        result["type"] = placeholder.get("type")
    if placeholder.get("idx") is not None:
        result["idx"] = placeholder.get("idx")
    return result or None


def _connector(element: Any, matrix: Matrix) -> dict[str, Any] | None:
    if _local_name(element.tag) != "cxnSp":
        return None
    properties = _child(element, "spPr")
    line = _child(properties, "ln")
    result: dict[str, Any] = {
        "start_emu": _round_point(_apply(matrix, (0.0, 0.0))),
        "end_emu": _round_point(_apply(matrix, (1.0, 1.0))),
    }
    connector_properties = _child(_child(element, "nvCxnSpPr"), "cNvCxnSpPr")
    if connector_properties is not None:
        for name, key in (("stCxn", "start_connection"), ("endCxn", "end_connection")):
            connection = _child(connector_properties, name)
            if connection is not None:
                result[key] = {attribute: connection.get(attribute) for attribute in ("id", "idx") if connection.get(attribute) is not None}
    if line is not None:
        for name, key in (("headEnd", "begin_arrow"), ("tailEnd", "end_arrow")):
            arrow = _child(line, name)
            if arrow is not None:
                result[key] = {attribute: arrow.get(attribute) for attribute in ("type", "w", "len") if arrow.get(attribute) is not None}
    return result


def geometry_for_object(
    element: Any,
    slide_width: float,
    slide_height: float,
    parent_matrix: Matrix = IDENTITY,
    transform_chain: list[str] | None = None,
    fallback_element: Any | None = None,
) -> tuple[list[float], dict[str, Any]]:
    values = _transform_values(element)
    transform_source = "shape"
    if values is None and fallback_element is not None:
        values = _transform_values(fallback_element)
        transform_source = "inherited_placeholder"
    matrix = parent_matrix if values is None else _multiply(parent_matrix, _box_matrix(values))
    emu_bbox = _bbox(matrix) if values is not None else [0.0, 0.0, 0.0, 0.0]
    normalized = [
        round(emu_bbox[0] / slide_width, 12),
        round(emu_bbox[1] / slide_height, 12),
        round(emu_bbox[2] / slide_width, 12),
        round(emu_bbox[3] / slide_height, 12),
    ]
    geometry: dict[str, Any] = {
        "bbox_emu": [round(value, 6) for value in emu_bbox],
        "transform": values,
        "transform_source": transform_source if values is not None else None,
        "transform_chain": list(transform_chain or []),
    }
    crop = _crop(element)
    if crop is not None:
        geometry["crop"] = crop
    placeholder = _placeholder(element)
    if placeholder is not None:
        geometry["placeholder"] = placeholder
    connector = _connector(element, matrix)
    if connector is not None:
        geometry["connector"] = connector
    return normalized, geometry
