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
from typing import Any, Sequence

from defusedxml import ElementTree as SafeET

from .models import ExtractionReport
from .visual import rendered_geometry_evidence

RENDERER = "aurochs"
RUNNER = Path(__file__).resolve().parents[2] / "renderers" / "aurochs" / "runner.ts"


def parse_slide_range(value: str) -> list[int]:
    """Parse ``1,3-5`` into sorted, unique 1-based slide numbers."""
    slides: set[int] = set()
    for part in value.split(","):
        bounds = part.split("-", 1)
        try:
            start = int(bounds[0])
            end = int(bounds[1]) if len(bounds) == 2 else start
        except ValueError as exc:
            raise ValueError(f"Invalid slide range: {part}") from exc
        if start < 1 or end < start:
            raise ValueError(f"Invalid slide range: {part}")
        slides.update(range(start, end + 1))
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
    selected = sorted(set(slides))
    if not selected:
        return []

    output_root = Path(evidence_dir).expanduser().resolve()
    rendered_root = output_root / "rendered"
    rendered_root.mkdir(parents=True, exist_ok=True)
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
    bun_argv = shlex.split(bun)

    source_path = Path(source).expanduser().resolve()
    renderer_version = _renderer_version(root)
    cache_root = _cache_root(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
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
            for result in payload.get("slides", []):
                slide = int(result["slide"])
                runner_results[slide] = result
                for warning in result.get("warnings", []):
                    _warning(report, f"Aurochs slide {slide}: {warning}")
                if result.get("error"):
                    _warning(report, f"Aurochs slide {slide} failed: {result['error']}")
                    continue
                rendered_path = Path(result.get("path", ""))
                if not rendered_path.is_file():
                    _warning(report, f"Aurochs slide {slide} produced no SVG")
                    continue
                key = _cache_key(report.source_sha256, slide, renderer_version)
                cached = cache_root / f"{_cache_filename(key)}.svg"
                shutil.copyfile(rendered_path, cached)
                cached_paths[slide] = cached

    for slide in selected:
        cached = cached_paths.get(slide)
        if cached is None:
            _set_render_visibility(report, [slide], "failed")
            continue
        output_path = rendered_root / f"slide-{slide:02d}.svg"
        shutil.copyfile(cached, output_path)
        data = output_path.read_bytes()
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
