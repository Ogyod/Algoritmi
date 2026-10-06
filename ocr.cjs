// Python sends image paths and dimensions through stdin, never through a shell.
const fs = require("node:fs");
const path = require("node:path");
const { createWorker } = require("tesseract.js");

async function main() {
  const images = JSON.parse(fs.readFileSync(0, "utf8"));
  const worker = await createWorker(["rus", "eng"], 1, {
    langPath: path.join(__dirname, ".models"),
    gzip: false,
    cacheMethod: "none",
    errorHandler: (error) => {
      process.stderr.write(String(error));
      process.exit(1);
    },
  });
  try {
    await worker.setParameters({
      tessedit_pageseg_mode: "3",
      preserve_interword_spaces: "1",
    });
    const pages = [];
    for (const image of images) {
      const { data } = await worker.recognize(
        image.path,
        {},
        { text: true, blocks: true },
      );
      const lines = (data.blocks || [])
        .flatMap((block) => block.paragraphs || [])
        .flatMap((paragraph) => paragraph.lines || []);
      pages.push(
        lines.map((line) => ({
          text: line.text.trim(),
          confidence: line.confidence / 100,
          x: line.bbox.x0 / image.width,
          y: line.bbox.y0 / image.height,
          width: (line.bbox.x1 - line.bbox.x0) / image.width,
          height: (line.bbox.y1 - line.bbox.y0) / image.height,
        })),
      );
    }
    process.stdout.write(JSON.stringify(pages));
  } finally {
    await worker.terminate();
  }
}

main().catch((error) => {
  process.stderr.write(String(error));
  process.exit(1);
});
