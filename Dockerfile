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

# Install python dependencies (including dev)
RUN uv sync --frozen

# Copy source code
COPY src/ ./src/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
COPY config.py ./config.py

# Ensure python path includes src
ENV PYTHONPATH=/app/src:/app

# Default command
CMD ["bash"]
