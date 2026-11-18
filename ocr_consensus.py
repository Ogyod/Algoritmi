"""Parse OCR observations and reconcile independent engines without inventing grades."""
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
import re
import unicodedata

WORDS = {'отлично': 5, 'хорошо': 4, 'удовлетворительно': 3, 'неудовлетворительно': 2}
OCR_THRESHOLD = .80
WORD_PATTERN = '|'.join(sorted(WORDS, key=len, reverse=True))
GRADE_END = re.compile(r'^(.+?)\s+(?:([2-5зЗ?])(?:\s*\(?(' + WORD_PATTERN + r')\)?)?|(' + WORD_PATTERN + r'))\s*[.;,_|]*$', re.I)
SUBJECT_HINTS = ('математик', 'алгебр', 'геометри', 'русский язык', 'литератур', 'истори', 'обществознан',
                 'физик', 'хими', 'биологи', 'географи', 'информатик', 'иностранный язык', 'английск',
                 'немецк', 'французск', 'физическая культура', 'безопасности', 'технологи', 'астрономи',
                 'музык', 'искусств', 'родной язык', 'индивидуальный проект', 'вероятность', 'статистика')


def normalized(text):
    return ' '.join(re.sub(r'[^\w\s]', ' ', unicodedata.normalize('NFKC', str(text)).casefold().replace('ё', 'е')).split())


def _same_band(first, second):
    overlap = min(first[3], second[3]) - max(first[1], second[1])
    # A tall scan artifact can overlap several rows. Compare with the taller
    # box so it cannot be used as the anchor for the entire subject column.
    height = max(first[3] - first[1], second[3] - second[1])
    return height > 0 and overlap > .55 * height


def _ordered_fragments(lines):
    bands = []
    for line in sorted(lines, key=lambda r: (r['page'], (r.get('box') or [0, 0])[1])):
        box = line.get('box')
        target = None
        if box:
            for band in reversed(bands[-6:]):
                other = band[0].get('box')
                if other and band[0]['page'] == line['page']:
                    if _same_band(box, other):
                        target = band
                        break
        if target is None:
            bands.append([line])
        else:
            target.append(line)
    return [line for band in bands for line in sorted(band, key=lambda r: (r.get('box') or [0])[0])]


def parse_lines(lines, subject_hints=()):
    hints = (*SUBJECT_HINTS, *subject_hints)
    merged = []
    for line in _ordered_fragments(lines):
        line = dict(line)
        box = line.get('box')
        previous = merged[-1] if merged else None
        if box and previous and previous.get('box') and previous['page'] == line['page']:
            other = previous['box']
            # Stop after a complete pair: the next fragment can be another table column.
            if (not GRADE_END.match(previous['text'].strip()) and
                    _same_band(box, other) and box[0] >= other[2] - 2):
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
        else:
            if len(text) >= 160:
                continue
            normalized_text = normalized(text)
            if not any(hint in normalized_text for hint in hints):
                continue
            subject, grade = text, None
        subject = re.sub(r'^\s*\d+[.)]\s*', '', subject).strip(' .:—-')
        if len(subject) < 2:
            continue
        # A row containing more than one numeric pair needs explicit table segmentation.
        if re.search(r'\s[2-5](?:\s|\()', subject):
            grade, conflict = None, True
        result.append({'subject': subject, 'grade': grade, 'include': True, 'reason': '', 'verified': False,
                       'confidence': float(line.get('confidence', 0)), 'page': line['page'],
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


def reconcile(observations, threshold=OCR_THRESHOLD, parser=None):
    """Accept a high-confidence grade unless another high-confidence OCR contradicts it."""
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
        winner = counts.most_common(1)[0][0] if counts else None
        high_conflict = len(counts) > 1
        accepted = bool(counts) and not high_conflict and not bucket['duplicate']
        winning_votes = {name: v for name, v in bucket['votes'].items()
                         if accepted and (normalized(v['subject']), v['grade']) == winner
                         and v['confidence'] >= threshold and not v['conflict']}
        numeric_votes = [v for v in bucket['votes'].values()
                         if type(v['grade']) is int and not v['conflict']]
        grade_counts = Counter(v['grade'] for v in numeric_votes)
        grade_confidence = {grade: sum(v['confidence'] for v in numeric_votes if v['grade'] == grade)
                            for grade in grade_counts}
        suggested = max(numeric_votes, key=lambda v: (grade_counts[v['grade']],
                         grade_confidence[v['grade']], v['confidence'])) if numeric_votes else None
        chosen = (max(winning_votes.values(), key=lambda v: v['confidence']) if winning_votes else
                  suggested or bucket['anchor'])
        row = dict(chosen)
        row['auto_verified'] = accepted
        row['verified'] = False
        row['verification'] = 'ocr_high_confidence' if accepted else 'manual_required'
        row['consensus_status'] = ('agreed' if len(winning_votes) > 1 else 'single_high_confidence') if accepted else 'unresolved'
        row['votes'] = bucket['votes']
        row['supporting_engines'] = list(winning_votes)
        row['issue'] = '' if accepted else ('Повтор предмета в результате OCR.' if bucket['duplicate'] else
                                           'Уверенные OCR дали разные оценки.' if high_conflict else
                                           'Оценка не прочитана.' if not numeric_votes else
                                           'Ни один OCR не достиг порога уверенности.')
        row['conflict'] = high_conflict or (not accepted and suggested is None)
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
