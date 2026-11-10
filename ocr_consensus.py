"""Parse OCR observations and reconcile independent engines without inventing grades."""
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
import re
import unicodedata

WORDS = {'отлично': 5, 'хорошо': 4, 'удовлетворительно': 3, 'неудовлетворительно': 2}
WORD_PATTERN = '|'.join(sorted(WORDS, key=len, reverse=True))
GRADE_END = re.compile(r'^(.+?)\s+(?:([2-5зЗ?])(?:\s*\(?(' + WORD_PATTERN + r')\)?)?|(' + WORD_PATTERN + r'))\s*[.;,]?$', re.I)
SUBJECT_HINTS = ('математик', 'алгебр', 'геометри', 'русский язык', 'литератур', 'истори', 'обществознан',
                 'физик', 'хими', 'биологи', 'географи', 'информатик', 'иностранный язык', 'английск',
                 'немецк', 'французск', 'физическая культура', 'безопасности', 'технологи', 'астрономи',
                 'музык', 'искусств', 'родной язык', 'индивидуальный проект', 'вероятность', 'статистика')


def normalized(text):
    return ' '.join(re.sub(r'[^\w\s]', ' ', unicodedata.normalize('NFKC', str(text)).casefold().replace('ё', 'е')).split())


def _ordered_fragments(lines):
    bands = []
    for line in sorted(lines, key=lambda r: (r['page'], (r.get('box') or [0, 0])[1])):
        box = line.get('box')
        target = None
        if box:
            for band in reversed(bands[-6:]):
                other = band[0].get('box')
                if other and band[0]['page'] == line['page']:
                    overlap = min(box[3], other[3]) - max(box[1], other[1])
                    if overlap > .55 * min(box[3] - box[1], other[3] - other[1]):
                        target = band
                        break
        if target is None:
            bands.append([line])
        else:
            target.append(line)
    return [line for band in bands for line in sorted(band, key=lambda r: (r.get('box') or [0])[0])]


def parse_lines(lines, subject_hints=()):
    merged = []
    for line in _ordered_fragments(lines):
        line = dict(line)
        box = line.get('box')
        previous = merged[-1] if merged else None
        if box and previous and previous.get('box') and previous['page'] == line['page']:
            other = previous['box']
            overlap = min(box[3], other[3]) - max(box[1], other[1])
            # Stop after a complete pair: the next fragment can be another table column.
            if (not GRADE_END.match(previous['text'].strip()) and
                    overlap > .55 * min(box[3] - box[1], other[3] - other[1]) and box[0] >= other[2] - 2):
                previous['text'] += ' ' + line['text']
                previous['box'] = [other[0], min(other[1], box[1]), box[2], max(other[3], box[3])]
                previous['confidence'] = min(previous.get('confidence', 0), line.get('confidence', 0))
                continue
        merged.append(line)
    result = []
    for line in merged:
        text = line['text'].strip()
        match = GRADE_END.match(text)
        conflict = False
        if match:
            subject = match[1]
            digit = int(match[2]) if match[2] in ('2', '3', '4', '5') else None
            word = (match[3] or match[4] or '').lower()
            grade = digit if digit is not None else WORDS.get(word)
            conflict = digit is not None and bool(word) and digit != WORDS[word]
            if conflict:
                grade = None
        elif any(hint in normalized(text) for hint in (*SUBJECT_HINTS, *subject_hints)) and len(text) < 160:
            subject, grade = text, None
        else:
            continue
        subject = re.sub(r'^\s*\d+[.)]\s*', '', subject).strip(' .:—-')
        if len(subject) < 2:
            continue
        # A row containing more than one numeric pair needs explicit table segmentation.
        if re.search(r'\s[2-5](?:\s|\()', subject):
            grade, conflict = None, True
        result.append({'subject': subject, 'grade': grade, 'include': True, 'reason': '', 'verified': False,
                       'confidence': round(float(line.get('confidence', 0)), 4), 'page': line['page'],
                       'box': line.get('box'), 'source': text, 'conflict': conflict})
    return result


def _same_row(first, second):
    if first['page'] != second['page']:
        return False
    a, b = normalized(first['subject']), normalized(second['subject'])
    if a == b:
        return True
    left, right = first.get('box'), second.get('box')
    if not left or not right or SequenceMatcher(None, a, b).ratio() < .84:
        return False
    height = max(left[3] - left[1], right[3] - right[1], 1)
    return abs((left[1] + left[3] - right[1] - right[3]) / 2) < height * .7 and abs(left[0] - right[0]) < height * 4


def reconcile(observations, threshold=.75, parser=None):
    """One vote per engine; a quorum requires the same subject AND grade."""
    parser = parser or parse_lines
    buckets = []
    for observation in observations:
        if not observation.get('ok'):
            continue
        for row in parser(observation['lines']):
            candidates = [b for b in buckets if _same_row(b['anchor'], row)]
            bucket = candidates[0] if len(candidates) == 1 else None
            if bucket is None:
                bucket = {'anchor': row, 'votes': {}, 'duplicate': False}
                buckets.append(bucket)
            engine = observation['engine']
            if engine in bucket['votes']:
                bucket['duplicate'] = True
            else:
                bucket['votes'][engine] = row
    rows = []
    for bucket in buckets:
        counts = Counter((normalized(v['subject']), v['grade']) for v in bucket['votes'].values()
                         if type(v['grade']) is int and not v['conflict'] and v['confidence'] >= threshold)
        winner, count = counts.most_common(1)[0] if counts else (None, 0)
        accepted = count >= 2 and not bucket['duplicate']
        winning_votes = {name: v for name, v in bucket['votes'].items()
                         if accepted and (normalized(v['subject']), v['grade']) == winner
                         and v['confidence'] >= threshold and not v['conflict']}
        chosen = max(winning_votes.values(), key=lambda v: v['confidence']) if winning_votes else bucket['anchor']
        row = dict(chosen)
        row['auto_verified'] = accepted
        row['verified'] = False
        row['verification'] = 'ocr_consensus' if accepted else 'manual_required'
        row['consensus_status'] = ('third_resolved' if 'easyocr' in winning_votes else 'agreed') if accepted else 'unresolved'
        row['votes'] = bucket['votes']
        row['supporting_engines'] = list(winning_votes)
        row['issue'] = '' if accepted else ('Повтор предмета в результате OCR.' if bucket['duplicate'] else
                                           'Нет двух уверенных совпадений предмета и оценки.')
        if not accepted:
            # Never publish an arbitrary candidate as the resolved numeric result.
            row['grade'] = None
        row['conflict'] = not accepted
        identity = {'page': row['page'], 'subject': row['subject'], 'votes': bucket['votes']}
        row['row_id'] = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        rows.append(row)
    rows.sort(key=lambda r: (r['page'], (r.get('box') or [0, 0])[1], (r.get('box') or [0])[0]))
    # Identical names in different buckets are still ambiguous for the arithmetic.
    names = Counter(normalized(row['subject']) for row in rows)
    for row in rows:
        if names[normalized(row['subject'])] > 1:
            row.update(auto_verified=False, verification='manual_required', consensus_status='unresolved',
                       issue='Повторяется предмет.', conflict=True)
    return rows


def needs_third(observations, rows):
    return not rows or any(not r['auto_verified'] for r in rows) or any(not o.get('ok') for o in observations)
