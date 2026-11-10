"""One engine per subprocess, models loaded once for a batch of pages."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from PIL import Image
from functools import lru_cache
from ocr_process import worker, shutdown_workers


@lru_cache(maxsize=4)
def rapid_reader(models, download):
    from rapidocr import RapidOCR, OCRVersion, ModelType, LangRec
    manifest = models / 'rapid-ready.json'
    if not download and not manifest.exists():
        raise RuntimeError('Run setup_ocr.ps1 first')
    params = {'Global.model_root_dir': str(models / 'rapid'), 'Global.text_score': 0.0,
              'Global.log_level': 'error', 'Det.ocr_version': OCRVersion.PPOCRV5,
              'Det.model_type': ModelType.MOBILE, 'Rec.ocr_version': OCRVersion.PPOCRV5,
              'Rec.model_type': ModelType.MOBILE, 'Rec.lang_type': LangRec.ESLAV,
              'EngineConfig.onnxruntime.intra_op_num_threads': 2,
              'EngineConfig.onnxruntime.inter_op_num_threads': 1}
    if not download:
        for key, value in json.loads(manifest.read_text(encoding='utf-8')).items():
            value = str(Path(value) if Path(value).is_absolute() else models / value)
            if not Path(value).is_file():
                raise RuntimeError('Missing local OCR model')
            params[key] = value
    return RapidOCR(params=params)


@lru_cache(maxsize=4)
def easy_reader(models, download):
    import easyocr
    import torch
    torch.set_num_threads(min(4, os.cpu_count() or 2))
    return easyocr.Reader(['ru', 'en'], gpu=False, model_storage_directory=str(models / 'easyocr'),
                          user_network_directory=str(models / 'easyocr-user'),
                          download_enabled=download, verbose=False, quantize=False, detector=False)


def enhance_image(source, destination):
    from PIL import ImageOps, ImageFilter
    with Image.open(source) as image:
        ImageOps.autocontrast(ImageOps.grayscale(image).filter(
            ImageFilter.GaussianBlur(.8)), cutoff=1).save(destination, compress_level=1)


def image_array(path):
    import numpy as np
    # OpenCV imread cannot reliably open Cyrillic Windows paths.
    with Image.open(path) as image:
        return np.asarray(image.convert('RGB'))


def rectangle(points):
    return [int(min(p[0] for p in points)), int(min(p[1] for p in points)),
            int(max(p[0] for p in points)), int(max(p[1] for p in points))]


def merge_regions(regions, width, height):
    """Read overlapping disputed rows once; retain their complete pixel coverage."""
    merged = []
    for x0, y0, x1, y1 in regions:
        box = [max(0, x0), max(0, y0), min(width, x1), min(height, y1)]
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        index = 0
        while index < len(merged):
            other = merged[index]
            if (min(box[2], other[2]) >= max(box[0], other[0]) and
                    min(box[3], other[3]) >= max(box[1], other[1])):
                box = [min(box[0], other[0]), min(box[1], other[1]),
                       max(box[2], other[2]), max(box[3], other[3])]
                merged.pop(index)
                index = 0
            else:
                index += 1
        merged.append(box)
    return sorted(merged, key=lambda box: (box[1], box[0]))


def recognize(engine, pages, models, download=False):
    lines = []
    if engine == 'rapidocr':
        manifest = models / 'rapid-ready.json'
        reader = rapid_reader(models, download)
        for page in pages:
            result = reader(image_array(page['path'])[:, :, ::-1].copy())
            if result.boxes is None:
                continue
            for box, text, score in zip(result.boxes, result.txts, result.scores):
                lines.append({'text': text, 'confidence': float(score), 'page': page['page'], 'box': rectangle(box)})
        if download:
            from rapidocr.inference_engine.base import FileInfo
            local = {}
            for prefix, part in [('Det', reader.text_det), ('Cls', reader.text_cls), ('Rec', reader.text_rec)]:
                cfg = reader.cfg[prefix]
                info = part.session.get_model_url(FileInfo(engine_type=cfg.engine_type, ocr_version=cfg.ocr_version,
                                                          task_type=cfg.task_type, lang_type=cfg.lang_type, model_type=cfg.model_type))
                local[prefix + '.model_path'] = 'rapid/' + Path(info['model_dir']).name
            manifest.write_text(json.dumps(local, indent=2), encoding='utf-8')
    elif engine == 'easyocr':
        reader = easy_reader(models, download)
        for page in pages:
            array = image_array(page['path'])
            regions = page.get('regions') or [[0, 0, array.shape[1], array.shape[0]]]
            direct = page.get('line_regions') or []
            for index, (x0, y0, x1, y1) in enumerate(regions):
                x0, y0, x1, y1 = max(0, x0), max(0, y0), min(array.shape[1], x1), min(array.shape[0], y1)
                if x1 <= x0 or y1 <= y0:
                    continue
                answer = None
                if index < len(direct):
                    left, top, right, bottom = direct[index]
                    left, top = max(0,left), max(0,top)
                    right, bottom = min(array.shape[1],right), min(array.shape[0],bottom)
                    if right-left > 3*(bottom-top) and 0 < bottom-top < array.shape[0]*.12:
                        crop = Image.fromarray(array[top:bottom,left:right]).convert('L')
                        import numpy as np
                        attempted = reader.recognize(np.asarray(crop), detail=1, paragraph=False)
                        from ocr_consensus import GRADE_END
                        if (attempted and all(float(item[2]) >= .75 and GRADE_END.match(item[1].strip())
                                              for item in attempted)):
                            answer = attempted
                            x0, y0 = left, top
                if answer is None:
                    if not hasattr(reader, 'detector'):
                        import torch
                        reader.setDetector('craft')
                        # Keep the CRAFT weights in the layout used by CPU convolutions.
                        reader.detector.to(memory_format=torch.channels_last)
                    answer = reader.readtext(array[y0:y1, x0:x1].copy(), detail=1, paragraph=False)
                for box, text, score in answer:
                    shifted = [[x + x0, y + y0] for x, y in box]
                    lines.append({'text': text, 'confidence': float(score), 'page': page['page'], 'box': rectangle(shifted)})
    elif engine == 'tesseract':
        configured = os.environ.get('TESSERACT_CMD')
        executable = configured or shutil.which('tesseract')
        installed = Path(r'C:\Program Files\Tesseract-OCR\tesseract.exe')
        executable = executable or (str(installed) if installed.exists() else None)
        if not executable:
            root = Path(__file__).resolve().parent
            bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
            node = shutil.which('node') or str(bundled / 'bin/node.exe')
            packages = root / 'node_modules'
            if not (packages / 'tesseract.js').exists():
                packages = bundled / 'node_modules'
            project_tesseract = (root / 'ocr.cjs').is_file()
            bridge = root / ('ocr.cjs' if project_tesseract else 'tesseract_bridge.cjs')
            payload = pages if project_tesseract else {'pages': pages, 'models': str(models / 'tesseract')}
            if project_tesseract:
                process = worker('tesseract-node', [node, str(bridge), '--serve'],
                                 {**os.environ, 'NODE_PATH': str(packages), 'OCR_WORKER_PYTHON': sys.executable})
                response = process.call(payload, timeout=100 + len(pages) * 45)
                if not response.get('ok'):
                    raise RuntimeError('Tesseract failed')
                recognized = response['pages']
            else:
                result = subprocess.run([node, str(bridge)], input=json.dumps(payload),
                                        text=True, encoding='utf-8', capture_output=True, check=True,
                                        timeout=100 + len(pages) * 45, env={**os.environ, 'NODE_PATH': str(packages)})
                recognized = json.loads(result.stdout)
            if not project_tesseract:
                return recognized
            for page, blocks in zip(pages, recognized):
                for block in blocks:
                    w, h = page['width'], page['height']
                    lines.append({'text': block['text'], 'confidence': block['confidence'], 'page': page['page'],
                                  'box': [int(block['x']*w), int(block['y']*h),
                                          int((block['x']+block['width'])*w), int((block['y']+block['height'])*h)]})
            return lines
        tessdata = Path(__file__).resolve().parent / '.models'
        parameters = ['-l', 'rus+eng', '--psm', '3', '-c', 'tessedit_create_tsv=1']
        if not os.environ.get('TESSDATA_PREFIX') and all((tessdata / f'{lang}.traineddata').is_file()
                                                       for lang in ('rus', 'eng')):
            parameters.extend(['--tessdata-dir', str(tessdata)])
        parallel = len(pages) > 1 and os.environ.get('OCR_PAGE_WORKERS') != '1'
        env = os.environ.copy()
        if parallel:
            env.setdefault('OMP_THREAD_LIMIT', '1')

        def recognize_page(page):
            result = subprocess.run([executable, page['path'], 'stdout', *parameters],
                                    capture_output=True, check=True, timeout=90, env=env)
            groups = {}
            for word in csv.DictReader(io.StringIO(result.stdout.decode('utf-8')), delimiter='\t'):
                if word.get('text', '').strip() and float(word['conf']) >= 0:
                    key = tuple(word[k] for k in ('block_num', 'par_num', 'line_num'))
                    groups.setdefault(key, []).append(word)
            return [{'text': ' '.join(w['text'] for w in words), 'page': page['page'],
                     'confidence': min(float(w['conf']) for w in words) / 100,
                     'box': [min(int(w['left']) for w in words), min(int(w['top']) for w in words),
                             max(int(w['left']) + int(w['width']) for w in words),
                             max(int(w['top']) + int(w['height']) for w in words)]}
                    for words in groups.values()]

        if parallel:
            with ThreadPoolExecutor(max_workers=2) as pool:
                return [line for page_lines in pool.map(recognize_page, pages) for line in page_lines]
        for page in pages:
            lines.extend(recognize_page(page))
    else:
        raise ValueError('Unknown OCR engine')
    return lines


if __name__ == '__main__':
    if sys.argv[1] == '--enhance':
        enhance_image(sys.argv[2], sys.argv[3])
    elif '--serve' in sys.argv:
        try:
            for line in sys.stdin:
                try:
                    payload = json.loads(line)
                    with contextlib.redirect_stdout(sys.stderr):
                        models = Path(payload['models'])
                        if payload.get('warmup') and sys.argv[1] == 'rapidocr':
                            rapid_reader(models, False)
                            answer = []
                        elif payload.get('warmup') and sys.argv[1] == 'easyocr':
                            easy_reader(models, False)
                            answer = []
                        else:
                            answer = recognize(sys.argv[1], payload['pages'], models)
                    response = {'ok': True, 'lines': answer}
                except Exception:
                    response = {'ok': False, 'lines': []}
                print(json.dumps(response, ensure_ascii=False), flush=True)
        finally:
            shutdown_workers()
    else:
        payload = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            answer = recognize(sys.argv[1], payload['pages'], Path(payload['models']))
        print(json.dumps(answer, ensure_ascii=False))
