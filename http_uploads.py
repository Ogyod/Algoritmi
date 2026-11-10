"""HTTP upload parsing shared by the browser and integration API."""
from email import policy
from email.parser import BytesParser
from pathlib import PurePosixPath

from uploads import Problem, MAX_UPLOAD


class HTTPProblem(Problem):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def read_body(handler, limit):
    if handler.headers.get('Transfer-Encoding'):
        raise HTTPProblem('Передавайте файл с Content-Length.', 411)
    try:
        lengths = handler.headers.get_all('Content-Length', [])
        if len(lengths) != 1:
            raise ValueError
        size = int(lengths[0])
    except ValueError:
        raise HTTPProblem('Нужен корректный Content-Length.', 411)
    if size <= 0:
        raise HTTPProblem('Тело запроса пустое.')
    if size > limit:
        raise HTTPProblem('Размер запроса превышает допустимый.', 413)
    handler.connection.settimeout(30)
    body = handler.rfile.read(size)
    if len(body) != size:
        raise HTTPProblem('Файл передан не полностью.')
    return body


def read_upload(handler):
    body = read_body(handler, MAX_UPLOAD + 1024 * 1024)
    content_type = handler.headers.get('Content-Type', '')
    mime = content_type.split(';', 1)[0].strip().lower()
    if mime == 'multipart/form-data':
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode('utf-8') + body)
        if not message.is_multipart() or message.defects:
            raise HTTPProblem('Некорректный multipart/form-data.')
        parts = list(message.iter_parts())
        if len(parts) != 1:
            raise HTTPProblem('Передайте один файл в поле file.')
        part = parts[0]
        if (part.is_multipart() or part.defects or part.get_content_disposition() != 'form-data' or
                part.get_param('name', header='content-disposition') != 'file' or not part.get_filename()):
            raise HTTPProblem('Передайте один файл в поле file.')
        filename = PurePosixPath(part.get_filename().replace('\\', '/')).name
        raw = part.get_payload(decode=True)
    else:
        names = {'application/zip': 'upload.zip', 'application/x-zip-compressed': 'upload.zip',
                 'application/pdf': 'upload.pdf', 'image/png': 'upload.png',
                 'image/jpeg': 'upload.jpg', 'image/tiff': 'upload.tiff', 'image/webp': 'upload.webp'}
        if mime not in names:
            raise HTTPProblem('Нужен multipart/form-data, application/zip, PDF или изображение.', 415)
        filename, raw = names[mime], body
    if not raw:
        raise HTTPProblem('Файл пустой.')
    if len(raw) > MAX_UPLOAD:
        raise HTTPProblem('Файл превышает 50 МБ.', 413)
    return raw, filename
