$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force -Path '.ocr-tmp' | Out-Null
$env:TEMP = Join-Path $PSScriptRoot '.ocr-tmp'
$env:TMP = $env:TEMP
$ocrPython = Join-Path $PSScriptRoot '.ocr-venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $ocrPython)) {
    $bundledPython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    if (Test-Path -LiteralPath $bundledPython) { & $bundledPython -m venv .ocr-venv }
    else { py -3.12 -m venv .ocr-venv }
    if ($LASTEXITCODE) { throw 'Нужен Python 3.12 для нового OCR-окружения.' }
}
& $ocrPython -m pip install -r requirements-ocr.txt --extra-index-url https://download.pytorch.org/whl/cpu
if ($LASTEXITCODE) { throw 'Установка зависимостей не завершена.' }
if (!(Test-Path -LiteralPath 'node_modules/tesseract.js')) {
    & npm.cmd install --ignore-scripts --no-audit --no-fund
    if ($LASTEXITCODE) { throw 'Не удалось установить Tesseract.js.' }
}
& $ocrPython -X utf8 prepare_ocr.py
if ($LASTEXITCODE) { throw 'Локальные модели OCR не подготовлены.' }
