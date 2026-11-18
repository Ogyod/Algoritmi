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
                       'confidence': min(row['confidence'], 1 if parsed['known_subject'] else .5),
                       'conflict': conflict, 'page': row['page'], 'box': row.get('box')})
    # Preserve additional unreadable/unknown rows found by the project's original parser.
    covered = {(row['page'], normalized(row['subject'])) for row in result}
    for parsed in extract_grades(lines):
        line = next((line for line in lines if line['text'] == parsed['source']), None)
        if line and (line['page'], normalized(parsed['subject'])) not in covered:
            result.append({**parsed, 'page': line['page'], 'box': line.get('box'),
                           'confidence': min(float(line.get('confidence', 0)),
                                             1 if parsed['known_subject'] else .5),
                           'verified': False, 'include': True, 'reason': ''})
            covered.add((line['page'], normalized(parsed['subject'])))
    return result


def interpret_consensus(recognition, kind='auto'):
    available = [o for o in recognition['observations'] if o['ok'] and o['lines']]
    candidates = [interpret_lines(o['lines'], kind) for o in available]
    # Select a reading per page so a fallback on page 2 retains the title on page 1.
    combined = []
    for page in sorted({line['page'] for o in available for line in o['lines']}):
        readings = [[line for line in o['lines'] if line['page'] == page] for o in available]
        def usefulness(lines):
            info = interpret_lines(lines, kind)
            return (info['detected_type'] in DOCUMENT_TYPES, bool(info['document_number']),
                    len([r for r in info['grades'] if r['grade'] is not None]),
                    sum(len(line['text']) * line['confidence'] for line in lines))
        combined.extend(max(readings, key=usefulness))
    result = interpret_lines(combined, kind)
    matching = [candidate for candidate in candidates if candidate['type'] == result['type']]
    for field in ('document_number', 'document_series'):
        values = {candidate[field] for candidate in matching if candidate[field]}
        if not result[field] and len(values) == 1:
            result[field] = values.pop()
    candidates.append(result)
    result['ocr'] = recognition['ocr']
    result['docling'] = recognition.get('docling', {'used': False, 'ok': False, 'suggested': 0, 'hints': 0})
    names = {'pdf_text': 'Текст PDF', 'docling': 'Docling', 'tesseract': 'Tesseract'}
    result['engine'] = ' + '.join(names[o['engine']] for o in recognition['observations'] if o['ok']) or 'OCR недоступен'
    result['auto_grades_verified'] = False
    result['auto_document_verified'] = False
    if result['type'] in CERTIFICATE_TYPES or result['type'] == 'education_unknown':
        result['grades'] = recognition['grades']
        accepted = recognition['ocr']['status'] == 'accepted'
        result['auto_grades_verified'] = accepted
        result['average'] = mean_grade(result['grades']) if accepted and result['type'] in CERTIFICATE_TYPES else None
        result['warnings'] = [warning for warning in result['warnings'] if warning not in GRADE_WARNINGS]
        if accepted:
            message = 'Предметы и оценки приняты по уверенности OCR от 80%.'
            if result['average'] is not None:
                message += ' Средний балл рассчитан автоматически.'
            matching = [candidate for candidate in candidates if candidate['type'] == result['type']]
            category_conflict = any(candidate['type'] in DOCUMENT_TYPES and candidate['type'] != result['type']
                                    for candidate in candidates)
            numbers = {candidate['document_number'] for candidate in matching if candidate.get('document_number')}
            series = {candidate['document_series'] for candidate in matching if candidate.get('document_series')}
            result['auto_document_verified'] = (result['type'] in CERTIFICATE_TYPES
                                                and bool(matching) and not category_conflict
                                                and all(len(candidate['identifier_candidates']) <= 1
                                                        for candidate in matching)
                                                and len(numbers) <= 1 and len(series) <= 1)
            message += (' Документ принят автоматически.' if result['auto_document_verified'] else
                        ' Категория или прочитанные реквизиты расходятся: проверьте их отдельно.')
            result['warnings'].append(message)
        elif result['grades'] and recognition['ocr']['unresolved']:
            result['warnings'].append(
                f"Не приняты автоматически {recognition['ocr']['unresolved']} строк(и): уверенность ниже 80% или есть противоречие. "
                'Прочитанные оценки подставлены предварительно; проверьте жёлтые строки по скану.')
        elif not result['grades']:
            result['warnings'].append('Оценки не найдены. Загрузите приложение с оценками или внесите строки вручную.')
    result['warnings'].extend(o['error'] for o in recognition['observations'] if not o['ok'])
    if result['docling']['suggested']:
        result['warnings'].append(f"Docling нашёл ещё {result['docling']['suggested']} строк(и) таблицы. Сверьте их со сканом.")
    if result['docling']['hints']:
        result['warnings'].append('Docling также проверил спорные строки. Сверьте подставленные оценки со сканом.')
    if recognition['ocr']['incomplete_pages']:
        result['grades_need_review'] = True
        numbers = ', '.join(map(str, recognition['ocr']['incomplete_pages']))
        result['warnings'].append(f'Требуют сверки страницы: {numbers}. Повторное чтение не удалось.')
    # Missing identifiers do not block an agreed certificate; conflicting readings still do.
    result['reviewed'] = result['auto_document_verified']
    return result
