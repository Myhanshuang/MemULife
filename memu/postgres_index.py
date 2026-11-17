"""Incremental PostgreSQL/pgvector index; bounded SQL retrieval."""
import hashlib
import json
import struct
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .errors import IndexNotReady
from .index import InMemoryIndex, VectorSpaceError, _embedding, _address


def _text_projection(value):
    """PostgreSQL text/jsonb cannot represent NUL; retain a visible escape in the index."""
    if isinstance(value, str):
        return value.replace('\x00', '\\u0000')
    if isinstance(value, dict):
        return {_text_projection(key): _text_projection(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_text_projection(item) for item in value]
    return value


def _block_hash(block, vector):
    # A file revision changes when a neighbouring Markdown entry is appended.
    # It is supplied from files at read time, not part of every block's identity.
    content = [{key: value for key, value in block.items() if key != 'revision'},
               {key: vector.get(key) for key in ('space_id', 'model_version', 'preprocess_version', 'metric')}
               if vector else {}]
    checksum = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':'), allow_nan=False).encode())
    values = vector.get('vector', [])
    checksum.update(struct.pack('!' + 'f' * len(values), *values))
    return checksum.hexdigest()


def _targets(refs):
    result = []
    for ref in refs:
        alias, lo, hi, start, end = _address(ref)
        result.append({'ref': ref, 'alias': alias, 'lo': lo, 'hi': hi, 'begin_seconds': start, 'end_seconds': end})
    return result


class PostgresIndex:
    incremental = True

    def __init__(self, dsn: str, *, initialize: bool = True, rrf_constant: int = 60) -> None:
        if type(rrf_constant) is not int or rrf_constant < 1:
            raise ValueError('RRF constant must be a positive integer')
        self.rrf_constant = rrf_constant
        try:
            import psycopg
            from psycopg import sql
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise IndexNotReady('PostgreSQL requires the optional psycopg[binary] dependency') from exc
        self._psycopg, self._sql, self._dict_row, self._json = psycopg, sql, dict_row, Jsonb
        self._dsn = dsn
        if initialize:
            with self._connect() as conn:
                conn.execute(Path(__file__).with_name('sql').joinpath('schema.sql').read_text())

    def _connect(self):
        return self._psycopg.connect(self._dsn, row_factory=self._dict_row)

    @staticmethod
    def _vector_text(vector: list[float]) -> str:
        return '[' + ','.join(str(v) for v in vector) + ']'

    @staticmethod
    def _cast(dimensions: int) -> tuple[str, str]:
        if dimensions <= 2000:
            return f'vector({dimensions})', 'vector'
        if dimensions <= 4000:
            return f'halfvec({dimensions})', 'halfvec'
        raise VectorSpaceError('HNSW supports at most 4000 dimensions; configure a smaller embedding')

    def vectors(self, aliases=None):
        with self._connect() as conn:
            where = ' AND f.alias=ANY(%s)' if aliases is not None else ''
            rows = conn.execute('''SELECT b.ref,b.embedding::text AS vector,b.model_version,b.space_id,
                b.preprocess_version,b.metric FROM blocks b JOIN files f ON f.id=b.file_id
                WHERE b.embedding IS NOT NULL''' + where,
                (list(aliases),) if aliases is not None else ()).fetchall()
        return {row['ref']: {key: value for key, value in row.items() if key != 'ref'} |
                {'vector': json.loads(row['vector'])} for row in rows}

    def file_revisions(self):
        with self._connect() as conn:
            return {row['alias']: row['revision'] for row in conn.execute('SELECT alias,revision FROM files')}

    def _dependents(self, conn, refs):
        rows = conn.execute('''WITH RECURSIVE affected(ref,alias,lo,hi,begin_seconds,end_seconds) AS (
            SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x(ref text,alias text,lo integer,hi integer,
                begin_seconds double precision,end_seconds double precision)
            UNION
            SELECT b.ref,f.alias,b.start_line,b.end_line,b.start_seconds,b.end_seconds
            FROM affected a JOIN reference_edges e ON e.target_alias=a.alias AND e.category='fact' AND (
                (a.lo IS NULL AND a.begin_seconds IS NULL) OR
                (e.start_line IS NULL AND e.start_seconds IS NULL) OR
                (a.lo IS NOT NULL AND e.start_line<=a.hi AND e.end_line>=a.lo) OR
                (a.begin_seconds IS NOT NULL AND e.start_seconds<a.end_seconds AND e.end_seconds>a.begin_seconds))
            JOIN blocks b ON b.ref=e.owner_ref JOIN files f ON f.id=b.file_id
        ) SELECT DISTINCT b.ref FROM affected a JOIN blocks b ON b.ref=a.ref WHERE b.kind='unit' ''',
            (self._json(_targets(refs)),)).fetchall()
        return {row['ref'] for row in rows}

    def dependent_blocks(self, refs):
        seeds = []
        for ref in refs:
            alias, lo, hi, start, end = _address(ref)
            seeds.append({'ref': ref, 'alias': alias, 'lo': lo, 'hi': hi, 'begin_seconds': start, 'end_seconds': end})
        with self._connect() as conn:
            rows = conn.execute('''WITH RECURSIVE reached(ref,alias,lo,hi,begin_seconds,end_seconds) AS (
                SELECT * FROM jsonb_to_recordset(%s::jsonb) AS x(ref text,alias text,lo integer,hi integer,
                    begin_seconds double precision,end_seconds double precision)
                UNION
                SELECT b.ref,f.alias,b.start_line,b.end_line,b.start_seconds,b.end_seconds
                FROM reached r JOIN reference_edges e ON e.target_alias=r.alias AND e.category='fact' AND
                    ((r.lo IS NULL AND r.begin_seconds IS NULL) OR
                     (e.start_line IS NULL AND e.start_seconds IS NULL) OR
                     (r.lo IS NOT NULL AND e.start_line<=r.hi AND e.end_line>=r.lo) OR
                     (r.begin_seconds IS NOT NULL AND e.start_seconds<r.end_seconds AND e.end_seconds>r.begin_seconds))
                JOIN blocks b ON b.ref=e.owner_ref JOIN files f ON f.id=b.file_id
            ) SELECT DISTINCT b.ref,b.payload || jsonb_build_object('revision',f.revision) AS payload
                FROM reached r JOIN files f ON f.alias=r.alias
                JOIN blocks b ON b.file_id=f.id WHERE b.ref=r.ref OR
                (b.kind='detail' AND r.lo IS NOT NULL AND b.start_line<=r.hi AND b.end_line>=r.lo)''',
                (self._json(seeds),)).fetchall()
        return [row['payload'] for row in rows]

    def sync(self, files, blocks, vectors, *, full=True):
        files, blocks = _text_projection(list(files)), _text_projection(list(blocks))
        # Validation is limited to the changed files. Cross-file edges are resolved
        # in SQL against both new and previously committed blocks.
        staged = InMemoryIndex(rrf_constant=self.rrf_constant)
        staged.sync(files, blocks, vectors)
        aliases = list(staged._files)
        with self._connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(716862039104143)')
            changed = set()
            if full:
                changed.update(row['ref'] for row in conn.execute('SELECT b.ref FROM blocks b JOIN files f '
                    'ON f.id=b.file_id WHERE NOT(f.alias=ANY(%s))', (aliases,)))
                conn.execute('DELETE FROM files WHERE NOT(alias=ANY(%s))', (aliases,))
            file_ids = {}
            for alias, file in staged._files.items():
                row = conn.execute('''INSERT INTO files(alias,path,kind,revision,metadata) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(alias) DO UPDATE SET path=excluded.path,kind=excluded.kind,
                    revision=excluded.revision,metadata=excluded.metadata RETURNING id''',
                    (alias, file['path'], file['kind'], file['revision'], self._json(file.get('metadata', {})))).fetchone()
                file_ids[alias] = row['id']
            refs = list(staged._blocks)
            changed.update(row['ref'] for row in conn.execute(
                'DELETE FROM blocks WHERE file_id=ANY(%s) AND NOT(ref=ANY(%s)) RETURNING ref',
                (list(file_ids.values()), refs)))
            previous = {row['ref']: row['content_hash'] for row in conn.execute(
                'SELECT ref,content_hash FROM blocks WHERE file_id=ANY(%s)', (list(file_ids.values()),))}
            for ref, block in staged._blocks.items():
                alias, lo, hi, start, end = _address(ref)
                vector = staged._vectors.get(ref, {})
                checksum = _block_hash(block, vector)
                if previous.get(ref) == checksum:
                    continue
                changed.add(ref)
                conn.execute('''INSERT INTO blocks(file_id,ref,kind,start_line,end_line,start_seconds,end_seconds,
                    event_time,source,topics,body,payload,embedding,model_version,space_id,preprocess_version,metric,content_hash)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s)
                    ON CONFLICT(ref) DO UPDATE SET kind=excluded.kind,start_line=excluded.start_line,
                    end_line=excluded.end_line,start_seconds=excluded.start_seconds,end_seconds=excluded.end_seconds,
                    event_time=excluded.event_time,source=excluded.source,topics=excluded.topics,body=excluded.body,
                    payload=excluded.payload,embedding=excluded.embedding,model_version=excluded.model_version,
                    space_id=excluded.space_id,preprocess_version=excluded.preprocess_version,metric=excluded.metric,
                    content_hash=excluded.content_hash''',
                    (file_ids[alias], ref, block['kind'], lo, hi, start, end,
                     self._json(block.get('time', {'precision': 'unknown', 'value': None})),
                     block.get('source', ''), block.get('topics', []), block.get('body', ''), self._json(block),
                     self._vector_text(vector['vector']) if vector else None, vector.get('model_version'),
                    vector.get('space_id'), vector.get('preprocess_version'), vector.get('metric'), checksum))
                conn.execute('DELETE FROM reference_edges WHERE owner_ref=%s', (ref,))
                edges = [(target, 'fact', '') for target in block.get('facts', [])]
                edges += [(item['target'], 'relation', item['rel']) for item in block.get('relations', [])]
                for target, category, rel in edges:
                    target_alias, first, last, begin, finish = _address(target)
                    conn.execute('''INSERT INTO reference_edges(owner_ref,target_ref,target_alias,start_line,end_line,
                        start_seconds,end_seconds,category,rel) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING''', (ref, target, target_alias, first, last, begin, finish, category, rel))
            owners = changed | {r['owner_ref'] for r in conn.execute('''SELECT DISTINCT e.owner_ref
                FROM jsonb_to_recordset(%s::jsonb) AS a(ref text,alias text,lo integer,hi integer,
                    begin_seconds double precision,end_seconds double precision)
                JOIN reference_edges e ON e.target_alias=a.alias AND (
                    (a.lo IS NULL AND a.begin_seconds IS NULL) OR
                    (e.start_line IS NULL AND e.start_seconds IS NULL) OR
                    (a.lo IS NOT NULL AND e.start_line<=a.hi AND e.end_line>=a.lo) OR
                    (a.begin_seconds IS NOT NULL AND e.start_seconds<a.end_seconds AND e.end_seconds>a.begin_seconds))''',
                (self._json(_targets(changed)),))}
            conn.execute('DELETE FROM links WHERE from_id IN (SELECT id FROM blocks WHERE ref=ANY(%s))', (list(owners),))
            conn.execute('''INSERT INTO links(from_id,to_id,category,rel,target_ref)
                SELECT owner.id,target.id,e.category,e.rel,e.target_ref FROM reference_edges e
                JOIN blocks owner ON owner.ref=e.owner_ref
                CROSS JOIN LATERAL (
                    SELECT b.id FROM blocks b JOIN files f ON f.id=b.file_id WHERE f.alias=e.target_alias AND (
                        (e.category='relation' AND b.ref=e.target_ref AND b.kind='unit') OR
                        (e.category='fact' AND (b.ref=e.target_ref OR
                          (e.start_line IS NOT NULL AND b.start_line<=e.start_line AND b.end_line>=e.end_line) OR
                          (e.start_seconds IS NOT NULL AND b.start_seconds<=e.start_seconds AND b.end_seconds>=e.end_seconds) OR
                          (b.start_line IS NULL AND b.start_seconds IS NULL))))
                    ORDER BY (b.ref=e.target_ref) DESC,COALESCE(b.end_line-b.start_line,2147483647),
                        COALESCE(b.end_seconds-b.start_seconds,1e100),b.ref LIMIT 1
                ) target WHERE e.owner_ref=ANY(%s) ON CONFLICT DO NOTHING''', (list(owners),))
            affected = self._dependents(conn, changed)
            conn.execute('DELETE FROM unit_vector_targets WHERE unit_ref=ANY(%s)', (list(affected),))
            # Traverse factual evidence only. Media spans overlap original facts;
            # relation links never cause a Unit to inherit another Unit's vector.
            conn.execute('''WITH RECURSIVE evidence(unit_ref,ref) AS (
                SELECT ref,ref FROM blocks WHERE ref=ANY(%s) AND kind='unit' UNION
                SELECT e.unit_ref,t.ref FROM evidence e JOIN blocks b ON b.ref=e.ref
                  JOIN links l ON l.from_id=b.id AND l.category='fact'
                  JOIN blocks t ON t.id=l.to_id WHERE b.kind IN ('unit','detail')
            ), targets AS (
                SELECT e.unit_ref,e.ref AS vector_ref FROM evidence e JOIN blocks b ON b.ref=e.ref
                  WHERE b.embedding IS NOT NULL AND b.kind='unit'
                UNION
                SELECT e.unit_ref,m.ref FROM evidence e JOIN reference_edges r ON r.owner_ref=e.ref AND r.category='fact'
                  JOIN files f ON f.alias=r.target_alias JOIN blocks m ON m.file_id=f.id
                  WHERE m.kind='media' AND m.embedding IS NOT NULL AND
                    (r.start_seconds IS NULL OR m.start_seconds IS NULL OR
                     (m.start_seconds<r.end_seconds AND m.end_seconds>r.start_seconds))
            ) INSERT INTO unit_vector_targets SELECT * FROM targets ON CONFLICT DO NOTHING''', (list(affected),))
            for value in {item['space_id']: item for item in staged._vectors.values()}.values():
                self._space(conn, value)

    def _space(self, conn, value):
        space_id, dimensions, metric = value['space_id'], len(value['vector']), value['metric']
        signature = {'dimensions': dimensions, 'metric': metric, 'model_version': value['model_version'],
                     'preprocess_version': value['preprocess_version']}
        prior = conn.execute('SELECT * FROM vector_spaces WHERE space_id=%s', (space_id,)).fetchone()
        if prior:
            if any(prior[key] != val for key, val in signature.items()):
                raise VectorSpaceError('Incompatible vector space identity')
            return
        conn.execute('''INSERT INTO vector_spaces(space_id,dimensions,metric,model_version,preprocess_version)
            VALUES (%s,%s,%s,%s,%s)''', (space_id, dimensions, metric, value['model_version'], value['preprocess_version']))
        cast, family = self._cast(dimensions)
        op = {'cosine': 'cosine', 'l2': 'l2', 'inner_product': 'ip'}[metric]
        suffix = hashlib.sha256(f'{space_id}:{dimensions}:{metric}'.encode()).hexdigest()[:24]
        conn.execute(self._sql.SQL('CREATE INDEX IF NOT EXISTS {} ON blocks USING hnsw ((embedding::{}) {}) '
                                  'WHERE space_id={} AND embedding IS NOT NULL').format(
            self._sql.Identifier('blocks_hnsw_' + suffix), self._sql.SQL(cast),
            self._sql.Identifier(f'{family}_{op}_ops'), self._sql.Literal(space_id)))

    @staticmethod
    def _scope(scope, alias='b'):
        scope = scope or {}
        clauses, params = [], []
        sources = scope.get('sources', scope.get('source', []))
        topics = scope.get('topics', [])
        if sources:
            clauses.append(f'{alias}.source=ANY(%s)')
            params.append([sources] if isinstance(sources, str) else sources)
        if topics:
            clauses.append(f'{alias}.topics && %s::text[]')
            params.append([topics] if isinstance(topics, str) else topics)
        directory = scope.get('directory', scope.get('path', scope.get('prefix')))
        if not directory:
            date = scope.get('day') or scope.get('month') or scope.get('year') or scope.get('date')
            directory = str(date).replace('-', '/') if date else None
        if directory:
            directory = str(directory).strip('/')
            if '..' in PurePosixPath(directory).parts:
                raise ValueError('Scope directory cannot contain ..')
            escaped = directory.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            clauses.append(f"({alias}.payload->>'path'=%s OR {alias}.payload->>'path' LIKE %s)")
            params.extend([directory, escaped + '/%'])
        return ' AND '.join(clauses) or 'TRUE', params

    def grep(self, query, scope=None, layers=None, limit=12, offset=0):
        clause, params = self._scope(scope)
        pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        with self._connect() as conn:
            rows = conn.execute(f'''SELECT b.payload || jsonb_build_object('revision',f.revision) AS payload
                FROM blocks b JOIN files f ON f.id=b.file_id WHERE {clause}
                AND (b.kind=ANY(%s) OR b.payload->>'view'=ANY(%s)) AND b.kind<>'media'
                AND b.body LIKE %s ORDER BY b.ref LIMIT %s OFFSET %s''',
                (*params, layers or ['unit'], layers or ['unit'], pattern, limit, offset)).fetchall()
            return [row['payload'] for row in rows]

    def search(self, query_vectors, scope=None, limit=12, offset=0):
        if limit < 0 or offset < 0 or limit + offset > 5000:
            raise ValueError('Semantic result window must be between 0 and 5000')
        queries = [_embedding(value) for value in query_vectors]
        clause, params = self._scope(scope, 'u')
        scores, matches, media = defaultdict(float), defaultdict(list), defaultdict(set)
        with self._connect() as conn:
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            version = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
            if version and tuple(int(part) for part in version['extversion'].split('.')[:2]) >= (0, 8):
                conn.execute("SET LOCAL hnsw.iterative_scan='strict_order'")
            for position, query in enumerate(queries):
                signature = conn.execute('SELECT dimensions,metric FROM vector_spaces WHERE space_id=%s',
                                         (query['space_id'],)).fetchone()
                if not signature:
                    continue
                if signature != {'dimensions': len(query['vector']), 'metric': query['metric']}:
                    raise VectorSpaceError('Incompatible query vector space')
                cast, _ = self._cast(len(query['vector']))
                op = {'cosine': '<=>', 'l2': '<->', 'inner_product': '<#>'}[query['metric']]
                vector = self._vector_text(query['vector'])
                rows = conn.execute(f'''SELECT b.ref,b.kind,(b.embedding::{cast} {op} %s::{cast}) AS distance
                    FROM blocks b WHERE b.space_id=%s AND b.embedding IS NOT NULL AND EXISTS (
                      SELECT 1 FROM unit_vector_targets m JOIN blocks u ON u.ref=m.unit_ref
                      WHERE m.vector_ref=b.ref AND {clause})
                    ORDER BY b.embedding::{cast} {op} %s::{cast} LIMIT %s''',
                    (vector, query['space_id'], *params, vector, min(20000, max(200, (limit + offset) * 16)))).fetchall()
                mapping = defaultdict(list)
                for row in conn.execute(f'''SELECT m.vector_ref,m.unit_ref FROM unit_vector_targets m
                    JOIN blocks u ON u.ref=m.unit_ref WHERE m.vector_ref=ANY(%s) AND {clause}
                    ORDER BY array_position(%s::text[],m.vector_ref),m.unit_ref LIMIT 20000''',
                    ([row['ref'] for row in rows], *params, [row['ref'] for row in rows])):
                    mapping[row['vector_ref']].append(row['unit_ref'])
                counted = set()
                for rank, row in enumerate(rows, 1):
                    for unit in mapping[row['ref']]:
                        if row['kind'] == 'media':
                            media[unit].add(row['ref'])
                        if unit in counted:
                            continue
                        counted.add(unit)
                        scores[unit] += 1 / (self.rrf_constant + rank)
                        matches[unit].append({'query_index': position, 'ref': row['ref'],
                                              'rank': rank, 'distance': row['distance']})
            ordered = sorted(scores, key=lambda ref: (-scores[ref], ref))[offset:offset + limit]
            payloads = {row['ref']: row['payload'] for row in conn.execute(
                "SELECT b.ref,b.payload || jsonb_build_object('revision',f.revision) AS payload "
                'FROM blocks b JOIN files f ON f.id=b.file_id WHERE b.ref=ANY(%s)', (ordered,))}
            return [{**payloads[ref], 'score': scores[ref], 'matches': matches[ref],
                     'matched_media': sorted(media[ref])} for ref in ordered]

    def links(self, ref: str, direction: str = 'both', relations: list[str] | None = None,
              hops: int = 1, category: str | None = 'relation') -> list[dict[str, Any]]:
        if direction not in ('in', 'out', 'both') or not 1 <= hops <= 32:
            raise ValueError('Invalid edge traversal direction or hops')
        direction_clause = {'in': 'l.to_id = w.node', 'out': 'l.from_id = w.node',
                            'both': '(l.from_id = w.node OR l.to_id = w.node)'}[direction]
        with self._connect() as conn:
            rows = conn.execute(f'''WITH RECURSIVE walk(node,depth,visited) AS (
                SELECT id,0,ARRAY[id] FROM blocks WHERE ref=%s
                UNION ALL
                SELECT CASE WHEN l.from_id=w.node THEN l.to_id ELSE l.from_id END,
                       w.depth+1,w.visited || CASE WHEN l.from_id=w.node THEN l.to_id ELSE l.from_id END
                FROM walk w JOIN links l ON {direction_clause}
                WHERE w.depth<%s AND (%s::text IS NULL OR l.category=%s)
                  AND (%s::text[] IS NULL OR l.rel=ANY(%s))
                  AND NOT (CASE WHEN l.from_id=w.node THEN l.to_id ELSE l.from_id END=ANY(w.visited))
            ) SELECT DISTINCT f.ref AS "from",t.ref AS "to",l.target_ref AS target,l.category,l.rel
              FROM walk w JOIN links l ON {direction_clause}
              JOIN blocks f ON f.id=l.from_id JOIN blocks t ON t.id=l.to_id
              WHERE w.depth<%s AND (%s::text IS NULL OR l.category=%s)
                  AND (%s::text[] IS NULL OR l.rel=ANY(%s))
              ORDER BY "from","to",l.rel''',
                                (ref, hops, category, category, relations, relations,
                                 hops, category, category, relations, relations)).fetchall()
            return rows

    def delete(self, refs: Iterable[str]) -> None:
        with self._connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(716862039104143)')
            conn.execute('DELETE FROM blocks WHERE ref=ANY(%s)', (list(refs),))

    def close(self) -> None:
        # Connections are scoped to calls; no socket remains open.
        pass
