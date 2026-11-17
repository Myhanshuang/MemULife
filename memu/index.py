"""Rebuildable indexes and reciprocal-rank fusion for the v1.3 repository.

``InMemoryIndex`` is an explicit offline/test implementation. Production never
falls back to it when PostgreSQL is unavailable.
"""

from __future__ import annotations

import copy
import math
import threading
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any, Iterable

from .refs import Ref


class IndexErrorBase(RuntimeError):
    """An index cannot be used safely; the caller must report it as not ready."""
    code = 'index_error'


class VectorSpaceError(IndexErrorBase):
    code = 'vector_space_error'


def _address(ref: str) -> tuple[str, int | None, int | None, float | None, float | None]:
    parsed = Ref.parse(ref)
    seconds = float(parsed.start_seconds) if parsed.start_seconds is not None else None
    until = float(parsed.end_seconds) if parsed.end_seconds is not None else None
    return parsed.alias, parsed.start, parsed.end, seconds, until


def _overlaps(left: str, right: str) -> bool:
    a, lo, hi, start, end = _address(left)
    b, other_lo, other_hi, other_start, other_end = _address(right)
    if a != b:
        return False
    if lo is not None and other_lo is not None:
        return lo <= other_hi and other_lo <= hi
    if start is not None and other_start is not None:
        return start < other_end and other_start < end
    return True  # A file entry includes its line ranges or media segments.


def matches_scope(block: dict[str, Any], scope: dict[str, Any] | None = None) -> bool:
    """Apply directory, provenance, and topic filters before ranking Units."""
    scope = scope or {}
    sources = scope.get('sources', scope.get('source', []))
    sources = [sources] if isinstance(sources, str) else sources
    if sources and block.get('source') not in sources:
        return False
    topics = scope.get('topics', [])
    topics = [topics] if isinstance(topics, str) else topics
    if topics and not set(topics).intersection(block.get('topics', [])):
        return False
    directory = scope.get('directory', scope.get('path', scope.get('prefix')))
    if not directory:
        date = scope.get('day') or scope.get('month') or scope.get('year') or scope.get('date')
        if date:
            directory = str(date).replace('-', '/')
    if directory:
        prefix = str(directory).strip('/')
        if '..' in PurePosixPath(prefix).parts:
            raise ValueError('Scope directory cannot contain ..')
        path = str(block.get('path', '')).strip('/')
        if prefix and path != prefix and not path.startswith(prefix + '/'):
            return False
    return True


def _embedding(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    vector = result.get('vector')
    if not isinstance(vector, (list, tuple)) or not vector:
        raise VectorSpaceError('An embedding must contain a nonempty vector')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
        raise VectorSpaceError('Embedding values must be finite numbers')
    result['vector'] = [float(v) for v in vector]
    for name in ('model_version', 'space_id', 'preprocess_version'):
        if not isinstance(result.get(name), str) or not result[name]:
            raise VectorSpaceError(f'Missing embedding metadata: {name}')
    metric = result.setdefault('metric', 'cosine')
    if metric not in ('cosine', 'l2', 'inner_product'):
        raise VectorSpaceError(f'Unsupported distance metric: {metric}')
    if metric == 'cosine' and not any(vector):
        raise VectorSpaceError('Cosine distance is undefined for a zero vector')
    return result


def _distance(query: dict[str, Any], target: dict[str, Any]) -> float:
    if query['space_id'] != target['space_id']:
        raise VectorSpaceError('Vectors from different spaces cannot be compared')
    a, b = query['vector'], target['vector']
    if len(a) != len(b) or query['metric'] != target['metric']:
        raise VectorSpaceError('The same space_id has incompatible dimensions or metrics')
    if query['metric'] == 'l2':
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
    dot = sum(x * y for x, y in zip(a, b))
    if query['metric'] == 'inner_product':
        return -dot
    return 1 - dot / math.sqrt(sum(x*x for x in a) * sum(y*y for y in b))


class InMemoryIndex:
    """An explicit, deterministic offline index with the production API."""

    def __init__(self, *, rrf_constant: int = 60) -> None:
        if type(rrf_constant) is not int or rrf_constant < 1:
            raise ValueError('RRF constant must be a positive integer')
        self.rrf_constant = rrf_constant
        self._lock = threading.RLock()
        self._files: dict[str, dict[str, Any]] = {}
        self._blocks: dict[str, dict[str, Any]] = {}
        self._vectors: dict[str, dict[str, Any]] = {}
        self._edges: list[dict[str, Any]] = []

    def sync(self, files: Iterable[dict[str, Any]], blocks: Iterable[dict[str, Any]],
             vectors: dict[str, dict[str, Any]]) -> None:
        """Validate a complete replacement before publishing any changes."""
        file_map: dict[str, dict[str, Any]] = {}
        paths: set[str] = set()
        for source in files:
            file = copy.deepcopy(source)
            alias, path = file['alias'], file['path']
            if alias in file_map or path in paths:
                raise ValueError('Aliases and file paths must be bijective')
            file_map[alias] = file
            paths.add(path)
        block_map: dict[str, dict[str, Any]] = {}
        positions: set[tuple[Any, ...]] = set()
        for source in blocks:
            block = copy.deepcopy(source)
            ref = block['ref'] = Ref.parse(block['ref']).canonical
            alias, start, end, seconds, until = _address(ref)
            if alias not in file_map:
                raise ValueError(f'Unknown file alias: {alias}')
            position = (alias, start) if start is not None else (alias, seconds, until)
            if ref in block_map or position in positions:
                raise ValueError(f'Duplicate block address: {ref}')
            positions.add(position)
            block.setdefault('path', file_map[alias]['path'])
            block.setdefault('revision', file_map[alias].get('revision', ''))
            block.setdefault('body', block.get('text', ''))
            block.setdefault('facts', [])
            block.setdefault('relations', [])
            block.setdefault('topics', [])
            block['facts'] = [Ref.parse(fact).canonical for fact in block['facts']]
            block['relations'] = [{**relation, 'target': Ref.parse(relation['target']).canonical}
                                  for relation in block['relations']]
            block_map[ref] = block
        normalized_vectors: dict[str, dict[str, Any]] = {}
        spaces: dict[str, tuple[int, str]] = {}
        for ref, value in vectors.items():
            ref = Ref.parse(ref).canonical
            if ref in normalized_vectors:
                raise ValueError(f'Duplicate canonical embedding locator: {ref}')
            if ref not in block_map:
                alias, _, _, start, end = _address(ref)
                parent = block_map.get(alias)
                if start is None or parent is None or parent['kind'] != 'media':
                    raise ValueError(f'Embedding object does not exist: {ref}')
                duration = parent.get('duration', parent.get('metadata', {}).get('duration'))
                if duration is not None and end > duration:
                    raise ValueError(f'Media segment exceeds duration: {ref}')
                block_map[ref] = {**parent, 'ref': ref, 'start': start, 'end': end}
            if block_map[ref]['kind'] not in ('unit', 'media'):
                raise ValueError('Only Unit bodies and original media have embeddings')
            embedding = _embedding(value)
            signature = (len(embedding['vector']), embedding['metric'])
            old = spaces.setdefault(embedding['space_id'], signature)
            if old != signature:
                raise VectorSpaceError('One space_id must have one dimension and metric')
            normalized_vectors[ref] = embedding
        edges: list[dict[str, Any]] = []
        for ref, block in block_map.items():
            for fact in block.get('facts', []):
                matches = self._resolve_target(fact, block_map)
                if matches:
                    edges.append({'from': ref, 'to': matches, 'target': fact, 'category': 'fact', 'rel': ''})
            for relation in block.get('relations', []):
                target = relation['target']
                if target in block_map and block_map[target]['kind'] == 'unit':
                    edges.append({'from': ref, 'to': target, 'target': target,
                                  'category': 'relation', 'rel': relation['rel']})
        with self._lock:
            self._files, self._blocks, self._vectors, self._edges = (
                file_map, block_map, normalized_vectors, edges)

    @staticmethod
    def _resolve_target(ref: str, blocks: dict[str, dict[str, Any]]) -> str | None:
        if ref in blocks:
            return ref
        alias, start, end, seconds, until = _address(ref)
        candidates = []
        for target in blocks:
            other_alias, lo, hi, low, high = _address(target)
            if alias != other_alias:
                continue
            if start is not None and lo is not None and lo <= start and end <= hi:
                candidates.append((hi - lo, target))
            elif seconds is not None and low is not None and low <= seconds and until <= high:
                candidates.append((high - low, target))
            elif lo is None and low is None:
                candidates.append((math.inf, target))
        return min(candidates)[1] if candidates else None

    def vectors(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._vectors)

    def _public_block(self, ref: str) -> dict[str, Any]:
        block = copy.deepcopy(self._blocks[ref])
        block['relations'] = [relation for relation in block.get('relations', [])
                              if relation['target'] in self._blocks]
        return block

    def _unit_targets(self, scope: dict[str, Any] | None) -> dict[str, set[str]]:
        """Map vector objects to Units through facts, never through relations."""
        targets: dict[str, set[str]] = defaultdict(set)
        for ref, block in self._blocks.items():
            if block['kind'] != 'unit' or not matches_scope(block, scope):
                continue
            targets[ref].add(ref)
            pending = list(block.get('facts', []))
            seen: set[str] = set()
            while pending:
                fact = pending.pop()
                if fact in seen:
                    continue
                seen.add(fact)
                for candidate, media in self._blocks.items():
                    if media['kind'] == 'media' and _overlaps(fact, candidate):
                        targets[candidate].add(ref)
                target = self._resolve_target(fact, self._blocks)
                if target and self._blocks[target]['kind'] == 'detail':
                    pending.extend(self._blocks[target].get('facts', []))
        return targets

    def _fuse(self, rankings: list[list[tuple[str, float]]],
              targets: dict[str, set[str]], limit: int, offset: int) -> list[dict[str, Any]]:
        scores: dict[str, float] = defaultdict(float)
        matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
        media: dict[str, set[str]] = defaultdict(set)
        for query_index, ranking in enumerate(rankings):
            counted: set[str] = set()
            for rank, (ref, distance) in enumerate(ranking, 1):
                for unit in targets.get(ref, set()):
                    if self._blocks[ref]['kind'] == 'media':
                        media[unit].add(ref)
                    if unit in counted:
                        continue
                    counted.add(unit)
                    scores[unit] += 1 / (self.rrf_constant + rank)
                    matches[unit].append({'query_index': query_index, 'ref': ref,
                                          'rank': rank, 'distance': distance})
        ordered = sorted(scores, key=lambda ref: (-scores[ref], ref))
        return [{**self._public_block(ref), 'score': scores[ref],
                 'matched_media': sorted(media[ref]), 'matches': matches[ref]}
                for ref in ordered[offset:offset + limit]]

    def search(self, query_vectors: list[dict[str, Any]], scope: dict[str, Any] | None = None,
               limit: int = 12, offset: int = 0) -> list[dict[str, Any]]:
        if limit < 0 or offset < 0:
            raise ValueError('Search limit and offset must be nonnegative')
        queries = [_embedding(vector) for vector in query_vectors]
        with self._lock:
            targets = self._unit_targets(scope)
            rankings = []
            for query in queries:
                ranking = [(ref, _distance(query, vector)) for ref, vector in self._vectors.items()
                           if ref in targets and query['space_id'] == vector['space_id']]
                rankings.append(sorted(ranking, key=lambda item: (item[1], item[0])))
            return self._fuse(rankings, targets, limit, offset)

    def grep(self, query: str, scope: dict[str, Any] | None = None,
             layers: list[str] | None = None, limit: int = 12, offset: int = 0) -> list[dict[str, Any]]:
        layers = layers or ['unit']
        with self._lock:
            matches = [self._public_block(ref) for ref, block in sorted(self._blocks.items())
                       if (block['kind'] in layers or block.get('view') in layers)
                       and block['kind'] != 'media' and matches_scope(block, scope)
                       and query in block.get('body', block.get('text', ''))]
        return matches[offset:offset + limit]

    def links(self, ref: str, direction: str = 'both', relations: list[str] | None = None,
              hops: int = 1, category: str | None = 'relation') -> list[dict[str, Any]]:
        if direction not in ('in', 'out', 'both') or not 1 <= hops <= 32:
            raise ValueError('Invalid edge traversal direction or hops')
        with self._lock:
            found: list[dict[str, Any]] = []
            visited = {ref}
            frontier = {ref}
            for _ in range(hops):
                following: set[str] = set()
                for edge in self._edges:
                    if category and edge['category'] != category:
                        continue
                    if relations and edge['rel'] not in relations:
                        continue
                    if direction in ('out', 'both') and edge['from'] in frontier:
                        following.add(edge['to'])
                        if edge not in found:
                            found.append(copy.deepcopy(edge))
                    if direction in ('in', 'both') and edge['to'] in frontier:
                        following.add(edge['from'])
                        if edge not in found:
                            found.append(copy.deepcopy(edge))
                frontier = following - visited
                visited.update(following)
                if not frontier:
                    break
            return found

    def delete(self, refs: Iterable[str]) -> None:
        deleted = set(refs)
        with self._lock:
            for ref in deleted:
                self._blocks.pop(ref, None)
                self._vectors.pop(ref, None)
            self._edges = [edge for edge in self._edges
                           if edge['from'] not in deleted and edge['to'] not in deleted]

    def close(self) -> None:
        pass


from .postgres_index import PostgresIndex  # noqa: E402,F401
