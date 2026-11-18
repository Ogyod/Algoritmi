"""Prepare local document pages, run OCR and attach previews to the result."""
import base64
import io
import os
from pathlib import Path
import tempfile

from PIL import Image, ImageOps, UnidentifiedImageError
import pypdfium2 as pdfium

from core import DOCUMENT_TYPES
from ocr_adapter import parse_engine_rows, interpret_consensus
from ocr_engines import recognize_document
from uploads import MAX_FILE

MAX_PAGES = 20
Image.MAX_IMAGE_PIXELS = 30_000_000


def recognize(images, kind='auto'):
    return recognize_document(images, parser=parse_engine_rows, include_observations=True,
                              requested_type=kind,
                              fast_third=os.environ.get('OCR_FULL_THIRD') != '1')


def process(data, kind):
    return process_group([('upload', data)], kind)


def pdf_render_scale(page, width, height):
    scale = min(300 / 72, 3600 / width, 4800 / height)
    try:
        objects = list(page.get_objects())
        if len(objects) != 1 or not isinstance(objects[0], pdfium.PdfImage):
            return scale
        image = objects[0]
        left, bottom, right, top = image.get_bounds()
        image_width, image_height = right - left, top - bottom
        if image_width <= 0 or image_height <= 0 or image_width * image_height < .8 * width * height:
            return scale
        pixels_width, pixels_height = image.get_px_size()
        if pixels_width <= 0 or pixels_height <= 0:
            return scale
        native_dpi = 72 * max(pixels_width / image_width, pixels_height / image_height)
        if native_dpi <= 180:
            return min(scale, 200 / 72)
    except Exception:
        # An optional size check must never prevent a valid PDF from rendering.
        pass
    return scale


def process_group(files, kind):
    if not isinstance(kind, str) or (kind != 'auto' and kind not in DOCUMENT_TYPES):
        raise ValueError('Неизвестный тип документа.')
    pages = []
    images = []
    with tempfile.TemporaryDirectory(prefix='admissions-') as temp:
        def handle_image(image, source_name, source_page):
            number = len(pages) + 1
            if number > MAX_PAGES:
                raise ValueError('В одном документе допускается до 20 страниц.')
            image = ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((3600, 4800))
            path = Path(temp) / f'{number}.png'
            image.save(path, compress_level=1)
            enhanced = Path(temp) / f'{number}-enhanced.png'
            images.append({'page': number, 'path': str(path), 'enhanced_path': str(enhanced),
                           'width': image.width, 'height': image.height})
            image.thumbnail((1000, 1400))
            buffer = io.BytesIO()
            image.save(buffer, 'JPEG', quality=83)
            pages.append({'number': number, 'source_file': source_name, 'source_page': source_page,
                          'preview': 'data:image/jpeg;base64,'+base64.b64encode(buffer.getvalue()).decode()})
        for source_name, data in files:
            if not data or len(data) > MAX_FILE:
                raise ValueError('Допустимый размер каждого файла — от 1 байта до 20 МБ.')
            if data.startswith(b'%PDF-'):
                try:
                    doc = pdfium.PdfDocument(data)
                except Exception:
                    raise ValueError('Не удалось открыть PDF. Проверьте файл и отсутствие пароля.')
                try:
                    if not 1 <= len(doc) <= MAX_PAGES:
                        raise ValueError('PDF должен содержать от 1 до 20 страниц. Разделите большой файл.')
                    for i in range(len(doc)):
                        page = doc[i]
                        try:
                            w, h = page.get_size()
                            if w <= 0 or h <= 0:
                                raise ValueError('Некорректный размер страницы PDF.')
                            bitmap = page.render(scale=pdf_render_scale(page, w, h))
                            try:
                                handle_image(bitmap.to_pil(), source_name, i + 1)
                            finally:
                                bitmap.close()
                        finally:
                            page.close()
                finally:
                    doc.close()
            else:
                try:
                    with Image.open(io.BytesIO(data)) as image:
                        if image.format not in ['JPEG', 'PNG', 'TIFF', 'WEBP']:
                            raise ValueError('Поддерживаются PDF, JPEG, PNG, TIFF и WEBP.')
                        if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                            raise ValueError('Размер изображения превышает 30 миллионов пикселей.')
                        frames = getattr(image, 'n_frames', 1)
                        if frames > MAX_PAGES:
                            raise ValueError('Изображение должно содержать не более 20 страниц.')
                        for i in range(frames):
                            image.seek(i)
                            if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                                raise ValueError('Размер страницы изображения превышает 30 миллионов пикселей.')
                            handle_image(image.copy(), source_name, i + 1)
                except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
                    raise ValueError('Не удалось прочитать изображение. Используйте PDF, JPEG, PNG, TIFF или WEBP.')
        recognized = recognize(images, kind)
        result = interpret_consensus(recognized, kind)
        result['pages'] = pages
    return result
