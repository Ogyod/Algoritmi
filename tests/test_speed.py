import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ocr_process import JSONWorker
from ocr_worker import merge_regions
from ocr_worker import recognize
from test_consensus import observation
from ocr_engines import recognize_document
from ocr_adapter import interpret_consensus, parse_engine_rows


class SpeedTests(unittest.TestCase):
    def test_third_reads_pixels_independently_without_repeating_detection(self):
        import numpy as np
        class Reader:
            def recognize(self, image, **kwargs):
                return [([[0,0],[200,0],[200,30],[0,30]], 'Алгебра 5', .98)]
            def readtext(self, *args, **kwargs):
                raise AssertionError('Detection is unnecessary for a confident complete row')
        page={'page':1,'path':'unused','regions':[[0,10,250,70]],'line_regions':[[10,20,210,50]]}
        with patch('ocr_worker.easy_reader',return_value=Reader()), patch('ocr_worker.image_array',return_value=np.zeros((400,400,3),dtype=np.uint8)):
            rows=recognize('easyocr',[page],Path('unused'))
        self.assertEqual(rows[0]['box'],[10,20,210,50])
        self.assertEqual(rows[0]['text'],'Алгебра 5')

    def test_uncertain_third_read_keeps_full_detection_fallback(self):
        import numpy as np
        class Reader:
            def recognize(self, image, **kwargs):
                return [([[0,0],[200,0],[200,30],[0,30]], 'Алгебра ?', .3)]
            def readtext(self, *args, **kwargs):
                return [([[0,0],[200,0],[200,30],[0,30]], 'Алгебра 5', .98)]
        page={'page':1,'path':'unused','regions':[[0,10,250,70]],'line_regions':[[10,20,210,50]]}
        with patch('ocr_worker.easy_reader',return_value=Reader()), patch('ocr_worker.image_array',return_value=np.zeros((400,400,3),dtype=np.uint8)):
            rows=recognize('easyocr',[page],Path('unused'))
        self.assertEqual(rows[0]['box'],[0,10,200,40])
    def test_worker_reuses_process_without_reusing_document_results(self):
        script = "import sys,json,os\nfor line in sys.stdin: print(json.dumps({'pid':os.getpid(),'value':json.loads(line)}),flush=True)"
        worker = JSONWorker([sys.executable, '-u', '-c', script])
        try:
            first = worker.call({'document': 1}, 5)
            second = worker.call({'document': 2}, 5)
            self.assertEqual(first['pid'], second['pid'])
            self.assertEqual(second['value'], {'document': 2})
            worker.process.kill()
            worker.process.wait()
            self.assertNotEqual(worker.call({}, 5)['pid'], first['pid'])
        finally:
            worker.close()

    def test_overlapping_regions_are_read_once(self):
        self.assertEqual(merge_regions([[-2, 10, 90, 40], [0, 30, 100, 60]], 100, 100),
                         [[0, 10, 100, 60]])

    def test_matching_grades_with_different_numbers_need_review(self):
        def runner(engine, pages):
            answer = observation(engine, ['Алгебра 5'])
            if engine == 'tesseract':
                answer['lines'][1]['text'] = 'Серия АБ Номер 9999999'
            return answer
        result = interpret_consensus(recognize_document([{'page': 1}], runner,
                                                       parse_engine_rows, True))
        self.assertTrue(result['auto_grades_verified'])
        self.assertFalse(result['reviewed'])
