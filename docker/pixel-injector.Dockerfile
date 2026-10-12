FROM python:3.12-slim

# Install uv and system dependencies
RUN pip install --no-cache-dir uv && \
    apt-get update && \
    apt-get install -y --no-install-recommends android-tools-adb ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# uv may select the project Python version instead of the base interpreter.
# Runtime services run as the host UID and cannot traverse /root.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python

# Copy project files
COPY . .
RUN chmod -R a+rX /app/src /app/scripts && chmod a+r /app/pyproject.toml /app/uv.lock

# Sync runtime dependencies only (exclude local dev/test groups)
RUN uv sync --frozen --no-group dev --no-group embeddings

# Run the pixel injector worker
CMD ["uv", "run", "--no-sync", "python", "-m", "pixav.pixel_injector.worker"]
