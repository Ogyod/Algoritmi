export const browserMode = document.documentElement.dataset.processing === "browser";
let browserProcessor;
async function browser() {
  return browserProcessor ||= import("./browser-processor.js");
}

async function requestJSON(path, body) {
  const response = await fetch(path, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body), signal: AbortSignal.timeout(200000),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Ошибка обработки");
  return result;
}

export async function processDocument(file, kind, progress) {
  if (browserMode) return (await browser()).processDocument(file, kind, progress);
  const data = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",")[1]);
    reader.onerror = () => reject(new Error("Не удалось прочитать файл."));
    reader.readAsDataURL(file);
  });
  return requestJSON("/api/process", {data, type: kind});
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
