import sys
from pathlib import Path
import unittest
import copy
import io
import tempfile
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ocr_engines import recognize_document
from ocr_adapter import parse_engine_rows, interpret_consensus
from batch_service import BatchService


def observation(engine, grades, confidence=.95):
    texts = ['АТТЕСТАТ ОБ ОСНОВНОМ ОБЩЕМ ОБРАЗОВАНИИ', 'Серия АБ Номер 0012345', 'Итоговые оценки'] + grades
    return {'engine': engine, 'ok': True, 'error': '', 'lines': [
        {'text': text, 'page': 1, 'confidence': confidence, 'box': [10, i*40, 700, i*40+30]}
        for i, text in enumerate(texts)]}


class ConsensusTests(unittest.TestCase):
    def result(self, answers, failed=()):
        calls = []
        def runner(engine, pages):
            calls.append(engine)
            if engine in failed:
                return {'engine': engine, 'ok': False, 'error': 'Недоступен', 'lines': []}
            return observation(engine, answers[engine])
        recognized = recognize_document([{'page': 1, 'path': 'unused'}], runner, parse_engine_rows, True)
        return interpret_consensus(recognized), calls

    def test_two_agree_no_third_and_immediate_average(self):
        result, calls = self.result({'rapidocr': ['Алгебра 5', 'Физика 4'], 'tesseract': ['Алгебра 5', 'Физика 4']})
        self.assertCountEqual(calls, ['rapidocr', 'tesseract'])
        self.assertTrue(result['auto_grades_verified'])
        self.assertEqual(result['average'], 4.5)
        self.assertEqual(result['document_number'], '0012345')
        self.assertTrue(result['reviewed'])
        self.assertTrue(result['auto_document_verified'])

    def test_third_resolves_grade_disagreement(self):
        result, calls = self.result({'rapidocr': ['Алгебра 5'], 'tesseract': ['Алгебра 4'], 'easyocr': ['Алгебра 5']})
        self.assertIn('easyocr', calls)
        self.assertEqual(result['average'], 5.0)
        self.assertEqual(result['grades'][0]['consensus_status'], 'third_resolved')

    def test_no_quorum_does_not_publish_average(self):
        result, _ = self.result({'rapidocr': ['Алгебра 5'], 'tesseract': ['Алгебра 4'], 'easyocr': ['Алгебра 3']})
        self.assertIsNone(result['average'])
        self.assertIsNone(result['grades'][0]['grade'])

    def test_missing_row_is_checked_by_third(self):
        result, _ = self.result({'rapidocr': ['Алгебра 5', 'Физика 4'], 'tesseract': ['Алгебра 5'], 'easyocr': ['Физика 4']})
        self.assertEqual(result['average'], 4.5)
        self.assertEqual(len(result['grades']), 2)

    def test_failed_engine_does_not_vote(self):
        result, _ = self.result({'rapidocr': ['Алгебра 5'], 'easyocr': ['Алгебра 5']}, failed=('tesseract',))
        self.assertEqual(result['average'], 5.0)
        self.assertEqual(result['grades'][0]['supporting_engines'], ['rapidocr', 'easyocr'])

    def test_internal_digit_word_conflict_is_not_a_valid_vote(self):
        result, _ = self.result({'rapidocr': ['Алгебра 5 (хорошо)'], 'tesseract': ['Алгебра 5'], 'easyocr': ['Алгебра 5']})
        self.assertEqual(result['average'], 5.0)
        self.assertNotIn('rapidocr', result['grades'][0]['supporting_engines'])

    def test_canonical_subject_catalog_is_retained(self):
        rows = parse_engine_rows(observation('rapidocr', ['Основы безопасности жизнедеятельности 5'])['lines'])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['known_subject'])

    def test_unreadable_catalog_subject_is_preserved(self):
        rows = parse_engine_rows(observation('rapidocr', ['Черчение неразборчиво'])['lines'])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['grade'])

    def test_unreadable_unknown_subject_in_table_is_preserved(self):
        rows = parse_engine_rows(observation('rapidocr', ['Краеведение неразборчиво'])['lines'])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['grade'])

    def test_agreed_diploma_skips_unneeded_full_page_third_ocr(self):
        calls = []
        def runner(engine, pages):
            calls.append(engine)
            return {'engine': engine, 'ok': True, 'error': '', 'lines': [
                {'page': 1, 'text': text, 'confidence': .96, 'box': [10, i*40, 700, i*40+30]}
                for i, text in enumerate(('Диплом бакалавра', 'Серия АБ Номер 0012345'))]}
        result = recognize_document([{'page': 1}], runner, parse_engine_rows, True)
        self.assertEqual(calls, ['rapidocr', 'tesseract'])
        self.assertFalse(result['ocr']['third_used'])
        self.assertEqual(result['grades'], [])

    def test_disputed_diploma_number_still_gets_third_read(self):
        calls = []
        def runner(engine, pages):
            calls.append(engine)
            number = '9999999' if engine == 'tesseract' else '0012345'
            return {'engine': engine, 'ok': True, 'error': '', 'lines': [
                {'page': 1, 'text': text, 'confidence': .96, 'box': [10, i*40, 700, i*40+30]}
                for i, text in enumerate(('Диплом бакалавра', 'Серия АБ Номер ' + number))]}
        result = recognize_document([{'page': 1}], runner, parse_engine_rows, True)
        self.assertEqual(calls, ['rapidocr', 'tesseract', 'easyocr'])
        self.assertTrue(result['ocr']['third_used'])

    def test_certificate_without_detected_grades_still_gets_third_read(self):
        calls = []
        def runner(engine, pages):
            calls.append(engine)
            return observation(engine, [])
        recognize_document([{'page': 1}], runner, parse_engine_rows, True)
        self.assertEqual(calls, ['rapidocr', 'tesseract', 'easyocr'])

    def test_docling_adds_missing_table_row_only_for_manual_review(self):
        calls = []
        def runner(engine, pages):
            return observation(engine, ['Алгебра 5'])
        def tables(pages):
            calls.extend(page['page'] for page in pages)
            return {'ok': True, 'rows': [{'page': 1, 'text': 'Физика 4'},
                                         {'page': 1, 'text': 'Алгебра 5'}]}
        recognition = recognize_document([{'page': 1}], runner, parse_engine_rows, True,
                                         docling_runner=tables)
        result = interpret_consensus(recognition)
        self.assertEqual(calls, [])  # Agreed grades do not start Docling.
        self.assertFalse(result['docling']['used'])

        def disputed(engine, pages):
            grade = {'rapidocr': 5, 'tesseract': 4, 'easyocr': 3}[engine]
            return observation(engine, [f'Алгебра {grade}'])
        recognition = recognize_document([{'page': 1}], disputed, parse_engine_rows, True,
                                         docling_runner=tables)
        result = interpret_consensus(recognition)
        self.assertEqual(calls, [1])
        self.assertEqual(result['docling']['suggested'], 1)
        self.assertEqual(result['docling']['hints'], 1)
        proposed = next(row for row in result['grades'] if row['subject'] == 'Физика')
        self.assertIsNone(proposed['grade'])
        self.assertEqual(proposed['suggested_grade'], 4)
        self.assertFalse(proposed['auto_verified'])
        disputed_row = next(row for row in result['grades'] if row['subject'] == 'Алгебра')
        self.assertEqual(disputed_row['votes']['docling']['grade'], 5)
        self.assertIsNone(disputed_row['grade'])
        self.assertIsNone(result['average'])


class BatchConsensusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.result, _ = ConsensusTests().result({'rapidocr': ['Алгебра 5', 'Физика 4'],
                                                  'tesseract': ['Алгебра 5', 'Физика 4']})
        def processor(files, kind):
            list(files)
            return copy.deepcopy(self.result)
        self.service = BatchService(self.temp.name, processor)
        output = io.BytesIO()
        Image.new('RGB', (100, 100), 'white').save(output, format='PNG')
        accepted = self.service.submit(output.getvalue(), 'sample.png')
        # The worker can be joined without closing the SQLite connection.
        self.service.worker.shutdown(wait=True)
        self.document = self.service.batch_result(accepted['batch_id'])['documents'][0]

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_calculation_is_available_before_identifier_review(self):
        self.assertEqual(self.document['calculation']['value'], '4.50')
        self.assertEqual(self.document['grades_verification'], 'automatic')
        self.assertFalse(self.document['requires_review'])

    def test_api_edit_cannot_keep_a_forged_automatic_flag(self):
        rows = copy.deepcopy(self.document['grades'])
        rows[0]['grade'] = 2
        rows[0]['auto_verified'] = True
        result = self.service.review(self.document['id'], {'revision': self.document['revision'],
                                                          'grades': rows, 'complete': False})
        self.assertFalse(result['grades'][0]['auto_verified'])
        self.assertIsNone(result['calculation'])


if __name__ == '__main__': unittest.main()
