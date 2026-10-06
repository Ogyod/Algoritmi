#!/bin/zsh
set -e
cd "${0:A:h}"
TASK_RUNTIME="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies"
if [[ -x "$TASK_RUNTIME/python/bin/python3" ]]; then
  TASK_PYTHON="$TASK_RUNTIME/python/bin/python3"
else
  if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
  fi
  TASK_PYTHON="$PWD/.venv/bin/python"
fi
if [[ -x "$TASK_RUNTIME/node/bin/node" ]]; then
  export PATH="$TASK_RUNTIME/node/bin:$PATH"
fi
if [[ ! -d node_modules/tesseract.js && ! -d "$TASK_RUNTIME/node/node_modules/tesseract.js" ]]; then
  npm install --ignore-scripts
fi
if "$TASK_PYTHON" - <<'PY'
from urllib.request import urlopen
import subprocess
try:
    with urlopen('http://127.0.0.1:8765/api/health',timeout=2) as response:
        import json
        if json.load(response).get('app') == 'admissions-document-processor':
            subprocess.run(['open','http://127.0.0.1:8765'])
            raise SystemExit(0)
except OSError:
    pass
raise SystemExit(1)
PY
then
  exit 0
fi
exec "$TASK_PYTHON" server.py --open
