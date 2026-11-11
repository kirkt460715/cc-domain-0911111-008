import unittest
from fractions import Fraction
from corpus_gate.adjudication import agreement


def span(span_id, start, end, label='term'):
    return {'span_id': span_id, 'start': start, 'end': end, 'label': label}


class OverlapTests(unittest.TestCase):
    def test_adjacent_spans_do_not_overlap(self):
        self.assertEqual(agreement.overlap_length(span('a', 0, 5), span('b', 5, 10)), 0)
        self.assertEqual(agreement.overlap_length(span('a', 0, 5), span('b', 3, 8)), 2)

    def test_nested_spans_match_and_are_one_to_one(self):
        a = [span('a1', 0, 10)]
        b = [span('b1', 2, 6), span('b2', 4, 8)]
        result = agreement.match_label(a, b)
        self.assertEqual(result['matched'], 1)
        # a1 overlaps b2 (4) more than b1 (4 as well: 2-6=4, 4-8=4). Tie on
        # overlap, so b.start decides: b1 starts at 2 before b2 at 4.
        self.assertEqual(result['pairs'][0]['span_b'], 'b1')
        self.assertEqual(result['unmatched_b'], ['b2'])

    def test_one_span_never_matches_two_candidates(self):
        a = [span('a1', 0, 10)]
        b = [span('b1', 0, 6), span('b2', 4, 10)]
        result = agreement.match_label(a, b)
        self.assertEqual(result['matched'], 1)
        self.assertEqual(len(result['pairs']), 1)
        self.assertEqual(result['unmatched_b'], ['b2'])

    def test_greedy_prefers_largest_overlap_before_position(self):
        a = [span('a1', 0, 10), span('a2', 9, 12)]
        b = [span('b1', 0, 10), span('b2', 9, 12)]
        result = agreement.match_label(a, b)
        # Two exact pairs each with overlap >= 3; greedy locks exact matches.
        self.assertEqual({(p['span_a'], p['span_b']) for p in result['pairs']},
                         {('a1', 'b1'), ('a2', 'b2')})
        self.assertEqual(result['unmatched_a'], [])
        self.assertEqual(result['unmatched_b'], [])

    def test_tie_break_is_total_and_deterministic(self):
        a = [span('a1', 0, 5), span('a2', 0, 5)]
        b = [span('b1', 0, 5), span('b2', 0, 5)]
        first = agreement.match_label(a, b)
        second = agreement.match_label(list(reversed(a)), list(reversed(b)))
        self.assertEqual(first['pairs'], second['pairs'])
        self.assertEqual(first['matched'], 2)
        # Deterministic identity order wins identical-overlap ties.
        self.assertEqual([(p['span_a'], p['span_b']) for p in first['pairs']],
                         [('a1', 'b1'), ('a2', 'b2')])


class KappaTests(unittest.TestCase):
    def doc(self, left, right):
        return ({'label': left, 'spans': []}, {'label': right, 'spans': []})

    def test_perfect_agreement_kappa_one(self):
        # With two labels actually used, perfect agreement gives kappa = 1.
        docs = {'1': self.doc('x', 'x'), '2': self.doc('x', 'x'),
                '3': self.doc('y', 'y'), '4': self.doc('y', 'y')}
        result = agreement.confusion_and_kappa(['x', 'y'], docs)
        self.assertTrue(result['defined'])
        self.assertEqual(result['raw_agreement']['numerator'], 1)
        self.assertEqual(result['kappa']['decimal'], 1.0)
        self.assertEqual(result['confusion']['x']['x'], 2)
        self.assertEqual(result['confusion']['y']['y'], 2)

    def test_single_label_perfect_agreement_kappa_undefined(self):
        # Both annotators always pick the only label: no expected disagreement,
        # so kappa is 0/0 and must be reported undefined rather than fabricated.
        docs = {str(i): self.doc('x', 'x') for i in range(3)}
        result = agreement.confusion_and_kappa(['x'], docs)
        self.assertFalse(result['kappa']['defined'])
        self.assertEqual(result['kappa']['reason'], 'zero_expected_disagreement')
        self.assertEqual(result['raw_agreement']['decimal'], 1.0)

    def test_constant_annotator_makes_kappa_undefined(self):
        docs = {str(i): self.doc('x', 'x') for i in range(2)}
        result = agreement.confusion_and_kappa(['x', 'y'], docs)
        self.assertFalse(result['kappa']['defined'])
        self.assertEqual(result['kappa']['reason'], 'zero_expected_disagreement')
        # Raw agreement is still a concrete number; we only refuse to invent kappa.
        self.assertTrue(result['raw_agreement']['defined'])

    def test_known_kappa_value(self):
        # Classic worked example: 50/100 agree, marginal split 0.5/0.5 each.
        docs = {}
        n = 0
        for _ in range(25):
            docs[n] = self.doc('x', 'x'); n += 1
        for _ in range(25):
            docs[n] = self.doc('x', 'y'); n += 1
        for _ in range(25):
            docs[n] = self.doc('y', 'x'); n += 1
        for _ in range(25):
            docs[n] = self.doc('y', 'y'); n += 1
        result = agreement.confusion_and_kappa(['x', 'y'], docs)
        self.assertEqual(result['raw_agreement']['numerator'], 1)
        self.assertEqual(result['raw_agreement']['denominator'], 2)
        self.assertEqual(Fraction(result['kappa']['numerator'], result['kappa']['denominator']), Fraction(0))

    def test_empty_evaluation_is_undefined_not_zero(self):
        result = agreement.confusion_and_kappa(['x'], {})
        self.assertFalse(result['defined'])
        self.assertEqual(result['reason'], 'no_evaluable_documents')
        self.assertNotIn('kappa', result)

    def test_none_sentinel_does_not_collide_with_labels(self):
        docs = {'1': self.doc(None, None)}
        result = agreement.confusion_and_kappa(['x'], docs)
        self.assertIn(agreement.NONE_LABEL, result['categories'])
        self.assertEqual(result['confusion'][agreement.NONE_LABEL][agreement.NONE_LABEL], 1)


class SpanAgreementTests(unittest.TestCase):
    def test_per_label_and_micro_counts(self):
        effective = {
            'd1': ({'label': None, 'spans': [span('a', 0, 5, 'term')]},
                   {'label': None, 'spans': [span('b', 0, 5, 'term')]}),
            'd2': ({'label': None, 'spans': [span('c', 0, 5, 'entity')]},
                   {'label': None, 'spans': []}),
        }
        result = agreement.span_agreement(['term', 'entity', 'unused'], effective)
        labels = {row['label']: row for row in result['labels']}
        self.assertNotIn('unused', labels)
        self.assertEqual(labels['entity']['unmatched_a'], 1)
        self.assertEqual(labels['entity']['unmatched_b'], 0)
        self.assertEqual(labels['term']['exact_span_matches'], 1)
        self.assertEqual(result['micro']['matched'], 1)
        self.assertTrue(result['micro']['matching_rule'])


if __name__ == '__main__':
    unittest.main()
