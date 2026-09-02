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

Write one descriptive Markdown report:

```bash
pptx-forensics input.pptx --output report.md
```

Use `--evidence-dir` when the original archive and package parts are also
needed; the report remains the single Markdown output:

```bash
pptx-forensics input.pptx \
  --evidence-dir evidence/input \
  --output report.md
```

Without `--output`, the Markdown report is printed to stdout. The evidence
directory contains only the original archive and package parts under `parts/`.

For library use:

```python
from pptx_forensics import extract_pptx

report = extract_pptx("input.pptx", evidence_dir="evidence/input")
payload = report.to_semantic_dict()
evaluator_input = report.to_evaluator_dict()
print(report.to_markdown())
```

`to_semantic_dict()` and `to_semantic_json()` contain document semantics only.
The full working DeckIR is available explicitly through `to_debug_dict()` and
is used by the optional evaluation code; it is not written to the Markdown
report.

Use `--native-only` when only package, slide, object, asset, and relationship
facts are required.

## Full Run

The complete workflow has four stages: native extraction, optional evidence
generation, DeckIR export, and rubric evaluation. Native extraction is always
local and deterministic. OCR, raster diagram analysis, rendering, and Gemini
are separate stages with their own dependencies, caches, and failure states.

### Prerequisites

Install the package with the extras used by a full local run:

```bash
python -m pip install -e ".[pptx,test,ocr]"
```

The OCR stage also needs Tesseract. Rendering needs Bun and an Aurochs checkout:

```bash
sudo apt install tesseract-ocr
./scripts/fetch_aurochs_renderer.sh
```

If rendering is not installed, omit the render options below. The extraction
and evaluation stages still complete and record rendering as unavailable.

### Extract Evidence

Set the input and selected slide range once. Selecting all slides and all image
assets is the fullest run, but OCR and raster analysis can be expensive for
large or high-resolution decks. Use `--ocr-assets` and `--diagram-assets` to
bound those stages when needed.

```bash
SOURCE="input.pptx"
SLIDES="1-20"
EVIDENCE="evidence/input"

pptx-forensics "$SOURCE" \
  --evidence-dir "$EVIDENCE" \
  --output "$EVIDENCE/report.md" \
  --deck-ir-output "$EVIDENCE/deck-ir.json" \
  --render-slides "$SLIDES" \
  --aurochs-root "${AUROCHS_ROOT:-vendor/aurochs}" \
  --render-cache-dir .cache/pptx-render \
  --ocr-slides "$SLIDES" \
  --ocr-cache-dir .cache/pptx-ocr \
  --diagram-slides "$SLIDES" \
  --diagram-ocr-cache-dir .cache/pptx-diagrams \
  --skip-vision
```

This command produces the Markdown report, package evidence under `parts/`,
rendered SVGs under `rendered/` when Aurochs is available, OCR records, native
diagram evidence, and raster diagram candidates. Raster graph topology remains
probabilistic evidence and is not included in deterministic rubric scoring.

### Export DeckIR

The `--deck-ir-output` option in the full command writes canonical DeckIR JSON
after all selected evidence stages have completed. This preserves render, OCR,
and diagram evidence in the same object that produced the Markdown report.

The equivalent library export is:

```bash
python - <<'PY'
import json
from pathlib import Path
from pptx_forensics import extract_pptx

source = Path("input.pptx")
evidence = Path("evidence/input")
report = extract_pptx(source, evidence_dir=evidence)
(evidence / "deck-ir.json").write_text(
    json.dumps(report.to_dict(), indent=2), encoding="utf-8"
)
PY
```

When rendered or OCR evidence must be part of the DeckIR, invoke those library
stages on the same `report` object before writing `report.to_dict()`. The CLI
above is convenient for the Markdown report; the library object is the
authoritative export boundary.

### Evaluate Deck

Run deterministic deck-quality scoring without external network calls:

```bash
evaluate-deck \
  --deck-ir evidence/input/deck-ir.json \
  --problem problem.json \
  --skip-semantic \
  --output evidence/input/evaluation.json
```

For a structural-only run when no problem statement exists:

```bash
evaluate-deck \
  --deck-ir evidence/input/deck-ir.json \
  --deck-only \
  --output evidence/input/evaluation.json
```

The result contains all deterministic metrics and score records. Semantic
component scores remain explicitly unavailable when `--skip-semantic`,
`--deck-only`, or a missing Gemini key prevents model evaluation. Consequently,
`final_score` is `null` unless both the `proposal_strength` and `deck_quality`
groups have scores.

### Semantic Run

Provide a validated problem statement and opt into Gemini only when semantic
evaluation is wanted. The evaluator supports only `gemini-2.5-flash`, caches
validated responses, and cites supporting slide numbers:

```bash
export GEMINI_API_KEY="..."

evaluate-deck \
  --deck-ir evidence/input/deck-ir.json \
  --problem problem.json \
  --render-dir evidence/input/renders \
  --semantic-cache-dir .cache/pptx-evaluation \
  --output evidence/input/evaluation.json
```

`--render-dir` accepts raster slide images such as PNG or JPEG. Aurochs writes
SVG evidence; provide rasterized slide images separately if Gemini should use
rendered pixels. No model score is inferred when the request, response schema,
or supporting evidence is unavailable.

### Output Layout

A full run commonly leaves this evidence bundle:

```text
evidence/input/
  original.pptx
  parts/              extracted package parts
  rendered/           optional Aurochs SVGs
  report.md           descriptive extraction report
  deck-ir.json        canonical evaluator input
  evaluation.json     deterministic and semantic score output
```

Cache directories should remain outside the evidence bundle or be ignored by
version control. A failed optional stage is recorded in the report and does not
invalidate native extraction.

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
The deck evaluator deliberately excludes these raster graph nodes and edges.

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
are printed to stderr so the Markdown report remains the only stdout output.

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

`evaluate_report()` remains the annotation-metric API for OCR, diagram, and
vision evidence. It is separate from the rubric evaluator below and is useful
for measuring extraction quality against labeled data.

The evaluator projection is available directly:

```python
from pptx_forensics import evaluator_json

print(evaluator_json(report))
```

## Evaluator Input

`ExtractionReport.to_evaluator_dict()` and `to_evaluator_json()` expose the
scoring boundary. The payload contains deck dimensions, deduplicated external
links and media, plus slide number, rendered-image references, visible native
text in reading order, text boxes, title candidates, slide metrics, tables, and
meaningful image evidence. Raw relationships, notes, styles, OCR word arrays,
raster graph data, and model telemetry remain outside this payload and can be
followed through `raw_evidence_ref` values. Slide metrics include compact native
geometry projections for occupied area, whitespace, empty regions, and
meaningful visual area; the raw rendered evidence records remain outside the
scoring payload.

## Deck Evaluation

The rubric evaluator consumes canonical DeckIR JSON and produces deterministic
deck-quality metrics plus optional semantic proposal scores:

```bash
evaluate-deck \
  --deck-ir evidence/input/deck-ir.json \
  --problem problem.json \
  --render-dir evidence/input/renders
```

Deterministic rubric metrics are recomputed from the supplied DeckIR on every
invocation. The output includes `rubric_version` and an
`evaluation_fingerprint`, which changes when the deck, problem statement,
rubric inputs, or semantic result changes; a previous evaluation JSON is never
used as the score source. Semantic responses are content-addressed and cached
by default so repeated evaluations of unchanged evidence remain consistent.
Use `--fresh-semantic` when a new Gemini response is explicitly required; this
bypasses the semantic cache and requires the configured API key.

Create the canonical input from the library when needed:

```python
import json
from pathlib import Path
from pptx_forensics import extract_pptx

report = extract_pptx("input.pptx")
Path("evidence/input/deck-ir.json").write_text(
    json.dumps(report.to_dict(), indent=2), encoding="utf-8"
)
```

Use `--deck-only` when no problem statement is available. Deck quality is still
reported, while proposal alignment and other semantic scores remain explicitly
unavailable. Gemini semantic scoring uses only `gemini-2.5-flash`; without
`GEMINI_API_KEY`, semantic score fields remain `null` and no network request is
made.

The output records `title_coverage`, `text_density`, `small_text_ratio`,
`overlap_ratio`, `clipping_rate`, `content_density_variation`,
`slide_type_coverage`, `evidence_visibility`, duplicate-content ratio, and
link/prototype evidence. It also measures `paragraph_content_ratio`,
`pointer_content_ratio`, `paragraph_heavy_slide_ratio`,
`ambiguous_claim_ratio`, `visual_coverage`, `whitespace_area_ratio`,
`largest_empty_region_ratio`, and `space_usage`. Long paragraph-like blocks
are scored separately from concise pointer-like blocks, and meaningful visual
area is used instead of raw image counts so decorative assets cannot satisfy
the visual criterion. Excess whitespace above 35% or a single empty region
above 20% reduces `space_usage`.

The final score is available only when both weighted groups have scores:

`final_score = 0.70 * proposal_strength + 0.30 * deck_quality`.

Each distinct missing-evidence item applies a 15-point penalty to its scored
component. Weighted groups expose the original score and total penalty in
`unpenalized_score` and `missing_evidence_penalty`.

Every evaluation also contains a `findings` dictionary with `strengths`,
`weaknesses`, and `ambiguous_points`. Findings cite slide numbers and the
metric or semantic component that produced them. Missing semantic evidence is
reported as an incomplete explanation and receives the existing component
penalty; unresolved semantic evaluation is reported as ambiguous rather than
being guessed.

## Semantic Output

`ExtractionReport.to_markdown()` is the consumer-facing report. The related
semantic dictionary and JSON methods use the same compact projection:

```json
{
  "schema_version": "1.0",
  "deck": {},
  "summary": {},
  "slides": [],
  "objects": [],
  "assets": [],
  "relationships": [],
  "visual_regions": [],
  "diagrams": [],
  "ocr": [],
  "vision": [],
  "comments": [],
  "warnings": []
}
```

Objects retain IDs, slide IDs, type, normalized `bbox`, text, z-order, concise
style properties, relationships, a simplified source, semantic status, and
final confidence. Diagram nodes and edges retain their normalized positions,
labels, direction, status, and actual flow. OCR retains extracted text,
optional line positions, status, and final confidence. Vision retains its
description, observations, nodes, edges, status, and final confidence.

Hashes, raw/resolved style payloads, EMU geometry, XML paths, evidence
references, OCR word arrays, model and engine metadata, request telemetry,
failure classifications, missing-evidence lists, detector flow candidates, and
intermediate raster detections are intentionally excluded from this output.

`DeckIR.to_dict()` and `to_canonical_json()` remain the full internal contract
for deterministic pipeline checks. They are not the consumer-facing report.
Semantic slides expose a single `flow` object with `present` and `direction`,
instead of detector-level flow fields.

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
visual evidence tests, OCR caching tests, diagram uncertainty tests,
Gemini schema/retry tests, evaluator-boundary tests, and evaluation metrics.

## Repository Layout

```text
src/pptx_forensics/
  extractor.py       Native OOXML extraction
  models.py          DeckIR contract and validation
  evaluator.py       Compact scoring input projection
  deck_evaluation.py Rubric metrics and optional semantic scoring
  evaluate_cli.py    Deck evaluation command line interface
  output.py          Semantic projection and Markdown output
  visual.py          Deterministic visual evidence
  ocr.py             Asset-scoped OCR
  diagrams.py        Native and raster diagram evidence
  vision.py          Optional Gemini vision
  evaluation.py      Annotation-based metrics
  render.py          Optional Aurochs rendering
  cli.py             Command-line interface
tests/               Regression tests
renderers/           Renderer runner integrations
scripts/             Optional renderer bootstrap scripts
```

Native extraction is production-oriented. Raster graph and Gemini results remain
probabilistic evidence; the deck evaluator excludes raster graph topology from
deterministic quality scoring and requires explicit evidence for semantic
judgments.

## License

Licensed under the GNU General Public License, version 3. See `LICENSE`.
