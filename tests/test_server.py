import sys
import json
import threading
from pathlib import Path
import unittest
from urllib.request import urlopen, Request
from urllib.error import HTTPError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = server.make_server(0)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.http.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def post(self, path, body, extra_headers=None):
        headers = {'Content-Type': 'application/json', **(extra_headers or {})}
        return urlopen(Request(self.url + path, data=json.dumps(body).encode(), headers=headers), timeout=10)

    def test_frontend_and_health(self):
        for path in ['/', '/app.js', '/domain.js', '/api/health']:
            with urlopen(self.url + path) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read())

    def test_invalid_file_data_and_lock_release(self):
        for body in [{'data': '!!!'}, {'data': ''}, {'data': 'dGVzdA==', 'type': 'passport'}, {'data': 'dGVzdA==', 'type': []}]:
            with self.assertRaises(HTTPError) as error:
                self.post('/api/process', body)
            self.assertEqual(error.exception.code, 422)
            self.assertFalse(server.OCR_LOCK.locked())

    def test_busy_ocr_returns_429(self):
        server.OCR_LOCK.acquire()
        try:
            with self.assertRaises(HTTPError) as error: self.post('/api/process', {'data': ''})
            self.assertEqual(error.exception.code, 429)
        finally: server.OCR_LOCK.release()

    def test_origin_restriction(self):
        with self.assertRaises(HTTPError) as error:
            self.post('/api/process', {'data': ''}, {'Origin': 'https://example.com'})
        self.assertEqual(error.exception.code, 403)

    def test_category_correction_and_invalid_lines(self):
        lines = [{'text': 'Серия АБ Номер 0012345', 'page': 1, 'confidence': .9}]
        with self.post('/api/reinterpret', {'lines': lines, 'type': 'diploma_master'}) as response:
            result = json.load(response)
        self.assertEqual(result['document_number'], '0012345')
        self.assertEqual(result['type'], 'diploma_master')
        with self.assertRaises(HTTPError) as error:
            self.post('/api/reinterpret', {'lines': [{'text': [], 'page': 1, 'confidence': 1}], 'type': 'diploma_master'})
        self.assertEqual(error.exception.code, 422)

    def test_export_attachment_utf8_and_single_use(self):
        with self.post('/api/export', {'format': 'json', 'content': '{"номер":"0012345"}'}) as response:
            link = json.load(response)['url']
        with urlopen(self.url + link) as response:
            self.assertIn('attachment', response.headers['Content-Disposition'])
            self.assertEqual(json.load(response)['номер'], '0012345')
        with self.assertRaises(HTTPError) as error: urlopen(self.url + link)
        self.assertEqual(error.exception.code, 404)


if __name__ == '__main__': unittest.main()
