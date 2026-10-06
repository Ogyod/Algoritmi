import test from "node:test";
import assert from "node:assert/strict";
import {headingPresent, recognizePage} from "../static/ocr-engine.js";
function data(texts) {
  return {text: texts.join("\n"), blocks: [{paragraphs: [{lines: texts.map((text, i) => ({
    text, confidence: 90, bbox: {x0: 10, y0: i * 30, x1: 300, y1: i * 30 + 20},
  }))}]}]};
}
function worker(...passes) {
  return {calls: 0, async setParameters() {}, async recognize() {
    return {data: passes[this.calls++]};
  }};
}
test("heading recognises inflection and a common OCR error", () => {
  assert.ok(headingPresent("К АТГЕСТАТУ ОБ ОСНОВНОМ ОБЩЕМ"));
  assert.ok(headingPresent("Диплом бакалавра"));
  assert.ok(!headingPresent("Наименование учебных предметов"));
});
test("sparse noisy title gets a bounded second pass", async () => {
  const w = worker(data(["00000000123456", "АБ ? =", ...Array(20).fill("< a. = >")]),
    data(["К АТГЕСТАТУ ОБ ОСНОВНОМ ОБЩЕМ", "ОБРАЗОВАНИИ", "Музыка 5", "99999999999999"]));
  const result = await recognizePage(w, "original", 1000, 1400, () => "enhanced");
  assert.equal(w.calls, 2);
  assert.ok(result.some((r) => r.text.includes("ОСНОВНОМ")));
  assert.ok(!result.some((r) => r.text === "Музыка 5" || r.text === "99999999999999"));
});
test("an already readable title skips extra OCR", async () => {
  const w = worker(data(["Аттестат об основном общем образовании"]));
  await recognizePage(w, "original", 1000, 1400, () => "enhanced");
  assert.equal(w.calls, 1);
});
test("a populated table keeps its original rows", async () => {
  const w = worker(data(Array.from({length: 17}, (_, i) => `Предмет ${i} 5`)));
  const rows = await recognizePage(w, "original", 1000, 1400, () => "enhanced");
  assert.equal(w.calls, 1); assert.equal(rows.length, 17);
});
test("bounding boxes are normalized and blank lines removed", async () => {
  const w = worker(data(["Диплом магистра", " "]));
  const rows = await recognizePage(w, "original", 1000, 1400);
  assert.equal(rows.length, 1); assert.equal(rows[0].x, .01);
  assert.equal(rows[0].confidence, .9);
});
