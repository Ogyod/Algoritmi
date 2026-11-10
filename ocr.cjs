// Python sends local image paths through stdin, never through a shell.
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const {execFile} = require('node:child_process');
const {promisify} = require('node:util');
const {createWorker} = require('tesseract.js');
const execute = promisify(execFile);

async function createRecognizer() {
  const {recognizePage} = await import('./static/ocr-engine.js');
  const workers = [];
  const configured = Number(process.env.OCR_PAGE_WORKERS || 2);
  const maximum = Number.isInteger(configured) ? Math.max(1, Math.min(2, configured)) : 2;
  async function ensureWorkers(count) {
    while (workers.length < count) {
      workers.push(await createWorker(['rus', 'eng'], 1, {
        langPath: path.join(__dirname, '.models'), gzip: false, cacheMethod: 'none',
        errorHandler: (error) => { process.stderr.write(String(error)); process.exit(1); },
      }));
    }
  }
  async function enhanced(image) {
    if (!fs.existsSync(image.enhanced_path)) {
      await execute(process.env.OCR_WORKER_PYTHON || 'python', [
        '-X', 'utf8', path.join(__dirname, 'ocr_worker.py'), '--enhance',
        image.path, image.enhanced_path,
      ], {timeout: 30000, windowsHide: true});
    }
    return image.enhanced_path;
  }
  return {
    async recognize(images) {
      const count = Math.min(maximum, images.length);
      await ensureWorkers(count);
      const pages = new Array(images.length);
      let next = 0;
      await Promise.all(workers.slice(0, count).map(async (worker) => {
        for (;;) {
          const index = next++;
          if (index >= images.length) return;
          const image = images[index];
          pages[index] = await recognizePage(worker, image.path, image.width, image.height,
            image.enhanced_path ? () => enhanced(image) : null);
        }
      }));
      return pages;
    },
    async close() { await Promise.all(workers.map(worker => worker.terminate())); },
  };
}

async function main() {
  const reader = await createRecognizer();
  try {
    if (process.argv.includes('--serve')) {
      const input = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
      for await (const line of input) {
        try {
          const pages = await reader.recognize(JSON.parse(line));
          process.stdout.write(JSON.stringify({ok: true, pages}) + '\n');
        } catch {
          process.stdout.write(JSON.stringify({ok: false, pages: []}) + '\n');
        }
      }
    } else {
      const images = JSON.parse(fs.readFileSync(0, 'utf8'));
      process.stdout.write(JSON.stringify(await reader.recognize(images)));
    }
  } finally {
    await reader.close();
  }
}

main().catch((error) => { process.stderr.write(String(error)); process.exit(1); });
