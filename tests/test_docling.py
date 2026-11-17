"""Docling output is table evidence, never a second OCR vote."""
import sys
from pathlib import Path
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docling_worker import extract
from ocr_consensus import parse_lines


class Table:
    def export_to_dataframe(self, doc=None):
        return SimpleNamespace(itertuples=lambda **kwargs: iter([
            ('Русский язык', '5', 'Алгебра', '4'),
            ('Физика', '4'), ('Биология 5', 'История 4'),
            ('География 5', ''),
            ('Заголовок', ''),
        ]))


class DoclingTests(unittest.TestCase):
    def test_only_table_cells_become_candidate_rows(self):
        document = SimpleNamespace(tables=[Table()])
        converter = SimpleNamespace(convert=lambda path: SimpleNamespace(document=document))
        rows = extract([{'page': 2, 'path': '/unused.png'}], '/unused', converter)
        self.assertEqual(rows, [{'page': 2, 'text': 'Русский язык 5'},
                                {'page': 2, 'text': 'Алгебра 4'},
                                {'page': 2, 'text': 'Физика 4'},
                                {'page': 2, 'text': 'Биология 5'},
                                {'page': 2, 'text': 'История 4'},
                                {'page': 2, 'text': 'География 5'}])
        parsed = parse_lines(rows)
        self.assertEqual([(row['subject'], row['grade']) for row in parsed], [
            ('Русский язык', 5), ('Алгебра', 4), ('Физика', 4),
            ('Биология', 5), ('История', 4), ('География', 5),
        ])
