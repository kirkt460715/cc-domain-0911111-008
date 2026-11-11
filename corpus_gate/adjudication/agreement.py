'''Pure agreement math for frozen adjudication rounds.

No database access lives here so the matching and kappa semantics can be tested
in isolation and stay deterministic. Offsets are always Unicode code point
offsets into the frozen normalized document text.
'''
from fractions import Fraction
from corpus_gate.errors import fail

# Document classification category used when a document carries no label.
# Labels are identifiers that must start with a letter, so this sentinel can
# never collide with a real vocabulary label.
NONE_LABEL = '__none__'

# Human readable, frozen contract for interval matching.
MATCHING_RULE = (
    'one-to-one greedy overlap matching per label; candidates require positive '
    'intersection (adjacent spans sharing only a boundary do not overlap); '
    'candidates are ordered by overlap length descending, then '
    '(a.start,a.end,b.start,b.end,a.span_id,b.span_id) ascending; each span is '
    'consumed by the first still available candidate, so one span can never '
    'match more than one counterpart and every tie has a deterministic winner'
)


def overlap_length(left, right):
    '''Positive intersection length in code points; adjacent spans return 0.'''
    return max(0, min(left['end'], right['end']) - max(left['start'], right['start']))


def match_label(spans_a, spans_b):
    '''Deterministic one-to-one matching for spans sharing one label.

    Nested and duplicated spans are legal on either side. The greedy order is
    total, so the result does not depend on dict ordering or insertion order.
    '''
    candidates = []
    for span_a in spans_a:
        for span_b in spans_b:
            overlap = overlap_length(span_a, span_b)
            if overlap <= 0:
                continue
            candidates.append((
                -overlap,
                span_a['start'], span_a['end'],
                span_b['start'], span_b['end'],
                span_a['span_id'], span_b['span_id'],
                overlap, span_a, span_b,
            ))
    candidates.sort()
    used_a, used_b = set(), set()
    pairs = []
    for entry in candidates:
        overlap, span_a, span_b = entry[7], entry[8], entry[9]
        if span_a['span_id'] in used_a or span_b['span_id'] in used_b:
            continue
        used_a.add(span_a['span_id'])
        used_b.add(span_b['span_id'])
        union = (span_a['end'] - span_a['start']) + (span_b['end'] - span_b['start']) - overlap
        pairs.append({
            'span_a': span_a['span_id'],
            'span_b': span_b['span_id'],
            'start_a': span_a['start'],
            'end_a': span_a['end'],
            'start_b': span_b['start'],
            'end_b': span_b['end'],
            'overlap': overlap,
            'union_length': union,
            'exact_span': (span_a['start'], span_a['end']) == (span_b['start'], span_b['end']),
        })
    unmatched_a = [span['span_id'] for span in spans_a if span['span_id'] not in used_a]
    unmatched_b = [span['span_id'] for span in spans_b if span['span_id'] not in used_b]
    return {
        'pairs': sorted(pairs, key=lambda item: (item['start_a'], item['span_a'], item['start_b'], item['span_b'])),
        'unmatched_a': sorted(unmatched_a),
        'unmatched_b': sorted(unmatched_b),
        'count_a': len(spans_a),
        'count_b': len(spans_b),
        'matched': len(pairs),
    }


def ratio(numerator, denominator):
    if denominator == 0:
        return {'defined': False, 'numerator': 0, 'denominator': 0}
    value = Fraction(numerator, denominator)
    return {'defined': True, 'numerator': value.numerator, 'denominator': value.denominator,
            'decimal': round(float(value), 6)}


def f1(matched, count_a, count_b):
    precision = Fraction(matched, count_a) if count_a else None
    recall = Fraction(matched, count_b) if count_b else None
    if precision is None or recall is None or precision + recall == 0:
        return {'defined': False, 'numerator': 0, 'denominator': 0}
    value = 2 * precision * recall / (precision + recall)
    return {'defined': True, 'numerator': value.numerator, 'denominator': value.denominator,
            'decimal': round(float(value), 6)}


def span_agreement(vocabulary, effective_by_document):
    '''Per-label one-to-one overlap results across all evaluable documents.

    effective_by_document maps document_id to (submission_a, submission_b)
    where both sides have a valid submission for the document.
    '''
    by_label = {label: {'spans_a': [], 'spans_b': [], 'matched': 0, 'exact': 0,
                        'boundary_only': 0, 'count_a': 0, 'count_b': 0}
                for label in vocabulary}
    for submission_a, submission_b in effective_by_document.values():
        grouped_a, grouped_b = {}, {}
        for span in submission_a['spans']:
            grouped_a.setdefault(span['label'], []).append(span)
        for span in submission_b['spans']:
            grouped_b.setdefault(span['label'], []).append(span)
        for label in vocabulary:
            result = match_label(grouped_a.get(label, []), grouped_b.get(label, []))
            bucket = by_label[label]
            bucket['spans_a'].extend(grouped_a.get(label, []))
            bucket['spans_b'].extend(grouped_b.get(label, []))
            bucket['count_a'] += result['count_a']
            bucket['count_b'] += result['count_b']
            bucket['matched'] += result['matched']
            bucket['exact'] += sum(1 for pair in result['pairs'] if pair['exact_span'])
            bucket['boundary_only'] += sum(1 for pair in result['pairs'] if not pair['exact_span'])
    labels = []
    totals = {'count_a': 0, 'count_b': 0, 'matched': 0, 'exact': 0, 'boundary_only': 0}
    for label in sorted(by_label):
        bucket = by_label[label]
        if bucket['count_a'] == 0 and bucket['count_b'] == 0:
            continue
        totals.update({key: totals[key] + bucket[key] for key in totals})
        # Annotator A is treated as the candidate side, B as the reference side.
        labels.append({
            'label': label,
            'count_a': bucket['count_a'],
            'count_b': bucket['count_b'],
            'matched': bucket['matched'],
            'exact_span_matches': bucket['exact'],
            'boundary_only_matches': bucket['boundary_only'],
            'unmatched_a': bucket['count_a'] - bucket['matched'],
            'unmatched_b': bucket['count_b'] - bucket['matched'],
            'precision': ratio(bucket['matched'], bucket['count_a']),
            'recall': ratio(bucket['matched'], bucket['count_b']),
            'f1': f1(bucket['matched'], bucket['count_a'], bucket['count_b']),
        })
    micro = {
        'labels_scored': len(labels),
        'count_a': totals['count_a'],
        'count_b': totals['count_b'],
        'matched': totals['matched'],
        'exact_span_matches': totals['exact'],
        'boundary_only_matches': totals['boundary_only'],
        'precision': ratio(totals['matched'], totals['count_a']),
        'recall': ratio(totals['matched'], totals['count_b']),
        'f1': f1(totals['matched'], totals['count_a'], totals['count_b']),
        'matching_rule': MATCHING_RULE,
    }
    return {'labels': labels, 'micro': micro}


def groupled_safe(grouped, label):
    return grouped.get(label, [])


def confusion_and_kappa(vocabulary, effective_by_document):
    '''Confusion matrix, raw agreement and Cohen kappa for document labels.'''
    categories = sorted(vocabulary) + [NONE_LABEL]
    matrix = {left: {right: 0 for right in categories} for left in categories}
    row_totals = {label: 0 for label in categories}
    col_totals = {label: 0 for label in categories}
    agreements = 0
    evaluated = 0
    for submission_a, submission_b in effective_by_document.values():
        left = submission_a['label'] or NONE_LABEL
        right = submission_b['label'] or NONE_LABEL
        if left not in matrix or right not in matrix:
            fail('adjudication_label', 'Submission label is outside frozen vocabulary')
        matrix[left][right] += 1
        row_totals[left] += 1
        col_totals[right] += 1
        evaluated += 1
        if left == right:
            agreements += 1
    if evaluated == 0:
        return {
            'defined': False,
            'reason': 'no_evaluable_documents',
            'evaluated': 0,
            'categories': categories,
            'confusion': matrix,
        }
    observed = Fraction(agreements, evaluated)
    expected = Fraction(
        sum(row_totals[label] * col_totals[label] for label in categories),
        evaluated * evaluated,
    )
    result = {
        'defined': True,
        'evaluated': evaluated,
        'categories': categories,
        'confusion': matrix,
        'raw_agreement': ratio(agreements, evaluated),
        'expected_agreement': ratio(expected.numerator, expected.denominator),
    }
    if expected == 1:
        # A constant annotator makes kappa mathematically undefined; reporting
        # any number here would be fabrication rather than measurement.
        result['kappa'] = {'defined': False, 'reason': 'zero_expected_disagreement'}
    else:
        kappa = (observed - expected) / (1 - expected)
        result['kappa'] = {
            'defined': True,
            'numerator': kappa.numerator,
            'denominator': kappa.denominator,
            'decimal': round(float(kappa), 6),
        }
    return result
