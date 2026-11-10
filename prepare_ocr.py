"""Explicit download/verification of local models, using a synthetic project sample."""
import json
from pathlib import Path
from PIL import Image
from ocr_worker import recognize

ROOT = Path(__file__).resolve().parent


def main():
    models = ROOT / '.ocr-models'
    models.mkdir(exist_ok=True)
    sample = ROOT / 'samples/certificate_9.png'
    with Image.open(sample) as image:
        page = {'page': 1, 'path': str(sample), 'width': image.width, 'height': image.height}
    for engine in ('rapidocr', 'easyocr'):
        print(engine + ': подготовка модели', flush=True)
        if not recognize(engine, [page], models, download=True):
            raise RuntimeError(engine + ': не прочитан контрольный образец')
    for engine in ('rapidocr', 'tesseract', 'easyocr'):
        if not recognize(engine, [page], models, download=False):
            raise RuntimeError(engine + ': проверка локальной модели не пройдена')
        print(engine + ': готов', flush=True)


if __name__ == '__main__': main()
