FROM python:3.11-slim-bookworm

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    ffmpeg \
    android-tools-adb \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
RUN pip install uv

# Set up work directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install runtime dependencies only (exclude local dev/test groups)
RUN uv sync --frozen --no-group dev --no-group embeddings

# Copy source code
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY migrations/ ./migrations/

# Ensure python path includes src
ENV PYTHONPATH=/app/src:/app

# Default command
CMD ["bash"]
