"""Persistent document batches using the existing Algoritmi OCR processor."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import uuid

from admissions import GRADE_FIELD, manual, project, stage_submission
from core import CERTIFICATE_TYPES, DOCUMENT_TYPES
from document_review import apply_review
from uploads import Problem, prepare_upload, MAX_FILES


class BatchService:
    def __init__(self, root, processor, lk_processor=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.processor = processor
        self.lk_processor = lk_processor
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'batches.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, batch TEXT, position INTEGER, status TEXT, revision INTEGER, payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS lk_snapshots (applicant TEXT PRIMARY KEY, documents TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS lk_batches (batch TEXT PRIMARY KEY, applicant TEXT NOT NULL, removed TEXT NOT NULL)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS lk_ocr (
            applicant TEXT, fingerprint TEXT, full INTEGER, analysis TEXT NOT NULL,
            PRIMARY KEY (applicant, fingerprint, full))''')
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
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=self.root) as staging:
            groups, skipped = prepare_upload(raw, filename, Path(staging))
            for group in groups:
                group['requested_type'] = kind
            return self.enqueue(groups, skipped)

    def submit_lk(self, data):
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=self.root) as staging:
            applicant, groups = stage_submission(data, Path(staging))
            return self.enqueue(groups, [], applicant)

    def enqueue(self, groups, skipped, applicant=None):
        batch = str(uuid.uuid4())
        created = []
        with self.lock:
            pending = self.db.execute("SELECT COUNT(*) FROM documents WHERE status IN ('queued','processing','interrupted')").fetchone()[0]
            if pending + len(groups) > MAX_FILES:
                raise Problem('В очереди уже 200 документов. Дождитесь завершения обработки.')
            try:
                if applicant is not None:
                    row = self.db.execute('SELECT documents FROM lk_snapshots WHERE applicant=?', (applicant,)).fetchone()
                    previous = json.loads(row[0]) if row else {}
                    snapshot = {group['document_key']: group['lk']['fingerprint'] for group in groups}
                    removed = sorted(previous.keys() - snapshot.keys())
                    self.db.execute('INSERT INTO lk_batches VALUES (?,?,?)',
                                    (batch, applicant, json.dumps(removed, ensure_ascii=False)))
                    self.db.execute('INSERT OR REPLACE INTO lk_snapshots VALUES (?,?)',
                                    (applicant, json.dumps(snapshot, ensure_ascii=False)))
                for n, group in enumerate(groups):
                    document_id = str(uuid.uuid4())
                    destination = self.root / (document_id + '-input')
                    group['folder'].rename(destination)
                    created.append(destination)
                    payload = {'document_key': group['document_key'], 'source_files': group['source_files'],
                               'requested_type': group['requested_type'], 'skipped_files': skipped if n == 0 else [],
                               'result': None, 'error': ''}
                    if applicant is not None:
                        lk = group['lk']
                        old = previous.get(lk['document_id'])
                        lk['change'] = 'new' if old is None else 'unchanged' if old == lk['fingerprint'] else 'changed'
                        payload['lk'] = lk
                    self.db.execute('INSERT INTO documents VALUES (?,?,?,?,?,?)',
                                    (document_id, batch, n, 'queued', 0, json.dumps(payload, ensure_ascii=False)))
                self.db.commit()
            except Exception:
                self.db.rollback()
                for folder in created:
                    shutil.rmtree(folder, ignore_errors=True)
                raise
            accepted = self.batch_result(batch)
            self.worker.submit(self.process_batch, batch)
            return accepted

    def process_lk(self, document, inputs):
        lk = document['lk']
        lookup = (lk['applicant_id'], lk['fingerprint'])
        full = GRADE_FIELD in lk['missing'] and lk['complete'] is not False
        reused = False
        kind = document['requested_type']
        if kind == 'foreign':
            analysis = manual(kind, 'Иностранный аттестат или перевод обрабатывается вручную.')
        elif not lk['missing']:
            analysis = {'type': None, 'fields': {}, 'issues': {}}
        elif kind not in CERTIFICATE_TYPES | {'auto'}:
            analysis = manual(kind, 'Этот тип документа передаётся на ручную проверку.')
        else:
            with self.lock:
                row = self.db.execute('''SELECT analysis FROM lk_ocr
                    WHERE applicant=? AND fingerprint=? AND full>=? ORDER BY full DESC LIMIT 1''',
                    (*lookup, full)).fetchone()
            if row:
                analysis, reused = json.loads(row[0]), True
            else:
                order = lk['order']
                if not full and set(lk['roles']) == {'certificate', 'appendix'}:
                    order = [i for i in order if lk['roles'][i] == 'certificate']
                files = ((document['source_files'][i], (inputs / f'{i}.bin').read_bytes()) for i in order)
                analysis = self.lk_processor(files, full)
                if analysis['cacheable']:
                    with self.lock:
                        self.db.execute('INSERT OR REPLACE INTO lk_ocr VALUES (?,?,?,?)',
                                        (*lookup, full, json.dumps(analysis, ensure_ascii=False)))
                        self.db.commit()
            if kind in CERTIFICATE_TYPES and analysis['type'] in CERTIFICATE_TYPES and kind != analysis['type']:
                analysis = manual(analysis['type'], 'Тип аттестата в ЛК и на скане различается.')
        patch, review = project(analysis, lk)
        document['result'] = {'detected_type': analysis['type'], 'fields_to_fill': patch,
                              'review': review, 'reused_ocr': reused}
        return 'review' if review else 'ready'

    def process_batch(self, batch):
        with self.lock:
            ids = [row[0] for row in self.db.execute("SELECT id FROM documents WHERE batch=? AND status IN ('queued','interrupted') ORDER BY position", (batch,))]
        for document_id in ids:
            with self.lock:
                document = self.get(document_id)
                self.save(document, 'processing')
            inputs = self.root / (document_id + '-input')
            try:
                if 'lk' in document:
                    status = self.process_lk(document, inputs)
                else:
                    files = ((name, (inputs / f'{n}.bin').read_bytes()) for n, name in enumerate(document['source_files']))
                    result = self.processor(files, document['requested_type'])
                    document['result'] = result
                    status = 'ready' if result.get('auto_document_verified') else 'review'
                with self.lock:
                    self.save(document, status)
                shutil.rmtree(inputs, ignore_errors=True)
            except Exception as error:
                with self.lock:
                    document['error'] = str(error) if isinstance(error, ValueError) else 'Не удалось обработать аттестат. Проверьте файл и настройку OCR.'
                    self.save(document, 'error')

    def document_result(self, document_id):
        with self.lock:
            document = self.get(document_id)
            result = document.get('result')
            if 'lk' in document:
                lk = document['lk']
                return {'id': document_id, 'batch_id': document['batch'],
                        'applicant_id': lk['applicant_id'], 'document_id': lk['document_id'],
                        'status': document['status'], 'change': lk['change'],
                        'error': document['error'], 'requires_review': document['status'] == 'review',
                        **(result or {'fields_to_fill': {}, 'review': [], 'reused_ocr': False})}
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
            lk = self.db.execute('SELECT applicant, removed FROM lk_batches WHERE batch=?', (batch,)).fetchone()
            integration = {'applicant_id': lk[0], 'removed_document_ids': json.loads(lk[1])} if lk else {}
            return {'batch_id': batch, 'status': status, 'total': len(rows), 'counts': counts, **integration,
                    'results_url': f'/api/v1/batches/{batch}',
                    'skipped_files': self.get(rows[0]['id']).get('skipped_files', []),
                    'documents': [self.document_result(row['id']) for row in rows]}

    def review(self, document_id, data):
        with self.lock:
            document = self.get(document_id)
            if 'lk' in document:
                raise Problem('Сверку документов из ЛК выполняйте в личном кабинете.')
            if document['status'] not in ('review', 'ready') or data.get('revision') != document['revision']:
                raise Problem('Документ не готов или ревизия устарела. Прочитайте результат заново.')
            result = apply_review(document['result'], data)
            document['result'] = result
            self.save(document, 'ready' if result['reviewed'] else 'review')
            return self.document_result(document_id)
