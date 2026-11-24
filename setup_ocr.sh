#!/bin/sh
set -eu
cd "$(dirname "$0")"

if command -v python3.12 >/dev/null 2>&1; then
  TASK_PYTHON_CMD=python3.12
else
  TASK_PYTHON_CMD=python3
fi
"$TASK_PYTHON_CMD" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Нужен Python 3.12"'

if [ "$(uname -s)" = Darwin ]; then
  if [ "$(uname -m)" != arm64 ]; then
    echo 'Для полного OCR и Docling на macOS нужен Mac с Apple Silicon.' >&2
    exit 1
  fi
fi
"$TASK_PYTHON_CMD" -m venv .ocr-venv

if [ "$(uname -s)" = Darwin ]; then
  .ocr-venv/bin/python -m pip install -r requirements.txt \
    'rapidocr==3.10.0' 'onnxruntime==1.31.1' \
    'torch==2.15.0' 'torchvision==0.29.2' 'docling==2.150.1'
else
  .ocr-venv/bin/python -m pip install -r requirements-ocr.txt \
    --extra-index-url https://download.pytorch.org/whl/cpu
fi
npm install --ignore-scripts --no-audit --no-fund
.ocr-venv/bin/python -X utf8 prepare_ocr.py
