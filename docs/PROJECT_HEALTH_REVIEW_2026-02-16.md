# Project Health Review (2026-02-16)

This review marks high-severity risks found in current `pixAV` implementation and tracks investigation status.

## HIGH Findings

- [x] `HIGH-01` Rate limiting implemented but not enabled on resolver app (`CLOSED 2026-02-16`)
  - Evidence:
    - Middleware exists and is attached in app factory: `src/pixav/strm_resolver/app.py`
    - Config value used to set RPM: `src/pixav/config.py:108`
  - Impact:
    - Resolver endpoints can be burst-abused without effective request throttling.

- [x] `HIGH-02` Coverage target (80%+) not enforced in CI (`CLOSED 2026-02-16`)
  - Evidence:
    - Target documented: `CLAUDE.md:132`
    - CI enforces coverage gate: `.github/workflows/ci.yml`
    - Current measured coverage: `81.53%`
  - Impact:
    - Regressions in critical paths can merge without failing CI.

- [x] `HIGH-03` Media-Loader fatal pre-check failures are returned but not persisted (`CLOSED 2026-02-16`)
  - Evidence:
    - Fatal pre-check path now persists failed state via repository updates: `src/pixav/media_loader/service.py`
  - Impact:
    - Task/video state can remain stale (e.g. still `downloading`) and failure reason is lost.

- [x] `HIGH-04` Media-Loader worker loop can exit on queue/parsing/runtime exceptions (`CLOSED 2026-02-16`)
  - Evidence:
    - Loop now wraps claim/parse/process with durable ack/nack and per-iteration exception handling: `src/pixav/media_loader/worker.py`
  - Impact:
    - One malformed queue payload or transient Redis error can stop the worker until manual restart.

- [x] `HIGH-05` Dispatch and state-transition are non-atomic, enabling duplicate processing (`CLOSED 2026-02-16`)
  - Evidence:
    - Orchestrator now claims pending task atomically before dispatch (`claim_for_dispatch`) and releases on failure: `src/pixav/maxwell_core/orchestrator.py`
    - Upload worker drops duplicate terminal-task payloads: `src/pixav/pixel_injector/worker.py`
  - Impact:
    - If state update fails after dispatch, same task can be re-dispatched and re-uploaded.

- [x] `HIGH-06` Queue consumption is non-durable (message loss risk on worker crash) (`CLOSED 2026-02-16`)
  - Evidence:
    - Queue now provides durable claim/ack/nack API (`pop_claim`, `ack`, `nack`, `requeue_inflight`): `src/pixav/shared/queue.py`
    - Runtime workers use claim/ack flow and startup inflight recovery: `src/pixav/media_loader/worker.py`, `src/pixav/pixel_injector/worker.py`, `src/pixav/maxwell_core/worker.py`
  - Impact:
    - If a worker crashes after `pop` but before durable state update/requeue, task payload can be permanently lost.

- [x] `HIGH-07` Pending task selection is not claim-locked for multi-orchestrator safety (`CLOSED 2026-02-16`)
  - Evidence:
    - Task repository now supports compare-and-set claim (`claim_for_dispatch`) and rollback (`release_dispatch_claim`): `src/pixav/shared/repository.py`
    - Orchestrator uses claim before dispatch to prevent concurrent duplicate claim: `src/pixav/maxwell_core/orchestrator.py`
  - Impact:
    - Multiple orchestrator instances can read and dispatch the same pending task concurrently.

## Additional Investigated Problem Points

- `INVESTIGATING` Critical workers under-tested:
  - `src/pixav/media_loader/worker.py` coverage `83%` (improved)
  - `src/pixav/pixel_injector/worker.py` coverage `54%`
  - `src/pixav/maxwell_core/worker.py` coverage `55%`

- `INVESTIGATING` Resolver CORS policy is broad (`allow_origins=["*"]` with credentials): `src/pixav/strm_resolver/middleware.py:68`

- `INVESTIGATING` qBittorrent client uses per-request cookies, already showing `httpx` deprecation warnings:
  - `src/pixav/media_loader/qbittorrent.py:110`
  - `src/pixav/media_loader/qbittorrent.py:135`
  - `src/pixav/media_loader/qbittorrent.py:182`

- `INVESTIGATING` Verify-stage contract drift:
  - `queue_verify` exists in config: `src/pixav/config.py:88`
  - `TaskState.VERIFYING` exists in enums: `src/pixav/shared/enums.py:16`
  - But no active producer/consumer path uses `pixav:verify` in runtime modules.

- `INVESTIGATING` Deployment topology drift:
  - Compose only runs `pixel_injector` worker from pipeline modules: `docker-compose.yml:98`
  - Yet project architecture/docs describe full staged microservice pipeline.

- `INVESTIGATING` Script/config drift:
  - `scripts/check_redis.py` defaults to passworded URL inconsistent with compose Redis.
  - `scripts/verify_e2e_pixel_injector.py` uses stale model fields (`source_url`, `size_bytes`, `type`) not present in current domain models.

- `INVESTIGATING` Codemap documentation drift:
  - `docs/CODEMAPS/infrastructure.md` lists migration files 001-004, but repo currently has only `migrations/001_initial_schema.sql`.
  - `docs/CODEMAPS/data-flow.md` queue producer/consumer mapping differs from current runtime orchestration.

## Suggested Remediation Order

1. `HIGH-06`, `HIGH-04`, `HIGH-03` (queue durability + worker resilience + state correctness)
2. `HIGH-07`, `HIGH-05` (task claim-locking + atomic dispatch/state + idempotency guard)
3. `HIGH-01` (enable resolver rate limiter)
4. `HIGH-02` (coverage gate in CI)
