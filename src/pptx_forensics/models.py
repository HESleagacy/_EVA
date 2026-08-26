"""Serializable data structures returned by the extractor."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any

DECKIR_SCHEMA = "deck-ir"
# Bump this value for every canonical schema change; validation pins v1.
DECKIR_SCHEMA_VERSION = "1.0"
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

    Native OOXML data belongs in the primary collections. Derived evidence is
    intentionally kept in separate collections so it cannot replace native
    facts.
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

    def to_canonical_json(self) -> str:
        """Return deterministic JSON suitable for hashes and golden files."""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
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
        if self.schema_version != DECKIR_SCHEMA_VERSION:
            raise ValueError(f"Unsupported DeckIR schema version: {self.schema_version}")
        if self.deck.get("schema") != DECKIR_SCHEMA or self.deck.get("schema_version") != DECKIR_SCHEMA_VERSION:
            raise ValueError("DeckIR deck metadata does not match the frozen schema")
        if self.provenance.get("schema") != DECKIR_SCHEMA or self.provenance.get("schema_version") != DECKIR_SCHEMA_VERSION:
            raise ValueError("DeckIR provenance does not match the frozen schema")
        for slide in self.slides:
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
            if slide["slide_reading_order"] not in READING_DIRECTIONS:
                raise ValueError(f"DeckIR slide {slide['id']} has an invalid slide_reading_order")
            if slide["diagram_flow_direction"] not in READING_DIRECTIONS:
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
                value not in EVIDENCE_STATUSES for value in visibility.values()
            ):
                raise ValueError(f"DeckIR slide {slide['id']} has invalid visual_evidence_visibility")
        for item in self.objects:
            missing = [key for key in OBJECT_KEYS if key not in item]
            if missing:
                raise ValueError(f"DeckIR object {item.get('id', '<unknown>')} is missing {missing}")
            bbox = item["bbox"]
            if not isinstance(bbox, list) or len(bbox) != 4:
                raise ValueError(f"DeckIR object {item['id']} must have a four-value bbox")
            source = item["source"]
            if source.get("layer") != "native_ooxml":
                raise ValueError(f"Native object {item['id']} has a non-native source layer")
        for collection, layer in EVIDENCE_LAYERS.items():
            for item in getattr(self, collection):
                self._validate_evidence_record(collection, item, layer)

    @staticmethod
    def _validate_evidence_record(collection: str, item: dict[str, Any], layer: str) -> None:
        required = {"id", "slide_id", "object_id", "bbox", "value", "status", "confidence", "source", "evidence_refs"}
        missing = required.difference(item)
        if missing:
            raise ValueError(f"{collection} evidence is missing {sorted(missing)}")
        if item["status"] not in EVIDENCE_STATUSES:
            raise ValueError(f"{collection} evidence has an invalid status: {item['status']}")
        source = item.get("source")
        if not isinstance(source, dict) or source.get("layer") != layer:
            raise ValueError(f"{collection} evidence must use source layer {layer}")
        confidence = item["confidence"]
        if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1):
            raise ValueError(f"{collection} evidence confidence must be between 0 and 1")
        bbox = item["bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"{collection} evidence bbox must contain four values")
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
        if evidence["status"] not in EVIDENCE_STATUSES:
            raise ValueError(f"Evidence status must be one of {sorted(EVIDENCE_STATUSES)}")
        confidence = evidence["confidence"]
        if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1):
            raise ValueError("Evidence confidence must be between 0 and 1")
        bbox = evidence["bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("Evidence bbox must contain four values")
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

    def to_dict(self) -> dict[str, Any]:
        if self.canonical is not None:
            return self.canonical.to_dict()
        return self.to_legacy_dict()

    def to_legacy_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("canonical", None)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_canonical_json(self) -> str:
        if self.canonical is None:
            raise ValueError("Canonical DeckIR is not available")
        return self.canonical.to_canonical_json()
