'''Frozen double-annotation rounds, kept separate from ordinary annotations.

A round freezes document versions (with text hashes), the label vocabulary and
the participant set at creation time. While open, annotators cannot read each
other's submissions. Every save appends an immutable revision row; the last
valid revision is the effective submission and the full history stays readable.
'''
import json
from corpus_gate.errors import identifier, integer, canonical, digest, shape, text, fail, symbols
from corpus_gate import documents
from corpus_gate.review import desk
from corpus_gate.adjudication import agreement

DDL = '''
CREATE TABLE IF NOT EXISTS adjudication_rounds(round_id TEXT PRIMARY KEY,task_kind TEXT NOT NULL,vocabulary TEXT NOT NULL,annotators TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,closed_at TEXT,definition TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS adjudication_documents(round_id TEXT NOT NULL,ordinal INTEGER NOT NULL,document_id TEXT NOT NULL,document_version INTEGER NOT NULL,text_hash TEXT NOT NULL,unavailable INTEGER NOT NULL,unavailable_reason TEXT,PRIMARY KEY(round_id,document_id));
CREATE TABLE IF NOT EXISTS adjudication_submissions(round_id TEXT NOT NULL,document_id TEXT NOT NULL,annotator TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,label TEXT,payload TEXT NOT NULL,submitted_at TEXT NOT NULL,PRIMARY KEY(round_id,document_id,annotator,revision));
CREATE TABLE IF NOT EXISTS adjudication_arbitrations(round_id TEXT NOT NULL,document_id TEXT NOT NULL,revision INTEGER NOT NULL,arbitrator TEXT NOT NULL,label TEXT,payload TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(round_id,document_id,revision));
'''


class Rounds:
    def __init__(self, db):
        self.db = db
        if db.depth:
            fail('adjudication_transaction', 'Initialize adjudication store outside transaction')
        db.db.executescript(DDL)

    # ---------- internal helpers ----------
    def row(self, round_id):
        row = self.db.one('SELECT * FROM adjudication_rounds WHERE round_id=?', (round_id,))
        if row is None:
            fail('adjudication_missing', 'Adjudication round does not exist')
        for key in ('vocabulary', 'annotators', 'definition'):
            row[key] = json.loads(row[key])
        return row

    def require_state(self, round_id, *states):
        row = self.row(round_id)
        if row['state'] not in states:
            fail('adjudication_state', 'Round is not in the required state', round=row['state'], required=sorted(states))
        return row

    def participant(self, row, annotator):
        identifier(annotator)
        if annotator not in row['annotators']:
            fail('adjudication_participant', 'Annotator is not a frozen participant of this round')
        desk.actor(self.db, annotator)
        return annotator

    def member(self, row, document_id):
        row_doc = self.db.one('SELECT * FROM adjudication_documents WHERE round_id=? AND document_id=?', (row['round_id'], document_id))
        if row_doc is None:
            fail('adjudication_document', 'Document is not part of this frozen round')
        return row_doc

    def last_revision(self, round_id, document_id, annotator):
        return self.db.scalar(
            'SELECT MAX(revision) FROM adjudication_submissions WHERE round_id=? AND document_id=? AND annotator=?',
            (round_id, document_id, annotator)) or 0

    def effective_row(self, round_id, document_id, annotator):
        # The effective submission is the highest revision overall. A retraction
        # is itself a revision (a tombstone), so when the latest row is retracted
        # there is no effective submission even though older valid rows remain
        # in the history.
        revision = self.db.scalar(
            'SELECT MAX(revision) FROM adjudication_submissions WHERE round_id=? AND document_id=? AND annotator=?',
            (round_id, document_id, annotator))
        if revision is None:
            return None
        row = self.db.one('SELECT * FROM adjudication_submissions WHERE round_id=? AND document_id=? AND annotator=? AND revision=?',
                          (round_id, document_id, annotator, revision))
        return row if row['state'] == 'valid' else None

    def load_payload(self, row_submission):
        payload = json.loads(row_submission['payload'])
        payload['label'] = row_submission['label']
        return payload

    def effective(self, round_id, document_id, annotator):
        row_submission = self.effective_row(round_id, document_id, annotator)
        return self.load_payload(row_submission) if row_submission else None

    # ---------- round lifecycle ----------
    def create(self, round_id, task_kind, document_ids, vocabulary, annotators):
        identifier(round_id)
        if task_kind not in {'document', 'span', 'both'}:
            fail('adjudication_task', 'task_kind must be document, span or both')
        if not isinstance(document_ids, list) or not 1 <= len(document_ids) <= 10000:
            fail('adjudication_documents', 'A round needs one to ten thousand documents')
        vocabulary = symbols(vocabulary, 'vocabulary')
        if not isinstance(annotators, list) or len(annotators) != 2 or len(set(annotators)) != 2:
            fail('adjudication_annotators', 'A double-annotation round requires exactly two distinct annotators')
        annotators = sorted(identifier(value, 'annotator') for value in annotators)
        for annotator in annotators:
            desk.actor(self.db, annotator)
        with self.db.transaction():
            if self.db.one('SELECT round_id FROM adjudication_rounds WHERE round_id=?', (round_id,)):
                fail('adjudication_exists', 'Adjudication round identifier exists')
            frozen = []
            for ordinal, document_id in enumerate(document_ids):
                document = documents.get(self.db, document_id)
                frozen.append({
                    'ordinal': ordinal,
                    'document_id': document_id,
                    'document_version': document['version'],
                    'source_id': document['source_id'],
                    'external_id': document['external_id'],
                    'text_hash': document['exact_hash'],
                    'length': len(document['normalized']),
                })
            definition = {'task_kind': task_kind, 'vocabulary': vocabulary, 'annotators': annotators,
                          'documents': frozen, 'corpus_revision': self.db.revision()}
            self.db.execute('INSERT INTO adjudication_rounds VALUES(?,?,?,?,?,?,?,?,?)',
                            (round_id, task_kind, canonical(vocabulary), canonical(annotators), 'open',
                             self.db.clock(), None, canonical(definition), digest(definition)))
            for item in frozen:
                self.db.execute('INSERT INTO adjudication_documents VALUES(?,?,?,?,?,0,NULL)',
                                (round_id, item['ordinal'], item['document_id'], item['document_version'], item['text_hash']))
            self.db.changed()
            self.db.log('adjudication.created', {'round_id': round_id, 'task_kind': task_kind,
                                                 'documents': len(frozen), 'labels': len(vocabulary)})
            return self.get(round_id)

    def get(self, round_id):
        row = self.row(round_id)
        docs = self.db.all('SELECT ordinal,document_id,document_version,text_hash,unavailable,unavailable_reason '
                           'FROM adjudication_documents WHERE round_id=? ORDER BY ordinal', (round_id,))
        for item in docs:
            item['unavailable'] = bool(item['unavailable'])
        return {'round_id': round_id, 'task_kind': row['task_kind'], 'state': row['state'],
                'vocabulary': row['vocabulary'], 'annotators': row['annotators'],
                'created_at': row['created_at'], 'closed_at': row['closed_at'],
                'digest': row['digest'], 'documents': docs}

    def list_rounds(self):
        rows = self.db.all('SELECT round_id FROM adjudication_rounds ORDER BY created_at,round_id')
        return [self.get(row['round_id']) for row in rows]

    def mark_unavailable(self, round_id, document_id, reason):
        reason = text(reason, 'reason')
        with self.db.transaction():
            row = self.require_state(round_id, 'open')
            self.member(row, document_id)
            self.db.execute('UPDATE adjudication_documents SET unavailable=1,unavailable_reason=? WHERE round_id=? AND document_id=?',
                            (reason, round_id, document_id))
            self.db.log('adjudication.unavailable', {'round_id': round_id, 'document_id': document_id})
            return {'round_id': round_id, 'document_id': document_id, 'unavailable': True, 'reason': reason}

    # ---------- validation of a submission payload ----------
    def validate_payload(self, row, document_id, payload):
        if not isinstance(payload, dict):
            fail('adjudication_payload', 'Submission must be an object')
        kind = row['task_kind']
        member = self.member(row, document_id)
        document = documents.get(self.db, document_id)
        if document['exact_hash'] != member['text_hash']:
            fail('adjudication_drift', 'Frozen document text no longer matches the round')
        length = len(document['normalized'])
        label = None
        spans = []
        if kind in ('document', 'both'):
            label = payload.get('label')
            if label is not None:
                identifier(label, 'label')
                if label not in row['vocabulary']:
                    fail('adjudication_label', 'Label is outside the frozen vocabulary')
        if kind in ('span', 'both'):
            raw_spans = payload.get('spans', [])
            if not isinstance(raw_spans, list) or len(raw_spans) > 100000:
                fail('adjudication_spans', 'Spans must be a bounded list')
            seen = set()
            normalized = document['normalized']
            for index, span in enumerate(raw_spans):
                shape(span, {'start', 'end', 'label'}, {'span_id'})
                integer(span['start'], 'start', 0, length)
                integer(span['end'], 'end', 1, length)
                if span['start'] >= span['end']:
                    fail('adjudication_span', 'Span must cover at least one Unicode code point')
                if span['label'] not in row['vocabulary']:
                    fail('adjudication_label', 'Span label is outside the frozen vocabulary')
                span_id = span.get('span_id') or f's{index}'
                identifier(span_id, 'span_id')
                if span_id in seen:
                    fail('adjudication_span_id', 'Span identifiers must be unique within a submission')
                seen.add(span_id)
                # Pin the slice to code point offsets in the frozen text.
                digest(normalized[span['start']:span['end']])
                spans.append({'span_id': span_id, 'start': span['start'], 'end': span['end'], 'label': span['label']})
        if kind == 'document' and 'spans' in payload:
            fail('adjudication_task', 'Document-only rounds cannot carry spans')
        if kind == 'span' and 'label' in payload:
            fail('adjudication_task', 'Span-only rounds cannot carry a document label')
        # Explicit empty means the annotator affirmatively declared "nothing
        # here": no document label for a document task, and no spans for a span
        # task. This is distinct from never submitting at all.
        if kind == 'document':
            explicit_empty = label is None
        elif kind == 'span':
            explicit_empty = len(spans) == 0
        else:
            explicit_empty = label is None and len(spans) == 0
        return label, spans, explicit_empty

    # ---------- submissions ----------
    def submit(self, round_id, document_id, annotator, payload):
        with self.db.transaction():
            row = self.require_state(round_id, 'open')
            self.participant(row, annotator)
            member = self.member(row, document_id)
            if member['unavailable']:
                fail('adjudication_unavailable', 'Document is marked unavailable for this round')
            label, spans, explicit_empty = self.validate_payload(row, document_id, payload)
            stored = {'spans': spans, 'explicit_empty': explicit_empty}
            revision = self.last_revision(round_id, document_id, annotator) + 1
            self.db.execute('INSERT INTO adjudication_submissions VALUES(?,?,?,?,?,?,?,?)',
                            (round_id, document_id, annotator, revision, 'valid', label,
                             canonical(stored), self.db.clock()))
            self.db.log('adjudication.submitted', {'round_id': round_id, 'document_id': document_id,
                                                   'annotator': annotator, 'revision': revision})
            return self.submission_history(round_id, document_id, annotator)

    def retract(self, round_id, document_id, annotator, reason):
        reason = text(reason, 'reason')
        with self.db.transaction():
            row = self.require_state(round_id, 'open')
            self.participant(row, annotator)
            self.member(row, document_id)
            if self.effective_row(round_id, document_id, annotator) is None:
                fail('adjudication_submission', 'No effective submission to retract')
            revision = self.last_revision(round_id, document_id, annotator) + 1
            self.db.execute('INSERT INTO adjudication_submissions VALUES(?,?,?,?,?,?,?,?)',
                            (round_id, document_id, annotator, revision, 'retracted', None,
                             canonical({'reason': reason}), self.db.clock()))
            self.db.log('adjudication.retracted', {'round_id': round_id, 'document_id': document_id,
                                                   'annotator': annotator, 'revision': revision})
            return self.submission_history(round_id, document_id, annotator)

    def submission_history(self, round_id, document_id, annotator, requester=None):
        row = self.row(round_id)
        self.participant(row, annotator)
        self.member(row, document_id)
        # Blind phase: while the round is open an annotator can only read their
        # own revisions. Cross-annotator visibility unlocks only after close.
        if requester is not None and row['state'] == 'open' and requester != annotator:
            fail('adjudication_blind', 'Submissions are invisible to the other annotator until the round closes')
        rows = self.db.all('SELECT revision,state,label,payload,submitted_at FROM adjudication_submissions '
                           'WHERE round_id=? AND document_id=? AND annotator=? ORDER BY revision',
                           (round_id, document_id, annotator))
        for item in rows:
            item['payload'] = json.loads(item['payload'])
        return {'round_id': round_id, 'document_id': document_id, 'annotator': annotator,
                'state': row['state'], 'revisions': rows}

    def progress(self, round_id):
        row = self.row(round_id)
        annotator_a, annotator_b = row['annotators']
        docs = self.db.all('SELECT document_id,unavailable,unavailable_reason FROM adjudication_documents WHERE round_id=? ORDER BY ordinal',
                           (round_id,))
        per_document = []
        totals = {annotator_a: self._empty_counts(), annotator_b: self._empty_counts()}
        for item in docs:
            document_id = item['document_id']
            record = {'document_id': document_id, 'unavailable': bool(item['unavailable'])}
            if item['unavailable']:
                record['status'] = 'unavailable'
                per_document.append(record)
                continue
            statuses = []
            for annotator in row['annotators']:
                effective = self.effective(round_id, document_id, annotator)
                if effective is None:
                    status = 'not_submitted'
                elif effective.get('explicit_empty'):
                    status = 'explicit_empty'
                else:
                    status = 'submitted'
                totals[annotator][status] += 1
                statuses.append(status)
                record[annotator] = status
            record['status'] = 'both_submitted' if all(value != 'not_submitted' for value in statuses) else 'pending'
            per_document.append(record)
        return {'round_id': round_id, 'state': row['state'], 'annotators': row['annotators'],
                'documents': len(docs), 'totals': totals, 'per_document': per_document}

    @staticmethod
    def _empty_counts():
        return {'submitted': 0, 'explicit_empty': 0, 'not_submitted': 0}

    # ---------- close and reporting ----------
    def _evaluable_pairs(self, round_id):
        row = self.row(round_id)
        annotator_a, annotator_b = row['annotators']
        pairs, skipped = [], []
        for item in self.db.all('SELECT document_id,unavailable,unavailable_reason FROM adjudication_documents WHERE round_id=? ORDER BY ordinal',
                                (round_id,)):
            document_id = item['document_id']
            if item['unavailable']:
                skipped.append({'document_id': document_id, 'status': 'unavailable', 'reason': item['unavailable_reason']})
                continue
            sub_a = self.effective(round_id, document_id, annotator_a)
            sub_b = self.effective(round_id, document_id, annotator_b)
            if sub_a is None or sub_b is None:
                missing = [name for name, value in ((annotator_a, sub_a), (annotator_b, sub_b)) if value is None]
                skipped.append({'document_id': document_id, 'status': 'not_submitted', 'missing': missing})
                continue
            pairs.append((document_id, sub_a, sub_b))
        return {'row': row, 'a': annotator_a, 'b': annotator_b, 'pairs': pairs, 'skipped': skipped}

    def close(self, round_id):
        with self.db.transaction():
            self.require_state(round_id, 'open')
            evaluated = self._evaluable_pairs(round_id)
            self.db.execute("UPDATE adjudication_rounds SET state='closed',closed_at=? WHERE round_id=?",
                            (self.db.clock(), round_id))
            self.db.changed()
            self.db.log('adjudication.closed', {'round_id': round_id,
                                                 'evaluable': len(evaluated['pairs']),
                                                 'skipped': len(evaluated['skipped'])})
            return self.report(round_id)

    def report(self, round_id):
        row = self.require_state(round_id, 'closed')
        annotator_a, annotator_b = row['annotators']
        evaluated = self._evaluable_pairs(round_id)
        pairs = evaluated['pairs']
        skipped = evaluated['skipped']
        effective_by_document = {document_id: (sub_a, sub_b) for document_id, sub_a, sub_b in pairs}
        result = {
            'round_id': round_id,
            'digest': row['digest'],
            'state': 'closed',
            'task_kind': row['task_kind'],
            'annotators': {'a': annotator_a, 'b': annotator_b},
            'vocabulary': row['vocabulary'],
            'evaluable_documents': len(pairs),
            'excluded': skipped,
            'coverage': self.progress(round_id)['totals'],
        }
        if row['task_kind'] in ('document', 'both'):
            result['document_classification'] = agreement.confusion_and_kappa(row['vocabulary'], effective_by_document)
        if row['task_kind'] in ('span', 'both'):
            result['span_matching'] = agreement.span_agreement(row['vocabulary'], effective_by_document)
            result['dispute_summary'] = self._span_summary(pairs)
        return result

    def _span_summary(self, pairs):
        exact = boundary = label_disagree = unmatched = 0
        for _, sub_a, sub_b in pairs:
            labels_a = {span['label'] for span in sub_a['spans']}
            labels_b = {span['label'] for span in sub_b['spans']}
            if labels_a != labels_b:
                label_disagree += 1
            per_label_a, per_label_b = {}, {}
            for span in sub_a['spans']:
                per_label_a.setdefault(span['label'], []).append(span)
            for span in sub_b['spans']:
                per_label_b.setdefault(span['label'], []).append(span)
            doc_boundary = doc_unmatched = False
            all_exact = labels_a == labels_b
            for label in labels_a | labels_b:
                matched = agreement.match_label(per_label_a.get(label, []), per_label_b.get(label, []))
                if matched['unmatched_a'] or matched['unmatched_b']:
                    doc_unmatched = True
                if any(not pair['exact_span'] for pair in matched['pairs']):
                    doc_boundary = True
                # Exact only when every span paired and every pair is an exact match.
                if matched['unmatched_a'] or matched['unmatched_b'] or any(not pair['exact_span'] for pair in matched['pairs']):
                    all_exact = False
            # Two explicit-empty submissions agree exactly (zero spans, same empty label set).
            if all_exact and not doc_boundary and not doc_unmatched:
                exact += 1
            if doc_boundary:
                boundary += 1
            if doc_unmatched:
                unmatched += 1
        return {'documents_exact': exact, 'documents_with_boundary_differences': boundary,
                'documents_with_unmatched_spans': unmatched, 'documents_with_label_differences': label_disagree}

    def disputes(self, round_id):
        row = self.require_state(round_id, 'closed')
        annotator_a, annotator_b = row['annotators']
        output = []
        for document_id, sub_a, sub_b in self._evaluable_pairs(round_id)['pairs']:
            disagreement = False
            detail = {'document_id': document_id,
                      'submissions': {annotator_a: self._submission_ref(round_id, document_id, annotator_a),
                                      annotator_b: self._submission_ref(round_id, document_id, annotator_b)}}
            if row['task_kind'] in ('document', 'both') and sub_a['label'] != sub_b['label']:
                disagreement = True
                detail['document_label'] = {annotator_a: sub_a['label'], annotator_b: sub_b['label']}
            if row['task_kind'] in ('span', 'both'):
                per_label_a, per_label_b = {}, {}
                for span in sub_a['spans']:
                    per_label_a.setdefault(span['label'], []).append(span)
                for span in sub_b['spans']:
                    per_label_b.setdefault(span['label'], []).append(span)
                labels = sorted(set(per_label_a) | set(per_label_b))
                span_detail = {}
                for label in labels:
                    matched = agreement.match_label(per_label_a.get(label, []), per_label_b.get(label, []))
                    if matched['unmatched_a'] or matched['unmatched_b'] or any(not pair['exact_span'] for pair in matched['pairs']):
                        disagreement = True
                    span_detail[label] = matched
                detail['span_matching'] = span_detail
            if disagreement:
                output.append(detail)
        return {'round_id': round_id, 'disputes': output, 'count': len(output)}

    def _submission_ref(self, round_id, document_id, annotator):
        row_submission = self.effective_row(round_id, document_id, annotator)
        return {'annotator': annotator, 'revision': row_submission['revision'], 'submitted_at': row_submission['submitted_at']}
