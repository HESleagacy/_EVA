"""Package-aware forensic extraction for PowerPoint Open XML files."""

from .extractor import ExtractionError, extract_pptx
from .config import load_dotenv
from .metrics import compute_metrics
from .models import DECKIR_SCHEMA, DECKIR_SCHEMA_VERSION, EVIDENCE_STATUSES, IMAGE_ROLES, DeckIR, ExtractionReport
from .ocr import OcrAdapter, OcrResult, TesseractOcrAdapter, run_ocr
from .diagrams import add_native_diagram_evidence, classify_raster_failure_classes, reconstruct_diagrams, reconstruct_raster_diagrams
from .render import parse_slide_range, render_selected_slides
from .validation import validate_with_openxml_sdk
from .visual import add_native_visual_evidence, classify_image_role, rendered_geometry_evidence
from .evaluation import (
    bbox_iou,
    character_error_rate,
    confidence_calibration,
    evaluate_diagram,
    evaluate_gemini,
    evaluate_image_role,
    evaluate_ocr,
    evaluate_report,
    vision_completeness,
    word_precision_recall,
)
from .vision import GeminiVisionAdapter, VisionAdapter, VisionImage, run_selective_vision, validate_vision_payload
from .output import SEMANTIC_SCHEMA_VERSION, render_markdown, render_semantic_json, semantic_dict

__all__ = [
    "DeckIR",
    "DECKIR_SCHEMA",
    "DECKIR_SCHEMA_VERSION",
    "EVIDENCE_STATUSES",
    "IMAGE_ROLES",
    "ExtractionError",
    "ExtractionReport",
    "OcrAdapter",
    "OcrResult",
    "TesseractOcrAdapter",
    "compute_metrics",
    "extract_pptx",
    "add_native_visual_evidence",
    "add_native_diagram_evidence",
    "classify_raster_failure_classes",
    "reconstruct_diagrams",
    "parse_slide_range",
    "rendered_geometry_evidence",
    "render_selected_slides",
    "run_ocr",
    "reconstruct_raster_diagrams",
    "validate_with_openxml_sdk",
    "GeminiVisionAdapter",
    "VisionAdapter",
    "VisionImage",
    "run_selective_vision",
    "validate_vision_payload",
    "SEMANTIC_SCHEMA_VERSION",
    "semantic_dict",
    "render_markdown",
    "render_semantic_json",
    "load_dotenv",
    "classify_image_role",
    "bbox_iou",
    "character_error_rate",
    "confidence_calibration",
    "evaluate_diagram",
    "evaluate_gemini",
    "evaluate_image_role",
    "evaluate_ocr",
    "evaluate_report",
    "vision_completeness",
    "word_precision_recall",
]
