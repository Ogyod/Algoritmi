// Local Tesseract fallback when the native executable is not installed.
const fs = require('node:fs');
const {createWorker} = require('tesseract.js');
(async () => {
  const {pages, models} = JSON.parse(fs.readFileSync(0, 'utf8'));
  for (const lang of ['rus', 'eng']) {
    if (!fs.existsSync(require('node:path').join(models, lang + '.traineddata'))) throw new Error('Local language data missing');
  }
  const worker = await createWorker(['rus', 'eng'], 1, {langPath: models, gzip: false, cacheMethod: 'none',
    errorHandler: () => process.exit(1)});
  const lines = [];
  try {
    await worker.setParameters({tessedit_pageseg_mode: '3'});
    for (const page of pages) {
      const {data} = await worker.recognize(page.path, {}, {blocks: true});
      for (const block of data.blocks || []) for (const paragraph of block.paragraphs || [])
        for (const line of paragraph.lines || []) if (line.text.trim()) lines.push({
          text: line.text.trim(), confidence: line.confidence / 100, page: page.page,
          box: [line.bbox.x0, line.bbox.y0, line.bbox.x1, line.bbox.y1],
        });
    }
    process.stdout.write(JSON.stringify(lines));
  } finally { await worker.terminate(); }
})().catch(() => process.exit(1));
