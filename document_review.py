"""Validate a review and build the result to be stored by the batch API."""
import re

from core import CERTIFICATE_TYPES, DOCUMENT_TYPES, EDUCATION_TYPES, mean_grade, normalize
from uploads import Problem


def apply_review(previous, data):
    """Return a revised result without changing the stored result on validation failure."""
    grades = data.get('grades')
    if not isinstance(grades, list) or len(grades) > 100:
        raise Problem('Нужен список не более 100 предметов.')
    complete = data.get('complete') is True
    result = dict(previous)
    saved_rows = {row.get('row_id'): row for row in result['grades'] if row.get('row_id')}
    cleaned = []
    names = set()
    for original in grades:
        if not isinstance(original, dict):
            raise Problem('Некорректная строка предмета.')
        row = dict(original)
        saved = saved_rows.get(row.get('row_id'))
        same = bool(saved) and all(row.get(key) == saved.get(key) for key in ('subject','grade','page','include','reason'))
        row['auto_verified'] = bool(same and saved.get('auto_verified'))
        row['votes'] = saved.get('votes', {}) if saved else {}
        row['supporting_engines'] = saved.get('supporting_engines', []) if row['auto_verified'] else []
        if row['auto_verified']:
            row['verification'] = saved.get('verification') or 'ocr_high_confidence'
        else:
            row['verification'] = 'manual' if row.get('verified') is True else 'manual_required'
        if row.get('verified') is True:
            row['conflict'] = False
        cleaned.append(row)
        subject = row.get('subject')
        if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 200:
            raise Problem('Укажите название каждого предмета.')
        name = normalize(subject)
        if name in names:
            raise Problem('Повторяется предмет: ' + subject)
        names.add(name)
        if complete and row.get('verified') is not True and not row['auto_verified']:
            raise Problem('Сверьте каждую оценку и передайте verified: true.')
    grades = cleaned
    kind = data.get('type', result['type'])
    if kind not in DOCUMENT_TYPES:
        raise Problem('Выберите категорию документа.')
    can_calculate = bool(grades) and all(type(row.get('grade')) is int and row['grade'] in (2,3,4,5)
                                        and not row.get('conflict') and (row.get('auto_verified') or row.get('verified') is True)
                                        for row in grades)
    if complete and grades and not can_calculate:
        raise Problem('Проверьте оценки от 2 до 5 и их подтверждение.')
    average = mean_grade(grades) if can_calculate and kind in CERTIFICATE_TYPES else None
    if complete and kind in CERTIFICATE_TYPES and not grades:
        raise Problem('Для расчёта аттестата нужен полный список оценок.')
    for field in ('document_series', 'document_number', 'series_not_present', 'achievement', 'points'):
        if field in data:
            result[field] = data[field]
    if complete and kind in EDUCATION_TYPES:
        series = result.get('document_series', '')
        number = result.get('document_number', '')
        absent = result.get('series_not_present') is True
        if (not isinstance(number, str) or (number and not re.fullmatch(r'\d{5,20}', number)) or
                not isinstance(series, str) or len(series) > 20 or
                (series and not re.fullmatch(r'[\w -]+', series)) or
                (series.strip() and absent)):
            raise Problem('Проверьте указанные серию и номер документа.')
    if complete and kind == 'achievement':
        points = result.get('points')
        if not isinstance(result.get('achievement'), str) or not result['achievement'].strip() or type(points) not in (int, float) or not 0 <= points < float('inf'):
            raise Problem('Укажите достижение и неотрицательные баллы.')
    result.update(auto_document_verified=False, type=kind, grades=grades, average=average, reviewed=complete,
                  auto_grades_verified=bool(grades) and all(row['auto_verified'] for row in grades))
    if result.get('ocr'):
        result['ocr'] = {
            **result['ocr'],
            'accepted': sum(bool(row['auto_verified']) for row in grades),
            'unresolved': sum(not (row['auto_verified'] or row.get('verified') is True) for row in grades),
            'status': 'accepted' if result['auto_grades_verified'] else 'reviewed' if complete else 'needs_review',
        }
    return result
