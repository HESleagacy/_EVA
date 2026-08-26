/**
 * Minimal Aurochs bridge used by pptx-forensics.
 *
 * It intentionally imports only the PPTX loader and SVG renderer. Aurochs is
 * kept outside the Python package and is invoked as an optional sidecar.
 */

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

type RenderOutput = {
  readonly slide: number;
  readonly path?: string;
  readonly warnings: readonly string[];
  readonly error?: string;
};

const args = process.argv.slice(2);

function option(name: string): string | undefined {
  const index = args.indexOf(name);
  return index >= 0 ? args[index + 1] : undefined;
}

function requiredOption(name: string): string {
  const value = option(name);
  if (!value) {
    throw new Error(`Missing ${name}`);
  }
  return value;
}

function parseSlides(value: string): number[] {
  const slides = new Set<number>();
  for (const part of value.split(",")) {
    const [startText, endText] = part.split("-");
    const start = Number.parseInt(startText, 10);
    const end = endText === undefined ? start : Number.parseInt(endText, 10);
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start) {
      throw new Error(`Invalid slide range: ${part}`);
    }
    for (let slide = start; slide <= end; slide += 1) {
      slides.add(slide);
    }
  }
  return [...slides].sort((a, b) => a - b);
}

async function main(): Promise<void> {
  const source = requiredOption("--source");
  const output = requiredOption("--output");
  const rendererRoot = requiredOption("--renderer-root");
  const slides = parseSlides(requiredOption("--slides"));
  await mkdir(output, { recursive: true });

  const root = rendererRoot.replace(/[\\/]$/, "");
  const pptxUrl = pathToFileURL(`${root}/packages/@aurochs-office/pptx/src/index.ts`).href;
  const rendererUrl = pathToFileURL(`${root}/packages/@aurochs-renderer/pptx/src/svg/index.ts`).href;
  const { loadPptxFromBuffer } = await import(pptxUrl);
  const { renderSlideToSvg } = await import(rendererUrl);
  const buffer = await readFile(source);
  const { presentation } = await loadPptxFromBuffer(buffer);
  const results: RenderOutput[] = [];

  for (const slideNumber of slides) {
    try {
      if (slideNumber > presentation.count) {
        results.push({ slide: slideNumber, warnings: [`Slide ${slideNumber} not found`] });
        continue;
      }
      const rendered = renderSlideToSvg(presentation.getSlide(slideNumber));
      const path = `${output}/slide-${String(slideNumber).padStart(2, "0")}.svg`;
      await writeFile(path, rendered.svg, "utf8");
      results.push({ slide: slideNumber, path, warnings: rendered.warnings });
    } catch (error) {
      results.push({ slide: slideNumber, warnings: [], error: error instanceof Error ? error.message : String(error) });
    }
  }

  process.stdout.write(JSON.stringify({ slides: results, presentation_slides: presentation.count }));
}

main().catch((error) => {
  process.stderr.write(error instanceof Error ? error.stack ?? error.message : String(error));
  process.exitCode = 1;
});
