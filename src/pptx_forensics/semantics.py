"""Native PresentationML style resolution and semantic projections."""

from __future__ import annotations

from typing import Any, Iterable

from .models import RelationshipRecord


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(root: Any, name: str) -> list[Any]:
    return [item for item in list(root) if _local_name(item.tag) == name]


def _child(root: Any, name: str) -> Any | None:
    return next(iter(_children(root, name)), None)


def _descendants(root: Any, name: str) -> list[Any]:
    return [item for item in root.iter() if _local_name(item.tag) == name]


def _first(root: Any, name: str) -> Any | None:
    return next(iter(_descendants(root, name)), None)


def _text(root: Any) -> str:
    return " ".join(item.text.strip() for item in _descendants(root, "t") if item.text and item.text.strip())


def _chart_text(root: Any | None) -> str:
    if root is None:
        return ""
    values = [item.text.strip() for item in root.iter() if _local_name(item.tag) in {"t", "v"} and item.text and item.text.strip()]
    return " ".join(values)


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value in {"1", "true", "on"}


def _number(value: str | None, divisor: float = 1.0) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / divisor
    except ValueError:
        return None


def _theme_colors(theme: Any | None) -> dict[str, str]:
    if theme is None:
        return {}
    scheme = _first(theme, "clrScheme")
    if scheme is None:
        return {}
    colors: dict[str, str] = {}
    for item in list(scheme):
        color = next((child for child in list(item) if _local_name(child.tag) in {"srgbClr", "sysClr", "scrgbClr"}), None)
        if color is not None:
            colors[_local_name(item.tag)] = color.get("lastClr") or color.get("val", "")
    colors.update({"tx1": colors.get("dk1", ""), "tx2": colors.get("dk2", ""), "bg1": colors.get("lt1", ""), "bg2": colors.get("lt2", "")})
    return {key: value for key, value in colors.items() if value}


def _theme_fonts(theme: Any | None) -> dict[str, str]:
    if theme is None:
        return {}
    scheme = _first(theme, "fontScheme")
    if scheme is None:
        return {}
    fonts: dict[str, str] = {}
    for name, key in (("majorFont", "major"), ("minorFont", "minor")):
        item = _child(scheme, name)
        latin = _child(item, "latin") if item is not None else None
        if latin is not None and latin.get("typeface"):
            fonts[key] = latin.get("typeface")
    return fonts


def _color(fill: Any | None, theme_colors: dict[str, str], raw: bool = False) -> dict[str, Any] | None:
    if fill is None:
        return None
    color = next((item for item in fill.iter() if _local_name(item.tag) in {"srgbClr", "schemeClr", "sysClr", "prstClr", "scrgbClr"}), None)
    if color is None:
        return {"kind": _local_name(fill.tag)}
    kind = _local_name(color.tag)
    value = color.get("val", "")
    resolved = value
    if kind == "schemeClr":
        resolved = theme_colors.get(value, value)
    alpha = _first(color, "alpha")
    alpha_value = _number(alpha.get("val") if alpha is not None else None, 100_000)
    result: dict[str, Any] = {"kind": "solid", "color": value if raw else resolved, "source": kind}
    if alpha_value is not None:
        result["opacity"] = round(alpha_value, 6)
        result["transparency"] = round(1.0 - alpha_value, 6)
    if kind == "schemeClr" and not raw:
        result["scheme"] = value
    return result


def _fill(sp_properties: Any | None, theme_colors: dict[str, str], raw: bool = False) -> dict[str, Any] | None:
    if sp_properties is None:
        return None
    for name in ("noFill", "solidFill", "gradFill", "pattFill", "blipFill"):
        item = _child(sp_properties, name)
        if item is not None:
            if name == "noFill":
                return {"kind": "none"}
            result = _color(item, theme_colors, raw)
            return result or {"kind": name}
    return None


def _line(sp_properties: Any | None, theme_colors: dict[str, str], raw: bool = False) -> dict[str, Any] | None:
    if sp_properties is None:
        return None
    line = _child(sp_properties, "ln")
    if line is None:
        return None
    result: dict[str, Any] = {}
    if line.get("w"):
        result["width_emu"] = line.get("w") if raw else _number(line.get("w"))
    fill = next((item for item in list(line) if _local_name(item.tag) in {"noFill", "solidFill", "gradFill", "pattFill"}), None)
    result["fill"] = _color(fill, theme_colors, raw) if fill is not None else None
    if line.get("cap"):
        result["cap"] = line.get("cap")
    if line.get("cmpd"):
        result["compound"] = line.get("cmpd")
    return result


def _rpr_style(rpr: Any | None, theme_colors: dict[str, str], raw: bool = False) -> dict[str, Any]:
    if rpr is None:
        return {}
    style: dict[str, Any] = {}
    if rpr.get("sz") is not None:
        style["font_size_pt"] = rpr.get("sz") if raw else _number(rpr.get("sz"), 100)
    for attribute, key in (("b", "bold"), ("i", "italic"), ("u", "underline"), ("strike", "strike") ):
        value = _bool(rpr.get(attribute))
        if value is not None:
            style[key] = rpr.get(attribute) if raw else value
    fonts: dict[str, str] = {}
    for child in list(rpr):
        if _local_name(child.tag) in {"latin", "ea", "cs"} and child.get("typeface"):
            fonts[_local_name(child.tag)] = child.get("typeface")
    if fonts:
        style["fonts"] = fonts
        style["font_family"] = fonts.get("latin") or fonts.get("ea") or fonts.get("cs")
    color = next((item for item in list(rpr) if _local_name(item.tag) in {"solidFill", "gradFill"}), None)
    if color is not None:
        style["font_color"] = _color(color, theme_colors, raw)
    return style


def _paragraph_style(tx_body: Any | None, raw: bool = False) -> dict[str, Any]:
    if tx_body is None:
        return {}
    paragraph = _child(tx_body, "p")
    if paragraph is None:
        return {}
    ppr = _child(paragraph, "pPr")
    if ppr is None:
        return {}
    result: dict[str, Any] = {}
    for attribute, key in (("algn", "alignment"), ("marL", "left_indent_emu"), ("marR", "right_indent_emu"), ("indent", "indent_emu")):
        if ppr.get(attribute) is not None:
            result[key] = ppr.get(attribute) if raw or attribute == "algn" else _number(ppr.get(attribute))
    for child_name, key in (("lnSpc", "line_spacing"), ("spcBef", "space_before"), ("spcAft", "space_after")):
        child = _child(ppr, child_name)
        if child is None or not list(child):
            continue
        value = list(child)[0]
        if _local_name(value.tag) == "spcPts":
            result[key] = {"points": value.get("val") if raw else _number(value.get("val"), 100)}
        elif _local_name(value.tag) == "spcPct":
            result[key] = {"percent": value.get("val") if raw else _number(value.get("val"), 1000)}
    return result


def _shape_properties(element: Any | None) -> Any | None:
    return _child(element, "spPr") if element is not None else None


def _text_body(element: Any | None) -> Any | None:
    return _child(element, "txBody") if element is not None else None


def _placeholder(element: Any | None) -> Any | None:
    nv = _first(element, "nvSpPr") if element is not None else None
    return _first(nv, "ph") if nv is not None else _first(element, "ph") if element is not None else None


def _placeholder_match(root: Any | None, placeholder: Any | None) -> Any | None:
    if root is None or placeholder is None:
        return None
    wanted_type = placeholder.get("type", "body")
    wanted_idx = placeholder.get("idx")
    for element in root.iter():
        if _local_name(element.tag) not in {"sp", "pic", "graphicFrame"}:
            continue
        candidate = _placeholder(element)
        if candidate is None or candidate.get("type", "body") != wanted_type:
            continue
        if wanted_idx is not None and candidate.get("idx") != wanted_idx:
            continue
        return element
    return None


def _raw_style(element: Any) -> dict[str, Any]:
    sp_properties = _shape_properties(element)
    tx_body = _text_body(element)
    runs = []
    if tx_body is not None:
        for run in _descendants(tx_body, "r"):
            runs.append(_rpr_style(_child(run, "rPr"), {}, raw=True))
    return {
        "shape": {"fill": _fill(sp_properties, {}, raw=True), "line": _line(sp_properties, {}, raw=True)},
        "paragraph": _paragraph_style(tx_body, raw=True),
        "runs": runs,
    }


def resolve_style(element: Any, layout: Any | None, master: Any | None, theme: Any | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Return raw style, effective style, and field-level inheritance sources."""
    theme_colors = _theme_colors(theme)
    theme_fonts = _theme_fonts(theme)
    placeholder = _placeholder(element)
    layout_placeholder = _placeholder_match(layout, placeholder)
    master_placeholder = _placeholder_match(master, placeholder)
    candidates = [("shape", element), ("layout", layout_placeholder), ("master", master_placeholder)]
    raw = _raw_style(element)
    resolved: dict[str, Any] = {}
    inherited_from: dict[str, str] = {}

    for candidate_source, candidate in candidates:
        if candidate is None:
            continue
        fill = _fill(_shape_properties(candidate), theme_colors)
        line = _line(_shape_properties(candidate), theme_colors)
        if fill is not None and "fill" not in resolved:
            resolved["fill"] = fill
            inherited_from["fill"] = candidate_source
        if line is not None and "line" not in resolved:
            resolved["line"] = line
            inherited_from["line"] = candidate_source
    text_defaults: dict[str, Any] = {}
    text_sources: dict[str, str] = {}
    paragraph: dict[str, Any] = {}
    for candidate_source, candidate in reversed(candidates):
        if candidate is None:
            continue
        tx_body = _text_body(candidate)
        candidate_paragraph = _paragraph_style(tx_body)
        paragraph.update(candidate_paragraph)
        for key in candidate_paragraph:
            text_sources[f"paragraph.{key}"] = candidate_source
        for def_rpr in _descendants(tx_body, "defRPr") if tx_body is not None else []:
            candidate_style = _rpr_style(def_rpr, theme_colors)
            text_defaults.update(candidate_style)
            for key in candidate_style:
                text_sources[key] = candidate_source
    if "font_family" not in text_defaults:
        text_defaults["font_family"] = theme_fonts.get("minor")
        if text_defaults["font_family"]:
            text_sources["font_family"] = "theme"
    run_defaults = dict(text_defaults)
    shape_text = _text_body(element)
    first_run = _first(shape_text, "r") if shape_text is not None else None
    if first_run is not None:
        run_style = _rpr_style(_child(first_run, "rPr"), theme_colors)
        text_defaults.update(run_style)
        for key in run_style:
            text_sources[key] = "shape"
    resolved.update(text_defaults)
    inherited_from.update(text_sources)
    if paragraph:
        resolved["paragraph"] = paragraph
    if shape_text is not None:
        run_font_sizes: list[float] = []
        run_font_colors: list[dict[str, Any]] = []
        for run in _descendants(shape_text, "r"):
            run_style = _rpr_style(_child(run, "rPr"), theme_colors)
            effective = {**run_defaults, **run_style}
            if effective.get("font_size_pt") is not None:
                run_font_sizes.append(effective["font_size_pt"])
            if isinstance(effective.get("font_color"), dict):
                run_font_colors.append(effective["font_color"])
        if run_font_sizes:
            resolved["run_font_sizes_pt"] = run_font_sizes
        if run_font_colors:
            resolved["run_font_colors"] = run_font_colors
    transform = _first(element, "xfrm")
    if transform is not None and transform.get("rot") is not None:
        resolved["rotation_degrees"] = _number(transform.get("rot"), 60_000)
        inherited_from["rotation_degrees"] = "shape"
    return raw, resolved, inherited_from


def _relationship(element: Any, relations: Iterable[RelationshipRecord], suffix: str | None = None) -> RelationshipRecord | None:
    relation_map = {item.relationship_id: item for item in relations}
    for descendant in element.iter():
        for key, value in descendant.attrib.items():
            if not key.partition("}")[0].endswith("relationships"):
                continue
            relation = relation_map.get(value)
            if relation is not None and (suffix is None or relation.relationship_type.endswith(suffix)):
                return relation
    return None


def _cache_values(root: Any | None) -> list[dict[str, Any]]:
    if root is None:
        return []
    values = []
    for point in _descendants(root, "pt"):
        value = _first(point, "v")
        if value is not None:
            values.append({"index": point.get("idx"), "value": value.text or ""})
    return values


def _chart_semantics(element: Any, relations: Iterable[RelationshipRecord], parts: dict[str, bytes]) -> dict[str, Any]:
    relation = _relationship(element, relations, "/chart")
    if relation is None or relation.resolved_target not in parts:
        return {"semantic_status": "unsupported", "unsupported_reason": "chart part relationship is missing"}
    from defusedxml import ElementTree as SafeET

    root = SafeET.fromstring(parts[relation.resolved_target])
    chart_types = [item for item in root.iter() if _local_name(item.tag).endswith("Chart") and _local_name(item.tag) not in {"chart", "chartSpace"}]
    chart_type = _local_name(chart_types[0].tag)[:-5] if chart_types else "unknown"
    series = []
    for series_element in _descendants(root, "ser"):
        title = _chart_text(_first(series_element, "tx"))
        category = _first(series_element, "cat")
        values = _first(series_element, "val")
        series.append({"title": title, "categories": _cache_values(category), "values": _cache_values(values)})
    axes = []
    for axis in root.iter():
        if _local_name(axis.tag) in {"catAx", "dateAx", "valAx", "serAx"}:
            axis_id = _first(axis, "axId")
            axes.append({"type": _local_name(axis.tag), "id": axis_id.get("val") if axis_id is not None else None, "title": _chart_text(_first(axis, "title"))})
    legend = _first(root, "legend")
    return {
        "semantic_status": "extracted",
        "chart_data": {
            "part": relation.resolved_target,
            "type": chart_type,
            "title": _chart_text(_first(root, "title")),
            "legend": _chart_text(legend),
            "axes": axes,
            "series": series,
        },
    }


def _smartart_semantics(element: Any, relations: Iterable[RelationshipRecord], parts: dict[str, bytes]) -> dict[str, Any]:
    relation = _relationship(element, relations, "/diagramData") or _relationship(element, relations, "/diagram")
    if relation is None or relation.resolved_target not in parts:
        return {"semantic_status": "unsupported", "unsupported_reason": "SmartArt data relationship is missing"}
    from defusedxml import ElementTree as SafeET

    root = SafeET.fromstring(parts[relation.resolved_target])
    nodes = []
    point_list = _first(root, "ptLst")
    if point_list is not None:
        for point in _children(point_list, "pt"):
            nodes.append({"id": point.get("modelId"), "type": point.get("type"), "text": _text(point)})
    edges = []
    connection_list = _first(root, "cxnLst")
    if connection_list is not None:
        for connection in _children(connection_list, "cxn"):
            edges.append({"id": connection.get("modelId"), "source": connection.get("srcId"), "target": connection.get("destId"), "type": connection.get("type")})
    return {"semantic_status": "extracted", "smartart_data": {"part": relation.resolved_target, "nodes": nodes, "connections": edges}}


def _table_semantics(element: Any) -> dict[str, Any]:
    table = _first(element, "tbl")
    if table is None:
        return {"semantic_status": "unsupported", "unsupported_reason": "table XML is missing"}
    rows = []
    for row in _children(table, "tr"):
        rows.append([_text(cell) for cell in _children(row, "tc")])
    return {"semantic_status": "extracted", "table_data": {"rows": rows, "row_count": len(rows), "column_count": max((len(row) for row in rows), default=0)}}


def native_semantics(
    element: Any,
    kind: str,
    relations: Iterable[RelationshipRecord],
    parts: dict[str, bytes],
    asset_ids: dict[str, str],
) -> dict[str, Any]:
    if kind == "chart":
        return _chart_semantics(element, relations, parts)
    if kind == "smartart":
        return _smartart_semantics(element, relations, parts)
    if kind == "table":
        return _table_semantics(element)
    ole = _relationship(element, relations, "/oleObject")
    if ole is not None:
        preview = None
        for relation in relations:
            if relation.relationship_id in {value for descendant in element.iter() for key, value in descendant.attrib.items() if key.partition("}")[0].endswith("relationships")} and relation.resolved_target in asset_ids and relation.resolved_target.startswith("ppt/media/"):
                preview = asset_ids[relation.resolved_target]
                break
        ole_element = _first(element, "oleObj")
        return {
            "semantic_status": "extracted",
            "embedded_object": {
                "part": ole.resolved_target,
                "prog_id": ole_element.get("progId") if ole_element is not None else None,
                "show_as_icon": ole_element.get("showAsIcon") if ole_element is not None else None,
                "preview_asset_id": preview,
                "executed": False,
            },
        }
    if kind in {"text", "shape", "connector", "group", "image"}:
        return {"semantic_status": "extracted"}
    return {"semantic_status": "unsupported", "unsupported_reason": f"No native semantic handler for {kind}"}
