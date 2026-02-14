# Pixel-Injector — Code Map

## Location
`src/pixav/pixel_injector/`

## Purpose
Upload media files to Google Photos via ephemeral Redroid (Android emulator) containers.

## Files

| File | Purpose |
|------|---------|
| `interfaces.py` | Protocols: `RedroidManager`, `FileUploader`, `UploadVerifier` |
| `service.py` | `PixelInjectorService` — orchestrates create→upload→verify→destroy |
| `worker.py` | BLPOP queue consumer on `pixav:upload` |
| `redroid.py` | `DockerRedroidManager` — Docker SDK container lifecycle (stub) |
| `adb.py` | `AdbConnection` — ADB bridge to Redroid (stub) |
| `uploader.py` | `UIAutomatorUploader` — uiautomator2 Google Photos automation (stub) |
| `verifier.py` | `GooglePhotosVerifier` — share URL validation (stub) |

## Flow
```
Queue (BLPOP) → worker → service.process_task()
  → redroid.create() → redroid.wait_ready()
  → uploader.push_file() → uploader.trigger_upload()
  → verifier.wait_for_share_url() → verifier.validate_share_url()
  → redroid.destroy()
```

## Key Design
- Redroid containers are **ephemeral** — created per task, destroyed after
- All dependencies injected via Protocol interfaces (enables full TDD with mocks)
- Container is always destroyed, even on failure (try/finally in service)
