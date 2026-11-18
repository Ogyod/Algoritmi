"""Bounded, local OCR subprocesses. Downloads are an explicit setup operation."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
from queue import Empty
from ocr_process import worker
from ocr_consensus import OCR_THRESHOLD, reconcile, needs_third
from core import CERTIFICATE_TYPES, DOCUMENT_TYPES, classify, extract_identifiers, normalize
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


def warmup():
    def load(engine):
        process = worker(engine, [worker_python(), '-u', '-X', 'utf8',
                                 str(ROOT / 'ocr_worker.py'), engine, '--serve'],
                         {**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        try:
            return process.call({'pages': [], 'models': str(MODEL_ROOT), 'warmup': True}, 90).get('ok', False)
        except Exception:
            return False
    with ThreadPoolExecutor(max_workers=3) as pool:
        loaded = dict(zip(('rapidocr', 'tesseract', 'easyocr'),
                          pool.map(load, ('rapidocr', 'tesseract', 'easyocr'))))
    # Prime inference kernels using only the bundled, synthetic sample.
    sample = ROOT / 'samples/certificate_9.png'
    if all(loaded.values()) and sample.is_file():
        from PIL import Image
        with Image.open(sample) as image:
            page = {'page': 1, 'path': str(sample), 'width': image.width, 'height': image.height}
        from ocr_adapter import parse_engine_rows
        recognize_document([page], parser=parse_engine_rows)
    return loaded


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


def agreed_non_grade_page(observations, requested_type):
    """A third grade reader cannot add a vote to an agreed diploma or award."""
    if requested_type in CERTIFICATE_TYPES or requested_type == 'education_unknown':
        return False
    if any(not o['ok'] or not o['lines'] for o in observations):
        return False
    kinds = [classify('\n'.join(line['text'] for line in o['lines'])) for o in observations]
    if (len(set(kinds)) != 1 or kinds[0] not in DOCUMENT_TYPES
            or kinds[0] in CERTIFICATE_TYPES
            or requested_type not in ('auto', kinds[0])):
        return False
    if any('итоговые оценки' in normalize(line['text']) or
           'наименование учебных предметов' in normalize(line['text'])
           for o in observations for line in o['lines']):
        return False
    if kinds[0] == 'achievement':
        return True
    identifiers = [extract_identifiers(o['lines'])[0] for o in observations]
    return (bool(identifiers[0]['document_number']) and
            all((item['document_series'], item['document_number']) ==
                (identifiers[0]['document_series'], identifiers[0]['document_number'])
                for item in identifiers))


def agreed_certificate_cover_page(observations):
    """An appendix cover with an agreed number has no grade table to reread."""
    if any(not o['ok'] or not o['lines'] for o in observations):
        return False
    texts = [normalize(' '.join(line['text'] for line in o['lines'])) for o in observations]
    if not any('приложение' in text and 'аттестат' in text for text in texts):
        return False
    if any('итоговые оценки' in text or 'наименование учебных предметов' in text
           for text in texts):
        return False
    identifiers = [extract_identifiers(o['lines'])[0] for o in observations]
    return (bool(identifiers[0]['document_number']) and
            all(item['document_number'] == identifiers[0]['document_number'] for item in identifiers))


def run_docling(pages, timeout=None):
    """Docling supplies review suggestions, never an independent OCR vote."""
    try:
        process = worker('docling', [worker_python(), '-u', '-X', 'utf8',
                                     str(ROOT / 'docling_worker.py')],
                         {**os.environ, 'PYTHONIOENCODING': 'utf-8',
                          'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                          'HF_HUB_DISABLE_TELEMETRY': '1'})
        response = process.call({'pages': pages, 'models': str(MODEL_ROOT)},
                                timeout=timeout or 120 + 60 * len(pages))
        if response.get('ok'):
            return response
    except (OSError, ValueError, Empty, subprocess.SubprocessError):
        pass
    return {'ok': False, 'rows': [], 'error': 'Docling недоступен. Проверьте подготовку моделей.'}


def warmup_docling():
    """Run the real image pipeline once, then keep its worker and models in memory."""
    sample = ROOT / 'samples/certificate_9.png'
    return sample.is_file() and run_docling([{'page': 1, 'path': str(sample)}], timeout=300)['ok']


def add_docling_suggestions(rows, table_rows, parser):
    """Expose table proposals without changing the OCR quorum or accepted grades."""
    if parser is None:
        return 0, 0
    known = {(row['page'], normalized(row['subject'])): row for row in rows}
    added = hints = 0
    for item in table_rows[:100]:
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
                if not existing['auto_verified'] and 'docling' not in existing['votes']:
                    existing['votes']['docling'] = evidence
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
                   'votes': {'docling': evidence},
                   'supporting_engines': [], 'suggested_grade': suggested,
                   'row_id': hashlib.sha256(f'docling:{page}:{key[1]}'.encode()).hexdigest()[:24]}
            rows.append(row)
            known[key] = row
            added += 1
    rows.sort(key=lambda row: (row['page'], (row.get('box') or [0, 0])[1],
                               (row.get('box') or [0])[0]))
    return added, hints


def needs_table_suggestions(rows):
    """A table pass helps only when OCR has too little numeric row evidence."""
    if not rows:
        return True
    # A populated subject table needs grade review, not another full-page
    # layout model. Docling cannot provide an independent OCR quorum.
    if len(rows) >= 8:
        return False
    for row in rows:
        if row['auto_verified']:
            continue
        numeric = sum(type(v.get('grade')) is int and not v.get('conflict')
                      for name, v in row.get('votes', {}).items() if name != 'docling')
        if numeric < 2:
            return True
    return False


def recognize_document(pages, runner=None, parser=None, include_observations=False,
                       requested_type='auto', docling_runner=None, fast_third=False):
    runner = runner or run_engine
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runner, engine, pages) for engine in ('rapidocr', 'tesseract')]
        observations = [f.result() for f in futures]
    threshold = OCR_THRESHOLD
    first_rows = reconcile(observations, threshold, parser)
    contested = {r['page'] for r in first_rows if not r['auto_verified']}
    # Pages with no candidates, or a failed primary engine, also need another reading.
    for page in pages:
        if page['page'] in contested:
            continue
        page_observations = [{**o, 'lines': [line for line in o['lines'] if line['page'] == page['page']]} for o in observations]
        page_rows = reconcile(page_observations, threshold, parser)
        if needs_third(page_observations, page_rows) and not (
                not page_rows and (agreed_non_grade_page(page_observations, requested_type)
                                   or agreed_certificate_cover_page(page_observations))):
            contested.add(page['page'])
    if contested:
        third_pages = []
        for page in pages:
            if page['page'] not in contested:
                continue
            regions = []
            line_regions = []
            detect_regions = []
            for row in first_rows:
                if row['page'] != page['page'] or row['auto_verified']:
                    continue
                boxes = [v['box'] for v in row['votes'].values() if v.get('box')]
                if boxes:
                    # OCR can merge several table rows into a tall box. The
                    # smallest complete reading is the useful third-reader crop.
                    left, top, right, bottom = min(
                        boxes, key=lambda b: max(1, b[2]-b[0]) * max(1, b[3]-b[1]))
                    margin = max(12, int((bottom - top) * .7))
                    regions.append([left - margin, top - margin, right + margin, bottom + margin])
                    padding = max(2, int((bottom - top) * .12))
                    line_regions.append([left-padding, top-padding, right+padding, bottom+padding])
                    numeric = any(type(v.get('grade')) is int and not v.get('conflict')
                                  for v in row['votes'].values())
                    detect_regions.append(not fast_third or not numeric)
            third_pages.append({**page, 'regions': regions, 'line_regions': line_regions,
                                'detect_regions': detect_regions})
        observations.append(runner('easyocr', third_pages))
    rows = reconcile(observations, threshold, parser) if contested else first_rows
    docling = {'used': False, 'ok': False, 'suggested': 0, 'hints': 0}
    document_text = '\n'.join(line['text'] for o in observations[:2] if o['ok'] for line in o['lines'])
    relevant = requested_type in CERTIFICATE_TYPES or (requested_type == 'auto' and
                classify(document_text) in CERTIFICATE_TYPES | {'education_unknown'})
    if relevant and (runner is run_engine or docling_runner is not None) and needs_table_suggestions(rows):
        uncertain = {row['page'] for row in rows if not row['auto_verified']}
        target_pages = [page for page in pages if page['page'] in uncertain]
        if not target_pages:
            target_pages = [page for page in pages if any(
                line['page'] == page['page'] and
                ('итоговые оценки' in normalize(line['text']) or
                 'наименование учебных предметов' in normalize(line['text']))
                for o in observations[:2] if o['ok'] for line in o['lines'])]
        target_pages = (target_pages or pages)[:4]
        docling = {'used': True, 'ok': False, 'suggested': 0, 'hints': 0,
                   'pages': [page['page'] for page in target_pages]}
        response = (docling_runner or run_docling)(target_pages)
        docling['ok'] = bool(response.get('ok'))
        if docling['ok']:
            docling['suggested'], docling['hints'] = add_docling_suggestions(
                rows, response.get('rows', []), parser)
    summary = {'engines': [{'name': o['engine'], 'ok': o['ok'], 'error': o['error'],
                            **({'seconds': o['seconds']} if 'seconds' in o else {})} for o in observations],
               'third_used': bool(contested), 'third_pages': sorted(contested), 'threshold': threshold,
               'accepted': sum(bool(r['auto_verified']) for r in rows),
               'unresolved': sum(not r['auto_verified'] for r in rows),
               'status': 'agreed' if rows and all(r['auto_verified'] for r in rows) else 'needs_review'}
    result = {'grades': rows, 'ocr': summary, 'docling': docling,
            'warning': 'Два независимых OCR согласовали строки.' if summary['status'] == 'agreed' else
                       'Спорные или пропущенные строки требуют сверки. Предлагаемые оценки показаны по каждому движку.'}

    if include_observations:
        result['observations'] = observations
    return result
