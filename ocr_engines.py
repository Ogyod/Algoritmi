"""Bounded, local OCR subprocesses. Downloads are an explicit setup operation."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
from queue import Empty
from ocr_process import worker
from ocr_consensus import GRADE_END, OCR_THRESHOLD, reconcile, parse_lines
from core import (DOCUMENT_TYPES, classify, extract_identifiers, has_grade_heading,
                  is_non_grade_page, pages_needing_review)
from ocr_consensus import normalized

ROOT = Path(__file__).resolve().parent
MODEL_ROOT = Path(os.environ.get('OCR_MODEL_DIR', ROOT / '.ocr-models'))
WARMUP_SAMPLE = ROOT / 'samples/certificate_9.png'


def worker_python():
    configured = os.environ.get('OCR_PYTHON')
    if configured:
        return configured
    for name in ('.ocr-venv/Scripts/python.exe', '.ocr-venv/bin/python'):
        candidate = ROOT / name
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def run_engine(engine, pages, timeout=None):
    start = time.perf_counter()
    try:
        process = worker(engine, [worker_python(), '-u', '-X', 'utf8',
                                 str(ROOT / 'ocr_worker.py'), engine, '--serve'],
                         {**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        result = process.call({'pages': pages},
                              timeout=timeout or 120 + 60 * len(pages))
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


def warmup_tesseract():
    """Give Tesseract.js enough pages to initialize both page workers."""
    from PIL import Image

    try:
        with Image.open(WARMUP_SAMPLE) as image:
            width, height = image.size
    except OSError:
        return False
    pages = [{'page': number, 'path': str(WARMUP_SAMPLE), 'width': width, 'height': height}
             for number in (1, 2)]
    result = run_engine('tesseract', pages, timeout=120)
    return result['ok'] and {line['page'] for line in result['lines'] if line['text'].strip()} == {1, 2}


def warmup_docling():
    """Run the real image pipeline once, then keep its worker and models in memory."""
    if not WARMUP_SAMPLE.is_file():
        return False
    result = run_docling([{'page': 1, 'path': str(WARMUP_SAMPLE)}], timeout=300)
    return result['ok'] and any(page['complete'] and page['lines'] for page in result['pages'])


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
                if existing['auto_verified'] and existing['grade'] != suggested:
                    existing.update(auto_verified=False, verification='manual_required',
                                    consensus_status='unresolved', supporting_engines=[], conflict=True,
                                    issue='Структура таблицы и распознанная оценка расходятся.')
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


def has_unpaired_grade(lines, rows):
    """A grade in its own OCR cell should be covered by a parsed row's box."""
    boxes = [row['box'] for row in rows if row.get('box')]
    for line in lines:
        match = GRADE_END.fullmatch('Предмет ' + line['text'].strip())
        if not match or match[1] != 'Предмет':
            continue
        box = line.get('box')
        if not box or not any(left <= box[0] and top <= box[1] and
                              right >= box[2] and bottom >= box[3]
                              for left, top, right, bottom in boxes):
            return True
    return False


def usable_page(page, parser, requested_type, embedded_text=False):
    lines = page['lines']
    if not page['complete'] or not lines:
        return False
    text = '\n'.join(line['text'] for line in lines)
    reliable = [line for line in lines if line['confidence'] >= OCR_THRESHOLD]
    detected = classify('\n'.join(line['text'] for line in reliable))
    if requested_type not in ('auto', detected) and detected in DOCUMENT_TYPES:
        return False
    engine = 'pdf_text' if embedded_text else 'tesseract'
    rows = reconcile([{'ok': True, 'engine': engine, 'lines': lines}], parser=parser)
    grade_heading = has_grade_heading(text)
    if rows:
        enough_rows = grade_heading if embedded_text else len(rows) >= 8
        return (enough_rows and all(row['auto_verified'] for row in rows)
                and not has_unpaired_grade(lines, rows))
    if grade_heading or not is_non_grade_page(lines):
        return False
    if embedded_text or detected == 'achievement':
        return True
    seen, _ = extract_identifiers(lines)
    verified, _ = extract_identifiers(reliable)
    return (len(seen['identifier_candidates']) <= 1
            and seen['document_number'] == verified['document_number']
            and seen['document_series'] == verified['document_series'])


def recognize_document(pages, runner=None, parser=None, include_observations=False,
                       requested_type='auto', docling_runner=None):
    parser = parser or parse_lines
    runner = runner or run_engine
    digital = [p['pdf_reading'] for p in pages if 'pdf_reading' in p]
    scans = [p for p in pages if 'pdf_reading' not in p]
    observations = []
    if digital:
        observations.append({'engine': 'pdf_text', 'ok': True, 'error': '',
                             'lines': [line for p in digital for line in p['lines']]})
    tesseract_seconds = docling_seconds = 0
    fallback = []
    by_page = {}
    lines_by_page = {}
    docling_lines = []
    if scans:
        started = time.perf_counter()
        primary = runner('tesseract', scans)
        tesseract_seconds = round(time.perf_counter() - started, 3)
        observations.append(primary)
        if primary['ok']:
            for line in primary['lines']:
                lines_by_page.setdefault(line['page'], []).append(line)
        fallback = [page for page in scans if not usable_page(
            {'complete': primary['ok'], 'lines': lines_by_page.get(page['page'], [])},
            parser, requested_type)]
    if fallback:
        started = time.perf_counter()
        response = (docling_runner or run_docling)(fallback)
        docling_seconds = round(time.perf_counter() - started, 3)
        by_page = {page['page']: page for page in response.get('pages', [])}
        docling_lines = [line for page in by_page.values() for line in page['lines']]
        observations.append({'engine': 'docling', 'ok': bool(docling_lines), 'lines': docling_lines,
                             'error': '' if docling_lines else response.get('error', 'Docling не прочитал текст.')})
    rows = reconcile(observations, parser=parser)
    table_rows = [{'page': page['page'], 'text': text}
                  for page in by_page.values() for text in page['table']]
    added, hints = add_docling_suggestions(rows, table_rows, parser)
    incomplete = pages_needing_review((page['page'] for page in pages),
                                     (o['lines'] for o in observations if o['ok']), rows)
    for page in fallback:
        number = page['page']
        second_reading = by_page.get(number, {})
        if second_reading.get('complete') and second_reading.get('lines'):
            continue
        lines = lines_by_page.get(number, [])
        # Optional cover details do not block the grade calculation.
        if not (is_non_grade_page(lines) and not parser(lines)
                and not has_grade_heading('\n'.join(line['text'] for line in lines))):
            incomplete.append(number)
    incomplete = sorted(set(incomplete))
    accepted = sum(row['auto_verified'] for row in rows)
    summary = {'engines': [{'name': o['engine'], 'ok': o['ok'], 'error': o['error']}
                           for o in observations],
               'first_only': not fallback, 'third_used': False, 'third_pages': [],
               'fallback_pages': [p['page'] for p in fallback], 'incomplete_pages': incomplete,
               'pdf_text_pages': [p['page'] for p in digital],
               'timing_seconds': {'docling': docling_seconds, 'tesseract': tesseract_seconds},
               'threshold': OCR_THRESHOLD, 'accepted': accepted, 'unresolved': len(rows) - accepted,
               'status': 'accepted' if rows and accepted == len(rows) and not incomplete else 'needs_review'}
    result = {'grades': rows, 'ocr': summary, 'docling': {
        'used': bool(fallback), 'ok': bool(docling_lines), 'pages': [p['page'] for p in fallback],
        'suggested': added, 'hints': hints}}
    if include_observations:
        result['observations'] = observations
    return result
