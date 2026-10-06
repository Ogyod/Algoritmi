import {cp, mkdir, readFile, writeFile, rm} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const out = path.join(root, "dist");
await rm(out, {recursive: true, force: true});
await mkdir(path.join(out, "vendor"), {recursive: true});
await cp(path.join(root, "static"), out, {recursive: true});
await cp(path.join(root, "core.py"), path.join(out, "core.py"));
await cp(path.join(root, "subjects.json"), path.join(out, "subjects.json"));
await cp(path.join(root, ".models"), path.join(out, "models"), {recursive: true});
let html = await readFile(path.join(out, "index.html"), "utf8");
await writeFile(path.join(out, "index.html"), html.replace('data-processing="local"', 'data-processing="browser"'));
await writeFile(path.join(out, ".nojekyll"), "");
for (const [source, dest] of [
  ["tesseract.js/dist", "tesseract"], ["tesseract.js-core", "tesseract-core"],
  ["pyodide", "pyodide"], ["pdfjs-dist/build", "pdfjs"],
  ["pdfjs-dist/cmaps", "cmaps"], ["pdfjs-dist/standard_fonts", "standard_fonts"],
  ["pdfjs-dist/wasm", "pdfjs-wasm"],
]) await cp(path.join(root, "node_modules", source), path.join(out, "vendor", dest), {
  recursive: true, dereference: true,
  filter: (name) => path.basename(name) !== "node_modules" && !name.endsWith(".map"),
});
for (const [source, dest] of [["utif/UTIF.js", "utif.js"], ["pako/dist/pako.min.js", "pako.js"]])
  await cp(path.join(root, "node_modules", source), path.join(out, "vendor", dest));
await mkdir(path.join(out, "vendor", "licenses"));
for (const [source, dest] of [["tesseract.js/LICENSE.md", "tesseract.txt"],
  ["pdfjs-dist/LICENSE", "pdfjs.txt"], ["utif/LICENSE", "utif.txt"],
  ["pako/LICENSE", "pako.txt"]])
  await cp(path.join(root, "node_modules", source), path.join(out, "vendor", "licenses", dest));
await cp(path.join(root, "licenses/pyodide.txt"), path.join(out, "vendor/licenses/pyodide.txt"));
console.log("GitHub Pages site built in dist/");
