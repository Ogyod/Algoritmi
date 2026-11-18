"""Local-only HTTP interface and genuine OCR; uploads live in temporary directories."""
import argparse
import base64
import binascii
import json
import os
import secrets
from urllib.parse import urlparse
from pathlib import Path
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from core import interpret_lines
from document_processing import MAX_PAGES, process, process_group
from ocr_engines import MODEL_ROOT
from ocr_process import shutdown_workers
from ocr_worker import native_tesseract, tesseract_node
from uploads import MAX_FILE, Problem, unpack_archive
from http_uploads import HTTPProblem, read_body, read_upload
from batch_service import BatchService

ROOT = Path(__file__).resolve().parent
MAX_BODY = 29 * 1024 * 1024
OCR_LOCK = threading.Lock()

def tesseract_js_available():
    node, packages = tesseract_node()
    tessdata = all((ROOT / '.models' / f'{lang}.traineddata').is_file() for lang in ('rus', 'eng'))
    return Path(node).is_file() and (packages / 'tesseract.js').is_dir() and tessdata

def engine():
    native = native_tesseract()
    tesseract = bool(native and (Path(native).is_file() or shutil.which(native))) if native else tesseract_js_available()
    rapid = (MODEL_ROOT / 'rapid-ready.json').is_file()
    easy = all((MODEL_ROOT / 'easyocr' / name).is_file() for name in ('craft_mlt_25k.pth', 'cyrillic_g2.pth'))
    if rapid and tesseract:
        return 'PaddleOCR + Tesseract → EasyOCR' if easy else 'PaddleOCR + Tesseract · EasyOCR требует настройки'
    if rapid:
        return 'PaddleOCR · Tesseract требует настройки'
    if tesseract:
        return 'Tesseract · порог 80%' if native else 'Tesseract.js · порог 80%'
    return None


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.request.settimeout(30)

    def log_message(self, fmt, *args):
        # Avoid logging document names or OCR text.
        return

    def send(self, status, data, mime='application/json; charset=utf-8', headers=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; object-src 'none'; frame-src blob:; base-uri 'none'; form-action 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        host = self.headers.get('Host', '')
        expected = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        return host in expected

    def do_GET(self):
        if not self.allowed():
            return self.send(403, {'error': 'Недопустимый адрес запроса.'})
        if self.path.startswith('/api/v1/'):
            return self.integration_request()
        if self.path == '/api/health':
            return self.send(200, {'ok': True, 'app': 'admissions-document-processor', 'engine': engine(), 'max_bytes': MAX_FILE,
                                   'ocr_ready': getattr(self.server, 'ocr_ready', False),
                                   'docling_ready': getattr(self.server, 'docling_ready', False)})
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/domain.js': ('domain.js', 'text/javascript; charset=utf-8'),
                 '/processor.js': ('processor.js', 'text/javascript; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8'),
                 '/favicon.svg': ('favicon.svg', 'image/svg+xml'),
                 '/display-font.ttf': ('display-font.ttf', 'font/ttf')}
        item = files.get(self.path)
        if not item:
            return self.send(404, {'error': 'Страница не найдена.'})
        self.send(200, (ROOT/'static'/item[0]).read_bytes(), item[1])

    def do_POST(self):
        if not self.allowed():
            return self.send(403, {'error': 'Недопустимый адрес запроса.'})
        if self.headers.get('Origin') not in [None, f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}']:
            return self.send(403, {'error': 'Запрос разрешён только из локального интерфейса.'})
        if self.path.startswith('/api/v1/') or self.path == '/api/archive':
            return self.integration_request()
        if self.path not in ['/api/process', '/api/process-group', '/api/reinterpret']:
            return self.send(404, {'error': 'Неизвестный запрос.'})
        processing = self.path in ['/api/process', '/api/process-group']
        if processing and not OCR_LOCK.acquire(blocking=False):
            return self.send(429, {'error': 'Другой документ уже обрабатывается. Повторите позже.'})
        try:
            status, result = self.read_request()
        except (binascii.Error, json.JSONDecodeError):
            status, result = 422, {'error': 'Некорректные данные файла или запроса.'}
        except subprocess.TimeoutExpired:
            status, result = 422, {'error': 'Распознавание заняло слишком много времени. Загрузите файл меньшего размера.'}
        except ValueError as exc:
            status, result = 422, {'error': str(exc)}
        except Exception:
            status, result = 500, {'error': 'Не удалось обработать файл. Попробуйте другой скан.'}
        finally:
            if processing:
                OCR_LOCK.release()
        # A completed response must allow the next request immediately.
        self.send(status, result)

    def integration_request(self):
        try:
            path = urlparse(self.path).path
            if path != '/api/archive':
                if not self.server.api_key:
                    raise HTTPProblem('API отключён. Установите APP_API_KEY перед запуском.', 503)
                supplied = self.headers.get('Authorization', '').encode('utf-8')
                expected = ('Bearer ' + self.server.api_key).encode('utf-8')
                if not secrets.compare_digest(supplied, expected):
                    raise HTTPProblem('Нужен корректный Authorization: Bearer <APP_API_KEY>.', 401)
            parts = path.strip('/').split('/')
            service = self.server.batches
            if self.command == 'POST' and path == '/api/archive':
                raw, filename = read_upload(self)
                return self.send(200, unpack_archive(raw, filename))
            if self.command == 'POST' and path == '/api/v1/batches':
                raw, filename = read_upload(self)
                result = service.submit(raw, filename)
                return self.send(202, result, headers={'Location': result['results_url'], 'Retry-After': '2'})
            if self.command == 'GET' and len(parts) == 4 and parts[:3] == ['api', 'v1', 'batches']:
                try:
                    return self.send(200, service.batch_result(parts[3]))
                except Problem as error:
                    raise HTTPProblem(str(error), 404)
            if self.command == 'GET' and len(parts) == 4 and parts[:3] == ['api', 'v1', 'documents']:
                try:
                    return self.send(200, service.document_result(parts[3]))
                except Problem as error:
                    raise HTTPProblem(str(error), 404)
            if self.command == 'POST' and len(parts) == 5 and parts[:3] == ['api', 'v1', 'documents'] and parts[4] == 'review':
                data = json.loads(read_body(self, 1024 * 1024))
                if not isinstance(data, dict):
                    raise HTTPProblem('Нужен JSON-объект.')
                return self.send(200, service.review(parts[3], data))
            raise HTTPProblem('Ресурс API не найден.', 404)
        except HTTPProblem as error:
            return self.send(error.status, {'error': str(error)})
        except Problem as error:
            return self.send(400, {'error': str(error)})
        except (ValueError, TypeError, KeyError):
            return self.send(400, {'error': 'Некорректный файл, архив или данные запроса.'})
        except Exception:
            return self.send(500, {'error': 'Не удалось выполнить операцию. Прочитайте сохранённый результат.'})

    def read_request(self):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= (280 * 1024 * 1024 if self.path == '/api/process-group' else MAX_BODY):
            return 413, {'error': 'Файл слишком большой. Максимум — 20 МБ.'}
        if 'application/json' not in self.headers.get('Content-Type', ''):
            return 415, {'error': 'Ожидается JSON.'}
        body = json.loads(self.rfile.read(length))
        if self.path == '/api/reinterpret':
            lines = body.get('lines') if isinstance(body, dict) else None
            if not isinstance(lines, list) or len(lines) > 5000:
                raise ValueError('Некорректный распознанный текст.')
            for line in lines:
                if (not isinstance(line, dict) or not isinstance(line.get('text'), str)
                        or len(line['text']) > 5000
                        or not isinstance(line.get('page'), int)
                        or not isinstance(line.get('confidence'), (float, int))
                        or not 0 <= line['confidence'] <= 1):
                    raise ValueError('Некорректная строка распознанного текста.')
            return 200, interpret_lines(lines, body.get('type', 'auto'))
        if self.path == '/api/process-group':
            files = body.get('files') if isinstance(body, dict) else None
            if not isinstance(files, list) or not 1 <= len(files) <= MAX_PAGES:
                raise ValueError('В одном документе допускается до 20 файлов.')
            decoded = []
            for item in files:
                if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('data'), str):
                    raise ValueError('Некорректная страница документа.')
                decoded.append((item['name'], base64.b64decode(item['data'], validate=True)))
            return 200, process_group(decoded, body.get('type', 'auto'))
        if not isinstance(body, dict) or not isinstance(body.get('data'), str):
            raise ValueError('Не переданы данные файла.')
        data = base64.b64decode(body['data'], validate=True)
        return 200, process(data, body.get('type', 'auto'))

def process_api_group(files, kind):
    with OCR_LOCK:
        return process_group(files, kind)


class Server(ThreadingHTTPServer):
    def server_close(self):
        super().server_close()
        self.batches.close()
        shutdown_workers()


def make_server(port=8765, root=None, api_key=None):
    http = Server(('127.0.0.1', port), Handler)
    http.api_key = api_key if api_key is not None else os.environ.get('APP_API_KEY', '')
    http.batches = BatchService(root or ROOT / 'data', process_api_group)
    return http


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true', help='Open the local interface in the browser')
    args = parser.parse_args()
    server = make_server(args.port)
    from ocr_engines import warmup, warmup_docling
    def prepare():
        server.ocr_ready = all(warmup().values())
        print('OCR: готов к обработке' if server.ocr_ready else 'OCR: проверьте локальные модели', flush=True)
        server.docling_ready = warmup_docling()
        print('Docling: готов к обработке' if server.docling_ready else 'Docling: проверьте локальные модели', flush=True)
    threading.Thread(target=prepare, daemon=True, name='ocr-warmup').start()
    print(f'Приём · Документы: http://127.0.0.1:{args.port}', flush=True)
    print(f'OCR: {engine() or "не настроен"}', flush=True)
    if args.open:
        import webbrowser
        threading.Timer(.7, lambda: webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
