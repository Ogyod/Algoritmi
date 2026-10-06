"""Local-only HTTP interface and genuine OCR; uploads live in temporary directories."""
import argparse
import base64
import binascii
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PIL import Image, ImageOps, ImageFilter, UnidentifiedImageError
import pypdfium2 as pdfium
from core import DOCUMENT_TYPES, interpret, interpret_lines

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 20 * 1024 * 1024
MAX_BODY = 29 * 1024 * 1024
MAX_PAGES = 20
Image.MAX_IMAGE_PIXELS = 30_000_000
OCR_LOCK = threading.Lock()

def node_environment():
    env = os.environ.copy()
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    node = shutil.which('node') or str(bundled/'bin/node')
    packages = ROOT / 'node_modules'
    if not (packages/'tesseract.js').exists():
        packages = bundled/'node_modules'
    env['NODE_PATH'] = str(packages)
    return node, env, (packages/'tesseract.js').exists()

def engine():
    node, _, installed = node_environment()
    models = all((ROOT / '.models' / f'{lang}.traineddata').is_file() for lang in ['rus', 'eng'])
    return 'Tesseract.js' if installed and Path(node).is_file() and models else None


def recognize(images):
    if not engine():
        raise ValueError('OCR не настроен. Запустите start.command или установите зависимости по README.')
    node, env, _ = node_environment()
    # One worker per document, reused for every page. No model downloads during OCR.
    result = subprocess.run(
        [node, str(ROOT / 'ocr.cjs')], input=json.dumps(images), env=env,
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode:
        raise ValueError('Не удалось распознать документ. Проверьте файл и установку зависимостей.')
    return json.loads(result.stdout)


def process(data, kind):
    if not data or len(data) > MAX_BYTES:
        raise ValueError('Допустимый размер файла — от 1 байта до 20 МБ.')
    if not isinstance(kind, str) or (kind != 'auto' and kind not in DOCUMENT_TYPES):
        raise ValueError('Неизвестный тип документа.')
    pages = []
    images = []
    with tempfile.TemporaryDirectory(prefix='admissions-') as temp:
        def handle_image(image, number):
            image = ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((2400, 3200))
            path = Path(temp) / f'{number}.png'
            image.save(path)
            enhanced = Path(temp) / f'{number}-enhanced.png'
            ImageOps.autocontrast(ImageOps.grayscale(image).filter(
                ImageFilter.GaussianBlur(.8)), cutoff=1).save(enhanced)
            images.append({'path': str(path), 'enhanced_path': str(enhanced),
                           'width': image.width, 'height': image.height})
            image.thumbnail((1000, 1400))
            buffer = io.BytesIO()
            image.save(buffer, 'JPEG', quality=83)
            pages.append({'number': number,
                          'preview': 'data:image/jpeg;base64,'+base64.b64encode(buffer.getvalue()).decode()})
        if data.startswith(b'%PDF-'):
            try:
                doc = pdfium.PdfDocument(data)
            except Exception:
                raise ValueError('Не удалось открыть PDF. Проверьте файл и отсутствие пароля.')
            try:
                if not 1 <= len(doc) <= MAX_PAGES:
                    raise ValueError('PDF должен содержать от 1 до 20 страниц. Разделите большой файл.')
                for i in range(len(doc)):
                    page = doc[i]
                    try:
                        w, h = page.get_size()
                        if w <= 0 or h <= 0:
                            raise ValueError('Некорректный размер страницы PDF.')
                        bitmap = page.render(scale=min(3, 2400/w, 3200/h))
                        try:
                            handle_image(bitmap.to_pil(), i+1)
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
            finally:
                doc.close()
        else:
            try:
                with Image.open(io.BytesIO(data)) as image:
                    if image.format not in ['JPEG', 'PNG', 'TIFF', 'WEBP']:
                        raise ValueError('Поддерживаются PDF, JPEG, PNG, TIFF и WEBP.')
                    if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                        raise ValueError('Размер изображения превышает 30 миллионов пикселей.')
                    frames = getattr(image, 'n_frames', 1)
                    if frames > MAX_PAGES:
                        raise ValueError('Изображение должно содержать не более 20 страниц.')
                    for i in range(frames):
                        image.seek(i)
                        if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                            raise ValueError('Размер страницы изображения превышает 30 миллионов пикселей.')
                        handle_image(image.copy(), i+1)
            except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
                raise ValueError('Не удалось прочитать изображение. Используйте PDF, JPEG, PNG, TIFF или WEBP.')
        recognized = recognize(images)
        if len(recognized) != len(pages):
            raise ValueError('OCR вернул неполный результат.')
        for page, blocks in zip(pages, recognized):
            page['blocks'] = blocks
    result = interpret(pages, kind)
    result['engine'] = engine()
    return result

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.request.settimeout(30)

    def log_message(self, fmt, *args):
        # Avoid logging document names or OCR text.
        return

    def send(self, status, data, mime='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; object-src 'none'; frame-src blob:; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        host = self.headers.get('Host', '')
        expected = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        return host in expected

    def do_GET(self):
        if not self.allowed():
            return self.send(403, {'error': 'Недопустимый адрес запроса.'})
        if self.path == '/api/health':
            return self.send(200, {'ok': True, 'app': 'admissions-document-processor', 'engine': engine(), 'max_bytes': MAX_BYTES})
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/domain.js': ('domain.js', 'text/javascript; charset=utf-8'),
                 '/processor.js': ('processor.js', 'text/javascript; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8'),
                 '/favicon.svg': ('favicon.svg', 'image/svg+xml')}
        item = files.get(self.path)
        if not item:
            return self.send(404, {'error': 'Страница не найдена.'})
        self.send(200, (ROOT/'static'/item[0]).read_bytes(), item[1])

    def do_POST(self):
        if not self.allowed():
            return self.send(403, {'error': 'Недопустимый адрес запроса.'})
        if self.headers.get('Origin') not in [None, f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}']:
            return self.send(403, {'error': 'Запрос разрешён только из локального интерфейса.'})
        if self.path not in ['/api/process', '/api/reinterpret']:
            return self.send(404, {'error': 'Неизвестный запрос.'})
        processing = self.path == '/api/process'
        if processing and not OCR_LOCK.acquire(blocking=False):
            return self.send(429, {'error': 'Другой документ уже обрабатывается. Повторите позже.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                return self.send(413, {'error': 'Файл слишком большой. Максимум — 20 МБ.'})
            if 'application/json' not in self.headers.get('Content-Type', ''):
                return self.send(415, {'error': 'Ожидается JSON.'})
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
                return self.send(200, interpret_lines(lines, body.get('type', 'auto')))
            if not isinstance(body, dict) or not isinstance(body.get('data'), str):
                raise ValueError('Не переданы данные файла.')
            data = base64.b64decode(body['data'], validate=True)
            result = process(data, body.get('type', 'auto'))
            self.send(200, result)
        except (binascii.Error, json.JSONDecodeError):
            self.send(422, {'error': 'Некорректные данные файла или запроса.'})
        except subprocess.TimeoutExpired:
            self.send(422, {'error': 'Распознавание заняло слишком много времени. Загрузите файл меньшего размера.'})
        except ValueError as exc:
            self.send(422, {'error': str(exc)})
        except Exception:
            self.send(500, {'error': 'Не удалось обработать файл. Попробуйте другой скан.'})
        finally:
            if processing:
                OCR_LOCK.release()

def make_server(port=8765):
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true', help='Open the local interface in the browser')
    args = parser.parse_args()
    server = make_server(args.port)
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
