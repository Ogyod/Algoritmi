import {cp, mkdir, readFile, writeFile, rm, readdir} from "node:fs/promises";
import {createHash} from "node:crypto";
import path from "node:path";
import {fileURLToPath} from "node:url";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const out = path.join(root, "dist");
// Every module, worker, parser and model belongs to the same immutable release.
// Browser caches must never combine a new interface with an old Python parser.
const hash = createHash("sha256");
const releaseFiles = ["core.py", "subjects.json", "pnpm-lock.yaml", "scripts/build-site.mjs",
  ...(await readdir(path.join(root, "static"))).map((name) => "static/" + name),
  ...(await readdir(path.join(root, ".models"))).map((name) => ".models/" + name)];
for (const name of releaseFiles.sort()) {
  hash.update(name + "\0"); hash.update(await readFile(path.join(root, name)));
}
const prefix = "assets/" + hash.digest("hex").slice(0, 16);
const assets = path.join(out, prefix);
await rm(out, {recursive: true, force: true});
await mkdir(path.join(assets, "vendor"), {recursive: true});
await cp(path.join(root, "static"), assets, {recursive: true});
await rm(path.join(assets, "index.html"));
await cp(path.join(root, "core.py"), path.join(assets, "core.py"));
await cp(path.join(root, "subjects.json"), path.join(assets, "subjects.json"));
await cp(path.join(root, ".models"), path.join(assets, "models"), {recursive: true});
let html = await readFile(path.join(root, "static/index.html"), "utf8");
html = html.replace('data-processing="local"', 'data-processing="browser"');
for (const name of ["favicon.svg", "style.css", "app.js"])
  html = html.replace(`"./${name}"`, `"./${prefix}/${name}"`);
await writeFile(path.join(out, "index.html"), html);
await writeFile(path.join(out, ".nojekyll"), "");
for (const [source, dest] of [
  ["tesseract.js/dist", "tesseract"], ["tesseract.js-core", "tesseract-core"],
  ["pyodide", "pyodide"], ["pdfjs-dist/build", "pdfjs"],
  ["pdfjs-dist/cmaps", "cmaps"], ["pdfjs-dist/standard_fonts", "standard_fonts"],
  ["pdfjs-dist/wasm", "pdfjs-wasm"],
]) await cp(path.join(root, "node_modules", source), path.join(assets, "vendor", dest), {
  recursive: true, dereference: true,
  filter: (name) => path.basename(name) !== "node_modules" && !name.endsWith(".map"),
});
for (const [source, dest] of [["utif/UTIF.js", "utif.js"], ["pako/dist/pako.min.js", "pako.js"]])
  await cp(path.join(root, "node_modules", source), path.join(assets, "vendor", dest));
await mkdir(path.join(assets, "vendor", "licenses"));
for (const [source, dest] of [["tesseract.js/LICENSE.md", "tesseract.txt"],
  ["pdfjs-dist/LICENSE", "pdfjs.txt"], ["utif/LICENSE", "utif.txt"],
  ["pako/LICENSE", "pako.txt"]])
  await cp(path.join(root, "node_modules", source), path.join(assets, "vendor", "licenses", dest));
await cp(path.join(root, "licenses/pyodide.txt"), path.join(assets, "vendor/licenses/pyodide.txt"));
console.log("GitHub Pages site built in dist/");
