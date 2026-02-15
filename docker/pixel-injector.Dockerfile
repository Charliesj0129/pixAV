FROM python:3.12-slim

# Install uv and system dependencies
RUN pip install --no-cache-dir uv && \
    apt-get update && \
    apt-get install -y --no-install-recommends android-tools-adb && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy project files
COPY . .

# Sync dependencies
RUN uv sync

# Run the pixel injector worker
CMD ["uv", "run", "python", "-m", "pixav.pixel_injector.worker"]
