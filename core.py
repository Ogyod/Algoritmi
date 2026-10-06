"""Interpret OCR text without executing or trusting instructions in documents."""
import re
from decimal import Decimal, ROUND_HALF_UP

DOCUMENT_TYPES = {
    'certificate_9': 'Аттестат · 9 классов (основное общее)',
    'certificate_11': 'Аттестат · 11 классов (среднее общее)',
    'diploma_spo': 'Диплом · СПО',
    'diploma_bachelor': 'Диплом · бакалавриат',
    'diploma_specialist': 'Диплом · специалитет',
    'diploma_master': 'Диплом · магистратура',
    'diploma_postgraduate': 'Диплом · аспирантура',
    'achievement': 'Дополнительное достижение',
}
EDUCATION_TYPES = set(DOCUMENT_TYPES) - {'achievement'}
CERTIFICATE_TYPES = {'certificate_9', 'certificate_11'}
SUBJECTS = [
    'Русский язык', 'Литература', 'Алгебра и начала математического анализа',
    'Алгебра', 'Геометрия', 'Математика', 'Информатика', 'История',
    'Обществознание', 'География', 'Биология', 'Физика', 'Химия',
    'Физическая культура', 'Иностранный язык', 'Английский язык',
    'Немецкий язык', 'Французский язык', 'Технология',
    'Основы безопасности жизнедеятельности', 'Основы безопасности и защиты Родины',
    'Индивидуальный проект', 'Вероятность и статистика', 'Астрономия',
    'Основы духовно-нравственной культуры народов России',
]
GRADE_WORDS = {'отлично': 5, 'хорошо': 4, 'удовлетворительно': 3,
               'неудовлетворительно': 2}


def normalize(text):
    return re.sub(r'\s+', ' ', text.lower().replace('ё', 'е')).strip()


def mean_grade(grades):
    if not grades:
        return None
    values = [row['grade'] for row in grades]
    if any(isinstance(v, bool) or not isinstance(v, int) or not 2 <= v <= 5 for v in values):
        raise ValueError('Оценка должна быть целым числом от 2 до 5.')
    value = sum(Decimal(v) for v in values) / Decimal(len(values))
    return float(value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def group_lines(blocks):
    """Join subject and grade boxes on the same visual row, within one page."""
    rows = []
    for block in sorted(blocks, key=lambda b: (b.get('y', 0), b.get('x', 0))):
        center = block.get('y', 0) + block.get('height', .02) / 2
        target = next((row for row in rows if abs(center - row['center']) <
                       .55 * max(block.get('height', .02), row['height'])), None)
        if target is None:
            rows.append({'center': center, 'height': block.get('height', .02), 'blocks': [block]})
        else:
            target['blocks'].append(block)
    return [{'text': ' '.join(b['text'] for b in sorted(r['blocks'], key=lambda b: b.get('x', 0))),
             'confidence': min(b.get('confidence', 0) for b in r['blocks'])} for r in rows]


def extract_grades(lines):
    result = []
    for line in lines:
        text = normalize(line['text'])
        subject = next((s for s in sorted(SUBJECTS, key=len, reverse=True)
                        if normalize(s) in text), None)
        if not subject:
            continue
        # Ignore row numbers before the subject. Read only the grade column afterwards.
        tail = text.split(normalize(subject), 1)[1]
        word = next((w for w in GRADE_WORDS if re.search(r'(?<!\w)' + w + r'(?!\w)', tail)), None)
        digits = re.findall(r'(?<!\w)([2-5])(?!\w)', tail)
        unique = set(digits)
        if not word and not digits:
            continue
        grade = GRADE_WORDS[word] if word else int(digits[0]) if len(unique) == 1 else None
        conflict = bool(len(unique) > 1 or (word and any(int(d) != grade for d in digits)))
        result.append({'subject': subject, 'grade': grade,
                       'confidence': round(line.get('confidence', 0), 3),
                       'source': line['text'], 'conflict': conflict})
    return result


def classify(text):
    text = normalize(text)
    patterns = {
        'certificate_9': r'аттестат.{0,60}об основном общем образовании',
        'certificate_11': r'аттестат.{0,60}о среднем (?:\(полном\) )?общем образовании',
        'diploma_spo': r'диплом.{0,60}(?:о среднем профессиональном образовании|квалифицированного рабочего|специалиста среднего звена)',
        'diploma_bachelor': r'диплом\s+(?:с отличием\s+)?бакалавра',
        'diploma_specialist': r'диплом\s+(?:с отличием\s+)?специалиста(?! среднего)',
        'diploma_master': r'диплом\s+(?:с отличием\s+)?магистра',
        'diploma_postgraduate': r'диплом.{0,40}(?:об окончании аспирантуры|аспирантуры)',
        'achievement': r'диплом\s+(?:победителя|призера|участника)|грамота|сертификат|знак.{0,15}гто',
    }
    text = text.split('предыдущий документ', 1)[0]
    first_title = re.search(r'\b(аттестат|диплом)\b', text)
    matches = [(match.start(), kind) for kind, pattern in patterns.items()
               if (match := re.search(pattern, text))
               and (not first_title or match.start() == first_title.start())]
    if matches:
        # The first title is more relevant than a mention of a previous qualification.
        return min(matches)[1]
    if 'аттестат' in text or 'диплом' in text or 'итоговые оценки' in text:
        return 'education_unknown'
    return 'unknown'


def extract_identifiers(lines):
    """Keep leading zeros; never confuse a registration number with a form number.

    We support explicit labels, legacy letter-series + №, and standalone
    13/14-digit identifiers. An unlabelled combined identifier is NOT split.
    """
    candidates = []
    for page in sorted({line.get('page', 1) for line in lines}):
        page_lines = [line for line in lines if line.get('page', 1) == page]
        series_rows = []
        ignored_context = False
        eligible = []
        for i, line in enumerate(page_lines):
            text = line['text'].strip()
            normalized = normalize(text)
            if 'предыдущий документ' in normalized:
                ignored_context = True
            if ignored_context or re.search(r'регистрационн|протокол|приказ|дата|телефон|снилс|инн', normalized):
                continue
            if (i > 0 and 'регистрационн' in normalize(page_lines[i - 1]['text'])
                    and re.fullmatch(r'(?:№\s*)?\d[\d -]*', text)):
                continue
            eligible.append((i, line))
            match = re.search(r'(?i)\bсерия\s*[:№]?\s*([А-ЯA-Z]{1,4}(?:[ -][А-ЯA-Z]{1,4})?|\d{2,8})\b', text)
            if match:
                series = re.sub(r'\s+', '', match[1]).upper()
                # Do not consume the following field's label as the series.
                if series not in {'НОМЕ', 'NOME'}:
                    series_rows.append((i, series, text))
            elif re.fullmatch(r'(?i)серия\s*:?', text) and i + 1 < len(page_lines):
                value = page_lines[i + 1]['text'].strip()
                if re.fullmatch(r'[А-ЯA-Z]{1,4}(?:[ -][А-ЯA-Z]{1,4})?|\d{2,8}', value):
                    series_rows.append((i, re.sub(r'\s+', '', value).upper(), text + ' / ' + value))
        for i, line in eligible:
            text = line['text'].strip()
            number = None
            series = ''
            method = ''
            source = text
            legacy = re.fullmatch(r'([А-ЯA-Z]{1,4}(?:[ -][А-ЯA-Z]{1,4})?)\s*№\s*(\d{5,15})', text, re.I)
            labelled = re.search(r'(?i)(?:\bномер(?:\s+(?:бланка|документа|аттестата|диплома))?|№|\bNo\.?|\bN\.)\s*[:№]?\s*(\d[\d -]{3,25}\d)(?!\d)', text)
            if legacy:
                series, number = re.sub(r'\s+', '', legacy[1]).upper(), legacy[2]
                method = 'labelled_series_and_number'
            elif labelled:
                number = re.sub(r'[ -]', '', labelled[1])
                method = 'labelled_number'
            elif re.fullmatch(r'(?i)(номер(?: бланка| документа| аттестата| диплома)?|№)\s*:?', text) and i + 1 < len(page_lines):
                value = page_lines[i + 1]['text'].strip()
                if re.fullmatch(r'\d[\d -]{3,25}\d', value):
                    number = re.sub(r'[ -]', '', value)
                    source += ' / ' + value
                    method = 'labelled_number'
            elif re.fullmatch(r'\d{13,14}', text):
                number = text
                method = 'combined_identifier'
            elif re.fullmatch(r'\d{6}\s+\d{7,8}', text):
                number = re.sub(r'\s+', '', text)
                method = 'combined_identifier'
            if not number or not 5 <= len(number) <= 20:
                continue
            nearby = [(abs(j - i), value, evidence) for j, value, evidence in series_rows if abs(j - i) <= 3]
            if not series and nearby:
                distance = min(item[0] for item in nearby)
                closest = [item for item in nearby if item[0] == distance]
                if len({item[1] for item in closest}) == 1:
                    _, series, series_source = closest[0]
                    source = series_source + ' / ' + source if series_source != source else source
                    method = 'labelled_series_and_number'
            candidates.append({'series': series, 'number': number, 'source': source,
                               'page': page, 'confidence': line.get('confidence', 0), 'method': method})
    # Repeated printing of the same identifier in the appendix is not a conflict.
    for item in candidates:
        if not item['series']:
            known = {c['series'] for c in candidates if c['number'] == item['number'] and c['series']}
            if len(known) == 1:
                item['series'] = known.pop()
    unique = {}
    for item in candidates:
        key = (item['series'], item['number'])
        if key not in unique or item['confidence'] > unique[key]['confidence']:
            unique[key] = item
    candidates = list(unique.values())
    warnings = []
    if len(candidates) == 1:
        chosen = candidates[0]
        if chosen['method'] == 'combined_identifier':
            warnings.append('Реквизит напечатан без явного разделения. Проверьте номер; серия автоматически не выделяется.')
        series, number = chosen['series'], chosen['number']
    else:
        series = number = ''
        if candidates:
            warnings.append('Найдены разные реквизиты. Выберите нужные по скану и заполните серию и номер вручную.')
        else:
            warnings.append('Серия и номер не найдены. Загрузите титульную страницу или заполните поля вручную.')
    return {'document_series': series, 'document_number': number,
            'series_not_present': False, 'identifier_candidates': candidates}, warnings


def interpret_lines(lines, requested_type='auto'):
    if not isinstance(requested_type, str) or (requested_type != 'auto' and requested_type not in DOCUMENT_TYPES):
        raise ValueError('Неизвестный тип документа.')
    text = '\n'.join(line['text'] for line in lines)
    detected_type = classify(text)
    kind = detected_type if requested_type == 'auto' else requested_type
    educational = kind in EDUCATION_TYPES or kind == 'education_unknown'
    # Keep candidate grades when the subtype is unknown, so category correction
    # in the interface does not require another OCR pass.
    grades = extract_grades(lines) if educational else []
    identifiers = {'document_series': '', 'document_number': '',
                   'series_not_present': False, 'identifier_candidates': []}
    warnings = []
    if not text.strip():
        warnings.append('Текст не распознан. Попробуйте более чёткий скан или заполните поля вручную.')
    if kind in {'unknown', 'education_unknown'}:
        warnings.append('Категория не определена точно. Выберите её в списке документа.')
    if educational:
        identifiers, identifier_warnings = extract_identifiers(lines)
        warnings.extend(identifier_warnings)
    if kind in CERTIFICATE_TYPES and not grades:
        warnings.append('Оценки не найдены. Для среднего балла добавьте приложение к аттестату.')
    if kind == 'achievement':
        warnings.append('Баллы укажите вручную по правилам приёма вашего учебного заведения.')
    if any(row['conflict'] for row in grades):
        warnings.append('Неоднозначные оценки выделены. Сверьте их со сканом.')
    names = [normalize(row['subject']) for row in grades]
    duplicates = len(names) != len(set(names))
    if duplicates:
        warnings.append('Повторяются предметы. Удалите дубликаты для расчёта среднего балла.')
    valid = grades and all(isinstance(row['grade'], int) for row in grades) and not duplicates
    return {'type': kind, 'detected_type': detected_type, 'text': text, 'grades': grades,
            'average': mean_grade(grades) if valid and kind in CERTIFICATE_TYPES else None,
            **identifiers, 'achievement': text[:300] if kind == 'achievement' else '',
            'points': None, 'warnings': warnings, 'reviewed': False,
            'ocr_lines': lines}


def interpret(pages, requested_type='auto'):
    lines = []
    for page in pages:
        lines.extend({**row, 'page': page['number']} for row in group_lines(page['blocks']))
    result = interpret_lines(lines, requested_type)
    result['pages'] = [{k: v for k, v in page.items() if k != 'blocks'} for page in pages]
    return result
