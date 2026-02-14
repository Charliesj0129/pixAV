---
name: postgres-patterns
description: PostgreSQL database patterns for query optimization, schema design, and indexing.
---

# PostgreSQL Patterns

Best practices for `pixAV`'s PostgreSQL database (accessed via `asyncpg`/`SQLAlchemy`).

## Indexing Strategy

- **B-tree**: Default for equality/range (`id = 1`, `created_at > '2024-01-01'`).
- **GIN**: JSONB columns (`metadata @> '{"type": "video"}'`).
- **Composite**: For multi-column queries (`WHERE status = 'pending' AND priority > 10`).

## Schema Design

- **IDs**: Use `BIGINT` (Identity) or `UUID`.
- **JSONB**: Use for flexible schema (e.g., `media_metadata`), but prefer columns for queryable fields.
- **Foreign Keys**: Always define FK constraints for integrity.

## Query Performance

- **Pagination**: Prefer **Cursor-based** (`WHERE id > last_seen_id ORDER BY id LIMIT 20`) over Offset (`OFFSET 10000`).
- **Update with Return**: `UPDATE tasks SET status='running' ... RETURNING id`.
- **Bulk Insert**: Use `executemany` or `COPY` for large datasets.

## Queue Table Pattern (Maxwell)

For implementing valid persistent queues in Postgres:

```sql
UPDATE tasks
SET status = 'processing', worker_id = $1, started_at = NOW()
WHERE id = (
  SELECT id
  FROM tasks
  WHERE status = 'pending'
  ORDER BY priority DESC, created_at ASC
  LIMIT 1
  FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

_Note: `SKIP LOCKED` is crucial for concurrency._
