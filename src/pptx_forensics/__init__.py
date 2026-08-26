"""Package-aware forensic extraction for PowerPoint Open XML files."""

from .extractor import ExtractionError, extract_pptx
from .config import load_dotenv
from .metrics import compute_metrics
from .models import DECKIR_SCHEMA, DECKIR_SCHEMA_VERSION, EVIDENCE_STATUSES, DeckIR, ExtractionReport
from .ocr import OcrAdapter, OcrResult, TesseractOcrAdapter, run_ocr
from .diagrams import add_native_diagram_evidence, reconstruct_diagrams, reconstruct_raster_diagrams
from .render import parse_slide_range, render_selected_slides
from .validation import validate_with_openxml_sdk
from .visual import add_native_visual_evidence, rendered_geometry_evidence
from .vision import GeminiVisionAdapter, VisionAdapter, VisionImage, run_selective_vision, validate_vision_payload

__all__ = [
    "DeckIR",
    "DECKIR_SCHEMA",
    "DECKIR_SCHEMA_VERSION",
    "EVIDENCE_STATUSES",
    "ExtractionError",
    "ExtractionReport",
    "OcrAdapter",
    "OcrResult",
    "TesseractOcrAdapter",
    "compute_metrics",
    "extract_pptx",
    "add_native_visual_evidence",
    "add_native_diagram_evidence",
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
    "load_dotenv",
]
