# Agentic IDE Execution Prompt

You are operating in the repository root. Complete the remaining submission evaluations end to end. Do not stop at a plan or at the first error.

## Objective

Process every row currently present in `submissions.tsv`. This manifest has already been reduced to submissions that do not yet have a confirmed numeric score. Do not restore scored rows and do not delete unresolved rows until their reports contain numeric final scores.

The completion condition is:

- Every submission row in `submissions.tsv` has a corresponding current report under `evidence/`.
- Every corresponding report contains a numeric `Final score` out of 100.
- No current submission remains in `REVIEW` or `failed` status.
- Gemini semantic scoring was used; do not substitute a deck-only or `--skip-semantic` score.

## Required Workflow

1. Inspect the repository, `submissions.tsv`, existing `evidence/` reports, `.env`, and the evaluation code before running anything.
2. Treat the `GEMINI_API_KEY` in `.env` as secret. Never print it, commit it, or include it in logs or command arguments. The CLI loads `.env` automatically.
3. Run the full pipeline for the current manifest. Do not use `--native-only`, `--skip-render`, `--skip-ocr`, `--skip-diagrams`, `--skip-vision`, `--skip-semantic`, or `--deck-only` for the final run.
4. Use the following command, adjusting only paths when necessary:

```bash
rank-submissions \
  --manifest submissions.tsv \
  --output-dir evidence \
  --semantic-timeout 120 \
  --fresh-semantic \
  --semantic-cache-dir .cache/pptx-evaluation \
  --render-cache-dir .cache/pptx-render \
  --ocr-cache-dir .cache/pptx-ocr \
  --diagram-ocr-cache-dir .cache/pptx-ocr \
  --vision-cache-dir .cache/pptx-vision
```

5. Run submissions sequentially unless there is a safe, explicit reason to partition the manifest. Do not allow two workers to write the same report directory or `ranking.md` concurrently.
6. Monitor progress and retain the full stdout/stderr log. A warning from OCR, rendering, or a PDF library is not by itself a failure; inspect the generated report and status.
7. For every `REVIEW` or `failed` result:
   - Read the report and log error.
   - Determine whether the problem is download, extraction, PS-ID inference, official problem lookup, Gemini timeout, Gemini schema validation, or output generation.
   - Fix the smallest correct code/configuration issue.
   - Add a focused regression test for a parser or evaluator bug when practical.
   - Rerun only the affected manifest row with the full pipeline and Gemini enabled.
   - Do not convert a missing model result into a guessed score.
8. If OCR splits an official ID, normalize IDs across whitespace and character spacing before problem lookup. If a deck explicitly contains a valid PS ID that inference cannot recover, use that verified ID with `--ps-id` for the single affected row. Do not invent a PS ID from a vague topic.
9. If Gemini returns a malformed semantic response, retry with the configured timeout and a repair instruction requiring numeric component scores, evidence slides, and explicit `missing_evidence`. Preserve strict validation and do not silently accept unsupported claims.
10. Use cached rendering/OCR/vision data to avoid unnecessary repeat work, but use `--fresh-semantic` whenever rerunning a failed or suspect Gemini score.

## Verification

After processing, run a verification script that:

- Parses all rows in the current `submissions.tsv`.
- Matches each team to its current report, not an old duplicate report from a previous run.
- Confirms every report has a numeric `Final score` between 0 and 100.
- Confirms no current report has status `failed` or final score `REVIEW`.
- Prints the number scored, number unresolved, and the unresolved team names.

Also run:

```bash
PYTHONPATH=src pytest -q
git diff --check
```

Do not stop with unresolved rows. Continue fixing and rerunning until the current manifest is fully scored, or report a concrete external blocker such as an unavailable URL or exhausted Gemini quota with its exact affected team names and logs.

## Final Response

Report:

- Number of current manifest rows processed.
- Number with numeric scores.
- Any unresolved rows and the exact external blocker.
- The output location of the reports and ranking.
- Tests and verification commands run.

Do not include the API key or any secret value in the response.
