"""One opt-in smoke test. MEMU_TEST_POSTGRES_DSN must name a disposable database."""

import os
from copy import deepcopy

import pytest

from memu.index import PostgresIndex
from test_index import embedding, fixture


@pytest.mark.skipif(not os.environ.get('MEMU_TEST_POSTGRES_DSN'),
                    reason='A disposable PostgreSQL database with pgvector was not configured')
def test_postgres_native_media_and_reverse_links():
    index = PostgresIndex(os.environ['MEMU_TEST_POSTGRES_DSN'])
    from psycopg.errors import NotNullViolation

    files, blocks, vectors = fixture(index)
    with index._connect() as connection:
        assert connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
        tables = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
        ).fetchall()
        assert {'files', 'blocks', 'links'} <= {row['tablename'] for row in tables}
        indexes = connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
            "AND tablename='blocks' AND indexdef LIKE '%USING hnsw%'"
        ).fetchall()
        assert indexes

    result = index.search([embedding([1, 0])], {'topics': ['memory']})[0]
    assert result['ref'] == 'u1:2'
    assert {'a1@0-5', 'a1@5-10'} <= set(result['matched_media'])
    assert index.grep('已测试', {'sources': ['agent1']})[0]['ref'] == 'u1:2'
    assert index.links('u1:2', direction='in')[0]['from'] == 'u1:3'

    # Fail inside the SQL transaction, after DELETE has run, to check rollback.
    invalid_blocks = deepcopy(blocks)
    invalid_blocks[0]['source'] = None
    with pytest.raises(NotNullViolation):
        index.sync(files, invalid_blocks, vectors)
    assert set(index.vectors()) == set(vectors)
    assert index.grep('已测试')[0]['ref'] == 'u1:2'

    index.delete(['u1:2'])
    assert index.links('u1:3') == []
    assert 'u1:2' not in index.vectors()
