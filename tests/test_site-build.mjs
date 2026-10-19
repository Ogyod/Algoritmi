import test from "node:test";
import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import {readFile, readdir, access} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
test("the website ships one complete version with every OCR fallback and no unused engines", async () => {
  execFileSync(process.execPath, ["scripts/build-site.mjs"], {cwd: root});
  const html = await readFile(path.join(root, "dist/index.html"), "utf8");
  assert.match(html, /data-processing="browser"/);
  const script = html.match(/src="\.\/(assets\/[a-f0-9]+)\/app\.js"/);
  assert.ok(script, "entry module must have a versioned URL");
  const assets = path.join(root, "dist", script[1]);
  assert.equal(await readFile(path.join(assets, "core.py"), "utf8"),
    await readFile(path.join(root, "core.py"), "utf8"));
  assert.equal(await readFile(path.join(assets, "subjects.json"), "utf8"),
    await readFile(path.join(root, "subjects.json"), "utf8"));
  for (const name of ["app.js", "domain.js", "processor.js", "browser-processor.js", "parser-worker.js", "ocr-engine.js"]) {
    const source = await readFile(path.join(assets, name), "utf8");
    // Check the actual local imports/fetches, so a renamed or omitted runtime
    // file cannot silently produce a broken published website.
    for (const match of source.matchAll(/(?:from\s+|import\(|fetch\(|asset\()["'](\.\/[^"']+)["']/g))
      await access(path.resolve(assets, match[1]));
  }
  const engines = await readdir(path.join(assets, "vendor/tesseract-core"));
  assert.deepEqual(engines.sort(), ["tesseract-core-lstm.wasm.js",
    "tesseract-core-relaxedsimd-lstm.wasm.js", "tesseract-core-simd-lstm.wasm.js"]);
  await access(path.join(assets, "vendor/pyodide/pyodide.asm.wasm"));
  await access(path.join(assets, "vendor/pyodide/python_stdlib.zip"));
  await access(path.join(assets, "models/rus.traineddata"));
  await access(path.join(assets, "models/eng.traineddata"));
  await access(path.join(assets, "vendor/licenses/tesseract-core.txt"));
});
