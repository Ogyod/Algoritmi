"""Attach independent OCR votes to Algoritmi's existing document interpretation."""
from core import DOCUMENT_TYPES, CERTIFICATE_TYPES, SUBJECT_CATALOG, extract_grades, interpret_lines, mean_grade
from ocr_consensus import parse_lines, normalized

SUBJECT_HINTS = tuple(normalized(alias) for name, aliases in SUBJECT_CATALOG.items() for alias in (name, *aliases))

GRADE_WARNINGS = {
    'Оценки не найдены. Для среднего балла добавьте приложение к аттестату.',
    'Неоднозначные оценки выделены. Сверьте их со сканом.',
    'Найдены предметы вне справочника. Их названия выделены для проверки.',
    'В таблице есть строки без надёжной оценки. Заполните их для расчёта среднего балла.',
    'Повторяются предметы. Удалите дубликаты для расчёта среднего балла.',
}


def parse_engine_rows(lines):
    rows = parse_lines(lines, SUBJECT_HINTS)
    result = []
    for row in rows:
        # Retain the existing canonical subject catalogue and unknown-subject policy.
        canonical = extract_grades([
            {'text': 'Итоговые оценки', 'page': row['page'], 'confidence': 1},
            {'text': row['source'], 'page': row['page'], 'confidence': row['confidence']},
        ])
        if not canonical:
            continue
        parsed = canonical[0]
        conflict = row['conflict'] or parsed['conflict']
        result.append({**row, **parsed, 'grade': None if conflict else parsed['grade'],
                       'conflict': conflict, 'page': row['page'], 'box': row.get('box')})
    # Preserve additional unreadable/unknown rows found by the project's original parser.
    covered = {(row['page'], normalized(row['subject'])) for row in result}
    for parsed in extract_grades(lines):
        line = next((line for line in lines if line['text'] == parsed['source']), None)
        if line and (line['page'], normalized(parsed['subject'])) not in covered:
            result.append({**parsed, 'page': line['page'], 'box': line.get('box'),
                           'verified': False, 'include': True, 'reason': ''})
            covered.add((line['page'], normalized(parsed['subject'])))
    return result


def interpret_consensus(recognition, kind='auto'):
    available = [o for o in recognition['observations'] if o['ok'] and o['lines']]
    candidates = [interpret_lines(o['lines'], kind) for o in available]
    def usefulness(result):
        return (8 * (result['type'] in DOCUMENT_TYPES) + 2 * bool(result['document_number'])
                + bool(result['grades']))
    result = max(candidates, key=usefulness) if candidates else interpret_lines([], kind)
    result['ocr'] = recognition['ocr']
    names = {'rapidocr': 'PaddleOCR', 'tesseract': 'Tesseract', 'easyocr': 'EasyOCR'}
    result['engine'] = ' + '.join(names[o['engine']] for o in recognition['observations'] if o['ok']) or 'OCR недоступен'
    result['auto_grades_verified'] = False
    result['auto_document_verified'] = False
    if result['type'] in CERTIFICATE_TYPES or result['type'] == 'education_unknown':
        result['grades'] = recognition['grades']
        agreed = bool(result['grades']) and all(row['auto_verified'] for row in result['grades'])
        result['auto_grades_verified'] = agreed
        result['average'] = mean_grade(result['grades']) if agreed and result['type'] in CERTIFICATE_TYPES else None
        result['warnings'] = [warning for warning in result['warnings'] if warning not in GRADE_WARNINGS]
        if agreed:
            message = 'Предметы и оценки согласованы двумя независимыми OCR.'
            if recognition['ocr']['third_used']:
                message += ' Расхождения проверены третьим распознавателем EasyOCR.'
            if result['average'] is not None:
                message += ' Средний балл рассчитан автоматически.'
            matching = [candidate for candidate in candidates
                        if candidate['type'] == result['type']
                        and candidate.get('document_number') == result.get('document_number')
                        and candidate.get('document_series', '') == result.get('document_series', '')]
            result['auto_document_verified'] = (result['type'] in CERTIFICATE_TYPES
                                                and bool(result.get('document_number'))
                                                and len(matching) >= 2)
            message += (' Документ принят автоматически.' if result['auto_document_verified'] else
                        ' Реквизиты не согласованы двумя OCR: проверьте их отдельно.')
            result['warnings'].append(message)
        elif result['grades']:
            result['warnings'].append(f"Нет согласия двух OCR для {recognition['ocr']['unresolved']} строк. Варианты движков доступны в подсказке строки; спорные оценки заполните по скану.")
        else:
            result['warnings'].append('Оценки не согласованы. Загрузите приложение с оценками или внесите строки вручную.')
    result['warnings'].extend(o['error'] for o in recognition['observations'] if not o['ok'])
    # Automatic grade agreement does not claim that series, number or category were manually reviewed.
    result['reviewed'] = result['auto_document_verified']
    if result['auto_document_verified'] and not result.get('document_series'):
        result['series_not_present'] = True
    return result
