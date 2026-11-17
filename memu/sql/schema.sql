-- The repository is authoritative. This schema contains only rebuildable indexes.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS files (
    id bigserial PRIMARY KEY,
    alias text NOT NULL UNIQUE,
    path text NOT NULL UNIQUE,
    kind text NOT NULL,
    revision text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS blocks (
    id bigserial PRIMARY KEY,
    file_id bigint NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ref text NOT NULL UNIQUE,
    kind text NOT NULL,
    start_line integer,
    end_line integer,
    start_seconds double precision,
    end_seconds double precision,
    event_time jsonb NOT NULL DEFAULT '{"precision":"unknown","value":null}'::jsonb,
    source text NOT NULL DEFAULT '',
    topics text[] NOT NULL DEFAULT '{}',
    body text NOT NULL DEFAULT '',
    payload jsonb NOT NULL,
    embedding vector,
    model_version text,
    space_id text,
    preprocess_version text,
    metric text,
    CHECK (start_line IS NULL OR (start_line > 0 AND end_line >= start_line)),
    CHECK (start_seconds IS NULL OR
        (start_seconds >= 0 AND end_seconds > start_seconds)),
    CHECK (embedding IS NULL OR (kind IN ('unit', 'media') AND
        model_version IS NOT NULL AND space_id IS NOT NULL AND
        preprocess_version IS NOT NULL AND metric IN ('cosine', 'l2', 'inner_product')))
);
CREATE UNIQUE INDEX IF NOT EXISTS blocks_file_line
    ON blocks(file_id, start_line) WHERE start_line IS NOT NULL;
ALTER TABLE blocks ADD COLUMN IF NOT EXISTS content_hash text;
CREATE UNIQUE INDEX IF NOT EXISTS blocks_media_segment
    ON blocks(file_id, start_seconds, end_seconds) WHERE start_seconds IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS blocks_file_entry
    ON blocks(file_id) WHERE start_line IS NULL AND start_seconds IS NULL;
CREATE INDEX IF NOT EXISTS blocks_source ON blocks(source);
CREATE INDEX IF NOT EXISTS blocks_topics ON blocks USING gin(topics);
CREATE INDEX IF NOT EXISTS blocks_space ON blocks(space_id);
CREATE INDEX IF NOT EXISTS blocks_body_trgm ON blocks USING gin(body gin_trgm_ops);

CREATE TABLE IF NOT EXISTS links (
    from_id bigint NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    to_id bigint NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    category text NOT NULL CHECK (category IN ('fact', 'relation')),
    rel text NOT NULL DEFAULT '',
    target_ref text NOT NULL,
    PRIMARY KEY (from_id, to_id, category, rel, target_ref)
);
CREATE INDEX IF NOT EXISTS links_from_rel ON links(from_id, rel);
CREATE INDEX IF NOT EXISTS links_to_rel ON links(to_id, rel);
CREATE TABLE IF NOT EXISTS reference_edges (
    owner_ref text NOT NULL REFERENCES blocks(ref) ON DELETE CASCADE,
    target_ref text NOT NULL,
    target_alias text NOT NULL,
    start_line integer, end_line integer,
    start_seconds double precision, end_seconds double precision,
    category text NOT NULL, rel text NOT NULL DEFAULT '',
    PRIMARY KEY(owner_ref,target_ref,category,rel)
);
CREATE INDEX IF NOT EXISTS reference_targets ON reference_edges(target_alias);
CREATE TABLE IF NOT EXISTS unit_vector_targets (
    unit_ref text NOT NULL REFERENCES blocks(ref) ON DELETE CASCADE,
    vector_ref text NOT NULL REFERENCES blocks(ref) ON DELETE CASCADE,
    PRIMARY KEY(unit_ref,vector_ref)
);
CREATE INDEX IF NOT EXISTS vector_unit_targets ON unit_vector_targets(vector_ref,unit_ref);
CREATE TABLE IF NOT EXISTS vector_spaces (
    space_id text PRIMARY KEY,
    dimensions integer NOT NULL, metric text NOT NULL,
    model_version text NOT NULL, preprocess_version text NOT NULL
);
-- Dimension-specific HNSW indexes, partitioned by space_id, are installed by
-- PostgresIndex.sync in the same transaction that publishes the new mirror.
