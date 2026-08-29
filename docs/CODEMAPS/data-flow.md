# Data Flow — Code Map

## End-to-End Pipeline

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐     ┌──────────────┐     ┌───────────────┐
│  SHT-Probe  │────▶│ Media-Loader │────▶│ Pixel-Injector  │────▶│ Maxwell-Core │────▶│ Strm-Resolver │
│  (crawler)  │     │ (download)   │     │ (upload)        │     │ (orchestrate)│     │ (playback)    │
└─────────────┘     └──────────────┘     └─────────────────┘     └──────────────┘     └───────────────┘
```

## Redis Queues

| Queue Name | Producer | Consumer | Payload |
|-----------|----------|----------|---------|
| `pixav:crawl` | SHT-Probe | Media-Loader | `{video_id, magnet_uri}` |
| `pixav:download` | Maxwell-Core | Media-Loader | `{task_id, video_id, magnet_uri}` |
| `pixav:upload` | Maxwell-Core | Pixel-Injector | `{task_id, video_id, local_path, account_id}` |

`verifying` remains a task-state concept, but verification currently runs inline inside `pixel_injector` (no dedicated `pixav:verify` queue in runtime modules).

## State Transitions

### Video Status
```
discovered → downloading → downloaded → uploading → available
                                                  ↘ expired
         any state → failed
```

### Task State
```
pending → dispatched → downloading → remuxing → uploading → verifying → complete
       any state → failed
```

Notes:
- `dispatched` means claimed/enqueued by `maxwell_core`, but not yet started by a worker.
- In current runtime, the upload worker performs verification inline and typically persists `uploading → complete` without routing through a separate verify queue.

## PostgreSQL as SSOT

All durable state lives in PostgreSQL. Redis queues are transient work buffers.
Modules read/write DB directly for state updates, use queues only for task dispatch.
