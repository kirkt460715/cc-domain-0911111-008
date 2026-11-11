from tests.support import Case, record
from corpus_gate.review import desk
from corpus_gate import documents
from corpus_gate.errors import GateError


class AdjudicationCase(Case):
    def setUp(self):
        super().setUp()
        self.ingest([
            record('d1', 'alpha beta gamma delta epsilon'),
            record('d2', 'one two three four five six seven'),
            record('d3', 'zeta eta theta iota kappa'),
        ])
        desk.register(self.db, 'ann1', 'reviewer')
        desk.register(self.db, 'ann2', 'reviewer')
        desk.register(self.db, 'boss', 'supervisor')
        self.ids = [documents.head(self.db, 'source', name)['document_id'] for name in ('d1', 'd2', 'd3')]

    def create_round(self, round_id='r1', kind='both', vocabulary=None):
        return self.app.rounds.create(round_id, kind, self.ids, vocabulary or ['term', 'entity'], ['ann1', 'ann2'])


class RoundLifecycleTests(AdjudicationCase):
    def test_round_freezes_documents_vocabulary_and_participants(self):
        round_row = self.create_round()
        self.assertEqual(round_row['state'], 'open')
        self.assertEqual(len(round_row['documents']), 3)
        self.assertEqual(round_row['annotators'], ['ann1', 'ann2'])
        # Frozen vocabulary rejects an unknown label.
        self.fails('adjudication_label', self.app.rounds.submit, 'r1', self.ids[0], 'ann1',
                   {'label': None, 'spans': [{'start': 0, 'end': 5, 'label': 'unknown'}]})
        # A non-participant cannot submit.
        self.fails('adjudication_participant', self.app.rounds.submit, 'r1', self.ids[0], 'boss',
                   {'label': 'term', 'spans': []})

    def test_requires_two_distinct_annotators(self):
        self.fails('adjudication_annotators', self.app.rounds.create, 'bad', 'document', self.ids,
                   ['term'], ['ann1', 'ann1'])

    def test_blind_phase_blocks_cross_annotator_reads(self):
        self.create_round()
        self.app.rounds.submit('r1', self.ids[0], 'ann1', {'label': 'term', 'spans': []})
        # ann2 cannot read ann1's revisions while open.
        self.fails('adjudication_blind', self.app.rounds.submission_history, 'r1', self.ids[0], 'ann1', 'ann2')
        # Own history remains visible.
        own = self.app.rounds.submission_history('r1', self.ids[0], 'ann1', 'ann1')
        self.assertEqual(len(own['revisions']), 1)
        self.app.rounds.submit('r1', self.ids[0], 'ann2', {'label': 'term', 'spans': []})
        self.app.rounds.close('r1')
        # After close the blind wall drops.
        cross = self.app.rounds.submission_history('r1', self.ids[0], 'ann1', 'ann2')
        self.assertEqual(len(cross['revisions']), 1)

    def test_revisions_keep_history_and_last_valid_wins(self):
        self.create_round(kind='span')
        self.app.rounds.submit('r1', self.ids[0], 'ann1',
                               {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]})
        self.app.rounds.submit('r1', self.ids[0], 'ann1',
                               {'spans': [{'start': 0, 'end': 10, 'label': 'term'}]})
        history = self.app.rounds.submission_history('r1', self.ids[0], 'ann1', 'ann1')
        self.assertEqual([row['revision'] for row in history['revisions']], [1, 2])
        effective = self.app.rounds.effective('r1', self.ids[0], 'ann1')
        self.assertEqual(effective['spans'][0]['end'], 10)
        # A retraction appends a row and removes the effective submission, but history survives.
        self.app.rounds.retract('r1', self.ids[0], 'ann1', 'mistake')
        self.assertIsNone(self.app.rounds.effective('r1', self.ids[0], 'ann1'))
        history = self.app.rounds.submission_history('r1', self.ids[0], 'ann1', 'ann1')
        self.assertEqual([row['state'] for row in history['revisions']], ['valid', 'valid', 'retracted'])


class SubmissionStatusTests(AdjudicationCase):
    def test_three_statuses_are_counted_separately(self):
        self.create_round(kind='span')
        # d1: ann1 explicit empty, ann2 real spans.
        self.app.rounds.submit('r1', self.ids[0], 'ann1', {'spans': []})
        self.app.rounds.submit('r1', self.ids[0], 'ann2',
                               {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]})
        # d2: both submit.
        self.app.rounds.submit('r1', self.ids[1], 'ann1', {'spans': [{'start': 0, 'end': 3, 'label': 'term'}]})
        self.app.rounds.submit('r1', self.ids[1], 'ann2', {'spans': [{'start': 0, 'end': 3, 'label': 'term'}]})
        # d3 marked unavailable.
        self.app.rounds.mark_unavailable('r1', self.ids[2], 'source corrupted')
        progress = self.app.rounds.progress('r1')
        totals = progress['totals']
        # d1: ann1 explicit empty; d2: both real submissions; d3 unavailable.
        self.assertEqual(totals['ann1'], {'submitted': 1, 'explicit_empty': 1, 'not_submitted': 0})
        self.assertEqual(totals['ann2'], {'submitted': 2, 'explicit_empty': 0, 'not_submitted': 0})
        statuses = {row['document_id']: row['status'] for row in progress['per_document']}
        self.assertEqual(statuses[self.ids[2]], 'unavailable')
        self.assertEqual(statuses[self.ids[1]], 'both_submitted')

    def test_report_excludes_missing_and_unavailable_but_lists_them(self):
        self.create_round(kind='span')
        self.app.rounds.submit('r1', self.ids[0], 'ann1', {'spans': []})
        self.app.rounds.submit('r1', self.ids[0], 'ann2', {'spans': []})
        self.app.rounds.mark_unavailable('r1', self.ids[2], 'broken')
        # d1 evaluable; d2 both missing; d3 unavailable
        report = self.app.rounds.close('r1')
        self.assertEqual(report['evaluable_documents'], 1)
        kinds = {item['status'] for item in report['excluded']}
        self.assertEqual(kinds, {'not_submitted', 'unavailable'})

    def test_closed_round_rejects_further_submission(self):
        self.create_round()
        self.app.rounds.close('r1')
        self.fails('adjudication_state', self.app.rounds.submit, 'r1', self.ids[0], 'ann1',
                   {'label': 'term', 'spans': []})


class AgreementReportTests(AdjudicationCase):
    def _fill_document_round(self):
        self.create_round(kind='document')
        labels = [('term', 'term'), ('term', 'entity'), ('entity', 'entity')]
        for document_id, (left, right) in zip(self.ids, labels):
            self.app.rounds.submit('r1', document_id, 'ann1', {'label': left})
            self.app.rounds.submit('r1', document_id, 'ann2', {'label': right})

    def test_confusion_matrix_raw_agreement_and_kappa(self):
        self._fill_document_round()
        report = self.app.rounds.close('r1')
        classification = report['document_classification']
        self.assertEqual(classification['evaluated'], 3)
        self.assertEqual(classification['confusion']['term']['term'], 1)
        self.assertEqual(classification['confusion']['term']['entity'], 1)
        self.assertEqual(classification['confusion']['entity']['entity'], 1)
        self.assertEqual(classification['raw_agreement']['numerator'], 2)
        self.assertEqual(classification['raw_agreement']['denominator'], 3)
        self.assertTrue(classification['kappa']['defined'])

    def test_kappa_undefined_is_not_fabricated(self):
        self.create_round(kind='document')
        for document_id in self.ids:
            self.app.rounds.submit('r1', document_id, 'ann1', {'label': 'term'})
            self.app.rounds.submit('r1', document_id, 'ann2', {'label': 'term'})
        report = self.app.rounds.close('r1')
        kappa = report['document_classification']['kappa']
        self.assertFalse(kappa['defined'])
        self.assertEqual(kappa['reason'], 'zero_expected_disagreement')

    def test_span_matching_distinguishes_boundary_and_label_disputes(self):
        self.create_round(kind='span')
        # d1 exact agreement
        self.app.rounds.submit('r1', self.ids[0], 'ann1', {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]})
        self.app.rounds.submit('r1', self.ids[0], 'ann2', {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]})
        # d2 boundary disagreement, same label
        self.app.rounds.submit('r1', self.ids[1], 'ann1', {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]})
        self.app.rounds.submit('r1', self.ids[1], 'ann2', {'spans': [{'start': 1, 'end': 6, 'label': 'term'}]})
        # d3 label disagreement on identical span
        self.app.rounds.submit('r1', self.ids[2], 'ann1', {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]})
        self.app.rounds.submit('r1', self.ids[2], 'ann2', {'spans': [{'start': 0, 'end': 5, 'label': 'entity'}]})
        report = self.app.rounds.close('r1')
        summary = report['dispute_summary']
        self.assertEqual(summary['documents_exact'], 1)
        self.assertEqual(summary['documents_with_boundary_differences'], 1)
        self.assertEqual(summary['documents_with_label_differences'], 1)
        disputes = self.app.rounds.disputes('r1')
        # d1 has no disagreement and must be absent from the dispute list.
        disputed = {item['document_id'] for item in disputes['disputes']}
        self.assertNotIn(self.ids[0], disputed)
        self.assertIn(self.ids[1], disputed)
        self.assertIn(self.ids[2], disputed)
        d2 = next(item for item in disputes['disputes'] if item['document_id'] == self.ids[1])
        pair = d2['span_matching']['term']['pairs'][0]
        self.assertFalse(pair['exact_span'])
        # Each dispute references both submissions and their revisions.
        self.assertEqual(set(d2['submissions']), {'ann1', 'ann2'})
        self.assertTrue(all(d2['submissions'][name]['revision'] == 1 for name in ('ann1', 'ann2')))

    def test_adjacent_spans_are_not_matched(self):
        self.create_round(kind='span', vocabulary=['term'])
        self.app.rounds.submit('r1', self.ids[0], 'ann1', {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]})
        self.app.rounds.submit('r1', self.ids[0], 'ann2', {'spans': [{'start': 5, 'end': 10, 'label': 'term'}]})
        report = self.app.rounds.close('r1')
        term = next(row for row in report['span_matching']['labels'] if row['label'] == 'term')
        self.assertEqual(term['matched'], 0)
        self.assertEqual(term['unmatched_a'], 1)
        self.assertEqual(term['unmatched_b'], 1)

    def test_mutual_explicit_empty_is_exact_agreement_and_not_a_dispute(self):
        self.create_round(kind='span', vocabulary=['term'])
        # d1 both explicitly empty; d2 only one side empty (the other has a span).
        self.app.rounds.submit('r1', self.ids[0], 'ann1', {'spans': []})
        self.app.rounds.submit('r1', self.ids[0], 'ann2', {'spans': []})
        self.app.rounds.submit('r1', self.ids[1], 'ann1', {'spans': []})
        self.app.rounds.submit('r1', self.ids[1], 'ann2', {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]})
        self.app.rounds.submit('r1', self.ids[2], 'ann1', {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]})
        self.app.rounds.submit('r1', self.ids[2], 'ann2', {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]})
        report = self.app.rounds.close('r1')
        self.assertEqual(report['dispute_summary']['documents_exact'], 2)  # d1 mutual empty + d3 exact
        disputes = self.app.rounds.disputes('r1')
        disputed = {item['document_id'] for item in disputes['disputes']}
        self.assertNotIn(self.ids[0], disputed)
        self.assertIn(self.ids[1], disputed)


class ArbitrationTests(AdjudicationCase):
    def test_arbitration_is_separate_and_keeps_history(self):
        self.create_round(kind='span')
        self.app.rounds.submit('r1', self.ids[0], 'ann1', {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]})
        self.app.rounds.submit('r1', self.ids[0], 'ann2', {'spans': [{'start': 1, 'end': 6, 'label': 'term'}]})
        self.app.rounds.close('r1')
        original_a = self.app.rounds.effective('r1', self.ids[0], 'ann1')
        self.app.arbitrations.decide('r1', self.ids[0], 'boss',
                                     {'spans': [{'start': 0, 'end': 6, 'label': 'term'}]}, 'merged boundary')
        # Re-arbitration appends a second revision.
        self.app.arbitrations.decide('r1', self.ids[0], 'boss',
                                     {'spans': [{'start': 0, 'end': 5, 'label': 'term'}]}, 'final call')
        record = self.app.arbitrations.get('r1', self.ids[0])
        self.assertEqual(record['latest_revision'], 2)
        self.assertEqual(len(record['history']), 2)
        # Original annotations are untouched.
        self.assertEqual(self.app.rounds.effective('r1', self.ids[0], 'ann1'), original_a)

    def test_annotator_cannot_arbitrate(self):
        self.create_round(kind='document')
        self.app.rounds.submit('r1', self.ids[0], 'ann1', {'label': 'term'})
        self.app.rounds.submit('r1', self.ids[0], 'ann2', {'label': 'entity'})
        self.app.rounds.close('r1')
        self.fails('adjudication_independence', self.app.arbitrations.decide, 'r1', self.ids[0], 'ann1',
                   {'label': 'term'}, 'self review')

    def test_export_round_trips_back_to_the_round(self):
        self.create_round(kind='both')
        for document_id in self.ids:
            self.app.rounds.submit('r1', document_id, 'ann1', {'label': 'term', 'spans': []})
            self.app.rounds.submit('r1', document_id, 'ann2', {'label': 'term', 'spans': []})
        self.app.rounds.close('r1')
        envelope = self.app.arbitrations.export('r1')
        self.assertEqual(envelope['round_id'], 'r1')
        self.assertEqual(envelope['round_digest'], self.app.rounds.get('r1')['digest'])
        self.assertEqual(len(envelope['documents']), 3)
        self.assertTrue(envelope['envelope_digest'])
        # Frozen definition is embedded so results point back to the exact round.
        self.assertEqual(envelope['frozen_definition']['documents'][0]['document_id'], self.ids[0])
        self.assertTrue(self.db.audit_check()['ok'])


class UnicodeOffsetTests(AdjudicationCase):
    def test_unicode_code_point_offsets(self):
        self.ingest([record('u', 'café naïve 日本語 text')], batch_id='unicode-batch')
        unicode_id = documents.head(self.db, 'source', 'u')['document_id']
        round_row = self.app.rounds.create('ru', 'span', [unicode_id], ['term'], ['ann1', 'ann2'])
        text = documents.get(self.db, unicode_id)['normalized']
        # Offsets index Python code points; 'café' occupies 0..4.
        self.assertEqual(text[0:4], 'café')
        self.app.rounds.submit('ru', unicode_id, 'ann1', {'spans': [{'start': 0, 'end': 4, 'label': 'term'}]})
        self.app.rounds.submit('ru', unicode_id, 'ann2', {'spans': [{'start': 0, 'end': 4, 'label': 'term'}]})
        report = self.app.rounds.close('ru')
        self.assertEqual(report['span_matching']['micro']['exact_span_matches'], 1)
        self.assertEqual(round_row['documents'][0]['text_hash'], documents.get(self.db, unicode_id)['exact_hash'])

    def test_out_of_range_and_empty_spans_rejected(self):
        self.create_round(kind='span')
        length = len(documents.get(self.db, self.ids[0])['normalized'])
        self.fails('integer', self.app.rounds.submit, 'r1', self.ids[0], 'ann1',
                   {'spans': [{'start': 0, 'end': length + 1, 'label': 'term'}]})
        self.fails('adjudication_span', self.app.rounds.submit, 'r1', self.ids[0], 'ann1',
                   {'spans': [{'start': 3, 'end': 3, 'label': 'term'}]})


if __name__ == '__main__':
    import unittest
    unittest.main()
