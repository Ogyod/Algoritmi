"""Repeatable synthetic PDF benchmark; no applicant documents are used."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
import ocr_engines


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else 'after'
    source = ROOT / 'test-results/benchmark.pdf'
    if not source.exists():
        from PIL import Image
        with Image.open(ROOT / 'samples/certificate_9.png') as image:
            image = image.convert('RGB')
            source.parent.mkdir(exist_ok=True)
            image.save(source, 'PDF', resolution=150, save_all=True,
                       append_images=[image.copy(), image.copy()])
    runs = []
    for attempt in range(2):
        timings = {}
        original = ocr_engines.run_engine
        def measured(engine, pages):
            start = time.perf_counter()
            answer = original(engine, pages)
            timings[engine] = round(time.perf_counter() - start, 3)
            return answer
        ocr_engines.run_engine = measured
        start = time.perf_counter()
        try:
            result = server.process(source.read_bytes(), 'auto')
        finally:
            ocr_engines.run_engine = original
        elapsed = round(time.perf_counter() - start, 3)
        semantic = {'type': result['type'], 'average': result['average'],
                    'grades': [{key: row.get(key) for key in
                                ('page', 'subject', 'grade', 'auto_verified', 'supporting_engines')}
                               for row in result['grades']],
                    'engines': result['ocr']['engines']}
        entry = {'attempt': attempt + 1, 'seconds': elapsed, 'engines_seconds': timings,
                 'semantic': semantic}
        runs.append(entry)
        print(json.dumps({'label': label, **entry}, ensure_ascii=False), flush=True)
    report = {'label': label, 'pdf_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'runs': runs}
    (ROOT / f'test-results/pdf-{label}.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
