"""Bounded, local OCR subprocesses. Downloads are an explicit setup operation."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
from queue import Empty
from ocr_process import worker
from ocr_consensus import OCR_THRESHOLD, reconcile, parse_lines
from core import DOCUMENT_TYPES, classify, extract_identifiers, normalize
from ocr_consensus import normalized

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


def run_docling(pages, timeout=None):
    """Read each page with the local Docling converter."""
    try:
        process = worker('docling', [worker_python(), '-u', '-X', 'utf8',
                                     str(ROOT / 'docling_worker.py')],
                         {**os.environ, 'PYTHONIOENCODING': 'utf-8',
                          'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                          'HF_HUB_DISABLE_TELEMETRY': '1'})
        response = process.call({'pages': pages, 'models': str(MODEL_ROOT)},
                                timeout=timeout or 120 + 95 * len(pages))
        if response.get('ok'):
            return response
    except (OSError, ValueError, Empty, subprocess.SubprocessError):
        pass
    return {'ok': False, 'pages': [], 'error': 'Docling недоступен. Проверьте подготовку моделей.'}


def warmup_docling():
    """Run the real image pipeline once, then keep its worker and models in memory."""
    sample = ROOT / 'samples/certificate_9.png'
    if not sample.is_file():
        return False
    result = run_docling([{'page': 1, 'path': str(sample)}], timeout=300)
    return any(page['lines'] for page in result.get('pages', []))


def add_docling_suggestions(rows, table_rows, parser):
    """Expose table proposals without changing the OCR quorum or accepted grades."""
    if parser is None:
        return 0, 0
    known = {(row['page'], normalized(row['subject'])): row for row in rows}
    added = hints = 0
    for item in table_rows:
        page, text = item['page'], item['text']
        lines = [{'text': 'Итоговые оценки', 'page': page, 'confidence': 0},
                 {'text': text, 'page': page, 'confidence': 0}]
        for candidate in parser(lines):
            key = page, normalized(candidate['subject'])
            if type(candidate['grade']) is not int or candidate['conflict']:
                continue
            suggested = candidate['grade']
            evidence = {'grade': suggested, 'confidence': 0, 'source': text}
            if key in known:
                existing = known[key]
                if not existing['auto_verified'] and 'docling_table' not in existing['votes']:
                    existing['votes']['docling_table'] = evidence
                    existing['suggested_grade'] = suggested
                    if existing['grade'] is None:
                        existing['grade'] = suggested
                        existing['conflict'] = False
                    hints += 1
                continue
            row = {**candidate, 'grade': suggested, 'confidence': 0, 'auto_verified': False,
                   'verified': False, 'verification': 'manual_required',
                   'consensus_status': 'unresolved', 'conflict': False,
                   'issue': 'Найдена таблицей Docling. Сверьте предмет и оценку со сканом.',
                   'votes': {'docling_table': evidence},
                   'supporting_engines': [], 'suggested_grade': suggested,
                   'row_id': hashlib.sha256(f'docling:{page}:{key[1]}'.encode()).hexdigest()[:24]}
            rows.append(row)
            known[key] = row
            added += 1
    rows.sort(key=lambda row: (row['page'], (row.get('box') or [0, 0])[1],
                               (row.get('box') or [0])[0]))
    return added, hints


def usable_page(page, parser, requested_type):
    lines = page['lines']
    if not page['complete'] or not any(any(c.isalpha() for c in line['text']) for line in lines):
        return False
    if any(line['confidence'] < OCR_THRESHOLD for line in lines):
        return False
    text = '\n'.join(line['text'] for line in lines)
    detected = classify(text)
    rows = reconcile([{'ok': True, 'engine': 'docling', 'lines': lines}], parser=parser)
    if requested_type not in ('auto', detected) and detected in DOCUMENT_TYPES:
        return False
    if rows:
        if not page['table'] and len(rows) < 8:
            return False
        if any(not row['auto_verified'] for row in rows):
            return False
        pairs = {(normalized(row['subject']), row['grade']) for row in rows}
        for entry in page['table']:
            for row in parser([{'text': entry, 'page': page['page'], 'confidence': 0}]):
                if (normalized(row['subject']), row['grade']) not in pairs:
                    return False
        return True
    if 'итоговые оценки' in normalize(text) or 'наименование учебных предметов' in normalize(text):
        return False
    if detected == 'achievement':
        return True
    if detected in DOCUMENT_TYPES or detected == 'education_unknown':
        return bool(extract_identifiers(lines)[0]['document_number'])
    return False


def recognize_document(pages, runner=None, parser=None, include_observations=False,
                       requested_type='auto', docling_runner=None):
    parser = parser or parse_lines
    runner = runner or run_engine
    response = (docling_runner or run_docling)(pages)
    by_page = {p['page']: p for p in response.get('pages', [])}
    primary = [line for page in pages for line in by_page.get(page['page'], {}).get('lines', [])]
    observations = [{'engine': 'docling', 'ok': bool(primary), 'lines': primary,
                     'error': '' if primary else 'Docling не прочитал текст.'}]
    fallback = [page for page in pages if page['page'] not in by_page
                or not usable_page(by_page[page['page']], parser, requested_type)]
    incomplete = []
    if fallback:
        secondary = runner('tesseract', fallback)
        observations.append(secondary)
        read_pages = {line['page'] for line in secondary['lines'] if line['text'].strip()}
        incomplete = [p['page'] for p in fallback if p['page'] not in read_pages]
    rows = reconcile(observations, parser=parser)
    table_rows = [{'page': page['page'], 'text': text}
                  for page in by_page.values() for text in page['table']]
    added, hints = add_docling_suggestions(rows, table_rows, parser)
    accepted = sum(row['auto_verified'] for row in rows)
    summary = {'engines': [{'name': o['engine'], 'ok': o['ok'], 'error': o['error']}
                           for o in observations],
               'first_only': not fallback, 'third_used': False, 'third_pages': [],
               'fallback_pages': [p['page'] for p in fallback], 'incomplete_pages': incomplete,
               'threshold': OCR_THRESHOLD, 'accepted': accepted, 'unresolved': len(rows) - accepted,
               'status': 'accepted' if rows and accepted == len(rows) and not incomplete else 'needs_review'}
    result = {'grades': rows, 'ocr': summary, 'docling': {
        'used': True, 'ok': bool(primary), 'pages': [p['page'] for p in pages],
        'suggested': added, 'hints': hints}}
    if include_observations:
        result['observations'] = observations
    return result
