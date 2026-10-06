// Python sends image paths and dimensions through stdin, never through a shell.
const fs = require("node:fs");
const path = require("node:path");
const { createWorker } = require("tesseract.js");

async function main() {
  const images = JSON.parse(fs.readFileSync(0, "utf8"));
  const {recognizePage} = await import("./static/ocr-engine.js");
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
    const pages = [];
    for (const image of images) {
      pages.push(await recognizePage(worker, image.path, image.width, image.height,
        image.enhanced_path ? () => image.enhanced_path : null));
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
