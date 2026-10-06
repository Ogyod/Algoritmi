// The same OCR passes run in Node and in the browser.
export function headingPresent(text) {
  return /ат[тг]естат[ау]?[^\n]{0,100}(?:основн|средн)|диплом[^\n]{0,100}(?:бакалавр|специалист|магистр|аспиран|профессионал)/iu.test(text);
}

function blocksFrom(data, width, height) {
  return (data.blocks || [])
    .flatMap((block) => block.paragraphs || [])
    .flatMap((paragraph) => paragraph.lines || [])
    .filter((line) => line.text.trim())
    .map((line) => ({
      text: line.text.trim(), confidence: line.confidence / 100,
      x: line.bbox.x0 / width, y: line.bbox.y0 / height,
      width: (line.bbox.x1 - line.bbox.x0) / width,
      height: (line.bbox.y1 - line.bbox.y0) / height,
    }));
}

export async function recognizePage(worker, image, width, height, enhancedImage) {
  await worker.setParameters({tessedit_pageseg_mode: "3", preserve_interword_spaces: "1"});
  const {data} = await worker.recognize(image, {}, {text: true, blocks: true});
  const blocks = blocksFrom(data, width, height);
  // A patterned title page can lose its coloured heading while retaining its
  // black serial number. Retry only sparse pages, keeping grade tables intact.
  const readableLines = blocks.filter((line) => /[а-яё]{4}/iu.test(line.text)).length;
  if (readableLines < 8 && !headingPresent(data.text) && enhancedImage) {
    await worker.setParameters({tessedit_pageseg_mode: "6"});
    const {data: heading} = await worker.recognize(await enhancedImage(), {
      rectangle: {left: Math.round(width * .1), top: Math.round(height * .18),
                  width: Math.floor(width * .8), height: Math.floor(height * .36)},
    }, {text: true, blocks: true});
    const extra = blocksFrom(heading, width, height).filter((line) =>
      /ат[тг]естат|диплом|основно|средн|образован|бакалавр|магистр|специалист|аспиран/iu.test(line.text));
    for (const line of extra) {
      if (!blocks.some((old) => old.text === line.text && Math.abs(old.y - line.y) < .02))
        blocks.push(line);
    }
  }
  return blocks;
}
