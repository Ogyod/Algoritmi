"""Validate personal-cabinet submissions and project OCR into fields the cabinet needs."""
import base64
import binascii
import hashlib
from pathlib import PurePosixPath
import re

from core import CERTIFICATE_TYPES, DOCUMENT_TYPES, classify, extract_identifiers, mean_grade, normalize
from ocr_adapter import parse_engine_rows
from ocr_consensus import OCR_THRESHOLD, normalized, reconcile
from ocr_engines import add_docling_suggestions
from regions import issuing_region
from uploads import EXTENSIONS, MAX_FILE, MAX_FILES, MAX_UPLOAD, Problem, safe_name

FIELDS = ('series', 'number', 'average_grade', 'region_code')
GRADE_FIELD = 'average_grade'
START = re.compile(r'наименован[иея]+ учебных предметов|итогов\w* (?:отмет\w*|оцен\w*)')
END = re.compile(r'\bдополнительные сведения\b')
APPENDIX = re.compile(r'\bприложение к аттестату\b')
FOREIGN = re.compile(r'нотариальн\w* перевод|перевод с .{2,40} языка|'
                     r'\b(?:high school diploma|general certificate of secondary education)\b')
PAGINATION = re.compile(r'\b(?:страница|стр|лист)\.?\s*(\d+)\s*(?:из|/)\s*(\d+)\b', re.I)
SERIES_VALUE = r'([А-ЯA-Z]{1,4}(?:-[А-ЯA-Z]{1,4})?|\d{2,8})'
SERIES = re.compile(r'^серия\s*[:№]?\s*' + SERIES_VALUE + r'(?=\s|$)', re.I)


def external_id(value):
    if type(value) is int and 0 <= value <= 2**53 - 1:
        return str(value)
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip() or any(ord(c) < 32 for c in value):
        raise Problem('Передайте ID абитуриента и ID каждого документа.')
    return value


def stage_submission(data, staging):
    if not isinstance(data, dict):
        raise Problem('Ожидается JSON-объект.')
    applicant = external_id(data.get('applicant_id'))
    documents = data.get('documents')
    if not isinstance(documents, list) or not 1 <= len(documents) <= MAX_FILES:
        raise Problem('Передайте от 1 до 200 документов одного абитуриента.')
    seen, groups = set(), []
    total_files = total_bytes = 0
    for document in documents:
        if not isinstance(document, dict):
            raise Problem('Некорректные данные документа.')
        document_id = external_id(document.get('document_id'))
        if document_id in seen:
            raise Problem('Укажите каждый ID документа один раз, объединив его файлы.')
        seen.add(document_id)
        fields = document.get('fields')
        if not isinstance(fields, dict) or set(fields) - set(FIELDS):
            raise Problem('Передайте текущие поля ЛК в fields.')
        for value in fields.values():
            if value is None or (isinstance(value, str) and len(value) <= 200):
                continue
            if type(value) in (int, float) and -10**20 <= value <= 10**20:
                continue
            raise Problem('Некорректное значение поля ЛК.')
        if document.get('complete') is not None and type(document['complete']) is not bool:
            raise Problem('complete принимает true, false или null.')
        kind = document.get('type', 'auto')
        if not isinstance(kind, str) or kind not in set(DOCUMENT_TYPES) | {'auto', 'foreign'}:
            raise Problem('Неизвестный тип документа.')
        files = document.get('files')
        if not isinstance(files, list) or not 1 <= len(files) <= 20:
            raise Problem('Передайте от 1 до 20 файлов для каждого документа.')
        folder = staging / str(len(groups))
        folder.mkdir()
        names, roles, hashes = [], [], []
        seen_parts = set()
        for file in files:
            if (not isinstance(file, dict) or not isinstance(file.get('name'), str)
                    or not isinstance(file.get('data'), str)):
                raise Problem('Укажите name и data (Base64) для каждого файла.')
            name = safe_name(file['name'])
            role = file.get('role', 'auto')
            if role not in ('auto', 'certificate', 'appendix') or PurePosixPath(name).suffix.lower() not in EXTENSIONS:
                raise Problem('Некорректная роль или расширение файла.')
            total_files += 1
            if total_files > MAX_FILES or len(file['data']) > 4 * ((MAX_FILE + 2) // 3):
                raise Problem('Допускается до 200 файлов, каждый до 20 МБ.')
            try:
                raw = base64.b64decode(file['data'], validate=True)
            except (ValueError, binascii.Error):
                raise Problem('Файл должен быть закодирован в Base64.') from None
            total_bytes += len(raw)
            if not 1 <= len(raw) <= MAX_FILE or total_bytes > MAX_UPLOAD:
                raise Problem('Допускается до 20 МБ на файл и 50 МБ на пакет.')
            digest = hashlib.sha256(raw).digest()
            if (digest, role) in seen_parts:
                continue
            seen_parts.add((digest, role))
            (folder / f'{len(names)}.bin').write_bytes(raw)
            names.append(name)
            roles.append(role)
            hashes.append(digest.hex())
        # Put an explicitly labelled title first, preserving order within each role.
        order = sorted(range(len(names)), key=lambda i: roles[i] != 'certificate')
        fingerprint = hashlib.sha256(''.join(f'{roles[i]}:{hashes[i]};' for i in order).encode()).hexdigest()
        missing = [key for key in FIELDS if fields.get(key) is None or
                   (isinstance(fields[key], str) and not fields[key].strip())]
        groups.append({'folder': folder, 'document_key': document_id, 'source_files': names,
                       'requested_type': kind, 'lk': {'applicant_id': applicant, 'document_id': document_id,
                       'fingerprint': fingerprint, 'missing': missing, 'complete': document.get('complete'),
                       'roles': roles, 'order': order}})
    return applicant, groups


def ordered_lines(lines):
    return sorted(lines, key=lambda row: (row['page'], (row.get('box') or [0, 0])[1],
                                         (row.get('box') or [0])[0]))


def sections(lines):
    start = next((i for i, line in enumerate(lines) if START.search(normalized(line['text']))), None)
    end = next((i for i, line in enumerate(lines) if END.search(normalized(line['text']))), None)
    return start, end


def page_title(lines):
    text = ' '.join(line['text'] for line in lines)
    return classify(text) if not APPENDIX.search(normalized(text)) else 'unknown'


def standalone_series(lines):
    for index, line in enumerate(lines):
        text = line['text'].strip()
        if re.search(r'предыдущий документ', normalize(text)):
            return
        match = SERIES.match(text)
        if match and match[1].upper() not in {'НОМЕ', 'NOME'}:
            yield match[1].upper(), line['confidence']
        elif re.fullmatch(r'серия\s*[:№]?', text, re.I) and index + 1 < len(lines):
            following = lines[index + 1]
            if following['page'] == line['page'] and (match := re.fullmatch(SERIES_VALUE, following['text'].strip())):
                yield match[1].upper(), min(line['confidence'], following['confidence'])


def read_identifiers(readings):
    candidates = [row for lines in readings for row in extract_identifiers(lines)[0]['identifier_candidates']]
    accepted = [row for lines in readings
                for row in extract_identifiers([line for line in lines if line['confidence'] >= OCR_THRESHOLD])[0]
                                              ['identifier_candidates']]
    series = [pair for lines in readings for pair in standalone_series(lines)]
    fields = {}
    for field in ('series', 'number'):
        values = {row[field] for row in candidates if row[field]}
        supported = {row[field] for row in accepted if row[field]}
        if field == 'series':
            values.update(value for value, _ in series)
            supported.update(value for value, confidence in series if confidence >= OCR_THRESHOLD)
        if len(values) == 1 and values <= supported:
            fields[field] = values.pop()
    return fields


def scoped_grades(observations, pages, table_rows):
    located = [(o, *sections(o['lines'])) for o in observations]
    starts = {o['lines'][start]['page'] for o, start, _ in located if start is not None}
    ends = {o['lines'][end]['page'] for o, _, end in located if end is not None}
    if len(starts) != 1 or len(ends) != 1:
        return None, 'Не найдены однозначные границы таблицы оценок.', False
    first, last = next(iter(starts)), next(iter(ends))
    if first > last:
        return None, 'Нарушен порядок страниц приложения.', False
    scoped = []
    for observation, start, end in located:
        lines = []
        for index, line in enumerate(observation['lines']):
            page = line['page']
            if not first <= page <= last:
                continue
            if page == first and (start is None or index <= start):
                continue
            if page == last and (end is None or index >= end):
                continue
            lines.append(line)
        scoped.append({**observation, 'lines': lines})
    markers = {}
    for o, _, _ in located:
        for line in o['lines']:
            if line['confidence'] >= OCR_THRESHOLD and (match := PAGINATION.search(line['text'])):
                markers.setdefault(line['page'], set()).add(tuple(map(int, match.groups())))
    if markers:
        totals = {total for marks in markers.values() for _, total in marks}
        if (len(totals) != 1 or not 1 <= next(iter(totals)) <= len(pages) or any(len(marks) != 1 for marks in markers.values())
                or [next(iter(marks))[0] for _, marks in sorted(markers.items())]
                != list(range(1, next(iter(totals)) + 1))):
            return None, 'По нумерации комплект страниц неполный.', False
    if pages - {line['page'] for o in observations for line in o['lines']}:
        return None, 'Не все страницы комплекта удалось прочитать.', False
    if not set(range(first, last)) <= {line['page'] for o in scoped for line in o['lines']}:
        return None, 'Не все страницы таблицы удалось прочитать.', False
    # Keep the table heading as parser context so unknown/unreadable rows are flagged.
    def parse(lines):
        return parse_engine_rows([{'page': page, 'text': 'Итоговые оценки', 'confidence': 1}
                                  for page in {line['page'] for line in lines}] + lines)
    rows = reconcile(scoped, parser=parse)
    # On boundary pages a table export has no line coordinates, so it cannot add rows.
    add_docling_suggestions(rows, [row for row in table_rows if first < row['page'] < last], parse_engine_rows)
    if not rows or any(not row['auto_verified'] for row in rows):
        return None, 'В таблице есть оценки, которые не удалось надёжно сверить.', False
    return mean_grade(rows), '', bool(markers)


def analyze(recognition, pages, full):
    observations = [{**o, 'lines': ordered_lines(o['lines'])}
                    for o in recognition['observations'] if o['ok'] and o['lines']]
    readings = [o['lines'] for o in observations]
    all_read = pages <= {line['page'] for lines in readings for line in lines}
    if not readings:
        return {'type': 'unknown', 'issues': {'document': 'Текст документа не читается.'}, 'fields': {}, 'cacheable': False}
    if any(FOREIGN.search(normalized(' '.join(line['text'] for line in lines))) for lines in readings):
        return manual('foreign', 'Иностранный аттестат или перевод обрабатывается вручную.', cacheable=True)
    title_readings = []
    types = set()
    for lines in readings:
        for page in {line['page'] for line in lines}:
            page_lines = [line for line in lines if line['page'] == page]
            kind = page_title([line for line in page_lines if line['confidence'] >= OCR_THRESHOLD])
            if kind in CERTIFICATE_TYPES:
                types.add(kind)
                title_readings.append(page_lines)
    if len(types) != 1:
        return manual('unknown', 'Не удалось уверенно определить тип российского аттестата по титульной части.',
                      cacheable=all_read)
    fields = read_identifiers(title_readings)
    codes = {code for lines in title_readings for code in issuing_region(lines)}
    if len(codes) == 1:
        fields['region_code'] = codes.pop()
    issues = {}
    numbered_complete = False
    if full:
        value, reason, numbered_complete = scoped_grades(observations, pages, recognition.get('table_rows', []))
        if reason:
            issues[GRADE_FIELD] = reason
        else:
            fields[GRADE_FIELD] = value
    return {'type': types.pop(), 'fields': fields, 'issues': issues, 'numbered_complete': numbered_complete, 'cacheable': all_read}


def manual(kind, reason, *, cacheable=False):
    return {'type': kind, 'fields': {}, 'issues': {'document': reason}, 'cacheable': cacheable}


def project(analysis, lk):
    missing = lk['missing']
    issues = analysis['issues']
    patch, review = {}, []
    if 'document' in issues:
        return patch, [{'field': 'document', 'reason': issues['document']}]
    for field in missing:
        if field == GRADE_FIELD and lk['complete'] is False:
            review.append({'field': field, 'reason': 'ЛК указал, что комплект аттестата неполный.'})
        elif (field == GRADE_FIELD and field in analysis['fields']
              and lk['complete'] is not True and not analysis.get('numbered_complete')):
            review.append({'field': field, 'reason': 'Полнота комплекта аттестата не подтверждена.'})
        elif field in analysis['fields']:
            patch[field] = analysis['fields'][field]
        else:
            review.append({'field': field, 'reason': issues.get(field, 'Значение не удалось определить уверенно.')})
    return patch, review
