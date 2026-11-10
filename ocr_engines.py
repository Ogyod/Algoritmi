"""Bounded, local OCR subprocesses. Downloads are an explicit setup operation."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from queue import Empty
from ocr_process import worker, shutdown_workers
from ocr_consensus import reconcile, needs_third

ROOT = Path(__file__).resolve().parent
MODEL_ROOT = Path(os.environ.get('OCR_MODEL_DIR', ROOT / '.ocr-models'))


def worker_python():
    configured = os.environ.get('OCR_PYTHON')
    if configured:
        return configured
    for name in ('.ocr-venv/Scripts/python.exe', '.ocr-venv/bin/python'):
        candidate = ROOT / name
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def run_engine(engine, pages):
    start = time.perf_counter()
    try:
        process = worker(engine, [worker_python(), '-u', '-X', 'utf8',
                                 str(ROOT / 'ocr_worker.py'), engine, '--serve'],
                         {**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        result = process.call({'pages': pages, 'models': str(MODEL_ROOT)},
                              timeout=120 + 60 * len(pages))
        if not result.get('ok'):
            raise ValueError('Unavailable OCR engine')
        return {'engine': engine, 'ok': True, 'lines': result['lines'], 'error': '',
                'seconds': round(time.perf_counter() - start, 3)}
    except (OSError, ValueError, KeyError, Empty, subprocess.SubprocessError):
        return {'engine': engine, 'ok': False, 'lines': [],
                'error': f'{engine}: распознавание не завершилось. Проверьте локальные модели (setup_ocr.ps1).',
                'seconds': round(time.perf_counter() - start, 3)}


def recognize_document(pages, runner=None, parser=None, include_observations=False):
    runner = runner or run_engine
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runner, engine, pages) for engine in ('rapidocr', 'tesseract')]
        observations = [f.result() for f in futures]
    threshold = .75
    first_rows = reconcile(observations, threshold, parser)
    contested = {r['page'] for r in first_rows if not r['auto_verified']}
    # Pages with no candidates, or a failed primary engine, also need another reading.
    for page in pages:
        page_observations = [{**o, 'lines': [line for line in o['lines'] if line['page'] == page['page']]} for o in observations]
        page_rows = reconcile(page_observations, threshold, parser)
        if needs_third(page_observations, page_rows):
            contested.add(page['page'])
    if contested:
        third_pages = []
        for page in pages:
            if page['page'] not in contested:
                continue
            regions = []
            for row in first_rows:
                if row['page'] != page['page'] or row['auto_verified']:
                    continue
                boxes = [v['box'] for v in row['votes'].values() if v.get('box')]
                if boxes:
                    left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
                    right, bottom = max(b[2] for b in boxes), max(b[3] for b in boxes)
                    margin = max(12, int((bottom - top) * .7))
                    regions.append([left - margin, top - margin, right + margin, bottom + margin])
            third_pages.append({**page, 'regions': regions})
        observations.append(runner('easyocr', third_pages))
    rows = reconcile(observations, threshold, parser)
    summary = {'engines': [{'name': o['engine'], 'ok': o['ok'], 'error': o['error'],
                            **({'seconds': o['seconds']} if 'seconds' in o else {})} for o in observations],
               'third_used': bool(contested), 'third_pages': sorted(contested), 'threshold': threshold,
               'accepted': sum(bool(r['auto_verified']) for r in rows),
               'unresolved': sum(not r['auto_verified'] for r in rows),
               'status': 'agreed' if rows and all(r['auto_verified'] for r in rows) else 'needs_review'}
    result = {'grades': rows, 'ocr': summary,
            'warning': 'Два независимых OCR согласовали строки.' if summary['status'] == 'agreed' else
                       'Спорные или пропущенные строки требуют сверки. Предлагаемые оценки показаны по каждому движку.'}

    if include_observations:
        result['observations'] = observations
    return result
