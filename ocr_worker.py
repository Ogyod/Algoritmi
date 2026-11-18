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
from ocr_process import worker, shutdown_workers


def enhance_image(source, destination):
    from PIL import ImageOps, ImageFilter
    with Image.open(source) as image:
        ImageOps.autocontrast(ImageOps.grayscale(image).filter(
            ImageFilter.GaussianBlur(.8)), cutoff=1).save(destination, compress_level=1)


def native_tesseract():
    executable = os.environ.get('TESSERACT_CMD') or shutil.which('tesseract')
    if executable:
        return executable
    installed = Path(r'C:\Program Files\Tesseract-OCR\tesseract.exe')
    return str(installed) if installed.is_file() else None


def tesseract_node():
    root = Path(__file__).resolve().parent
    bundled = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    node_name = 'node.exe' if os.name == 'nt' else 'node'
    node = shutil.which('node') or str(bundled / 'bin' / node_name)
    packages = root / 'node_modules'
    if not (packages / 'tesseract.js').is_dir():
        packages = bundled / 'node_modules'
    return node, packages


def recognize(engine, pages):
    if engine != 'tesseract':
        raise ValueError('Unknown OCR engine')
    lines = []
    executable = native_tesseract()
    if not executable:
        root = Path(__file__).resolve().parent
        node, packages = tesseract_node()
        process = worker('tesseract-node', [node, str(root / 'ocr.cjs'), '--serve'],
                         {**os.environ, 'NODE_PATH': str(packages), 'OCR_WORKER_PYTHON': sys.executable})
        response = process.call(pages, timeout=100 + len(pages) * 45)
        if not response.get('ok'):
            raise RuntimeError('Tesseract failed')
        recognized = response['pages']
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
                        answer = recognize(sys.argv[1], payload['pages'])
                    response = {'ok': True, 'lines': answer}
                except Exception:
                    response = {'ok': False, 'lines': []}
                print(json.dumps(response, ensure_ascii=False), flush=True)
        finally:
            shutdown_workers()
    else:
        payload = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            answer = recognize(sys.argv[1], payload['pages'])
        print(json.dumps(answer, ensure_ascii=False))
