// Pure functions: no DOM, files, network, or OCR. Tested independently from the UI.
export const DOCUMENT_TYPES = {
  certificate_9: "Аттестат · 9 классов (основное общее)",
  certificate_11: "Аттестат · 11 классов (среднее общее)",
  diploma_spo: "Диплом · СПО",
  diploma_bachelor: "Диплом · бакалавриат",
  diploma_specialist: "Диплом · специалитет",
  diploma_master: "Диплом · магистратура",
  diploma_postgraduate: "Диплом · аспирантура",
  achievement: "Дополнительное достижение",
};
export const TYPE_LABELS = {
  auto: "Определить автоматически",
  ...DOCUMENT_TYPES,
  education_unknown: "Образование · выберите категорию",
  unknown: "Категория не определена",
};

export const isCertificate = (type) =>
  ["certificate_9", "certificate_11"].includes(type);
export const isEducation = (type) =>
  type === "education_unknown" || (Object.hasOwn(DOCUMENT_TYPES, type) && type !== "achievement");
export const hasGradeTable = (type) => isCertificate(type) || type === "education_unknown";
export const normalize = (value) =>
  value.trim().toLowerCase().replaceAll("ё", "е").replace(/\s+/g, " ");

export function duplicateSubjects(grades) {
  const seen = new Set();
  const duplicates = new Set();
  for (const row of grades) {
    const key = normalize(row.subject);
    if (seen.has(key)) duplicates.add(row.subject.trim());
    seen.add(key);
  }
  return [...duplicates];
}

export function meanGrade(grades) {
  if (!grades.length || duplicateSubjects(grades).length) return null;
  if (
    grades.some(
      (row) =>
        (row.verification === "manual_required" && !row.verified) ||
        row.conflict ||
        !row.subject.trim() ||
        !Number.isInteger(row.grade) ||
        row.grade < 2 ||
        row.grade > 5,
    )
  )
    return null;
  // Integer sum, deterministic rounding to two decimal places.
  const sum = grades.reduce((total, row) => total + row.grade, 0);
  return Math.floor((sum * 100 + grades.length / 2) / grades.length) / 100;
}

export function resultAverage(result) {
  return result.grades_need_review && !result.reviewed ? null : meanGrade(result.grades);
}

export function invalidateGradeReview(result, index) {
  result.auto_document_verified = false;
  result.reviewed = false;
  result.auto_grades_verified = false;
  result.grades_need_review = true;
  result.average = null;
  if (index != null && result.grades[index]) {
    Object.assign(result.grades[index], {auto_verified: false, verified: false, verification: "manual_required"});
  }
}

export function confirmReview(result) {
  if (!result) return reviewError(result);
  const candidate = {...result, grades: (result.grades || []).map(row => ({...row, verified: true,
    verification: row.auto_verified ? (row.verification || "ocr_high_confidence") : "manual"}))};
  const error = reviewError(candidate);
  if (error) return error;
  result.grades = candidate.grades;
  result.reviewed = true;
  result.grades_need_review = false;
  result.average = meanGrade(result.grades);
  return null;
}

export function reviewError(result) {
  if (!result || !Object.hasOwn(DOCUMENT_TYPES, result.type))
    return "Выберите категорию документа.";
  if (isEducation(result.type)) {
    if (typeof result.document_number !== "string" ||
        (result.document_number && !/^\d{5,20}$/.test(result.document_number)))
      return "Проверьте номер: от 5 до 20 цифр. Начальные нули сохраняются.";
    if (typeof result.document_series !== "string")
      return "Проверьте серию документа.";
    if (
      result.document_series.trim() &&
      !/^[\p{L}\p{N} -]{1,20}$/u.test(result.document_series)
    )
      return "Проверьте серию: только буквы, цифры, пробелы и дефис, до 20 знаков.";
    if (result.series_not_present && result.document_series.trim())
      return "Уберите отметку об отсутствии серии или очистите поле серии.";
    if (
      isCertificate(result.type) &&
      result.grades.length &&
      meanGrade(result.grades) === null
    )
      return "Проверьте предметы, оценки от 2 до 5 и повторяющиеся строки.";
  } else {
    if (
      !result.achievement.trim() ||
      !Number.isFinite(result.points) ||
      result.points < 0
    )
      return "Укажите достижение и неотрицательное количество баллов по правилам приёма.";
  }
  return null;
}

export function acceptSingleOCR(result) {
  result.grades = result.grades.map(row => {
    const accepted = Number.isInteger(row.grade) && !row.conflict && row.confidence >= .80;
    return {...row, auto_verified: accepted, verified: false,
      verification: accepted ? "ocr_high_confidence" : "manual_required"};
  });
  const average = isCertificate(result.type) ? meanGrade(result.grades) : null;
  result.auto_grades_verified = average !== null && result.grades.every(row => row.auto_verified);
  result.average = result.auto_grades_verified ? average : null;
  const identifiersConflict = (result.identifier_candidates?.length ?? 0) > 1;
  result.auto_document_verified = result.auto_grades_verified && !identifiersConflict && !reviewError(result);
  result.reviewed = result.auto_document_verified;
  return result;
}

export function applyCategoryChange(previous, result) {
  result.pages = previous.pages;
  result.engine = previous.engine;
  if (isEducation(previous.type) && isEducation(result.type)) {
    for (const field of ["document_series", "document_number", "series_not_present"])
      result[field] = previous[field];
  }
  if (hasGradeTable(result.type)) {
    if (hasGradeTable(previous.type)) {
      for (const field of ["grades", "ocr", "docling", "auto_grades_verified", "grades_need_review"])
        result[field] = previous[field];
    } else {
      result.grades = result.grades.map(row => ({...row,
        auto_verified: false, verified: false, verification: "manual_required"}));
      invalidateGradeReview(result);
      if (result.grades.length) result.warnings.push("Сверьте найденные оценки после смены категории.");
    }
  }
  if (previous.type === "achievement" && result.type === "achievement") {
    result.achievement = previous.achievement;
    result.points = previous.points;
  }
  result.average = isCertificate(result.type) ? resultAverage(result) : null;
  return result;
}

export function buildPacket(files, applicantId) {
  const documents = files.map((file) => {
    const base = {
      file_name: file.name,
      status: file.status,
      error: file.error || null,
    };
    const r = file.result;
    if (!r) return base;
    const grades = hasGradeTable(r.type) ? r.grades : [];
    return {
      ...base,
      type: r.type,
      reviewed: !!r.reviewed && !reviewError(r),
      document_series: isEducation(r.type) ? r.document_series : null,
      document_number: isEducation(r.type) ? r.document_number : null,
      series_not_present: isEducation(r.type) ? !!r.series_not_present : null,
      identifier_candidates: isEducation(r.type) ? r.identifier_candidates : [],
      grades,
      ocr: r.ocr || null,
      docling: r.docling || null,
      grades_verification: r.auto_grades_verified ? "automatic" : r.reviewed ? "manual" : "needs_review",
      average_grade: isCertificate(r.type) ? resultAverage(r) : null,
      achievement: r.type === "achievement" ? r.achievement : null,
      points: r.type === "achievement" ? r.points : null,
      recognized_text: r.text,
      warnings: r.warnings,
    };
  });
  const certificates = documents.filter(
    (d) => isCertificate(d.type) && d.grades.length,
  );
  const achievements = documents.filter((d) => d.type === "achievement");
  const warnings = [];
  for (const doc of certificates) {
    for (const subject of duplicateSubjects(doc.grades))
      warnings.push(`${doc.file_name}: повторяется предмет «${subject}».`);
  }
  if (certificates.length > 1)
    warnings.push(
      "В пакете несколько аттестатов с оценками. Средний балл сохраняется отдельно для каждого документа.",
    );
  const complete =
    documents.length > 0 &&
    documents.every((d) => d.status === "done" && d.reviewed);
  const pointsKnown =
    achievements.length > 0 &&
    achievements.every((d) => Number.isFinite(d.points) && d.points >= 0);
  return {
    schema_version: "2.0",
    applicant_id: applicantId.trim() || null,
    review_status: complete ? "reviewed" : "needs_review",
    average_grade:
      certificates.length === 1 ? certificates[0].average_grade : null,
    grade_count:
      certificates.length === 1 ? certificates[0].grades.length : null,
    achievement_points: pointsKnown
      ? Math.round(achievements.reduce((n, d) => n + d.points, 0) * 100) / 100
      : null,
    warnings,
    documents,
  };
}

export function packetCSV(packet) {
  const cell = (value) => {
    let text = String(value ?? "");
    // A value from OCR must remain text when opened in a spreadsheet.
    if (/^[\s]*[=+\-@]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  const rows = [
    [
      "ID абитуриента",
      "Статус пакета",
      "Файл",
      "Категория",
      "Серия",
      "Номер",
      "Серия не указана",
      "Средний балл",
      "Предмет",
      "Оценка",
      "Достижение",
      "Баллы",
      "Проверен",
    ],
  ];
  for (const doc of packet.documents) {
    const base = [
      packet.applicant_id,
      packet.review_status,
      doc.file_name,
      TYPE_LABELS[doc.type] || "",
      doc.document_series,
      doc.document_number,
      doc.series_not_present === true
        ? "Да"
        : doc.series_not_present === false
          ? "Нет"
          : "",
      doc.average_grade,
    ];
    const gradeRows = doc.grades?.length
      ? doc.grades
      : [{ subject: "", grade: "" }];
    for (const row of gradeRows)
      rows.push([
        ...base,
        row.subject,
        row.grade,
        doc.achievement,
        doc.points,
        doc.reviewed ? "Да" : "Нет",
      ]);
  }
  return "\uFEFF" + rows.map((row) => row.map(cell).join(";")).join("\r\n");
}
