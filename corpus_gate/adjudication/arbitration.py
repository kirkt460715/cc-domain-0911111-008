'''Adjudication decisions stored separately from the original submissions.

Arbitration never mutates the annotators' effective submissions. Each decision
appends a new revision row, so re-arbitrating a document keeps the prior
decisions traceable.
'''
import json
from corpus_gate.errors import identifier, canonical, digest, text, fail
from corpus_gate.review import desk
from corpus_gate.adjudication import rounds as rounds_module


class Arbitrations:
    def __init__(self, db):
        self.db = db

    def _rounds(self):
        # Reuse one Rounds instance; constructing it only runs IF NOT EXISTS DDL.
        if not hasattr(self, '_rounds_store'):
            self._rounds_store = rounds_module.Rounds(self.db)
        return self._rounds_store

    def decide(self, round_id, document_id, arbitrator, payload, reason):
        reason = text(reason, 'reason')
        store = self._rounds()
        with self.db.transaction():
            row = store.require_state(round_id, 'closed')
            desk.actor(self.db, identifier(arbitrator, 'arbitrator'))
            if arbitrator in row['annotators']:
                fail('adjudication_independence', 'An arbitrator cannot be one of the round annotators')
            member = store.member(row, document_id)
            if member['unavailable']:
                fail('adjudication_unavailable', 'Cannot arbitrate an unavailable document')
            label, spans, explicit_empty = store.validate_payload(row, document_id, payload)
            revision = (self.db.scalar('SELECT MAX(revision) FROM adjudication_arbitrations WHERE round_id=? AND document_id=?',
                                       (round_id, document_id)) or 0) + 1
            stored = {'spans': spans, 'explicit_empty': explicit_empty}
            self.db.execute('INSERT INTO adjudication_arbitrations VALUES(?,?,?,?,?,?,?,?)',
                            (round_id, document_id, revision, arbitrator, label, canonical(stored), reason, self.db.clock()))
            self.db.log('adjudication.arbitrated', {'round_id': round_id, 'document_id': document_id,
                                                    'arbitrator': arbitrator, 'revision': revision})
            return self.get(round_id, document_id)

    def get(self, round_id, document_id):
        store = self._rounds()
        store.row(round_id)
        store.member(store.row(round_id), document_id)
        rows = self.db.all('SELECT revision,arbitrator,label,payload,reason,created_at FROM adjudication_arbitrations '
                           'WHERE round_id=? AND document_id=? ORDER BY revision', (round_id, document_id))
        if not rows:
            return {'round_id': round_id, 'document_id': document_id, 'arbitrated': False, 'history': []}
        for item in rows:
            item['payload'] = json.loads(item['payload'])
        latest = rows[-1]
        return {'round_id': round_id, 'document_id': document_id, 'arbitrated': True,
                'latest_revision': latest['revision'], 'arbitrator': latest['arbitrator'],
                'label': latest['label'], 'payload': latest['payload'], 'reason': latest['reason'],
                'created_at': latest['created_at'], 'history': rows}

    def list_for_round(self, round_id):
        store = self._rounds()
        store.require_state(round_id, 'closed')
        docs = self.db.all('SELECT document_id FROM adjudication_documents WHERE round_id=? ORDER BY ordinal', (round_id,))
        return [self.get(round_id, item['document_id']) for item in docs if self.get(round_id, item['document_id'])['arbitrated']]

    def export(self, round_id):
        '''Round-bound export: every payload carries the frozen round digest.'''
        store = self._rounds()
        row = store.require_state(round_id, 'closed')
        report = store.report(round_id)
        annotator_a, annotator_b = row['annotators']
        documents_out = []
        for document_id, sub_a, sub_b in store._evaluable_pairs(round_id)['pairs']:
            arbitration = self.get(round_id, document_id)
            documents_out.append({
                'document_id': document_id,
                'submission_a': {'annotator': annotator_a, **sub_a},
                'submission_b': {'annotator': annotator_b, **sub_b},
                'arbitration': arbitration if arbitration['arbitrated'] else None,
            })
        envelope = {
            'round_id': round_id,
            'round_digest': row['digest'],
            'frozen_definition': row['definition'],
            'task_kind': row['task_kind'],
            'vocabulary': row['vocabulary'],
            'annotators': row['annotators'],
            'exported_at': self.db.clock(),
            'report': report,
            'documents': documents_out,
            'excluded': report['excluded'],
        }
        envelope['envelope_digest'] = digest({key: value for key, value in envelope.items() if key != 'envelope_digest'})
        return envelope
