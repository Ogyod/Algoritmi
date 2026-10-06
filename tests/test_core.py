import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import (DOCUMENT_TYPES, classify, extract_grades, extract_identifiers,
                  mean_grade, group_lines, interpret_lines)


def lines(*values, page=1):
    return [{'text': value, 'confidence': .9, 'page': page} for value in values]


class ExtractionTests(unittest.TestCase):
    def test_all_education_categories(self):
        titles = {
            'certificate_9': 'Аттестат об основном общем образовании',
            'certificate_11': 'Аттестат о среднем общем образовании',
            'diploma_spo': 'Диплом о среднем профессиональном образовании',
            'diploma_bachelor': 'Диплом бакалавра',
            'diploma_specialist': 'Диплом специалиста',
            'diploma_master': 'Диплом магистра',
            'diploma_postgraduate': 'Диплом об окончании аспирантуры',
        }
        for kind, title in titles.items():
            with self.subTest(kind=kind): self.assertEqual(classify(title), kind)

    def test_winners_diploma_is_achievement(self):
        self.assertEqual(classify('Диплом победителя олимпиады'), 'achievement')
        self.assertEqual(classify('Диплом бакалавра'), 'diploma_bachelor')

    def test_previous_document_does_not_override_title(self):
        self.assertEqual(classify('Диплом магистра. Предыдущий документ об образовании: диплом бакалавра'), 'diploma_master')
        self.assertEqual(classify('Диплом. Предыдущий документ: аттестат об основном общем образовании'), 'education_unknown')

    def test_unspecified_school_level_requires_review(self):
        self.assertEqual(classify('Приложение к аттестату. Итоговые оценки'), 'education_unknown')

    def test_words_digits_and_conflict(self):
        rows = extract_grades(lines('Алгебра 5 (отлично)', 'История хорошо',
                                    'Химия неудовлетворительно', 'Биология 5 (хорошо)'))
        self.assertEqual([row['grade'] for row in rows], [5, 4, 2, 4])
        self.assertTrue(rows[-1]['conflict'])

    def test_row_number_is_not_grade(self):
        self.assertEqual(extract_grades(lines('3 Русский язык 5'))[0]['grade'], 5)

    def test_year_and_serial_are_not_grades(self):
        self.assertEqual(extract_grades(lines('Алгебра 2025', 'Русский язык 4515')), [])

    def test_multiple_numeric_grades_remain_unresolved(self):
        row = extract_grades(lines('Алгебра 3 4 5'))[0]
        self.assertIsNone(row['grade'])
        self.assertTrue(row['conflict'])

    def test_average_empty_and_invalid(self):
        self.assertIsNone(mean_grade([]))
        self.assertEqual(mean_grade([{'grade': 5}, {'grade': 4}, {'grade': 5}]), 4.67)
        for bad in [0, 6, 4.5, True]:
            with self.assertRaises(ValueError): mean_grade([{'grade': bad}])

    def test_separate_columns(self):
        blocks = [{'text': 'Алгебра', 'x': .1, 'y': .2, 'height': .02, 'confidence': .9},
                  {'text': '5 (отлично)', 'x': .7, 'y': .2, 'height': .02, 'confidence': .8},
                  {'text': 'Геометрия', 'x': .1, 'y': .3, 'height': .02, 'confidence': .9},
                  {'text': '4', 'x': .7, 'y': .3, 'height': .02, 'confidence': .9}]
        self.assertEqual([r['grade'] for r in extract_grades(group_lines(blocks))], [5, 4])

    def test_letter_series_and_leading_zeros(self):
        for text in ['Серия АБ Номер 0012345', 'АБ № 0012345']:
            result, _ = extract_identifiers(lines(text))
            self.assertEqual(result['document_series'], 'АБ')
            self.assertEqual(result['document_number'], '0012345')

    def test_labelled_numeric_series(self):
        result, _ = extract_identifiers(lines('Серия 107724', 'Номер 0001234', 'Регистрационный номер 45678'))
        self.assertEqual((result['document_series'], result['document_number']), ('107724', '0001234'))

    def test_labels_on_separate_lines(self):
        result, _ = extract_identifiers(lines('Серия', 'АБ', 'Номер', '0012345'))
        self.assertEqual((result['document_series'], result['document_number']), ('АБ', '0012345'))

    def test_combined_number_is_not_split(self):
        for text in ['07724000123456', '107724 0001234']:
            result, warnings = extract_identifiers(lines(text))
            self.assertEqual(result['document_series'], '')
            self.assertEqual(result['document_number'], text.replace(' ', ''))
            self.assertTrue(warnings)

    def test_registration_number_on_next_line_is_ignored(self):
        result, _ = extract_identifiers(lines('Регистрационный номер', '07724000123456'))
        self.assertEqual(result['document_number'], '')

    def test_previous_number_is_ignored(self):
        result, _ = extract_identifiers(lines('Номер 0012345', 'Предыдущий документ', 'Номер 9876543'))
        self.assertEqual(result['document_number'], '0012345')

    def test_repeated_identifier_is_not_conflict(self):
        result, _ = extract_identifiers(lines('Серия АБ Номер 0012345') + lines('Номер 0012345', page=2))
        self.assertEqual(result['document_number'], '0012345')
        self.assertEqual(len(result['identifier_candidates']), 1)

    def test_conflicting_identifiers_require_manual_selection(self):
        result, warnings = extract_identifiers(lines('Номер 0012345') + lines('Номер 9876543', page=2))
        self.assertEqual(result['document_number'], '')
        self.assertEqual(len(result['identifier_candidates']), 2)
        self.assertTrue(warnings)

    def test_no_auto_points_and_no_address_field(self):
        result = interpret_lines(lines('Диплом победителя'))
        self.assertIsNone(result['points'])
        self.assertNotIn('address', result)
        self.assertFalse(result['reviewed'])
        self.assertNotIn('passport', DOCUMENT_TYPES)

    def test_category_override_reuses_text(self):
        result = interpret_lines(lines('Приложение к аттестату', 'Номер 0012345', 'Алгебра 5'), 'certificate_9')
        self.assertEqual(result['average'], 5.0)
        self.assertEqual(result['document_number'], '0012345')

    def test_duplicate_subjects_do_not_get_average(self):
        result = interpret_lines(lines('Алгебра 5', 'Алгебра 4'), 'certificate_9')
        self.assertIsNone(result['average'])


if __name__ == '__main__': unittest.main()
