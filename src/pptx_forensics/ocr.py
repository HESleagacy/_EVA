"""Asset-scoped OCR with native-text gating and reproducible caching."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Protocol, Sequence
import zipfile

from .models import ExtractionReport

OCR_SCHEMA_VERSION = "ocr-v2"


def _evidence_status(status: str) -> str:
    return {
        "ok": "verified",
        "ocr_unavailable": "not_applicable",
        "ocr_failed": "failed",
    }.get(status, "failed")


def _failure_class(status: str) -> str | None:
    return "ocr_failure" if status != "ok" else None


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

class OcrAdapter(Protocol):
    """Engine contract used by the asset-scoped OCR pipeline."""

    name: str
    version: str

    def recognize(self, image: bytes, content_type: str) -> "OcrResult":
        ...


@dataclass(frozen=True)
class OcrResult:
    status: str
    text: str
    words: list[dict[str, Any]]
    lines: list[dict[str, Any]]
    image_width: int | None
    image_height: int | None
    confidence: float | None
    error: str | None = None
    failure_class: str | None = None


def _normalised_box(left: float, top: float, width: float, height: float, image_width: int, image_height: int) -> list[float]:
    return [
        round(left / image_width, 12),
        round(top / image_height, 12),
        round(width / image_width, 12),
        round(height / image_height, 12),
    ]


def _image_size(image: bytes) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(BytesIO(image)) as opened:
            return opened.size
    except ImportError as exc:
        raise RuntimeError("Pillow is not installed") from exc
    except Exception as exc:
        raise RuntimeError(f"image decode failed: {exc}") from exc


class TesseractOcrAdapter:
    """Tesseract TSV adapter returning word and line geometry."""

    name = "tesseract"

    def __init__(self, language: str = "eng", psm: int = 6, executable: str | None = None) -> None:
        self.language = language
        self.psm = psm
        self.executable = executable or shutil.which("tesseract")
        self.version = self._version()

    def _version(self) -> str:
        if not self.executable:
            return "unavailable"
        try:
            result = subprocess.run([self.executable, "--version"], capture_output=True, text=True, check=True)
            return result.stdout.splitlines()[0].strip() if result.stdout else "unknown"
        except (OSError, subprocess.CalledProcessError):
            return "unknown"

    def recognize(self, image: bytes, content_type: str) -> OcrResult:
        try:
            width, height = _image_size(image)
        except RuntimeError as exc:
            return OcrResult("ocr_unavailable", "", [], [], None, None, None, str(exc))
        if not self.executable:
            return OcrResult("ocr_unavailable", "", [], [], width, height, None, "tesseract is not installed")

        suffix = "." + content_type.split("/", 1)[-1].replace("jpeg", "jpg")
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix) as image_file:
                image_file.write(image)
                image_file.flush()
                result = subprocess.run(
                    [self.executable, image_file.name, "stdout", "--psm", str(self.psm), "-l", self.language, "tsv"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        except OSError as exc:
            return OcrResult("ocr_failed", "", [], [], width, height, None, str(exc))
        if result.returncode != 0:
            return OcrResult("ocr_failed", "", [], [], width, height, None, result.stderr.strip() or "tesseract failed")

        words: list[dict[str, Any]] = []
        line_words: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        rows = result.stdout.splitlines()
        for row in rows[1:]:
            columns = row.split("\t")
            if len(columns) < 12 or columns[0] != "5":
                continue
            text = columns[11].strip()
            if not text:
                continue
            try:
                left, top, box_width, box_height = (float(columns[index]) for index in (6, 7, 8, 9))
                confidence = float(columns[10]) / 100.0
            except ValueError:
                continue
            word = {
                "text": text,
                "bbox": _normalised_box(left, top, box_width, box_height, width, height),
                "confidence": round(max(0.0, min(1.0, confidence)), 6),
            }
            words.append(word)
            key = (columns[2], columns[3], columns[4])
            line_words.setdefault(key, []).append(word)

        lines: list[dict[str, Any]] = []
        for line_items in line_words.values():
            boxes = [item["bbox"] for item in line_items]
            left = min(item[0] for item in boxes)
            top = min(item[1] for item in boxes)
            right = max(item[0] + item[2] for item in boxes)
            bottom = max(item[1] + item[3] for item in boxes)
            lines.append(
                {
                    "text": " ".join(item["text"] for item in line_items),
                    "bbox": [round(left, 12), round(top, 12), round(right - left, 12), round(bottom - top, 12)],
                    "confidence": round(sum(item["confidence"] for item in line_items) / len(line_items), 6),
                }
            )
        confidence = round(sum(item["confidence"] for item in words) / len(words), 6) if words else None
        return OcrResult(
            "ok",
            "\n".join(item["text"] for item in lines),
            words,
            lines,
            width,
            height,
            confidence,
        )


def _cache_root(cache_dir: str | Path | None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).expanduser().resolve()
    return Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "pptx-forensics" / "ocr"


def _cache_key(asset_hash: str, adapter: OcrAdapter) -> str:
    language = getattr(adapter, "language", "")
    psm = getattr(adapter, "psm", "")
    return f"{asset_hash}+{adapter.name}+{adapter.version}+{language}+{psm}+{OCR_SCHEMA_VERSION}"


def _cache_path(cache_root: Path, key: str) -> Path:
    return cache_root / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"


def _asset_candidates(report: ExtractionReport, slides: Sequence[int] | None, min_dimension: int) -> list[dict[str, Any]]:
    if report.canonical is None:
        return []
    allowed_slides = {f"slide-{number:02d}" for number in slides} if slides else None
    image_objects = [item for item in report.canonical.objects if item.get("type") == "image"]
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for item in image_objects:
        if allowed_slides is None or item.get("slide_id") in allowed_slides:
            asset_id = item.get("asset_id")
            if asset_id:
                by_asset.setdefault(asset_id, []).append(item)
    candidates = []
    for asset in report.canonical.assets:
        if asset.get("type") != "media" or asset.get("id") not in by_asset:
            continue
        if any(item.get("text") for item in by_asset[asset["id"]]):
            continue  # Native text is authoritative; never OCR over it.
        candidates.append({"asset": asset, "objects": by_asset[asset["id"]]})
    return candidates


def run_ocr(
    report: ExtractionReport,
    source: str | Path,
    *,
    slides: Sequence[int] | None = None,
    asset_ids: Sequence[str] | None = None,
    adapter: OcrAdapter | None = None,
    cache_dir: str | Path | None = None,
    min_dimension: int = 256,
    skip: bool = False,
) -> list[dict[str, Any]]:
    """OCR displayed image assets only; never rasterize or OCR a full slide."""
    if skip or report.canonical is None:
        return []
    ocr_adapter = adapter or TesseractOcrAdapter()
    wanted = set(asset_ids or [])
    candidates = _asset_candidates(report, slides, min_dimension)
    cache_root = _cache_root(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    source_path = Path(source).expanduser().resolve()
    evidence: list[dict[str, Any]] = []

    with zipfile.ZipFile(source_path) as archive:
        for candidate in candidates:
            asset = candidate["asset"]
            if wanted and asset["id"] not in wanted:
                continue
            try:
                image = archive.read(asset["part"])
            except (KeyError, OSError, RuntimeError) as exc:
                result = OcrResult("ocr_failed", "", [], [], None, None, None, str(exc))
                cache_hit = False
                key = _cache_key(asset["sha256"], ocr_adapter)
            else:
                if not wanted:
                    try:
                        width, height = _image_size(image)
                    except RuntimeError:
                        width = height = 0
                    if max(width, height) < min_dimension:
                        continue
                key = _cache_key(asset["sha256"], ocr_adapter)
                cached = _cache_path(cache_root, key)
                cache_hit = cached.is_file()
                if cache_hit:
                    try:
                        result = OcrResult(**json.loads(cached.read_text(encoding="utf-8")))
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        cache_hit = False
                if not cache_hit:
                    result = ocr_adapter.recognize(image, asset["content_type"])
                    if result.status == "ok":
                        cached.write_text(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")), encoding="utf-8")

            first_object = candidate["objects"][0]
            evidence_status = _evidence_status(result.status)
            record = {
                "id": f"ocr-{asset['id']}",
                "slide_id": first_object["slide_id"],
                "object_id": first_object["id"],
                "bbox": [0.0, 0.0, 1.0, 1.0],
                "value": {
                    "status": evidence_status,
                    "engine_status": result.status,
                    "asset_id": asset["id"],
                    "asset_sha256": asset["sha256"],
                    "coordinate_space": "asset_normalized",
                    "text": result.text,
                    "words": result.words,
                    "lines": result.lines,
                    "image_size": [result.image_width, result.image_height],
                    "error": result.error,
                    "failure_class": result.failure_class or _failure_class(result.status),
                    "cache_key": key,
                    "native_text_used": False,
                },
                "status": evidence_status,
                "confidence": result.confidence,
                "evidence_refs": [
                    {"id": first_object["id"], "kind": "native_object"},
                    {"id": asset["id"], "kind": "native_asset"},
                ],
                "source": {
                    "layer": "ocr",
                    "engine": ocr_adapter.name,
                    "engine_version": ocr_adapter.version,
                    "adapter": ocr_adapter.__class__.__name__,
                    "cache_hit": cache_hit,
                    "status": evidence_status,
                },
            }
            existing_index = next(
                (index for index, item in enumerate(report.canonical.ocr_evidence) if item.get("id") == record["id"]),
                None,
            )
            if existing_index is None:
                report.canonical.add_evidence("ocr_evidence", record)
            else:
                report.canonical.ocr_evidence[existing_index] = record
            for slide_id in {item.get("slide_id") for item in candidate["objects"] if item.get("slide_id")}:
                slide_record = next((item for item in report.canonical.slides if item.get("id") == slide_id), None)
                if slide_record is not None:
                    visibility = slide_record.setdefault("visual_evidence_visibility", {})
                    visibility["ocr"] = _merge_stage_status(visibility.get("ocr", "not_requested"), evidence_status)
            evidence.append(record)
    return evidence
