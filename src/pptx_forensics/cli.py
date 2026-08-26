"""Command-line interface for PPTX forensic extraction."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from .extractor import ExtractionError, extract_pptx
from .config import load_dotenv
from .diagrams import reconstruct_raster_diagrams
from .ocr import run_ocr
from .render import parse_slide_range, render_selected_slides
from .vision import run_selective_vision


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Extract a PPTX as an OOXML forensic package")
    parser.add_argument("source", type=Path)
    parser.add_argument("--evidence-dir", type=Path, help="directory in which to retain original and package parts")
    parser.add_argument("--render-slides", help="render selected slides with Aurochs, e.g. 1,3-5")
    parser.add_argument("--aurochs-root", type=Path, help="sparse Aurochs checkout (or use AUROCHS_ROOT)")
    parser.add_argument("--render-cache-dir", type=Path, help="render cache directory")
    parser.add_argument("--ocr-slides", help="OCR image assets displayed on selected slides, e.g. 8-10")
    parser.add_argument("--ocr-assets", help="OCR selected asset IDs, e.g. asset-0016,asset-0029")
    parser.add_argument("--ocr-cache-dir", type=Path, help="OCR cache directory")
    parser.add_argument("--skip-ocr", action="store_true", help="explicitly skip OCR")
    parser.add_argument("--native-only", action="store_true", help="omit derived visual and native diagram evidence")
    parser.add_argument("--diagram-slides", help="reconstruct raster diagrams on selected slides, e.g. 8-10")
    parser.add_argument("--diagram-assets", help="reconstruct selected raster diagram assets, e.g. asset-0029")
    parser.add_argument("--diagram-ocr-cache-dir", type=Path, help="OCR cache directory for raster diagrams")
    parser.add_argument("--vision-slides", help="select slides for optional Gemini vision, e.g. 8-10")
    parser.add_argument("--vision-assets", help="select image assets for optional Gemini vision, e.g. asset-0029")
    parser.add_argument("--vision-cache-dir", type=Path, help="content-hash cache directory for Gemini vision")
    parser.add_argument("--vision-ocr-cache-dir", type=Path, help="OCR cache directory used before Gemini selection")
    parser.add_argument("--vision-render-dir", type=Path, help="directory containing selected rendered slide images")
    parser.add_argument("--vision-model", default=None, help="Gemini model name (or GEMINI_MODEL)")
    parser.add_argument("--vision-timeout", type=float, default=30.0, help="Gemini request timeout in seconds")
    parser.add_argument("--vision-retries", type=int, default=2, help="Gemini retries per selected request")
    parser.add_argument("--vision-thinking-budget", type=int, default=1024, help="Gemini thinking token budget")
    parser.add_argument("--vision-max-output-tokens", type=int, default=8192, help="Gemini output token limit")
    parser.add_argument("--vision-concurrency", type=int, default=2, help="maximum concurrent Gemini requests")
    parser.add_argument("--vision-include-noise", action="store_true", help="allow likely logos and template images through the vision gate")
    parser.add_argument("--skip-vision", action="store_true", help="do not run the optional Gemini vision stage")
    args = parser.parse_args(argv)
    if args.render_slides and args.evidence_dir is None:
        parser.error("--evidence-dir is required when --render-slides is used")
    try:
        report = extract_pptx(
            args.source,
            args.evidence_dir,
            include_visual_evidence=not args.native_only,
            include_native_diagrams=not args.native_only,
        )
        enriched = False
        if args.render_slides:
            try:
                slides = parse_slide_range(args.render_slides)
            except ValueError as exc:
                parser.error(str(exc))
            render_selected_slides(
                report,
                args.source,
                args.evidence_dir,
                slides,
                renderer_root=args.aurochs_root,
                cache_dir=args.render_cache_dir,
            )
            enriched = True
        if (args.ocr_slides or args.ocr_assets) and not args.skip_ocr:
            try:
                ocr_slides = parse_slide_range(args.ocr_slides) if args.ocr_slides else None
            except ValueError as exc:
                parser.error(str(exc))
            ocr_assets = [item for item in args.ocr_assets.split(",") if item] if args.ocr_assets else None
            run_ocr(
                report,
                args.source,
                slides=ocr_slides,
                asset_ids=ocr_assets,
                cache_dir=args.ocr_cache_dir,
            )
            enriched = True
        if args.diagram_slides or args.diagram_assets:
            try:
                diagram_slides = parse_slide_range(args.diagram_slides) if args.diagram_slides else None
            except ValueError as exc:
                parser.error(str(exc))
            diagram_assets = [item for item in args.diagram_assets.split(",") if item] if args.diagram_assets else None
            reconstruct_raster_diagrams(
                report,
                args.source,
                slides=diagram_slides,
                asset_ids=diagram_assets,
                ocr_cache_dir=args.diagram_ocr_cache_dir or args.ocr_cache_dir,
                run_ocr_stage=not args.skip_ocr,
                skip_ocr=args.skip_ocr,
            )
            enriched = True
        if (args.vision_slides or args.vision_assets) and not args.skip_vision:
            try:
                vision_slides = parse_slide_range(args.vision_slides) if args.vision_slides else None
            except ValueError as exc:
                parser.error(str(exc))
            vision_assets = [item for item in args.vision_assets.split(",") if item] if args.vision_assets else None
            run_selective_vision(
                report,
                args.source,
                slides=vision_slides,
                asset_ids=vision_assets,
                model=args.vision_model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                cache_dir=args.vision_cache_dir,
                ocr_cache_dir=args.vision_ocr_cache_dir or args.ocr_cache_dir or args.diagram_ocr_cache_dir,
                rendered_dir=args.vision_render_dir or args.evidence_dir,
                run_ocr_stage=not args.skip_ocr,
                skip_ocr=args.skip_ocr,
                include_noise=args.vision_include_noise,
                timeout=args.vision_timeout,
                retries=args.vision_retries,
                thinking_budget=args.vision_thinking_budget,
                max_output_tokens=args.vision_max_output_tokens,
                max_concurrency=args.vision_concurrency,
            )
            enriched = True
        if enriched and args.evidence_dir is not None:
            evidence_dir = Path(args.evidence_dir).expanduser().resolve()
            (evidence_dir / "manifest.json").write_text(report.to_json() + "\n", encoding="utf-8")
    except ExtractionError as exc:
        parser.error(str(exc))
    print(report.to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
