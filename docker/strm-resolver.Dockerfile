FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --no-cache-dir uv

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Sync runtime dependencies only (exclude local dev/test groups)
RUN uv sync --frozen --no-group dev --no-group embeddings

# Run the STRM resolver API
CMD ["uv", "run", "--no-sync", "uvicorn", "pixav.strm_resolver.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
