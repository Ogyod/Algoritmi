"""Bounded ZIP uploads. Archive paths are labels, never filesystem destinations."""
import hashlib
import io
from pathlib import PurePosixPath
import re
import stat
import zipfile
import zlib

class Problem(ValueError):
    pass

MAX_UPLOAD = 50 * 1024 * 1024
MAX_FILE = 20 * 1024 * 1024
MAX_EXPANDED = 200 * 1024 * 1024
MAX_FILES = 200
EXTENSIONS = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp', '.pdf'}


def natural_key(name):
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r'(\d+)', name)]


def safe_name(name):
    name = name.replace('\\', '/')
    parts = name.rstrip('/').split('/')
    if (not name or len(name) > 512 or name.startswith('/') or
            any(p in ('', '.', '..') for p in parts) or
            any(ord(c) < 32 or c == ':' for c in name)):
        raise Problem('ZIP содержит недопустимый путь файла.')
    return '/'.join(parts)


def prepare_upload(raw, filename, staging):
    """Write to numbered staging folders; validate the entire archive before import."""
    if not raw or len(raw) > MAX_UPLOAD:
        raise Problem('Загрузите непустой файл размером до 50 МБ.')
    filename = safe_name(filename)
    skipped, groups = [], {}

    def store(name, content, grouped):
        if not content or len(content) > MAX_FILE:
            raise Problem('Каждый документ должен быть непустым и не превышать 20 МБ.')
        parent = str(PurePosixPath(name).parent)
        key = parent if grouped and parent != '.' else name
        if key not in groups:
            folder = staging / str(len(groups))
            folder.mkdir()
            groups[key] = {'document_key': key, 'folder': folder, 'source_files': [], 'hashes': []}
        group = groups[key]
        (group['folder'] / f'{len(group["source_files"])}.bin').write_bytes(content)
        group['source_files'].append(name)
        group['hashes'].append(hashlib.sha256(content).hexdigest())

    if PurePosixPath(filename).suffix.lower() != '.zip':
        if PurePosixPath(filename).suffix.lower() not in EXTENSIONS:
            raise Problem('Поддерживаются ZIP, PDF, PNG, JPEG и TIFF.')
        store(filename, raw, False)
        return list(groups.values()), skipped

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 500 or sum(i.file_size for i in entries) > MAX_EXPANDED:
                raise Problem('ZIP превышает лимит: 500 записей или 200 МБ после распаковки.')
            files, seen = [], set()
            for entry in entries:
                name = safe_name(entry.filename)
                identity = name.casefold()
                if identity in seen:
                    raise Problem('ZIP содержит повторяющиеся пути файлов.')
                seen.add(identity)
                mode = entry.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise Problem('ZIP содержит ссылку или специальный файл.')
                if entry.flag_bits & 1:
                    raise Problem('ZIP с паролем не поддерживается.')
                if entry.is_dir():
                    continue
                if entry.file_size > MAX_FILE:
                    raise Problem('Файл внутри ZIP превышает 20 МБ.')
                if (any(p.startswith('.') or p == '__MACOSX' for p in name.split('/')) or
                        PurePosixPath(name).name.casefold() == 'thumbs.db' or
                        PurePosixPath(name).suffix.lower() not in EXTENSIONS):
                    skipped.append(name)
                    continue
                files.append((name, entry))
            if not 1 <= len(files) <= MAX_FILES:
                raise Problem('ZIP должен содержать от 1 до 200 файлов PDF/PNG/JPEG/TIFF.')
            for name, entry in sorted(files, key=lambda item: natural_key(item[0])):
                with archive.open(entry) as member:
                    content = member.read(MAX_FILE + 1)
                store(name, content, True)
    except Problem:
        raise
    except (zipfile.BadZipFile, zlib.error, OSError, RuntimeError, ValueError, NotImplementedError):
        raise Problem('Не удалось прочитать ZIP: архив повреждён или его формат не поддерживается.')
    return list(groups.values()), skipped


def unpack_archive(raw, filename='upload.zip'):
    """The same bounded ZIP validation in Python and the browser Python worker."""
    import base64
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix='admissions-zip-') as folder:
        groups, skipped = prepare_upload(raw, filename, Path(folder))
        return {'documents': [
            {'document_key': group['document_key'],
             'files': [{'name': name, 'data': base64.b64encode(
                 (group['folder'] / f'{n}.bin').read_bytes()).decode('ascii')}
                       for n, name in enumerate(group['source_files'])]}
            for group in groups], 'skipped_files': skipped}
