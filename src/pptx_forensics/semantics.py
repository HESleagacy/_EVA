"""Native PresentationML style resolution and semantic projections."""

from __future__ import annotations

import math
from typing import Any, Iterable

from .models import RelationshipRecord


RELATIONSHIP_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "http://purl.oclc.org/ooxml/officeDocument/relationships",
    }
)
STRICT_RELATIONSHIP_TYPE_NS = "http://purl.oclc.org/ooxml"
CHART_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/drawingml/2006/chart",
        "http://purl.oclc.org/ooxml/drawingml/chart",
    }
)
DIAGRAM_NAMESPACES = frozenset(
    {
        "http://schemas.openxmlformats.org/drawingml/2006/diagram",
        "http://purl.oclc.org/ooxml/drawingml/diagram",
    }
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1 : tag.index("}")] if tag.startswith("{") and "}" in tag else ""


def _known_tag(tag: str) -> bool:
    namespace = _namespace(tag)
    return not namespace or namespace in {
        *RELATIONSHIP_NAMESPACES,
        "http://schemas.openxmlformats.org/presentationml/2006/main",
        "http://purl.oclc.org/ooxml/presentationml/main",
        "http://schemas.openxmlformats.org/drawingml/2006/main",
        "http://purl.oclc.org/ooxml/drawingml/main",
        "http://schemas.openxmlformats.org/drawingml/2006/chart",
        "http://purl.oclc.org/ooxml/drawingml/chart",
        "http://schemas.openxmlformats.org/drawingml/2006/diagram",
        "http://purl.oclc.org/ooxml/drawingml/diagram",
    }


def _children(root: Any, name: str) -> list[Any]:
    return [item for item in list(root) if _local_name(item.tag) == name and _known_tag(item.tag)]


def _child(root: Any, name: str) -> Any | None:
    return next(iter(_children(root, name)), None)


def _descendants(root: Any, name: str) -> list[Any]:
    return [item for item in root.iter() if _local_name(item.tag) == name and _known_tag(item.tag)]


def _first(root: Any, name: str) -> Any | None:
    return next(iter(_descendants(root, name)), None)


def _text(root: Any) -> str:
    paragraphs: list[str] = []
    for paragraph in (item for item in root.iter() if _local_name(item.tag) == "p" and _known_tag(item.tag)):
        pieces: list[str] = []
        for descendant in paragraph.iter():
            if not _known_tag(descendant.tag):
                continue
            kind = _local_name(descendant.tag)
            if kind == "t" and descendant.text is not None:
                pieces.append(descendant.text)
            elif kind == "br":
                pieces.append("\n")
            elif kind == "tab":
                pieces.append("\t")
        paragraphs.append("".join(pieces))
    if paragraphs:
        return "\n".join(paragraphs)
    return " ".join(item.text.strip() for item in _descendants(root, "t") if item.text and item.text.strip())


def _chart_text(root: Any | None) -> str:
    if root is None:
        return ""
    values = [
        item.text.strip()
        for item in root.iter()
        if _local_name(item.tag) in {"t", "v"}
        and _known_tag(item.tag)
        and item.text
        and item.text.strip()
    ]
    return " ".join(values)


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.casefold() in {"1", "true", "on"}


def _number(value: str | None, divisor: float = 1.0) -> float | None:
    if value is None:
        return None
    try:
        result = float(value) / divisor
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _theme_colors(theme: Any | None) -> dict[str, str]:
    if theme is None:
        return {}
    scheme = _first(theme, "clrScheme")
    if scheme is None:
        return {}
    colors: dict[str, str] = {}
    for item in list(scheme):
        if not _known_tag(item.tag):
            continue
        color = next(
            (
                child
                for child in list(item)
                if _local_name(child.tag) in {"srgbClr", "sysClr", "scrgbClr"} and _known_tag(child.tag)
            ),
            None,
        )
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
    color = next(
        (
            item
            for item in fill.iter()
            if _local_name(item.tag) in {"srgbClr", "schemeClr", "sysClr", "prstClr", "scrgbClr"}
            and _known_tag(item.tag)
        ),
        None,
    )
    if color is None:
        return {"kind": _local_name(fill.tag)}
    kind = _local_name(color.tag)
    value = color.get("val", "")
    resolved = value
    if kind == "schemeClr":
        resolved = theme_colors.get(value, value)
    elif kind == "sysClr":
        resolved = color.get("lastClr") or value
    elif kind == "scrgbClr" and not value:
        components: list[int] = []
        for attribute in ("r", "g", "b"):
            try:
                component = float(color.get(attribute, ""))
            except (TypeError, ValueError):
                components = []
                break
            if not math.isfinite(component) or not 0 <= component <= 100000:
                components = []
                break
            components.append(round(component * 255 / 100000))
        if len(components) == 3:
            resolved = "".join(f"{component:02x}" for component in components)
    alpha = _first(color, "alpha")
    alpha_value = _number(alpha.get("val") if alpha is not None else None, 100_000)
    result: dict[str, Any] = {"kind": "solid", "color": value if raw else resolved, "source": kind}
    if kind == "scrgbClr" and not value:
        result["components"] = {attribute: color.get(attribute) for attribute in ("r", "g", "b") if color.get(attribute) is not None}
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
    fill = next(
        (
            item
            for item in list(line)
            if _local_name(item.tag) in {"noFill", "solidFill", "gradFill", "pattFill"} and _known_tag(item.tag)
        ),
        None,
    )
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
        if _local_name(child.tag) in {"latin", "ea", "cs"} and _known_tag(child.tag) and child.get("typeface"):
            fonts[_local_name(child.tag)] = child.get("typeface")
    if fonts:
        style["fonts"] = fonts
        style["font_family"] = fonts.get("latin") or fonts.get("ea") or fonts.get("cs")
    color = next(
        (
            item
            for item in list(rpr)
            if _local_name(item.tag) in {"solidFill", "gradFill"} and _known_tag(item.tag)
        ),
        None,
    )
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
        if _local_name(element.tag) not in {"sp", "pic", "graphicFrame"} or not _known_tag(element.tag):
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
    relation_map: dict[str, RelationshipRecord] = {}
    for item in relations:
        relation_map.setdefault(item.relationship_id, item)
    for descendant in element.iter():
        for key, value in descendant.attrib.items():
            if _namespace(key) not in RELATIONSHIP_NAMESPACES:
                continue
            relation = relation_map.get(value)
            if relation is not None and (suffix is None or _relationship_type_name(relation.relationship_type) == _relationship_type_name(suffix)):
                return relation
    return None


def _relationship_type_name(value: str | None) -> str:
    raw = (value or "").strip().rstrip("/")
    if raw.startswith("/"):
        raw = raw[1:]
    known = {
        "chart",
        "diagram",
        "diagramData",
        "hyperlink",
        "image",
        "notesSlide",
        "officeDocument",
        "oleObject",
        "slide",
        "slideLayout",
        "slideMaster",
        "theme",
    }
    if raw in known:
        return raw.casefold()
    for namespace in (*RELATIONSHIP_NAMESPACES, STRICT_RELATIONSHIP_TYPE_NS):
        prefix = f"{namespace}/"
        original = (value or "").strip().rstrip("/")
        if original.startswith(prefix) and original[len(prefix) :] in known:
            return original[len(prefix) :].casefold()
    return ""


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

    try:
        root = SafeET.fromstring(parts[relation.resolved_target])
    except Exception as exc:
        return {
            "semantic_status": "failed",
            "unsupported_reason": "chart XML is invalid",
            "error": str(exc),
        }
    if _local_name(root.tag) != "chartSpace" or (_namespace(root.tag) and _namespace(root.tag) not in CHART_NAMESPACES):
        return {
            "semantic_status": "unsupported",
            "unsupported_reason": "chart XML root is not a recognized chartSpace",
        }
    chart_types = [
        item
        for item in root.iter()
        if _known_tag(item.tag)
        and _local_name(item.tag).endswith("Chart")
        and _local_name(item.tag) not in {"chart", "chartSpace"}
    ]
    chart_type = _local_name(chart_types[0].tag)[:-5] if chart_types else "unknown"
    chart = _child(root, "chart")
    series = []
    for series_element in _descendants(root, "ser"):
        title = _chart_text(_first(series_element, "tx"))
        category = _first(series_element, "cat")
        if category is None:
            category = _first(series_element, "xVal")
        values = _first(series_element, "val")
        if values is None:
            values = _first(series_element, "yVal")
        series.append({"title": title, "categories": _cache_values(category), "values": _cache_values(values)})
    axes = []
    for axis in root.iter():
        if _known_tag(axis.tag) and _local_name(axis.tag) in {"catAx", "dateAx", "valAx", "serAx"}:
            axis_id = _first(axis, "axId")
            axes.append({"type": _local_name(axis.tag), "id": axis_id.get("val") if axis_id is not None else None, "title": _chart_text(_first(axis, "title"))})
    legend = _child(chart, "legend") if chart is not None else _first(root, "legend")
    return {
        "semantic_status": "extracted",
        "chart_data": {
            "part": relation.resolved_target,
            "type": chart_type,
            "title": _chart_text(_child(chart, "title") if chart is not None else None),
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

    try:
        root = SafeET.fromstring(parts[relation.resolved_target])
    except Exception as exc:
        return {
            "semantic_status": "failed",
            "unsupported_reason": "SmartArt XML is invalid",
            "error": str(exc),
        }
    if _local_name(root.tag) != "data" or (_namespace(root.tag) and _namespace(root.tag) not in DIAGRAM_NAMESPACES):
        return {
            "semantic_status": "unsupported",
            "unsupported_reason": "SmartArt XML root is not a recognized data part",
        }
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
    cell_spans = []
    grid = _child(table, "tblGrid")
    grid_column_count = len(_children(grid, "gridCol")) if grid is not None else 0
    for row_index, row in enumerate(_children(table, "tr")):
        row_values = []
        column_index = 0
        for cell in _children(row, "tc"):
            row_values.append(_text(cell))
            try:
                grid_span = max(1, int(cell.get("gridSpan", "1")))
            except (TypeError, ValueError):
                grid_span = 1
            try:
                row_span = max(1, int(cell.get("rowSpan", "1")))
            except (TypeError, ValueError):
                row_span = 1
            merged = grid_span != 1 or row_span != 1 or cell.get("hMerge") is not None or cell.get("vMerge") is not None
            if merged:
                cell_spans.append(
                    {
                        "row": row_index,
                        "column": column_index,
                        "grid_span": grid_span,
                        "row_span": row_span,
                        "horizontal_merge": cell.get("hMerge"),
                        "vertical_merge": cell.get("vMerge"),
                    }
                )
            column_index += grid_span
        grid_column_count = max(grid_column_count, column_index)
        rows.append(row_values)
    table_data: dict[str, Any] = {
        "rows": rows,
        "row_count": len(rows),
        "column_count": grid_column_count or max((len(row) for row in rows), default=0),
    }
    if grid_column_count or cell_spans:
        if grid_column_count:
            table_data["grid_column_count"] = grid_column_count
        if cell_spans:
            table_data["cell_spans"] = cell_spans
    return {"semantic_status": "extracted", "table_data": table_data}


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
        relationship_ids = set()
        for descendant in element.iter():
            for key, value in descendant.attrib.items():
                if _namespace(key) in RELATIONSHIP_NAMESPACES:
                    relationship_ids.add(value)
        for relation in relations:
            if relation.relationship_id in relationship_ids and relation.resolved_target in asset_ids and relation.resolved_target != ole.resolved_target:
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
