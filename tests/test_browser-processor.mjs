import assert from "node:assert/strict";
import test from "node:test";
import {createDocumentProcessor} from "../static/browser-processor.js";

test("a batch reuses OCR, restarts it after a failure and releases it when done", async (t) => {
  const workers = [];
  let parserStarted = false;
  const context = {fillRect() {}, drawImage() {}};
  const globals = {
    document: {
      createElement: (name) => name === "canvas"
        ? {getContext: () => context, toDataURL: () => "preview"}
        : {},
      head: {appendChild: (script) => script.onload()},
    },
    createImageBitmap: async () => ({width: 1000, height: 1400, close() {}}),
    Worker: class {
      constructor() {parserStarted = true;}
      postMessage({id, payload}) {
        this.onmessage({data: {id, result: {pages: payload.pages, grades: [], warnings: []}}});
      }
    },
    Tesseract: {async createWorker() {
      assert.ok(parserStarted);
      const worker = {
        terminations: 0,
        async setParameters() {},
        async recognize() {
          if (this.fail) throw new Error("OCR stopped");
          return {data: {text: "Аттестат об основном общем образовании"}};
        },
        async terminate() {this.terminations++;},
      };
      workers.push(worker);
      return worker;
    }},
  };
  for (const [name, value] of Object.entries(globals)) {
    const original = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, {value, configurable: true});
    t.after(() => {
      if (original) Object.defineProperty(globalThis, name, original);
      else delete globalThis[name];
    });
  }

  const processor = createDocumentProcessor();
  t.after(() => processor.close());
  const scan = (name) => new File(["scan"], name, {type: "image/png"});
  for (const name of ["first.png", "second.png"]) {
    const result = await processor.process([scan(name)], "auto");
    assert.deepEqual(result.pages.map(({number, source_file}) => [number, source_file]), [[1, name]]);
  }
  assert.equal(workers.length, 1);
  assert.equal(workers[0].terminations, 0);

  workers[0].fail = true;
  await assert.rejects(processor.process([scan("broken.png")], "auto"), /OCR stopped/);
  assert.equal(workers[0].terminations, 1);
  const recovered = await processor.process([scan("last.png")], "auto");
  assert.equal(recovered.pages[0].source_file, "last.png");
  assert.equal(workers.length, 2);

  await processor.close();
  await processor.close();
  assert.deepEqual(workers.map(worker => worker.terminations), [1, 1]);
});
