from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import zipfile

import pytest

from pptx_forensics import diagrams
from pptx_forensics import DeckIR, ExtractionError, add_native_diagram_evidence, extract_pptx, load_dotenv, reconstruct_raster_diagrams, run_selective_vision
from pptx_forensics.metrics import compute_metrics
from pptx_forensics.ocr import OcrResult, TesseractOcrAdapter, run_ocr
from pptx_forensics.render import _svg_visual_features, parse_slide_range, render_selected_slides
from pptx_forensics.validation import validate_with_openxml_sdk
from pptx_forensics.visual import add_native_visual_evidence, rendered_geometry_evidence


def _package(path: Path) -> bytes:
    parts = {
        "[Content_Types].xml": b'''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>''',
        "ppt/presentation.xml": b'''<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="1" r:id="rId1"/></p:sldIdLst></p:presentation>''',
        "ppt/slides/slide1.xml": b'''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="2" name="Title" descr="A useful description"/><p:nvPr/></p:nvSpPr><p:txBody><a:p><a:r><a:t>Forensic text</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld><p:timing><p:par/></p:timing><a:hlinkClick xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="rIdLink"/></p:sld>''',
        "ppt/slideLayouts/slideLayout1.xml": b"<layout/>",
        "ppt/slideMasters/slideMaster1.xml": b"<master/>",
        "ppt/theme/theme1.xml": b"<theme/>",
        "ppt/notesSlides/notesSlide1.xml": b'''<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>Presenter note</a:t></p:notes>''',
        "ppt/comments/comment1.xml": b'''<p:cmLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cm authorId="7"><a:t xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">Review this</a:t></p:cm></p:cmLst>''',
        "ppt/media/image1.png": b"fake image bytes",
        "_rels/.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdRoot" Type="officeDocument" Target="ppt/presentation.xml"/></Relationships>''',
        "ppt/_rels/presentation.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="/slide" Target="slides/slide1.xml"/></Relationships>''',
        "ppt/slides/_rels/slide1.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdLayout" Type="/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rIdNotes" Type="/notesSlide" Target="../notesSlides/notesSlide1.xml"/><Relationship Id="rIdLink" Type="/hyperlink" Target="https://example.test" TargetMode="External"/></Relationships>''',
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdMaster" Type="/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>''',
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdTheme" Type="/theme" Target="../theme/theme1.xml"/></Relationships>''',
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path.read_bytes()


def _feature_package(path: Path) -> bytes:
    parts = {
        "[Content_Types].xml": b'''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Default Extension="bin" ContentType="application/octet-stream"/></Types>''',
        "ppt/presentation.xml": b'''<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldSz cx="1000" cy="1000"/><p:sldIdLst><p:sldId id="1" r:id="rId1"/></p:sldIdLst></p:presentation>''',
        "ppt/slides/slide1.xml": b'''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"><p:cSld><p:spTree><p:grpSp><p:nvGrpSpPr><p:cNvPr id="1" name="Group"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="100" y="100"/><a:ext cx="500" cy="500"/><a:chOff x="100" y="100"/><a:chExt cx="500" cy="500"/></a:xfrm></p:grpSpPr><p:sp><p:nvSpPr><p:cNvPr id="2" name="Grouped text"/><p:nvSpPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="150" y="150"/><a:ext cx="300" cy="100"/></a:xfrm></p:spPr><p:txBody><a:p><a:r><a:t>Grouped text</a:t></a:r></a:p></p:txBody></p:sp></p:grpSp><p:cxnSp><p:nvCxnSpPr><p:cNvPr id="3" name="Connector"/><p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr><p:spPr><a:xfrm><a:off x="10" y="10"/><a:ext cx="100" cy="20"/></a:xfrm></p:spPr></p:cxnSp><p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="4" name="Table"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="10" y="200"/><a:ext cx="300" cy="200"/></p:xfrm><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl/></a:graphicData></a:graphic></p:graphicFrame><p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="5" name="Chart"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="10" y="420"/><a:ext cx="300" cy="200"/></p:xfrm><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:chart r:id="rIdChart"/></a:graphicData></a:graphic></p:graphicFrame><p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="6" name="SmartArt"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="400" y="420"/><a:ext cx="300" cy="200"/></p:xfrm><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/diagram"><dgm:relIds r:dm="rIdSmart"/></a:graphicData></a:graphic></p:graphicFrame><p:pic><p:nvPicPr><p:cNvPr id="7" name="Image"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="rIdImage"/><a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr><a:xfrm><a:off x="500" y="10"/><a:ext cx="200" cy="200"/></a:xfrm></p:spPr></p:pic><p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="8" name="Embedded object"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="700" y="10"/><a:ext cx="200" cy="200"/></p:xfrm><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/presentationml/2006/ole"><p:oleObj r:id="rIdOle"/></a:graphicData></a:graphic></p:graphicFrame><p:sp><p:nvSpPr><p:cNvPr id="9" name="Rotated"/><p:nvSpPr/></p:nvSpPr><p:spPr><a:xfrm rot="5400000"><a:off x="100" y="700"/><a:ext cx="400" cy="200"/></a:xfrm></p:spPr><p:txBody><a:p><a:r><a:t>Rotated</a:t></a:r></a:p></p:txBody><p:style><a:hlinkClick r:id="rIdLink"/></p:style></p:sp></p:spTree></p:cSld></p:sld>''',
        "ppt/slideLayouts/slideLayout1.xml": b"<layout/>",
        "ppt/slideMasters/slideMaster1.xml": b"<master/>",
        "ppt/theme/theme1.xml": b"<theme/>",
        "ppt/charts/chart1.xml": b'''<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><c:chart><c:title><c:tx><c:rich><a:p><a:r><a:t>Revenue</a:t></a:r></a:p></c:rich></c:tx></c:title><c:plotArea><c:barChart><c:ser><c:tx><c:v>Actuals</c:v></c:tx><c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>Jan</c:v></c:pt><c:pt idx="1"><c:v>Feb</c:v></c:pt></c:strCache></c:strRef></c:cat><c:val><c:numRef><c:numCache><c:pt idx="0"><c:v>10</c:v></c:pt><c:pt idx="1"><c:v>20</c:v></c:pt></c:numCache></c:numRef></c:val></c:ser></c:barChart><c:catAx><c:axId val="1"/><c:title><c:tx><c:rich><a:p><a:r><a:t>Months</a:t></a:r></a:p></c:rich></c:tx></c:title></c:catAx><c:valAx><c:axId val="2"/></c:valAx></c:plotArea></c:chart></c:chartSpace>''',
        "ppt/diagrams/data1.xml": b'''<dgm:data xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><dgm:ptLst><dgm:pt modelId="1" type="root"><dgm:t><a:p><a:r><a:t>Root</a:t></a:r></a:p></dgm:t></dgm:pt><dgm:pt modelId="2" type="child"><dgm:t><a:p><a:r><a:t>Child</a:t></a:r></a:p></dgm:t></dgm:pt></dgm:ptLst><dgm:cxnLst><dgm:cxn modelId="3" srcId="1" destId="2" type="parOf"/></dgm:cxnLst></dgm:data>''',
        "ppt/media/image1.png": b"image",
        "ppt/embeddings/oleObject1.bin": b"embedded",
        "ppt/notesSlides/notesSlide1.xml": b'''<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>Note</a:t></p:notes>''',
        "_rels/.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdRoot" Type="officeDocument" Target="ppt/presentation.xml"/></Relationships>''',
        "ppt/_rels/presentation.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="/slide" Target="slides/slide1.xml"/></Relationships>''',
        "ppt/slides/_rels/slide1.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdLayout" Type="/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rIdChart" Type="/chart" Target="../charts/chart1.xml"/><Relationship Id="rIdSmart" Type="/diagramData" Target="../diagrams/data1.xml"/><Relationship Id="rIdImage" Type="/image" Target="../media/image1.png"/><Relationship Id="rIdOle" Type="/oleObject" Target="../embeddings/oleObject1.bin"/><Relationship Id="rIdNotes" Type="/notesSlide" Target="../notesSlides/notesSlide1.xml"/><Relationship Id="rIdLink" Type="/hyperlink" Target="https://example.test" TargetMode="External"/></Relationships>''',
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdMaster" Type="/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>''',
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdTheme" Type="/theme" Target="../theme/theme1.xml"/></Relationships>''',
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path.read_bytes()


def _style_package(path: Path) -> bytes:
    parts = {
        "[Content_Types].xml": b'''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>''',
        "ppt/presentation.xml": b'''<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="1" r:id="rId1"/></p:sldIdLst></p:presentation>''',
        "ppt/slides/slide1.xml": b'''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="2" name="Styled title"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:spPr><a:solidFill><a:schemeClr val="accent1"><a:alpha val="50000"/></a:schemeClr></a:solidFill><a:ln w="12700"><a:solidFill><a:srgbClr val="00ff00"/></a:solidFill></a:ln></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:pPr algn="ctr"><a:lnSpc><a:spcPct val="120000"/></a:lnSpc></a:pPr><a:r><a:t>Styled title</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>''',
        "ppt/slideLayouts/slideLayout1.xml": b'''<p:sldLayout xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:cNvPr id="2" name="Title placeholder"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:defRPr sz="1800"><a:latin typeface="LayoutFont"/></a:defRPr></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sldLayout>''',
        "ppt/slideMasters/slideMaster1.xml": b'''<p:sldMaster xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:spTree/></p:cSld></p:sldMaster>''',
        "ppt/theme/theme1.xml": b'''<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:themeElements><a:clrScheme name="Test"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="ffffff"/></a:lt1><a:accent1><a:srgbClr val="112233"/></a:accent1></a:clrScheme><a:fontScheme name="Test"><a:majorFont><a:latin typeface="MajorFont"/></a:majorFont><a:minorFont><a:latin typeface="ThemeFont"/></a:minorFont></a:fontScheme></a:themeElements></a:theme>''',
        "_rels/.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdRoot" Type="officeDocument" Target="ppt/presentation.xml"/></Relationships>''',
        "ppt/_rels/presentation.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="/slide" Target="slides/slide1.xml"/></Relationships>''',
        "ppt/slides/_rels/slide1.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdLayout" Type="/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>''',
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdMaster" Type="/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>''',
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdTheme" Type="/theme" Target="../theme/theme1.xml"/></Relationships>''',
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path.read_bytes()


def _geometry_package(path: Path) -> bytes:
    parts = {
        "[Content_Types].xml": b'''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/></Types>''',
        "ppt/presentation.xml": b'''<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldSz cx="1000" cy="1000"/><p:sldIdLst><p:sldId id="1" r:id="rId1"/></p:sldIdLst></p:presentation>''',
        "ppt/slides/slide1.xml": b'''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name="Root"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSp><p:nvGrpSpPr><p:cNvPr id="2" name="Scaled group"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="100" y="100"/><a:ext cx="800" cy="400"/><a:chOff x="0" y="0"/><a:chExt cx="400" cy="200"/></a:xfrm></p:grpSpPr><p:sp><p:nvSpPr><p:cNvPr id="3" name="Group child"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="100" y="50"/><a:ext cx="100" cy="50"/></a:xfrm></p:spPr></p:sp></p:grpSp><p:cxnSp><p:nvCxnSpPr><p:cNvPr id="4" name="Flipped connector"/><p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr><p:spPr><a:xfrm flipH="1"><a:off x="100" y="700"/><a:ext cx="200" cy="100"/></a:xfrm><a:ln><a:headEnd type="triangle" w="med" len="med"/><a:tailEnd type="stealth"/></a:ln></p:spPr></p:cxnSp><p:pic><p:nvPicPr><p:cNvPr id="5" name="Cropped image"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="rIdImage"/><a:srcRect l="10000" t="20000" r="30000" b="40000"/><a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr><a:xfrm><a:off x="500" y="100"/><a:ext cx="200" cy="200"/></a:xfrm></p:spPr></p:pic></p:spTree></p:cSld></p:sld>''',
        "ppt/media/image1.png": b"image",
        "_rels/.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdRoot" Type="officeDocument" Target="ppt/presentation.xml"/></Relationships>''',
        "ppt/_rels/presentation.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="/slide" Target="slides/slide1.xml"/></Relationships>''',
        "ppt/slides/_rels/slide1.xml.rels": b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdImage" Type="/image" Target="../media/image1.png"/></Relationships>''',
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return path.read_bytes()


def test_extracts_package_evidence_and_slide_semantics(tmp_path: Path) -> None:
    source = tmp_path / "sample.pptx"
    source_bytes = _package(source)
    evidence = tmp_path / "evidence"

    report = extract_pptx(source, evidence)
    slide = report.slides[0]

    assert report.source_sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert {part.name for part in report.package_parts} >= {"ppt/slides/slide1.xml", "ppt/media/image1.png"}
    assert report.media[0].sha256 == hashlib.sha256(b"fake image bytes").hexdigest()
    assert slide.text == ["Forensic text"]
    assert slide.layout_part == "ppt/slideLayouts/slideLayout1.xml"
    assert slide.master_part == "ppt/slideMasters/slideMaster1.xml"
    assert slide.theme_part == "ppt/theme/theme1.xml"
    assert slide.notes == ["Presenter note"]
    assert slide.hyperlinks[0]["target"] == "https://example.test"
    assert slide.alt_text[0]["descr"] == "A useful description"
    assert slide.animations[0]["element"] == "timing"
    assert report.comments[0]["text"] == "Review this"
    assert (evidence / "original.pptx").read_bytes() == source_bytes
    assert (evidence / "parts/ppt/slides/slide1.xml").exists()
    assert (evidence / "manifest.json").exists()

    canonical = report.to_dict()
    assert set(canonical) == {
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
    }
    assert canonical["slides"][0]["id"] == "slide-01"
    assert canonical["objects"][0]["slide_id"] == "slide-01"
    assert set(canonical["objects"][0]) >= {
        "id",
        "slide_id",
        "parent_id",
        "type",
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
    }
    assert canonical["objects"][0]["source"]["layer"] == "native_ooxml"
    assert canonical["provenance"]["authority"]["vision_model"] == "probabilistic"
    assert report.to_legacy_dict()["source"] == report.source

    native_text = canonical["objects"][0]["text"]
    report.canonical.add_evidence(
        "vision_evidence",
        {
            "id": "vision-01",
            "slide_id": "slide-01",
            "object_id": canonical["objects"][0]["id"],
            "bbox": [0.1, 0.2, 0.3, 0.4],
            "value": {"description": "A title"},
            "status": "partial",
            "confidence": 0.8,
            "evidence_refs": [{"id": canonical["objects"][0]["id"], "kind": "native_object"}],
            "source": {"layer": "vision_model", "model": "test"},
        },
    )
    assert report.canonical.objects[0]["text"] == native_text
    assert len(report.to_dict()["vision_evidence"]) == 1


def test_deckir_v1_schema_audit_freezes_statuses_and_flow_fields(tmp_path: Path) -> None:
    source = tmp_path / "schema-v1.pptx"
    _feature_package(source)

    canonical = extract_pptx(source).to_dict()
    assert canonical["schema_version"] == "1.0"
    assert canonical["deck"]["schema"] == "deck-ir"
    assert canonical["deck"]["schema_version"] == "1.0"
    assert canonical["provenance"]["schema_version"] == "1.0"
    assert canonical["visual_regions"]
    allowed = {"verified", "partial", "unverified", "failed", "not_requested", "not_applicable"}
    for slide in canonical["slides"]:
        assert slide["slide_reading_order"] in {"left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top", "unknown"}
        assert slide["diagram_flow_direction"] in {"left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top", "unknown"}
        assert slide["flow_present"] in {True, None}
        if slide["flow_present"] is True:
            assert slide["flow_presence_basis"] in {"native_connector", "raster_edge_candidate"}
        assert set(slide["visual_evidence_visibility"]) == {"native", "rendered", "ocr", "vision"}
        assert set(slide["visual_evidence_visibility"].values()) <= allowed
    for collection in ("visual_regions", "rendered_evidence"):
        for evidence in canonical[collection]:
            assert evidence["status"] in allowed
            assert evidence["source"]
            assert "confidence" in evidence
            assert evidence["evidence_refs"]
    graph = next(item for item in canonical["rendered_evidence"] if item["value"].get("type") == "diagram_graph")
    assert graph["value"]["flow_present"] is True
    assert graph["value"]["diagram_flow_direction"] in {"left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top", "unknown"}
    assert "reading_direction" not in graph["value"]


def test_deckir_v1_rejects_schema_and_derived_claim_regressions(tmp_path: Path) -> None:
    source = tmp_path / "schema-regressions.pptx"
    _feature_package(source)

    report = extract_pptx(source)
    report.canonical.schema_version = "2.0"
    with pytest.raises(ValueError, match="Unsupported DeckIR schema version"):
        report.to_dict()

    report = extract_pptx(source)
    report.canonical.rendered_evidence[0]["evidence_refs"] = []
    with pytest.raises(ValueError, match="require evidence_refs"):
        report.to_dict()

    report = extract_pptx(source)
    report.canonical.rendered_evidence[0].pop("source")
    with pytest.raises(ValueError, match="missing"):
        report.to_dict()

    report = extract_pptx(source)
    report.canonical.rendered_evidence[0].pop("confidence")
    with pytest.raises(ValueError, match="missing"):
        report.to_dict()

    report = extract_pptx(source)
    report.canonical.slides[0]["flow_present"] = False
    report.canonical.slides[0]["flow_presence_basis"] = "undetermined"
    with pytest.raises(ValueError, match="flow absence"):
        report.to_dict()

    report = extract_pptx(source)
    report.canonical.slides[0]["flow_present"] = False
    report.canonical.slides[0]["flow_presence_basis"] = "supported_absence"
    report.to_dict()

    report = extract_pptx(source)
    not_requested = deepcopy(report.canonical.rendered_evidence[0])
    not_requested["status"] = "not_requested"
    not_requested["value"]["status"] = "not_requested"
    not_requested["evidence_refs"] = []
    report.canonical.rendered_evidence = [not_requested]
    report.to_dict()


def test_resolves_style_layers_without_replacing_native_style(tmp_path: Path) -> None:
    source = tmp_path / "styles.pptx"
    _style_package(source)

    object_record = extract_pptx(source).to_dict()["objects"][0]

    assert object_record["raw_style"]["shape"]["fill"]["color"] == "accent1"
    assert object_record["resolved_style"]["fill"]["color"] == "112233"
    assert object_record["resolved_style"]["fill"]["transparency"] == 0.5
    assert object_record["resolved_style"]["font_family"] == "LayoutFont"
    assert object_record["resolved_style"]["font_size_pt"] == 18.0
    assert object_record["resolved_style"]["paragraph"]["alignment"] == "ctr"
    assert object_record["inherited_from"]["fill"] == "shape"
    assert object_record["inherited_from"]["font_family"] == "layout"
    assert object_record["semantic_status"] == "extracted"


def test_tracks_group_transforms_connectors_and_crops(tmp_path: Path) -> None:
    source = tmp_path / "geometry.pptx"
    _geometry_package(source)

    objects = extract_pptx(source).to_dict()["objects"]
    group = next(item for item in objects if item["name"] == "Scaled group")
    child = next(item for item in objects if item["name"] == "Group child")
    connector = next(item for item in objects if item["name"] == "Flipped connector")
    image = next(item for item in objects if item["name"] == "Cropped image")

    assert group["bbox"] == [0.1, 0.1, 0.8, 0.4]
    assert child["bbox"] == [0.3, 0.2, 0.2, 0.1]
    assert child["geometry"]["transform_chain"] == [group["id"], child["id"]]
    assert child["geometry"]["bbox_emu"] == [300.0, 200.0, 200.0, 100.0]
    assert connector["geometry"]["transform"]["flip_h"] is True
    assert connector["geometry"]["connector"]["start_emu"] == [300.0, 700.0]
    assert connector["geometry"]["connector"]["end_emu"] == [100.0, 800.0]
    assert connector["geometry"]["connector"]["begin_arrow"] == {"type": "triangle", "w": "med", "len": "med"}
    assert connector["geometry"]["connector"]["end_arrow"] == {"type": "stealth"}
    assert image["geometry"]["crop"] == {"left": 0.1, "top": 0.2, "right": 0.3, "bottom": 0.4}


def test_native_visual_geometry_is_reproducible_and_asset_scoped(tmp_path: Path) -> None:
    source = tmp_path / "visual.pptx"
    _feature_package(source)

    first = extract_pptx(source).to_dict()
    second = extract_pptx(source).to_dict()
    assert first["rendered_evidence"] == second["rendered_evidence"]

    facts = first["rendered_evidence"]
    fact_types = {item["value"]["type"] for item in facts}
    assert {
        "slide_occupancy",
        "whitespace_region",
        "margins",
        "object_overlap",
        "alignment",
        "font_size_distribution",
        "color_consistency",
        "shape_density",
        "text_density",
        "image_asset_analysis",
    } <= fact_types
    assert all(item["source"]["layer"] == "rendered_cv" for item in facts)
    image_fact = next(item for item in facts if item["value"]["type"] == "image_asset_analysis")
    assert image_fact["value"]["analysis_target"] == "original_asset"
    assert image_fact["value"]["asset_id"] == "asset-0002"
    assert image_fact["value"]["decode_status"] == "header_unavailable"


def test_rendered_geometry_verification_is_optional_and_reports_mismatches(tmp_path: Path) -> None:
    source = tmp_path / "rendered-geometry.pptx"
    _geometry_package(source)
    report = extract_pptx(source)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000"><g data-ooxml-id="4"><path d="M 0 0 L 100 0 L 100 100 L 0 100 Z"/></g></svg>'

    evidence = rendered_geometry_evidence(report.canonical, 1, svg)
    mismatch = next(
        item
        for item in evidence
        if item["value"]["type"] == "native_rendered_geometry_mismatch"
        and item["value"]["reason"] == "bbox_delta_exceeds_tolerance"
    )
    assert mismatch["value"]["reason"] == "bbox_delta_exceeds_tolerance"
    assert mismatch["value"]["source"] == "aurochs_svg"
    assert any(item["value"]["type"] == "native_rendered_geometry_verification" for item in evidence)


def test_native_visual_facts_cover_spacing_overflow_and_are_idempotent() -> None:
    deck = DeckIR(
        deck={"slide_aspect_ratio": 1.0},
        slides=[{"id": "slide-01", "number": 1}],
        objects=[
            {"id": "shape-1", "slide_id": "slide-01", "type": "shape", "bbox": [0.1, 0.1, 0.1, 0.1], "z_order": 1},
            {"id": "shape-2", "slide_id": "slide-01", "type": "shape", "bbox": [0.3, 0.1, 0.1, 0.1], "z_order": 2},
            {"id": "shape-3", "slide_id": "slide-01", "type": "shape", "bbox": [0.5, 0.1, 0.1, 0.1], "z_order": 3},
            {"id": "shape-4", "slide_id": "slide-01", "type": "shape", "bbox": [-0.1, 0.3, 0.2, 0.1], "z_order": 4, "style": {"rotation_degrees": 15}},
            {"id": "shape-5", "slide_id": "slide-01", "type": "shape", "bbox": [0.13, 0.12, 0.1, 0.1], "z_order": 5},
            {"id": "group-1", "slide_id": "slide-01", "type": "group", "bbox": [0.1, 0.6, 0.3, 0.2], "z_order": 6},
            {"id": "group-child", "slide_id": "slide-01", "type": "shape", "parent_id": "group-1", "bbox": [0.15, 0.65, 0.1, 0.1], "z_order": 7},
        ],
        assets=[],
        relationships=[],
        rendered_evidence=[],
        ocr_evidence=[],
        vision_evidence=[],
        warnings=[],
        provenance={},
    )

    first = add_native_visual_evidence(deck)
    second = add_native_visual_evidence(deck)
    assert len(first) == len(second) == len(deck.rendered_evidence)
    types = {item["value"]["type"] for item in first}
    assert "equal_spacing" in types
    assert "clipping_overflow" in types
    assert {"alignment_mismatch", "rotation", "rotation_distribution", "shape_hierarchy_candidate", "shape_peer_group"} <= types
    mismatch = next(item for item in first if item["value"]["type"] == "alignment_mismatch")
    assert mismatch["value"]["source"] == "native_ooxml"
    assert mismatch["value"]["objects"] == ["shape-1", "shape-5"]
    assert mismatch["value"]["distance"] == 0.03


def test_native_diagram_graph_uses_connector_evidence_and_preserves_status() -> None:
    native_source = {"layer": "native_ooxml", "xml_part": "ppt/slides/slide1.xml", "xml_path": "/sld[1]", "confidence": 1.0}
    deck = DeckIR(
        deck={"slide_size_emu": [1000.0, 1000.0]},
        slides=[{"id": "slide-01", "number": 1}],
        objects=[
            {"id": "shape-1", "slide_id": "slide-01", "native_id": "2", "type": "shape", "bbox": [0.1, 0.4, 0.2, 0.1], "z_order": 1, "source": native_source},
            {"id": "shape-2", "slide_id": "slide-01", "native_id": "3", "type": "shape", "bbox": [0.7, 0.4, 0.2, 0.1], "z_order": 2, "source": native_source},
            {
                "id": "connector-1",
                "slide_id": "slide-01",
                "native_id": "4",
                "type": "connector",
                "bbox": [0.3, 0.45, 0.4, 0.01],
                "z_order": 3,
                "geometry": {
                    "connector": {
                        "start_emu": [300.0, 450.0],
                        "end_emu": [700.0, 450.0],
                        "start_connection": {"id": "2", "idx": "0"},
                        "end_connection": {"id": "3", "idx": "0"},
                        "end_arrow": {"type": "triangle"},
                    }
                },
                "source": native_source,
            },
        ],
        assets=[],
        relationships=[],
        rendered_evidence=[],
        ocr_evidence=[],
        vision_evidence=[],
        warnings=[],
        provenance={},
    )

    records = add_native_diagram_evidence(deck)
    graph = records[0]["value"]
    assert graph["status"] == "verified"
    assert graph["diagram_flow_direction"] == "left_to_right"
    assert graph["flow_present"] is True
    assert graph["edge_verification"] == 1.0
    assert graph["edges"][0]["source"] == graph["nodes"][0]["id"]
    assert graph["edges"][0]["target"] == graph["nodes"][1]["id"]
    assert graph["edges"][0]["arrowheads"]["end"] == {"type": "triangle"}
    assert graph["nodes"][0]["evidence_refs"]
    assert graph["edges"][0]["evidence_refs"]


def test_raster_diagram_missing_ocr_remains_unverified(tmp_path: Path) -> None:
    source = tmp_path / "raster-diagram.pptx"
    _feature_package(source)
    report = extract_pptx(source)

    records = reconstruct_raster_diagrams(
        report,
        source,
        slides=[1],
        asset_ids=["asset-0002"],
        run_ocr_stage=False,
        skip_ocr=True,
    )
    graph = records[0]["value"]
    assert graph["branch"] == "raster"
    assert graph["status"] in {"partial", "unverified", "failed"}
    assert graph["status"] != "verified"
    assert graph["ocr_status"] == "unverified"
    assert "ocr" in graph["missing_evidence"] or graph["status"] == "failed"
    assert all(node["evidence_refs"] for node in graph["nodes"])
    assert all(edge["evidence_refs"] for edge in graph["edges"])


def test_raster_candidate_edges_require_two_endpoints_and_stay_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeImage:
        def getdata(self) -> list[int]:
            return [255] * 100

    monkeypatch.setattr(diagrams, "_load_raster", lambda data: (FakeImage(), 10, 10, None))
    monkeypatch.setattr(
        diagrams,
        "_dark_runs",
        lambda values, width, height: ([{"orientation": "horizontal", "bbox_px": [1, 5, 8, 1]}], []),
    )
    monkeypatch.setattr(diagrams, "_detect_contours", lambda values, width, height: [])
    monkeypatch.setattr(diagrams, "_detect_boxes", lambda lines: [])
    monkeypatch.setattr(diagrams, "_detect_arrows", lambda values, width, height, lines: [])
    ocr = {
        "id": "ocr-01",
        "value": {
            "asset_id": "asset-01",
            "status": "verified",
            "lines": [
                {"text": "Start", "bbox": [0.05, 0.4, 0.1, 0.2]},
                {"text": "End", "bbox": [0.85, 0.4, 0.1, 0.2]},
            ],
        },
        "status": "verified",
        "evidence_refs": [{"id": "image-object-01", "kind": "native_object"}],
        "source": {"layer": "ocr"},
    }
    deck = DeckIR(
        deck={"slide_size_emu": [1000.0, 1000.0]},
        slides=[{"id": "slide-01", "number": 1}],
        objects=[
            {
                "id": "image-object-01",
                "slide_id": "slide-01",
                "type": "image",
                "asset_id": "asset-01",
                "bbox": [0.0, 0.0, 1.0, 1.0],
            }
        ],
        assets=[{"id": "asset-01", "part": "ppt/media/image1.png"}],
        relationships=[],
        rendered_evidence=[],
        ocr_evidence=[],
        vision_evidence=[],
        warnings=[],
        provenance={},
    )

    record = diagrams._raster_graph(deck, "slide-01", deck.assets[0], b"image", ocr)[0]
    graph = record["value"]
    assert graph["status"] == "unverified"
    assert graph["edge_verification"] == 0.0
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["source"] is not None
    assert graph["edges"][0]["target"] is not None
    assert graph["edges"][0]["evidence_refs"]


def test_extracts_deterministic_svg_visual_features() -> None:
    features = _svg_visual_features(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="0 0 100 50"><g data-ooxml-id="2"><path/><text>Words</text><image/></g></svg>'
    )

    assert features == {
        "status": "ok",
        "schema": "svg-features-v1",
        "width": "100",
        "height": "50",
        "view_box": ["0", "0", "100", "50"],
        "element_counts": {"g": 1, "image": 1, "path": 1, "svg": 1, "text": 1},
        "object_ids": ["2"],
        "text_nodes": 1,
        "path_nodes": 1,
        "image_nodes": 1,
    }


def test_rejects_path_traversal_in_zip_members(tmp_path: Path) -> None:
    source = tmp_path / "unsafe.pptx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("../outside.xml", b"<root/>")

    with pytest.raises(ExtractionError, match="Unsafe package part"):
        extract_pptx(source)


def test_rejects_malformed_and_external_entity_xml(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.pptx"
    with zipfile.ZipFile(malformed, "w") as archive:
        archive.writestr("ppt/presentation.xml", b"<p:presentation>")
    with pytest.raises(ExtractionError, match="Invalid XML"):
        extract_pptx(malformed)

    entity = tmp_path / "entity.pptx"
    with zipfile.ZipFile(entity, "w") as archive:
        archive.writestr(
            "ppt/presentation.xml",
            b'<!DOCTYPE root [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><root>&xxe;</root>',
        )
    with pytest.raises(ExtractionError, match="Invalid XML"):
        extract_pptx(entity)


def test_feature_fixture_and_reproducibility(tmp_path: Path) -> None:
    source = tmp_path / "features.pptx"
    _feature_package(source)

    first = extract_pptx(source)
    second = extract_pptx(source)
    objects = first.to_dict()["objects"]
    object_types = {item["type"] for item in objects}

    assert first.to_canonical_json() == second.to_canonical_json()
    assert {"text", "group", "connector", "table", "chart", "smartart", "image", "shape"} <= object_types
    assert any(item.get("embedded") for item in objects)
    assert all(item["semantic_status"] for item in objects)
    assert next(item for item in objects if item["type"] == "table")["table_data"] == {
        "rows": [],
        "row_count": 0,
        "column_count": 0,
    }
    chart = next(item for item in objects if item["type"] == "chart")["chart_data"]
    assert chart["part"] == "ppt/charts/chart1.xml"
    assert chart["type"] == "bar"
    assert chart["title"] == "Revenue"
    assert chart["series"][0]["title"] == "Actuals"
    assert chart["series"][0]["categories"] == [{"index": "0", "value": "Jan"}, {"index": "1", "value": "Feb"}]
    assert chart["axes"][0] == {"type": "catAx", "id": "1", "title": "Months"}
    smartart = next(item for item in objects if item["type"] == "smartart")["smartart_data"]
    assert smartart["part"] == "ppt/diagrams/data1.xml"
    assert smartart["nodes"] == [
        {"id": "1", "type": "root", "text": "Root"},
        {"id": "2", "type": "child", "text": "Child"},
    ]
    assert smartart["connections"] == [{"id": "3", "source": "1", "target": "2", "type": "parOf"}]
    assert any(item.get("embedded_object", {}).get("executed") is False for item in objects)
    rotated = next(item for item in objects if item["name"] == "Rotated")
    assert rotated["style"]["rotation_degrees"] == 90.0
    assert rotated["bbox"] == [0.2, 0.6, 0.2, 0.4]
    assert first.slides[0].notes == ["Note"]
    assert compute_metrics(
        first,
        {
            "text": ["Grouped text", "Rotated"],
            "objects": [{"id": rotated["id"], "bbox": rotated["bbox"]}],
            "relationships": [{"id": "ppt/slides/slide1.xml:rIdImage"}],
            "assets": [{"part": "ppt/media/image1.png"}],
        },
    ) == {
        "text_recall": 1.0,
        "object_recall": 1.0,
        "bounding_box_accuracy": 1.0,
        "relationship_resolution": 1.0,
        "asset_resolution": 1.0,
    }


def test_native_shape_subtypes_and_native_only_extraction(tmp_path: Path) -> None:
    source = tmp_path / "subtypes.pptx"
    _feature_package(source)

    report = extract_pptx(source)
    objects = report.to_dict()["objects"]
    assert next(item for item in objects if item["type"] == "group")["shape_type"] == "GROUP"
    assert next(item for item in objects if item["type"] == "connector")["shape_type"] == "CONNECTOR"
    assert next(item for item in objects if item["type"] == "image")["shape_type"] == "PICTURE"
    assert next(item for item in objects if item["type"] == "table")["shape_type"] == "TABLE"
    assert next(item for item in objects if item["type"] == "chart")["shape_type"] == "CHART"
    assert next(item for item in objects if item["type"] == "smartart")["shape_type"] == "SMARTART"

    native_only = extract_pptx(source, include_visual_evidence=False, include_native_diagrams=False).to_dict()
    assert native_only["objects"]
    assert native_only["rendered_evidence"] == []


def test_openxml_validator_is_optional() -> None:
    assert validate_with_openxml_sdk("missing.pptx")["status"] == "not_configured"


def test_native_only_mode_and_render_failure_are_non_fatal(tmp_path: Path) -> None:
    source = tmp_path / "native-only.pptx"
    _package(source)
    report = extract_pptx(source)

    native_facts = report.to_dict()["rendered_evidence"]
    assert any(item["value"]["type"] == "slide_occupancy" for item in native_facts)
    assert all(item["value"]["source"] != "aurochs_svg" for item in native_facts)
    assert parse_slide_range("1,3-4,4") == [1, 3, 4]
    evidence = tmp_path / "evidence"
    assert render_selected_slides(report, source, evidence, [1], renderer_root=tmp_path / "missing") == []
    assert any("renderer root does not exist" in warning for warning in report.warnings)
    assert report.to_dict()["slides"][0]["visual_evidence_visibility"]["rendered"] == "failed"


def test_render_success_records_valid_deckir_evidence(tmp_path: Path) -> None:
    source = tmp_path / "render-success.pptx"
    _feature_package(source)
    renderer_root = tmp_path / "renderer"
    renderer_root.mkdir()
    fake_bun = tmp_path / "fake-bun"
    fake_bun.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "output = Path(args[args.index('--output') + 1])\n"
        "output.mkdir(parents=True, exist_ok=True)\n"
        "path = output / 'slide-1.svg'\n"
        "path.write_text('<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 100 100\"/>', encoding='utf-8')\n"
        "print(json.dumps({'slides': [{'slide': 1, 'path': str(path), 'warnings': []}]}))\n",
        encoding="utf-8",
    )
    fake_bun.chmod(0o755)

    report = extract_pptx(source)
    evidence = render_selected_slides(
        report,
        source,
        tmp_path / "evidence",
        [1],
        renderer_root=renderer_root,
        bun_command=str(fake_bun),
    )
    canonical = report.to_dict()

    rendered_slide = next(item for item in evidence if item["value"].get("type") == "rendered_slide")
    assert rendered_slide["status"] == "verified"
    assert rendered_slide["source"]["layer"] == "rendered_cv"
    assert canonical["slides"][0]["visual_evidence_visibility"]["rendered"] == "verified"
    assert any(item["value"].get("type") == "native_rendered_geometry_verification" for item in canonical["rendered_evidence"])


def test_ocr_is_asset_scoped_cached_and_skippable(tmp_path: Path) -> None:
    source = tmp_path / "ocr.pptx"
    _feature_package(source)

    class FakeOcr:
        name = "fake-ocr"
        version = "1.0"

        def __init__(self) -> None:
            self.calls = 0

        def recognize(self, image: bytes, content_type: str) -> OcrResult:
            self.calls += 1
            return OcrResult(
                "ok",
                "Image words",
                [{"text": "Image", "bbox": [0.1, 0.2, 0.3, 0.1], "confidence": 0.9}],
                [{"text": "Image words", "bbox": [0.1, 0.2, 0.5, 0.1], "confidence": 0.9}],
                100,
                100,
                0.9,
            )

    first_adapter = FakeOcr()
    report = extract_pptx(source)
    first = run_ocr(report, source, asset_ids=["asset-0002"], adapter=first_adapter, cache_dir=tmp_path / "ocr-cache", min_dimension=0)
    assert first_adapter.calls == 1
    assert first[0]["value"]["status"] == "verified"
    assert first[0]["value"]["words"][0]["bbox"] == [0.1, 0.2, 0.3, 0.1]
    assert report.to_dict()["slides"][0]["visual_evidence_visibility"]["ocr"] == "verified"

    second_adapter = FakeOcr()
    second = run_ocr(report, source, asset_ids=["asset-0002"], adapter=second_adapter, cache_dir=tmp_path / "ocr-cache", min_dimension=0)
    assert second_adapter.calls == 0
    assert second[0]["source"]["cache_hit"] is True
    image_object = next(item for item in report.canonical.objects if item.get("asset_id") == "asset-0002")
    assert image_object["text"] == ""
    assert run_ocr(report, source, adapter=second_adapter, skip=True) == []

    unavailable = TesseractOcrAdapter(executable="/missing/tesseract")
    failure = run_ocr(report, source, asset_ids=["asset-0002"], adapter=unavailable, cache_dir=tmp_path / "unavailable", min_dimension=0)
    assert failure[0]["value"]["status"] == "not_applicable"
    assert failure[0]["confidence"] is None


def _vision_payload() -> dict[str, object]:
    return {
        "schema_version": "gemini-vision-v2",
        "image_role": "diagram",
        "summary": "A candidate diagram.",
        "slide_reading_order": "unknown",
        "diagram_flow_direction": "unknown",
        "flow_present": None,
        "nodes": [],
        "edges": [],
        "observations": [],
    }


def test_selective_vision_filters_noise_and_caches_valid_json(tmp_path: Path) -> None:
    source = tmp_path / "vision.pptx"
    _feature_package(source)

    class FakeVision:
        name = "fake-vision"
        version = "1.0"

        def __init__(self) -> None:
            self.calls = 0

        def analyze(self, prompt: str, images: list[object], timeout: float) -> str:
            self.calls += 1
            assert images
            return json.dumps(_vision_payload())

    noise_adapter = FakeVision()
    noise_report = extract_pptx(source)
    skipped = run_selective_vision(
        noise_report,
        source,
        asset_ids=["asset-0002"],
        adapter=noise_adapter,
        run_ocr_stage=False,
        cache_dir=tmp_path / "vision-cache",
        retry_backoff=0,
    )
    assert noise_adapter.calls == 0
    assert skipped[0]["value"]["status"] == "not_requested"
    assert "noise_filtered" in skipped[0]["value"]["metadata"]["skipped_assets"]["asset-0002"]

    adapter = FakeVision()
    report = extract_pptx(source)
    native_text = report.canonical.objects[0]["text"]
    first = run_selective_vision(
        report,
        source,
        asset_ids=["asset-0002"],
        adapter=adapter,
        run_ocr_stage=False,
        include_noise=True,
        cache_dir=tmp_path / "vision-cache",
        retry_backoff=0,
    )
    second = run_selective_vision(
        report,
        source,
        asset_ids=["asset-0002"],
        adapter=adapter,
        run_ocr_stage=False,
        include_noise=True,
        cache_dir=tmp_path / "vision-cache",
        retry_backoff=0,
    )
    assert adapter.calls == 1
    assert first[0]["source"]["layer"] == "vision_model"
    assert first[0]["value"]["status"] == "partial"
    assert second[0]["source"]["cache_hit"] is True
    assert report.canonical.objects[0]["text"] == native_text


def test_selective_vision_sanitizes_model_statuses_and_audits_usage(tmp_path: Path) -> None:
    source = tmp_path / "vision-sanitized.pptx"
    _feature_package(source)

    class FakeVision:
        name = "fake-vision"
        version = "1.0"

        def __init__(self) -> None:
            self.calls = 0
            self.last_usage = None
            self.role = "diagram"

        def analyze(self, prompt: str, images: list[object], timeout: float) -> str:
            self.calls += 1
            self.last_usage = {"promptTokenCount": 12, "candidatesTokenCount": 4}
            payload = _vision_payload()
            payload["image_role"] = self.role
            payload["nodes"] = [
                {"id": "logo", "label": "Logo", "bbox": [0.0, 0.0, 0.05, 0.05], "status": "verified"},
                {"id": "node-a", "label": "A", "bbox": [0.2, 0.2, 0.2, 0.2], "status": "verified"},
                {"id": "node-b", "label": "B", "bbox": [0.6, 0.2, 0.2, 0.2], "status": "partial"},
            ]
            payload["edges"] = [
                {"source": "node-a", "target": "node-b", "label": "", "direction": "left_to_right", "status": "verified"},
                {"source": "logo", "target": "node-a", "label": "", "direction": "unknown", "status": "verified"},
            ]
            return json.dumps(payload)

    adapter = FakeVision()
    report = extract_pptx(source)
    first = run_selective_vision(
        report,
        source,
        asset_ids=["asset-0002"],
        adapter=adapter,
        run_ocr_stage=False,
        include_noise=True,
        retries=0,
        retry_backoff=0,
        cache_dir=tmp_path / "vision-sanitized-cache",
    )
    analysis = first[0]["value"]["analysis"]
    assert {node["id"] for node in analysis["nodes"]} == {"node-a", "node-b"}
    assert all(node["status"] != "verified" for node in analysis["nodes"])
    assert all(edge["status"] == "unverified" for edge in analysis["edges"])
    assert first[0]["value"]["metadata"]["usage"] == {"promptTokenCount": 12, "candidatesTokenCount": 4}
    assert first[0]["value"]["metadata"]["estimated_cost_usd"] == 0.0000136
    assert first[0]["value"]["metadata"]["attempts"] == 1
    assert first[0]["value"]["metadata"]["sanitization"]["removed_noise_nodes"] == 1
    assert first[0]["confidence"] <= 0.75

    second = run_selective_vision(
        report,
        source,
        asset_ids=["asset-0002"],
        adapter=adapter,
        run_ocr_stage=False,
        include_noise=True,
        retries=0,
        retry_backoff=0,
        cache_dir=tmp_path / "vision-sanitized-cache",
    )
    assert adapter.calls == 1
    assert second[0]["source"]["cache_hit"] is True
    assert second[0]["value"]["metadata"]["usage"] == {"promptTokenCount": 12, "candidatesTokenCount": 4}
    assert second[0]["value"]["metadata"]["sanitization"]["removed_noise_nodes"] == 1
    assert report.to_dict()["vision_evidence"][0]["status"] == "partial"

    logo_adapter = FakeVision()
    logo_report = extract_pptx(source)
    logo_adapter.role = "logo"
    logo_result = run_selective_vision(
        logo_report,
        source,
        asset_ids=["asset-0002"],
        adapter=logo_adapter,
        run_ocr_stage=False,
        include_noise=True,
        retries=0,
        retry_backoff=0,
        cache_dir=tmp_path / "logo-cache",
    )
    assert logo_result[0]["value"]["analysis"]["nodes"] == []


def test_selective_vision_rejects_non_strict_model_json_and_skip_is_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "vision-invalid.pptx"
    _feature_package(source)

    class InvalidVision:
        name = "invalid-vision"
        version = "1.0"

        def analyze(self, prompt: str, images: list[object], timeout: float) -> str:
            payload = _vision_payload()
            payload["extra"] = "reject me"
            return json.dumps(payload)

    report = extract_pptx(source)
    before = report.to_canonical_json()
    assert run_selective_vision(report, source, skip=True) == []
    assert report.to_canonical_json() == before
    records = run_selective_vision(
        report,
        source,
        asset_ids=["asset-0002"],
        adapter=InvalidVision(),
        run_ocr_stage=False,
        include_noise=True,
        retries=0,
        retry_backoff=0,
        cache_dir=tmp_path / "invalid-cache",
    )
    assert records[0]["value"]["status"] == "failed"
    assert "unexpected or missing" in records[0]["value"]["error"]


def test_load_dotenv_reads_local_values_without_overriding_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("# comment\nexport GEMINI_API_KEY=from-file\nGEMINI_MODEL=\"gemini-test\"\n", encoding="utf-8")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    assert load_dotenv(dotenv) == dotenv.resolve()
    assert os.environ["GEMINI_API_KEY"] == "from-file"
    assert os.environ["GEMINI_MODEL"] == "gemini-test"

    dotenv.write_text("GEMINI_API_KEY=should-not-win\n", encoding="utf-8")
    assert load_dotenv(dotenv) == dotenv.resolve()
    assert os.environ["GEMINI_API_KEY"] == "from-file"


def test_llvm_golden_report() -> None:
    source = Path(__file__).parents[1] / "Copy of LLVM (1).pptx"
    if not source.exists():
        pytest.skip("LLVM deck is supplied as a local benchmark artifact")
    golden = json.loads((Path(__file__).parent / "golden/llvm.deckir.golden.json").read_text())
    report = extract_pptx(source)
    canonical = report.to_dict()
    object_types = {}
    for item in canonical["objects"]:
        object_types[item["type"]] = object_types.get(item["type"], 0) + 1

    assert hashlib.sha256(report.to_canonical_json().encode()).hexdigest() == golden["canonical_sha256"]
    assert len(report.to_canonical_json().encode()) == golden["canonical_utf8_bytes"]
    assert canonical["deck"]["source_sha256"] == golden["source_sha256"]
    assert {
        "slides": len(canonical["slides"]),
        "objects": len(canonical["objects"]),
        "assets": len(canonical["assets"]),
        "relationships": len(canonical["relationships"]),
        "package_parts": len(canonical["provenance"]["package_parts"]),
    } == golden["counts"]
    assert object_types == golden["object_types"]
    assert [item["part"] for item in canonical["slides"]] == [f"ppt/slides/slide{number}.xml" for number in range(1, 18)]
