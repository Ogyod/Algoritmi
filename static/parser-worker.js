// Runs the existing Python parser in WebAssembly. Document text is passed as
// JSON data to a fixed function; it is never executed as Python or JavaScript.
import {loadPyodide} from "./vendor/pyodide/pyodide.mjs";
const ready = (async () => {
  const python = await loadPyodide({indexURL: new URL("./vendor/pyodide/", self.location.href).href});
  const response = await fetch("./core.py");
  if (!response.ok) throw new Error("Не удалось загрузить обработчик документа.");
  python.FS.writeFile("/home/pyodide/core.py", await response.text());
  const subjects = await fetch("./subjects.json");
  if (!subjects.ok) throw new Error("Не удалось загрузить справочник предметов.");
  python.FS.writeFile("/home/pyodide/subjects.json", await subjects.text());
  python.runPython(`
import json
from core import interpret, interpret_lines
def process_request(payload):
    data = json.loads(payload)
    result = (interpret(data['pages'], data['type']) if 'pages' in data
              else interpret_lines(data['lines'], data['type']))
    return json.dumps(result, ensure_ascii=False)
`);
  return python.globals.get("process_request");
})();
// Keep a failed initialization available to each request without an unhandled rejection.
ready.catch(() => {});
self.onmessage = async ({data}) => {
  try {
    const process = await ready;
    self.postMessage({id: data.id, result: JSON.parse(process(JSON.stringify(data.payload)))});
  } catch {
    self.postMessage({id: data.id, error: "Обработчик не загрузился. Проверьте соединение и обновите страницу."});
  }
};
