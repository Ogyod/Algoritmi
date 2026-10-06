import test from "node:test";
import assert from "node:assert/strict";
import {
  meanGrade,
  reviewError,
  buildPacket,
  packetCSV,
} from "../static/domain.js";

function document(type = "certificate_9", overrides = {}) {
  return {
    type,
    reviewed: true,
    document_series: "АБ",
    document_number: "0012345",
    series_not_present: false,
    identifier_candidates: [],
    grades: [{ subject: "Алгебра", grade: 5 }],
    achievement: "",
    points: null,
    text: "Учебный образец",
    warnings: [],
    ...overrides,
  };
}
const file = (result, name = "scan.png") => ({ name, status: "done", result });

test("mean and rounding", () => {
  assert.equal(
    meanGrade([
      { subject: "Алгебра", grade: 5 },
      { subject: "История", grade: 4 },
      { subject: "Физика", grade: 5 },
    ]),
    4.67,
  );
  assert.equal(meanGrade([]), null);
  assert.equal(meanGrade([{ subject: "Алгебра", grade: 5.5 }]), null);
});
test("duplicate subjects ignore case, spacing and ё", () => {
  assert.equal(
    meanGrade([
      { subject: " Русский  язык ", grade: 5 },
      { subject: "РУССКИЙ ЯЗЫК", grade: 4 },
    ]),
    null,
  );
});
test("different certificates never get mixed", () => {
  const packet = buildPacket(
    [
      file(document()),
      file(
        document("certificate_11", {
          grades: [{ subject: "Алгебра", grade: 3 }],
        }),
        "second.png",
      ),
    ],
    "4515",
  );
  assert.equal(packet.average_grade, null);
  assert.equal(packet.documents[0].average_grade, 5);
  assert.equal(packet.documents[1].average_grade, 3);
  assert.equal(packet.review_status, "reviewed");
});
test("numbers remain strings and preserve leading zeros", () => {
  assert.equal(
    buildPacket([file(document())], "").documents[0].document_number,
    "0012345",
  );
});
test("series absence must be explicit", () => {
  assert.match(
    reviewError(document("certificate_9", { document_series: "" })),
    /сери/,
  );
  assert.equal(
    reviewError(
      document("certificate_9", {
        document_series: "",
        series_not_present: true,
      }),
    ),
    null,
  );
});
test("title without grades can be reviewed, but has no mean", () => {
  assert.equal(reviewError(document("certificate_9", { grades: [] })), null);
  assert.equal(
    buildPacket([file(document("certificate_9", { grades: [] }))], "")
      .average_grade,
    null,
  );
});
test("invalid numbers and contradictory series flags cannot be reviewed", () => {
  assert.match(
    reviewError(document("diploma_master", { document_number: "2024-01-01" })),
    /номер/,
  );
  assert.ok(
    reviewError(document("diploma_master", { series_not_present: true })),
  );
});
test("a stale reviewed flag does not certify invalid edits", () => {
  const packet = buildPacket(
    [file(document("diploma_bachelor", { document_number: "" }))],
    "4515",
  );
  assert.equal(packet.review_status, "needs_review");
  assert.equal(packet.documents[0].reviewed, false);
});
test("diploma grades are not treated as certificate grades", () => {
  const packet = buildPacket([file(document("diploma_spo"))], "");
  assert.equal(packet.average_grade, null);
  assert.deepEqual(packet.documents[0].grades, []);
});
test("achievements require finite nonnegative points without an invented cap", () => {
  assert.equal(
    reviewError(
      document("achievement", { achievement: "Учебный пример", points: 150 }),
    ),
    null,
  );
  assert.ok(
    reviewError(
      document("achievement", { achievement: "Учебный пример", points: NaN }),
    ),
  );
  assert.ok(
    reviewError(
      document("achievement", { achievement: "Учебный пример", points: -1 }),
    ),
  );
});
test("one failed file makes the packet incomplete", () => {
  const packet = buildPacket(
    [file(document()), { name: "bad.pdf", status: "error", error: "Ошибка" }],
    "",
  );
  assert.equal(packet.review_status, "needs_review");
});
test("CSV escapes formulas and quotes, and has the new metadata columns", () => {
  const packet = buildPacket(
    [
      file(
        document("certificate_9", { grades: [{ subject: "=2+2", grade: 5 }] }),
        "=example.png",
      ),
    ],
    "4515",
  );
  const csv = packetCSV(packet);
  assert.ok(csv.startsWith("\uFEFF"));
  assert.match(csv, /"Серия";"Номер"/);
  assert.match(csv, /"'=example.png"/);
  assert.match(csv, /"'=2\+2"/);
  assert.doesNotMatch(csv, /Адрес/);
});

test('a conflicting OCR grade cannot be reviewed until corrected', () => {
  assert.equal(meanGrade([{subject: 'Музыка', grade: 5, conflict: true}]), null);
  assert.equal(meanGrade([{subject: 'Музыка', grade: 5, conflict: false}]), 5);
});
