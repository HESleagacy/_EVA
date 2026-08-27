"""Reproducible OCR, diagram, and vision evaluation against annotations."""

from __future__ import annotations

from collections import Counter
import math
from typing import Any, Iterable, Mapping, Sequence


def _payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_debug_dict"):
        return value.to_debug_dict()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value if isinstance(value, dict) else {}


def _normalise_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


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


def bbox_iou(first: Any, second: Any) -> float:
    """Return axis-aligned intersection-over-union for normalized boxes."""
    first_box, second_box = _bbox(first), _bbox(second)
    if first_box is None or second_box is None:
        return 0.0
    first_right, first_bottom = first_box[0] + first_box[2], first_box[1] + first_box[3]
    second_right, second_bottom = second_box[0] + second_box[2], second_box[1] + second_box[3]
    intersection = max(0.0, min(first_right, second_right) - max(first_box[0], second_box[0])) * max(
        0.0, min(first_bottom, second_bottom) - max(first_box[1], second_box[1])
    )
    union = first_box[2] * first_box[3] + second_box[2] * second_box[3] - intersection
    return intersection / union if union else 1.0 if first_box == second_box else 0.0


def _levenshtein(first: Sequence[Any], second: Sequence[Any]) -> int:
    previous = list(range(len(second) + 1))
    for first_index, first_value in enumerate(first, 1):
        current = [first_index]
        for second_index, second_value in enumerate(second, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[second_index] + 1,
                    previous[second_index - 1] + (first_value != second_value),
                )
            )
        previous = current
    return previous[-1]


def character_error_rate(reference: Any, hypothesis: Any) -> float:
    """Compute normalized character edit distance after whitespace normalization."""
    if isinstance(reference, Mapping):
        reference = reference.get("text", "")
    if isinstance(hypothesis, Mapping):
        hypothesis = hypothesis.get("text", "")
    expected, actual = _normalise_text(reference), _normalise_text(hypothesis)
    if not expected:
        return 0.0 if not actual else 1.0
    return _levenshtein(expected, actual) / len(expected)


def _word_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        words = value.get("words")
        if isinstance(words, list):
            return [item if isinstance(item, dict) else {"text": str(item)} for item in words]
        value = value.get("text", "")
    if isinstance(value, list):
        return [item if isinstance(item, dict) else {"text": str(item)} for item in value]
    return [{"text": word} for word in str(value or "").split()]


def _prf(true_positive: int, predicted: int, expected: int) -> dict[str, float]:
    precision = true_positive / predicted if predicted else 1.0 if expected == 0 else 0.0
    recall = true_positive / expected if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def word_precision_recall(reference: Any, hypothesis: Any) -> dict[str, float]:
    """Compute multiset word precision, recall, and F1."""
    expected = Counter(_normalise_text(item.get("text")) for item in _word_items(reference) if _normalise_text(item.get("text")))
    actual = Counter(_normalise_text(item.get("text")) for item in _word_items(hypothesis) if _normalise_text(item.get("text")))
    matched = sum((expected & actual).values())
    result = _prf(matched, sum(actual.values()), sum(expected.values()))
    return {
        "word_precision": result["precision"],
        "word_recall": result["recall"],
        "word_f1": result["f1"],
        "matched_words": matched,
        "predicted_words": sum(actual.values()),
        "reference_words": sum(expected.values()),
    }


def _word_matches(reference: list[dict[str, Any]], hypothesis: list[dict[str, Any]]) -> list[tuple[int, int]]:
    available = set(range(len(reference)))
    matches: list[tuple[int, int]] = []
    for predicted_index, predicted in enumerate(hypothesis):
        text = _normalise_text(predicted.get("text"))
        match = next(
            (reference_index for reference_index in sorted(available) if _normalise_text(reference[reference_index].get("text")) == text),
            None,
        )
        if match is not None:
            available.remove(match)
            matches.append((match, predicted_index))
    return matches


def confidence_calibration(samples: Iterable[Any], bins: int = 10) -> dict[str, Any]:
    """Compute Brier score and expected calibration error for confidence labels."""
    values: list[tuple[float, bool]] = []
    for sample in samples:
        if isinstance(sample, Mapping):
            confidence, correct = sample.get("confidence"), sample.get("correct", sample.get("is_correct"))
        elif isinstance(sample, (list, tuple)) and len(sample) >= 2:
            confidence, correct = sample[0], sample[1]
        else:
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not isinstance(correct, bool):
            continue
        values.append((max(0.0, min(1.0, float(confidence))), correct))
    bins = max(1, int(bins))
    histogram = [{"bin": index, "count": 0, "mean_confidence": None, "accuracy": None} for index in range(bins)]
    for confidence, correct in values:
        index = min(bins - 1, int(confidence * bins))
        histogram[index]["count"] += 1
        histogram[index].setdefault("_confidence_sum", 0.0)
        histogram[index].setdefault("_correct_sum", 0)
        histogram[index]["_confidence_sum"] += confidence
        histogram[index]["_correct_sum"] += int(correct)
    ece = 0.0
    for item in histogram:
        count = item["count"]
        if count:
            item["mean_confidence"] = item.pop("_confidence_sum") / count
            item["accuracy"] = item.pop("_correct_sum") / count
            ece += count / len(values) * abs(item["mean_confidence"] - item["accuracy"])
        else:
            item.pop("_confidence_sum", None)
            item.pop("_correct_sum", None)
    brier = sum((confidence - int(correct)) ** 2 for confidence, correct in values) / len(values) if values else None
    return {
        "sample_count": len(values),
        "brier_score": brier,
        "expected_calibration_error": ece if values else None,
        "ece": ece if values else None,
        "bins": histogram,
    }


def evaluate_ocr(reference: Any, hypothesis: Any) -> dict[str, Any]:
    """Evaluate OCR text, words, boxes, and confidence calibration."""
    reference_words = _word_items(reference)
    hypothesis_words = _word_items(hypothesis)
    matches = _word_matches(reference_words, hypothesis_words)
    matched_predicted = {predicted for _, predicted in matches}
    ious = [bbox_iou(reference_words[expected].get("bbox"), hypothesis_words[actual].get("bbox")) for expected, actual in matches]
    calibration = confidence_calibration(
        {"confidence": item.get("confidence"), "correct": index in matched_predicted}
        for index, item in enumerate(hypothesis_words)
        if item.get("confidence") is not None
    )
    words = word_precision_recall(reference, hypothesis)
    return {
        "character_error_rate": character_error_rate(reference, hypothesis),
        **words,
        "bbox_iou": sum(ious) / len(ious) if ious else 0.0 if hypothesis_words or reference_words else 1.0,
        "bbox_iou_matched_words": len(ious),
        "confidence_calibration": calibration,
    }


def _node_label(node: Mapping[str, Any]) -> str:
    return _normalise_text(node.get("label", node.get("text", "")))


def _node_key(node: Mapping[str, Any], index: int) -> str:
    value = node.get("id")
    return str(value) if value else f"node-{index}"


def _match_nodes(reference: list[dict[str, Any]], hypothesis: list[dict[str, Any]]) -> tuple[dict[str, str], list[tuple[int, int]]]:
    reference_keys = {_node_key(node, index): index for index, node in enumerate(reference)}
    mapping: dict[str, str] = {}
    matched: list[tuple[int, int]] = []
    used_reference: set[int] = set()
    for predicted_index, predicted in enumerate(hypothesis):
        predicted_key = _node_key(predicted, predicted_index)
        exact = reference_keys.get(predicted_key)
        if exact is not None and exact not in used_reference:
            mapping[predicted_key] = _node_key(reference[exact], exact)
            used_reference.add(exact)
            matched.append((exact, predicted_index))
    for predicted_index, predicted in enumerate(hypothesis):
        if any(index == predicted_index for _, index in matched):
            continue
        candidates = [
            (index, candidate)
            for index, candidate in enumerate(reference)
            if index not in used_reference and _node_label(candidate) and _node_label(candidate) == _node_label(predicted)
        ]
        if not candidates:
            candidates = [
                (index, candidate)
                for index, candidate in enumerate(reference)
                if index not in used_reference and bbox_iou(candidate.get("bbox"), predicted.get("bbox")) >= 0.5
            ]
        if not candidates:
            continue
        reference_index = max(candidates, key=lambda item: (bbox_iou(item[1].get("bbox"), predicted.get("bbox")), -item[0]))[0]
        predicted_key = _node_key(predicted, predicted_index)
        mapping[predicted_key] = _node_key(reference[reference_index], reference_index)
        used_reference.add(reference_index)
        matched.append((reference_index, predicted_index))
    return mapping, matched


def _edge_direction(edge: Mapping[str, Any]) -> str | None:
    value = edge.get("direction", edge.get("diagram_flow_direction", edge.get("flow_direction")))
    return str(value) if value not in (None, "", "unknown") else None


def _edge_endpoints(edge: Mapping[str, Any]) -> tuple[str | None, str | None]:
    source = edge.get("source", edge.get("src"))
    target = edge.get("target", edge.get("dst", edge.get("dest")))
    return (str(source) if source is not None else None, str(target) if target is not None else None)


def _edge_verified(edge: Mapping[str, Any]) -> bool:
    return edge.get("status") == "verified"


def _reference_edge_available(edge: Mapping[str, Any]) -> bool:
    """Treat omitted status as valid annotation data, but not failed evidence."""
    return edge.get("status") not in {"unverified", "failed"}


def _connectivity_accuracy(node_ids: set[str], reference_edges: list[tuple[str, str]], predicted_edges: list[tuple[str, str]]) -> float:
    if len(node_ids) < 2:
        return 1.0 if not reference_edges and not predicted_edges else 0.0

    def components(edges: list[tuple[str, str]]) -> dict[str, str]:
        parent = {node: node for node in node_ids}

        def root(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for first, second in edges:
            if first not in parent or second not in parent:
                continue
            first_root, second_root = root(first), root(second)
            if first_root != second_root:
                parent[second_root] = first_root
        return {node: root(node) for node in node_ids}

    expected, actual = components(reference_edges), components(predicted_edges)
    pairs = [(first, second) for first in sorted(node_ids) for second in sorted(node_ids) if first < second]
    return sum((expected[first] == expected[second]) == (actual[first] == actual[second]) for first, second in pairs) / len(pairs)


def evaluate_diagram(reference: Any, hypothesis: Any) -> dict[str, Any]:
    """Evaluate diagram nodes and edges, treating unverified edges as incorrect."""
    reference = reference if isinstance(reference, Mapping) else {}
    hypothesis = hypothesis if isinstance(hypothesis, Mapping) else {}
    reference_nodes = [item for item in reference.get("nodes", []) if isinstance(item, dict)]
    hypothesis_nodes = [item for item in hypothesis.get("nodes", []) if isinstance(item, dict)]
    mapping, node_matches = _match_nodes(reference_nodes, hypothesis_nodes)
    node_scores = _prf(len(node_matches), len(hypothesis_nodes), len(reference_nodes))

    reference_edges = [item for item in reference.get("edges", []) if isinstance(item, dict)]
    hypothesis_edges = [item for item in hypothesis.get("edges", []) if isinstance(item, dict)]
    used_reference_edges: set[int] = set()
    matched_edges: list[tuple[int, int]] = []
    endpoint_matches = 0
    for predicted_index, predicted in enumerate(hypothesis_edges):
        if not _edge_verified(predicted):
            continue
        source, target = _edge_endpoints(predicted)
        mapped_source, mapped_target = mapping.get(source or ""), mapping.get(target or "")
        if mapped_source is None or mapped_target is None:
            continue
        endpoint_matches += 1
        for reference_index, expected in enumerate(reference_edges):
            if reference_index in used_reference_edges:
                continue
            expected_source, expected_target = _edge_endpoints(expected)
            if (mapped_source, mapped_target) == (expected_source, expected_target):
                used_reference_edges.add(reference_index)
                matched_edges.append((reference_index, predicted_index))
                break
    edge_scores = _prf(len(matched_edges), len(hypothesis_edges), len(reference_edges))
    direction_pairs = [
        (_edge_direction(reference_edges[expected]), _edge_direction(hypothesis_edges[predicted]))
        for expected, predicted in matched_edges
        if _edge_direction(reference_edges[expected]) is not None and _edge_direction(hypothesis_edges[predicted]) is not None
    ]
    direction_accuracy = sum(first == second for first, second in direction_pairs) / len(direction_pairs) if direction_pairs else None
    common_nodes = {
        _node_key(reference_nodes[expected], expected)
        for expected, _ in node_matches
    }
    expected_connectivity = [
        _edge_endpoints(edge)
        for edge in reference_edges
        if _reference_edge_available(edge) and all(endpoint in common_nodes for endpoint in _edge_endpoints(edge))
    ]
    predicted_connectivity = []
    for expected, predicted in matched_edges:
        source, target = _edge_endpoints(hypothesis_edges[predicted])
        mapped_source, mapped_target = mapping.get(source or ""), mapping.get(target or "")
        if mapped_source and mapped_target:
            predicted_connectivity.append((mapped_source, mapped_target))
    connectivity = _connectivity_accuracy(common_nodes, expected_connectivity, predicted_connectivity)
    unverified_edges = sum(not _edge_verified(item) for item in hypothesis_edges)
    return {
        "nodes": {**node_scores, "true_positive": len(node_matches), "predicted": len(hypothesis_nodes), "expected": len(reference_nodes)},
        "edges": {**edge_scores, "true_positive": len(matched_edges), "predicted": len(hypothesis_edges), "expected": len(reference_edges)},
        "node_precision": node_scores["precision"],
        "node_recall": node_scores["recall"],
        "node_f1": node_scores["f1"],
        "edge_precision": edge_scores["precision"],
        "edge_recall": edge_scores["recall"],
        "edge_f1": edge_scores["f1"],
        "endpoint_accuracy": endpoint_matches / len(hypothesis_edges) if hypothesis_edges else 1.0 if not reference_edges else 0.0,
        "direction_accuracy": direction_accuracy,
        "graph_connectivity_accuracy": connectivity,
        "verified_edge_count": sum(_edge_verified(item) for item in hypothesis_edges),
        "unverified_edge_count": unverified_edges,
        "unverified_edges_counted_as_true_positive": 0,
    }


def evaluate_image_role(reference: Any, hypothesis: Any) -> dict[str, Any]:
    expected = str(reference) if reference is not None else None
    actual = str(hypothesis) if hypothesis is not None else None
    return {"expected": expected, "actual": actual, "correct": expected == actual, "accuracy": 1.0 if expected == actual else 0.0}


def vision_completeness(requested_targets: Sequence[str], records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    expected = {str(item) for item in requested_targets}
    returned = {
        str(record.get("value", {}).get("target", record.get("slide_id")))
        for record in records
        if isinstance(record.get("value"), Mapping) or record.get("slide_id")
    }
    missing = sorted(expected - returned)
    unexpected = sorted(returned - expected) if expected else []
    return {
        "requested_count": len(expected),
        "response_count": len(returned & expected) if expected else len(returned),
        "missing_targets": missing,
        "unexpected_targets": unexpected,
        "complete": not missing,
    }


def _vision_labels(labels: Any) -> list[dict[str, Any]]:
    if not isinstance(labels, Mapping):
        return []
    values = labels.get("targets", labels.get("vision_targets", labels.get("vision", [])))
    if isinstance(values, Mapping):
        return [{"target": key, **value} if isinstance(value, Mapping) else {"target": key, "image_role": value} for key, value in values.items()]
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _vision_target(record: Mapping[str, Any]) -> str:
    value = record.get("value") if isinstance(record.get("value"), Mapping) else {}
    return str(value.get("target", record.get("slide_id", record.get("id", ""))))


def evaluate_gemini(records: Any, labels: Any, baseline: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate completeness, role/flow accuracy, graph quality, and run telemetry."""
    if isinstance(records, Mapping):
        records = records.get("vision_evidence", [])
    records = [item for item in records if isinstance(item, Mapping)] if isinstance(records, list) else []
    target_labels = _vision_labels(labels)
    requested = labels.get("requested_targets", []) if isinstance(labels, Mapping) else []
    requested = list(requested) if isinstance(requested, list) else []
    requested.extend(str(item.get("target")) for item in target_labels if item.get("target") is not None)
    completeness = vision_completeness(sorted(set(requested)), records)
    by_target = {_vision_target(record): record for record in records}
    role_results: list[bool] = []
    reading_results: list[bool] = []
    flow_results: list[bool] = []
    diagram_results: list[dict[str, Any]] = []
    grounding_scores: list[float] = []
    hallucination_count = 0
    invalid_count = 0
    cache_hits = 0
    costs: list[float] = []
    durations: list[float] = []
    for target_label in target_labels:
        target = str(target_label.get("target", ""))
        record = by_target.get(target)
        analysis = record.get("value", {}).get("analysis") if record and isinstance(record.get("value"), Mapping) else None
        if not isinstance(analysis, Mapping):
            invalid_count += 1
            continue
        if target_label.get("image_role") is not None:
            role_results.append(analysis.get("image_role") == target_label["image_role"])
        if target_label.get("slide_reading_order") is not None:
            reading_results.append(analysis.get("slide_reading_order") == target_label["slide_reading_order"])
        if "flow_present" in target_label:
            flow_results.append(analysis.get("flow_present") == target_label["flow_present"])
        if target_label.get("diagram_flow_direction") is not None:
            flow_results.append(analysis.get("diagram_flow_direction") == target_label["diagram_flow_direction"])
        if "nodes" in target_label or "edges" in target_label:
            diagram_results.append(evaluate_diagram(target_label, analysis))
        metadata = record.get("value", {}).get("metadata", {}) if isinstance(record.get("value"), Mapping) else {}
        grounding = metadata.get("evidence_grounding") if isinstance(metadata, Mapping) else None
        if isinstance(grounding, Mapping) and isinstance(grounding.get("grounding_score"), (int, float)):
            grounding_scores.append(float(grounding["grounding_score"]))
            hallucination_count += int(bool(grounding.get("hallucination_detected")))
        source = record.get("source", {}) if isinstance(record.get("source"), Mapping) else {}
        cache_hits += int(bool(source.get("cache_hit")))
        cost = source.get("estimated_cost_usd", metadata.get("estimated_cost_usd") if isinstance(metadata, Mapping) else None)
        duration = source.get("request_seconds", metadata.get("request_seconds") if isinstance(metadata, Mapping) else None)
        if isinstance(cost, (int, float)):
            costs.append(float(cost))
        if isinstance(duration, (int, float)):
            durations.append(float(duration))
    result: dict[str, Any] = {
        "completeness": completeness,
        "role_accuracy": sum(role_results) / len(role_results) if role_results else None,
        "reading_order_accuracy": sum(reading_results) / len(reading_results) if reading_results else None,
        "flow_direction_accuracy": sum(flow_results) / len(flow_results) if flow_results else None,
        "diagram_evaluation": {
            key: sum(item[key] for item in diagram_results if isinstance(item.get(key), (int, float))) / len(diagram_results)
            for key in ("node_precision", "node_recall", "node_f1", "edge_precision", "edge_recall", "edge_f1", "endpoint_accuracy", "direction_accuracy", "graph_connectivity_accuracy")
            if any(isinstance(item.get(key), (int, float)) for item in diagram_results)
        },
        "grounding_score": sum(grounding_scores) / len(grounding_scores) if grounding_scores else None,
        "hallucination_rate": hallucination_count / len(grounding_scores) if grounding_scores else None,
        "invalid_response_count": invalid_count,
        "cache_hit_count": cache_hits,
        "cache_hit_rate": cache_hits / len(records) if records else None,
        "request_count": sum(record.get("status") not in {"not_requested", "not_applicable"} for record in records),
        "estimated_cost_usd": sum(costs) if costs else 0.0,
        "latency_seconds": sum(durations) if durations else 0.0,
    }
    if baseline:
        result["improvement_over_baseline"] = {
            key: value - baseline[key]
            for key, value in result["diagram_evaluation"].items()
            if isinstance(value, (int, float)) and isinstance(baseline.get(key), (int, float))
        }
    return result


def evaluate_report(report: Any, labels: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate labeled OCR, diagrams, and Gemini records from a DeckIR report."""
    payload = _payload(report)
    result: dict[str, Any] = {"schema_version": payload.get("schema_version")}
    ocr_labels = labels.get("ocr", {}) if isinstance(labels, Mapping) else {}
    ocr_records = {
        str(item.get("value", {}).get("asset_id")): item
        for item in payload.get("ocr_evidence", [])
        if isinstance(item, Mapping) and isinstance(item.get("value"), Mapping) and item["value"].get("asset_id")
    }
    if isinstance(ocr_labels, Mapping):
        result["ocr"] = {
            asset_id: evaluate_ocr(annotation, ocr_records.get(str(asset_id), {}).get("value", {}))
            for asset_id, annotation in ocr_labels.items()
        }
    diagram_labels = labels.get("diagrams", labels.get("diagram", {})) if isinstance(labels, Mapping) else {}
    graph_records = [
        item
        for item in payload.get("rendered_evidence", [])
        if isinstance(item, Mapping) and isinstance(item.get("value"), Mapping) and item["value"].get("type") == "diagram_graph"
    ]
    if isinstance(diagram_labels, Mapping):
        result["diagrams"] = {}
        for key, annotation in diagram_labels.items():
            matching = next(
                (
                    item
                    for item in graph_records
                    if key in {item.get("id"), f"{item.get('slide_id')}:{item.get('value', {}).get('asset_id')}"}
                ),
                {},
            )
            result["diagrams"][key] = evaluate_diagram(annotation, matching.get("value", {}))
    vision_labels = labels.get("vision", labels) if isinstance(labels, Mapping) else {}
    if isinstance(vision_labels, Mapping) and ("targets" in vision_labels or "requested_targets" in vision_labels or "vision_targets" in vision_labels):
        result["vision"] = evaluate_gemini(payload.get("vision_evidence", []), vision_labels)
    return result


evaluate_diagram_graph = evaluate_diagram
evaluate_vision = evaluate_gemini
