# Document Forensics

Score and rank presentation submissions (`.pptx`, `.ppt`, `.pdf`) against a
problem statement. Drop the decks in a folder, run one command, and get a
Markdown scorecard per submission plus a ranked leaderboard.

Built for hackathon-style judging: every score cites slide numbers, missing
evidence is called out instead of guessed, and runs are reproducible.

- **Reads files natively.** PPTX is parsed as OOXML and PDF as PDF objects.
  Text, shapes, layout, images and diagrams come straight from the file, not
  from screenshots.
- **AI is optional.** With a Gemini API key it scores proposal quality and
  checks rendered slides. Without one it still scores deck quality offline.
- **One bad file never stops a batch.** Failures get a `REVIEW` report and
  the run continues.

## Quick start

Requires Python 3.10+.

```bash
git clone https://github.com/HESleagacy/_EVA.git
cd _EVA
python -m venv .venv && source .venv/bin/activate
pip install ".[pptx,ocr]"

cp .env.example .env          # add your GEMINI_API_KEY (optional)
mkdir -p submissions          # put your .pptx / .ppt / .pdf files here
review-submissions --problem problem.json
```

Results land in `evidence/`:

```text
evidence/
  ranking.md                              leaderboard, grouped by problem statement
  <problem-id>/<team>-<file>-<hash>/report.md   one scorecard per submission
```

### With Docker

```bash
docker build -t document-forensics .
docker run --rm --env-file .env \
  -v "$PWD/submissions:/workspace/submissions:ro" \
  -v "$PWD/problem.json:/workspace/problem.json:ro" \
  -v "$PWD/evidence:/workspace/evidence" \
  document-forensics review-submissions --problem problem.json
```

The image already includes Tesseract (OCR), Poppler (PDF rendering) and
LibreOffice (`.ppt` conversion).

## The problem statement

Every deck is scored against a problem statement JSON file. Requirement weights
are relative, so they don't need to add up to anything:

```json
{
  "id": "PS-1",
  "title": "Make systems safer",
  "description": "Provide a practical system hardening solution.",
  "requirements": [
    {"id": "r1", "description": "Explain the threat", "weight": 2},
    {"id": "r2", "description": "Show the solution", "weight": 1}
  ]
}
```

| Situation | Use |
| --- | --- |
| All decks answer the same problem | `--problem problem.json` |
| Decks answer different problems | `--problem-dir problems/` (one JSON per problem, matched by `id` or filename) |
| Smart India Hackathon decks | Omit both. The tool reads the PS ID from the slides (e.g. `SIH26168`) and fetches it from the official SIH site. Use `--sih-url` to pick another edition. |
| Force one PS ID for every deck | `--ps-id 26168` |

To save an SIH problem statement locally: `scrape-problem 26168`. It writes
`problems/26168.json`.

## How scoring works

Each deck gets a score out of 100:

| Proposal strength (70) | Points | Deck quality (30) | Points |
| --- | ---: | --- | ---: |
| Problem-statement alignment | 20 | Text-visual balance | 8 |
| Solution clarity | 20 | Concise, structured content | 5 |
| Technical feasibility | 15 | Readability | 5 |
| Uniqueness | 10 | Visual relevance and coherence | 5 |
| Prototype / implementation evidence | 5 | Narrative flow | 4 |
| | | Layout and use of space | 3 |

- **Proposal strength needs Gemini.** Without a key, proposal scores and the
  final score stay empty (`REVIEW`). Deck quality is still scored from the
  slide geometry.
- **Missing evidence costs points.** Each missing-evidence item deducts 10
  points from that criterion's 0–100 score (never below zero). Change it with
  `--missing-evidence-penalty`.
- **Quality penalty.** A rendered-slide review can subtract up to 4 deck points
  for repeated, visible problems such as filler text, garbled labels or
  irrelevant decoration. It never guesses whether AI wrote the deck, and color
  variety alone is never penalized.
- **Buckets.** `A - Excellent` (85+), `B - Strong` (70+), `C - Promising`
  (55+), `D - Needs work`, and `REVIEW` for anything that couldn't be scored.
  Adjust with `--excellent-threshold`, `--strong-threshold` and
  `--promising-threshold`.

## Common options

`review-submissions --help` lists everything. The ones you'll actually use:

| Option | What it does |
| --- | --- |
| `review-submissions a.pptx b.pdf` | Score specific files instead of `./submissions` |
| `--input-dir DIR` | Read a different folder (repeatable) |
| `--output-dir DIR` | Write reports somewhere other than `./evidence` |
| `--skip-semantic` | No Gemini calls: offline, free, deck quality only |
| `--native-only` | Fastest run: skip rendering, OCR, diagrams and vision |
| `--fresh-semantic` | Ignore cached Gemini scores and ask again |
| `--quiet` | Hide per-file progress |

## Configuration

Settings come from environment variables or a local `.env` file. Real
environment variables always win over `.env`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | none | Enables semantic scoring and vision. Leave it unset for offline runs. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model for slide vision. Semantic scoring always uses `gemini-2.5-flash`. |
| `AUROCHS_ROOT` | none | PPTX slide renderer checkout; set to `vendor/aurochs` after running the fetch script (see [Optional tools](#optional-tools)) |
| `XDG_CACHE_HOME` | `~/.cache` | Where render, OCR and Gemini caches live |

Gemini requests send selected slide images and extracted text to Google, so
check that's acceptable for your data first. Responses are cached under
`$XDG_CACHE_HOME/pptx-forensics/`, so re-runs on unchanged decks cost nothing.
Delete the cache, or pass `--fresh-semantic`, to re-score.

## Optional tools

Everything below is optional. When a tool is missing, its stage is recorded as
unavailable and the rest of the run continues.

| Tool | Enables | Install |
| --- | --- | --- |
| Tesseract | OCR on images inside slides | `sudo apt install tesseract-ocr` |
| Poppler | PDF page rendering | `sudo apt install poppler-utils` |
| LibreOffice | `.ppt` input | `sudo apt install libreoffice-impress` |
| Bun + Aurochs | PPTX slide rendering | `./scripts/fetch_aurochs_renderer.sh` |

## Troubleshooting

- **Every deck lands in `REVIEW`:** `GEMINI_API_KEY` is probably missing. The run
  prints a note at startup when it is. Use `--skip-semantic` if that's intended.
- **"no problem statement found for PS ID …":** the deck's PS ID wasn't in
  `--problem-dir`. Add the file or pass `--ps-id`.
- **`.ppt` files fail:** install LibreOffice (`libreoffice` or `soffice` must be
  on `PATH`).
- **A score looks stale:** results are cached by content. Use `--fresh-semantic`.
- **Runs are slow:** try `--native-only`, or `--skip-ocr` / `--skip-render`.

## Other commands

These are for working with a single file or wiring the tool into your own
pipeline.

**Extract one deck to a Markdown report:**

```bash
pptx-forensics deck.pptx --output report.md                       # also accepts .pdf
pptx-forensics deck.pptx --evidence-dir out/ --deck-ir-output out/deck-ir.json
```

Use `--render-slides`, `--ocr-slides`, `--diagram-slides` and `--vision-slides`
(e.g. `1,3-5`) to run the optional stages on chosen slides. Use `--native-only`
for raw file facts only.

**Score one extracted deck:**

```bash
evaluate-deck --deck-ir out/deck-ir.json --problem problem.json --output evaluation.json
evaluate-deck --deck-ir out/deck-ir.json --deck-only     # no problem statement, deck quality only
```

**From Python:**

```python
from pptx_forensics import extract_document, evaluate_deck, load_problem_statement

report = extract_document("deck.pptx", evidence_dir="out")
print(report.to_markdown())
result = evaluate_deck(report, load_problem_statement("problem.json"), skip_semantic=True)
print(result["final_score"], result["scores"]["deck_quality"]["score"])
```

## Contributing

Contributions are welcome: bug reports, scoring improvements and new input
formats alike.

```bash
pip install -e ".[pptx,ocr,test]"
pytest -q                     # all tests, offline, no API key needed
```

**How a deck flows through the code:**

```text
input file ─► extractor.py / pdf.py    native facts (authoritative, deterministic)
           ─► visual.py, diagrams.py   derived layout and diagram evidence
           ─► render.py, ocr.py        optional rendering and OCR
           ─► vision.py                optional Gemini vision
           ─► evaluator.py             compact scoring input
           ─► deck_evaluation.py       rubric scores (+ optional Gemini semantic scoring)
           ─► ranking.py               batch runs, buckets, Markdown reports
```

| Module | Role |
| --- | --- |
| `models.py` | The DeckIR data contract shared by every stage |
| `output.py` | Markdown and JSON report rendering |
| `problem_scraper.py` | Fetches SIH problem statements |
| `evaluation.py` | Accuracy metrics for OCR, diagrams and vision against labeled data |
| `cli.py`, `evaluate_cli.py`, `rank_cli.py`, `problem_cli.py` | The four commands |

**Ground rules:**

- **Native facts win.** Derived evidence (OCR, diagrams, Gemini) is stored
  alongside native data and never overwrites it. Every derived record carries a
  status (`verified`, `partial`, `unverified`, `failed`, …) and a confidence.
- **No guessing.** When evidence is missing, say so (`missing_evidence`, `null`
  scores) rather than inventing a value.
- **Optional stages fail soft.** A missing tool or failed API call becomes a
  warning, never a crash.
- **Tests stay offline.** Mock Gemini with a fake adapter; see
  `tests/test_evaluator.py` for examples.
- **Changing the rubric?** Bump `RUBRIC_VERSION` in `deck_evaluation.py` so
  cached results and fingerprints don't mix versions.

Untrusted input is expected. XML is parsed with `defusedxml`, ZIP paths are
validated, and embedded files are kept as evidence, never executed. If you find
a security issue, report it privately rather than in a public issue.

## License

GPL-3.0. See [`LICENSE`](LICENSE).
