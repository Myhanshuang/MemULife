-- Authoritative control data. Index rebuilds MUST NOT drop these tables.
CREATE TABLE IF NOT EXISTS memu_repository (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    root text NOT NULL,
    repository_id text NOT NULL,
    content_version bigint NOT NULL DEFAULT 0,
    search_version bigint NOT NULL DEFAULT 0
);
ALTER TABLE memu_repository ADD COLUMN IF NOT EXISTS grep_version bigint NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS memu_catalog (
    alias text PRIMARY KEY,
    path text NOT NULL UNIQUE,
    kind text NOT NULL,
    revision text NOT NULL,
    payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS memu_control (
    namespace text NOT NULL,
    key text NOT NULL,
    value jsonb NOT NULL,
    PRIMARY KEY(namespace,key)
);
CREATE INDEX IF NOT EXISTS memu_catalog_kind ON memu_catalog(kind);
CREATE INDEX IF NOT EXISTS memu_write_state ON memu_control((value->>'state'), (value->>'retry_at'))
    WHERE namespace='writes';
CREATE INDEX IF NOT EXISTS memu_conversation_order ON memu_control((value->>'conversation'), (value->>'position'), key)
    WHERE namespace='conversation_turns';
CREATE TABLE IF NOT EXISTS memu_write_inputs (
    request_id text NOT NULL, alias text NOT NULL, PRIMARY KEY(request_id,alias)
);
CREATE INDEX IF NOT EXISTS memu_write_alias ON memu_write_inputs(alias,request_id);
CREATE INDEX IF NOT EXISTS memu_import_lookup ON memu_catalog
    ((payload->'metadata'->'memulife'->>'source_instance_id'),
     (payload->'metadata'->'memulife'->>'object_id')) WHERE kind='source';
CREATE TABLE IF NOT EXISTS memu_counters (kind text PRIMARY KEY, value bigint NOT NULL);
CREATE TABLE IF NOT EXISTS memu_commits (
    id text PRIMARY KEY,
    files jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
