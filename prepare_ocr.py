"""Prepare the local models used for Russian OCR, layout and grade tables."""
from hashlib import file_digest
from pathlib import Path
from shutil import copyfileobj
from urllib.request import urlopen

from docling_worker import rapid_model_paths

ROOT = Path(__file__).resolve().parent
MODEL_URL = 'https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/'
ARTIFACTS = {
    'ch_PP-OCRv5_det_mobile.onnx': (
        'PP-OCRv5/det', '4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae'),
    'ch_ppocr_mobile_v2.0_cls_mobile.onnx': (
        'PP-OCRv4/cls', 'e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c'),
    'eslav_PP-OCRv5_rec_mobile.onnx': (
        'PP-OCRv5/rec', '08705d6721849b1347d26187f15a5e362c431963a2a62bfff4feac578c489aab'),
}


def matches(path, expected):
    if not path.is_file():
        return False
    with path.open('rb') as model:
        return file_digest(model, 'sha256').hexdigest() == expected


def prepare(path):
    subdir, checksum = ARTIFACTS[path.name]
    if matches(path, checksum):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f'Скачиваю модель OCR: {path.name}', flush=True)
    partial = path.with_suffix('.onnx.part')
    try:
        with urlopen(f'{MODEL_URL}{subdir}/{path.name}', timeout=90) as response, partial.open('wb') as out:
            copyfileobj(response, out)
        if not matches(partial, checksum):
            raise ValueError(f'Контрольная сумма модели не совпала: {path.name}')
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


if __name__ == '__main__':
    from huggingface_hub import snapshot_download

    models = ROOT / '.ocr-models'
    for model in rapid_model_paths(models):
        prepare(model)
    # These snapshots match the layout and FAST table models in docling_worker.py.
    for repo, revision, patterns in (
        ('docling-project/docling-layout-heron', '8f39ad3c0b4c58e9c2d2c84a38465abf757272d8',
         ['*.json', 'model.safetensors', 'README*', 'LICENSE*']),
        ('docling-project/docling-models', 'fc0f2d45e2218ea24bce5045f58a389aed16dc23',
         ['model_artifacts/tableformer/fast/*', 'README*', 'LICENSE*']),
    ):
        snapshot_download(repo, revision=revision, allow_patterns=patterns,
                          local_dir=models / 'docling' / repo.replace('/', '--'))
