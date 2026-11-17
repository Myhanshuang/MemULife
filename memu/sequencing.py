"""Conversation order is known data; no language-model call is needed for continuation edges."""
import hashlib
import json

from .storage import _render_entry


class ConversationSequence:
    def _conversation_identity(self, request):
        content = request['metadata'].get('content', {})
        origin = request['metadata'].get('memulife', {})
        return hashlib.sha256(json.dumps([request['source'], origin.get('source_instance_id'),
                                          content.get('conversation_id')]).encode()).hexdigest()

    def _sequence_neighbors(self, conversation, position, key, exclude=()):
        if getattr(self.repo, 'postgres', False):
            rows = []
            for operator, direction in (('<', 'DESC'), ('>', 'ASC')):
                row = self.repo.db.execute(f'''SELECT key,value FROM memu_control WHERE namespace='conversation_turns'
                    AND value->>'conversation'=%s AND NOT (key = ANY(%s)) AND (value->>'position',key) {operator} (%s,%s)
                    ORDER BY value->>'position' {direction},key {direction} LIMIT 1''',
                    (conversation, list(exclude), position, key)).fetchone()
                rows.append(self.repo.state['conversation_turns'][row['key']] if row else None)
            return rows
        rows = sorted((value['position'], k, value) for k,value in self.repo.state.setdefault('conversation_turns', {}).items()
                      if value['conversation'] == conversation and k != key and k not in exclude)
        before = [v for p,k,v in rows if (p,k) < (position,key)]
        after = [v for p,k,v in rows if (p,k) > (position,key)]
        return (before[-1] if before else None, after[0] if after else None)

    def _set_continuation(self, ref, previous):
        entry = self.repo.read(ref)
        entry['relations'] = [rel for rel in entry.get('relations', []) if rel['rel'] != '延续']
        if previous and '延续' in self.config.build.relation_vocab:
            entry['relations'].append({'rel': '延续', 'target': previous})
        lines = self.repo._bytes(entry['path']).decode().splitlines(keepends=True)
        rendered = _render_entry(entry, entry['source']).splitlines(keepends=True)
        if len(rendered) != entry['end_line'] - entry['start_line'] + 1:
            raise ValueError('Conversation link must preserve stable line positions')
        lines[entry['start_line']-1:entry['end_line']] = rendered
        self.repo._transaction({entry['path']: ''.join(lines)})

    def _link_conversation(self, request, refs):
        if not refs:
            return
        conversation = self._conversation_identity(request)
        stamp = request['time'].get('value')
        position = stamp if isinstance(stamp, str) else f"unknown:{request.get('created_at', 0):020.6f}"
        before, after = self._sequence_neighbors(conversation, position, request['request_id'])
        prior = before['last'] if before else None
        for ref in refs:
            self._set_continuation(ref, prior); prior = ref
        if after:
            self._set_continuation(after['first'], refs[-1])
        self.repo.state.setdefault('conversation_turns', {})[request['request_id']] = {
            'conversation': conversation, 'position': position, 'first': refs[0], 'last': refs[-1], 'refs': refs}
        for ref in refs:
            self.repo.state.setdefault('conversation_units', {})[ref] = request['request_id']

    def _unlink_conversation(self, request_id, exclude=()):
        row = self.repo.state.setdefault('conversation_turns', {}).get(request_id)
        if not row:
            return
        before, after = self._sequence_neighbors(row['conversation'], row['position'], request_id, exclude)
        if after:
            self._set_continuation(after['first'], before['last'] if before else None)
        for ref in row.get('refs', [row['first'], row['last']]):
            self.repo.state.setdefault('conversation_units', {}).pop(ref, None)
        del self.repo.state['conversation_turns'][request_id]

    def _remove_conversation_units(self, erased):
        owners = self.repo.state.setdefault('conversation_units', {})
        keys = {owners[ref] for ref in erased if ref in owners}
        turns = self.repo.state.setdefault('conversation_turns', {})
        removed, retained = [], []
        for key in keys:
            row = turns[key]
            refs = [ref for ref in row['refs'] if ref not in erased]
            if refs:
                row.update(refs=refs, first=refs[0], last=refs[-1]); retained.append(key)
            else:
                removed.append(key)
        for key in removed:
            self._unlink_conversation(key, exclude=removed)
        for key in retained:
            row = turns[key]
            before, after = self._sequence_neighbors(row['conversation'], row['position'], key, removed)
            prior = before['last'] if before else None
            for ref in row['refs']:
                self._set_continuation(ref, prior); prior = ref
            if after:
                self._set_continuation(after['first'], prior)
        for ref in erased:
            owners.pop(ref, None)
