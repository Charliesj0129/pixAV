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
| `pixav:verify` | Pixel-Injector | Maxwell-Core | `{task_id, video_id, share_url}` |

## State Transitions

### Video Status
```
discovered → downloading → downloaded → uploading → available
                                                  ↘ expired
         any state → failed
```

### Task State
```
pending → downloading → remuxing → uploading → verifying → complete
       any state → failed
```

## PostgreSQL as SSOT

All durable state lives in PostgreSQL. Redis queues are transient work buffers.
Modules read/write DB directly for state updates, use queues only for task dispatch.
