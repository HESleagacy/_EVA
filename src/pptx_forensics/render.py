"""Optional Aurochs SVG rendering with cache and evidence provenance."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
from typing import Any, Mapping, Sequence

from defusedxml import ElementTree as SafeET

from .models import ExtractionReport
from .visual import rendered_geometry_evidence

RENDERER = "aurochs"
RUNNER = Path(__file__).resolve().parents[2] / "renderers" / "aurochs" / "runner.ts"
MAX_SLIDE_RANGE = 10_000


def parse_slide_range(value: str) -> list[int]:
    """Parse ``1,3-5`` into sorted, unique 1-based slide numbers."""
    slides: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Invalid slide range: {part!r}")
        bounds = part.split("-", 1)
        try:
            start = int(bounds[0].strip())
            end = int(bounds[1].strip()) if len(bounds) == 2 else start
        except ValueError as exc:
            raise ValueError(f"Invalid slide range: {part}") from exc
        if start < 1 or end < start:
            raise ValueError(f"Invalid slide range: {part}")
        if end - start + 1 > MAX_SLIDE_RANGE:
            raise ValueError(f"Slide range is too large: {part}")
        slides.update(range(start, end + 1))
        if len(slides) > MAX_SLIDE_RANGE:
            raise ValueError(f"Slide range is too large: {part}")
    return sorted(slides)


def _renderer_version(renderer_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(renderer_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _cache_root(cache_dir: str | Path | None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).expanduser().resolve()
    return Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "pptx-forensics" / "render"


def _cache_key(source_hash: str, slide: int, renderer_version: str) -> str:
    return f"{source_hash}+{slide}+{RENDERER}+{renderer_version}"


def _cache_filename(cache_key: str) -> str:
    return hashlib.sha256(cache_key.encode("utf-8")).hexdigest()


def _warning(report: ExtractionReport, message: str) -> None:
    report.warnings.append(message)


def _set_render_visibility(report: ExtractionReport, slides: Sequence[int], status: str) -> None:
    if report.canonical is None:
        return
    wanted = {f"slide-{slide:02d}" for slide in slides}
    for slide in report.canonical.slides:
        if slide.get("id") in wanted:
            slide.setdefault("visual_evidence_visibility", {})["rendered"] = status


def _svg_visual_features(data: bytes) -> dict[str, Any]:
    """Extract lightweight, deterministic features from rendered SVG evidence."""
    try:
        root = SafeET.fromstring(data)
    except Exception as exc:
        return {"status": "invalid_svg", "error": str(exc)}
    counts: dict[str, int] = {}
    object_ids: set[str] = set()
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        counts[name] = counts.get(name, 0) + 1
        if element.get("data-ooxml-id"):
            object_ids.add(element.get("data-ooxml-id"))
    view_box = root.get("viewBox", "").split()
    return {
        "status": "ok",
        "schema": "svg-features-v1",
        "width": root.get("width"),
        "height": root.get("height"),
        "view_box": view_box,
        "element_counts": {key: counts[key] for key in sorted(counts)},
        "object_ids": sorted(object_ids),
        "text_nodes": counts.get("text", 0),
        "path_nodes": counts.get("path", 0),
        "image_nodes": counts.get("image", 0),
    }


def render_selected_slides(
    report: ExtractionReport,
    source: str | Path,
    evidence_dir: str | Path,
    slides: Sequence[int],
    *,
    renderer_root: str | Path | None = None,
    cache_dir: str | Path | None = None,
    bun_command: str | None = None,
) -> list[dict[str, Any]]:
    """Render only selected slides and append hash/provenance evidence.

    Renderer failures are warnings on the existing report. Native extraction
    has already completed before this function is called.
    """
    if report.canonical is None:
        raise ValueError("Rendering requires a canonical DeckIR report")
    if isinstance(report.convenience, Mapping) and report.convenience.get("adapter") == "pdf":
        return render_selected_pdf_pages(
            report,
            source,
            evidence_dir,
            slides,
            cache_dir=cache_dir,
        )
    available = {
        item.get("number")
        for item in report.canonical.slides
        if isinstance(item.get("number"), int) and not isinstance(item.get("number"), bool)
    }
    selected: list[int] = []
    for slide in slides:
        if isinstance(slide, bool) or not isinstance(slide, int) or slide not in available:
            _warning(report, f"Aurochs rendering skipped unknown slide selection: {slide!r}")
            continue
        selected.append(slide)
    selected = sorted(set(selected))
    if not selected:
        return []

    output_root = Path(evidence_dir).expanduser().resolve()
    rendered_root = output_root / "rendered"
    try:
        rendered_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warning(report, f"Aurochs rendering output directory is unavailable: {exc}")
        _set_render_visibility(report, selected, "failed")
        return []
    configured_root = renderer_root or os.environ.get("AUROCHS_ROOT")
    if not configured_root:
        _warning(report, "Aurochs rendering skipped: AUROCHS_ROOT is not configured")
        _set_render_visibility(report, selected, "failed")
        return []
    root = Path(configured_root).expanduser().resolve()
    if not root.is_dir():
        _warning(report, f"Aurochs rendering skipped: renderer root does not exist: {root}")
        _set_render_visibility(report, selected, "failed")
        return []
    if not RUNNER.is_file():
        _warning(report, f"Aurochs rendering skipped: runner is missing: {RUNNER}")
        _set_render_visibility(report, selected, "failed")
        return []
    bun = bun_command or os.environ.get("AUROCHS_BUN_COMMAND") or shutil.which("bun")
    if not bun:
        _warning(report, "Aurochs rendering skipped: Bun is not installed")
        _set_render_visibility(report, selected, "failed")
        return []
    try:
        bun_argv = shlex.split(bun)
    except ValueError as exc:
        _warning(report, f"Aurochs rendering skipped: invalid Bun command: {exc}")
        _set_render_visibility(report, selected, "failed")
        return []
    if not bun_argv:
        _warning(report, "Aurochs rendering skipped: Bun command is empty")
        _set_render_visibility(report, selected, "failed")
        return []

    source_path = Path(source).expanduser().resolve()
    renderer_version = _renderer_version(root)
    cache_root = _cache_root(cache_dir)
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warning(report, f"Aurochs render cache is unavailable: {exc}")
        _set_render_visibility(report, selected, "failed")
        return []
    evidence: list[dict[str, Any]] = []
    uncached = []
    cached_paths: dict[int, Path] = {}
    for slide in selected:
        key = _cache_key(report.source_sha256, slide, renderer_version)
        cached = cache_root / f"{_cache_filename(key)}.svg"
        if cached.is_file():
            cached_paths[slide] = cached
        else:
            uncached.append(slide)

    runner_results: dict[int, dict[str, Any]] = {}
    if uncached:
        with tempfile.TemporaryDirectory(prefix="pptx-forensics-aurochs-") as temporary:
            command = [
                *bun_argv,
                str(RUNNER),
                "--source",
                str(source_path),
                "--slides",
                ",".join(str(slide) for slide in uncached),
                "--output",
                temporary,
                "--renderer-root",
                str(root),
            ]
            try:
                completed = subprocess.run(command, capture_output=True, text=True, check=False)
            except OSError as exc:
                _warning(report, f"Aurochs rendering failed to start: {exc}")
                _set_render_visibility(report, selected, "failed")
                return []
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or "unknown renderer error"
                _warning(report, f"Aurochs rendering failed: {detail}")
                _set_render_visibility(report, selected, "failed")
                return []
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                _warning(report, f"Aurochs rendering returned invalid JSON: {exc}")
                _set_render_visibility(report, selected, "failed")
                return []
            if not isinstance(payload, Mapping) or not isinstance(payload.get("slides"), list):
                _warning(report, "Aurochs rendering returned an invalid result payload")
                _set_render_visibility(report, selected, "failed")
                return []
            temporary_root = Path(temporary).resolve()
            for result in payload["slides"]:
                if not isinstance(result, Mapping):
                    _warning(report, "Aurochs rendering returned an invalid slide result")
                    continue
                try:
                    slide = int(result["slide"])
                except (KeyError, TypeError, ValueError):
                    _warning(report, "Aurochs rendering returned a slide without a valid number")
                    continue
                if slide not in uncached:
                    _warning(report, f"Aurochs rendering returned an unexpected slide: {slide}")
                    continue
                runner_results[slide] = result
                for warning in result.get("warnings", []) if isinstance(result.get("warnings", []), list) else []:
                    _warning(report, f"Aurochs slide {slide}: {warning}")
                if result.get("error"):
                    _warning(report, f"Aurochs slide {slide} failed: {result['error']}")
                    continue
                raw_path = result.get("path")
                if not isinstance(raw_path, str) or not raw_path:
                    _warning(report, f"Aurochs slide {slide} produced no SVG")
                    continue
                rendered_path = Path(raw_path)
                try:
                    rendered_path = rendered_path.expanduser().resolve()
                except OSError:
                    rendered_path = Path()
                if not rendered_path.is_file() or not rendered_path.is_relative_to(temporary_root):
                    _warning(report, f"Aurochs slide {slide} produced no SVG")
                    continue
                key = _cache_key(report.source_sha256, slide, renderer_version)
                cached = cache_root / f"{_cache_filename(key)}.svg"
                try:
                    shutil.copyfile(rendered_path, cached)
                except OSError as exc:
                    _warning(report, f"Aurochs slide {slide} could not be cached: {exc}")
                    continue
                cached_paths[slide] = cached

    for slide in selected:
        cached = cached_paths.get(slide)
        if cached is None:
            _set_render_visibility(report, [slide], "failed")
            continue
        output_path = rendered_root / f"slide-{slide:02d}.svg"
        try:
            shutil.copyfile(cached, output_path)
            data = output_path.read_bytes()
        except OSError as exc:
            _warning(report, f"Aurochs slide {slide} could not be copied: {exc}")
            _set_render_visibility(report, [slide], "failed")
            continue
        key = _cache_key(report.source_sha256, slide, renderer_version)
        visual_features = _svg_visual_features(data)
        if visual_features.get("status") != "ok":
            _warning(report, f"Aurochs slide {slide} produced invalid SVG evidence")
        render_status = "verified" if visual_features.get("status") == "ok" else "failed"
        record = {
            "id": f"rendered-slide-{slide:02d}",
            "slide_id": f"slide-{slide:02d}",
            "object_id": None,
            "bbox": [0.0, 0.0, 1.0, 1.0],
            "value": {
                "type": "rendered_slide",
                "format": "svg",
                "path": str(output_path.relative_to(output_root)),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "cache_key": key,
                "visual_features": visual_features,
                "status": render_status,
            },
            "status": render_status,
            "confidence": 1.0 if render_status == "verified" else None,
            "evidence_refs": [{"id": f"slide-{slide:02d}", "kind": "native_slide"}],
            "source": {
                "layer": "rendered_cv",
                "renderer": RENDERER,
                "renderer_version": renderer_version,
                "runner": str(RUNNER.relative_to(RUNNER.parents[2])),
                "cache_hit": slide in cached_paths and slide not in runner_results,
                "status": render_status,
            },
        }
        existing = next((item for item in report.canonical.rendered_evidence if item.get("id") == record["id"]), None)
        if existing is None:
            report.canonical.add_evidence("rendered_evidence", record)
            existing = record
        evidence.append(existing)
        slide_record = next((item for item in report.canonical.slides if item.get("id") == f"slide-{slide:02d}"), None)
        if slide_record is not None:
            visibility = slide_record.setdefault("visual_evidence_visibility", {})
            visibility["rendered"] = render_status
        evidence.extend(rendered_geometry_evidence(report.canonical, slide, data))
    return evidence


def _pdf_renderer_version(executable: str) -> str:
    try:
        result = subprocess.run([executable, "-v"], capture_output=True, text=True, check=False)
    except OSError:
        return "unknown"
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0].strip() if output else "unknown"


def _pdf_render_cache_key(source_hash: str, page: int, renderer_version: str, dpi: int) -> str:
    return f"{source_hash}+{page}+pdftocairo+{renderer_version}+{dpi}"


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def render_selected_pdf_pages(
    report: ExtractionReport,
    source: str | Path,
    evidence_dir: str | Path,
    pages: Sequence[int],
    *,
    cache_dir: str | Path | None = None,
    executable: str | None = None,
    dpi: int = 144,
) -> list[dict[str, Any]]:
    """Render selected PDF pages with local Poppler without executing PDF actions."""
    if report.canonical is None:
        raise ValueError("PDF rendering requires a canonical DeckIR report")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi < 1 or dpi > 600:
        raise ValueError("PDF render DPI must be an integer between 1 and 600")
    available = {
        item.get("number")
        for item in report.canonical.slides
        if isinstance(item.get("number"), int) and not isinstance(item.get("number"), bool)
    }
    selected: list[int] = []
    for page in pages:
        if isinstance(page, bool) or not isinstance(page, int) or page not in available:
            _warning(report, f"PDF rendering skipped unknown page selection: {page!r}")
            continue
        selected.append(page)
    selected = sorted(set(selected))
    if not selected:
        return []

    output_root = Path(evidence_dir).expanduser().resolve()
    rendered_root = output_root / "rendered"
    try:
        rendered_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warning(report, f"PDF rendering output directory is unavailable: {exc}")
        _set_render_visibility(report, selected, "failed")
        return []
    renderer = executable or shutil.which("pdftocairo")
    if not renderer:
        _warning(report, "PDF rendering skipped: pdftocairo is not installed")
        _set_render_visibility(report, selected, "failed")
        return []
    renderer_version = _pdf_renderer_version(renderer)
    cache_root = _cache_root(cache_dir)
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warning(report, f"PDF render cache is unavailable: {exc}")
        _set_render_visibility(report, selected, "failed")
        return []

    source_path = Path(source).expanduser().resolve()
    evidence: list[dict[str, Any]] = []
    for page in selected:
        cache_key = _pdf_render_cache_key(report.source_sha256, page, renderer_version, dpi)
        cached = cache_root / f"{hashlib.sha256(cache_key.encode('utf-8')).hexdigest()}.png"
        cache_hit = cached.is_file()
        if cache_hit:
            try:
                cache_hit = _png_dimensions(cached.read_bytes()) is not None
            except OSError:
                cache_hit = False
        if not cache_hit:
            with tempfile.TemporaryDirectory(prefix="pptx-forensics-pdf-") as temporary:
                prefix = Path(temporary) / "page"
                command = [
                    renderer,
                    "-singlefile",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-png",
                    "-r",
                    str(dpi),
                    str(source_path),
                    str(prefix),
                ]
                try:
                    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30.0)
                except subprocess.TimeoutExpired:
                    _warning(report, f"PDF page {page} rendering timed out after 30s")
                    _set_render_visibility(report, [page], "failed")
                    continue
                except OSError as exc:
                    _warning(report, f"PDF page {page} rendering failed to start: {exc}")
                    _set_render_visibility(report, [page], "failed")
                    continue
                if completed.returncode != 0:
                    detail = completed.stderr.strip() or completed.stdout.strip() or "unknown renderer error"
                    _warning(report, f"PDF page {page} rendering failed: {detail}")
                    _set_render_visibility(report, [page], "failed")
                    continue
                generated = Path(f"{prefix}.png")
                if not generated.is_file():
                    _warning(report, f"PDF page {page} renderer produced no PNG")
                    _set_render_visibility(report, [page], "failed")
                    continue
                try:
                    shutil.copyfile(generated, cached)
                except OSError as exc:
                    _warning(report, f"PDF page {page} could not be cached: {exc}")
                    _set_render_visibility(report, [page], "failed")
                    continue
        output_path = rendered_root / f"slide-{page:02d}.png"
        try:
            shutil.copyfile(cached, output_path)
            data = output_path.read_bytes()
        except OSError as exc:
            _warning(report, f"PDF page {page} could not be copied: {exc}")
            _set_render_visibility(report, [page], "failed")
            continue
        dimensions = _png_dimensions(data)
        status = "verified" if dimensions and dimensions[0] > 0 and dimensions[1] > 0 else "failed"
        if status == "failed":
            _warning(report, f"PDF page {page} produced invalid PNG evidence")
        record = {
            "id": f"rendered-slide-{page:02d}",
            "slide_id": f"slide-{page:02d}",
            "object_id": None,
            "bbox": [0.0, 0.0, 1.0, 1.0],
            "value": {
                "type": "rendered_slide",
                "format": "png",
                "path": str(output_path.relative_to(output_root)),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "cache_key": cache_key,
                "image_size": list(dimensions) if dimensions else None,
                "dpi": dpi,
                "status": status,
            },
            "status": status,
            "confidence": 1.0 if status == "verified" else None,
            "evidence_refs": [{"id": f"slide-{page:02d}", "kind": "native_slide"}],
            "source": {
                "layer": "rendered_cv",
                "renderer": "pdftocairo",
                "renderer_version": renderer_version,
                "cache_hit": cache_hit,
                "status": status,
            },
        }
        existing_index = next(
            (index for index, item in enumerate(report.canonical.rendered_evidence) if item.get("id") == record["id"]),
            None,
        )
        if existing_index is None:
            report.canonical.add_evidence("rendered_evidence", record)
        else:
            report.canonical.rendered_evidence[existing_index] = record
        _set_render_visibility(report, [page], status)
        evidence.append(record)
    return evidence
