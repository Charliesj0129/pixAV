# ADR-001: Rust + Python Hybrid Architecture

## Status
Accepted

## Context
pixAV is a distributed media pipeline with five modules that communicate via Redis queues. We need to balance rapid prototyping with eventual performance requirements:

- **SHT-Probe** (crawler) — I/O bound, benefits from Rust's async ecosystem
- **Media-Loader** (download+remux) — CPU bound FFmpeg, orchestration is simple
- **Pixel-Injector** (upload) — Depends on uiautomator2 (Python-only), Docker SDK
- **Maxwell-Core** (orchestrator) — Scheduling logic, moderate complexity
- **Strm-Resolver** (proxy) — Low-latency HTTP, benefits from Rust

## Decision
**Start all modules as Python prototypes.** Rewrite to Rust only when:

1. The Redis queue contract (message schema, error handling, retry semantics) is proven stable through production use
2. Performance profiling identifies the module as a bottleneck
3. The module does NOT depend on Python-only libraries (e.g., uiautomator2)

Rewrite candidates (in priority order):
1. **SHT-Probe** — High request volume, Rust async is natural fit
2. **Maxwell-Core** — Scheduling hot path, Rust's type system helps correctness
3. **Media-Loader** — Orchestration only; FFmpeg is already native

**Pixel-Injector stays Python permanently** due to uiautomator2 dependency.
**Strm-Resolver stays Python** unless latency benchmarks justify rewrite.

## Consequences
- **Easier**: Rapid iteration on queue contracts, single language for initial team
- **Easier**: Protocol-based interfaces mean Rust modules are drop-in replacements
- **Harder**: Maintaining two language ecosystems once rewrites begin
- **Harder**: Rust developers must understand the Python prototype to rewrite correctly
