import {recognizePage} from "./ocr-engine.js";
const MAX_BYTES = 20 * 1024 * 1024, MAX_PAGES = 20, MAX_PIXELS = 30000000;
const asset = (path) => new URL(path, import.meta.url).href;
const scripts = new Map();
function loadScript(path) {
  if (!scripts.has(path)) scripts.set(path, new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = asset(path);
    script.onload = resolve;
    script.onerror = () => {scripts.delete(path); script.remove(); reject(new Error("Не удалось загрузить распознавание. Проверьте соединение."));};
    document.head.appendChild(script);
  }));
  return scripts.get(path);
}

let parser;
const pending = new Map();
export function interpretDocument(payload) {
  if (!parser) {
    parser = new Worker(asset("./parser-worker.js"), {type: "module"});
    parser.onmessage = ({data}) => {
      const request = pending.get(data.id);
      if (!request) return;
      clearTimeout(request.timer);
      pending.delete(data.id);
      data.error ? request.reject(new Error(data.error)) : request.resolve(data.result);
    };
    parser.onerror = (event) => {
      console.error("Document parser worker:", event.message);
      for (const request of pending.values()) {
        clearTimeout(request.timer);
        request.reject(new Error("Обработчик остановился. Обновите страницу и повторите загрузку."));
      }
      pending.clear(); parser.terminate(); parser = null;
    };
  }
  return new Promise((resolve, reject) => {
    const id = crypto.randomUUID();
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error("Загрузка обработчика заняла слишком много времени. Обновите страницу."));
    }, 300000);
    pending.set(id, {resolve, reject, timer});
    parser.postMessage({id, payload});
  });
}

function canvas(width, height) {
  const result = document.createElement("canvas");
  result.width = Math.max(1, Math.round(width)); result.height = Math.max(1, Math.round(height));
  return result;
}
function checkDimensions(width, height) {
  if (!Number.isFinite(width * height) || width < 1 || height < 1 || width * height > MAX_PIXELS)
    throw new Error("Размер страницы изображения превышает 30 миллионов пикселей.");
}
function scaled(source) {
  const factor = Math.min(1, 2400 / source.width, 3200 / source.height);
  const result = canvas(source.width * factor, source.height * factor);
  const ctx = result.getContext("2d");
  ctx.fillStyle = "white"; ctx.fillRect(0, 0, result.width, result.height);
  ctx.drawImage(source, 0, 0, result.width, result.height);
  return result;
}
function enhance(source) {
  const result = canvas(source.width, source.height);
  const ctx = result.getContext("2d", {willReadFrequently: true});
  ctx.filter = "grayscale(1) blur(0.8px)";
  ctx.drawImage(source, 0, 0);
  const pixels = ctx.getImageData(0, 0, result.width, result.height);
  const counts = new Uint32Array(256);
  for (let i = 0; i < pixels.data.length; i += 4) counts[pixels.data[i]]++;
  const cutoff = result.width * result.height * .01;
  let low = 0, high = 255, count = 0;
  while (low < 255 && count + counts[low] <= cutoff) count += counts[low++];
  count = 0;
  while (high > 0 && count + counts[high] <= cutoff) count += counts[high--];
  if (high > low) for (let i = 0; i < pixels.data.length; i += 4) {
    const value = Math.max(0, Math.min(255, (pixels.data[i] - low) * 255 / (high - low)));
    pixels.data[i] = pixels.data[i + 1] = pixels.data[i + 2] = value;
  }
  ctx.putImageData(pixels, 0, 0);
  return result;
}
function preview(source) {
  const scale = Math.min(1, 1000 / source.width, 1400 / source.height);
  const small = canvas(source.width * scale, source.height * scale);
  small.getContext("2d").drawImage(source, 0, 0, small.width, small.height);
  const url = small.toDataURL("image/jpeg", .83);
  small.width = small.height = 1;
  return url;
}

async function* readPages(file) {
  const buffer = await file.arrayBuffer(), bytes = new Uint8Array(buffer);
  if (bytes[0] === 37 && bytes[1] === 80 && bytes[2] === 68 && bytes[3] === 70 && bytes[4] === 45) {
    const pdfjs = await import("./vendor/pdfjs/pdf.mjs");
    pdfjs.GlobalWorkerOptions.workerSrc = asset("./vendor/pdfjs/pdf.worker.mjs");
    const task = pdfjs.getDocument({data: bytes, isEvalSupported: false,
      cMapUrl: asset("./vendor/cmaps/"), cMapPacked: true,
      standardFontDataUrl: asset("./vendor/standard_fonts/"), wasmUrl: asset("./vendor/pdfjs-wasm/")});
    try {
      const pdf = await task.promise;
      if (!pdf.numPages || pdf.numPages > MAX_PAGES) throw new Error("PDF должен содержать от 1 до 20 страниц.");
      for (let i = 1; i <= pdf.numPages; i++) {
        const page = await pdf.getPage(i), base = page.getViewport({scale: 1});
        if (!(base.width > 0 && base.height > 0)) throw new Error("Некорректный размер страницы PDF.");
        const viewport = page.getViewport({scale: Math.min(3, 2400 / base.width, 3200 / base.height)});
        const image = canvas(viewport.width, viewport.height);
        try {
          await page.render({canvasContext: image.getContext("2d"), viewport}).promise;
          yield {image, number: i, total: pdf.numPages};
        } finally {image.width = image.height = 1; page.cleanup();}
      }
    } finally {await task.destroy();}
  } else if ((bytes[0] === 73 && bytes[1] === 73) || (bytes[0] === 77 && bytes[1] === 77)) {
    await loadScript("./vendor/pako.js"); await loadScript("./vendor/utif.js");
    const ifds = UTIF.decode(buffer).filter((ifd) => ifd.t256 && ifd.t257);
    if (!ifds.length || ifds.length > MAX_PAGES) throw new Error("TIFF должен содержать от 1 до 20 страниц.");
    for (let i = 0; i < ifds.length; i++) {
      const ifd = ifds[i]; checkDimensions(ifd.t256[0], ifd.t257[0]);
      UTIF.decodeImage(buffer, ifd);
      const raw = canvas(ifd.width, ifd.height);
      raw.getContext("2d").putImageData(new ImageData(new Uint8ClampedArray(UTIF.toRGBA8(ifd)), ifd.width, ifd.height), 0, 0);
      const orientation = ifd.t274?.[0] || 1;
      const oriented = canvas(orientation >= 5 ? raw.height : raw.width, orientation >= 5 ? raw.width : raw.height);
      const transforms = {2: [-1,0,0,1,raw.width,0], 3: [-1,0,0,-1,raw.width,raw.height],
        4: [1,0,0,-1,0,raw.height], 5: [0,1,1,0,0,0], 6: [0,1,-1,0,raw.height,0],
        7: [0,-1,-1,0,raw.height,raw.width], 8: [0,-1,1,0,0,raw.width]};
      const ctx = oriented.getContext("2d");
      if (transforms[orientation]) ctx.setTransform(...transforms[orientation]);
      ctx.drawImage(raw, 0, 0);
      const image = scaled(oriented);
      raw.width = raw.height = oriented.width = oriented.height = 1;
      delete ifd.data;
      try {yield {image, number: i + 1, total: ifds.length};}
      finally {image.width = image.height = 1;}
    }
  } else {
    let bitmap;
    try {bitmap = await createImageBitmap(new Blob([buffer], {type: file.type}));}
    catch {throw new Error("Не удалось открыть изображение. Используйте PDF, JPG, PNG, TIFF или WEBP.");}
    let image;
    try {checkDimensions(bitmap.width, bitmap.height); image = scaled(bitmap);}
    finally {bitmap.close();}
    try {yield {image, number: 1, total: 1};}
    finally {image.width = image.height = 1;}
  }
}

export async function processDocument(file, kind, progress = () => {}) {
  if (!file.size || file.size > MAX_BYTES) throw new Error("Допустимый размер файла — от 1 байта до 20 МБ.");
  progress("Загрузка распознавания… Первый запуск может занять больше времени.");
  await loadScript("./vendor/tesseract/tesseract.min.js");
  const worker = await Tesseract.createWorker(["rus", "eng"], 1, {
    workerPath: asset("./vendor/tesseract/worker.min.js"),
    corePath: asset("./vendor/tesseract-core/"), langPath: asset("./models").replace(/\/$/, ""),
    gzip: false, cacheMethod: "none",
  });
  const pages = [];
  try {
    for await (const {image, number, total} of readPages(file)) {
      progress(`Распознаётся страница ${number} из ${total}…`);
      let enhanced;
      try {
        const blocks = await recognizePage(worker, image, image.width, image.height,
          () => (enhanced = enhance(image)));
        pages.push({number, preview: preview(image), blocks});
      } finally {if (enhanced) enhanced.width = enhanced.height = 1;}
    }
  } finally {await worker.terminate();}
  progress("Определение категории и разбор оценок…");
  const result = await interpretDocument({pages, type: kind});
  result.engine = "Tesseract.js · браузер";
  return result;
}
