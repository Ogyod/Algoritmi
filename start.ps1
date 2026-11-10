param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$ocrPython = Join-Path $PSScriptRoot '.ocr-venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $ocrPython)) { $ocrPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe' }
if (!(Test-Path -LiteralPath $ocrPython)) { throw 'Выполните setup_ocr.ps1 для подготовки окружения.' }
& $ocrPython -u -X utf8 server.py --port $Port --open
