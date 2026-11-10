export const browserMode = document.documentElement.dataset.processing === "browser";
let browserProcessor;
async function browser() {
  return browserProcessor ||= import("./browser-processor.js");
}

async function requestJSON(path, body) {
  const response = await fetch(path, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body), signal: AbortSignal.timeout(1800000),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Ошибка обработки");
  return result;
}

function base64File(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",")[1]);
    reader.onerror = () => reject(new Error("Не удалось прочитать файл."));
    reader.readAsDataURL(file);
  });
}

export async function processDocument(file, kind, progress) {
  return processDocumentGroup([file], kind, progress);
}

export async function processDocumentGroup(files, kind, progress) {
  if (browserMode) return (await browser()).processDocumentGroup(files, kind, progress);
  progress?.("PaddleOCR и Tesseract читают документ; EasyOCR проверит расхождения.");
  if (files.length === 1) return requestJSON("/api/process", {data: await base64File(files[0]), type: kind});
  const payload = [];
  for (const file of files) payload.push({name: file.name, data: await base64File(file)});
  return requestJSON("/api/process-group", {files: payload, type: kind});
}

export async function readArchive(file) {
  if (!file.size || file.size > 50 * 1024 * 1024) throw new Error("ZIP должен быть непустым и не превышать 50 МБ.");
  let result;
  if (browserMode) {
    result = await (await browser()).interpretDocument({action: "unpack_archive", data: await base64File(file), filename: file.name});
  } else {
    const body = new FormData(); body.append("file", file);
    const response = await fetch("/api/archive", {method: "POST", body, signal: AbortSignal.timeout(1800000)});
    result = await response.json();
    if (!response.ok) throw new Error(result.error || "Не удалось открыть ZIP.");
  }
  if (result.archive_error) throw new Error(result.archive_error);
  const documents = result.documents.map(group => ({name: group.document_key,
    files: group.files.map(item => {
      const bytes = Uint8Array.from(atob(item.data), c => c.charCodeAt(0));
      return new File([bytes], item.name, {type: "application/octet-stream"});
    })}));
  return {documents, skipped_files: result.skipped_files};
}

export async function reinterpretDocument(lines, kind) {
  return browserMode
    ? (await browser()).interpretDocument({lines, type: kind})
    : requestJSON("/api/reinterpret", {lines, type: kind});
}

export function downloadPacket(content, format, demo) {
  const url = URL.createObjectURL(new Blob([content], {
    type: format === "json" ? "application/json;charset=utf-8" : "text/csv;charset=utf-8",
  }));
  const link = document.createElement("a");
  link.href = url;
  link.download = (demo ? "demo-result" : "admissions-result") + "." + format;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
