import {
  DOCUMENT_TYPES,
  TYPE_LABELS,
  isCertificate,
  isEducation,
  meanGrade,
  buildPacket,
  packetCSV,
  reviewError,
} from "./domain.js";
import {browserMode, processDocument, reinterpretDocument, downloadPacket} from "./processor.js";

const $ = (id) => document.getElementById(id);
const state = {
  files: [],
  selected: null,
  page: 0,
  tab: "data",
  busy: false,
  demo: false,
};
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char],
  );
const selected = () => state.files.find((file) => file.id === state.selected);
const packet = () => buildPacket(state.files, $("applicant-id").value);
const format = (value) =>
  value == null ? "—" : value.toFixed(2).replace(".", ",");
let toastTimer;

function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 5000);
}

function clearFiles() {
  for (const file of state.files) if (file.url) URL.revokeObjectURL(file.url);
  state.files = [];
  state.selected = null;
}

function addFiles(files) {
  if (state.busy) return;
  if (state.demo) {
    clearFiles();
    state.demo = false;
  }
  for (const file of files) {
    if (state.files.length >= 12) {
      toast("В одном пакете может быть до 12 файлов.");
      break;
    }
    if (!/\.(pdf|jpe?g|png|tiff?|webp)$/i.test(file.name)) {
      toast(`Формат файла «${file.name}» не поддерживается.`);
      continue;
    }
    if (!file.size || file.size > 20 * 1024 * 1024) {
      toast(`Файл «${file.name}» должен быть меньше 20 МБ и не пустым.`);
      continue;
    }
    const entry = {
      id: crypto.randomUUID(),
      name: file.name,
      file,
      url: URL.createObjectURL(file),
      kind: "auto",
      status: "ready",
      result: null,
    };
    state.files.push(entry);
    if (!state.selected) state.selected = entry.id;
  }
  state.page = 0;
  render();
}

function renderQueue() {
  $("file-count").textContent = state.files.length;
  $("queue").innerHTML = state.files
    .map((file) => {
      const status =
        file.status === "processing"
          ? file.progress || "Распознаём…"
          : file.status === "done"
            ? file.result.reviewed
              ? "Проверен"
              : "Нужна проверка"
            : file.status === "error"
              ? file.error
              : "Готов к обработке";
      return `<div class="queue-item ${file.id === state.selected ? "selected" : ""}">
      <button class="queue-title" data-select="${file.id}">${escapeHTML(file.name)}</button>
      <button class="remove" data-remove="${file.id}" aria-label="Удалить ${escapeHTML(file.name)}" ${state.busy ? "disabled" : ""}>×</button>
      <div class="queue-meta">${file.file ? (file.file.size / 1024 / 1024).toFixed(2) + " МБ" : "Демонстрационные данные"}</div>
      <select aria-label="Категория ${escapeHTML(file.name)}" data-kind="${file.id}" ${state.busy ? "disabled" : ""}>
        ${Object.entries({ auto: TYPE_LABELS.auto, ...DOCUMENT_TYPES })
          .map(
            ([key, label]) =>
              `<option value="${key}" ${file.kind === key ? "selected" : ""}>${label}</option>`,
          )
          .join("")}
      </select><p class="queue-status ${file.status === "error" ? "error" : ""}">${escapeHTML(status)}</p></div>`;
    })
    .join("");
  $("process").disabled =
    state.busy ||
    state.demo ||
    !state.files.some((file) => file.status !== "done");
  $("process").textContent = state.busy
    ? "Идёт распознавание…"
    : "Распознать документы";
  $("choose").disabled = state.busy;
  $("demo").disabled = state.busy;
}

function renderPreview() {
  const file = selected();
  $("scan-type").textContent = file
    ? TYPE_LABELS[file.result?.type || file.kind]
    : "Нет файла";
  $("scan-name").textContent = file ? file.name : "Предпросмотр скана";
  $("open-original").disabled = !file?.url;
  const pages = file?.result?.pages || [];
  state.page = Math.max(0, Math.min(state.page, Math.max(0, pages.length - 1)));
  $("prev-page").disabled = state.page === 0;
  $("next-page").disabled = state.page >= pages.length - 1;
  $("page-count").textContent = pages.length
    ? `${state.page + 1} / ${pages.length}`
    : "—";
  if (pages.length)
    $("preview").innerHTML =
      `<img src="${escapeHTML(pages[state.page].preview)}" alt="Скан, страница ${state.page + 1}">`;
  else if (file?.url && /\.pdf$/i.test(file.name))
    $("preview").innerHTML =
      `<iframe src="${escapeHTML(file.url)}" title="Исходный PDF"></iframe>`;
  else if (file?.url && !/\.tiff?$/i.test(file.name))
    $("preview").innerHTML =
      `<img src="${escapeHTML(file.url)}" alt="Исходный скан">`;
  else
    $("preview").innerHTML =
      `<div class="empty-state"><h3>${file ? "Скан появится после распознавания" : "Ваш документ появится здесь"}</h3><p>${state.demo ? "Это пример с заранее заданными данными." : "Загрузите файл и выберите его из пакета."}</p></div>`;
  $("scan-hint").textContent = state.demo
    ? "Пример · не результат распознавания"
    : "Оригинал всегда рядом с данными";
}

function educationalFields(result) {
  const candidates = result.identifier_candidates || [];
  return `<div class="identifier-fields">
    <label for="document-series" class="result-label">Серия документа</label>
    <input id="document-series" maxlength="20" value="${escapeHTML(result.document_series)}" placeholder="Например, АБ или 107724" ${result.series_not_present ? "disabled" : ""}>
    <label class="checkbox-label"><input id="series-not-present" type="checkbox" ${result.series_not_present ? "checked" : ""}> Серия на документе не указана</label>
    <label for="document-number" class="result-label">Номер документа</label>
    <input id="document-number" inputmode="numeric" maxlength="20" value="${escapeHTML(result.document_number)}" placeholder="Начальные нули сохраняются">
    ${candidates.length ? `<details class="identifier-evidence"><summary>Где найдены реквизиты</summary>${candidates.map((item) => `<p>Страница ${item.page}: ${escapeHTML(item.source)}</p>`).join("")}</details>` : ""}
  </div>`;
}

function gradeTable(result) {
  const categoryPending = result.type === "education_unknown";
  const sum = result.grades.reduce(
    (n, row) => n + (Number.isFinite(row.grade) ? row.grade : 0),
    0,
  );
  return `<div class="average-card"><div><span>${categoryPending ? "Найденные оценки" : "Средний балл этого аттестата"}</span><small id="grade-formula">${categoryPending ? "Выберите категорию аттестата для расчёта среднего балла" : result.grades.length ? `${sum} ÷ ${result.grades.length} предметов` : "Нужно приложение с оценками"}</small></div><strong id="doc-average">${format(categoryPending ? null : meanGrade(result.grades))}</strong></div>
    <table class="grade-table"><thead><tr><th>Предмет</th><th>Оценка</th><th></th></tr></thead><tbody>
    ${result.grades
      .map(
        (
          row,
          index,
        ) => `<tr class="${row.conflict || row.confidence < 0.8 ? "low-confidence" : ""}" title="${escapeHTML(row.source || "Добавлено вручную")}">
      <td><input aria-label="Предмет ${index + 1}" value="${escapeHTML(row.subject)}" data-subject="${index}"></td>
      <td><input aria-label="Оценка ${index + 1}" type="number" min="2" max="5" step="1" value="${row.grade ?? ""}" data-grade="${index}"></td>
      <td><button class="delete-grade" data-delete-grade="${index}" aria-label="Удалить предмет ${index + 1}">×</button></td></tr>`,
      )
      .join("")}
    </tbody></table><button id="add-grade" class="text-button add-grade">+ Добавить предмет</button>`;
}

function renderResults() {
  const file = selected(),
    result = file?.result;
  $("result-content").inert = state.busy;
  for (const name of ["data", "text"]) {
    $("tab-" + name).classList.toggle("active", state.tab === name);
    $("tab-" + name).setAttribute("aria-selected", String(state.tab === name));
  }
  $("review-actions").hidden =
    !result || !Object.hasOwn(DOCUMENT_TYPES, result.type);
  $("confirm").textContent = result?.reviewed
    ? "Проверка подтверждена"
    : "Подтвердить проверку документа";
  $("confirm").disabled = !!result?.reviewed || state.busy;
  $("review-state").textContent = result
    ? result.reviewed
      ? "Проверен"
      : "Нужна проверка"
    : file?.status === "processing"
      ? "Распознаём"
      : "Ожидает загрузки";
  $("review-state").className =
    "badge " + (result ? (result.reviewed ? "success" : "warning") : "neutral");
  if (!result) {
    $("result-content").innerHTML =
      file?.status === "processing"
        ? '<div class="progress">Распознаём текст и разбираем документ.<br>Многостраничный PDF может занять несколько минут.</div>'
        : file?.status === "error"
          ? `<div class="notice">${escapeHTML(file.error)}</div>`
          : '<div class="result-empty"><h3>Сначала загрузите сканы</h3><p>Здесь будут категория, серия и номер документа; для аттестатов — оценки и средний балл.</p><div class="supported"><span>9 классов</span><span>11 классов</span><span>Дипломы</span></div></div>';
    return;
  }
  const demo = state.demo
    ? '<div class="demo-label">Демонстрационные данные. Это не результат OCR.</div>'
    : "";
  if (state.tab === "text") {
    $("result-content").innerHTML =
      demo +
      `<div class="raw-text">${escapeHTML(result.text) || "Текст не найден."}</div>`;
    return;
  }
  let html =
    demo +
    `<div class="document-category">${escapeHTML(TYPE_LABELS[result.type])}</div>`;
  html += result.warnings
    .map((message) => `<div class="notice">${escapeHTML(message)}</div>`)
    .join("");
  if (isEducation(result.type) || result.type === "education_unknown") html += educationalFields(result);
  if (isCertificate(result.type) || (result.type === "education_unknown" && result.grades.length)) html += gradeTable(result);
  if (result.type === "achievement")
    html += `<label for="achievement" class="result-label">Подтверждённое достижение</label><textarea id="achievement" rows="4">${escapeHTML(result.achievement)}</textarea><label for="points" class="result-label">Баллы по правилам приёма</label><input id="points" type="number" min="0" step="0.01" placeholder="Не начислены" value="${result.points ?? ""}">`;
  $("result-content").innerHTML = html;
}

function renderSummary() {
  const result = packet(),
    done = state.files.filter((file) => file.result).length;
  $("summary-average").textContent = format(result.average_grade);
  $("summary-points").textContent =
    result.achievement_points == null
      ? "—"
      : String(result.achievement_points).replace(".", ",");
  $("summary-hint").textContent = !done
    ? "Результаты появятся после распознавания документов."
    : result.warnings.length
      ? result.warnings.join(" ")
      : `${result.documents.filter((doc) => doc.reviewed).length} из ${state.files.length} документов проверено. ${result.review_status === "reviewed" ? "Пакет проверен." : "Выгрузка будет помечена «нужна проверка»."}`;
  $("export-json").disabled = !done || state.busy;
  $("export-csv").disabled = !done || state.busy;
}

function render() {
  renderQueue();
  renderPreview();
  renderResults();
  renderSummary();
}

function edit() {
  const result = selected()?.result;
  if (!result) return;
  result.reviewed = false;
  renderQueue();
  renderSummary();
  $("review-state").textContent = "Нужна проверка";
  $("review-state").className = "badge warning";
  $("confirm").disabled = false;
  $("confirm").textContent = "Подтвердить проверку документа";
  if (isCertificate(result.type) && $("doc-average")) {
    $("doc-average").textContent = format(meanGrade(result.grades));
    const sum = result.grades.reduce(
      (n, row) => n + (Number.isFinite(row.grade) ? row.grade : 0),
      0,
    );
    $("grade-formula").textContent = result.grades.length
      ? `${sum} ÷ ${result.grades.length} предметов`
      : "Добавьте оценки";
  }
}

async function processFiles() {
  if (state.busy || state.demo) return;
  state.busy = true;
  const pending = state.files.filter((file) => file.status !== "done");
  try {
    for (const file of pending) {
      state.selected = file.id;
      state.page = 0;
      file.status = "processing";
      render();
      try {
        file.result = await processDocument(file.file, file.kind, (message) => {
          file.progress = message;
          renderQueue();
        });
        file.status = "done";
        if (Object.hasOwn(DOCUMENT_TYPES, file.result.type))
          file.kind = file.result.type;
        delete file.error;
      } catch (error) {
        file.status = "error";
        file.error = error.message;
      }
      render();
    }
  } finally {
    state.busy = false;
    render();
  }
  toast(
    pending.some((file) => file.status === "error")
      ? "Часть файлов не обработана. Проверьте сообщения в пакете."
      : "Распознавание завершено. Сверьте данные со сканами.",
  );
}

async function changeCategory(file, kind) {
  if (state.busy) return;
  file.kind = kind;
  if (!file.result) {
    render();
    return;
  }
  if (state.demo) {
    file.result.type = kind === "auto" ? file.result.detected_type : kind;
    file.result.reviewed = false;
    render();
    return;
  }
  state.busy = true;
  render();
  try {
    const previous = file.result;
    const result = await reinterpretDocument(previous.ocr_lines, kind);
    // Reuse OCR text and previews; do not recognize the same scan again.
    result.pages = previous.pages;
    result.engine = previous.engine;
    if ((isEducation(previous.type) || previous.type === "education_unknown") && isEducation(result.type)) {
      for (const field of [
        "document_series",
        "document_number",
        "series_not_present",
      ])
        result[field] = previous[field];
    }
    if ((isCertificate(previous.type) || previous.type === "education_unknown") && isCertificate(result.type))
      result.grades = previous.grades;
    if (previous.type === "achievement" && result.type === "achievement") {
      result.achievement = previous.achievement;
      result.points = previous.points;
    }
    file.result = result;
    if (Object.hasOwn(DOCUMENT_TYPES, result.type)) file.kind = result.type;
  } catch (error) {
    toast(error.message);
    file.kind = file.result.type;
  } finally {
    state.busy = false;
    render();
  }
}

async function exportPacket(formatType) {
  const result = {
    ...packet(),
    exported_at: new Date().toISOString(),
    demo: state.demo,
  };
  try {
    downloadPacket(formatType === "json" ? JSON.stringify(result, null, 2) : packetCSV(result),
                   formatType, state.demo);
  } catch (error) {
    toast(error.message);
  }
}

// Events keep UI actions separate from recognition and pure calculations.
$("choose").onclick = () => $("files").click();
$("files").onchange = (event) => {
  addFiles(event.target.files);
  event.target.value = "";
};
for (const name of ["dragenter", "dragover"])
  $("dropzone").addEventListener(name, (event) => {
    event.preventDefault();
    $("dropzone").classList.add("dragover");
  });
for (const name of ["dragleave", "drop"])
  $("dropzone").addEventListener(name, (event) => {
    event.preventDefault();
    $("dropzone").classList.remove("dragover");
    if (name === "drop") addFiles(event.dataTransfer.files);
  });

$("queue").onclick = (event) => {
  const select = event.target.closest("[data-select]"),
    remove = event.target.closest("[data-remove]");
  if (select) {
    state.selected = select.dataset.select;
    state.page = 0;
    render();
  }
  if (remove && !state.busy) {
    const file = state.files.find((item) => item.id === remove.dataset.remove);
    if (file.url) URL.revokeObjectURL(file.url);
    state.files = state.files.filter((item) => item !== file);
    if (state.selected === file.id) state.selected = state.files[0]?.id || null;
    state.page = 0;
    render();
  }
};
$("queue").onchange = (event) => {
  if (!event.target.dataset.kind) return;
  const file = state.files.find(
    (item) => item.id === event.target.dataset.kind,
  );
  changeCategory(file, event.target.value);
};

$("result-content").oninput = (event) => {
  const result = selected()?.result,
    target = event.target;
  if (!result || state.busy) return;
  if (target.dataset.grade != null) {
    result.grades[+target.dataset.grade].grade =
      target.value === "" ? null : Number(target.value);
    result.grades[+target.dataset.grade].conflict = false;
  } else if (target.dataset.subject != null)
    result.grades[+target.dataset.subject].subject = target.value;
  else if (target.id === "document-series")
    result.document_series = target.value;
  else if (target.id === "document-number")
    result.document_number = target.value.replace(/\s+/g, "");
  else if (target.id === "achievement") result.achievement = target.value;
  else if (target.id === "points")
    result.points = target.value === "" ? null : Number(target.value);
  else return;
  edit();
};
$("result-content").onchange = (event) => {
  if (event.target.id !== "series-not-present" || state.busy) return;
  const result = selected().result;
  result.series_not_present = event.target.checked;
  if (result.series_not_present) result.document_series = "";
  edit();
  renderResults();
};
$("result-content").onclick = (event) => {
  const result = selected()?.result;
  if (!result || state.busy) return;
  if (event.target.id === "add-grade")
    result.grades.push({
      subject: "",
      grade: null,
      confidence: 1,
      source: "Добавлено вручную",
      conflict: false,
    });
  else if (event.target.dataset.deleteGrade != null)
    result.grades.splice(+event.target.dataset.deleteGrade, 1);
  else return;
  result.reviewed = false;
  render();
};

$("tab-data").onclick = () => {
  state.tab = "data";
  renderResults();
};
$("tab-text").onclick = () => {
  state.tab = "text";
  renderResults();
};
$("confirm").onclick = () => {
  const result = selected()?.result,
    error = reviewError(result);
  if (error) {
    toast(error);
    return;
  }
  result.reviewed = true;
  render();
};
$("prev-page").onclick = () => {
  state.page--;
  renderPreview();
};
$("next-page").onclick = () => {
  state.page++;
  renderPreview();
};
$("open-original").onclick = () => {
  const file = selected();
  if (file?.url) window.open(file.url, "_blank", "noopener");
};
$("process").onclick = processFiles;
$("applicant-id").oninput = renderSummary;
$("export-json").onclick = () => exportPacket("json");
$("export-csv").onclick = () => exportPacket("csv");

$("demo").onclick = () => {
  if (state.busy) return;
  clearFiles();
  state.demo = true;
  state.tab = "data";
  state.page = 0;
  const grades = [
    ["Русский язык", 5],
    ["Литература", 4],
    ["Алгебра", 5],
    ["Геометрия", 4],
    ["История", 5],
    ["Биология", 4],
  ].map(([subject, grade]) => ({
    subject,
    grade,
    confidence: 1,
    source: "Демонстрационный пример",
    conflict: false,
  }));
  const base = {
    grades: [],
    achievement: "",
    points: null,
    warnings: [],
    pages: [],
    ocr_lines: [],
    identifier_candidates: [],
    reviewed: false,
    series_not_present: false,
  };
  state.files = [
    {
      id: crypto.randomUUID(),
      name: "Пример · аттестат за 9 классов",
      kind: "certificate_9",
      status: "done",
      result: {
        ...structuredClone(base),
        type: "certificate_9",
        detected_type: "certificate_9",
        document_series: "АБ",
        document_number: "0012345",
        grades,
        text:
          "ДЕМОНСТРАЦИОННЫЙ ПРИМЕР\nАттестат об основном общем образовании\nСерия АБ Номер 0012345\n" +
          grades.map((row) => `${row.subject} ${row.grade}`).join("\n"),
      },
    },
    {
      id: crypto.randomUUID(),
      name: "Пример · диплом бакалавра",
      kind: "diploma_bachelor",
      status: "done",
      result: {
        ...structuredClone(base),
        type: "diploma_bachelor",
        detected_type: "diploma_bachelor",
        document_series: "107724",
        document_number: "0001234",
        text: "ДЕМОНСТРАЦИОННЫЙ ПРИМЕР\nДиплом бакалавра\nСерия 107724\nНомер 0001234",
      },
    },
  ];
  state.selected = state.files[0].id;
  render();
  toast(
    "Это заранее заданный пример. Загрузите сканы для настоящего распознавания.",
  );
};

if (browserMode) $("engine").textContent = "Распознавание в браузере";
else fetch("/api/health")
  .then((response) => response.json())
  .then((result) => {
    $("engine").textContent = result.engine
      ? "Распознавание готово"
      : "OCR требует настройки";
  })
  .catch(() => ($("engine").textContent = "Нет связи с обработчиком"));
render();
