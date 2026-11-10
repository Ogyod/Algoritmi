"""Explicit real-engine check on the project's synthetic certificate sample."""
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import process

root = Path(__file__).resolve().parents[1]
start = time.monotonic()
result = process((root/'samples/certificate_9.png').read_bytes(), 'auto')
summary = {'type': result['type'], 'average': result['average'], 'ocr': result['ocr'],
           'grades': [(row['subject'], row['grade'], row['auto_verified']) for row in result['grades']],
           'seconds': round(time.monotonic()-start, 2)}
print(json.dumps(summary, ensure_ascii=False), flush=True)
(root/'test-results').mkdir(exist_ok=True)
(root/'test-results/ocr-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
assert result['type'] == 'certificate_9'
assert len(result['grades']) == 6
assert result['average'] == 4.5
assert result['auto_grades_verified']
assert all(engine['ok'] for engine in result['ocr']['engines'])
