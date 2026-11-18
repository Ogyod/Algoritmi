"""Read local page images with Docling, retaining OCR text, geometry and scores."""
import contextlib
import json
import re
import sys
from pathlib import Path

from ocr_consensus import GRADE_END

GRADE_CELL = re.compile(
    r'(?:[2-5](?:\s*\([^)]*\))?|отлично|хорошо|удовлетворительно|неудовлетворительно)', re.I)


def rapid_model_paths(root):
    rapid = root / 'rapid'
    return [rapid / name for name in (
        'ch_PP-OCRv5_det_mobile.onnx', 'ch_ppocr_mobile_v2.0_cls_mobile.onnx',
        'eslav_PP-OCRv5_rec_mobile.onnx')]


def converter_for(models):
    root = Path(models)
    paths = rapid_model_paths(root)
    artifacts = root / 'docling'
    if not artifacts.is_dir() or not all(path.is_file() for path in paths):
        raise FileNotFoundError('Модели Docling не подготовлены (setup_ocr.ps1).')
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        OcrMode, PdfPipelineOptions, RapidOcrOptions, TableFormerMode,
        TableStructureOptions,
    )
    from docling.document_converter import DocumentConverter, ImageFormatOption
    options = PdfPipelineOptions()
    options.artifacts_path = artifacts
    options.enable_remote_services = False
    options.allow_external_plugins = False
    options.document_timeout = 90
    options.generate_parsed_pages = True
    options.do_ocr = True
    options.do_table_structure = True
    options.table_structure_options = TableStructureOptions(mode=TableFormerMode.FAST)
    options.ocr_options = RapidOcrOptions(
        mode=OcrMode.FULL_PAGE, lang=['iso:ru'], scale=1.0, text_score=0.0,
        det_model_path=str(paths[0]), cls_model_path=str(paths[1]),
        rec_model_path=str(paths[2]))
    options.accelerator_options.num_threads = 2
    return DocumentConverter(format_options={InputFormat.IMAGE: ImageFormatOption(pipeline_options=options)})


def table_rows(document):
    """Keep only actual table cells; a layout paragraph is not a grade table."""
    rows = []
    for table in document.tables:
        frame = table.export_to_dataframe(doc=document)
        for cells in frame.itertuples(index=False, name=None):
            parts = [str(cell).strip() for cell in cells if cell is not None and str(cell).strip()]
            if len(parts) >= 4 and len(parts) % 2 == 0 and all(
                    GRADE_CELL.fullmatch(parts[i + 1]) for i in range(0, len(parts), 2)):
                rows.extend(f'{parts[i]} {parts[i + 1]}'[:500] for i in range(0, len(parts), 2))
            elif parts and all(GRADE_END.fullmatch(part) for part in parts):
                rows.extend(part[:500] for part in parts)
            elif len(parts) >= 2:
                rows.append(' '.join(parts)[:500])
    return rows[:100]


def extract(page, converter):
    result = converter.convert(Path(page['path']))
    parsed = result.pages[0]
    lines = []
    sx = page.get('width', parsed.size.width) / parsed.size.width
    sy = page.get('height', parsed.size.height) / parsed.size.height
    for cell in parsed.cells:
        if not cell.text.strip():
            continue
        box = cell.to_bounding_box().to_top_left_origin(parsed.size.height)
        lines.append({'text': cell.text.strip(), 'confidence': cell.confidence,
                      'page': page['page'], 'box': [round(box.l*sx), round(box.t*sy),
                                                    round(box.r*sx), round(box.b*sy)]})
    return {'page': page['page'], 'complete': result.status.value == 'success',
            'lines': lines, 'table': table_rows(result.document)}


def main():
    converter = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            with contextlib.redirect_stdout(sys.stderr):
                if converter is None:
                    converter = converter_for(request['models'])
                pages = []
                for page in request['pages']:
                    try:
                        pages.append(extract(page, converter))
                    except Exception:
                        pages.append({'page': page['page'], 'complete': False, 'lines': [], 'table': []})
            response = {'ok': True, 'pages': pages}
        except Exception:
            response = {'ok': False, 'pages': [], 'error': 'Docling не смог разобрать таблицу.'}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
