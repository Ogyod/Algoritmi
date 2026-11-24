import unittest
from unittest.mock import Mock, patch

import server
from ocr_engines import warmup_tesseract


class ServerStartupTests(unittest.TestCase):
    def test_warmup_finishes_before_server_creation_and_browser_open(self):
        local_server = Mock(server_port=8765)
        with patch.multiple(server, warmup_tesseract=Mock(return_value=True),
                            warmup_docling=Mock(return_value=True),
                            make_server=Mock(return_value=local_server)), \
                patch.object(server.threading, 'Timer') as timer, \
                patch('sys.argv', ['server.py', '--open']), patch('builtins.print'):
            calls = Mock()
            calls.attach_mock(server.warmup_tesseract, 'tesseract')
            calls.attach_mock(server.warmup_docling, 'docling')
            calls.attach_mock(server.make_server, 'make_server')
            calls.attach_mock(timer.return_value.start, 'open_browser')
            calls.attach_mock(local_server.serve_forever, 'serve')
            server.main()

        self.assertEqual([call[0] for call in calls.mock_calls],
                         ['tesseract', 'docling', 'make_server', 'open_browser', 'serve'])
        self.assertIs(local_server.ocr_ready, True)
        self.assertIs(local_server.docling_ready, True)
        local_server.server_close.assert_called_once()

    def test_failed_warmup_leaves_site_closed(self):
        for failing in ('Tesseract', 'Docling'):
            with self.subTest(engine=failing), \
                    patch.multiple(server, warmup_tesseract=Mock(return_value=failing != 'Tesseract'),
                                   warmup_docling=Mock(return_value=False)), \
                    patch.object(server, 'make_server') as make_server, \
                    patch.object(server, 'shutdown_workers') as shutdown, \
                    patch.object(server.threading, 'Timer') as timer, \
                    patch('sys.argv', ['server.py', '--open']), patch('builtins.print'):
                with self.assertRaisesRegex(SystemExit, failing):
                    server.main()
                make_server.assert_not_called()
                timer.assert_not_called()
                shutdown.assert_called_once()

    def test_tesseract_checks_both_sample_pages(self):
        with patch('ocr_engines.run_engine', return_value={
                'ok': True, 'lines': [{'page': 1, 'text': 'Аттестат'}]}) as recognize:
            self.assertFalse(warmup_tesseract())
        engine, pages = recognize.call_args.args
        self.assertEqual(engine, 'tesseract')
        self.assertEqual([page['page'] for page in pages], [1, 2])


if __name__ == '__main__':
    unittest.main()
