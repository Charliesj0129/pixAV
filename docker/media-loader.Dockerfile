FROM python:3.12-slim

# Install uv and system dependencies.
# FFmpeg is required by media_loader.remuxer.FFmpegRemuxer.
RUN pip install --no-cache-dir uv && \
    apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Sync runtime dependencies only (exclude local dev/test groups)
RUN uv sync --frozen --no-group dev --no-group embeddings

# Run the Media-Loader worker
CMD ["uv", "run", "--no-sync", "python", "-m", "pixav.media_loader.worker"]
