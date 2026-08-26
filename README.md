# PPTX Forensics

Forensic extraction and evidence generation for PowerPoint `.pptx` files.

`pptx-forensics` reads a presentation as an OOXML package first and a slide
deck second. It preserves the original package, extracts native structure and
provenance, and adds optional OCR, diagram, rendering, and vision evidence
without allowing derived results to overwrite native facts.

## What It Does

- Extracts package parts, hashes, relationships, slide inheritance, media,
  notes, comments, hyperlinks, animations, alt text, and embedded parts.
- Extracts native objects with stable IDs, parent relationships, normalized
  geometry, z-order, text, styles, semantic projections, and XML provenance.
- Produces deterministic visual evidence for occupancy, whitespace, margins,
  overlap, alignment, spacing, clipping, density, font/color patterns,
  rotations, hierarchy, and image roles.
- Runs OCR on selected image assets only, with normalized word/line boxes,
  confidence, caching, and failure metadata.
- Reconstructs native and raster diagram candidates with explicit uncertainty,
  endpoint checks, OCR masking, and failure classes.
- Supports opt-in Gemini vision with strict JSON validation, conservative
  sanitization, evidence grounding, retries, caching, cost, and latency
  metadata.
- Evaluates OCR, diagram, and vision results against labeled annotations.

## Design Principles

- Native OOXML is authoritative.
- Derived evidence is stored separately from native objects.
- Every derived record includes status, confidence, source, and evidence
  references.
- `verified`, `partial`, `unverified`, `failed`, `not_requested`, and
  `not_applicable` remain distinct states.
- No unverified diagram edge is counted as a verified result.
- Optional stages are explicit and never required for native extraction.
- Canonical output is deterministic and suitable for hashing and golden tests.

## Installation

The package requires Python 3.10 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[pptx]"
```

Install development and optional OCR dependencies when needed:

```bash
python -m pip install -e ".[pptx,test,ocr]"
```

The OCR extra installs Pillow. Tesseract must also be installed separately if
the default Tesseract adapter is used.

## Quick Start

Print a canonical report to stdout and retain the source archive and package
parts as evidence:

```bash
pptx-forensics input.pptx \
  --evidence-dir evidence/input \
  > report.json
```

Without `--evidence-dir`, extraction is read-only and only the JSON report is
printed. The evidence directory contains the original archive, package parts
under `parts/`, and `manifest.json`.

For library use:

```python
from pptx_forensics import extract_pptx

report = extract_pptx("input.pptx", evidence_dir="evidence/input")
payload = report.to_dict()
print(report.to_canonical_json())
```

Use `--native-only` when only package, slide, object, asset, and relationship
facts are required.

## Command Line Usage

Run `pptx-forensics --help` for all options. Common workflows are below.

### Rendering

Rendering is optional and only runs for explicitly selected slides. The Aurochs
renderer is kept outside the Python package:

```bash
./scripts/fetch_aurochs_renderer.sh

AUROCHS_ROOT=vendor/aurochs \
  pptx-forensics input.pptx \
  --evidence-dir evidence/input \
  --render-slides 1,3-5
```

Native boxes remain authoritative. Rendered SVG data is used only for
native-versus-rendered geometry checks. Missing renderers or slide failures are
recorded as warnings.

### OCR

OCR is asset-scoped. Native text is preferred, and image assets containing
native text are not sent to OCR.

```bash
pptx-forensics input.pptx \
  --evidence-dir evidence/input \
  --ocr-slides 3,5-10 \
  --ocr-cache-dir .cache/pptx-ocr
```

Use `--ocr-assets asset-0016,asset-0029` for explicit asset selection. Use
`--skip-ocr` to leave the native report unchanged.

### Diagram Reconstruction

Native diagrams use OOXML shapes, connectors, endpoints, arrowheads, and group
hierarchy. Raster diagrams use the original image asset, optional OCR, and
deterministic line, contour, box, and arrow heuristics.

```bash
pptx-forensics input.pptx \
  --diagram-slides 8-10 \
  --diagram-ocr-cache-dir .cache/pptx-ocr
```

Raster detections are candidates, not proof. Missing OCR, unresolved endpoints,
or zero verified edges keep the graph uncertain. Failure classes include
`ocr_failure`, `text_mask_failure`, `line_detection_failure`,
`arrowhead_failure`, `endpoint_matching_failure`, and `graph_assembly_failure`.

### Gemini Vision

Gemini is opt-in. Set `GEMINI_API_KEY` in the environment or a local `.env`
file before selecting slides or assets:

```bash
export GEMINI_API_KEY="..."

pptx-forensics input.pptx \
  --evidence-dir evidence/input \
  --vision-slides 8-10 \
  --vision-cache-dir .cache/pptx-vision \
  --vision-render-dir evidence/input
```

Requests are limited to selected logical slide or asset targets. Likely logos,
decorative images, badges, and template images are filtered unless
`--vision-include-noise` is supplied. Without an API key, the optional stage
records an unavailable result and makes no network request.

Vision responses use strict `gemini-vision-v3` JSON. Model-only verified claims
are downgraded, model-only edges remain `unverified`, and invalid JSON/schema
responses are retried within the configured retry budget. Cache records retain
the prompt/model/image hash, usage, estimated cost, duration, attempts, and
sanitization metadata.

## Deterministic Visual Evidence

Visual evidence excludes likely slide chrome from content-area calculations
while retaining the original native objects. Exclusion reasons can include
background, master, footer, page number, repeated footer, badge, and template
noise.

Available signals include:

- Largest empty regions and whitespace balance.
- Candidate slide titles with placeholder, position, font, and character-count
  selection evidence.
- Font distributions and consistency weighted by visible character count.
- Alignment and spacing peer groups.
- Native connector count and nullable `flow_candidate` semantics.
- Image-role candidates based on deterministic native metadata and geometry.

The six content roles are `diagram`, `screenshot`, `chart`, `evidence_image`,
`logo`, and `decorative_image`. `template` and `unknown` are retained as
conservative fallback classifications for gating and uncertainty.

## Evaluation

`pptx_forensics.evaluation` provides reproducible metrics against annotations:

- OCR character error rate, word precision/recall/F1, matched-word bounding-box
  IoU, Brier score, and expected calibration error.
- Diagram node/edge precision/recall/F1, endpoint accuracy, direction accuracy,
  and graph connectivity.
- Vision target completeness, role accuracy, reading order, flow direction,
  diagram metrics, evidence grounding, hallucination indicators, cache hits,
  estimated cost, and latency.

Evaluation is written separately from canonical DeckIR:

```bash
pptx-forensics input.pptx \
  --diagram-slides 8-10 \
  --evaluate annotations.json \
  --evaluation-output evidence/input/evaluation.json
```

When `--evidence-dir` is supplied without `--evaluation-output`, the sidecar is
written to `evidence-dir/evaluation.json`. If neither path is supplied, metrics
are printed to stderr so the canonical report remains valid JSON on stdout.

Annotation files are JSON objects with optional `ocr`, `diagrams`, and `vision`
sections:

```json
{
  "ocr": {
    "asset-0001": {
      "text": "Alpha Beta",
      "words": [
        {"text": "Alpha", "bbox": [0.1, 0.1, 0.2, 0.1]}
      ]
    }
  },
  "diagrams": {
    "slide-08:asset-0002": {
      "nodes": [
        {"id": "n1", "label": "Start", "bbox": [0.1, 0.4, 0.1, 0.1]}
      ],
      "edges": []
    }
  },
  "vision": {
    "requested_targets": ["slide-08"],
    "targets": [
      {"target": "slide-08", "image_role": "diagram"}
    ]
  }
}
```

The same report can be evaluated from Python:

```python
from pptx_forensics import evaluate_report

metrics = evaluate_report(report, labels)
```

## DeckIR Output

`ExtractionReport.to_dict()` and `manifest.json` use the DeckIR v1 contract:

```json
{
  "schema_version": "1.0",
  "deck": {},
  "slides": [],
  "objects": [],
  "assets": [],
  "relationships": [],
  "visual_regions": [],
  "rendered_evidence": [],
  "ocr_evidence": [],
  "vision_evidence": [],
  "warnings": [],
  "provenance": {}
}
```

Native objects use the `native_ooxml` source layer. Derived records are kept in
`visual_regions`, `rendered_evidence`, `ocr_evidence`, and `vision_evidence`.
They contain source, confidence, status, and evidence references. The schema
validator rejects unsupported versions, missing required fields, invalid
statuses, and derived records without provenance.

Slides expose `slide_reading_order`, `diagram_flow_direction`, `flow_present`,
and `flow_presence_basis` separately. `flow_present` is `true` only when flow
evidence exists, `false` only with a supported absence basis, and `null` when
absence cannot be established.

## Security And Privacy

- XML parsing uses `defusedxml`.
- ZIP path traversal and malformed package inputs are validated.
- Embedded parts are retained as evidence and never executed.
- Gemini is disabled unless explicitly selected and configured with an API key.
- API keys belong in the environment or an ignored local `.env` file, never in
  source control.

## Development

Run the regression suite from the repository root:

```bash
python -m compileall -q src tests
pytest -q
```

The suite includes a synthetic package, adversarial XML/ZIP cases, deterministic
visual evidence tests, OCR caching tests, diagram uncertainty tests, Gemini
schema/retry tests, evaluation metrics, and an LLVM golden report.

The current suite passes 38 tests. The LLVM fixture contains 17 slides, 634
native objects, 37 assets, and 118 relationships.

## Repository Layout

```text
src/pptx_forensics/
  extractor.py       Native OOXML extraction
  models.py          DeckIR contract and validation
  visual.py          Deterministic visual evidence
  ocr.py             Asset-scoped OCR
  diagrams.py        Native and raster diagram evidence
  vision.py          Optional Gemini vision
  evaluation.py      Annotation-based metrics
  render.py          Optional Aurochs rendering
  cli.py             Command-line interface
tests/               Regression and benchmark tests
renderers/           Renderer runner integrations
scripts/             Optional renderer bootstrap scripts
```

Native extraction is production-oriented. Raster semantic recovery and Gemini
interpretation remain probabilistic and should be evaluated against labeled
data before being treated as production-grade quality judgments.

## License

Licensed under the GNU General Public License, version 3. See `LICENSE`.
