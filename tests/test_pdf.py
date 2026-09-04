from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from pptx_forensics import ExtractionError, extract_document, extract_pdf
from pptx_forensics.ocr import OcrResult, run_ocr
from pptx_forensics.pdf import _filter_names
from pptx_forensics.render import render_selected_pdf_pages
from pptx_forensics.vision import run_selective_vision


PDF_FIXTURE = Path(__file__).parents[1] / "SMART INDIA HACKATHON 2025.pdf"


@pytest.mark.skipif(not PDF_FIXTURE.is_file(), reason="PDF fixture is not present")
def test_extracts_native_pdf_fixture_and_retains_evidence(tmp_path: Path) -> None:
    source_bytes = PDF_FIXTURE.read_bytes()
    report = extract_pdf(PDF_FIXTURE, tmp_path / "evidence", include_visual_evidence=False)
    canonical = report.to_dict()

    assert report.source_sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert canonical["schema_version"] == "1.0"
    assert canonical["deck"]["source_format"] == "pdf"
    assert canonical["deck"]["pdf"]["version"] == "1.4"
    assert canonical["deck"]["pdf"]["page_count"] == 8
    assert canonical["provenance"]["native_layer"] == "native_pdf"
    assert len(canonical["slides"]) == 8
    assert canonical["slides"][0]["text"][0] == "SMART INDIA HACKATHON 2025"
    assert all(item["source"]["layer"] == "native_pdf" for item in canonical["objects"])
    assert sum(item["type"] == "text" for item in canonical["objects"]) > 0
    assert sum(item["type"] == "image" for item in canonical["objects"]) > 0
    assert sum(item["type"] == "vector" for item in canonical["objects"]) > 0
    assert canonical["slides"][0]["hyperlinks"][0]["target"].startswith("https://")
    assert all(
        annotation["bbox"] is not None
        for page in canonical["deck"]["pdf"]["page_records"]
        for annotation in page["annotations"]
    )
    assert canonical["assets"]
    assert all(report.asset_bytes(item["id"]) for item in canonical["assets"] if item["type"] == "media")

    evidence = tmp_path / "evidence"
    assert (evidence / "original.pdf").read_bytes() == source_bytes
    assert (evidence / "parts/document.pdf").read_bytes() == source_bytes
    assert (evidence / "pages/page-01.json").is_file()
    assert any((evidence / asset["part"]).is_file() for asset in canonical["assets"] if asset["type"] == "media")

    assert report.to_semantic_dict()["deck"]["source_format"] == "pdf"
    assert report.to_markdown().startswith("# Parsed PDF\n")


def test_pdf_filter_arrays_preserve_each_filter_name() -> None:
    assert _filter_names(["/FlateDecode", "/DCTDecode"]) == ["FlateDecode", "DCTDecode"]


def test_extract_document_dispatches_pdf(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    source = tmp_path / "dispatch.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    with source.open("wb") as stream:
        writer.write(stream)

    report = extract_document(source, include_visual_evidence=False)

    assert len(report.slides) == 1
    assert report.canonical is not None
    assert report.canonical.provenance["native_layer"] == "native_pdf"
    assert report.canonical.deck["pdf"]["page_sizes_points"] == [[100.0, 200.0]]


def test_pdf_rotation_is_reflected_in_displayed_page_dimensions(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    source = tmp_path / "rotated.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=200)
    page.rotate(90)
    with source.open("wb") as stream:
        writer.write(stream)

    report = extract_pdf(source, include_visual_evidence=False)
    page_record = report.canonical.slides[0]["native_pdf"]

    assert page_record["rotation"] == 90
    assert page_record["display_size_points"] == [200.0, 100.0]
    assert report.canonical.deck["page_size_points"] == [200.0, 100.0]


def test_encrypted_pdf_requires_explicit_password(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    source = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    with source.open("wb") as stream:
        writer.write(stream)

    with pytest.raises(ExtractionError, match="explicit password"):
        extract_pdf(source, include_visual_evidence=False)

    report = extract_pdf(source, include_visual_evidence=False, password="secret")
    assert report.canonical.deck["pdf"]["security"]["encrypted"] is True
    assert report.canonical.deck["pdf"]["security"]["password_supplied"] is True


@pytest.mark.skipif(not PDF_FIXTURE.is_file(), reason="PDF fixture is not present")
def test_pdf_assets_are_available_to_ocr_without_reopening_as_zip(tmp_path: Path) -> None:
    class FakeOcr:
        name = "fake-ocr"
        version = "1.0"

        def recognize(self, image: bytes, content_type: str) -> OcrResult:
            assert image
            return OcrResult("ok", "native-stage-test", [], [], 10, 10, 1.0)

    report = extract_pdf(PDF_FIXTURE, include_visual_evidence=False)
    records = run_ocr(
        report,
        PDF_FIXTURE,
        slides=[1],
        adapter=FakeOcr(),
        cache_dir=tmp_path / "ocr-cache",
        min_dimension=0,
    )

    assert records
    assert len(report.canonical.ocr_evidence) == len(records)
    assert not any("could not open source package" in warning for warning in report.warnings)
    report.canonical.validate()


@pytest.mark.skipif(not PDF_FIXTURE.is_file() or shutil.which("pdftocairo") is None, reason="PDF fixture or Poppler is not present")
def test_pdf_pages_render_as_png_evidence(tmp_path: Path) -> None:
    report = extract_pdf(PDF_FIXTURE, tmp_path / "evidence", include_visual_evidence=False)
    records = render_selected_pdf_pages(
        report,
        PDF_FIXTURE,
        tmp_path / "evidence",
        [1],
        cache_dir=tmp_path / "render-cache",
        dpi=36,
    )

    assert records[0]["value"]["format"] == "png"
    assert records[0]["value"]["image_size"] == [360, 203]
    assert (tmp_path / "evidence/rendered/slide-01.png").is_file()
    assert report.canonical.slides[0]["visual_evidence_visibility"]["rendered"] == "verified"
    report.canonical.validate()


@pytest.mark.skipif(not PDF_FIXTURE.is_file(), reason="PDF fixture is not present")
def test_pdf_assets_are_available_to_vision_without_zip_assumptions(tmp_path: Path) -> None:
    class FakeVision:
        name = "fake-vision"
        version = "1.0"

        def analyze(self, prompt: str, images: list[object], timeout: float) -> str:
            assert images
            return json.dumps(
                {
                    "schema_version": "gemini-vision-v3",
                    "image_role": "screenshot",
                    "summary": "PDF asset",
                    "slide_reading_order": "unknown",
                    "diagram_flow_direction": "unknown",
                    "flow_present": None,
                    "nodes": [],
                    "edges": [],
                    "observations": [],
                }
            )

    report = extract_pdf(PDF_FIXTURE, include_visual_evidence=False)
    asset = next(item for item in report.canonical.assets if item["content_type"] == "image/jpeg")
    records = run_selective_vision(
        report,
        PDF_FIXTURE,
        slides=[1],
        asset_ids=[asset["id"]],
        adapter=FakeVision(),
        run_ocr_stage=False,
        include_noise=True,
        retries=0,
        retry_backoff=0,
        cache_dir=tmp_path / "vision-cache",
    )

    assert records[0]["value"]["metadata"]["asset_images"]
    assert records[0]["value"]["status"] == "partial"
    report.canonical.validate()
