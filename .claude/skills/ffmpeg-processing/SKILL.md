---
name: ffmpeg-processing
description: Expert recipes for FFmpeg video processing, transcoding, HLS streaming, and hardware acceleration.
---

# FFmpeg Processing

Standardized recipes for `pixAV` media processing.

## General Principles

- **Stream Copy**: Use `-c copy` whenever possible to avoid re-encoding.
- **Containers**: Prefer MKV for intermediate storage, MP4/HLS for delivery.
- **Audio**: AAC (High Quality) or Opus.

## Hardware Acceleration

- **NVIDIA (NVENC)**: `-c:v h264_nvenc` / `-c:v hevc_nvenc`.
- **Intel (QSV)**: `-c:v h264_qsv`.
- **Software (CPU)**: `-c:v libx264 -preset medium`.

## Common Recipes

### 1. Transcode to H.264 (Compatible)

```bash
ffmpeg -i input.mkv \
  -c:v libx264 -preset slow -crf 22 \
  -c:a aac -b:a 192k \
  -movflags +faststart \
  output.mp4
```

### 2. Extract Audio

```bash
ffmpeg -i input.mkv -vn -c:a copy audio.mka
```

### 3. Generate HLS Playlist (Streaming)

```bash
ffmpeg -i input.mp4 \
  -c:v copy -c:a copy \
  -f hls -hls_time 6 -hls_playlist_type vod \
  -hls_segment_filename "segment_%03d.ts" \
  index.m3u8
```

### 4. Hardware Decode + Encode (NVENC)

```bash
ffmpeg -hwaccel cuda -i input.mkv \
  -c:v hevc_nvenc -preset p7 -cq 20 \
  -c:a copy \
  output.mp4
```

## Python Integration

- Use `asyncio.create_subprocess_exec`.
- Parse `stderr` for progress markup (FFmpeg writes logs to stderr).
