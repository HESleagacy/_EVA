# PPTX Forensics

`pptx-forensics` treats a `.pptx` as an OOXML package first and a presentation
second. It records the source hash, every package part, relationship graph,
slide inheritance, media hashes, and XML-visible semantic signals. The
`python-pptx` layer is optional enrichment and is never the source of truth.

## Usage

```bash
pip install -e '.[pptx,test]'
pptx-forensics input.pptx --evidence-dir evidence/input
```

The evidence directory contains the original archive, a copy of each package
part under `parts/`, and `manifest.json`. Without `--evidence-dir`, extraction
is read-only and the report is printed as JSON.

As a library:

```python
from pptx_forensics import extract_pptx

report = extract_pptx("deck.pptx", evidence_dir="evidence/deck")
print(report.to_json())
```

The package parser uses `defusedxml` for untrusted XML. `python-pptx` can be
installed as the optional `pptx` extra for convenience-layer shape summaries.

## Canonical DeckIR

`ExtractionReport.to_dict()` and `manifest.json` use one stable DeckIR contract:

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

Native objects are sourced from `native_ooxml` and contain stable IDs, slide
IDs, parent IDs, normalized bounding boxes, z-order, text, raw and resolved
style metadata, field-level inheritance sources, semantic status, relationship
IDs, native shape subtypes, and XML provenance. Native visual geometry facts are emitted in
`rendered_evidence` with the `rendered_cv` source layer. Rendered computer
vision, OCR, and vision-model results have separate source provenance. Use
`DeckIR.add_evidence()` to append derived evidence; it validates the layer and
cannot overwrite native object facts.

## Phase 10: Frozen DeckIR v1

DeckIR v1 is identified by `schema_version: "1.0"` at the canonical root and
in `deck` and `provenance` metadata. Any schema change requires a version bump;
the validator rejects unsupported versions and missing required fields.

Derived records in `visual_regions`, `rendered_evidence`, `ocr_evidence`, and
`vision_evidence` require top-level `status`, `source`, `confidence`, and
`evidence_refs`. The only evidence statuses are `verified`, `partial`,
`unverified`, `failed`, `not_requested`, and `not_applicable`.

Slides expose separate `slide_reading_order` and `diagram_flow_direction`
fields. `flow_present` is `true` only when evidence supports a flow, `false`
only with a supported absence basis, and `null` when it cannot be determined.
`visual_regions` is a collection of compact region records rather than one
overloaded visual-region field. Layer availability is named
`visual_evidence_visibility`; `evidence_quality` is not part of DeckIR v1.

## Reproducibility And Benchmarks

`ExtractionReport.to_canonical_json()` emits compact, UTF-8 JSON with sorted
keys for hashing and golden comparisons. Native slide and object ordering is
deterministic, and geometry values are rounded to stable precision.

The benchmark suite includes a golden LLVM report and a synthetic fixture for
text, tables, charts, SmartArt, groups, connectors, hyperlinks, notes,
embedded objects, and rotated shapes. `compute_metrics()` reports text recall,
object recall, bounding-box accuracy, relationship resolution, and asset
resolution.

Malformed XML, external entities, and ZIP path traversal are covered by
adversarial tests. Open XML SDK validation is an optional sidecar callable
from Python:

```bash
python -c 'from pptx_forensics import validate_with_openxml_sdk; print(validate_with_openxml_sdk("input.pptx"))'
```

Set `OPENXML_VALIDATOR_COMMAND` or pass `command=...` to
`validate_with_openxml_sdk(path, command=...)`. The sidecar is not required for
native extraction.

## Native Style And Semantics

Phase 3 resolves theme, master, layout, placeholder, shape, paragraph, and run
formatting without replacing raw OOXML values. Objects also receive native
semantic projections for tables, charts, SmartArt, groups, images, and embedded
objects. Embedded parts are retained as evidence and are never executed.

## Phase 6: Native Visual Geometry And Optional Aurochs Rendering

Phase 6 visual extraction is native-only by default. Each slide receives
deterministic evidence for occupancy, whitespace regions, margins, overlap,
alignment, equal spacing, clipping/overflow, font-size distribution, color
consistency, shape density, text density, rotations, native shape hierarchy
candidates, and native geometry peer groups. Near-aligned objects also produce
`alignment_mismatch` evidence.
Displayed image objects inspect the original package asset directly and do not
rasterize the complete slide. Native fact values use `source: "native_ooxml"`
and retain the calculation method separately. These records are evidence, not
SIH quality scores.

When a slide is explicitly rendered, the optional SVG stage adds native versus
rendered geometry verification facts. It does not replace native boxes and is
not needed for ordinary extraction.

Native OOXML and asset extraction is the default. Rendering is a separate
optional stage and runs only for explicitly selected slides:

```bash
./scripts/fetch_aurochs_renderer.sh
AUROCHS_ROOT=vendor/aurochs \
  pptx-forensics deck.pptx \
  --evidence-dir evidence/deck \
  --render-slides 1,3-5
```

The bootstrap uses a sparse checkout of Aurochs' PPTX parser, SVG renderer,
and their required dependency trees. It does not copy Aurochs into the Python
package. Rendering creates `rendered/slide-XX.svg` files and appends records to
`rendered_evidence` containing SVG hashes, cache keys, renderer name, renderer
commit, and runner provenance.

Render cache keys are:

```text
pptx_hash + slide_number + renderer + renderer_version
```

Missing renderers and per-slide render failures become warnings. Native-only
mode does not require Bun, Aurochs, or any renderer. Use `--native-only` when
only package, slide, object, asset, and relationship facts are wanted.

## Optional OCR

OCR is asset-scoped and is never run against a complete slide. Native text is
used first; only displayed image assets without native text are sent to the
OCR adapter:

```bash
pptx-forensics deck.pptx \
  --evidence-dir evidence/deck \
  --ocr-slides 3,5-10 \
  --ocr-cache-dir .cache/pptx-ocr
```

Install the Python OCR extra and ensure the Tesseract executable is available:
`pip install -e '.[ocr]'`. OCR evidence contains normalized word and line
boxes, confidence, asset hash, engine/version provenance, status, and cache
key. Slide selection ignores assets smaller than the default `256`-pixel
threshold; `--ocr-assets` explicitly selects an asset and bypasses that gate.
Use `--skip-ocr` to produce the same valid native report without OCR.

## Phase 7: Diagram Reconstruction

Phase 7 emits `diagram_graph` evidence records in `rendered_evidence`. Native
graphs use shape nodes, connector edges, native endpoints, arrowheads, group
hierarchy, and spatial endpoint proximity. Every node and edge carries
`evidence_refs` back to native objects or detection/OCR records.

Raster reconstruction is explicit and asset-scoped. It runs OCR, masks OCR text
regions before line/arrow scans, then applies deterministic
line/contour/box/arrow heuristics and candidate graph assembly without a model:

```bash
pptx-forensics deck.pptx \
  --diagram-slides 8-10 \
  --diagram-ocr-cache-dir .cache/pptx-ocr
```

`verified`, `partial`, `unverified`, and `failed` statuses remain visible in
the graph and its nodes/edges. Raster pixel detections are candidate evidence;
uncertain endpoint or edge verification keeps the graph `unverified`. Missing
OCR or zero edge verification can never produce a `verified` graph. Graph
output is evidence rather than a judgment.

## Phase 9: Selective Gemini Vision

Gemini is an explicit, opt-in interpretation stage. It receives only selected
original image assets and, when needed, selected rendered slide images or crops,
together with OCR,
native object candidates, and diagram evidence. Repeated small corner assets,
logos, decorative images, and repeated full-slide template images are filtered
before a request unless `--vision-include-noise` is supplied.

Requests are gated by low OCR confidence, missing diagram edges, unknown image
role, unclear reading direction, native/rendered disagreement, or unsupported
SmartArt. Each request is limited to one slide or crop and uses a content-hash
cache that includes the prompt version, model, image hashes, OCR, and native
context. Requests use compact context, bounded thinking/output budgets, optional
 bounded concurrency, retries only for transient transport failures, timeouts,
 and a circuit breaker.

Responses are post-processed conservatively: model-only verified statuses are
downgraded, model-only edges remain `unverified`, and obvious slide chrome is
removed from the diagram graph. Usage, duration, attempts, and sanitization
counts are retained in the evidence metadata.
When usage metadata is returned, the evidence also includes an estimated
standard-tier USD cost using the configured model's reported input, output, and
thinking token counts.

Gemini results are strict JSON in `vision_evidence` with the `vision_model`
source layer. They never overwrite native OOXML, rendered geometry, or OCR
records. Without a configured `GEMINI_API_KEY`, the stage records an unavailable
optional result and makes no network request. `--skip-vision` leaves the
deterministic native report unchanged:

```bash
# Copy `.env.example` to `.env` and set `GEMINI_API_KEY` first.
pptx-forensics deck.pptx \
  --vision-slides 8-10 \
  --vision-cache-dir .cache/pptx-vision \
  --vision-render-dir evidence/deck
```

The current vision defaults are a `1024`-token thinking budget, an `8192`
token output limit, and up to two concurrent Gemini requests. Override them
with `--vision-thinking-budget`, `--vision-max-output-tokens`, and
`--vision-concurrency`. Cache records retain the raw API usage metadata,
estimated standard-tier cost, request duration, attempt count, and
sanitization counts.

## Current Implementation Status

The parser currently provides:

- Direct OOXML package extraction with source/package-part hashes, relationships,
  slide inheritance, media, notes, comments, hyperlinks, animations, alt text,
  embedded parts, native shape subtypes, normalized geometry, styles, and
  provenance.
- Deterministic native visual evidence for occupancy, whitespace, margins,
  overlap, alignment, equal spacing, clipping, font/color distributions,
  density, rotations, hierarchy, peer groups, and image-asset metadata.
- Optional Aurochs SVG comparison without replacing native geometry.
- Asset-scoped OCR with normalized word/line boxes and caching.
- Conservative native and raster diagram candidates with evidence references,
  OCR masking for line/arrow scans, four diagonal arrow directions, endpoint
  checks, and explicit uncertainty statuses.
- Opt-in Gemini analysis with strict JSON validation, compact context, image
  cropping, noise gating, role-aware sanitization, bounded retries/concurrency,
  circuit breaking, caching, and audit metadata.

The LLVM benchmark contains `17` slides, `634` native objects, `37` assets, and
`118` relationships. A fresh slides 8-10 enrichment run completed in `16.42s`
at an estimated `$0.008298`, with one Gemini request and two logo-only slides
skipped. Raster reconstruction produced `7` graph candidates with `16` nodes
and `5` edges; all edges correctly remain unverified because no stronger edge
evidence is available.

The regression suite currently passes `25` tests. The post-freeze native/OCR/
raster audit serialized DeckIR `1.0` with `6` OCR records and `7` raster graph
records without a live Gemini request.

The native extraction layer is production-oriented. Raster semantic recovery
and Gemini interpretation remain probabilistic and require labeled diagrams for
precision/recall evaluation before they should be treated as production-grade.
