"""Serializable data structures returned by the extractor."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
from typing import Any, Mapping

DECKIR_SCHEMA = "deck-ir"
# Bump this value for every canonical schema change; validation pins v1.
DECKIR_SCHEMA_VERSION = "1.0"
NATIVE_SOURCE_LAYERS = frozenset({"native_ooxml", "native_pdf"})
EVIDENCE_STATUSES = frozenset(
    {
        "verified",
        "partial",
        "unverified",
        "failed",
        "not_requested",
        "not_applicable",
    }
)
READING_DIRECTIONS = frozenset(
    {
        "left_to_right",
        "right_to_left",
        "top_to_bottom",
        "bottom_to_top",
        "unknown",
    }
)
IMAGE_ROLES = frozenset(
    {
        "diagram",
        "screenshot",
        "chart",
        "evidence_image",
        "logo",
        "decorative_image",
        "template",
        "unknown",
    }
)
CANONICAL_KEYS = (
    "schema_version",
    "deck",
    "slides",
    "objects",
    "assets",
    "relationships",
    "visual_regions",
    "rendered_evidence",
    "ocr_evidence",
    "vision_evidence",
    "warnings",
    "provenance",
)
OBJECT_KEYS = (
    "id",
    "slide_id",
    "parent_id",
    "type",
    "shape_type",
    "bbox",
    "z_order",
    "text",
    "style",
    "geometry",
    "raw_style",
    "resolved_style",
    "inherited_from",
    "semantic_status",
    "relationships",
    "source",
)
EVIDENCE_LAYERS = {
    "visual_regions": "rendered_cv",
    "rendered_evidence": "rendered_cv",
    "ocr_evidence": "ocr",
    "vision_evidence": "vision_model",
}
CANONICAL_FIELD_TYPES = {
    "schema_version": str,
    "deck": dict,
    "slides": list,
    "objects": list,
    "assets": list,
    "relationships": list,
    "visual_regions": list,
    "rendered_evidence": list,
    "ocr_evidence": list,
    "vision_evidence": list,
    "warnings": list,
    "provenance": dict,
}


def _finite_float(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _validate_bbox(value: Any, context: str) -> None:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{context} must have a four-value bbox")
    if any(isinstance(item, bool) for item in value):
        raise ValueError(f"{context} bbox must contain finite numbers")
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{context} bbox must contain finite numbers") from exc
    if not all(math.isfinite(item) for item in numbers) or numbers[2] < 0 or numbers[3] < 0:
        raise ValueError(f"{context} bbox must contain finite numbers with non-negative size")


def _validate_finite(value: Any, context: str = "DeckIR") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{context} contains a non-finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_finite(item, f"{context}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_finite(item, f"{context}[{index}]")

@dataclass(frozen=True)
class PartRecord:
    name: str
    content_type: str
    size: int
    compressed_size: int
    crc32: str
    sha256: str
    is_xml: bool


@dataclass(frozen=True)
class RelationshipRecord:
    source_part: str
    relationship_id: str
    relationship_type: str
    target: str
    target_mode: str | None
    resolved_target: str | None


@dataclass
class SlideRecord:
    number: int
    part: str
    layout_part: str | None
    master_part: str | None
    theme_part: str | None
    text: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    hyperlinks: list[dict[str, Any]] = field(default_factory=list)
    alt_text: list[dict[str, str]] = field(default_factory=list)
    animations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class MediaRecord:
    part: str
    content_type: str
    size: int
    sha256: str


@dataclass
class DeckIR:
    """The stable interchange contract shared by all extraction adapters.

    Native source data belongs in the primary collections. Derived evidence is
    intentionally kept in separate collections so it cannot replace native
    facts. ``provenance.native_layer`` identifies the authoritative adapter.
    """

    deck: dict[str, Any]
    slides: list[dict[str, Any]]
    objects: list[dict[str, Any]]
    assets: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    rendered_evidence: list[dict[str, Any]]
    ocr_evidence: list[dict[str, Any]]
    vision_evidence: list[dict[str, Any]]
    warnings: list[str]
    provenance: dict[str, Any]
    visual_regions: list[dict[str, Any]] = field(default_factory=list)
    schema_version: str = DECKIR_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        # Keep this explicit: changing a field name is a schema change.
        self.validate()
        return {
            "schema_version": self.schema_version,
            "deck": self.deck,
            "slides": self.slides,
            "objects": self.objects,
            "assets": self.assets,
            "relationships": self.relationships,
            "visual_regions": self.visual_regions,
            "rendered_evidence": self.rendered_evidence,
            "ocr_evidence": self.ocr_evidence,
            "vision_evidence": self.vision_evidence,
            "warnings": self.warnings,
            "provenance": self.provenance,
        }

    def to_semantic_dict(self) -> dict[str, Any]:
        """Return the compact document-semantic projection for consumers."""
        from .output import semantic_dict

        return semantic_dict(self)

    def to_semantic_json(self) -> str:
        """Return deterministic JSON without parser internals or telemetry."""
        from .output import render_semantic_json

        return render_semantic_json(self)

    def to_markdown(self) -> str:
        """Return a descriptive Markdown document containing parsed semantics."""
        from .output import render_markdown

        return render_markdown(self)

    def to_evaluator_ir(self) -> Any:
        """Return the compact scoring projection without parser internals."""
        from .evaluator import build_evaluator_ir

        return build_evaluator_ir(self)

    def to_evaluator_dict(self) -> dict[str, Any]:
        return self.to_evaluator_ir().to_dict()

    def to_evaluator_json(self) -> str:
        return self.to_evaluator_ir().to_json()

    def to_canonical_json(self) -> str:
        """Return deterministic JSON suitable for hashes and golden files."""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def validate(self) -> None:
        """Validate the stable shape and authority boundaries."""
        payload = {
            "schema_version": self.schema_version,
            "deck": self.deck,
            "slides": self.slides,
            "objects": self.objects,
            "assets": self.assets,
            "relationships": self.relationships,
            "visual_regions": self.visual_regions,
            "rendered_evidence": self.rendered_evidence,
            "ocr_evidence": self.ocr_evidence,
            "vision_evidence": self.vision_evidence,
            "warnings": self.warnings,
            "provenance": self.provenance,
        }
        if tuple(payload) != CANONICAL_KEYS:
            raise ValueError("DeckIR top-level keys do not match the canonical schema")
        for key in CANONICAL_KEYS:
            expected = CANONICAL_FIELD_TYPES[key]
            if not isinstance(payload[key], expected):
                raise ValueError(f"DeckIR field {key} has an invalid type")
        if self.schema_version != DECKIR_SCHEMA_VERSION:
            raise ValueError(f"Unsupported DeckIR schema version: {self.schema_version}")
        if self.deck.get("schema") != DECKIR_SCHEMA or self.deck.get("schema_version") != DECKIR_SCHEMA_VERSION:
            raise ValueError("DeckIR deck metadata does not match the frozen schema")
        if self.provenance.get("schema") != DECKIR_SCHEMA or self.provenance.get("schema_version") != DECKIR_SCHEMA_VERSION:
            raise ValueError("DeckIR provenance does not match the frozen schema")
        native_layer = self.provenance.get("native_layer", "native_ooxml")
        if not isinstance(native_layer, str) or native_layer not in NATIVE_SOURCE_LAYERS:
            raise ValueError(f"DeckIR has an unsupported native source layer: {native_layer}")
        slide_size = self.deck.get("slide_size_emu")
        if slide_size is not None:
            if not isinstance(slide_size, list) or len(slide_size) != 2:
                raise ValueError("DeckIR slide_size_emu must contain two values")
            try:
                width, height = (float(value) for value in slide_size)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("DeckIR slide_size_emu must contain finite positive numbers") from exc
            if any(isinstance(value, bool) for value in slide_size) or not all(math.isfinite(value) and value > 0 for value in (width, height)):
                raise ValueError("DeckIR slide_size_emu must contain finite positive numbers")
        aspect_ratio = self.deck.get("slide_aspect_ratio")
        if aspect_ratio is not None:
            try:
                valid_aspect_ratio = (
                    not isinstance(aspect_ratio, bool)
                    and isinstance(aspect_ratio, (int, float))
                    and math.isfinite(float(aspect_ratio))
                    and aspect_ratio > 0
                )
            except (TypeError, ValueError, OverflowError):
                valid_aspect_ratio = False
            if not valid_aspect_ratio:
                raise ValueError("DeckIR slide_aspect_ratio must be finite and positive")
        slide_ids = set()
        slide_numbers = set()
        for slide in self.slides:
            if not isinstance(slide, Mapping):
                raise ValueError("DeckIR slides must be objects")
            missing = [
                key
                for key in (
                    "id",
                    "number",
                    "part",
                    "layout_part",
                    "master_part",
                    "theme_part",
                    "text",
                    "notes",
                    "hyperlinks",
                    "alt_text",
                    "animations",
                    "slide_reading_order",
                    "diagram_flow_direction",
                    "flow_present",
                    "flow_presence_basis",
                    "visual_region_ids",
                    "visual_evidence_visibility",
                )
                if key not in slide
            ]
            if missing:
                raise ValueError(f"DeckIR slide {slide.get('id', '<unknown>')} is missing {missing}")
            if not isinstance(slide["id"], str) or not slide["id"]:
                raise ValueError("DeckIR slides must have non-empty string IDs")
            if slide["id"] in slide_ids:
                raise ValueError(f"DeckIR has duplicate slide ID: {slide['id']}")
            slide_ids.add(slide["id"])
            if isinstance(slide["number"], bool) or not isinstance(slide["number"], int) or slide["number"] < 1:
                raise ValueError(f"DeckIR slide {slide['id']} has an invalid number")
            if slide["number"] in slide_numbers:
                raise ValueError(f"DeckIR has duplicate slide number: {slide['number']}")
            slide_numbers.add(slide["number"])
            if not isinstance(slide["slide_reading_order"], str) or slide["slide_reading_order"] not in READING_DIRECTIONS:
                raise ValueError(f"DeckIR slide {slide['id']} has an invalid slide_reading_order")
            if not isinstance(slide["diagram_flow_direction"], str) or slide["diagram_flow_direction"] not in READING_DIRECTIONS:
                raise ValueError(f"DeckIR slide {slide['id']} has an invalid diagram_flow_direction")
            if slide["flow_present"] is not None and not isinstance(slide["flow_present"], bool):
                raise ValueError(f"DeckIR slide {slide['id']} has an invalid flow_present value")
            if not isinstance(slide["flow_presence_basis"], str) or not slide["flow_presence_basis"]:
                raise ValueError(f"DeckIR slide {slide['id']} has an invalid flow_presence_basis")
            if not isinstance(slide["visual_region_ids"], list) or not all(isinstance(value, str) and value for value in slide["visual_region_ids"]):
                raise ValueError(f"DeckIR slide {slide['id']} has invalid visual_region_ids")
            if slide["flow_present"] is False and slide.get("flow_presence_basis") != "supported_absence":
                raise ValueError(f"DeckIR slide {slide['id']} cannot claim flow absence without supported evidence")
            visibility = slide["visual_evidence_visibility"]
            if not isinstance(visibility, dict) or set(visibility) != {"native", "rendered", "ocr", "vision"} or any(
                not isinstance(value, str) or value not in EVIDENCE_STATUSES for value in visibility.values()
            ):
                raise ValueError(f"DeckIR slide {slide['id']} has invalid visual_evidence_visibility")
        object_ids = set()
        object_slide_ids: dict[str, str] = {}
        for item in self.objects:
            if not isinstance(item, Mapping):
                raise ValueError("DeckIR objects must be objects")
            missing = [key for key in OBJECT_KEYS if key not in item]
            if missing:
                raise ValueError(f"DeckIR object {item.get('id', '<unknown>')} is missing {missing}")
            if not isinstance(item["id"], str) or not item["id"]:
                raise ValueError("DeckIR objects must have non-empty string IDs")
            if item["id"] in object_ids:
                raise ValueError(f"DeckIR has duplicate object ID: {item['id']}")
            object_ids.add(item["id"])
            object_slide_ids[item["id"]] = item["slide_id"]
            if not isinstance(item.get("slide_id"), str) or item["slide_id"] not in slide_ids:
                raise ValueError(f"DeckIR object {item['id']} references an unknown slide")
            _validate_bbox(item["bbox"], f"DeckIR object {item['id']}")
            source = item["source"]
            if not isinstance(source, Mapping) or source.get("layer") != native_layer:
                raise ValueError(f"Native object {item['id']} has a non-native source layer")
        evidence_slide_ids = slide_ids
        for collection, layer in EVIDENCE_LAYERS.items():
            for item in getattr(self, collection):
                if not isinstance(item, Mapping):
                    raise ValueError(f"{collection} evidence records must be objects")
                if not isinstance(item.get("id"), str) or not item["id"]:
                    raise ValueError(f"{collection} evidence must have a non-empty ID")
                slide_id = item.get("slide_id")
                if not isinstance(slide_id, str) or slide_id not in evidence_slide_ids:
                    raise ValueError(f"{collection} evidence references an unknown slide")
                object_id = item.get("object_id")
                if object_id is not None and (not isinstance(object_id, str) or object_id not in object_ids):
                    raise ValueError(f"{collection} evidence references an unknown object")
                if object_id is not None and object_slide_ids[object_id] != slide_id:
                    raise ValueError(f"{collection} evidence references an object on another slide")
                self._validate_evidence_record(collection, item, layer)
        _validate_finite(payload)

    @staticmethod
    def _validate_evidence_record(collection: str, item: dict[str, Any], layer: str) -> None:
        if not isinstance(item, Mapping):
            raise ValueError(f"{collection} evidence records must be objects")
        required = {"id", "slide_id", "object_id", "bbox", "value", "status", "confidence", "source", "evidence_refs"}
        missing = required.difference(item)
        if missing:
            raise ValueError(f"{collection} evidence is missing {sorted(missing)}")
        if not isinstance(item["status"], str) or item["status"] not in EVIDENCE_STATUSES:
            raise ValueError(f"{collection} evidence has an invalid status: {item['status']}")
        source = item.get("source")
        if not isinstance(source, dict) or source.get("layer") != layer:
            raise ValueError(f"{collection} evidence must use source layer {layer}")
        confidence = item["confidence"]
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not _finite_float(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError(f"{collection} evidence confidence must be between 0 and 1")
        bbox = item["bbox"]
        _validate_bbox(bbox, f"{collection} evidence")
        refs = item["evidence_refs"]
        if not isinstance(refs, list) or not all(isinstance(ref, dict) and isinstance(ref.get("id"), str) and ref["id"] for ref in refs):
            raise ValueError(f"{collection} evidence references must be non-empty identified records")
        if item["status"] not in {"not_requested", "not_applicable"} and not refs:
            raise ValueError(f"{collection} claims require evidence_refs")

    def add_evidence(self, collection: str, evidence: dict[str, Any]) -> None:
        """Append derived evidence without allowing it to replace native data."""
        if collection not in EVIDENCE_LAYERS:
            raise ValueError(f"Unknown derived evidence collection: {collection}")
        required = {"id", "slide_id", "object_id", "bbox", "value", "confidence", "source"}
        required.update({"status", "evidence_refs"})
        missing = required.difference(evidence)
        if missing:
            raise ValueError(f"Evidence is missing {sorted(missing)}")
        if not isinstance(evidence["source"], dict) or evidence["source"].get("layer") != EVIDENCE_LAYERS[collection]:
            raise ValueError(f"Evidence source layer must be {EVIDENCE_LAYERS[collection]}")
        if not isinstance(evidence["status"], str) or evidence["status"] not in EVIDENCE_STATUSES:
            raise ValueError(f"Evidence status must be one of {sorted(EVIDENCE_STATUSES)}")
        confidence = evidence["confidence"]
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not _finite_float(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("Evidence confidence must be between 0 and 1")
        bbox = evidence["bbox"]
        _validate_bbox(bbox, "Evidence")
        refs = evidence["evidence_refs"]
        if not isinstance(refs, list) or not all(isinstance(ref, dict) and isinstance(ref.get("id"), str) and ref["id"] for ref in refs):
            raise ValueError("Evidence references must be a list of identified records")
        if evidence["status"] not in {"not_requested", "not_applicable"} and not refs:
            raise ValueError("Derived claims require evidence_refs")
        getattr(self, collection).append(evidence)


@dataclass
class ExtractionReport:
    source: str
    source_sha256: str
    parser_version: str
    dependencies: dict[str, str]
    package_parts: list[PartRecord]
    relationships: list[RelationshipRecord]
    slides: list[SlideRecord]
    media: list[MediaRecord]
    comments: list[dict[str, Any]] = field(default_factory=list)
    convenience: dict[str, Any] = field(default_factory=dict)
    evidence_dir: str | None = None
    warnings: list[str] = field(default_factory=list)
    canonical: DeckIR | None = None
    # PDF assets are kept in memory so optional evidence stages do not need to
    # reopen the source with a format-specific container assumption.
    asset_bytes_by_id: dict[str, bytes] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Return the full DeckIR contract used by pipeline diagnostics."""
        if self.canonical is not None:
            return self.canonical.to_dict()
        return self.to_legacy_dict()

    def to_semantic_dict(self) -> dict[str, Any]:
        """Return the compact document-semantic projection for consumers."""
        from .output import semantic_dict

        if self.canonical is None:
            return self.to_dict()
        return semantic_dict(self)

    def to_debug_dict(self) -> dict[str, Any]:
        """Return the internal/full report for pipeline diagnostics and evaluation."""
        if self.canonical is not None:
            payload = self.canonical.to_dict()
            payload["comments"] = self.comments
            payload["convenience"] = self.convenience
            if self.evidence_dir is not None:
                payload["evidence_dir"] = self.evidence_dir
            return payload
        return self.to_legacy_dict()

    def to_legacy_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("canonical", None)
        payload.pop("asset_bytes_by_id", None)
        return payload

    def asset_bytes(self, asset_id: str) -> bytes | None:
        """Return retained native asset bytes for optional evidence stages."""
        return self.asset_bytes_by_id.get(asset_id)

    def to_json(self) -> str:
        from .output import render_semantic_json

        if self.canonical is not None:
            return render_semantic_json(self).rstrip("\n")
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_semantic_json(self) -> str:
        """Return the consumer-facing semantic JSON representation."""
        return self.to_json()

    def to_markdown(self) -> str:
        """Return a descriptive Markdown document containing parsed semantics."""
        from .output import render_markdown

        if self.canonical is None:
            raise ValueError("Markdown output requires a canonical DeckIR report")
        return render_markdown(self)

    def to_evaluator_ir(self) -> Any:
        """Return the compact scoring projection without parser internals."""
        from .evaluator import build_evaluator_ir

        return build_evaluator_ir(self)

    def to_evaluator_dict(self) -> dict[str, Any]:
        return self.to_evaluator_ir().to_dict()

    def to_evaluator_json(self) -> str:
        return self.to_evaluator_ir().to_json()

    def to_canonical_json(self) -> str:
        if self.canonical is None:
            raise ValueError("Canonical DeckIR is not available")
        return self.canonical.to_canonical_json()
