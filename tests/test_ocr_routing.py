import unittest
from unittest.mock import Mock

from ocr_adapter import parse_engine_rows
from ocr_engines import recognize_document, usable_page


def line(page, text, confidence=.95):
    return {'page': page, 'text': text, 'confidence': confidence}


def grades():
    return [line(1, 'Итоговые оценки'), *[
        line(1, f'{subject} 5') for subject in (
            'Алгебра', 'Геометрия', 'Русский язык', 'Литература', 'История',
            'Физика', 'Химия', 'Биология',
        )
    ]]


def cover(confidence=.95):
    return [line(2, 'Аттестат об основном общем образовании'),
            line(2, 'Номер 0012345', confidence), line(2, 'Дата рождения')]


class OCRRoutingTests(unittest.TestCase):
    def setUp(self):
        self.pages = [{'page': 1}, {'page': 2}]
        self.calls = []
        self.docling = Mock(side_effect=self.read_docling)

    def read_docling(self, pages):
        self.calls.append('docling')
        return {'pages': [{'page': page['page'], 'complete': True,
                           'lines': grades() if page['page'] == 1 else cover(),
                           'table': []} for page in pages]}

    def recognize(self, lines):
        def tesseract(engine, pages):
            self.calls.append(engine)
            self.assertEqual(pages, self.pages)
            return {'engine': engine, 'ok': True, 'lines': lines, 'error': ''}

        return recognize_document(self.pages, tesseract, parse_engine_rows,
                                  docling_runner=self.docling)

    def test_confident_table_and_cover_ignore_unrelated_noisy_text(self):
        result = self.recognize(grades() + cover() + [line(1, '***', .15), line(2, '///', .12)])
        self.assertEqual(self.calls, ['tesseract'])
        self.docling.assert_not_called()
        self.assertEqual(result['ocr']['accepted'], 8)

    def test_uncertain_grade_rereads_only_its_page(self):
        lines = grades()
        lines[-1]['confidence'] = .4
        result = self.recognize(lines + cover())
        self.assertEqual(self.calls, ['tesseract', 'docling'])
        self.docling.assert_called_once_with([{'page': 1}])
        self.assertEqual(result['ocr']['fallback_pages'], [1])
        self.assertEqual(result['ocr']['accepted'], 8)

    def test_uncertain_cover_number_rereads_only_cover(self):
        result = self.recognize(grades() + cover(confidence=.4))
        self.docling.assert_called_once_with([{'page': 2}])
        self.assertEqual(result['ocr']['fallback_pages'], [2])

    def test_recognized_cover_without_optional_number_skips_docling(self):
        result = self.recognize(grades() + [cover()[0], cover()[2]])
        self.docling.assert_not_called()
        self.assertEqual(result['ocr']['status'], 'accepted')

    def test_cover_with_conflicting_numbers_is_reread(self):
        self.recognize(grades() + cover() + [line(2, 'Номер 0076543')])
        self.docling.assert_called_once_with([{'page': 2}])

    def test_failed_fallback_keeps_partial_table_on_review(self):
        self.docling.side_effect = None
        self.docling.return_value = {'ok': False, 'pages': [], 'error': 'unavailable'}
        result = self.recognize(grades()[:-2] + cover())
        self.assertEqual(result['ocr']['incomplete_pages'], [1])
        self.assertEqual(result['ocr']['status'], 'needs_review')

    def test_failed_reread_for_optional_number_does_not_block_grades(self):
        self.docling.side_effect = None
        self.docling.return_value = {'ok': False, 'pages': [], 'error': 'unavailable'}
        result = self.recognize(grades() + cover(confidence=.4))
        self.assertEqual(result['ocr']['fallback_pages'], [2])
        self.assertEqual(result['ocr']['incomplete_pages'], [])
        self.assertEqual(result['ocr']['status'], 'accepted')

    def test_pdf_text_does_not_skip_ocr_with_unpaired_grade(self):
        page = {'page': 1, 'complete': True, 'lines': [line(1, 'Итоговые оценки'),
                line(1, 'Алгебра 5'), line(1, 'Практический курс риторики'), line(1, '4')]}
        self.assertFalse(usable_page(page, parse_engine_rows, 'auto', embedded_text=True))

    def test_pdf_text_only_skips_both_engines(self):
        reading = {'page': 1, 'lines': grades()}
        tesseract = Mock()
        result = recognize_document([{'page': 1, 'pdf_reading': reading}], tesseract,
                                    parse_engine_rows, docling_runner=self.docling)
        tesseract.assert_not_called()
        self.docling.assert_not_called()
        self.assertEqual(result['ocr']['pdf_text_pages'], [1])

    def test_same_unreadable_row_on_two_pages_keeps_both_sources(self):
        lines = [line(page, text) for page in (1, 2)
                 for text in ('Итоговые оценки', 'Каллиграфия неразборчиво')]
        rows = parse_engine_rows(lines)
        self.assertEqual([(r['page'], r['grade']) for r in rows], [(1, None), (2, None)])


if __name__ == '__main__':
    unittest.main()
