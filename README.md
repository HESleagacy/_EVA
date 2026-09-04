# Document Forensics

Forensic extraction and evidence generation for PowerPoint `.pptx` and PDF files.

`pptx-forensics` selects a native adapter from the input signature. PPTX files
are read as OOXML packages and PDF files are read as PDF objects. It preserves
the source structure and provenance, and adds optional OCR, diagram, rendering,
and vision evidence without allowing derived results to overwrite native facts.

## What It Does

- Extracts package parts, hashes, relationships, slide inheritance, media,
  notes, comments, hyperlinks, animations, alt text, and embedded parts.
- Extracts PDF metadata, page boxes, rotation, labels, outlines, text spans,
  image XObjects, vector paths, annotations, attachments, and security flags.
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

- Native source facts are authoritative (`native_ooxml` for PPTX and
  `native_pdf` for PDF).
- Derived evidence is stored separately from native objects.
- Every derived record includes status, confidence, source, and evidence
  references.
- `verified`, `partial`, `unverified`, `failed`, `not_requested`, and
  `not_applicable` remain distinct states.
- No unverified diagram edge is counted as a verified result.
- A Gemini diagram may be promoted to `verified` only when it has non-noise
  nodes, all returned nodes and edges are verified, and mean observation
  confidence is at least `0.75`; native and raster records remain unchanged.
- Optional stages are explicit and never required for native extraction.
- Canonical output is deterministic and suitable for hashing and golden tests.

## Operations Guide

### Runtime Model

`pptx-forensics` is a batch processor. Each CLI invocation reads one PPTX or PDF and
writes a report and optional evidence files; the repository does not include a
web server, worker queue, database, container image, or telemetry backend.
Applications that need an API or queue should wrap the CLI or library and own
job scheduling, authentication, quotas, and retention.

The processing path is:

```text
PPTX or PDF input
  -> native source extraction (local and deterministic)
  -> optional rendering, OCR, diagram, and vision stages
  -> DeckIR, Markdown report, evidence bundle, and optional evaluation JSON
```

Use a unique evidence directory for every job. The extractor copies the source
and native evidence parts into that directory, so an evidence directory is
both an output location and a copy of the input. Do not let concurrent jobs
write to the same evidence directory.

The native extractor reads the source bytes into memory and retains native and
derived records in the process. The PDF adapter also applies defensive defaults
of 256 MiB, 10,000 pages, and 500,000 native objects; callers can tighten those
limits through the library API. Set upload-size, memory, CPU, timeout, and
temporary-storage limits in the surrounding service or job runner as well.

### Stage Matrix

| Stage | Required | Runtime dependencies | Network at runtime | Failure behavior |
| --- | --- | --- | --- | --- |
| Native OOXML extraction | Yes | Python, `defusedxml`, `lxml` | None | Invalid input exits non-zero |
| Native PDF extraction | Yes for PDF input | Python, `pypdf` | None | Invalid input or missing password exits non-zero |
| Native visual and diagram evidence | Default, disable with `--native-only` | Python package | None | Evidence is marked uncertain or warnings are recorded |
| OCR | Optional | Pillow and Tesseract | None | Record becomes unavailable or failed; extraction continues |
| Aurochs rendering | Optional | Bun and sparse Aurochs checkout | None after setup | Warning and failed render visibility; native data remains |
| PDF page rendering | Optional | Poppler `pdftocairo` | None | Warning and failed render visibility; native data remains |
| Gemini vision | Optional | API key and outbound HTTPS | Gemini API | Unavailable, failed, or circuit-open evidence; no guessed result |
| Deck rubric evaluation | Optional | `evaluate-deck` and DeckIR JSON | None with `--skip-semantic` or `--deck-only` | Input/configuration errors exit non-zero |
| Gemini semantic evaluation | Optional | Valid problem statement and API key | Gemini API | Semantic status is recorded; scores remain unavailable when needed evidence is missing |

Optional-stage failures do not invalidate a completed native extraction. Treat
the status fields, `warnings`, `failure_class`, and evaluation `missing_evidence`
fields as part of the operational result rather than relying only on the
process exit code.

### Deployment Profiles

Use the smallest profile that satisfies the job:

| Profile | Install | Use |
| --- | --- | --- |
| Deterministic CI extraction | `.[pptx]` | Native package facts and stable DeckIR with no external services |
| Evidence worker | `.[pptx,ocr]` plus Tesseract; optionally Bun/Aurochs and Poppler | OCR, rendering, and diagram evidence with local dependencies |
| Semantic evaluator | `.[pptx]` plus a secret-managed Gemini key | Proposal scoring and optional rendered-image context |
| Repository test job | `.[pptx,test,ocr]` | Unit and regression tests; Tesseract is only needed for OCR paths |

For production, build a wheel in CI and install that artifact in an isolated
runtime. The repository currently has no lockfile, Dockerfile, or CI workflow;
pin the Python version and resolved dependency versions in the deployment
system or an external constraints file.

### Production Installation

The editable install is convenient for development. A runtime image or host
should install the built package instead:

```bash
python -m venv /opt/pptx-forensics/venv
source /opt/pptx-forensics/venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ".[pptx]"
```

Install the optional extras only in images or workers that use them:

```bash
python -m pip install ".[pptx,ocr]"
sudo apt-get install -y tesseract-ocr
```

Keep the Aurochs checkout outside the Python package. The bootstrap script
uses a sparse checkout and skips Puppeteer browser download by default because
the SVG bridge does not require it:

```bash
./scripts/fetch_aurochs_renderer.sh
```

The script needs GitHub and package-registry access during image or host
provisioning. It is not required for native extraction, OCR, or diagram
processing.

### Configuration And Secrets

Configuration is intentionally small. Explicit function or CLI arguments take
precedence over environment variables, and a local `.env` file only fills
variables that are not already set in the environment. `.env` is suitable for
local development only; use the workload identity or secret manager provided
by the deployment platform in production.

| Variable | Default | Used by | Operational meaning |
| --- | --- | --- | --- |
| `GEMINI_API_KEY` | None | Gemini vision and semantic evaluation | Secret; absence disables the corresponding optional stage |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini vision | Overrides the vision model; keep the semantic evaluator on its supported model |
| `AUROCHS_ROOT` | None at runtime; bootstrap defaults to `vendor/aurochs` | Rendering | Path to the sparse Aurochs checkout |
| `AUROCHS_BUN_COMMAND` | `bun` from `PATH` | Rendering | Optional Bun executable or command override |
| `XDG_CACHE_HOME` | `~/.cache` | Render, OCR, vision, and evaluation caches | Base directory for default cache roots |
| `OPENXML_VALIDATOR_COMMAND` | None | `validate_with_openxml_sdk()` | Optional local validator command; the PPTX path is appended to it |

The example configuration is in `.env.example`:

```bash
cp .env.example .env
# edit .env locally; never commit it or print it in CI logs
```

The CLI calls `load_dotenv()` and checks the current working directory and
repository root. Environment variables already supplied by the process are
never overwritten.

### Storage And Caching

There is no database or server-side application state. The two state classes
are job evidence and local caches:

| Location | Contents | Safe to remove |
| --- | --- | --- |
| `evidence/<job>/` | Original PPTX, package parts, reports, renders, and evidence records | Yes after retention requirements are met; it cannot be reconstructed without the input |
| `$XDG_CACHE_HOME/pptx-forensics/render/` | Aurochs SVGs keyed by source, slide, and renderer version | Yes; the next render rebuilds it |
| `$XDG_CACHE_HOME/pptx-forensics/ocr/` | Validated OCR results keyed by asset and OCR engine settings | Yes; the next OCR run rebuilds it |
| `$XDG_CACHE_HOME/pptx-forensics/vision/` | Validated Gemini responses and usage metadata | Yes; deleting it may create new API calls and cost |
| `$XDG_CACHE_HOME/pptx-forensics/evaluation/` | Validated semantic evaluation responses | Yes; deleting it may create a new semantic request |

Explicit `--*-cache-dir` arguments override the default cache locations. Mount
cache directories as writable persistent volumes when repeatability and API
cost matter. Use isolated cache locations when different tenants or data
classification boundaries must not share derived content. Cache keys include
content and relevant engine/model versions, but caches are not an access
control boundary.

Evidence and caches can contain extracted text, images, OCR output, and model
responses. Apply restrictive volume permissions, retention policies, and
encryption appropriate to the input data. The `evidence/`, `*.pptx`, `.env`,
and renderer checkout paths are ignored by this repository's Git configuration;
that does not replace storage access controls.

### Network, Egress, And Cost

Native extraction, native visual evidence, OCR, diagram reconstruction, and
evaluation with `--skip-semantic` or `--deck-only` can run without runtime
network access. Aurochs setup requires source and package-registry access, but
rendering itself runs locally after dependencies are installed.

Gemini is the only runtime network integration. If it is enabled, allow
outbound HTTPS to `generativelanguage.googleapis.com` and inject
`GEMINI_API_KEY` without placing it in command arguments. Requests can include
selected slide images and extracted text, so obtain approval for the relevant
data boundary before enabling the stage.

Vision requests default to a 30-second timeout, two retries, and a maximum of
two concurrent requests. Select only the required slides or assets, use the
cache, and monitor the emitted `estimated_cost_usd`, token usage, request
duration, `cache_hit`, and `attempts` metadata. Cost estimates are indicative,
not a billing record. Use `--skip-vision`, `--skip-semantic`, or `--deck-only`
for an explicitly offline run.

### CI/CD Checks

Run these checks from a clean checkout. Use an isolated temporary directory for
generated reports and evidence:

```bash
python -m pip install -e "[pptx,test,ocr]"
python -m pip check
python -m compileall -q src tests
PYTHONPATH=src pytest -q
pptx-forensics --help
evaluate-deck --help
git diff --check
```

For a deterministic smoke test, provide a fixture through `INPUT_PPTX`:

```bash
RUN_DIR="$(mktemp -d)"
pptx-forensics "$INPUT_PPTX" \
  --native-only \
  --evidence-dir "$RUN_DIR/evidence" \
  --output "$RUN_DIR/report.md" \
  --deck-ir-output "$RUN_DIR/deck-ir.json"

evaluate-deck \
  --deck-ir "$RUN_DIR/deck-ir.json" \
  --deck-only \
  --output "$RUN_DIR/evaluation.json"

test -s "$RUN_DIR/report.md"
test -s "$RUN_DIR/deck-ir.json"
test -s "$RUN_DIR/evaluation.json"
```

Do not make CI depend on a Gemini response for a pass/fail check unless the
pipeline explicitly controls API credentials, egress, quota, model version,
and cost. Prefer deterministic extraction and `--deck-only` evaluation for
pull-request checks; run semantic scoring as a separately observable job.

### Runbook And Troubleshooting

- **Rendering is skipped:** confirm `AUROCHS_ROOT` points to the sparse checkout
  and that Bun is available. Native extraction and the rest of the evidence
  pipeline can still be accepted when rendering is optional.
- **OCR is unavailable:** install Tesseract and the `ocr` extra, or treat the
  recorded `not_applicable`/`failed` status as an explicit missing capability.
- **Gemini is unavailable:** check secret injection, outbound DNS/HTTPS, model
  access, timeout, and quota. The output remains valid with semantic scores
  set to unavailable.
- **A run is unexpectedly slow or large:** restrict slide and asset selectors,
  use `--native-only` for the minimum path, and move caches/evidence to storage
  with sufficient space.
- **A result appears stale:** inspect `cache_hit`, `content_hash`, and
  `evaluation_fingerprint`; remove the relevant cache or use
  `--fresh-semantic` when a new semantic request is intentional.
- **A job fails before producing a report:** classify it as an input or
  configuration failure and retain stderr plus the command version. Optional
  stage failures should instead be visible in report warnings and evidence
  statuses.

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
the default Tesseract adapter is used. PDF page rendering uses the optional
local Poppler `pdftocairo` command.

## Quick Start

Write one descriptive Markdown report:

```bash
pptx-forensics input.pptx --output report.md
```

The same command accepts a PDF:

```bash
pptx-forensics input.pdf --output report.md
```

Use `--evidence-dir` when the original archive and package parts are also
needed; the report remains the single Markdown output:

```bash
pptx-forensics input.pptx \
  --evidence-dir evidence/input \
  --output report.md
```

Without `--output`, the Markdown report is printed to stdout. PPTX evidence
contains the original archive and package parts under `parts/`; PDF evidence
also contains extracted native assets and per-page native records.

For library use:

```python
from pptx_forensics import extract_document

report = extract_document("input.pptx", evidence_dir="evidence/input")
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

Missing evidence has a high default penalty of 25 points per distinct item.
Override it explicitly with `--missing-evidence-penalty` when a rubric requires
a different policy.

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

### Batch Submission Ranking

Use `rank-submissions` when several PPTX or PDF submissions target the same
problem statement. Provide one validated weighted problem JSON for that group:

```bash
rank-submissions \
  --input-dir submissions \
  --problem problems/26168.json \
  --output-dir reports
```

The command runs native extraction and the selected evidence stages for every
submission, evaluates each deck against the same problem statement, ranks only
submissions with a numeric final score, and places unavailable or failed cases
in `REVIEW`. Use `--problem-dir` to resolve different problem JSON files by PS
ID. The default buckets are `A - Excellent` (85-100), `B - Strong` (70-84.99),
`C - Promising` (55-69.99), `D - Needs work` (below 55), and `REVIEW`.

The output directory contains only Markdown files: one `report.md` per
submission and one `ranking.md` grouped by PS. Intermediate DeckIR, evidence,
and cache data are kept outside the output directory.

When semantic evaluation is available, each submission report also contains an
evidence-grounded reviewer assessment with five area ratings, strengths, risks,
limitations, a decision, and the next evidence requested. Linked repositories
and external PS pages are not browsed; missing evidence is reported and
penalized rather than guessed.

### Output Layout

A full run commonly leaves this evidence bundle:

```text
evidence/input/
  original.pptx       or original.pdf
  parts/              extracted package parts or document.pdf
  pdf/assets/         extracted PDF image/attachment assets
  pages/              per-page PDF native records
  rendered/           optional Aurochs SVGs or PDF PNGs
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

Vision responses use strict `gemini-vision-v3` JSON. Valid model-only claims
remain `partial` unless the response satisfies the high-confidence diagram
promotion gate above; invalid JSON/schema responses are retried within the
configured retry budget. Cache records retain the prompt/model/image hash,
usage, estimated cost, duration, attempts, evidence status, and sanitization
metadata.

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

Each distinct missing-evidence item applies a 25-point penalty to its scored
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
