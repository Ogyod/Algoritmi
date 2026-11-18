"""Prepare the Russian RapidOCR weights used inside Docling."""
from pathlib import Path
from rapidocr import RapidOCR, OCRVersion, ModelType, LangRec

ROOT = Path(__file__).resolve().parent


def main():
    directory = ROOT / '.ocr-models' / 'rapid'
    directory.mkdir(parents=True, exist_ok=True)
    RapidOCR(params={'Global.model_root_dir': str(directory),
                     'Det.ocr_version': OCRVersion.PPOCRV5, 'Det.model_type': ModelType.MOBILE,
                     'Rec.ocr_version': OCRVersion.PPOCRV5, 'Rec.model_type': ModelType.MOBILE,
                     'Rec.lang_type': LangRec.ESLAV})
    from docling_worker import rapid_model_paths
    for path in rapid_model_paths(directory.parent):
        if not path.is_file():
            raise FileNotFoundError(f'Не подготовлена модель Docling: {path.name}')


if __name__ == '__main__':
    main()
