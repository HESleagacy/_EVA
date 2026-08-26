"""Selective, cacheable Gemini vision evidence.

Vision is an optional interpretation layer.  Native OOXML, OCR, rendered
geometry, and diagram candidates remain unchanged when this module runs.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
from time import perf_counter
import time
from typing import Any, Mapping, Protocol, Sequence
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from .models import ExtractionReport, IMAGE_ROLES as DECK_IMAGE_ROLES
from .ocr import run_ocr
from .config import load_dotenv


VISION_SCHEMA_VERSION = "gemini-vision-v3"
VISION_PROMPT_VERSION = "selective-gemini-v4"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_THINKING_BUDGET = 1024
DEFAULT_MAX_OUTPUT_TOKENS = 8192
MAX_VISION_NODES = 128
MAX_VISION_EDGES = 256
MAX_VISION_OBSERVATIONS = 64
MODEL_CONFIDENCE_CAP = 0.75
GEMINI_INPUT_USD_PER_MILLION = 0.30
GEMINI_OUTPUT_USD_PER_MILLION = 2.50
LOW_OCR_CONFIDENCE = 0.65
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRIES = 2
DEFAULT_CIRCUIT_BREAKER_FAILURES = 3
SUPPORTED_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
}
VISION_STATUSES = {"verified", "partial", "unverified"}
IMAGE_ROLES = set(DECK_IMAGE_ROLES)
READING_DIRECTIONS = {"left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top", "unknown"}


class VisionRequestError(RuntimeError):
    """Raised when a vision request cannot produce a response."""


def _retryable_vision_error(error: Exception) -> bool:
    """Retry transport/rate-limit failures, not deterministic schema failures."""
    message = str(error).lower()
    if "http 429" in message or "http 500" in message or "http 502" in message or "http 503" in message or "http 504" in message:
        return True
    return any(term in message for term in ("timed out", "timeout", "temporarily unavailable", "connection reset", "connection refused"))


def _invalid_vision_response(error: Exception) -> bool:
    if isinstance(error, (json.JSONDecodeError, TypeError)):
        return True
    message = str(error).lower()
    return any(
        term in message
        for term in (
            "vision response must be",
            "vision response has",
            "vision response schema",
            "vision response did not",
            "vision response was empty",
            "vision node schema",
            "vision edge schema",
            "vision observation",
        )
    )


def _vision_evidence_status(status: str) -> str:
    return {
        "ok": "partial",
        "skipped": "not_requested",
        "unavailable": "not_applicable",
        "failed": "failed",
        "circuit_open": "failed",
    }.get(status, "unverified")


def _merge_stage_status(previous: str, current: str) -> str:
    if previous == "not_requested":
        return current
    if previous == current:
        return current
    if "failed" in {previous, current} and "verified" in {previous, current}:
        return "partial"
    if "not_applicable" in {previous, current}:
        return current if previous == "not_applicable" else previous
    return "partial"


def _set_slide_visibility(deck: Any, slide_id: str, stage: str, status: str) -> None:
    slide = next((item for item in deck.slides if item.get("id") == slide_id), None)
    if slide is None:
        return
    visibility = slide.setdefault("visual_evidence_visibility", {})
    visibility[stage] = _merge_stage_status(visibility.get(stage, "not_requested"), status)


@dataclass(frozen=True)
class VisionImage:
    label: str
    mime_type: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class VisionAdapter(Protocol):
    name: str
    version: str

    def analyze(self, prompt: str, images: Sequence[VisionImage], timeout: float) -> str | dict[str, Any]:
        ...


class GeminiVisionAdapter:
    """Small REST adapter for the Gemini API using only the standard library."""

    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        thinking_budget: int = DEFAULT_THINKING_BUDGET,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        endpoint: str = "https://generativelanguage.googleapis.com/v1beta/models",
    ) -> None:
        load_dotenv()
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
        self.thinking_budget = max(0, thinking_budget)
        self.max_output_tokens = max(1, max_output_tokens)
        self.endpoint = endpoint.rstrip("/")
        self.version = self.model
        self.last_usage: dict[str, Any] | None = None
        self.usage_records: list[dict[str, Any]] = []
        self.last_duration_seconds: float | None = None

    def analyze(self, prompt: str, images: Sequence[VisionImage], timeout: float) -> str:
        if not self.api_key:
            raise VisionRequestError("GEMINI_API_KEY is not configured")
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for image in images:
            if image.mime_type not in SUPPORTED_IMAGE_MIMES:
                raise VisionRequestError(f"unsupported Gemini image MIME type: {image.mime_type}")
            parts.append(
                {
                    "inline_data": {
                        "mime_type": image.mime_type,
                        "data": base64.b64encode(image.data).decode("ascii"),
                    }
                }
            )
        request_payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": self.max_output_tokens,
                "thinkingConfig": {"thinkingBudget": self.thinking_budget},
                "responseMimeType": "application/json",
                "responseSchema": _gemini_response_schema(),
            },
        }
        encoded_key = urllib.parse.quote(self.api_key, safe="")
        url = f"{self.endpoint}/{urllib.parse.quote(self.model, safe='')}:generateContent?key={encoded_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(request_payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.last_duration_seconds = round(perf_counter() - started, 6)
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise VisionRequestError(f"Gemini HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.last_duration_seconds = round(perf_counter() - started, 6)
            raise VisionRequestError(f"Gemini request failed: {exc}") from exc
        self.last_duration_seconds = round(perf_counter() - started, 6)
        usage = response_payload.get("usageMetadata")
        self.last_usage = usage if isinstance(usage, dict) else None
        if self.last_usage is not None:
            self.usage_records.append(dict(self.last_usage))

        try:
            candidates = response_payload["candidates"]
            parts = candidates[0]["content"]["parts"]
            text = next(item["text"] for item in parts if isinstance(item, dict) and isinstance(item.get("text"), str))
        except (KeyError, IndexError, TypeError, StopIteration) as exc:
            raise VisionRequestError("Gemini response did not contain candidate JSON text") from exc
        return text


def _gemini_response_schema() -> dict[str, Any]:
    """Return the Gemini responseSchema, while local validation stays strict."""
    return {
        "type": "OBJECT",
        "properties": {
            "schema_version": {"type": "STRING", "enum": [VISION_SCHEMA_VERSION]},
            "image_role": {"type": "STRING", "enum": sorted(IMAGE_ROLES)},
            "summary": {"type": "STRING"},
            "slide_reading_order": {"type": "STRING", "enum": sorted(READING_DIRECTIONS)},
            "diagram_flow_direction": {"type": "STRING", "enum": sorted(READING_DIRECTIONS)},
            "flow_present": {"type": "BOOLEAN", "nullable": True},
            "nodes": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "id": {"type": "STRING"},
                        "label": {"type": "STRING"},
                        "bbox": {"type": "ARRAY", "items": {"type": "NUMBER"}},
                        "status": {"type": "STRING", "enum": sorted(VISION_STATUSES)},
                    },
                    "required": ["id", "label", "bbox", "status"],
                },
            },
            "edges": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "source": {"type": "STRING"},
                        "target": {"type": "STRING"},
                        "label": {"type": "STRING"},
                        "direction": {"type": "STRING", "enum": sorted(READING_DIRECTIONS)},
                        "status": {"type": "STRING", "enum": sorted(VISION_STATUSES)},
                    },
                    "required": ["source", "target", "label", "direction", "status"],
                },
            },
            "observations": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "type": {"type": "STRING"},
                        "objects": {"type": "ARRAY", "items": {"type": "STRING"}},
                        "description": {"type": "STRING"},
                        "confidence": {"type": "NUMBER"},
                    },
                    "required": ["type", "objects", "description", "confidence"],
                },
            },
        },
        "required": ["schema_version", "image_role", "summary", "slide_reading_order", "diagram_flow_direction", "flow_present", "nodes", "edges", "observations"],
    }


def validate_vision_payload(payload: Any) -> tuple[bool, str]:
    """Validate the exact JSON contract returned by the model."""
    if not isinstance(payload, dict):
        return False, "vision response must be an object"
    required = {"schema_version", "image_role", "summary", "slide_reading_order", "diagram_flow_direction", "flow_present", "nodes", "edges", "observations"}
    if set(payload) != required:
        return False, "vision response has unexpected or missing top-level fields"
    if payload["schema_version"] != VISION_SCHEMA_VERSION:
        return False, "vision response schema version is unsupported"
    if payload["image_role"] not in IMAGE_ROLES:
        return False, "vision response has an invalid image role"
    if (
        not isinstance(payload["summary"], str)
        or payload["slide_reading_order"] not in READING_DIRECTIONS
        or payload["diagram_flow_direction"] not in READING_DIRECTIONS
        or (payload["flow_present"] is not None and not isinstance(payload["flow_present"], bool))
    ):
        return False, "vision response has invalid summary or reading direction"
    if not isinstance(payload["nodes"], list) or not isinstance(payload["edges"], list) or not isinstance(payload["observations"], list):
        return False, "vision response collections must be arrays"
    if len(payload["nodes"]) > MAX_VISION_NODES or len(payload["edges"]) > MAX_VISION_EDGES or len(payload["observations"]) > MAX_VISION_OBSERVATIONS:
        return False, "vision response exceeds collection limits"
    node_ids: set[str] = set()
    for node in payload["nodes"]:
        if not isinstance(node, dict) or set(node) != {"id", "label", "bbox", "status"}:
            return False, "vision node schema is invalid"
        if not isinstance(node["id"], str) or not node["id"] or node["id"] in node_ids:
            return False, "vision node IDs must be unique non-empty strings"
        node_ids.add(node["id"])
        if not isinstance(node["label"], str) or node["status"] not in VISION_STATUSES or not _valid_bbox(node["bbox"]):
            return False, "vision node fields are invalid"
    for edge in payload["edges"]:
        if not isinstance(edge, dict) or set(edge) != {"source", "target", "label", "direction", "status"}:
            return False, "vision edge schema is invalid"
        if not isinstance(edge["source"], str) or not isinstance(edge["target"], str):
            return False, "vision edge endpoints must be strings"
        if edge["source"] not in node_ids or edge["target"] not in node_ids:
            return False, "vision edge endpoints must reference returned nodes"
        if edge["direction"] not in READING_DIRECTIONS or edge["status"] not in VISION_STATUSES or not isinstance(edge["label"], str):
            return False, "vision edge fields are invalid"
    for observation in payload["observations"]:
        if not isinstance(observation, dict) or set(observation) != {"type", "objects", "description", "confidence"}:
            return False, "vision observation schema is invalid"
        if not isinstance(observation["type"], str) or not isinstance(observation["description"], str):
            return False, "vision observation text is invalid"
        if not isinstance(observation["objects"], list) or not all(isinstance(item, str) for item in observation["objects"]):
            return False, "vision observation objects are invalid"
        confidence = observation["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            return False, "vision observation confidence is invalid"
    return True, "ok"


def _valid_bbox(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) and 0 <= item <= 1 for item in value)
    )


def _cache_root(cache_dir: str | Path | None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).expanduser().resolve()
    return Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "pptx-forensics" / "vision"


def _mime_for_path(path: Path) -> str | None:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(path.suffix.lower())


def _round_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        result = [round(float(item), 12) for item in value]
    except (TypeError, ValueError):
        return None
    return result if all(item == item and abs(item) != float("inf") for item in result) else None


def _object_area(item: dict[str, Any]) -> float:
    bbox = _round_bbox(item.get("bbox"))
    return max(0.0, bbox[2]) * max(0.0, bbox[3]) if bbox else 0.0


def _is_corner_object(item: dict[str, Any]) -> bool:
    bbox = _round_bbox(item.get("bbox"))
    if not bbox:
        return False
    left, top, width, height = bbox
    return left <= 0.08 or top <= 0.08 or left + width >= 0.92 or top + height >= 0.92


def _asset_noise_reasons(
    asset_id: str,
    objects: list[dict[str, Any]],
    occurrences: Mapping[str, list[dict[str, Any]]],
    ocr: dict[str, Any] | None,
    graphs: list[dict[str, Any]],
    deterministic_role: str | None = None,
    *,
    include_noise: bool,
) -> list[str]:
    if include_noise:
        return []
    value = ocr.get("value", {}) if isinstance(ocr, dict) and isinstance(ocr.get("value"), dict) else {}
    word_count = len(value.get("words", [])) if isinstance(value.get("words"), list) else 0
    ocr_text = str(value.get("text", "")).lower()
    edge_count = sum(len(graph.get("edges", [])) for graph in graphs)
    asset_objects = occurrences.get(asset_id, objects)
    repeated_slides = len({item.get("slide_id") for item in asset_objects})
    small = any(max((_round_bbox(item.get("bbox")) or [0, 0, 0, 0])[2:]) < 0.22 or _object_area(item) < 0.04 for item in asset_objects)
    tiny = all(_object_area(item) < 0.01 for item in asset_objects)
    corner = any(_is_corner_object(item) for item in asset_objects)
    full_slide = any(_object_area(item) > 0.65 for item in asset_objects)
    reasons: list[str] = []
    if repeated_slides >= 2 and small and word_count <= 6 and edge_count == 0:
        reasons.append("repeated_small_corner_asset")
    if repeated_slides >= 2 and full_slide and word_count == 0 and edge_count == 0:
        reasons.append("repeated_full_slide_template_asset")
    if small and corner and word_count <= 2 and edge_count == 0:
        reasons.append("decorative_or_logo_asset")
    if small and corner and any(term in ocr_text for term in ("smart india", "hackathon", "sih")):
        reasons.append("recognized_logo_or_watermark_text")
    if tiny and word_count <= 3 and edge_count == 0:
        reasons.append("tiny_low_information_asset")
    if deterministic_role in {"logo", "decorative_image", "decorative", "template"} and edge_count == 0:
        reasons.append("deterministic_role_noise")
    return reasons


def _asset_gate(
    asset_id: str,
    slide_id: str,
    objects: list[dict[str, Any]],
    occurrences: Mapping[str, list[dict[str, Any]]],
    ocr: dict[str, Any] | None,
    graphs: list[dict[str, Any]],
    slide_facts: list[dict[str, Any]],
    unsupported_smartart: bool,
    deterministic_role: str | None = None,
    *,
    include_noise: bool,
) -> dict[str, Any]:
    noise_reasons = _asset_noise_reasons(
        asset_id,
        objects,
        occurrences,
        ocr,
        graphs,
        deterministic_role,
        include_noise=include_noise,
    )
    if noise_reasons:
        return {"selected": False, "reasons": ["noise_filtered", *noise_reasons]}
    reasons: list[str] = []
    value = ocr.get("value", {}) if isinstance(ocr, dict) and isinstance(ocr.get("value"), dict) else {}
    ocr_status = value.get("status")
    confidence = value.get("confidence")
    if ocr_status != "verified" or not isinstance(confidence, (int, float)) or confidence < LOW_OCR_CONFIDENCE:
        reasons.append("low_or_missing_ocr_confidence")
    if not graphs:
        reasons.append("image_role_unknown")
    for graph in graphs:
        if not graph.get("edges") or graph.get("edge_verification", 0.0) <= 0:
            reasons.append("diagram_edges_missing")
        if graph.get("diagram_flow_direction") == "unknown":
            reasons.append("diagram_flow_direction_unclear")
    if any(item.get("value", {}).get("type") == "native_rendered_geometry_mismatch" for item in slide_facts):
        reasons.append("native_visual_disagreement")
    if unsupported_smartart:
        reasons.append("unsupported_smartart_interpretation")
    return {"selected": True, "reasons": sorted(set(reasons))}


def _native_candidates(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = ("id", "parent_id", "type", "shape_type", "bbox", "z_order", "text", "asset_id", "semantic_status")
    candidates: list[dict[str, Any]] = []
    for item in objects:
        if item.get("type") in {"group", "shape"} and not item.get("text"):
            continue
        candidate = {key: item.get(key) for key in fields}
        text = candidate.get("text")
        if isinstance(text, str) and len(text) > 500:
            candidate["text"] = text[:500] + "..."
        candidates.append(candidate)
    return candidates[:96]


def _is_noise_node(node: dict[str, Any]) -> bool:
    label = " ".join(str(node.get("label", "")).strip().lower().split())
    if not label:
        return False
    page_label = label.removeprefix("page ").removeprefix("slide ").strip(" -:#")
    if label.isdigit() or page_label.isdigit() or any(term in label for term in ("logo", "watermark", "page number", "slide number", "hackathon")):
        return True
    if label in {"point blank", "smart india hackathon 2025"}:
        return True
    bbox = _round_bbox(node.get("bbox"))
    if bbox is None:
        return False
    left, top, width, height = bbox
    near_edge = left <= 0.08 or top <= 0.08 or left + width >= 0.92 or top + height >= 0.92
    return near_edge and len(label) <= 24 and any(term in label for term in ("blank", "india", "point", "2025"))


def _conservatize_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Keep model interpretation probabilistic and remove obvious slide chrome."""
    role_noise = payload.get("image_role") in {"logo", "template", "decorative", "decorative_image"}
    removed_ids = {
        node["id"]
        for node in payload["nodes"]
        if role_noise or _is_noise_node(node)
    }
    nodes = []
    downgraded_nodes = 0
    for node in payload["nodes"]:
        if node["id"] in removed_ids:
            continue
        item = dict(node)
        if item["status"] == "verified":
            item["status"] = "partial"
            downgraded_nodes += 1
        nodes.append(item)
    edges = []
    removed_edges = 0
    node_ids = {node["id"] for node in nodes}
    for edge in payload["edges"]:
        if edge["source"] not in node_ids or edge["target"] not in node_ids:
            removed_edges += 1
            continue
        item = dict(edge)
        if item["status"] != "unverified":
            item["status"] = "unverified"
        edges.append(item)
    observations = [dict(item) for item in payload["observations"]]
    if removed_ids:
        observations.append(
            {
                "type": "noise_filtered",
                "objects": sorted(removed_ids),
                "description": "Logo, page-number, or decorative slide chrome was excluded from the diagram graph.",
                "confidence": 1.0,
            }
        )
    flow_present = payload["flow_present"]
    if flow_present is False:
        flow_present = None
    return (
        {
            **payload,
            "nodes": nodes,
            "edges": edges,
            "observations": observations[:MAX_VISION_OBSERVATIONS],
            "flow_present": flow_present,
        },
        {
            "removed_noise_nodes": len(removed_ids),
            "removed_edges": removed_edges,
            "downgraded_nodes": downgraded_nodes,
        },
    )


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _reconcile_payload(
    payload: dict[str, Any] | None,
    native: list[dict[str, Any]],
    ocr: Mapping[str, Any],
    graphs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Summarize independent context overlap without upgrading model evidence."""
    if payload is None:
        return None
    native_ids = {item.get("id") for item in native if item.get("id")}
    context_text = [
        _normalized_text(item.get("text"))
        for item in native
        if len(_normalized_text(item.get("text"))) >= 4
    ]
    for value in ocr.values():
        if isinstance(value, dict):
            text = _normalized_text(value.get("text"))
            if len(text) >= 4:
                context_text.append(text)
    graph_text = [
        _normalized_text(node.get("text"))
        for graph in graphs
        if isinstance(graph, dict)
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and len(_normalized_text(node.get("text"))) >= 4
    ]
    context_text.extend(graph_text)
    nodes = payload.get("nodes", [])
    id_matches = 0
    text_matches = 0
    matched_nodes: set[str] = set()
    for node in nodes:
        label = _normalized_text(node.get("label"))
        if node.get("id") in native_ids:
            id_matches += 1
            matched_nodes.add(str(node.get("id")))
        if label and len(label) >= 4 and any(label in text or text in label for text in context_text):
            text_matches += 1
            matched_nodes.add(str(node.get("id")))
    node_count = len(nodes)
    node_matches = len(matched_nodes)
    edge_count = len(payload.get("edges", []))
    node_ids = {node.get("id") for node in nodes}
    supported_edges = sum(
        1
        for edge in payload.get("edges", [])
        if edge.get("source") in node_ids and edge.get("target") in node_ids
    )
    return {
        "native_id_matches": id_matches,
        "text_overlap_matches": text_matches,
        "node_count": node_count,
        "node_context_overlap_rate": round(node_matches / node_count, 6) if node_count else None,
        "edge_count": edge_count,
        "edge_endpoint_context_count": supported_edges,
        "warning": "context overlap is audit metadata only and does not verify model nodes or edges",
    }


def _evidence_grounding(
    payload: Mapping[str, Any] | None,
    native: list[dict[str, Any]],
    ocr: Mapping[str, Any],
    graphs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Audit whether model claims have an identifiable native/OCR anchor."""
    if payload is None:
        return None
    native_ids = {str(item.get("id")) for item in native if item.get("id")}
    context_text = {
        _normalized_text(item.get("text"))
        for item in native
        if len(_normalized_text(item.get("text"))) >= 2
    }
    for value in ocr.values():
        if isinstance(value, dict):
            context_text.add(_normalized_text(value.get("text")))
            context_text.update(
                _normalized_text(item.get("text"))
                for item in value.get("lines", [])
                if isinstance(item, dict)
            )
    for graph in graphs:
        for node in graph.get("nodes", []):
            if isinstance(node, dict):
                context_text.add(_normalized_text(node.get("text")))
    context_text.discard("")
    grounded_nodes: dict[str, bool] = {}
    node_details: list[dict[str, Any]] = []
    for node in payload.get("nodes", []):
        node_id = str(node.get("id", ""))
        label = _normalized_text(node.get("label"))
        bases: list[str] = []
        if node_id in native_ids:
            bases.append("native_object_id")
        if label and any(label in text or text in label for text in context_text if len(text) >= 2):
            bases.append("native_or_ocr_text")
        grounded = bool(bases)
        grounded_nodes[node_id] = grounded
        node_details.append({"id": node_id, "grounded": grounded, "basis": bases})
    edge_details: list[dict[str, Any]] = []
    for edge in payload.get("edges", []):
        source, target = str(edge.get("source", "")), str(edge.get("target", ""))
        grounded = grounded_nodes.get(source, False) and grounded_nodes.get(target, False)
        edge_details.append({"source": source, "target": target, "grounded": grounded})
    node_count = len(node_details)
    edge_count = len(edge_details)
    grounded_node_count = sum(item["grounded"] for item in node_details)
    grounded_edge_count = sum(item["grounded"] for item in edge_details)
    checks = []
    if any(not item["grounded"] for item in node_details):
        checks.append("ungrounded_node_claim")
    if any(not item["grounded"] for item in edge_details):
        checks.append("ungrounded_edge_claim")
    return {
        "grounding_score": grounded_node_count / node_count if node_count else 1.0,
        "edge_grounding_score": grounded_edge_count / edge_count if edge_count else 1.0,
        "grounded_node_count": grounded_node_count,
        "node_count": node_count,
        "grounded_edge_count": grounded_edge_count,
        "edge_count": edge_count,
        "nodes": node_details,
        "edges": edge_details,
        "checks": checks,
        "hallucination_detected": bool(checks),
        "warning": "grounding is an audit heuristic and does not prove visual truth",
    }


def _slide_facts(deck: Any, slide_id: str) -> list[dict[str, Any]]:
    useful_types = {
        "slide_occupancy",
        "margins",
        "text_density",
        "image_asset_analysis",
        "alignment_mismatch",
        "shape_hierarchy_candidate",
        "rotation",
        "native_rendered_geometry_mismatch",
        "shape_peer_group",
        "alignment_peer_group",
        "spacing_peer_group",
        "largest_empty_region",
        "whitespace_balance",
        "native_connector_count",
        "flow_candidate",
        "visual_exclusions",
        "slide_title_candidate",
        "font_consistency",
        "image_role_candidate",
    }
    return [
        {
            "type": item.get("value", {}).get("type"),
            "value": {
                key: value
                for key, value in item.get("value", {}).items()
                if key
                in {
                    "type",
                    "objects",
                    "object",
                    "parent",
                    "children",
                    "edge",
                    "axis",
                    "distance",
                    "status",
                    "occupied_area_ratio",
                    "occupied_bbox",
                    "margins",
                    "text_object_count",
                    "word_count",
                    "character_count",
                    "asset_id",
                    "display_bbox",
                    "analysis_target",
                    "reason",
                    "count",
                    "signature",
                    "basis",
                    "region",
                    "area_ratio",
                    "balance",
                    "horizontal_balance",
                    "vertical_balance",
                    "whitespace_area_ratio",
                    "native_connector_count",
                    "flow_candidate",
                    "evidence_sources",
                    "image_role",
                    "role",
                    "role_scores",
                    "role_evidence",
                    "selected_object_id",
                    "selected_text",
                    "candidate_count",
                    "excluded_candidates",
                    "weighting",
                    "visible_character_count",
                    "dominant_size_pt",
                    "dominant_character_ratio",
                    "weighted_size_variance",
                }
            },
        }
        for item in deck.rendered_evidence
        if item.get("slide_id") == slide_id
        and isinstance(item.get("value"), dict)
        and item["value"].get("type") in useful_types
    ]


def _graph_by_asset(deck: Any, slide_id: str, asset_id: str) -> list[dict[str, Any]]:
    return [
        item["value"]
        for item in deck.rendered_evidence
        if item.get("slide_id") == slide_id
        and isinstance(item.get("value"), dict)
        and item["value"].get("type") == "diagram_graph"
        and item["value"].get("asset_id") == asset_id
    ]


def _image_role_by_asset(deck: Any) -> dict[str, str]:
    roles: dict[str, str] = {}
    for item in deck.rendered_evidence:
        value = item.get("value") if isinstance(item.get("value"), dict) else {}
        asset_id, role = value.get("asset_id"), value.get("image_role", value.get("role"))
        if value.get("type") == "image_role_candidate" and isinstance(asset_id, str) and isinstance(role, str):
            roles.setdefault(asset_id, role)
    return roles


def _ocr_by_asset(deck: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in deck.ocr_evidence:
        value = item.get("value", {}) if isinstance(item.get("value"), dict) else {}
        if value.get("asset_id"):
            result[value["asset_id"]] = item
    return result


def _vision_evidence_refs(
    slide_id: str,
    slide_objects: list[dict[str, Any]],
    selected_asset_ids: Sequence[str],
    ocr_by_asset: Mapping[str, dict[str, Any]],
    graphs: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in slide_objects:
        if item.get("asset_id") in selected_asset_ids and item.get("id"):
            refs.append({"id": item["id"], "kind": "native_object"})
    for asset_id in selected_asset_ids:
        refs.append({"id": asset_id, "kind": "native_asset"})
        ocr_record = ocr_by_asset.get(asset_id)
        if ocr_record and ocr_record.get("id"):
            refs.append({"id": ocr_record["id"], "kind": "ocr_evidence"})
    for graph in graphs:
        asset_id = graph.get("asset_id") if isinstance(graph, dict) else None
        if asset_id:
            refs.append({"id": f"diagram-raster-{slide_id}-{asset_id}", "kind": "diagram_evidence"})
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for ref in refs:
        unique[(ref["id"], ref["kind"])] = ref
    return list(unique.values()) or [{"id": slide_id, "kind": "native_slide"}]


def _prompt(
    slide_id: str,
    metadata: dict[str, Any],
    native: list[dict[str, Any]],
    ocr: dict[str, Any],
    graphs: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> str:
    compact_ocr = {
        asset_id: {
            "status": value.get("status"),
            "confidence": value.get("confidence"),
            "text": str(value.get("text", ""))[:4000],
            "lines": value.get("lines", [])[:128] if isinstance(value.get("lines"), list) else [],
            "word_count": len(value.get("words", [])) if isinstance(value.get("words"), list) else 0,
        }
        for asset_id, value in ocr.items()
        if isinstance(value, dict)
    }
    compact_graphs = [
        {
            "asset_id": graph.get("asset_id"),
            "status": graph.get("status"),
            "ocr_status": graph.get("ocr_status"),
            "node_recovery": graph.get("node_recovery"),
            "edge_verification": graph.get("edge_verification"),
            "diagram_flow_direction": graph.get("diagram_flow_direction"),
            "flow_present": graph.get("flow_present"),
            "missing_evidence": graph.get("missing_evidence", []),
            "nodes": [
                {
                    "id": node.get("id"),
                    "bbox": node.get("bbox"),
                    "text": str(node.get("text", ""))[:300],
                    "status": node.get("status"),
                }
                for node in graph.get("nodes", [])[:128]
            ],
            "edges": [
                {
                    "source": edge.get("source"),
                    "target": edge.get("target"),
                    "status": edge.get("status"),
                    "arrow_present": edge.get("arrow") is not None,
                }
                for edge in graph.get("edges", [])[:128]
            ],
        }
        for graph in graphs
        if isinstance(graph, dict)
    ]
    compact_facts = facts[:64]
    context = {
        "target": metadata,
        "ocr": compact_ocr,
        "native_object_candidates": native,
        "diagram_candidates": compact_graphs,
        "native_visual_facts": compact_facts,
    }
    return (
        "You are a forensic visual analyst. Interpret only the supplied slide or diagram images. "
        "Native OOXML is authoritative; do not claim that a visual observation overwrites it. "
        "Treat OCR and heuristic diagram edges as evidence, not truth. "
        "Use native candidates only to cross-check labels and coordinates; never convert the candidate list itself into nodes. "
        "Do not include logos, page numbers, watermarks, or decorative/template elements as diagram nodes or edges; "
        "report them only as observations. Never mark model-only nodes verified. "
        "Return exactly one JSON object matching the supplied schema, with no markdown and no extra fields. "
        "Use partial or unverified statuses whenever evidence is uncertain. "
        "Return slide_reading_order and diagram_flow_direction separately. Set flow_present to false only when "
        "the supplied evidence supports absence; otherwise use null when it cannot be determined. "
        f"Target slide: {slide_id}. Context JSON:\n{json.dumps(context, sort_keys=True, separators=(',', ':'))}"
    )


def _content_hash(prompt: str, images: Sequence[VisionImage], metadata: dict[str, Any]) -> str:
    payload = {
        "schema": VISION_SCHEMA_VERSION,
        "prompt_version": VISION_PROMPT_VERSION,
        "prompt": prompt,
        "metadata": metadata,
        "images": [{"label": item.label, "mime_type": item.mime_type, "sha256": item.sha256} for item in images],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _payload_confidence(payload: dict[str, Any]) -> float | None:
    values = [item["confidence"] for item in payload.get("observations", []) if isinstance(item, dict)]
    return min(round(sum(values) / len(values), 6), MODEL_CONFIDENCE_CAP) if values else None


def _estimated_cost_usd(usage: Any) -> float | None:
    if not isinstance(usage, dict):
        return None
    try:
        prompt_tokens = int(usage.get("promptTokenCount", 0))
        output_tokens = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
    except (TypeError, ValueError):
        return None
    if prompt_tokens < 0 or output_tokens < 0:
        return None
    return round(
        prompt_tokens * GEMINI_INPUT_USD_PER_MILLION / 1_000_000
        + output_tokens * GEMINI_OUTPUT_USD_PER_MILLION / 1_000_000,
        8,
    )


def _read_cache(path: Path, key: str) -> dict[str, Any] | None:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if cached.get("content_hash") != key or not isinstance(cached.get("analysis"), dict):
        return None
    valid, _ = validate_vision_payload(cached["analysis"])
    return cached if valid else None


def _write_cache(
    path: Path,
    key: str,
    payload: dict[str, Any],
    response_sha256: str,
    usage: dict[str, Any] | None = None,
    request_seconds: float | None = None,
    attempts: int = 1,
    sanitization: dict[str, Any] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "content_hash": key,
                "response_sha256": response_sha256,
                "analysis": payload,
                "usage": usage,
                "request_seconds": request_seconds,
                "attempts": attempts,
                "sanitization": sanitization,
                "estimated_cost_usd": _estimated_cost_usd(usage),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _upsert_vision_record(deck: Any, record: dict[str, Any]) -> dict[str, Any]:
    existing_index = next((index for index, item in enumerate(deck.vision_evidence) if item.get("id") == record["id"]), None)
    if existing_index is None:
        deck.add_evidence("vision_evidence", record)
    else:
        deck.vision_evidence[existing_index] = record
    return record


def _vision_record(
    *,
    slide_id: str,
    object_id: str | None,
    target: str,
    status: str,
    metadata: dict[str, Any],
    analysis: dict[str, Any] | None,
    evidence_refs: list[dict[str, Any]],
    source: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    evidence_status = _vision_evidence_status(status)
    value = {
        "type": "gemini_vision_analysis",
        "status": evidence_status,
        "model_status": status,
        "schema_version": VISION_SCHEMA_VERSION,
        "target": target,
        "metadata": metadata,
        "analysis": analysis,
        "error": error,
    }
    return {
        "id": f"vision-gemini-{target}",
        "slide_id": slide_id,
        "object_id": object_id,
        "bbox": [0.0, 0.0, 1.0, 1.0],
        "value": value,
        "status": evidence_status,
        "confidence": _payload_confidence(analysis) if analysis else None,
        "evidence_refs": evidence_refs or [{"id": slide_id, "kind": "native_slide"}],
        "source": {"layer": "vision_model", **source, "status": evidence_status},
    }


def _rendered_image(
    slide_id: str,
    deck: Any,
    rendered_images: Mapping[str, VisionImage | bytes] | None,
    rendered_dir: str | Path | None,
) -> VisionImage | None:
    provided = rendered_images or {}
    value = provided.get(slide_id) or provided.get(slide_id.removeprefix("slide-"))
    if isinstance(value, VisionImage):
        return value
    if isinstance(value, bytes):
        return VisionImage(f"rendered:{slide_id}", "image/png", value)
    root = Path(rendered_dir).expanduser().resolve() if rendered_dir else None
    if root is None:
        return None
    for suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        path = root / "rendered" / f"{slide_id}{suffix}"
        if not path.is_file():
            path = root / f"{slide_id}{suffix}"
        if path.is_file() and (mime := _mime_for_path(path)):
            try:
                return VisionImage(f"rendered:{slide_id}", mime, path.read_bytes())
            except OSError:
                return None
    return None


def _crop_rendered_image(rendered: VisionImage, objects: list[dict[str, Any]]) -> tuple[VisionImage, bool]:
    """Crop a rendered slide around selected image objects when possible."""
    boxes = [_round_bbox(item.get("bbox")) for item in objects]
    boxes = [box for box in boxes if box is not None]
    if not boxes:
        return rendered, False
    try:
        from PIL import Image

        with Image.open(BytesIO(rendered.data)) as opened:
            width, height = opened.size
            left = max(0.0, min(box[0] for box in boxes) - 0.04)
            top = max(0.0, min(box[1] for box in boxes) - 0.04)
            right = min(1.0, max(box[0] + box[2] for box in boxes) + 0.04)
            bottom = min(1.0, max(box[1] + box[3] for box in boxes) + 0.04)
            if right <= left or bottom <= top:
                return rendered, False
            cropped = opened.convert("RGB").crop((round(left * width), round(top * height), round(right * width), round(bottom * height)))
            output = BytesIO()
            cropped.save(output, format="PNG")
    except (ImportError, OSError, ValueError):
        return rendered, False
    return VisionImage(f"{rendered.label}:crop", "image/png", output.getvalue()), True


def _analyze_with_retries(
    adapter: VisionAdapter,
    prompt: str,
    images: Sequence[VisionImage],
    *,
    timeout: float,
    retries: int,
    retry_backoff: float,
) -> dict[str, Any]:
    """Execute one request and return auditable result metadata."""
    started = perf_counter()
    attempts = 0
    error: str | None = None
    for attempt in range(max(0, retries) + 1):
        attempts = attempt + 1
        try:
            response = adapter.analyze(prompt, images, timeout)
            if isinstance(response, dict):
                candidate = response
                response_text = json.dumps(response, sort_keys=True, separators=(",", ":"))
            else:
                response_text = response
                if not isinstance(response_text, str) or not response_text.strip():
                    raise VisionRequestError("vision response was empty")
                candidate = json.loads(response)
            valid, validation_error = validate_vision_payload(candidate)
            if not valid:
                raise VisionRequestError(validation_error)
            analysis, sanitization = _conservatize_payload(candidate)
            return {
                "status": "ok",
                "error": None,
                "analysis": analysis,
                "response_sha256": hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
                "usage": getattr(adapter, "last_usage", None),
                "estimated_cost_usd": _estimated_cost_usd(getattr(adapter, "last_usage", None)),
                "request_seconds": round(perf_counter() - started, 6),
                "attempts": attempts,
                "sanitization": sanitization,
            }
        except Exception as exc:
            error = str(exc)
            if attempt < max(0, retries) and (_retryable_vision_error(exc) or _invalid_vision_response(exc)):
                if retry_backoff > 0:
                    time.sleep(retry_backoff * (2**attempt))
    return {
        "status": "failed",
        "error": error,
        "analysis": None,
        "response_sha256": None,
        "usage": getattr(adapter, "last_usage", None),
        "estimated_cost_usd": _estimated_cost_usd(getattr(adapter, "last_usage", None)),
        "request_seconds": round(perf_counter() - started, 6),
        "attempts": attempts,
        "sanitization": None,
    }


def run_selective_vision(
    report: ExtractionReport,
    source: str | Path,
    *,
    slides: Sequence[int] | None = None,
    asset_ids: Sequence[str] | None = None,
    adapter: VisionAdapter | None = None,
    api_key: str | None = None,
    model: str | None = None,
    cache_dir: str | Path | None = None,
    ocr_cache_dir: str | Path | None = None,
    rendered_images: Mapping[str, VisionImage | bytes] | None = None,
    rendered_dir: str | Path | None = None,
    run_ocr_stage: bool = True,
    skip_ocr: bool = False,
    skip: bool = False,
    include_noise: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    retry_backoff: float = 0.25,
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    max_concurrency: int = 2,
    circuit_breaker_failures: int = DEFAULT_CIRCUIT_BREAKER_FAILURES,
) -> list[dict[str, Any]]:
    """Run selective Gemini analysis without changing native or derived facts."""
    if skip or report.canonical is None:
        return []
    load_dotenv()
    configured_model = model or os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    deck = report.canonical
    selected_slides = {f"slide-{number:02d}" for number in slides} if slides else set()
    selected_assets = set(asset_ids or [])
    objects_by_asset: dict[str, list[dict[str, Any]]] = {}
    objects_by_slide: dict[str, list[dict[str, Any]]] = {}
    for item in deck.objects:
        if item.get("slide_id"):
            objects_by_slide.setdefault(item["slide_id"], []).append(item)
        if item.get("type") == "image" and item.get("asset_id"):
            objects_by_asset.setdefault(item["asset_id"], []).append(item)
    if run_ocr_stage and not skip_ocr:
        run_ocr(report, source, slides=slides, asset_ids=asset_ids, cache_dir=ocr_cache_dir, skip=skip_ocr)
    ocr_by_asset = _ocr_by_asset(deck)
    deterministic_roles = _image_role_by_asset(deck)
    assets = {item.get("id"): item for item in deck.assets if item.get("id")}
    target_slides = set(selected_slides)
    if not target_slides:
        target_slides = {
            item["slide_id"]
            for asset_id, items in objects_by_asset.items()
            if not selected_assets or asset_id in selected_assets
            for item in items
        }
        if not target_slides:
            target_slides = {
                slide_id
                for slide_id, items in objects_by_slide.items()
                if any(item.get("type") in {"image", "smartart"} for item in items)
            }
    target_slides = {slide_id for slide_id in target_slides if slide_id in objects_by_slide or selected_slides}
    cache_root = _cache_root(cache_dir)
    vision_adapter = adapter or GeminiVisionAdapter(
        api_key=api_key,
        model=configured_model,
        thinking_budget=thinking_budget,
        max_output_tokens=max_output_tokens,
    )
    failures = 0
    circuit_open = False
    results: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    source_path = Path(source).expanduser().resolve()
    with zipfile.ZipFile(source_path) as archive:
        for slide_id in sorted(target_slides):
            slide_objects = objects_by_slide.get(slide_id, [])
            slide_image_ids = sorted({item["asset_id"] for item in slide_objects if item.get("type") == "image" and item.get("asset_id")})
            if selected_assets:
                slide_image_ids = [item for item in slide_image_ids if item in selected_assets]
            facts = _slide_facts(deck, slide_id)
            unsupported_smartart = any(
                item.get("type") == "smartart"
                and not (isinstance(item.get("smartart_data"), dict) and item["smartart_data"].get("nodes"))
                for item in slide_objects
            )
            selected_asset_ids: list[str] = []
            selection_reasons: dict[str, list[str]] = {}
            skipped_assets: dict[str, list[str]] = {}
            graph_values: list[dict[str, Any]] = []
            for asset_id in slide_image_ids:
                graphs = _graph_by_asset(deck, slide_id, asset_id)
                graph_values.extend(graphs)
                gate = _asset_gate(
                    asset_id,
                    slide_id,
                    slide_objects,
                    objects_by_asset,
                    ocr_by_asset.get(asset_id),
                    graphs,
                    facts,
                    unsupported_smartart,
                    deterministic_roles.get(asset_id),
                    include_noise=include_noise,
                )
                if gate["selected"]:
                    selected_asset_ids.append(asset_id)
                    selection_reasons[asset_id] = gate["reasons"]
                else:
                    skipped_assets[asset_id] = gate["reasons"]
            vision_refs = _vision_evidence_refs(slide_id, slide_objects, selected_asset_ids, ocr_by_asset, graph_values)
            rendered = _rendered_image(slide_id, deck, rendered_images, rendered_dir)
            rendered_cropped = False
            if rendered and selected_asset_ids:
                rendered, rendered_cropped = _crop_rendered_image(
                    rendered,
                    [item for item in slide_objects if item.get("asset_id") in selected_asset_ids],
                )
                if not unsupported_smartart and not any(
                    item.get("value", {}).get("type") == "native_rendered_geometry_mismatch" for item in facts
                ):
                    rendered = None
                    rendered_cropped = False
            only_noise_assets = bool(slide_image_ids) and bool(skipped_assets) and not selected_asset_ids and all(
                "noise_filtered" in reasons for reasons in skipped_assets.values()
            )
            if only_noise_assets:
                rendered = None
            slide_reasons = sorted({reason for reasons in selection_reasons.values() for reason in reasons})
            if unsupported_smartart and not slide_reasons:
                slide_reasons.append("unsupported_smartart_interpretation")
            if rendered and not slide_reasons:
                slide_reasons.append("selected_rendered_slide")
            images: list[VisionImage] = []
            asset_metadata: list[dict[str, Any]] = []
            try:
                for asset_id in selected_asset_ids:
                    asset = assets.get(asset_id)
                    if not asset:
                        continue
                    mime_type = asset.get("content_type")
                    if mime_type not in SUPPORTED_IMAGE_MIMES:
                        skipped_assets.setdefault(asset_id, []).append("unsupported_image_type")
                        continue
                    try:
                        data = archive.read(asset["part"])
                    except (KeyError, OSError, RuntimeError):
                        skipped_assets.setdefault(asset_id, []).append("asset_unavailable")
                        continue
                    images.append(VisionImage(f"asset:{asset_id}", mime_type, data))
                    asset_metadata.append({"asset_id": asset_id, "sha256": hashlib.sha256(data).hexdigest(), "mime_type": mime_type})
            except KeyError:
                pass
            if rendered:
                images.insert(0, rendered)
            if not images or not slide_reasons:
                metadata = {
                    "slide_id": slide_id,
                    "selected_asset_ids": selected_asset_ids,
                    "skipped_assets": skipped_assets,
                    "selection_reasons": selection_reasons,
                    "deterministic_image_roles": {asset_id: deterministic_roles.get(asset_id) for asset_id in selected_asset_ids},
                    "rendered_slide_included": bool(rendered),
                    "rendered_slide_cropped": rendered_cropped,
                    "reason": "noise_filtered" if skipped_assets and not selected_asset_ids and not rendered else "no_visual_input" if not images else "no_trigger",
                }
                results.append(
                    _upsert_vision_record(
                        deck,
                        _vision_record(
                            slide_id=slide_id,
                            object_id=None,
                            target=f"selection-{slide_id}",
                            status="skipped",
                            metadata=metadata,
                            analysis=None,
                            evidence_refs=vision_refs,
                            source={"model": getattr(vision_adapter, "version", configured_model), "adapter": getattr(vision_adapter, "name", "vision"), "prompt_version": VISION_PROMPT_VERSION, "cache_hit": False},
                            error=metadata["reason"],
                        ),
                    )
                )
                _set_slide_visibility(deck, slide_id, "vision", "not_requested")
                continue
            metadata = {
                "source_sha256": report.source_sha256,
                "slide_id": slide_id,
                "model": getattr(vision_adapter, "version", configured_model),
                "prompt_version": VISION_PROMPT_VERSION,
                "thinking_budget": thinking_budget,
                "max_output_tokens": max_output_tokens,
                "max_concurrency": max(1, max_concurrency),
                "selected_asset_ids": selected_asset_ids,
                "skipped_assets": skipped_assets,
                "selection_reasons": selection_reasons,
                "deterministic_image_roles": {asset_id: deterministic_roles.get(asset_id) for asset_id in selected_asset_ids},
                "slide_reasons": slide_reasons,
                "rendered_slide_included": bool(rendered),
                "rendered_slide_cropped": rendered_cropped,
                "asset_images": asset_metadata,
                "image_hashes": [{"label": image.label, "sha256": image.sha256} for image in images],
            }
            native = _native_candidates(slide_objects)
            ocr_context = {asset_id: ocr_by_asset.get(asset_id, {}).get("value", {}) for asset_id in selected_asset_ids}
            prompt = _prompt(slide_id, metadata, native, ocr_context, graph_values, facts)
            content_hash = _content_hash(prompt, images, metadata)
            metadata["content_hash"] = content_hash
            cache_path = cache_root / f"{content_hash}.json"
            cached = _read_cache(cache_path, content_hash)
            if cached:
                metadata["usage"] = cached.get("usage")
                metadata["estimated_cost_usd"] = cached.get("estimated_cost_usd", _estimated_cost_usd(cached.get("usage")))
                metadata["request_seconds"] = cached.get("request_seconds")
                metadata["attempts"] = cached.get("attempts", 0)
                metadata["sanitization"] = cached.get("sanitization")
                metadata["evidence_reconciliation"] = _reconcile_payload(cached["analysis"], native, ocr_context, graph_values)
                metadata["evidence_grounding"] = _evidence_grounding(cached["analysis"], native, ocr_context, graph_values)
                results.append(
                    _upsert_vision_record(
                        deck,
                        _vision_record(
                            slide_id=slide_id,
                            object_id=None,
                            target=slide_id,
                            status="ok",
                            metadata=metadata,
                            analysis=cached["analysis"],
                            evidence_refs=vision_refs,
                            source={"model": getattr(vision_adapter, "version", configured_model), "adapter": getattr(vision_adapter, "name", "vision"), "prompt_version": VISION_PROMPT_VERSION, "cache_hit": True, "response_sha256": cached.get("response_sha256"), "usage": cached.get("usage"), "estimated_cost_usd": cached.get("estimated_cost_usd", _estimated_cost_usd(cached.get("usage"))), "request_seconds": cached.get("request_seconds"), "attempts": cached.get("attempts", 0)},
                        ),
                    )
                )
                _set_slide_visibility(deck, slide_id, "vision", "partial")
                continue
            if isinstance(vision_adapter, GeminiVisionAdapter) and not vision_adapter.api_key:
                status, error, analysis = "unavailable", "GEMINI_API_KEY is not configured", None
            elif circuit_open:
                status, error, analysis = "circuit_open", "vision circuit breaker is open", None
            else:
                pending.append(
                    {
                        "slide_id": slide_id,
                        "metadata": metadata,
                        "prompt": prompt,
                        "images": images,
                        "cache_path": cache_path,
                        "content_hash": content_hash,
                        "native": native,
                        "ocr": ocr_context,
                        "graphs": graph_values,
                        "evidence_refs": vision_refs,
                    }
                )
                continue
            results.append(
                _upsert_vision_record(
                    deck,
                    _vision_record(
                        slide_id=slide_id,
                        object_id=None,
                        target=slide_id,
                        status=status,
                        metadata=metadata,
                        analysis=analysis,
                        evidence_refs=vision_refs,
                        source={"model": getattr(vision_adapter, "version", configured_model), "adapter": getattr(vision_adapter, "name", "vision"), "prompt_version": VISION_PROMPT_VERSION, "cache_hit": False},
                        error=error,
                    ),
                )
            )
            _set_slide_visibility(deck, slide_id, "vision", _vision_evidence_status(status))
    if pending:
        worker_count = min(max(1, max_concurrency), len(pending))
        if not isinstance(vision_adapter, GeminiVisionAdapter):
            worker_count = 1

        def analyze_job(job: dict[str, Any]) -> dict[str, Any]:
            job_adapter: VisionAdapter = vision_adapter
            if isinstance(vision_adapter, GeminiVisionAdapter):
                job_adapter = GeminiVisionAdapter(
                    api_key=vision_adapter.api_key,
                    model=vision_adapter.model,
                    thinking_budget=vision_adapter.thinking_budget,
                    max_output_tokens=vision_adapter.max_output_tokens,
                    endpoint=vision_adapter.endpoint,
                )
            return _analyze_with_retries(
                job_adapter,
                job["prompt"],
                job["images"],
                timeout=timeout,
                retries=retries,
                retry_backoff=retry_backoff,
            )

        if worker_count == 1:
            request_results = []
            sequential_failures = 0
            for job in pending:
                if sequential_failures >= max(1, circuit_breaker_failures):
                    request_results.append(
                        {
                            "status": "circuit_open",
                            "error": "vision circuit breaker is open",
                            "analysis": None,
                            "response_sha256": None,
                            "usage": None,
                            "estimated_cost_usd": None,
                            "request_seconds": 0.0,
                            "attempts": 0,
                            "sanitization": None,
                        }
                    )
                    continue
                request_result = analyze_job(job)
                request_results.append(request_result)
                sequential_failures = sequential_failures + 1 if request_result["status"] != "ok" else 0
        else:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="pptx-vision") as executor:
                request_results = list(executor.map(analyze_job, pending))
        for job, request_result in zip(pending, request_results):
            metadata = job["metadata"]
            status = request_result["status"]
            analysis = request_result["analysis"]
            metadata["usage"] = request_result["usage"]
            metadata["estimated_cost_usd"] = request_result["estimated_cost_usd"]
            metadata["request_seconds"] = request_result["request_seconds"]
            metadata["attempts"] = request_result["attempts"]
            metadata["sanitization"] = request_result["sanitization"]
            metadata["evidence_reconciliation"] = _reconcile_payload(
                analysis,
                job["native"],
                job["ocr"],
                job["graphs"],
            )
            metadata["evidence_grounding"] = _evidence_grounding(
                analysis,
                job["native"],
                job["ocr"],
                job["graphs"],
            )
            if status == "ok":
                _write_cache(
                    job["cache_path"],
                    job["content_hash"],
                    analysis,
                    request_result["response_sha256"],
                    request_result["usage"],
                    request_result["request_seconds"],
                    request_result["attempts"],
                    request_result["sanitization"],
                )
                failures = 0
            else:
                failures += 1
                if failures >= max(1, circuit_breaker_failures):
                    circuit_open = True
            results.append(
                _upsert_vision_record(
                    deck,
                    _vision_record(
                        slide_id=job["slide_id"],
                        object_id=None,
                        target=job["slide_id"],
                        status=status,
                        metadata=metadata,
                        analysis=analysis,
                        evidence_refs=job["evidence_refs"],
                        source={"model": getattr(vision_adapter, "version", configured_model), "adapter": getattr(vision_adapter, "name", "vision"), "prompt_version": VISION_PROMPT_VERSION, "cache_hit": False, "usage": metadata.get("usage"), "estimated_cost_usd": metadata.get("estimated_cost_usd"), "request_seconds": metadata.get("request_seconds"), "attempts": metadata.get("attempts", 0)},
                        error=request_result["error"],
                    ),
                )
            )
            _set_slide_visibility(deck, job["slide_id"], "vision", _vision_evidence_status(status))
    results.sort(key=lambda item: (item.get("slide_id", ""), item.get("id", "")))
    deck.vision_evidence.sort(key=lambda item: (item.get("slide_id", ""), item.get("id", "")))
    return results
