"""Persistent document batches using the existing Algoritmi OCR processor."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import uuid

from core import CERTIFICATE_TYPES, DOCUMENT_TYPES, EDUCATION_TYPES, mean_grade, normalize
import re
from uploads import Problem, prepare_upload, MAX_FILES


class BatchService:
    def __init__(self, root, processor):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.processor = processor
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'batches.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, batch TEXT, position INTEGER, status TEXT, revision INTEGER, payload TEXT)')
        self.db.execute("UPDATE documents SET status='interrupted' WHERE status='processing'")
        self.db.commit()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='archive-ocr')
        batches = [row[0] for row in self.db.execute("SELECT DISTINCT batch FROM documents WHERE status IN ('queued','interrupted')")]
        for batch in batches:
            self.worker.submit(self.process_batch, batch)

    def close(self):
        self.worker.shutdown(wait=True, cancel_futures=True)
        self.db.close()

    def get(self, document_id):
        row = self.db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
        if row is None:
            raise Problem('Документ не найден.')
        return {**dict(row), **json.loads(row['payload'])}

    def save(self, document, status):
        document['revision'] += 1
        document['status'] = status
        payload = {k: v for k, v in document.items() if k not in {'id', 'batch', 'position', 'status', 'revision', 'payload'}}
        self.db.execute('UPDATE documents SET status=?, revision=?, payload=? WHERE id=?',
                        (status, document['revision'], json.dumps(payload, ensure_ascii=False), document['id']))
        self.db.commit()

    def submit(self, raw, filename, kind='auto'):
        if kind != 'auto' and kind not in DOCUMENT_TYPES:
            raise Problem('Неизвестный тип документа.')
        batch = str(uuid.uuid4())
        created = []
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=self.root) as staging:
            groups, skipped = prepare_upload(raw, filename, Path(staging))
            with self.lock:
                pending = self.db.execute("SELECT COUNT(*) FROM documents WHERE status IN ('queued','processing','interrupted')").fetchone()[0]
                if pending + len(groups) > MAX_FILES:
                    raise Problem('В очереди уже 200 документов. Дождитесь завершения обработки.')
                try:
                    for n, group in enumerate(groups):
                        document_id = str(uuid.uuid4())
                        destination = self.root / (document_id + '-input')
                        group['folder'].rename(destination)
                        created.append(destination)
                        payload = {'document_key': group['document_key'], 'source_files': group['source_files'],
                                   'requested_type': kind, 'skipped_files': skipped if n == 0 else [],
                                   'result': None, 'error': ''}
                        self.db.execute('INSERT INTO documents VALUES (?,?,?,?,?,?)',
                                        (document_id, batch, n, 'queued', 0, json.dumps(payload, ensure_ascii=False)))
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    for folder in created:
                        shutil.rmtree(folder)
                    raise
                accepted = self.batch_result(batch)
                self.worker.submit(self.process_batch, batch)
                return accepted

    def process_batch(self, batch):
        with self.lock:
            ids = [row[0] for row in self.db.execute("SELECT id FROM documents WHERE batch=? AND status IN ('queued','interrupted') ORDER BY position", (batch,))]
        for document_id in ids:
            with self.lock:
                document = self.get(document_id)
                self.save(document, 'processing')
            inputs = self.root / (document_id + '-input')
            try:
                files = ((name, (inputs / f'{n}.bin').read_bytes()) for n, name in enumerate(document['source_files']))
                result = self.processor(files, document['requested_type'])
                with self.lock:
                    document.update(result=result, error='')
                    self.save(document, 'ready' if result.get('auto_document_verified') else 'review')
                shutil.rmtree(inputs, ignore_errors=True)
            except Exception as error:
                with self.lock:
                    document['error'] = str(error) if isinstance(error, ValueError) else 'Не удалось обработать аттестат. Проверьте файл и настройку OCR.'
                    self.save(document, 'error')

    def document_result(self, document_id):
        with self.lock:
            document = self.get(document_id)
            result = document.get('result')
            calculation = None
            if result and result.get('average') is not None and (document['status'] == 'ready' or result.get('auto_grades_verified')):
                values = [g['grade'] for g in result['grades']]
                calculation = {'sum': sum(values), 'count': len(values), 'value': f'{result["average"]:.2f}'}
            return {'id': document_id, 'batch_id': document['batch'], 'document_key': document['document_key'],
                    'status': document['status'], 'revision': document['revision'],
                    'source_files': document['source_files'], 'error': document['error'],
                    'requires_review': document['status'] == 'review', 'result': result,
                    'grades_verification': ('automatic' if result and result.get('auto_grades_verified') else
                                            'manual' if result and result.get('reviewed') else 'needs_review'),
                    'grades': result.get('grades', []) if result else [],
                    'pages': result.get('pages', []) if result else [], 'calculation': calculation}

    def batch_result(self, batch):
        with self.lock:
            rows = list(self.db.execute('SELECT id,status FROM documents WHERE batch=? ORDER BY position', (batch,)))
            if not rows:
                raise Problem('Пакет не найден.')
            counts = {status: sum(row['status'] == status for row in rows)
                      for status in ('queued', 'processing', 'interrupted', 'review', 'ready', 'error')}
            status = ('processing' if counts['queued'] or counts['processing'] else
                      'interrupted' if counts['interrupted'] else
                      'failed' if counts['error'] == len(rows) else
                      'completed_with_errors' if counts['error'] else 'completed')
            return {'batch_id': batch, 'status': status, 'total': len(rows), 'counts': counts,
                    'results_url': f'/api/v1/batches/{batch}',
                    'skipped_files': self.get(rows[0]['id']).get('skipped_files', []),
                    'documents': [self.document_result(row['id']) for row in rows]}

    def review(self, document_id, data):
        with self.lock:
            document = self.get(document_id)
            if document['status'] not in ('review', 'ready') or data.get('revision') != document['revision']:
                raise Problem('Документ не готов или ревизия устарела. Прочитайте результат заново.')
            grades = data.get('grades')
            if not isinstance(grades, list) or len(grades) > 100:
                raise Problem('Нужен список не более 100 предметов.')
            result = document['result']
            saved_rows = {row.get('row_id'): row for row in result['grades'] if row.get('row_id')}
            cleaned = []
            names = set()
            for original in grades:
                if not isinstance(original, dict):
                    raise Problem('Некорректная строка предмета.')
                row = dict(original)
                saved = saved_rows.get(row.get('row_id'))
                same = bool(saved) and all(row.get(key) == saved.get(key) for key in ('subject','grade','page','include','reason'))
                row['auto_verified'] = bool(same and saved.get('auto_verified'))
                row['votes'] = saved.get('votes', {}) if saved else {}
                row['supporting_engines'] = saved.get('supporting_engines', []) if row['auto_verified'] else []
                row['verification'] = 'ocr_consensus' if row['auto_verified'] else 'manual' if row.get('verified') is True else 'manual_required'
                if row.get('verified') is True:
                    row['conflict'] = False
                cleaned.append(row)
                if not isinstance(row, dict) or not isinstance(row.get('subject'), str) or not 1 <= len(row['subject'].strip()) <= 200:
                    raise Problem('Укажите название каждого предмета.')
                name = normalize(row['subject'])
                if name in names:
                    raise Problem('Повторяется предмет: ' + row['subject'])
                names.add(name)
                if data.get('complete') is True and row.get('verified') is not True and not row['auto_verified']:
                    raise Problem('Сверьте каждую оценку и передайте verified: true.')
            grades = cleaned
            kind = data.get('type', result['type'])
            if kind not in DOCUMENT_TYPES:
                raise Problem('Выберите категорию документа.')
            can_calculate = bool(grades) and all(type(row.get('grade')) is int and row['grade'] in (2,3,4,5)
                                                and not row.get('conflict') and (row.get('auto_verified') or row.get('verified') is True)
                                                for row in grades)
            if data.get('complete') is True and grades and not can_calculate:
                raise Problem('Проверьте оценки от 2 до 5 и их подтверждение.')
            average = mean_grade(grades) if can_calculate and kind in CERTIFICATE_TYPES else None
            if data.get('complete') is True and kind in CERTIFICATE_TYPES and not grades:
                raise Problem('Для расчёта аттестата нужен полный список оценок.')
            for field in ('document_series', 'document_number', 'series_not_present', 'achievement', 'points'):
                if field in data:
                    result[field] = data[field]
            if data.get('complete') is True and kind in EDUCATION_TYPES:
                series = result.get('document_series', '')
                number = result.get('document_number', '')
                absent = result.get('series_not_present') is True
                if (not isinstance(number, str) or not re.fullmatch(r'\d{5,20}', number) or
                        not isinstance(series, str) or len(series) > 20 or
                        (series and not re.fullmatch(r'[\w -]+', series)) or
                        (not series.strip() and not absent) or (series.strip() and absent)):
                    raise Problem('Проверьте серию и номер документа; отсутствие серии подтвердите явно.')
            if data.get('complete') is True and kind == 'achievement':
                points = result.get('points')
                if not isinstance(result.get('achievement'), str) or not result['achievement'].strip() or type(points) not in (int, float) or not 0 <= points < float('inf'):
                    raise Problem('Укажите достижение и неотрицательные баллы.')
            result.update(auto_document_verified=False, type=kind, grades=grades, average=average, reviewed=data.get('complete') is True,
                          auto_grades_verified=bool(grades) and all(row['auto_verified'] for row in grades))
            if result.get('ocr'):
                result['ocr']['accepted'] = sum(bool(row['auto_verified']) for row in grades)
                result['ocr']['unresolved'] = sum(not (row['auto_verified'] or row.get('verified') is True) for row in grades)
                result['ocr']['status'] = 'agreed' if result['auto_grades_verified'] else 'reviewed' if result['reviewed'] else 'needs_review'
            self.save(document, 'ready' if result['reviewed'] else 'review')
            return self.document_result(document_id)
