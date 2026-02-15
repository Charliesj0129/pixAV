# Advanced Hybrid Search Functionality

This feature implements a state-of-the-art search engine combining:

1.  **Semantic Search (`pgvector`)**:
    - Uses `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`.
    - Understands query meaning (e.g. "office lady" finds related videos without exact word match).
    - 384-dimensional vector space.

2.  **Keyword Search (`pg_trgm`)**:
    - Uses PostgreSQL `tsvector` and `pg_trgm`.
    - Ensures exact matches for codes (`IPX-123`) and names.

3.  **Reciprocal Rank Fusion (RRF)**:
    - Combines results effectively: `score = 1/(k + vector_rank) + 1/(k + keyword_rank)`.

## How to Verify

### 1. Wait for Dependency Download

The first run of any script using `EmbeddingService` (like `migrate.py` or the demo) will triggers a download of:

- `torch` (~800MB)
- `sentence-transformers` model (~400MB)

### 2. Run Interactive Demo

Once installed:

```bash
uv run python scripts/demo_hybrid_search.py
```

This script will:

- Seed sample videos (if DB is empty).
- Let you type queries.
- Show RRF-ranked results.

### 3. Backfill Existing Videos

To generate embeddings for your existing library:

```bash
uv run python scripts/backfill_embeddings.py
```
