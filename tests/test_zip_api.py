import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import zipfile
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from batch_service import BatchService
from uploads import Problem, unpack_archive


def picture(color='white'):
    output = io.BytesIO()
    with Image.new('RGB', (100, 80), color) as image:
        image.save(output, 'PNG')
    return output.getvalue()


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, raw in entries:
            z.writestr(name, raw)
    return output.getvalue()


def recognized(images):
    from ocr_engines import recognize_document
    from ocr_adapter import parse_engine_rows
    def runner(engine, pages):
        lines = []
        for page in pages:
            texts = ['Аттестат об основном общем образовании', 'Серия АБ Номер 0012345',
                     'Наименование учебных предметов Итоговые оценки',
                     'Математика 5' if page['page'] == 1 else 'Физика 4']
            lines.extend({'text': text, 'confidence': .95, 'page': page['page'],
                          'box': [0, n*8, 80, n*8+4]} for n, text in enumerate(texts))
        return {'engine': engine, 'ok': True, 'error': '', 'lines': lines}
    return recognize_document(images, runner, parse_engine_rows, True)


class Archives(unittest.TestCase):
    def test_grouping_natural_order_and_ignored_files(self):
        result = unpack_archive(archive([('001/10.png', picture()), ('001/2.png', picture()),
                                         ('002/1.png', picture()), ('single.png', picture()), ('notes.txt', b'notes')]))
        self.assertEqual([d['document_key'] for d in result['documents']], ['001', '002', 'single.png'])
        self.assertEqual([f['name'] for f in result['documents'][0]['files']], ['001/2.png', '001/10.png'])
        self.assertEqual(result['skipped_files'], ['notes.txt'])

    def test_rejects_bad_archives_paths_duplicates_and_expansion(self):
        for raw in [b'bad', archive([('../bad.png', picture())]), archive([('/bad.png', picture())]),
                    archive([('a.png', picture()), ('A.PNG', picture())]), archive([('nested.zip', b'bad')])]:
            with self.assertRaises(Problem):
                unpack_archive(raw)
        with patch('uploads.MAX_EXPANDED', 10):
            with self.assertRaises(Problem):
                unpack_archive(archive([('1.png', picture())]))

    def test_grouped_images_use_distinct_page_numbers_and_origins(self):
        with patch('server.recognize', side_effect=recognized), patch('server.engine', return_value='test'):
            result = server.process_group([('001/2.png', picture()), ('001/10.png', picture())], 'auto')
        self.assertEqual(result['average'], 4.5)
        self.assertEqual([p['number'] for p in result['pages']], [1, 2])
        self.assertEqual([p['source_file'] for p in result['pages']], ['001/2.png', '001/10.png'])

    def test_grouped_document_page_limit(self):
        with self.assertRaises(ValueError):
            server.process_group([(str(n), picture()) for n in range(21)], 'auto')


class API(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ocr = patch('server.recognize', side_effect=recognized)
        self.ocr.start()
        self.engine = patch('server.engine', return_value='test')
        self.engine.start()
        self.http = server.make_server(0, self.temp.name, api_key='test-key')
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join()
        self.engine.stop(); self.ocr.stop(); self.temp.cleanup()

    def request(self, path, data=None, mime='application/zip', key='test-key'):
        headers = {'Content-Type': mime}
        if key is not None: headers['Authorization'] = 'Bearer ' + key
        with urlopen(Request(self.base + path, data, headers), timeout=10) as response:
            return response.status, dict(response.headers), json.load(response)

    def wait(self, path):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.request(path)[2]
            if result['status'] != 'processing': return result
            time.sleep(.01)
        self.fail('Batch timed out')

    def test_submit_status_result_review_and_stale_revision(self):
        status, headers, accepted = self.request('/api/v1/batches', archive([('001/1.png', picture()), ('001/2.png', picture())]))
        self.assertEqual(status, 202)
        self.assertEqual(headers['Location'], accepted['results_url'])
        result = self.wait(accepted['results_url'])
        self.assertEqual(result['status'], 'completed')
        document = result['documents'][0]
        self.assertEqual(document['result']['average'], 4.5)
        self.assertEqual(len(document['pages']), 2)
        path = '/api/v1/documents/' + document['id']
        body = json.dumps({'revision': document['revision'], 'grades': [{**g, 'verified': True} for g in document['grades']], 'complete': True}).encode()
        ready = self.request(path + '/review', body, 'application/json')[2]
        self.assertEqual(ready['calculation']['value'], '4.50')
        with self.assertRaises(HTTPError): self.request(path + '/review', body, 'application/json')
        self.assertEqual(self.request(path)[2]['status'], 'ready')

    def test_one_bad_document_does_not_stop_batch(self):
        result = self.request('/api/v1/batches', archive([('bad/1.png', b'bad'), ('good/1.png', picture())]))[2]
        done = self.wait(result['results_url'])
        self.assertEqual(done['status'], 'completed_with_errors')
        self.assertEqual([d['status'] for d in done['documents']], ['error', 'ready'])

    def test_authentication_and_unknown_batch(self):
        for key in (None, 'wrong'):
            with self.assertRaises(HTTPError) as error: self.request('/api/v1/batches', picture(), 'image/png', key)
            self.assertEqual(error.exception.code, 401)
            with self.assertRaises(HTTPError) as error: self.request('/api/v1/batches/missing', key=key)
            self.assertEqual(error.exception.code, 401)
        with self.assertRaises(HTTPError) as error: self.request('/api/v1/batches/missing')
        self.assertEqual(error.exception.code, 404)

    def test_multipart_upload_and_browser_unpack(self):
        raw = archive([('001/1.png', picture())])
        body = b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="photos.zip"\r\nContent-Type: application/zip\r\n\r\n' + raw + b'\r\n--boundary--\r\n'
        mime = 'multipart/form-data; boundary=boundary'
        accepted = self.request('/api/v1/batches', body, mime)[2]
        self.assertEqual(self.wait(accepted['results_url'])['status'], 'completed')
        unpacked = self.request('/api/archive', body, mime, key=None)[2]
        self.assertEqual(unpacked['documents'][0]['document_key'], '001')

    def test_slow_ocr_leaves_results_readable(self):
        entered, release = threading.Event(), threading.Event()
        def slow(images):
            entered.set(); release.wait(10)
            return recognized(images)
        with patch('server.recognize', side_effect=slow):
            try:
                accepted = self.request('/api/v1/batches', picture(), 'image/png')[2]
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.request(accepted['results_url'])[2]['counts']['processing'], 1)
            finally: release.set()
            self.assertEqual(self.wait(accepted['results_url'])['status'], 'completed')

    def test_origin_and_size_limits(self):
        request = Request(self.base + '/api/archive', archive([('1.png', picture())]), {'Content-Type': 'application/zip', 'Origin': 'https://example.com'})
        with self.assertRaises(HTTPError) as error: urlopen(request)
        self.assertEqual(error.exception.code, 403)
        request = Request(self.base + '/api/v1/batches', b'x', {'Content-Type': 'application/zip', 'Authorization': 'Bearer test-key', 'Content-Length': str(52 * 1024 * 1024)})
        with self.assertRaises(HTTPError) as error: urlopen(request)
        self.assertEqual(error.exception.code, 413)


class Recovery(unittest.TestCase):
    def test_saved_results_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            def processor(files, kind):
                list(files)
                return {'type': 'certificate_9', 'grades': [], 'pages': [], 'average': None}
            service = BatchService(folder, processor)
            accepted = service.submit(picture(), 'photo.png')
            service.close()
            service = BatchService(folder, processor)
            try:
                self.assertEqual(service.batch_result(accepted['batch_id'])['status'], 'completed')
            finally: service.close()


if __name__ == '__main__': unittest.main()
