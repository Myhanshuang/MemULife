import copy

import pytest

from memu.index import InMemoryIndex, VectorSpaceError


def embedding(vector, space='native'):
    return {'vector': vector, 'space_id': space, 'model_version': 'test-v1',
            'preprocess_version': 'raw-v1', 'metric': 'cosine'}


def fixture(index):
    files = [{'alias': alias, 'path': path, 'kind': kind, 'revision': 'rev1'}
             for alias, path, kind in [('u1', '2026/11/10/units.md', 'unit'),
                                       ('a1', '2026/11/13/source/clip.mp4', 'media'),
                                       ('s1', '2026/11/13/detail.md', 'detail')]]
    blocks = [
        {'ref': 'u1:2', 'kind': 'unit', 'body': '用户已测试 B', 'source': 'agent1',
         'topics': ['memory'], 'facts': ['a1@0-10'], 'relations': []},
        {'ref': 'u1:3', 'kind': 'unit', 'body': '用户提出项目计划', 'source': 'agent2',
         'topics': ['project'], 'facts': ['s1:2'],
         'relations': [{'rel': '相关', 'target': 'u1:2'}]},
        {'ref': 's1:2', 'kind': 'detail', 'body': '原始视频文字入口', 'facts': ['a1@10-20']},
        {'ref': 'a1', 'kind': 'media', 'duration': 20, 'body': ''},
    ]
    vectors = {'u1:2': embedding([0, 1]), 'u1:3': embedding([1, 0]),
               'a1@0-5': embedding([1, 0]), 'a1@5-10': embedding([0.9, 0.1]),
               'a1@10-20': embedding([0.7, 0.3])}
    index.sync(files, blocks, vectors)
    return files, blocks, vectors


def test_media_rank_fusion_counts_best_rank_once_and_filters_before_ranking():
    index = InMemoryIndex(rrf_constant=17)
    fixture(index)
    results = index.search([embedding([1, 0]), embedding([0, 1])])
    unit = next(row for row in results if row['ref'] == 'u1:2')
    assert len(unit['matches']) == 2
    assert unit['score'] == pytest.approx(sum(1 / (17 + match['rank']) for match in unit['matches']))
    assert {'a1@0-5', 'a1@5-10'} <= set(unit['matched_media'])
    assert 'a1@10-20' not in unit['matched_media']
    assert [row['ref'] for row in index.search([embedding([1, 0])], {'topics': ['memory']})] == ['u1:2']
    assert index.search([embedding([1, 0], 'incompatible')]) == []
    assert index.search([embedding([1, 0])], {'directory': '2026/11/13'}) == []
    assert index.grep('已测试', {'sources': ['agent1']})[0]['ref'] == 'u1:2'


def test_atomic_sync_rejects_invalid_spaces_and_only_embeds_native_objects():
    index = InMemoryIndex()
    files, blocks, vectors = fixture(index)
    changed = copy.deepcopy(vectors)
    changed['u1:3'] = embedding([1, 2, 3])
    with pytest.raises(VectorSpaceError):
        index.sync(files, blocks, changed)
    assert index.vectors() == vectors
    changed = {**vectors, 's1:2': embedding([1, 0])}
    with pytest.raises(ValueError, match='Only Unit'):
        index.sync(files, blocks, changed)
    with pytest.raises(VectorSpaceError):
        index.search([embedding([1, 2, 3])])
    assert len(index.search([embedding([1, 0])])) == 2


def test_deletion_removes_both_edge_directions_without_deleting_related_events():
    index = InMemoryIndex()
    fixture(index)
    assert index.links('u1:2', direction='in')[0]['from'] == 'u1:3'
    index.delete(['u1:2'])
    assert index.links('u1:3') == []
    assert 'u1:2' not in index.vectors()
    assert [row['ref'] for row in index.search([embedding([1, 0])])] == ['u1:3']
    assert index.search([embedding([1, 0])])[0]['matched_media'] == ['a1@10-20']
