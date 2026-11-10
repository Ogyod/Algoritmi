import json
from pathlib import Path
from types import SimpleNamespace
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
            def setDetector(self, *args):
                raise AssertionError('Detection is unnecessary for a confident complete row')
        page={'page':1,'path':'unused','regions':[[0,10,250,70]],'line_regions':[[10,20,210,50]]}
        with patch('ocr_worker.easy_reader',return_value=Reader()), patch('ocr_worker.image_array',return_value=np.zeros((400,400,3),dtype=np.uint8)):
            rows=recognize('easyocr',[page],Path('unused'))
        self.assertEqual(rows[0]['box'],[10,20,210,50])
        self.assertEqual(rows[0]['text'],'Алгебра 5')

    def test_uncertain_third_read_keeps_full_detection_fallback(self):
        import numpy as np
        class Reader:
            def setDetector(self, network):
                if hasattr(self, 'detector'):
                    raise AssertionError('Reuse the detector after it is loaded')
                self.detector = SimpleNamespace(network=network, to=self.set_layout)
            def set_layout(self, *, memory_format):
                self.memory_format = memory_format
            def recognize(self, image, **kwargs):
                return [([[0,0],[200,0],[200,30],[0,30]], 'Алгебра ?', .3)]
            def readtext(self, *args, **kwargs):
                if self.detector.network != 'craft' or self.memory_format != 'channels_last':
                    raise AssertionError('Detection must use the configured CRAFT model')
                return [([[0,0],[200,0],[200,30],[0,30]], 'Алгебра 5', .98)]
        page={'page':1,'path':'unused','regions':[[0,10,250,70]],'line_regions':[[10,20,210,50]]}
        reader = Reader()
        with patch.dict('sys.modules', {'torch': SimpleNamespace(channels_last='channels_last')}), \
                patch('ocr_worker.easy_reader', return_value=reader) as load_reader, \
                patch('ocr_worker.image_array', return_value=np.zeros((400,400,3), dtype=np.uint8)):
            rows=recognize('easyocr',[page, {**page, 'page': 2}],Path('unused'))
        load_reader.assert_called_once_with(Path('unused'), False)
        self.assertEqual([(row['page'], row['box']) for row in rows], [(1, [0,10,200,40]), (2, [0,10,200,40])])

    def test_native_tesseract_uses_bundled_languages_and_returns_pages_in_order(self):
        tsv = ('block_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
               '1\t1\t1\t10\t20\t200\t30\t95\tАлгебра 5\n').encode()
        with patch.dict('os.environ', {'TESSERACT_CMD': 'tesseract'}, clear=True), \
                patch('ocr_worker.subprocess.run', return_value=SimpleNamespace(stdout=tsv)) as command:
            rows = recognize('tesseract', [{'path': 'p2.png', 'page': 2}, {'path': 'p1.png', 'page': 1}], Path('unused'))
        self.assertEqual([(row['page'], row['text']) for row in rows], [(2, 'Алгебра 5'), (1, 'Алгебра 5')])
        self.assertEqual(command.call_count, 2)
        for call in command.call_args_list:
            args = call.args[0]
            self.assertEqual(args[args.index('--tessdata-dir') + 1], str(Path(__file__).resolve().parents[1] / '.models'))
            self.assertIn('tessedit_create_tsv=1', args)

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
